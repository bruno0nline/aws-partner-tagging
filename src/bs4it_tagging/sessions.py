from __future__ import annotations

import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


def get_session(profile: str | None = None) -> boto3.Session:
    """Return a boto3 session using the standard credential provider chain."""
    return boto3.Session(profile_name=profile)


def assume_role_session(
    base_session: boto3.Session,
    account_id: str,
    role_name: str,
    session_name: str = "PRMTaggingSession",
    external_id: str | None = None,
) -> boto3.Session | None:
    """Assume a role in a target account and return a new session."""
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    sts = base_session.client("sts")
    kwargs: dict = {"RoleArn": role_arn, "RoleSessionName": session_name}
    if external_id:
        kwargs["ExternalId"] = external_id
    try:
        resp = sts.assume_role(**kwargs)
        creds = resp["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
    except (BotoCoreError, ClientError) as e:
        logger.error("Failed to assume role %s: %s", role_arn, e)
        return None


def get_caller_identity(session: boto3.Session) -> dict:
    return session.client("sts").get_caller_identity()


def get_enabled_regions(
    session: boto3.Session,
    exclude: list[str] | None = None,
    allowlist: list[str] | None = None,
) -> list[str]:
    """Return enabled regions, optionally filtered by allowlist and/or exclude list."""
    if allowlist:
        return sorted(allowlist)
    ec2 = session.client("ec2")
    resp = ec2.describe_regions(Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}])
    regions = [r["RegionName"] for r in resp["Regions"]]
    if exclude:
        regions = [r for r in regions if r not in exclude]
    return sorted(regions)
