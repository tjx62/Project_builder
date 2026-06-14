"""
Registry of specialist agents.

Each specialist is a focused expert in a single domain (an AWS service, a
programming language, etc.). The SPECIALISTS dict at the bottom maps a short
ID like 'ec2' or 'python' to a factory function that creates the agent.

To add a new specialist:
    1. Write a factory function below that returns an Agent.
    2. Add an entry to SPECIALISTS mapping a short ID to your factory.
    3. Add a one-line description to SPECIALIST_DESCRIPTIONS so the planner
       knows when to pick it.
"""

from executor import AgentSpec


_DEFAULT_CONTEXT = "No additional organizational context provided."


def _backstory(base: str, additional_context: str | None) -> str:
    """Append org guidelines so they land in the cacheable system prompt."""
    if not additional_context or additional_context == _DEFAULT_CONTEXT:
        return base
    return f"{base}\n\n# Organizational Guidelines\n{additional_context}"


# ==========================================
# AWS SPECIALISTS
# ==========================================

def vpc_specialist(tier, model, additional_context: str | None = None):
    return AgentSpec(
        role='AWS VPC & Networking Specialist',
        goal=(
            'Define precise network architecture requirements for VPCs, subnets, route tables, '
            'NAT gateways, and security groups as structured specifications. '
            'Express WHAT to build and WHY — not Terraform code.'
        ),
        backstory=_backstory(
            'You are a network architect who has spent a decade designing secure, multi-AZ AWS '
            'network topologies. You produce clear requirement specs that a Terraform author '
            'can implement without ambiguity. You always specify CIDR blocks, AZ placement, '
            'route table associations, and security group rules with source/destination precision.',
            additional_context,
        ),
        tier=tier,
        model=model,
    )


def ec2_specialist(tier, model, additional_context: str | None = None):
    return AgentSpec(
        role='AWS EC2 Specialist',
        goal=(
            'Define compute requirements for EC2 instances, autoscaling groups, and launch '
            'templates as structured specifications. Express WHAT to build — not Terraform code.'
        ),
        backstory=_backstory(
            'You know AWS compute inside and out. You pick the right instance family, size, '
            'and AMI for the workload and specify exactly which subnets, security groups, '
            'and IAM instance profiles the compute tier needs. You produce requirement specs '
            'a Terraform author can implement without asking follow-up questions.',
            additional_context,
        ),
        tier=tier,
        model=model,
    )


def rds_specialist(tier, model, additional_context: str | None = None):
    return AgentSpec(
        role='AWS RDS & Database Specialist',
        goal=(
            'Define database requirements for RDS instances and Aurora clusters as structured '
            'specifications. Express WHAT to build — not Terraform code.'
        ),
        backstory=_backstory(
            'You have deep experience with managed AWS databases. You specify engine, version, '
            'instance class, multi-AZ, encryption, backup retention, parameter group needs, '
            'and subnet group placement. You produce requirement specs a Terraform author can '
            'implement directly.',
            additional_context,
        ),
        tier=tier,
        model=model,
    )


def s3_specialist(tier, model, additional_context: str | None = None):
    return AgentSpec(
        role='AWS S3 & Storage Specialist',
        goal=(
            'Define storage requirements for S3 buckets, policies, and lifecycle rules as '
            'structured specifications. Express WHAT to build — not Terraform code.'
        ),
        backstory=_backstory(
            'You design S3 configurations for security, durability, and cost optimisation. '
            'You specify versioning, encryption type and key, public access settings, access '
            'logging targets, lifecycle transitions, and bucket policies. You always require '
            'versioning and encryption by default. You produce specs a Terraform author can '
            'implement without guessing.',
            additional_context,
        ),
        tier=tier,
        model=model,
    )


def iam_specialist(tier, model, additional_context: str | None = None):
    return AgentSpec(
        role='AWS IAM & Security Specialist',
        goal=(
            'Define access-control requirements for IAM roles, policies, and trust relationships '
            'as structured specifications. Express WHAT to build — not Terraform code.'
        ),
        backstory=_backstory(
            'You enforce least-privilege access. You specify trust policies (who can assume a '
            'role), permission policies (exact actions and resource ARNs or logical references), '
            'and instance profile associations. You never grant wildcard permissions and always '
            'scope to specific resources. You produce specs a Terraform author can implement '
            'precisely.',
            additional_context,
        ),
        tier=tier,
        model=model,
    )


def lambda_specialist(tier, model, additional_context: str | None = None):
    return AgentSpec(
        role='AWS Lambda & Serverless Specialist',
        goal=(
            'Define serverless compute requirements for Lambda functions, event sources, and '
            'execution roles as structured specifications. Express WHAT to build — not Terraform code.'
        ),
        backstory=_backstory(
            'You build serverless architectures. You specify runtime, memory, timeout, handler, '
            'environment variables, event source mappings, VPC placement (if needed), and the '
            'IAM permissions the execution role requires. You produce specs a Terraform author '
            'can implement without ambiguity.',
            additional_context,
        ),
        tier=tier,
        model=model,
    )


# ==========================================
# LANGUAGE SPECIALISTS
# ==========================================

def python_specialist(tier, model, additional_context: str | None = None):
    return AgentSpec(
        role='Senior Python Developer',
        goal='Write idiomatic, well-tested Python code following PEP 8 and modern best practices.',
        backstory=_backstory(
            'You have written production Python for over a decade. You favor explicit over implicit, use type hints, and structure code into clean modules.',
            additional_context,
        ),
        tier=tier,
        model=model,
    )


def javascript_specialist(tier, model, additional_context: str | None = None):
    return AgentSpec(
        role='Senior JavaScript/TypeScript Developer',
        goal='Write modern JavaScript and TypeScript code following current best practices.',
        backstory=_backstory(
            'You build production Node.js services and frontend applications. You prefer TypeScript, async/await over callbacks, and modern ES module syntax.',
            additional_context,
        ),
        tier=tier,
        model=model,
    )


def go_specialist(tier, model, additional_context: str | None = None):
    return AgentSpec(
        role='Senior Go Developer',
        goal='Write idiomatic Go code following effective Go principles.',
        backstory=_backstory(
            'You write production Go services. You favor simplicity, explicit error handling, and standard library solutions over third-party dependencies.',
            additional_context,
        ),
        tier=tier,
        model=model,
    )


# ==========================================
# REGISTRY
# ==========================================
# Maps short specialist ID → factory function.
# Add new specialists here.

# True  → AWS service specialist; outputs requirement specs for the Terraform specialist.
# False → language specialist; outputs application code files directly.
SPECIALIST_IS_AWS = {
    'vpc': True, 'ec2': True, 'rds': True, 's3': True, 'iam': True, 'lambda': True,
    'python': False, 'javascript': False, 'go': False,
}

SPECIALISTS = {
    'vpc': vpc_specialist,
    'ec2': ec2_specialist,
    'rds': rds_specialist,
    's3': s3_specialist,
    'iam': iam_specialist,
    'lambda': lambda_specialist,
    'python': python_specialist,
    'javascript': javascript_specialist,
    'go': go_specialist,
}

# Short descriptions shown to the planner so it knows when to pick each one.
# Keep these to one line — distinct and specific.
SPECIALIST_DESCRIPTIONS = {
    'vpc': 'AWS networking: VPCs, subnets, route tables, security groups, NAT gateways.',
    'ec2': 'AWS compute: EC2 instances, AMIs, autoscaling groups, launch templates.',
    'rds': 'AWS managed databases: RDS, Aurora, parameter groups, backups.',
    's3': 'AWS object storage: S3 buckets, policies, lifecycle rules.',
    'iam': 'AWS access control: IAM roles, policies, trust relationships.',
    'lambda': 'AWS serverless compute: Lambda functions, event sources, layers.',
    'python': 'Python application code (backend services, scripts, libraries).',
    'javascript': 'JavaScript/TypeScript code (Node.js backend, frontend apps).',
    'go': 'Go application code (services, CLIs, system tools).',
}
