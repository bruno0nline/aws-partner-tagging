"""Unit tests for AWS PRM Resource Tagging."""

import pytest
from botocore.exceptions import ClientError

from bs4it_tagging.config import AppConfig, PartnerConfig
from bs4it_tagging.discovery import (
    Ec2NativeProvider,
    NativeInventoryProvider,
    TaggingAPIProvider,
    build_providers,
    classify_resource,
    discover_resources,
)
from bs4it_tagging.models import Action, ResourceRecord, TagStatus
from bs4it_tagging.tagging import _tags_to_apply, apply_tags, process_records

# ── Helpers ───────────────────────────────────────────────────────────────────


def _record(**kwargs) -> ResourceRecord:
    defaults = {
        "account_id": "123456789012",
        "account_name": "test-account",
        "region": "us-east-1",
        "service": "ec2",
        "resource_type": "instance",
        "resource_arn": "arn:aws:ec2:us-east-1:123456789012:instance/i-abc",
        "existing_tags": {},
    }
    defaults.update(kwargs)
    return ResourceRecord(**defaults)


# Required tags used across tests — simulates a configured partner
REQUIRED = {
    "aws-apn-id": "pc:testcode123",
    "ManagedBy": "TestPartner",
    "ManagementScope": "ManagedServices",
}

REQUIRED_APN_ONLY = {
    "aws-apn-id": "pc:testcode123",
}


# ── PartnerConfig / product_code → aws-apn-id ─────────────────────────────────


def test_partner_config_apn_tag_value():
    p = PartnerConfig(product_code="abc123xyz")
    assert p.apn_tag_value == "pc:abc123xyz"


def test_partner_config_empty_product_code():
    p = PartnerConfig(product_code="")
    assert p.apn_tag_value == ""


def test_appconfig_required_tags_built_from_partner():
    cfg = AppConfig(
        partner=PartnerConfig(product_code="mycode"),
        additional_tags={"ManagedBy": "Acme"},
    )
    tags = cfg.required_tags
    assert tags["aws-apn-id"] == "pc:mycode"
    assert tags["ManagedBy"] == "Acme"


def test_appconfig_required_tags_no_product_code():
    cfg = AppConfig(
        partner=PartnerConfig(product_code=""),
        additional_tags={"ManagedBy": "Acme"},
    )
    tags = cfg.required_tags
    assert "aws-apn-id" not in tags
    assert tags["ManagedBy"] == "Acme"


def test_appconfig_additional_tags_empty():
    cfg = AppConfig(
        partner=PartnerConfig(product_code="code1"),
        additional_tags={},
    )
    assert cfg.required_tags == {"aws-apn-id": "pc:code1"}


def test_additional_tags_cannot_override_apn_id():
    cfg = AppConfig(
        partner=PartnerConfig(product_code="real-code"),
        additional_tags={"aws-apn-id": "pc:other"},
    )
    assert cfg.required_tags["aws-apn-id"] == "pc:real-code"


# ── classify_resource ─────────────────────────────────────────────────────────


def test_classify_compliant_all_tags():
    r = _record(existing_tags=dict(REQUIRED))
    assert classify_resource(r, REQUIRED) == TagStatus.COMPLIANT


def test_classify_compliant_apn_only():
    r = _record(existing_tags=dict(REQUIRED_APN_ONLY))
    assert classify_resource(r, REQUIRED_APN_ONLY) == TagStatus.COMPLIANT


def test_classify_missing_all():
    r = _record(existing_tags={})
    assert classify_resource(r, REQUIRED) == TagStatus.MISSING


def test_classify_missing_partial():
    r = _record(existing_tags={"aws-apn-id": REQUIRED["aws-apn-id"]})
    assert classify_resource(r, REQUIRED) == TagStatus.MISSING


def test_classify_conflict_apn_id():
    """aws-apn-id with different value → CONFLICT."""
    r = _record(existing_tags={"aws-apn-id": "pc:DIFFERENT_PARTNER"})
    assert classify_resource(r, REQUIRED) == TagStatus.CONFLICT


def test_classify_conflict_empty_existing_apn_id():
    r = _record(existing_tags={"aws-apn-id": ""})
    assert classify_resource(r, REQUIRED) == TagStatus.CONFLICT


def test_classify_conflict_additional_tag():
    """Additional tag with different value → CONFLICT."""
    r = _record(
        existing_tags={
            "aws-apn-id": REQUIRED["aws-apn-id"],
            "ManagedBy": "SomeOtherCompany",
        }
    )
    assert classify_resource(r, REQUIRED) == TagStatus.CONFLICT


def test_classify_conflict_additional_tag_only():
    """CONFLICT even when aws-apn-id is absent but another tag conflicts."""
    r = _record(existing_tags={"ManagedBy": "Manual"})
    required = {"aws-apn-id": "pc:code", "ManagedBy": "TestPartner"}
    assert classify_resource(r, required) == TagStatus.CONFLICT


def test_classify_missing_when_additional_tag_absent():
    """Additional tag absent (not conflicting) → MISSING, not CONFLICT."""
    r = _record(existing_tags={"aws-apn-id": REQUIRED["aws-apn-id"]})
    assert classify_resource(r, REQUIRED) == TagStatus.MISSING


# ── _tags_to_apply ────────────────────────────────────────────────────────────


def test_tags_to_apply_all_missing():
    r = _record(existing_tags={})
    tags = _tags_to_apply(r, REQUIRED)
    assert tags == REQUIRED


def test_tags_to_apply_none_missing():
    r = _record(existing_tags=dict(REQUIRED))
    tags = _tags_to_apply(r, REQUIRED)
    assert tags == {}


def test_tags_to_apply_partial():
    r = _record(existing_tags={"aws-apn-id": REQUIRED["aws-apn-id"]})
    tags = _tags_to_apply(r, REQUIRED)
    assert "aws-apn-id" not in tags
    assert "ManagedBy" in tags
    assert "ManagementScope" in tags


def test_tags_to_apply_never_returns_conflicting_key():
    r = _record(existing_tags={"ManagedBy": "another-owner"})
    assert _tags_to_apply(r, {"ManagedBy": "expected", "aws-apn-id": "pc:test"}) == {"aws-apn-id": "pc:test"}


# ── apply_tags — audit never writes ──────────────────────────────────────────


def test_audit_never_calls_aws_on_missing(mocker):
    """audit (dry_run=True) must never call any AWS API."""
    r = _record(existing_tags={}, status=TagStatus.MISSING)
    session = mocker.MagicMock()
    result = apply_tags(session, r, REQUIRED, dry_run=True)
    assert result.action == Action.DRY_RUN
    session.client.assert_not_called()


def test_audit_never_calls_aws_on_conflict(mocker):
    """audit must not call AWS even for CONFLICT resources."""
    r = _record(existing_tags={"aws-apn-id": "pc:OTHER"}, status=TagStatus.CONFLICT)
    session = mocker.MagicMock()
    result = apply_tags(session, r, REQUIRED, dry_run=True)
    assert result.action == Action.SKIPPED
    session.client.assert_not_called()


def test_audit_never_calls_aws_on_compliant(mocker):
    """audit must not call AWS for COMPLIANT resources."""
    r = _record(existing_tags=dict(REQUIRED), status=TagStatus.COMPLIANT)
    session = mocker.MagicMock()
    result = apply_tags(session, r, REQUIRED, dry_run=True)
    assert result.action == Action.NONE
    session.client.assert_not_called()


# ── apply_tags — apply only MISSING ──────────────────────────────────────────


def test_apply_tags_missing_calls_aws(mocker):
    """apply (dry_run=False) must call tag_resources for MISSING resources."""
    r = _record(existing_tags={}, status=TagStatus.MISSING)
    mock_client = mocker.MagicMock()
    mock_client.get_resources.return_value = {"ResourceTagMappingList": []}
    mock_client.tag_resources.return_value = {"FailedResourcesMap": {}}
    session = mocker.MagicMock()
    session.client.return_value = mock_client
    result = apply_tags(session, r, REQUIRED, dry_run=False)
    mock_client.tag_resources.assert_called_once()
    assert result.action == Action.TAG_APPLIED
    assert result.status == TagStatus.COMPLIANT


def test_apply_tags_conflict_never_writes(mocker):
    """apply must never write to CONFLICT resources."""
    r = _record(existing_tags={"aws-apn-id": "pc:OTHER"}, status=TagStatus.CONFLICT)
    session = mocker.MagicMock()
    result = apply_tags(session, r, REQUIRED, dry_run=False)
    assert result.action == Action.SKIPPED
    session.client.assert_not_called()


def test_apply_defensively_reclassifies_incorrect_missing_conflict(mocker):
    r = _record(existing_tags={"ManagedBy": "someone-else"}, status=TagStatus.MISSING)
    session = mocker.MagicMock()
    result = apply_tags(session, r, {"ManagedBy": "expected"}, dry_run=False)
    assert result.status == TagStatus.CONFLICT
    assert result.action == Action.SKIPPED
    session.client.assert_not_called()


def test_apply_tags_compliant_no_action(mocker):
    """apply must not write to already COMPLIANT resources."""
    r = _record(existing_tags=dict(REQUIRED), status=TagStatus.COMPLIANT)
    session = mocker.MagicMock()
    result = apply_tags(session, r, REQUIRED, dry_run=False)
    assert result.action == Action.NONE
    session.client.assert_not_called()


# ── Idempotency ───────────────────────────────────────────────────────────────


def test_idempotency_second_apply_is_noop(mocker):
    """After a successful apply, a second apply must produce no changes."""
    r = _record(existing_tags={}, status=TagStatus.MISSING)
    mock_client = mocker.MagicMock()
    mock_client.get_resources.return_value = {"ResourceTagMappingList": []}
    mock_client.tag_resources.return_value = {"FailedResourcesMap": {}}
    session = mocker.MagicMock()
    session.client.return_value = mock_client

    r = apply_tags(session, r, REQUIRED, dry_run=False)
    assert r.status == TagStatus.COMPLIANT
    assert r.action == Action.TAG_APPLIED

    r2 = apply_tags(session, r, REQUIRED, dry_run=False)
    assert r2.action == Action.NONE
    assert mock_client.tag_resources.call_count == 1  # called only once total


def test_apply_rechecks_and_skips_new_conflict(mocker):
    r = _record(existing_tags={}, status=TagStatus.MISSING)
    mock_client = mocker.MagicMock()
    mock_client.get_resources.return_value = {
        "ResourceTagMappingList": [
            {
                "ResourceARN": r.resource_arn,
                "Tags": [{"Key": "aws-apn-id", "Value": "pc:other"}],
            }
        ]
    }
    session = mocker.MagicMock()
    session.client.return_value = mock_client

    result = apply_tags(session, r, REQUIRED_APN_ONLY, dry_run=False)

    assert result.status == TagStatus.CONFLICT
    assert result.action == Action.SKIPPED
    mock_client.tag_resources.assert_not_called()


def test_apply_handles_failed_resources_map(mocker):
    r = _record(existing_tags={}, status=TagStatus.MISSING)
    mock_client = mocker.MagicMock()
    mock_client.get_resources.return_value = {"ResourceTagMappingList": []}
    mock_client.tag_resources.return_value = {
        "FailedResourcesMap": {r.resource_arn: {"ErrorCode": "AccessDeniedException", "ErrorMessage": "denied"}}
    }
    session = mocker.MagicMock()
    session.client.return_value = mock_client

    result = apply_tags(session, r, REQUIRED_APN_ONLY, dry_run=False)

    assert result.status == TagStatus.ERROR
    assert result.action == Action.SKIPPED
    assert "AccessDeniedException" in result.error


def test_build_providers_uses_native_ec2():
    providers = build_providers(["ec2", "s3"], resource_types={"ec2": ["instance", "snapshot"]})
    assert isinstance(providers[0], Ec2NativeProvider)
    assert isinstance(providers[1], NativeInventoryProvider)


def test_s3_native_discovers_untagged_bucket(mocker):
    client = mocker.MagicMock()
    client.list_buckets.return_value = {"Buckets": [{"Name": "example-bucket"}]}
    client.get_bucket_location.return_value = {"LocationConstraint": None}
    client.get_bucket_tagging.side_effect = ClientError(
        {"Error": {"Code": "NoSuchTagSet", "Message": "none"}}, "GetBucketTagging"
    )
    session = mocker.MagicMock()
    session.client.return_value = client
    session.get_partition_for_region.return_value = "aws"

    records = list(NativeInventoryProvider("s3", ["bucket"]).discover(session, "us-east-1", "123456789012", "test"))

    assert len(records) == 1
    assert records[0].resource_arn == "arn:aws:s3:::example-bucket"
    assert records[0].existing_tags == {}


def test_s3_apply_merges_existing_tags_without_removing(mocker):
    record = _record(service="s3", resource_type="bucket", resource_arn="arn:aws:s3:::example-bucket", native_id="example-bucket")
    client = mocker.MagicMock()
    client.get_bucket_tagging.return_value = {"TagSet": [{"Key": "Owner", "Value": "platform"}]}
    session = mocker.MagicMock()
    session.client.return_value = client

    result = apply_tags(session, record, REQUIRED_APN_ONLY, dry_run=False)

    assert result.status == TagStatus.COMPLIANT
    client.put_bucket_tagging.assert_called_once_with(
        Bucket="example-bucket",
        Tagging={"TagSet": [{"Key": "Owner", "Value": "platform"}, {"Key": "aws-apn-id", "Value": "pc:testcode123"}]},
    )


def test_s3_apply_rechecks_conflict_and_never_writes(mocker):
    record = _record(service="s3", resource_type="bucket", resource_arn="arn:aws:s3:::example-bucket", native_id="example-bucket")
    client = mocker.MagicMock()
    client.get_bucket_tagging.return_value = {"TagSet": [{"Key": "aws-apn-id", "Value": "pc:different"}]}
    session = mocker.MagicMock()
    session.client.return_value = client

    result = apply_tags(session, record, REQUIRED_APN_ONLY, dry_run=False)

    assert result.status == TagStatus.CONFLICT
    assert result.action == Action.SKIPPED
    client.put_bucket_tagging.assert_not_called()


def test_lambda_apply_uses_native_api_and_preserves_existing_tags(mocker):
    record = _record(
        service="lambda",
        resource_type="function",
        resource_arn="arn:aws:lambda:us-east-1:123456789012:function:example",
        native_id="arn:aws:lambda:us-east-1:123456789012:function:example",
    )
    client = mocker.MagicMock()
    client.list_tags.return_value = {"Tags": {"Owner": "platform"}}
    session = mocker.MagicMock()
    session.client.return_value = client

    result = apply_tags(session, record, REQUIRED_APN_ONLY, dry_run=False)

    assert result.status == TagStatus.COMPLIANT
    client.tag_resource.assert_called_once_with(Resource=record.resource_arn, Tags=REQUIRED_APN_ONLY)
    session.client.assert_called_with("lambda", region_name="us-east-1")


def test_all_v1_services_build_native_providers():
    services = [
        "s3", "rds", "elasticloadbalancing", "lambda", "ecs", "eks", "dynamodb", "elasticache", "efs",
        "backup", "secretsmanager", "sns", "sqs", "apigateway", "cloudfront", "route53",
    ]
    assert all(isinstance(provider, NativeInventoryProvider) for provider in build_providers(services))


def test_ec2_native_discovers_zero_tagged_snapshot(mocker):
    client = mocker.MagicMock()
    client.can_paginate.return_value = False
    client.describe_snapshots.return_value = {"Snapshots": [{"SnapshotId": "snap-123", "Tags": []}]}
    session = mocker.MagicMock()
    session.client.return_value = client
    session.get_partition_for_region.return_value = "aws"

    records = list(Ec2NativeProvider(["snapshot"]).discover(session, "us-east-1", "123456789012", "test"))

    assert len(records) == 1
    assert records[0].resource_arn.endswith(":snapshot/snap-123")
    assert records[0].existing_tags == {}
    client.describe_snapshots.assert_called_once_with(OwnerIds=["self"])


# ── resource_type filter ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "resource_type,expected",
    [
        ("instance", True),
        ("volume", True),
        ("vpc", True),
        ("subnet", True),
        ("internet-gateway", True),
        ("natgateway", True),
        ("elastic-ip", True),
        ("vpc-endpoint", True),
        ("snapshot", False),
        ("security-group", False),
        ("route-table", False),
        ("dhcp-options", False),
        ("network-acl", False),
        ("network-interface", False),
        ("ami", False),
        ("image", False),
    ],
)
def test_ec2_resource_type_filter(resource_type, expected):
    allowed = ["instance", "volume", "vpc", "subnet", "internet-gateway", "natgateway", "elastic-ip", "vpc-endpoint"]
    provider = TaggingAPIProvider("ec2", allowed_resource_types=allowed)
    arn = f"arn:aws:ec2:us-east-1:123456789012:{resource_type}/res-abc123"
    assert provider._is_allowed_type(arn) is expected


def test_resource_type_filter_none_allows_all():
    """When allowed_resource_types is None, all resource types pass."""
    provider = TaggingAPIProvider("ec2", allowed_resource_types=None)
    assert provider._is_allowed_type("arn:aws:ec2:us-east-1:123456789012:snapshot/snap-abc") is True
    assert provider._is_allowed_type("arn:aws:ec2:us-east-1:123456789012:security-group/sg-abc") is True


def test_resource_type_filter_security_group_allowed_when_configured():
    """A different client can include security-group in their config."""
    provider = TaggingAPIProvider("ec2", allowed_resource_types=["instance", "security-group"])
    assert provider._is_allowed_type("arn:aws:ec2:us-east-1:123456789012:security-group/sg-abc") is True
    assert provider._is_allowed_type("arn:aws:ec2:us-east-1:123456789012:snapshot/snap-abc") is False


# ── ARN parsing ───────────────────────────────────────────────────────────────


def test_resource_type_from_arn_slash():
    arn = "arn:aws:ec2:us-east-1:123456789012:instance/i-abc123"
    assert TaggingAPIProvider._resource_type_from_arn(arn) == "instance"


def test_resource_type_from_arn_volume():
    arn = "arn:aws:ec2:us-east-1:123456789012:volume/vol-abc123"
    assert TaggingAPIProvider._resource_type_from_arn(arn) == "volume"


def test_resource_type_from_arn_lambda_colon():
    arn = "arn:aws:lambda:us-east-1:123456789012:function:my-function"
    assert TaggingAPIProvider._resource_type_from_arn(arn) == "function"


def test_resource_type_from_arn_s3():
    arn = "arn:aws:s3:::my-bucket"
    assert TaggingAPIProvider._resource_type_from_arn(arn) == "bucket"


def test_resource_type_from_arn_rds():
    arn = "arn:aws:rds:us-east-1:123456789012:db:my-database"
    assert TaggingAPIProvider._resource_type_from_arn(arn) == "db"


@pytest.mark.parametrize(
    "arn",
    [
        "not-an-arn",
        "arn:aws:ec2:us-east-1:123456789012:",
        "arn:aws:ec2:us-east-1:123456789012:instance/",
        "arn:aws:ec2:us-east-1:123456789012:i-ambiguous",
    ],
)
def test_invalid_or_ambiguous_arn_is_denied(arn):
    provider = TaggingAPIProvider("ec2", allowed_resource_types=None)
    assert provider._is_allowed_type(arn) is False


def test_arn_service_must_match_provider():
    provider = TaggingAPIProvider("s3", allowed_resource_types=None)
    assert provider._is_allowed_type("arn:aws:ec2:us-east-1:123456789012:instance/i-example") is False


# ── Config loading ────────────────────────────────────────────────────────────


def test_config_partner_product_code(tmp_path):
    yaml_content = """
partner:
  product_code: "abc123xyz"
additional_tags:
  ManagedBy: "Acme"
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)
    cfg = AppConfig.load(str(cfg_file))
    assert cfg.partner.product_code == "abc123xyz"
    assert cfg.required_tags["aws-apn-id"] == "pc:abc123xyz"
    assert cfg.required_tags["ManagedBy"] == "Acme"


def test_config_without_additional_tags(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text('partner:\n  product_code: "example"\n', encoding="utf-8")
    cfg = AppConfig.load(str(cfg_file))
    assert cfg.additional_tags == {}
    assert cfg.required_tags == {"aws-apn-id": "pc:example"}


def test_config_without_resource_types_uses_safe_ec2_default(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text('partner:\n  product_code: "example"\n', encoding="utf-8")
    cfg = AppConfig.load(str(cfg_file))
    assert "ec2" in cfg.resource_types
    assert "instance" in cfg.resource_types["ec2"]


def test_config_legacy_required_tags_migration(tmp_path):
    """Old required_tags format is still loaded with a deprecation warning."""
    yaml_content = """
required_tags:
  aws-apn-id: "pc:legacycode"
  ManagedBy: "OldPartner"
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)
    cfg = AppConfig.load(str(cfg_file))
    assert cfg.partner.product_code == "legacycode"
    assert cfg.required_tags["aws-apn-id"] == "pc:legacycode"
    assert cfg.required_tags["ManagedBy"] == "OldPartner"


def test_config_resource_types_loaded(tmp_path):
    yaml_content = """
allowed_services:
  - ec2
  - s3
resource_types:
  ec2:
    include:
      - instance
      - vpc
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)
    cfg = AppConfig.load(str(cfg_file))
    assert cfg.resource_types == {"ec2": ["instance", "vpc"]}
    assert "s3" not in cfg.resource_types


def test_config_resource_types_default_has_ec2():
    """Default config includes ec2 resource type filter with instance and volume."""
    cfg = AppConfig.load(path="nonexistent.yaml")
    assert "ec2" in cfg.resource_types
    assert "instance" in cfg.resource_types["ec2"]
    assert "volume" in cfg.resource_types["ec2"]
    assert "snapshot" in cfg.resource_types["ec2"]


def test_config_defaults_no_ssm():
    """ssm must not be in default allowed_services."""
    cfg = AppConfig.load(path="nonexistent-path.yaml")
    assert "ssm" not in cfg.allowed_services


def test_example_config_enables_all_v1_services():
    cfg = AppConfig.load("config/config.example.yaml")
    assert set(cfg.allowed_services) == {
        "ec2", "s3", "rds", "elasticloadbalancing", "lambda", "ecs", "eks", "dynamodb", "elasticache",
        "efs", "backup", "secretsmanager", "sns", "sqs", "apigateway", "cloudfront", "route53",
    }


def test_config_from_yaml_full(tmp_path):
    yaml_content = """
partner:
  product_code: "testcode"
additional_tags:
  ManagedBy: "TestCo"
allowed_services:
  - ec2
  - s3
exclude_regions:
  - ap-east-1
organization:
  role_name: "MyRole"
  external_id: "MyExternalId"
  exclude_accounts:
    - "222222222222"
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)
    cfg = AppConfig.load(str(cfg_file))
    assert cfg.allowed_services == ["ec2", "s3"]
    assert cfg.exclude_regions == ["ap-east-1"]
    assert cfg.organization is not None
    assert cfg.organization.role_name == "MyRole"
    assert cfg.organization.external_id == "MyExternalId"
    assert "222222222222" in cfg.organization.exclude_accounts


# ── Organizations: account isolation ─────────────────────────────────────────


def test_organizations_account_error_does_not_propagate(mocker):
    """An exception in one account must not prevent processing of other accounts."""
    from bs4it_tagging.cli import _run_for_account

    config = AppConfig(
        partner=PartnerConfig(product_code="testcode"),
        include_regions=["us-east-1"],
    )

    session_ok = mocker.MagicMock()
    session_fail = mocker.MagicMock()

    mocker.patch(
        "bs4it_tagging.cli.get_enabled_regions",
        side_effect=[Exception("Simulated account failure"), ["us-east-1"]],
    )
    mocker.patch("bs4it_tagging.cli.build_providers", return_value=[])
    mocker.patch("bs4it_tagging.cli.discover_resources", return_value=[])
    mocker.patch("bs4it_tagging.cli.process_records", return_value=[])

    result_fail = _run_for_account(session_fail, "111111111111", "failing-account", config, dry_run=True)
    result_ok = _run_for_account(session_ok, "222222222222", "ok-account", config, dry_run=True)

    assert len(result_fail) == 1
    assert result_fail[0].status == TagStatus.ERROR
    assert result_ok == []


def test_tagging_api_discovery_error_is_reported_and_does_not_stop_next_provider(mocker):
    error = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetResources")
    failed = mocker.MagicMock(service="s3")
    failed.discover.side_effect = error
    successful = mocker.MagicMock(service="lambda")
    successful.discover.return_value = iter([_record(service="lambda")])

    records = discover_resources(
        mocker.MagicMock(),
        "123456789012",
        "example",
        ["us-east-1"],
        [failed, successful],
        REQUIRED_APN_ONLY,
    )

    assert [record.status for record in records] == [TagStatus.ERROR, TagStatus.MISSING]


def test_process_records_isolates_unexpected_tagging_failure(mocker):
    first = _record(resource_arn="arn:aws:ec2:us-east-1:123456789012:instance/i-first")
    second = _record(
        resource_arn="arn:aws:ec2:us-east-1:123456789012:instance/i-second",
        existing_tags=dict(REQUIRED_APN_ONLY),
    )
    mocker.patch("bs4it_tagging.tagging.apply_tags", side_effect=[RuntimeError("boom"), second])

    records = process_records(mocker.MagicMock(), [first, second], REQUIRED_APN_ONLY, dry_run=False)

    assert records[0].status == TagStatus.ERROR
    assert records[1] is second


def test_cli_rejects_missing_product_code(mocker):
    from bs4it_tagging.cli import main

    mocker.patch("bs4it_tagging.cli.AppConfig.load", return_value=AppConfig())
    get_session = mocker.patch("bs4it_tagging.cli.get_session")

    assert main(["audit"]) == 2
    get_session.assert_not_called()


# ── ResourceRecord.get_tag ────────────────────────────────────────────────────


def test_resource_record_get_tag():
    r = _record(existing_tags={"ManagedBy": "Acme", "aws-apn-id": "pc:code"})
    assert r.get_tag("ManagedBy") == "Acme"
    assert r.get_tag("aws-apn-id") == "pc:code"
    assert r.get_tag("NonExistent") == ""
    assert r.aws_apn_id == "pc:code"


def test_report_escapes_spreadsheet_formulas(tmp_path):
    from bs4it_tagging.reporting import generate_report

    record = _record(account_name='=HYPERLINK("https://example.invalid")')
    path = generate_report([record], str(tmp_path), REQUIRED_APN_ONLY)
    assert "'=HYPERLINK" in path.read_text(encoding="utf-8")
