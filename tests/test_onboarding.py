from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from bs4it_tagging.onboarding import (
    OrganizationInventory,
    inspect_organization,
    main,
    provision_organization,
)


def _client_error(code: str, message: str = "test") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "TestOperation")


def _session(mocker, *, organizations=None, cloudformation=None, sts=None, region="us-east-1"):
    session = mocker.MagicMock(region_name=region)

    def client(name, **kwargs):
        return {"organizations": organizations, "cloudformation": cloudformation, "sts": sts}[name]

    session.client.side_effect = client
    return session


def _inventory() -> OrganizationInventory:
    return OrganizationInventory("000000000001", ["r-example"], ["000000000002"], ["ou-example-one"])


def _cloudformation(mocker, *, exists=False, initial_instances=None, final_instances=None):
    client = mocker.MagicMock()
    client.can_paginate.return_value = False
    if exists:
        client.describe_stack_set.return_value = {"StackSet": {"StackSetName": "PRM-Resource-Tagging"}}
    else:
        client.describe_stack_set.side_effect = _client_error("StackSetNotFoundException")
    client.describe_organizations_access.return_value = {"Status": "ENABLED"}
    client.list_stack_instances.side_effect = [
        {"Summaries": initial_instances or []},
        {"Summaries": final_instances or [{"Status": "CURRENT", "StackInstanceStatus": {"DetailedStatus": "SUCCEEDED"}}]},
    ]
    client.create_stack_instances.return_value = {"OperationId": "instances-create"}
    client.update_stack_instances.return_value = {"OperationId": "instances-update"}
    client.update_stack_set.return_value = {"OperationId": "stackset-update"}
    client.describe_stack_set_operation.return_value = {"StackSetOperation": {"Status": "SUCCEEDED"}}
    return client


def test_single_account_main_does_not_create_infrastructure(mocker, tmp_path: Path):
    sts = mocker.MagicMock()
    sts.get_caller_identity.return_value = {"Account": "000000000001", "Arn": "arn:aws:iam::000000000001:user/test"}
    organizations = mocker.MagicMock()
    organizations.describe_organization.side_effect = _client_error("AWSOrganizationsNotInUseException")
    session = _session(mocker, organizations=organizations, sts=sts)
    mocker.patch("bs4it_tagging.onboarding.boto3.Session", return_value=session)
    template = tmp_path / "template.yaml"
    config = tmp_path / "config.yaml"
    template.write_text("template", encoding="utf-8")
    config.write_text("organization: {}", encoding="utf-8")

    assert main(["--template", str(template), "--config", str(config)]) == 0
    assert all(call.args[0] != "cloudformation" for call in session.client.call_args_list)


def test_organization_inventory_requires_and_detects_management_account(mocker):
    organizations = mocker.MagicMock()
    organizations.can_paginate.return_value = False
    organizations.describe_organization.return_value = {"Organization": {"ManagementAccountId": "000000000001"}}
    organizations.list_roots.return_value = {"Roots": [{"Id": "r-example"}]}
    organizations.list_accounts.return_value = {
        "Accounts": [
            {"Id": "000000000001", "Status": "ACTIVE"},
            {"Id": "000000000002", "Status": "ACTIVE"},
            {"Id": "000000000003", "Status": "SUSPENDED"},
        ]
    }
    organizations.list_organizational_units_for_parent.side_effect = [
        {"OrganizationalUnits": [{"Id": "ou-example-one"}]},
        {"OrganizationalUnits": []},
    ]
    inventory = inspect_organization(_session(mocker, organizations=organizations), "000000000001")
    assert inventory == _inventory()

    with pytest.raises(RuntimeError, match="not the management account"):
        inspect_organization(_session(mocker, organizations=organizations), "000000000009")


def test_confirmation_refusal_performs_no_aws_mutation(mocker):
    cloudformation = _cloudformation(mocker)
    session = _session(mocker, cloudformation=cloudformation)
    assert not provision_organization(
        session, _inventory(), "template", "PRM-TaggingRole", "example-id", "us-east-1", confirm=lambda _: "no"
    )
    cloudformation.activate_organizations_access.assert_not_called()
    cloudformation.create_stack_set.assert_not_called()
    cloudformation.create_stack_instances.assert_not_called()


def test_new_stackset_is_created_and_deployed(mocker):
    cloudformation = _cloudformation(mocker)
    session = _session(mocker, cloudformation=cloudformation)
    assert provision_organization(
        session,
        _inventory(),
        "template",
        "PRM-TaggingRole",
        "example-id",
        "us-east-1",
        confirm=lambda _: "provision",
        sleep=lambda _: None,
    )
    cloudformation.create_stack_set.assert_called_once()
    cloudformation.create_stack_instances.assert_called_once()
    cloudformation.update_stack_set.assert_not_called()


def test_trusted_access_is_activated_and_waited_for(mocker):
    cloudformation = _cloudformation(mocker)
    cloudformation.describe_organizations_access.side_effect = [
        {"Status": "DISABLED"},
        {"Status": "ENABLED"},
    ]
    session = _session(mocker, cloudformation=cloudformation)
    assert provision_organization(
        session,
        _inventory(),
        "template",
        "PRM-TaggingRole",
        "example-id",
        "us-east-1",
        confirm=lambda _: "provision",
        sleep=lambda _: None,
    )
    cloudformation.activate_organizations_access.assert_called_once_with()


def test_existing_stackset_is_updated_on_rerun(mocker):
    existing = [{"Status": "CURRENT", "StackInstanceStatus": {"DetailedStatus": "SUCCEEDED"}}]
    cloudformation = _cloudformation(mocker, exists=True, initial_instances=existing)
    session = _session(mocker, cloudformation=cloudformation)
    assert provision_organization(
        session,
        _inventory(),
        "template",
        "PRM-TaggingRole",
        "example-id",
        "us-east-1",
        confirm=lambda _: "provision",
        sleep=lambda _: None,
    )
    cloudformation.update_stack_set.assert_called_once()
    cloudformation.update_stack_instances.assert_called_once()
    cloudformation.create_stack_set.assert_not_called()


def test_rerun_with_unchanged_stackset_still_refreshes_instances(mocker):
    existing = [{"Status": "CURRENT", "StackInstanceStatus": {"DetailedStatus": "SUCCEEDED"}}]
    cloudformation = _cloudformation(mocker, exists=True, initial_instances=existing)
    cloudformation.update_stack_set.side_effect = _client_error("ValidationError", "No updates are to be performed")
    session = _session(mocker, cloudformation=cloudformation)

    assert provision_organization(
        session,
        _inventory(),
        "template",
        "PRM-TaggingRole",
        "example-id",
        "us-east-1",
        confirm=lambda _: "provision",
        sleep=lambda _: None,
    )
    cloudformation.update_stack_instances.assert_called_once()


def test_partial_stackset_failure_is_reported(mocker):
    cloudformation = _cloudformation(mocker)
    cloudformation.describe_stack_set_operation.return_value = {
        "StackSetOperation": {"Status": "FAILED", "StatusReason": "one member account failed"}
    }
    session = _session(mocker, cloudformation=cloudformation)
    with pytest.raises(RuntimeError, match="one member account failed"):
        provision_organization(
            session,
            _inventory(),
            "template",
            "PRM-TaggingRole",
            "example-id",
            "us-east-1",
            confirm=lambda _: "provision",
            sleep=lambda _: None,
        )
