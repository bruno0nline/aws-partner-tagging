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


class NativeInventoryProvider:
    """Native inventory for V1 services, including resources with no tags."""

    _GLOBAL_SERVICES: ClassVar[set[str]] = {"cloudfront", "route53"}

    def __init__(self, service: str, allowed_resource_types: list[str] | None = None) -> None:
        self.service = service
        self._allowed = allowed_resource_types
        self._processed_accounts: set[str] = set()

    def _allowed_type(self, resource_type: str) -> bool:
        return self._allowed is None or resource_type in self._allowed

    @staticmethod
    def _tag_dict(tags: list[dict] | dict | None) -> dict[str, str]:
        if isinstance(tags, dict):
            return {str(k): str(v) for k, v in tags.items()}
        return {tag["Key"]: tag.get("Value", "") for tag in tags or []}

    @staticmethod
    def _pages(client, operation: str, **kwargs) -> Iterator[dict]:
        if client.can_paginate(operation):
            yield from client.get_paginator(operation).paginate(**kwargs)
        else:
            yield getattr(client, operation)(**kwargs)

    def _record(
        self, account_id: str, account_name: str, region: str, resource_type: str, arn: str, tags, native_id=None
    ) -> ResourceRecord | None:
        if not self._allowed_type(resource_type):
            return None
        return ResourceRecord(
            account_id=account_id,
            account_name=account_name,
            region=region,
            service=self.service,
            resource_type=resource_type,
            resource_arn=arn,
            existing_tags=self._tag_dict(tags),
            native_id=native_id,
        )

    def _inventory(self, session, region: str, account_id: str, account_name: str) -> Iterator[ResourceRecord]:
        partition = session.get_partition_for_region(region)
        client_name = "elbv2" if self.service == "elasticloadbalancing" else self.service
        client = session.client(client_name, region_name=region)

        if self.service == "s3":
            for bucket in client.list_buckets().get("Buckets", []):
                name = bucket["Name"]
                location = client.get_bucket_location(Bucket=name).get("LocationConstraint") or "us-east-1"
                if location == "EU":
                    location = "eu-west-1"
                if location != region:
                    continue
                try:
                    tags = client.get_bucket_tagging(Bucket=name).get("TagSet", [])
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") in {"NoSuchTagSet", "NoSuchTagSetError"}:
                        tags = []
                    else:
                        raise
                record = self._record(account_id, account_name, location, "bucket", f"arn:{partition}:s3:::{name}", tags, name)
                if record:
                    yield record
            return

        if self.service == "rds":
            specs = (("describe_db_instances", "DBInstances", "DBInstanceArn", "db"), ("describe_db_clusters", "DBClusters", "DBClusterArn", "cluster"))
            for operation, collection, arn_key, resource_type in specs:
                for page in self._pages(client, operation):
                    for item in page.get(collection, []):
                        arn = item[arn_key]
                        tags = client.list_tags_for_resource(ResourceName=arn).get("TagList", [])
                        record = self._record(account_id, account_name, region, resource_type, arn, tags, arn)
                        if record:
                            yield record
            return

        if self.service == "elasticloadbalancing":
            for page in self._pages(client, "describe_load_balancers"):
                for item in page.get("LoadBalancers", []):
                    arn = item["LoadBalancerArn"]
                    response = client.describe_tags(ResourceArns=[arn]).get("TagDescriptions", [])
                    tags = response[0].get("Tags", []) if response else []
                    record = self._record(account_id, account_name, region, "loadbalancer", arn, tags, arn)
                    if record:
                        yield record
            classic = session.client("elb", region_name=region)
            for page in self._pages(classic, "describe_load_balancers"):
                for item in page.get("LoadBalancerDescriptions", []):
                    name = item["LoadBalancerName"]
                    arn = f"arn:{partition}:elasticloadbalancing:{region}:{account_id}:loadbalancer/{name}"
                    response = classic.describe_tags(LoadBalancerNames=[name]).get("TagDescriptions", [])
                    tags = response[0].get("Tags", []) if response else []
                    record = self._record(
                        account_id, account_name, region, "classic-loadbalancer", arn, tags, f"classic:{name}"
                    )
                    if record:
                        yield record
            return

        if self.service == "lambda":
            for page in self._pages(client, "list_functions"):
                for item in page.get("Functions", []):
                    arn = item["FunctionArn"]
                    tags = client.list_tags(Resource=arn).get("Tags", {})
                    record = self._record(account_id, account_name, region, "function", arn, tags, arn)
                    if record:
                        yield record
            return

        if self.service == "ecs":
            for cluster_page in self._pages(client, "list_clusters"):
                for cluster_arn in cluster_page.get("clusterArns", []):
                    ecs_tags = client.list_tags_for_resource(resourceArn=cluster_arn).get("tags", [])
                    tags = {tag["key"]: tag.get("value", "") for tag in ecs_tags}
                    record = self._record(account_id, account_name, region, "cluster", cluster_arn, tags, cluster_arn)
                    if record:
                        yield record
                    for service_page in self._pages(client, "list_services", cluster=cluster_arn):
                        for service_arn in service_page.get("serviceArns", []):
                            ecs_tags = client.list_tags_for_resource(resourceArn=service_arn).get("tags", [])
                            tags = {tag["key"]: tag.get("value", "") for tag in ecs_tags}
                            record = self._record(account_id, account_name, region, "service", service_arn, tags, service_arn)
                            if record:
                                yield record
            return

        if self.service == "eks":
            for page in self._pages(client, "list_clusters"):
                for name in page.get("clusters", []):
                    cluster = client.describe_cluster(name=name)["cluster"]
                    record = self._record(account_id, account_name, region, "cluster", cluster["arn"], cluster.get("tags"), cluster["arn"])
                    if record:
                        yield record
            return

        if self.service == "dynamodb":
            for page in self._pages(client, "list_tables"):
                for name in page.get("TableNames", []):
                    arn = client.describe_table(TableName=name)["Table"]["TableArn"]
                    tags = client.list_tags_of_resource(ResourceArn=arn).get("Tags", [])
                    record = self._record(account_id, account_name, region, "table", arn, tags, arn)
                    if record:
                        yield record
            return

        if self.service == "elasticache":
            for page in self._pages(client, "describe_cache_clusters"):
                for item in page.get("CacheClusters", []):
                    arn = item["ARN"]
                    tags = client.list_tags_for_resource(ResourceName=arn).get("TagList", [])
                    record = self._record(account_id, account_name, region, "cluster", arn, tags, arn)
                    if record:
                        yield record
            return

        if self.service == "efs":
            for page in self._pages(client, "describe_file_systems"):
                for item in page.get("FileSystems", []):
                    file_system_id = item["FileSystemId"]
                    arn = item.get("FileSystemArn") or f"arn:{partition}:elasticfilesystem:{region}:{account_id}:file-system/{file_system_id}"
                    tags = client.list_tags_for_resource(ResourceId=file_system_id).get("Tags", [])
                    record = self._record(account_id, account_name, region, "file-system", arn, tags, file_system_id)
                    if record:
                        yield record
            return

        if self.service == "backup":
            for page in self._pages(client, "list_backup_vaults"):
                for item in page.get("BackupVaultList", []):
                    arn = item["BackupVaultArn"]
                    tags = client.list_tags(ResourceArn=arn).get("Tags", {})
                    record = self._record(account_id, account_name, region, "backup-vault", arn, tags, arn)
                    if record:
                        yield record
            return

        if self.service == "secretsmanager":
            for page in self._pages(client, "list_secrets", IncludePlannedDeletion=False):
                for item in page.get("SecretList", []):
                    record = self._record(account_id, account_name, region, "secret", item["ARN"], item.get("Tags"), item["ARN"])
                    if record:
                        yield record
            return

        if self.service == "sns":
            for page in self._pages(client, "list_topics"):
                for item in page.get("Topics", []):
                    arn = item["TopicArn"]
                    tags = client.list_tags_for_resource(ResourceArn=arn).get("Tags", [])
                    record = self._record(account_id, account_name, region, "topic", arn, tags, arn)
                    if record:
                        yield record
            return

        if self.service == "sqs":
            for page in self._pages(client, "list_queues"):
                for url in page.get("QueueUrls", []):
                    arn = client.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
                    tags = client.list_queue_tags(QueueUrl=url).get("Tags", {})
                    record = self._record(account_id, account_name, region, "queue", arn, tags, url)
                    if record:
                        yield record
            return

        if self.service == "apigateway":
            for page in self._pages(client, "get_rest_apis"):
                for item in page.get("items", []):
                    arn = f"arn:{partition}:apigateway:{region}::/restapis/{item['id']}"
                    tags = client.get_tags(resourceArn=arn).get("tags", {})
                    record = self._record(account_id, account_name, region, "restapis", arn, tags, arn)
                    if record:
                        yield record
            return

        if self.service == "cloudfront":
            for page in self._pages(client, "list_distributions"):
                for item in page.get("DistributionList", {}).get("Items", []):
                    arn = item["ARN"]
                    tags = client.list_tags_for_resource(Resource=arn).get("Tags", {}).get("Items", [])
                    record = self._record(account_id, account_name, region, "distribution", arn, tags, arn)
                    if record:
                        yield record
            return

        if self.service == "route53":
            for page in self._pages(client, "list_hosted_zones"):
                for item in page.get("HostedZones", []):
                    zone_id = item["Id"].split("/")[-1]
                    arn = f"arn:{partition}:route53:::hostedzone/{zone_id}"
                    tags = client.list_tags_for_resource(ResourceType="hostedzone", ResourceId=zone_id).get("ResourceTagSet", {}).get("Tags", [])
                    record = self._record(account_id, account_name, region, "hostedzone", arn, tags, zone_id)
                    if record:
                        yield record

    def discover(self, session, region: str, account_id: str, account_name: str) -> Iterator[ResourceRecord]:
        if self.service in self._GLOBAL_SERVICES:
            if account_id in self._processed_accounts:
                return
            self._processed_accounts.add(account_id)
        try:
            yield from self._inventory(session, region, account_id, account_name)
        except (BotoCoreError, ClientError) as error:
            logger.warning("Native discovery error for %s in %s/%s: %s", self.service, account_id, region, error)
            yield ResourceRecord(
                account_id=account_id, account_name=account_name, region=region, service=self.service,
                resource_type="discovery", resource_arn=f"discovery-error:{account_id}:{region}:{self.service}",
                status=TagStatus.ERROR, error=str(error),
            )


NATIVE_V1_SERVICES = {
    "s3", "rds", "elasticloadbalancing", "lambda", "ecs", "eks", "dynamodb", "elasticache", "efs",
    "backup", "secretsmanager", "sns", "sqs", "apigateway", "cloudfront", "route53",
}


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
        elif svc in NATIVE_V1_SERVICES:
            providers.append(NativeInventoryProvider(svc, allowed_types))
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
