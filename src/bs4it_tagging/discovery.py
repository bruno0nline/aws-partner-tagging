from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import ClassVar, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .models import ResourceRecord, TagStatus

logger = logging.getLogger(__name__)

# ── Discovery coverage note ───────────────────────────────────────────────────
# The Resource Groups Tagging API (GetResources) only returns resources that
# have AT LEAST ONE tag. Resources with zero tags are invisible to this API.
#
# This is a known limitation for PRM audit completeness:
#   - A resource with no tags at all will NOT appear in the audit report.
#   - The report therefore cannot guarantee 100% coverage of untagged resources.
#
# Ec2NativeProvider mitigates this for the configured EC2/EBS MVP types. Other
# services still need native providers or Resource Explorer tag:none discovery.
# ─────────────────────────────────────────────────────────────────────────────


class ResourceProvider(Protocol):
    """Protocol for service-specific resource providers.

    Implement this protocol to add native-API providers for services where
    the Resource Groups Tagging API does not provide complete coverage
    (e.g. resources with zero tags).
    """

    service: str

    def discover(
        self,
        session: boto3.Session,
        region: str,
        account_id: str,
        account_name: str,
    ) -> Iterator[ResourceRecord]: ...


class TaggingAPIProvider:
    """Discovers resources via Resource Groups Tagging API for a given service prefix.

    Coverage limitation: only returns resources that already have at least one tag.
    Resources with zero tags are not returned by this API and will not appear in
    audit reports. See module docstring for mitigation options.
    """

    def __init__(
        self,
        service: str,
        filter_tags: dict[str, str] | None = None,
        allowed_resource_types: list[str] | None = None,
    ) -> None:
        self.service = service
        self._filter_tags = filter_tags or {}
        # None means "all types allowed"; empty list would mean "nothing allowed"
        self._allowed_resource_types: list[str] | None = allowed_resource_types

    @staticmethod
    def _resource_type_from_arn(arn: str) -> str | None:
        """Extract the resource type segment from an ARN.

        ARN format: arn:partition:service:region:account:resource-type/resource-id
        or:         arn:partition:service:region:account:resource-type:resource-id
        Returns the resource-type portion (e.g. 'instance', 'vpc', 'function').

        Examples:
            arn:aws:ec2:us-east-1:123:instance/i-abc   -> 'instance'
            arn:aws:lambda:us-east-1:123:function:foo  -> 'function'
            arn:aws:s3:::my-bucket                     -> 'bucket'
        """
        parts = arn.split(":", 5)
        if len(parts) != 6 or parts[0] != "arn" or not parts[1] or not parts[2]:
            return None
        resource_section = parts[5]
        if not resource_section:
            return None
        if "/" in resource_section:
            resource_type, resource_id = resource_section.split("/", 1)
        elif ":" in resource_section:
            resource_type, resource_id = resource_section.split(":", 1)
        elif parts[2] == "s3":
            resource_type, resource_id = "bucket", resource_section
        else:
            return None
        if not resource_type or not resource_id:
            return None
        return resource_type

    def _is_allowed_type(self, arn: str) -> bool:
        parts = arn.split(":", 5)
        if len(parts) != 6 or parts[2] != self.service:
            return False
        resource_type = self._resource_type_from_arn(arn)
        if resource_type is None:
            return False
        if self._allowed_resource_types is None:
            return True
        return resource_type in self._allowed_resource_types

    def discover(
        self,
        session: boto3.Session,
        region: str,
        account_id: str,
        account_name: str,
    ) -> Iterator[ResourceRecord]:
        client = session.client("resourcegroupstaggingapi", region_name=region)
        paginator = client.get_paginator("get_resources")
        kwargs: dict = {"ResourceTypeFilters": [self.service]}
        if self._filter_tags:
            kwargs["TagFilters"] = [{"Key": k, "Values": [v]} for k, v in self._filter_tags.items()]
        try:
            for page in paginator.paginate(**kwargs):
                for resource in page.get("ResourceTagMappingList", []):
                    arn = resource["ResourceARN"]
                    resource_type = self._resource_type_from_arn(arn)
                    if resource_type is None or not self._is_allowed_type(arn):
                        logger.warning("Skipping resource with invalid or excluded ARN/type: %s", arn)
                        continue
                    tags = {t["Key"]: t["Value"] for t in resource.get("Tags", [])}
                    yield ResourceRecord(
                        account_id=account_id,
                        account_name=account_name,
                        region=region,
                        service=self.service,
                        resource_type=resource_type,
                        resource_arn=arn,
                        existing_tags=tags,
                    )
        except (BotoCoreError, ClientError) as e:
            logger.warning("TaggingAPI error for service %s in %s/%s: %s", self.service, account_id, region, e)
            yield ResourceRecord(
                account_id=account_id,
                account_name=account_name,
                region=region,
                service=self.service,
                resource_type="discovery",
                resource_arn=f"discovery-error:{account_id}:{region}:{self.service}",
                status=TagStatus.ERROR,
                error=str(e),
            )


class Ec2NativeProvider:
    """Enumerate supported EC2/EBS resources, including completely untagged ones."""

    service = "ec2"
    _SPECS: ClassVar[dict[str, tuple[str, str, str, dict]]] = {
        "volume": ("describe_volumes", "Volumes", "VolumeId", {}),
        "snapshot": ("describe_snapshots", "Snapshots", "SnapshotId", {"OwnerIds": ["self"]}),
        "vpc": ("describe_vpcs", "Vpcs", "VpcId", {}),
        "subnet": ("describe_subnets", "Subnets", "SubnetId", {}),
        "internet-gateway": ("describe_internet_gateways", "InternetGateways", "InternetGatewayId", {}),
        "natgateway": ("describe_nat_gateways", "NatGateways", "NatGatewayId", {}),
        "elastic-ip": ("describe_addresses", "Addresses", "AllocationId", {}),
        "vpc-endpoint": ("describe_vpc_endpoints", "VpcEndpoints", "VpcEndpointId", {}),
        "transit-gateway": ("describe_transit_gateways", "TransitGateways", "TransitGatewayId", {}),
        "transit-gateway-attachment": (
            "describe_transit_gateway_attachments",
            "TransitGatewayAttachments",
            "TransitGatewayAttachmentId",
            {},
        ),
    }

    def __init__(self, allowed_resource_types: list[str] | None = None) -> None:
        self._allowed_resource_types = (
            ["instance", *self._SPECS] if allowed_resource_types is None else allowed_resource_types
        )

    @staticmethod
    def _tags(resource: dict) -> dict[str, str]:
        return {tag["Key"]: tag.get("Value", "") for tag in resource.get("Tags", [])}

    @staticmethod
    def _pages(client, operation: str, kwargs: dict) -> Iterator[dict]:
        if client.can_paginate(operation):
            yield from client.get_paginator(operation).paginate(**kwargs)
        else:
            yield getattr(client, operation)(**kwargs)

    def _record(
        self,
        resource_type: str,
        resource_id: str,
        resource: dict,
        partition: str,
        region: str,
        account_id: str,
        account_name: str,
    ) -> ResourceRecord:
        return ResourceRecord(
            account_id=account_id,
            account_name=account_name,
            region=region,
            service=self.service,
            resource_type=resource_type,
            resource_arn=f"arn:{partition}:ec2:{region}:{account_id}:{resource_type}/{resource_id}",
            existing_tags=self._tags(resource),
        )

    def discover(
        self, session: boto3.Session, region: str, account_id: str, account_name: str
    ) -> Iterator[ResourceRecord]:
        client = session.client("ec2", region_name=region)
        partition = session.get_partition_for_region(region)
        for resource_type in self._allowed_resource_types:
            try:
                if resource_type == "instance":
                    for page in self._pages(client, "describe_instances", {}):
                        for reservation in page.get("Reservations", []):
                            for resource in reservation.get("Instances", []):
                                if resource.get("State", {}).get("Name") != "terminated":
                                    yield self._record(
                                        resource_type,
                                        resource["InstanceId"],
                                        resource,
                                        partition,
                                        region,
                                        account_id,
                                        account_name,
                                    )
                    continue
                spec = self._SPECS.get(resource_type)
                if spec is None:
                    logger.warning("EC2 native discovery does not support configured type %s", resource_type)
                    continue
                operation, collection, id_field, kwargs = spec
                for page in self._pages(client, operation, kwargs):
                    for resource in page.get(collection, []):
                        resource_id = resource.get(id_field)
                        if resource_id:
                            yield self._record(
                                resource_type, resource_id, resource, partition, region, account_id, account_name
                            )
            except (BotoCoreError, ClientError) as e:
                logger.warning("EC2 discovery error for %s in %s/%s: %s", resource_type, account_id, region, e)
                yield ResourceRecord(
                    account_id=account_id,
                    account_name=account_name,
                    region=region,
                    service=self.service,
                    resource_type=resource_type,
                    resource_arn=f"discovery-error:{region}:{resource_type}",
                    status=TagStatus.ERROR,
                    error=str(e),
                )


def build_providers(
    allowed_services: list[str],
    filter_tags: dict[str, str] | None = None,
    resource_types: dict[str, list[str]] | None = None,
) -> list[ResourceProvider]:
    """Build the list of providers for the allowed services."""
    providers: list[ResourceProvider] = []
    for svc in allowed_services:
        allowed_types = (resource_types or {}).get(svc)
        if svc == "ec2":
            providers.append(Ec2NativeProvider(allowed_types))
        else:
            providers.append(TaggingAPIProvider(svc, filter_tags=filter_tags, allowed_resource_types=allowed_types))
    return providers


def classify_resource(record: ResourceRecord, required_tags: dict[str, str]) -> TagStatus:
    """Classify a resource against the required tags.

    Rules (in priority order):
    1. CONFLICT: aws-apn-id exists with a DIFFERENT value → never overwrite.
    2. CONFLICT: any additional_tag exists with a DIFFERENT value → never overwrite.
    3. MISSING:  one or more required tags are absent or have wrong value.
    4. COMPLIANT: all required tags present with correct values.
    """
    apn_key = "aws-apn-id"

    # Rule 1: aws-apn-id conflict — highest priority
    if apn_key in required_tags:
        existing_apn = record.existing_tags.get(apn_key)
        if existing_apn is not None and existing_apn != required_tags[apn_key]:
            return TagStatus.CONFLICT

    # Rule 2: any additional tag conflict
    for key, expected_value in required_tags.items():
        if key == apn_key:
            continue
        existing_value = record.existing_tags.get(key)
        if existing_value is not None and existing_value != expected_value:
            return TagStatus.CONFLICT

    # Rule 3 & 4: missing vs compliant
    missing = any(record.existing_tags.get(k) != v for k, v in required_tags.items())
    return TagStatus.MISSING if missing else TagStatus.COMPLIANT


def discover_resources(
    session: boto3.Session,
    account_id: str,
    account_name: str,
    regions: list[str],
    providers: list[ResourceProvider],
    required_tags: dict[str, str],
) -> list[ResourceRecord]:
    records: list[ResourceRecord] = []
    for region in regions:
        logger.info("Discovering resources in %s / %s", account_id, region)
        for provider in providers:
            try:
                for record in provider.discover(session, region, account_id, account_name):
                    if record.status != TagStatus.ERROR:
                        record.status = classify_resource(record, required_tags)
                    records.append(record)
            except Exception as e:  # noqa: BLE001 - isolate an individual provider failure
                logger.error("Discovery failed for %s in %s/%s: %s", provider.service, account_id, region, e)
                records.append(
                    ResourceRecord(
                        account_id=account_id,
                        account_name=account_name,
                        region=region,
                        service=provider.service,
                        resource_type="discovery",
                        resource_arn=f"discovery-error:{account_id}:{region}:{provider.service}",
                        status=TagStatus.ERROR,
                        error=str(e),
                    )
                )
    return list({record.resource_arn: record for record in records}.values())
