# Organizational Engineering Standards & Guidelines

## Cloud Platform: AWS GovCloud (us-gov-west-1)

All infrastructure must be deployed to AWS GovCloud. Commercial region resources are prohibited for any workload handling CUI or FedRAMP-scoped data. Cross-region replication is permitted only to us-gov-east-1 and requires explicit approval from the ISSO.

## Resource Naming Convention

All AWS resources must follow this naming scheme:
`{env}-{team}-{service}-{resource-type}`

Examples:
- `prod-platform-auth-rds` — RDS instance for the auth service in production
- `dev-platform-logs-s3` — S3 bucket for logs in dev
- `staging-data-pipeline-lambda` — Lambda in staging for the data pipeline team

Valid environments: `prod`, `staging`, `dev`, `sandbox`
Valid teams: `platform`, `data`, `security`, `infra`

## Mandatory Resource Tagging

Every AWS resource must carry these tags or the Terraform plan will be rejected by CI:

| Tag Key         | Description                          | Example                    |
|-----------------|--------------------------------------|----------------------------|
| `Environment`   | Deployment tier                      | `prod`                     |
| `Team`          | Owning team                          | `platform`                 |
| `Service`       | Logical service name                 | `auth`                     |
| `CostCenter`    | FinOps cost center code              | `CC-1042`                  |
| `DataClass`     | Data classification                  | `CUI` or `Public`          |
| `ManagedBy`     | IaC tool                             | `terraform`                |
| `FedRAMPScope`  | Whether resource is in FedRAMP scope | `true` or `false`          |

## Encryption Requirements

### At Rest
- All S3 buckets: SSE-KMS with a customer-managed key (CMK). SSE-S3 is not permitted for FedRAMP-scoped buckets.
- All RDS instances: storage encryption enabled with CMK.
- All EBS volumes: encrypted with CMK.
- KMS keys must have key rotation enabled (`enable_key_rotation = true`).
- KMS key policies must restrict usage to the owning service's IAM role plus the security team's break-glass role.

### In Transit
- All ALB listeners must use HTTPS (TLS 1.2 minimum, TLS 1.3 preferred).
- S3 bucket policies must deny `aws:SecureTransport = false`.
- RDS parameter groups must enforce `ssl = 1`.

## IAM Hardening Rules

- No wildcard (`*`) actions or resources in any IAM policy attached to a service role.
- All service roles must use the principle of least privilege scoped to the specific resources the service touches.
- IAM roles must not have `AdministratorAccess` or `PowerUserAccess` managed policies.
- All human access must go through IAM Identity Center (SSO). Long-term access keys for human users are prohibited.
- Service accounts (non-human) must rotate credentials via AWS Secrets Manager with a maximum 30-day rotation period.
- MFA is required for all console access. Enforce with an IAM condition: `aws:MultiFactorAuthPresent = true`.

## S3 Bucket Hardening

Every S3 bucket must:
- Block all public access (`block_public_acls`, `block_public_policy`, `ignore_public_acls`, `restrict_public_buckets` all `true`).
- Enable versioning.
- Enable access logging to the designated audit log bucket (`prod-security-audit-logs-s3`).
- Apply a lifecycle policy: transition to S3 Intelligent-Tiering after 30 days, expire non-current versions after 90 days.
- Use bucket-owner-enforced ACL (`object_ownership = "BucketOwnerEnforced"`).

## VPC & Networking

- All compute must run in private subnets. No public-facing EC2 instances.
- NAT Gateways are required in each AZ for outbound internet access from private subnets.
- Security groups must follow least-privilege: no `0.0.0.0/0` ingress except on ALBs for ports 80/443.
- VPC Flow Logs must be enabled and sent to CloudWatch Logs with a 90-day retention policy.
- Network ACLs should be used as a secondary defense layer for subnet-level controls.

## Lambda Standards

- Runtime must be a currently supported version (Node 20.x, Python 3.12, Java 21).
- All Lambda functions must have a Dead Letter Queue (DLQ) configured (SQS or SNS).
- Reserved concurrency must be set to prevent runaway invocations.
- Environment variables must not contain secrets; use Secrets Manager or Parameter Store.
- Lambda execution roles must follow least-privilege per function.
- X-Ray tracing must be enabled (`tracing_config { mode = "Active" }`).

## Terraform Conventions

- Terraform version: `>= 1.6.0`
- AWS provider version: `>= 5.0`
- Remote state: S3 backend with DynamoDB locking. State bucket is `prod-infra-tfstate-s3`.
- All modules must declare `required_providers` and pin to a minor version range (e.g., `~> 5.0`).
- Use `terraform fmt` and `terraform validate` before every PR. CI enforces this.
- Outputs must be defined for every resource that downstream modules or services reference.
- Sensitive outputs must use `sensitive = true`.

## Compliance & Audit

- All changes to FedRAMP-scoped infrastructure must be reviewed by the ISSO before merge.
- CloudTrail must be enabled in all regions with log file validation and S3 log delivery to the audit bucket.
- AWS Config must be enabled with conformance packs for CIS AWS Foundations Benchmark and FedRAMP High.
- GuardDuty must be enabled and alerts routed to the Security Hub.
- Security Hub findings of severity HIGH or CRITICAL must be remediated within 30 days (HIGH) or 7 days (CRITICAL).
