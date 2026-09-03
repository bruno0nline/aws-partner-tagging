from __future__ import annotations

import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .discovery import classify_resource
from .models import Action, ResourceRecord, TagStatus

logger = logging.getLogger(__name__)

APN_TAG_KEY = "aws-apn-id"


def _tags_to_apply(record: ResourceRecord, required_tags: dict[str, str]) -> dict[str, str]:
    """Return absent tags only; never return an existing key for replacement."""
    return {k: v for k, v in required_tags.items() if k not in record.existing_tags}


def apply_tags(
    session: boto3.Session,
    record: ResourceRecord,
    required_tags: dict[str, str],
    dry_run: bool = True,
) -> ResourceRecord:
    if record.status != TagStatus.ERROR:
        record.status = classify_resource(record, required_tags)

    if record.status == TagStatus.CONFLICT:
        logger.warning("CONFLICT on %s — skipping to avoid overwriting existing tags", record.resource_arn)
        record.action = Action.SKIPPED
        return record

    if record.status == TagStatus.COMPLIANT:
        record.action = Action.NONE
        return record

    if record.status != TagStatus.MISSING:
        record.action = Action.SKIPPED
        return record

    tags_to_add = _tags_to_apply(record, required_tags)
    if not tags_to_add:
        record.status = TagStatus.COMPLIANT
        record.action = Action.NONE
        return record

    if dry_run:
        logger.info("DRY-RUN: would tag %s with %s", record.resource_arn, tags_to_add)
        record.action = Action.DRY_RUN
        return record

    region = record.region
    client = session.client("resourcegroupstaggingapi", region_name=region)
    try:
        current = client.get_resources(ResourceARNList=[record.resource_arn])
        mappings = current.get("ResourceTagMappingList", [])
        mapping = next((item for item in mappings if item.get("ResourceARN") == record.resource_arn), None)
        if mapping is not None:
            record.existing_tags = {t["Key"]: t["Value"] for t in mapping.get("Tags", [])}
            record.status = classify_resource(record, required_tags)
            if record.status == TagStatus.CONFLICT:
                record.action = Action.SKIPPED
                return record
            if record.status == TagStatus.COMPLIANT:
                record.action = Action.NONE
                return record
            tags_to_add = _tags_to_apply(record, required_tags)

        response = client.tag_resources(
            ResourceARNList=[record.resource_arn],
            Tags=tags_to_add,
        )
        failed = response.get("FailedResourcesMap", {})
        if failed:
            failure = failed.get(record.resource_arn) or next(iter(failed.values()))
            record.status = TagStatus.ERROR
            record.action = Action.SKIPPED
            record.error = f"{failure.get('ErrorCode', 'TagResourcesError')}: {failure.get('ErrorMessage', '')}".strip()
            return record
        record.existing_tags.update(tags_to_add)
        record.status = TagStatus.COMPLIANT
        record.action = Action.TAG_APPLIED
        logger.info("Tagged %s", record.resource_arn)
    except (BotoCoreError, ClientError) as e:
        record.status = TagStatus.ERROR
        record.action = Action.SKIPPED
        record.error = str(e)
        logger.error("Failed to tag %s: %s", record.resource_arn, e)

    return record


def process_records(
    session: boto3.Session,
    records: list[ResourceRecord],
    required_tags: dict[str, str],
    dry_run: bool = True,
) -> list[ResourceRecord]:
    processed: list[ResourceRecord] = []
    for record in records:
        try:
            processed.append(apply_tags(session, record, required_tags, dry_run=dry_run))
        except Exception as e:
            logger.exception("Unexpected failure processing %s", record.resource_arn)
            record.status = TagStatus.ERROR
            record.action = Action.SKIPPED
            record.error = str(e)
            processed.append(record)
    return processed
