from __future__ import annotations

import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .discovery import classify_resource
from .models import Action, ResourceRecord, TagStatus

logger = logging.getLogger(__name__)

APN_TAG_KEY = "aws-apn-id"
NATIVE_TAG_SERVICES = {
    "s3", "rds", "elasticloadbalancing", "lambda", "ecs", "eks", "dynamodb", "elasticache", "efs",
    "backup", "secretsmanager", "sns", "sqs", "apigateway", "cloudfront", "route53",
}


def _tags_to_apply(record: ResourceRecord, required_tags: dict[str, str]) -> dict[str, str]:
    """Return absent tags only; never return an existing key for replacement."""
    return {k: v for k, v in required_tags.items() if k not in record.existing_tags}


def _apply_s3_tags(session, record: ResourceRecord, required_tags: dict[str, str]) -> ResourceRecord:
    """Re-read and merge S3 bucket tags because PutBucketTagging replaces the tag set."""
    client = session.client("s3", region_name=record.region)
    bucket = record.native_id or record.resource_arn.rsplit(":::", 1)[-1]
    try:
        try:
            response = client.get_bucket_tagging(Bucket=bucket)
            record.existing_tags = {tag["Key"]: tag.get("Value", "") for tag in response.get("TagSet", [])}
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"NoSuchTagSet", "NoSuchTagSetError"}:
                record.existing_tags = {}
            else:
                raise
        record.status = classify_resource(record, required_tags)
        if record.status == TagStatus.CONFLICT:
            record.action = Action.SKIPPED
            return record
        if record.status == TagStatus.COMPLIANT:
            record.action = Action.NONE
            return record
        tags_to_add = _tags_to_apply(record, required_tags)
        merged = {**record.existing_tags, **tags_to_add}
        client.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in merged.items()]})
        record.existing_tags = merged
        record.status = TagStatus.COMPLIANT
        record.action = Action.TAG_APPLIED
    except (BotoCoreError, ClientError) as error:
        record.status = TagStatus.ERROR
        record.action = Action.SKIPPED
        record.error = str(error)
    return record


def _native_tags(session, record: ResourceRecord, tags_to_add: dict[str, str] | None = None) -> dict[str, str]:
    """Read tags, or add tags when supplied, through a V1 service's native API."""
    service = record.service
    classic_elb = service == "elasticloadbalancing" and str(record.native_id).startswith("classic:")
    client_name = "elb" if classic_elb else ("elbv2" if service == "elasticloadbalancing" else service)
    client = session.client(client_name, region_name=record.region)
    resource = record.native_id or record.resource_arn

    if service == "rds":
        if tags_to_add is not None:
            client.add_tags_to_resource(ResourceName=resource, Tags=[{"Key": k, "Value": v} for k, v in tags_to_add.items()])
        return {tag["Key"]: tag.get("Value", "") for tag in client.list_tags_for_resource(ResourceName=resource).get("TagList", [])}
    if service == "elasticloadbalancing":
        if classic_elb:
            name = str(resource).split(":", 1)[1]
            if tags_to_add is not None:
                client.add_tags(LoadBalancerNames=[name], Tags=[{"Key": k, "Value": v} for k, v in tags_to_add.items()])
            descriptions = client.describe_tags(LoadBalancerNames=[name]).get("TagDescriptions", [])
            return {tag["Key"]: tag.get("Value", "") for tag in (descriptions[0].get("Tags", []) if descriptions else [])}
        if tags_to_add is not None:
            client.add_tags(ResourceArns=[resource], Tags=[{"Key": k, "Value": v} for k, v in tags_to_add.items()])
        descriptions = client.describe_tags(ResourceArns=[resource]).get("TagDescriptions", [])
        return {tag["Key"]: tag.get("Value", "") for tag in (descriptions[0].get("Tags", []) if descriptions else [])}
    if service == "lambda":
        if tags_to_add is not None:
            client.tag_resource(Resource=resource, Tags=tags_to_add)
        return dict(client.list_tags(Resource=resource).get("Tags", {}))
    if service == "ecs":
        if tags_to_add is not None:
            client.tag_resource(resourceArn=resource, tags=[{"key": k, "value": v} for k, v in tags_to_add.items()])
        return {tag["key"]: tag.get("value", "") for tag in client.list_tags_for_resource(resourceArn=resource).get("tags", [])}
    if service == "eks":
        if tags_to_add is not None:
            client.tag_resource(resourceArn=resource, tags=tags_to_add)
        return dict(client.list_tags_for_resource(resourceArn=resource).get("tags", {}))
    if service == "dynamodb":
        if tags_to_add is not None:
            client.tag_resource(ResourceArn=resource, Tags=[{"Key": k, "Value": v} for k, v in tags_to_add.items()])
        return {tag["Key"]: tag.get("Value", "") for tag in client.list_tags_of_resource(ResourceArn=resource).get("Tags", [])}
    if service == "elasticache":
        if tags_to_add is not None:
            client.add_tags_to_resource(ResourceName=resource, Tags=[{"Key": k, "Value": v} for k, v in tags_to_add.items()])
        return {tag["Key"]: tag.get("Value", "") for tag in client.list_tags_for_resource(ResourceName=resource).get("TagList", [])}
    if service == "efs":
        if tags_to_add is not None:
            client.tag_resource(ResourceId=resource, Tags=[{"Key": k, "Value": v} for k, v in tags_to_add.items()])
        return {tag["Key"]: tag.get("Value", "") for tag in client.list_tags_for_resource(ResourceId=resource).get("Tags", [])}
    if service == "backup":
        if tags_to_add is not None:
            client.tag_resource(ResourceArn=resource, Tags=tags_to_add)
        return dict(client.list_tags(ResourceArn=resource).get("Tags", {}))
    if service == "secretsmanager":
        if tags_to_add is not None:
            client.tag_resource(SecretId=resource, Tags=[{"Key": k, "Value": v} for k, v in tags_to_add.items()])
        return {tag["Key"]: tag.get("Value", "") for tag in client.describe_secret(SecretId=resource).get("Tags", [])}
    if service == "sns":
        if tags_to_add is not None:
            client.tag_resource(ResourceArn=resource, Tags=[{"Key": k, "Value": v} for k, v in tags_to_add.items()])
        return {tag["Key"]: tag.get("Value", "") for tag in client.list_tags_for_resource(ResourceArn=resource).get("Tags", [])}
    if service == "sqs":
        if tags_to_add is not None:
            client.tag_queue(QueueUrl=resource, Tags=tags_to_add)
        return dict(client.list_queue_tags(QueueUrl=resource).get("Tags", {}))
    if service == "apigateway":
        if tags_to_add is not None:
            client.tag_resource(resourceArn=resource, tags=tags_to_add)
        return dict(client.get_tags(resourceArn=resource).get("tags", {}))
    if service == "cloudfront":
        if tags_to_add is not None:
            client.tag_resource(Resource=resource, Tags={"Items": [{"Key": k, "Value": v} for k, v in tags_to_add.items()]})
        tags = client.list_tags_for_resource(Resource=resource).get("Tags", {}).get("Items", [])
        return {tag["Key"]: tag.get("Value", "") for tag in tags}
    if service == "route53":
        if tags_to_add is not None:
            client.change_tags_for_resource(
                ResourceType="hostedzone", ResourceId=resource,
                AddTags=[{"Key": k, "Value": v} for k, v in tags_to_add.items()],
            )
        tags = client.list_tags_for_resource(ResourceType="hostedzone", ResourceId=resource).get("ResourceTagSet", {}).get("Tags", [])
        return {tag["Key"]: tag.get("Value", "") for tag in tags}
    raise ValueError(f"No native tagging adapter for service {service}")


def _apply_native_tags(session, record: ResourceRecord, required_tags: dict[str, str]) -> ResourceRecord:
    try:
        record.existing_tags = _native_tags(session, record)
        record.status = classify_resource(record, required_tags)
        if record.status == TagStatus.CONFLICT:
            record.action = Action.SKIPPED
            return record
        if record.status == TagStatus.COMPLIANT:
            record.action = Action.NONE
            return record
        tags_to_add = _tags_to_apply(record, required_tags)
        _native_tags(session, record, tags_to_add)
        record.existing_tags.update(tags_to_add)
        record.status = TagStatus.COMPLIANT
        record.action = Action.TAG_APPLIED
    except (BotoCoreError, ClientError, ValueError) as error:
        record.status = TagStatus.ERROR
        record.action = Action.SKIPPED
        record.error = str(error)
    return record


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

    if record.service == "s3":
        return _apply_s3_tags(session, record, required_tags)
    if record.service in NATIVE_TAG_SERVICES:
        return _apply_native_tags(session, record, required_tags)

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
