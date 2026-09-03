"""AWS PRM Resource Tagging CLI."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import AppConfig
from .discovery import build_providers, discover_resources
from .models import Action, ResourceRecord, TagStatus
from .organizations import list_active_accounts
from .reporting import generate_report, print_summary
from .sessions import assume_role_session, get_caller_identity, get_enabled_regions, get_session
from .tagging import process_records

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _error_record(account_id: str, account_name: str, error: str) -> ResourceRecord:
    return ResourceRecord(
        account_id=account_id,
        account_name=account_name,
        region="",
        service="account",
        resource_type="account",
        resource_arn=f"arn:aws:iam::{account_id}:root",
        status=TagStatus.ERROR,
        action=Action.SKIPPED,
        error=error,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prm-tagger",
        description=(
            "AWS PRM Resource Tagging - discover, audit and apply "
            "AWS Partner Revenue Measurement tags across AWS resources."
        ),
    )
    parser.add_argument("--config", metavar="FILE", help="Path to config YAML file.")

    sub = parser.add_subparsers(dest="command", required=True)

    for cmd in ("audit", "apply"):
        p = sub.add_parser(cmd, help=f"Run in {'dry-run audit' if cmd == 'audit' else 'apply'} mode.")
        p.add_argument("--organization", action="store_true", help="Iterate over all accounts in the AWS Organization.")
        p.add_argument("--yes", action="store_true", help="Skip confirmation prompt (apply only, for CI/CD).")
        p.add_argument("--config", metavar="FILE", help="Path to config YAML file.")

    return parser


def _print_identity(identity: dict) -> None:
    print(f"\n  AWS Identity : {identity.get('Arn', identity['Account'])}")
    print(f"  Account      : {identity['Account']}\n")


def _confirm_apply(organization: bool) -> bool:
    """Strong typed confirmation before any apply. Returns True if confirmed."""
    print("=" * 60)
    print("  WARNING: APPLY MODE — tags will be written to AWS")
    if organization:
        print("  Scope  : ALL accounts in the AWS Organization")
    else:
        print("  Scope  : current account only")
    print("  Action : add MISSING tags only")
    print("  Safety : existing tags never modified or deleted")
    print("           CONFLICT resources always skipped")
    print("=" * 60)
    answer = input("  Type 'apply' to confirm, anything else to abort: ").strip().lower()
    if answer != "apply":
        print("Aborted.")
        return False
    return True


def _run_for_account(
    session,
    account_id: str,
    account_name: str,
    config: AppConfig,
    dry_run: bool,
) -> list:
    try:
        regions = get_enabled_regions(session, exclude=config.exclude_regions, allowlist=config.include_regions or None)
        providers = build_providers(
            config.allowed_services,
            filter_tags=config.filter_tags or None,
            resource_types=config.resource_types or None,
        )
        required_tags = config.required_tags
        records = discover_resources(session, account_id, account_name, regions, providers, required_tags)
        return process_records(session, records, required_tags, dry_run=dry_run)
    # Account isolation is a safety requirement: SDK, provider, and parsing
    # failures in one member must not abort processing of later accounts.
    except Exception as e:  # noqa: BLE001
        logger.error("Unexpected error processing account %s (%s): %s", account_id, account_name, e)
        return [_error_record(account_id, account_name, str(e))]


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    config_path = getattr(args, "config", None)
    try:
        config = AppConfig.load(config_path)
    except (OSError, TypeError, ValueError) as e:
        logger.error("Invalid configuration: %s", e)
        return 2
    dry_run = args.command == "audit"

    if not config.partner.product_code or config.partner.product_code.startswith("<"):
        logger.error("Set partner.product_code to a real AWS Marketplace product code before running.")
        return 2

    base_session = get_session()
    identity = get_caller_identity(base_session)
    caller_account = identity["Account"]
    _print_identity(identity)

    if args.command == "apply":
        if args.yes:
            logger.warning("apply --yes: skipping interactive confirmation.")
        else:
            if not _confirm_apply(args.organization):
                return 0

    all_records = []

    if args.organization:
        if not config.organization:
            logger.error("Organization config missing. Add 'organization:' block to config.yaml.")
            return 1

        org_config = config.organization
        accounts = list_active_accounts(base_session, exclude=org_config.exclude_accounts)
        logger.info("Found %d active accounts in organization.", len(accounts))

        for acct in accounts:
            if acct.account_id == caller_account:
                logger.info("Processing account %s (%s) [caller — no AssumeRole]", acct.account_id, acct.name)
                records = _run_for_account(base_session, acct.account_id, acct.name, config, dry_run)
            else:
                logger.info("Processing account %s (%s)", acct.account_id, acct.name)
                session = assume_role_session(
                    base_session,
                    acct.account_id,
                    org_config.role_name,
                    external_id=org_config.external_id or None,
                )
                if session is None:
                    all_records.append(_error_record(acct.account_id, acct.name, "Could not assume configured role"))
                    logger.error("Skipping account %s — could not assume role.", acct.account_id)
                    continue
                records = _run_for_account(session, acct.account_id, acct.name, config, dry_run)
            all_records.extend(records)
    else:
        account_name = identity.get("Arn", caller_account).split("/")[-1]
        logger.info("Running for account %s", caller_account)
        all_records = _run_for_account(base_session, caller_account, account_name, config, dry_run)

    print_summary(all_records)
    report_path = generate_report(all_records, config.reports_dir, required_tags=config.required_tags)
    print(f"Report saved to: {report_path}")
    return 0


def entrypoint() -> None:
    sys.exit(main())
