"""Interactive, idempotent AWS Organizations onboarding for the PRM tagger."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import BotoCoreError, ClientError

STACKSET_NAME = "PRM-Resource-Tagging"
ROLE_NAME = "PRM-TaggingRole"
STACKSET_SERVICE_PRINCIPAL = "member.org.stacksets.cloudformation.amazonaws.com"
TERMINAL_OPERATION_STATUSES = {"SUCCEEDED", "FAILED", "STOPPED"}


@dataclass(frozen=True)
class OrganizationInventory:
    management_account_id: str
    root_ids: list[str]
    account_ids: list[str]
    ou_ids: list[str]


def _error_code(error: Exception) -> str:
    if isinstance(error, ClientError):
        return str(error.response.get("Error", {}).get("Code", ""))
    return ""


def _pages(client, operation: str, **kwargs):
    if client.can_paginate(operation):
        yield from client.get_paginator(operation).paginate(**kwargs)
    else:
        yield getattr(client, operation)(**kwargs)


def _discover_ous(organizations, parent_ids: list[str]) -> list[str]:
    discovered: list[str] = []
    pending = list(parent_ids)
    while pending:
        parent_id = pending.pop(0)
        children: list[str] = []
        for page in _pages(organizations, "list_organizational_units_for_parent", ParentId=parent_id):
            children.extend(ou["Id"] for ou in page.get("OrganizationalUnits", []))
        discovered.extend(children)
        pending.extend(children)
    return discovered


def inspect_organization(session, caller_account_id: str) -> OrganizationInventory | None:
    """Return inventory only when the caller is the Organizations management account."""
    organizations = session.client("organizations")
    try:
        organization = organizations.describe_organization()["Organization"]
    except ClientError as error:
        if _error_code(error) == "AWSOrganizationsNotInUseException":
            return None
        raise RuntimeError(f"Unable to inspect AWS Organizations: {error}") from error

    management_id = organization.get("ManagementAccountId") or organization.get("MasterAccountId")
    if not management_id:
        raise RuntimeError("AWS Organizations did not return a management account ID.")
    if caller_account_id != management_id:
        raise RuntimeError(
            "This account belongs to AWS Organizations but is not the management account. "
            "Run bootstrap from the management account; no infrastructure was changed."
        )

    root_ids: list[str] = []
    for page in _pages(organizations, "list_roots"):
        root_ids.extend(root["Id"] for root in page.get("Roots", []))

    account_ids: list[str] = []
    for page in _pages(organizations, "list_accounts"):
        account_ids.extend(
            account["Id"]
            for account in page.get("Accounts", [])
            if account.get("Id") != management_id and (account.get("State") or account.get("Status")) == "ACTIVE"
        )

    if not root_ids:
        raise RuntimeError("No AWS Organizations root was returned; no infrastructure was changed.")
    return OrganizationInventory(management_id, root_ids, account_ids, _discover_ous(organizations, root_ids))


def _load_onboarding_config(path: Path) -> tuple[str, str, list[str]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    organization = data.get("organization") or {}
    role_name = str(organization.get("role_name", ROLE_NAME))
    external_id = str(organization.get("external_id", "PRMTaggingAutomation"))
    if role_name != ROLE_NAME:
        raise ValueError(f"organization.role_name must be {ROLE_NAME!r} for automated onboarding.")
    if len(external_id) < 2:
        raise ValueError("organization.external_id must contain at least two characters.")
    excluded = [str(account_id) for account_id in organization.get("exclude_accounts", [])]
    if any(len(account_id) != 12 or not account_id.isdigit() for account_id in excluded):
        raise ValueError("organization.exclude_accounts entries must be 12-digit AWS account IDs.")
    return role_name, external_id, excluded


def _stackset_exists(cloudformation) -> bool:
    try:
        cloudformation.describe_stack_set(StackSetName=STACKSET_NAME, CallAs="SELF")
        return True
    except ClientError as error:
        if _error_code(error) == "StackSetNotFoundException":
            return False
        raise


def _organizations_access_enabled(cloudformation) -> bool:
    try:
        response = cloudformation.describe_organizations_access(CallAs="SELF")
        return response.get("Status") == "ENABLED"
    except ClientError as error:
        if _error_code(error) in {"OrganizationsAccessNotConfiguredException", "AccessDenied"}:
            return False
        raise


def _wait_for_operation(cloudformation, operation_id: str, sleep: Callable[[float], None]) -> None:
    while True:
        operation = cloudformation.describe_stack_set_operation(
            StackSetName=STACKSET_NAME, OperationId=operation_id, CallAs="SELF"
        )["StackSetOperation"]
        status = operation["Status"]
        if status in TERMINAL_OPERATION_STATUSES:
            if status != "SUCCEEDED":
                reason = operation.get("StatusReason", "no status reason returned")
                raise RuntimeError(f"StackSet operation {operation_id} ended as {status}: {reason}")
            return
        sleep(5)


def _wait_for_organizations_access(cloudformation, sleep: Callable[[float], None]) -> None:
    for _ in range(60):
        if _organizations_access_enabled(cloudformation):
            return
        sleep(5)
    raise RuntimeError("Timed out waiting for CloudFormation StackSets trusted access to become enabled.")


def _instance_summaries(cloudformation) -> list[dict]:
    summaries: list[dict] = []
    for page in _pages(cloudformation, "list_stack_instances", StackSetName=STACKSET_NAME, CallAs="SELF"):
        summaries.extend(page.get("Summaries", []))
    return summaries


def _print_plan(
    inventory: OrganizationInventory,
    region: str,
    stackset_exists: bool,
    access_enabled: bool,
    excluded_account_ids: list[str],
    output: Callable[[str], None],
) -> None:
    output("")
    output("AWS CHANGE PLAN")
    output(f"  Management account: {inventory.management_account_id}")
    output(f"  Active member accounts discovered: {len(inventory.account_ids)}")
    output(f"  Member accounts excluded by configuration: {len(excluded_account_ids)}")
    output(f"  Organizational units discovered: {len(inventory.ou_ids)}")
    output(f"  Deployment targets (Organization roots): {', '.join(inventory.root_ids)}")
    output(f"  Administration/deployment Region: {region}")
    output(f"  Organizations trusted access: {'already enabled' if access_enabled else 'will be enabled'}")
    output(f"  StackSet {STACKSET_NAME}: {'will be updated' if stackset_exists else 'will be created'}")
    output(f"  IAM role in member accounts: {ROLE_NAME}")
    output("  Organization structure: unchanged; no accounts or OUs will be moved")
    output("  PRM tags: not applied; audit/apply are never run by bootstrap")
    output("")


def provision_organization(
    session,
    inventory: OrganizationInventory,
    template_body: str,
    role_name: str,
    external_id: str,
    region: str,
    *,
    confirm: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    excluded_account_ids: list[str] | None = None,
) -> bool:
    cloudformation = session.client("cloudformation", region_name=region)
    exists = _stackset_exists(cloudformation)
    access_enabled = _organizations_access_enabled(cloudformation)
    excluded = sorted(set(excluded_account_ids or []).intersection(inventory.account_ids))
    _print_plan(inventory, region, exists, access_enabled, excluded, output)
    if confirm("Type 'provision' to authorize these AWS changes: ").strip().lower() != "provision":
        output("Provisioning declined. No AWS infrastructure changes were made.")
        return False

    if not access_enabled:
        cloudformation.activate_organizations_access()
        _wait_for_organizations_access(cloudformation, sleep)

    parameters = [
        {"ParameterKey": "ManagementAccountId", "ParameterValue": inventory.management_account_id},
        {"ParameterKey": "RoleName", "ParameterValue": role_name},
        {"ParameterKey": "ExternalId", "ParameterValue": external_id},
    ]
    common = {
        "StackSetName": STACKSET_NAME,
        "TemplateBody": template_body,
        "Parameters": parameters,
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
        "PermissionModel": "SERVICE_MANAGED",
        "AutoDeployment": {"Enabled": True, "RetainStacksOnAccountRemoval": False},
        "CallAs": "SELF",
    }
    if exists:
        try:
            response = cloudformation.update_stack_set(**common)
            if response.get("OperationId"):
                _wait_for_operation(cloudformation, response["OperationId"], sleep)
        except ClientError as error:
            if "didn't contain changes" not in str(error) and "No updates are to be performed" not in str(error):
                raise
    else:
        cloudformation.create_stack_set(**common)

    targets: dict[str, object] = {"OrganizationalUnitIds": inventory.root_ids}
    if excluded:
        targets.update({"Accounts": excluded, "AccountFilterType": "DIFFERENCE"})
    instances = _instance_summaries(cloudformation)
    operation = (
        cloudformation.update_stack_instances(
            StackSetName=STACKSET_NAME,
            DeploymentTargets=targets,
            Regions=[region],
            OperationPreferences={"FailureTolerancePercentage": 0, "MaxConcurrentPercentage": 100},
            CallAs="SELF",
        )
        if instances
        else cloudformation.create_stack_instances(
            StackSetName=STACKSET_NAME,
            DeploymentTargets=targets,
            Regions=[region],
            OperationPreferences={"FailureTolerancePercentage": 0, "MaxConcurrentPercentage": 100},
            CallAs="SELF",
        )
    )
    _wait_for_operation(cloudformation, operation["OperationId"], sleep)

    failures = []
    for summary in _instance_summaries(cloudformation):
        detailed = summary.get("StackInstanceStatus", {}).get("DetailedStatus")
        if summary.get("Status") != "CURRENT" or (detailed and detailed != "SUCCEEDED"):
            failures.append(summary)
    if failures:
        output("Stack instance failures: " + json.dumps(failures, default=str))
        raise RuntimeError(f"{len(failures)} Stack Instance(s) did not reach CURRENT/SUCCEEDED.")

    output(f"StackSet completed successfully for {len(inventory.account_ids)} active member account(s).")
    return True


def _region(session) -> str:
    region = session.region_name or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if not region:
        raise RuntimeError("No AWS Region is configured. Set AWS_REGION or run 'aws configure set region REGION'.")
    return region


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare AWS Organizations infrastructure for AWS PRM tagging.")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        session = boto3.Session()
        identity = session.client("sts").get_caller_identity()
        caller_account = identity["Account"]
        print(f"AWS identity: {identity.get('Arn', caller_account)}")
        inventory = inspect_organization(session, caller_account)
        if inventory is None:
            print("AWS Organizations is not in use. Single-account setup is complete; no AWS infrastructure changes are needed.")
            return 0
        role_name, external_id, excluded_account_ids = _load_onboarding_config(args.config)
        provision_organization(
            session,
            inventory,
            args.template.read_text(encoding="utf-8"),
            role_name,
            external_id,
            _region(session),
            excluded_account_ids=excluded_account_ids,
        )
        return 0
    except (BotoCoreError, ClientError, OSError, TypeError, ValueError, RuntimeError) as error:
        print(f"ERROR: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
