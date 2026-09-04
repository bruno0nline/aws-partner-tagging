# AWS PRM Resource Tagging

`prm-tagger` is an independent Python CLI for discovering, auditing, reporting, and safely adding AWS Partner Revenue Measurement (PRM) resource tags in one AWS account or across AWS Organizations. It is not an official AWS tool.

The complete onboarding and audit workflow has been validated successfully in a real AWS Organizations environment, including management-account detection, service-managed StackSet provisioning, member-account role deployment, and an Organization-wide read-only audit. The validation did not move accounts or OUs and did not apply tags.

The required PRM tag is:

```text
aws-apn-id = pc:<product-code>
```

`ManagedBy`, `ManagementScope`, and every entry under `additional_tags` are optional Partner governance tags. They are not AWS PRM requirements.

The internal Python package remains `bs4it_tagging` for backward compatibility. The product name and supported CLI are AWS PRM Resource Tagging and `prm-tagger`.

## Quick start

### 1. Prerequisites

- Python 3.11 or newer, Git, AWS CLI, and a configured default AWS Region.
- Authenticated AWS credentials with read permissions for audit.
- For automatic Organizations onboarding, run from the management account with permission to inspect Organizations, manage service-managed CloudFormation StackSets, activate trusted access, create the named IAM role through CloudFormation, and later call `sts:AssumeRole`.
- Before apply, review current AWS PRM eligibility and obtain change approval.

### 2. Clone and bootstrap

```bash
git clone https://github.com/<owner>/aws-partner-tagging.git
cd aws-partner-tagging
bash bootstrap.sh
```

If only `bootstrap.sh` is present and the project has not been cloned:

```bash
bash bootstrap.sh https://github.com/<owner>/aws-partner-tagging.git
```

The script is idempotent, does not pull automatically, and never overwrites `config/config.yaml`.

After installing the CLI, bootstrap reads the current STS identity and checks AWS Organizations:

- In a standalone account, it finishes without creating AWS infrastructure.
- In an Organization member account, it stops and directs the analyst to run from the management account.
- In the management account, it inventories active member accounts, roots, and nested OUs without moving anything. It then displays the complete infrastructure plan and requires the analyst to type `provision` before any AWS change.
- Once confirmed, it activates StackSets trusted access when needed, creates or updates the service-managed `PRM-Resource-Tagging` StackSet, targets the existing Organization roots, waits for operations, and validates Stack Instance results.

Bootstrap never runs `audit` or `apply`. Declining the confirmation leaves AWS unchanged. Re-running bootstrap updates the existing StackSet and Stack Instances rather than creating a second deployment.

### 3. Configure scope and product code

Edit the ignored local file:

```yaml
partner:
  product_code: "<your-product-code>"

additional_tags:
  ManagedBy: "<your-company>"
  ManagementScope: "ManagedServices"

include_regions: []

organization:
  role_name: "PRM-TaggingRole"
  external_id: "PRMTaggingAutomation"
  exclude_accounts: []
```

An empty `include_regions` discovers every enabled or opted-in Region returned by `ec2:DescribeRegions`, minus `exclude_regions`. Set an explicit list to constrain execution. Never store AWS credentials in this YAML.

`PRMTaggingAutomation` is a public example ExternalId, not a secret. Prefer a unique value per customer and pass the same value to the StackSet and local configuration.

The checked-in example intentionally enables only explicitly listed EC2 resource types. Add services and resource types only after verifying that they are part of the Partner solution and eligible under the current AWS PRM documentation.

### 4. Review and confirm `PRM-TaggingRole` provisioning

When bootstrap prints `AWS CHANGE PLAN`, verify the management account, Region, discovered account count, Organization roots, StackSet action, and role name. Type `provision` only after review. The management account is not given a Stack Instance; local audits there use caller credentials.

### 5. Audit, review, apply, audit again

```bash
prm-tagger audit --organization
ls -1 reports/tagging-report-*.csv
prm-tagger apply --organization
prm-tagger audit --organization
```

For one account, omit `--organization`. `apply` requires typing `apply`; `--yes` is intended only for automation with an external approval control.

The intended operational sequence is:

1. `bootstrap.sh` installs the CLI, detects the AWS environment, and—after explicit confirmation—prepares Organizations infrastructure.
2. The service-managed `PRM-Resource-Tagging` StackSet deploys `PRM-TaggingRole` to member accounts.
3. `prm-tagger audit --organization` performs read-only discovery and produces a local report.
4. An analyst reviews scope, eligibility, conflicts, errors, and proposed missing tags.
5. Only after approval, the analyst runs `prm-tagger apply --organization`; bootstrap never runs it automatically.

## AWS PRM scope

The tool derives `aws-apn-id` only from `partner.product_code`. AWS PRM eligibility changes over time and can be narrower than a service namespace. A resource being taggable does not make it PRM eligible. Configure `allowed_services` and `resource_types` to the services/resources used by the Partner solution and compare them with the current [AWS PRM resource-tagging guidance](https://docs.aws.amazon.com/PRM/latest/aws-prm-onboarding-guide/resource-tagging.html) and [included-services list](https://docs.aws.amazon.com/PRM/latest/aws-prm-onboarding-guide/resource-tagging-included-services.html).

For IaC-managed resources, prefer adding the PRM tag in CloudFormation, Terraform, or CDK to avoid drift.

### V1 supported services

V1 provides native inventory for EC2/EBS (including Transit Gateway), S3, RDS, Elastic Load Balancing, Lambda, ECS, EKS, DynamoDB, ElastiCache, EFS, AWS Backup, Secrets Manager, SNS, SQS, API Gateway REST APIs, CloudFront, and Route 53 hosted zones. The checked-in configuration keeps every service and resource type explicitly selectable.

S3 uses its native APIs for discovery, tag reads, and tag writes. Before `PutBucketTagging`, the tool reads the current tag set again, treats any divergent desired value as `CONFLICT`, and merges absent tags with all existing tags because the S3 operation replaces the complete tag set.

## Status model

| Status | Meaning | Automatic behavior |
|---|---|---|
| `COMPLIANT` | Every desired tag exists with the configured value. | No write. |
| `MISSING` | At least one desired tag is absent and none conflicts. | Audit reports `DRY_RUN`; apply adds only absent keys. |
| `CONFLICT` | Any desired key already exists with a different value. | The entire resource is skipped; no tag is changed. |
| `ERROR` | Discovery, parsing, permission, or tagging failed. | Reported and skipped. |

## Safety model

- Audit calls read APIs only. It writes a local CSV report, never AWS tags.
- Apply processes only records classified `MISSING` and submits only absent keys.
- A conflict in `aws-apn-id` or any optional additional tag blocks all changes to that resource.
- Existing tags are never intentionally removed; the code contains no untag operation.
- Apply refreshes current tags immediately before writing and skips a newly conflicting or compliant resource.
- `FailedResourcesMap` and API failures become `ERROR`.
- Account and provider failures are isolated and reported so later accounts/providers continue.
- A successful `audit -> apply -> audit` sequence is idempotent: the last audit is `COMPLIANT` and no repeat tag write is needed.

AWS has no universal atomic “set this tag only if absent” operation. A different writer can change a key in the very small interval between the final read and `TagResources`; avoid concurrent tag writers during apply. IAM authorizes API capabilities, not the CLI safety algorithm: tightly control who may assume `PRM-TaggingRole`.

## Discovery architecture and known limitations

Discovery uses an extensible provider pattern:

- `Ec2NativeProvider` calls native read-only EC2 APIs and finds completely untagged instances, volumes, account-owned snapshots, VPCs, subnets, internet gateways, NAT gateways, Elastic IP allocations, VPC endpoints, transit gateways, and attachments when those types are configured.
- Other configured services use Resource Groups Tagging API `GetResources`.

`GetResources` returns tagged or previously tagged resources and [does not return never-tagged resources](https://docs.aws.amazon.com/resourcegroupstagging/latest/APIReference/API_GetResources.html). V1 therefore uses native inventory for its supported services. Services added only through the generic provider retain the `GetResources` limitation. `filter_tags` applies only to that generic provider. Global services are inventoried once per account to avoid duplicate records.

V1 intentionally limits several broad services to their principal billable resource types: RDS DB instances/clusters, Classic and ELBv2 load balancers, ECS clusters/services, EKS clusters, ElastiCache clusters, Backup vaults, API Gateway REST APIs, Route 53 hosted zones, and the explicit EC2 list in the example configuration. It does not yet inventory API Gateway v2 APIs, Route 53 health checks, ECS tasks/task definitions, RDS snapshots/proxies, or every secondary resource exposed by those services.

Malformed or ambiguous ARNs are skipped rather than tagged. Both `resource-type/id` and `resource-type:id` ARN formats are supported; S3 bucket ARNs are recognized as type `bucket`. Add native providers deliberately instead of assuming broad coverage.

## AWS Organizations / StackSets

```text
Management Account
        |
        +-- local execution with caller credentials
        |
        +-- Member Accounts
              |
              +-- PRM-TaggingRole
                    ^
                    |
               sts:AssumeRole
```

Service-managed StackSets integrate with AWS Organizations. Bootstrap uses the existing Organization roots as deployment targets, so all current member accounts are covered and automatic deployment covers accounts later added beneath those roots. It enumerates nested OUs for the plan but never creates, deletes, or moves an OU or account. CloudFormation [does not deploy service-managed stack instances to the management account](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/stacksets-orgs-associate-stackset-with-org.html).

The administration and deployment Region is the Region selected by the normal AWS SDK configuration (`AWS_REGION`, `AWS_DEFAULT_REGION`, or the configured profile). Set it explicitly before bootstrap if needed:

```bash
export AWS_REGION="<stackset-administration-region>"
bash bootstrap.sh
```

The automated workflow uses `infrastructure/cloudformation/tagging-role.yaml`, StackSet name `PRM-Resource-Tagging`, role name `PRM-TaggingRole`, service-managed permissions, automatic deployment, and `RetainStacksOnAccountRemoval=false`. It creates or updates the same resources on rerun.

The IAM role is global, so one enabled deployment Region per account is sufficient. Inspect the StackSet operation before auditing. The supplied role contains only Tagging API discovery/write, native EC2 discovery/tagging, and explicit tag-removal denies. If additional services require service-specific permissions behind `TagResources`, extend the customer-local policy narrowly and review it before deployment. See [StackSets concepts](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/stacksets-concepts.html), [service-managed StackSets](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/stacksets-orgs-associate-stackset-with-org.html), [AWS Organizations](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_introduction.html), and [IAM AssumeRole](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html).

## Troubleshooting

- **AccessDenied during Organizations discovery:** the caller needs `organizations:ListAccounts`; verify that execution is from an authorized management/delegated account.
- **Bootstrap reports a member account:** automatic provisioning intentionally requires the management account so that management-account detection and StackSet ownership are unambiguous.
- **Trusted-access failure:** verify permission for CloudFormation `ActivateOrganizationsAccess`, Organizations service access, and service-linked role creation. Bootstrap stops and reports the AWS error.
- **StackSet partial failure:** inspect the reported StackSet operation and failed Stack Instance, correct permissions/quotas in that member account, then rerun bootstrap. The existing StackSet is updated idempotently.
- **AssumeRole fails:** confirm the role exists in that member account, the caller can call `sts:AssumeRole`, and the role trust principal points to the correct management account.
- **ExternalId mismatch:** `organization.external_id` must exactly match the StackSet `ExternalId` parameter. Treat a unique customer value as defense in depth, not as a password.
- **Describe failure:** add only the read permission required by the configured native provider and verify the Region is enabled.
- **Tagging failure:** `tag:TagResources` and, where required, the target service's native tagging permission must be allowed. Review `FailedResourcesMap` in the CSV error column.
- **Untagged resource absent:** this is expected for services still using `GetResources`; use a native provider or a separately reviewed inventory source.
- **CONFLICT:** inspect the existing value and its owner. The tool will not overwrite it; resolve ownership manually or update the source-of-truth IaC.

## Local validation

These commands do not contact AWS:

```bash
python -m pip install -e ".[dev]"
python -m bs4it_tagging --help
python -m pytest -q
ruff check .
```

Reports can contain account IDs, account names, ARNs, and tag values. They and `config/config.yaml` are ignored by Git; store and share them as sensitive operational data.

## Official references

- [AWS Partner Revenue Measurement](https://docs.aws.amazon.com/PRM/latest/aws-prm-onboarding-guide/what-is-aws-prm.html)
- [AWS PRM resource tagging](https://docs.aws.amazon.com/PRM/latest/aws-prm-onboarding-guide/resource-tagging.html)
- [Resource Groups Tagging API](https://docs.aws.amazon.com/resourcegroupstagging/latest/APIReference/Welcome.html)
- [AWS Organizations](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_introduction.html)
- [CloudFormation StackSets](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/what-is-cfnstacksets.html)
- [IAM/STS AssumeRole](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html)
