from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# ── PRM defaults ──────────────────────────────────────────────────────────────
# Do not use a code constant as the PRM source of truth; AWS changes the list.
# Historical namespace inventory retained for compatibility only. It is not used
# for discovery and MUST NOT be interpreted as the current official PRM list.
LEGACY_SERVICE_NAMESPACE_INVENTORY = [
    "ec2",
    "ebs",  # EBS volumes (also returned under ec2 prefix in Tagging API)
    "rds",
    "s3",
    "lambda",
    "ecs",
    "eks",
    "elasticloadbalancing",
    "apigateway",
    "cloudfront",
    "elasticache",
    "es",  # OpenSearch / Elasticsearch
    "opensearch",
    "redshift",
    "kinesis",
    "firehose",
    "sns",
    "sqs",
    "dynamodb",
    "glue",
    "backup",
    "bedrock",
    "sagemaker",
    "emr",
    "msk",  # Managed Streaming for Apache Kafka
    "kafka",
    "secretsmanager",
    "kms",
    "wafv2",
    "route53",
    "cloudwatch",
    "logs",  # CloudWatch Logs
    "states",  # Step Functions
    "events",  # EventBridge
    "codecommit",
    "codebuild",
    "codepipeline",
    "codedeploy",
    "ecr",
    "efs",
    "fsx",
    "storagegateway",
    "datasync",
    "transfer",
    "dms",
    "athena",
    "lakeformation",
    "quicksight",
]

# Conservative default: only the native provider with an explicit type allowlist.
# AWS documentation remains the source of truth for PRM eligibility.
DEFAULT_ALLOWED_SERVICES = ["ec2"]

# Default EC2/EBS types covered by native discovery, including snapshots.
DEFAULT_EC2_RESOURCE_TYPES = [
    "instance",
    "volume",  # EBS volumes
    "snapshot",  # EBS snapshots owned by the account
    "vpc",
    "subnet",
    "internet-gateway",
    "natgateway",
    "elastic-ip",
    "vpc-endpoint",
    "transit-gateway",
    "transit-gateway-attachment",
]

DEFAULT_RESOURCE_TYPES: dict[str, list[str]] = {
    "ec2": DEFAULT_EC2_RESOURCE_TYPES,
}


@dataclass
class PartnerConfig:
    """AWS Partner Revenue Measurement (PRM) identity."""

    product_code: str = ""

    @property
    def apn_tag_value(self) -> str:
        """Returns the aws-apn-id tag value: pc:<product_code>"""
        return f"pc:{self.product_code}" if self.product_code else ""


@dataclass
class OrganizationConfig:
    role_name: str = "PRM-TaggingRole"
    external_id: str = "PRMTaggingAutomation"
    exclude_accounts: list[str] = field(default_factory=list)


@dataclass
class AppConfig:
    partner: PartnerConfig = field(default_factory=PartnerConfig)
    additional_tags: dict[str, str] = field(default_factory=dict)
    allowed_services: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_SERVICES))
    resource_types: dict[str, list[str]] = field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_RESOURCE_TYPES.items()}
    )
    exclude_regions: list[str] = field(default_factory=list)
    include_regions: list[str] = field(default_factory=list)
    filter_tags: dict[str, str] = field(default_factory=dict)
    reports_dir: str = "reports"
    organization: OrganizationConfig | None = None

    @property
    def required_tags(self) -> dict[str, str]:
        """Build the full set of required tags from partner config + additional_tags."""
        tags: dict[str, str] = {}
        if self.partner.apn_tag_value:
            tags["aws-apn-id"] = self.partner.apn_tag_value
        # aws-apn-id is reserved and can only be derived from product_code.
        tags.update({k: v for k, v in self.additional_tags.items() if k != "aws-apn-id"})
        return tags

    @classmethod
    def load(cls, path: str | None = None) -> AppConfig:
        if path is None:
            candidates = [
                Path("config/config.yaml"),
                Path("config/config.yml"),
            ]
            path_obj = next((p for p in candidates if p.exists()), None)
        else:
            path_obj = Path(path)

        if path_obj is None or not path_obj.exists():
            logger.info("No config file found, using defaults.")
            return cls()

        try:
            with open(path_obj, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in {path_obj}: {e}") from e
        if not isinstance(data, dict):
            raise TypeError("Configuration root must be a YAML mapping.")

        # ── partner block ──────────────────────────────────────────────────────
        partner_data = data.get("partner") or {}
        if not isinstance(partner_data, dict):
            raise TypeError("'partner' must be a mapping.")
        partner = PartnerConfig(
            product_code=str(partner_data.get("product_code", "")).strip(),
        )

        # ── legacy required_tags support ───────────────────────────────────────
        # If an old config uses required_tags directly, honour it for backwards
        # compatibility but warn the operator to migrate.
        legacy_required = data.get("required_tags")
        additional_tags: dict[str, str] = {}
        if legacy_required:
            logger.warning(
                "config: 'required_tags' is deprecated. Migrate to 'partner.product_code' and 'additional_tags'."
            )
            legacy_required = dict(legacy_required)
            apn_val = legacy_required.pop("aws-apn-id", "")
            if apn_val and apn_val.startswith("pc:") and not partner.product_code:
                partner = PartnerConfig(product_code=apn_val[3:])
            additional_tags = {k: v for k, v in legacy_required.items()}
        else:
            additional_tags = data.get("additional_tags") or {}
        if not isinstance(additional_tags, dict):
            raise TypeError("'additional_tags' must be a mapping.")
        additional_tags = {str(k): str(v) for k, v in additional_tags.items()}

        # ── organization block ─────────────────────────────────────────────────
        org_data = data.get("organization")
        org = None
        if org_data:
            if not isinstance(org_data, dict):
                raise TypeError("'organization' must be a mapping.")
            org = OrganizationConfig(
                role_name=org_data.get("role_name", "PRM-TaggingRole"),
                external_id=org_data.get("external_id", "PRMTaggingAutomation"),
                exclude_accounts=[str(a) for a in org_data.get("exclude_accounts", [])],
            )

        # ── resource_types ─────────────────────────────────────────────────────
        rt_data = data.get("resource_types")
        if rt_data is not None:
            if not isinstance(rt_data, dict):
                raise TypeError("'resource_types' must be a mapping.")
            resource_types = {}
            for svc, type_config in rt_data.items():
                if not isinstance(type_config, dict) or not isinstance(type_config.get("include"), list):
                    raise TypeError(f"resource_types.{svc}.include must be a list.")
                resource_types[str(svc)] = [str(value) for value in type_config["include"]]
        else:
            resource_types = {k: list(v) for k, v in DEFAULT_RESOURCE_TYPES.items()}

        allowed_services = data.get("allowed_services", list(DEFAULT_ALLOWED_SERVICES))
        include_regions = data.get("include_regions", [])
        exclude_regions = data.get("exclude_regions", [])
        filter_tags = data.get("filter_tags", {}) or {}
        for name, value in (
            ("allowed_services", allowed_services),
            ("include_regions", include_regions),
            ("exclude_regions", exclude_regions),
        ):
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise TypeError(f"'{name}' must be a list of strings.")
        if not isinstance(filter_tags, dict):
            raise TypeError("'filter_tags' must be a mapping.")

        return cls(
            partner=partner,
            additional_tags=additional_tags,
            allowed_services=allowed_services,
            resource_types=resource_types,
            exclude_regions=exclude_regions,
            include_regions=include_regions,
            filter_tags={str(k): str(v) for k, v in filter_tags.items()},
            reports_dir=data.get("reports_dir", "reports"),
            organization=org,
        )
