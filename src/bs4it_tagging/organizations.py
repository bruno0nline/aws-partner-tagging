from __future__ import annotations

import logging
from dataclasses import dataclass

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


@dataclass
class AccountInfo:
    account_id: str
    name: str
    status: str


def list_active_accounts(session: boto3.Session, exclude: list[str] | None = None) -> list[AccountInfo]:
    """List all ACTIVE accounts in the organization."""
    org = session.client("organizations")
    accounts: list[AccountInfo] = []
    paginator = org.get_paginator("list_accounts")
    try:
        for page in paginator.paginate():
            for acct in page["Accounts"]:
                state = acct.get("State", acct.get("Status"))
                if state != "ACTIVE":
                    continue
                if exclude and acct["Id"] in exclude:
                    logger.info("Skipping excluded account %s (%s)", acct["Id"], acct["Name"])
                    continue
                accounts.append(AccountInfo(account_id=acct["Id"], name=acct["Name"], status=state))
    except (BotoCoreError, ClientError) as e:
        logger.error("Failed to list organization accounts: %s", e)
        raise
    return accounts
