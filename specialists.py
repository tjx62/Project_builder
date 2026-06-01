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

from crewai import Agent


_DEFAULT_CONTEXT = "No additional organizational context provided."


def _backstory(base: str, additional_context: str | None) -> str:
    """Append org guidelines so they land in the cacheable system prompt."""
    if not additional_context or additional_context == _DEFAULT_CONTEXT:
        return base
    return f"{base}\n\n# Organizational Guidelines\n{additional_context}"


# ==========================================
# AWS SPECIALISTS
# ==========================================

def vpc_specialist(llm, additional_context: str | None = None):
    return Agent(
        role='AWS VPC & Networking Specialist',
        goal='Design VPCs, subnets, route tables, NAT gateways, and security groups.',
        backstory=_backstory(
            'You are a network architect who has spent a decade designing secure, multi-AZ AWS network topologies. You always start with the network because everything else depends on it.',
            additional_context,
        ),
        llm=llm,
        allow_delegation=False,
        max_iter=3
    )


def ec2_specialist(llm, additional_context: str | None = None):
    return Agent(
        role='AWS EC2 Specialist',
        goal='Design EC2 instances, AMIs, instance types, autoscaling groups, and launch templates.',
        backstory=_backstory(
            'You know AWS compute inside and out. You pick the right instance family, size, and AMI for the workload, and you always place instances in the appropriate subnets the networking team designed.',
            additional_context,
        ),
        llm=llm,
        allow_delegation=False,
        max_iter=3
    )


def rds_specialist(llm, additional_context: str | None = None):
    return Agent(
        role='AWS RDS & Database Specialist',
        goal='Design RDS instances, Aurora clusters, parameter groups, and backup strategies.',
        backstory=_backstory(
            'You have deep experience with managed AWS databases. You handle engine selection, multi-AZ deployments, encryption at rest, and read replicas.',
            additional_context,
        ),
        llm=llm,
        allow_delegation=False,
        max_iter=3
    )


def s3_specialist(llm, additional_context: str | None = None):
    return Agent(
        role='AWS S3 & Storage Specialist',
        goal='Design S3 buckets, bucket policies, lifecycle rules, and storage classes.',
        backstory=_backstory(
            'You design S3 buckets for security, durability, and cost optimization. You always enable versioning, encryption, and access logging by default.',
            additional_context,
        ),
        llm=llm,
        allow_delegation=False,
        max_iter=3
    )


def iam_specialist(llm, additional_context: str | None = None):
    return Agent(
        role='AWS IAM & Security Specialist',
        goal='Design IAM roles, policies, instance profiles, and trust relationships.',
        backstory=_backstory(
            'You enforce least-privilege access. You never grant wildcard permissions and you always scope policies to specific resources.',
            additional_context,
        ),
        llm=llm,
        allow_delegation=False,
        max_iter=3
    )


def lambda_specialist(llm, additional_context: str | None = None):
    return Agent(
        role='AWS Lambda & Serverless Specialist',
        goal='Design Lambda functions, runtimes, event sources, and IAM execution roles.',
        backstory=_backstory(
            'You build serverless architectures. You pick the right memory/timeout settings, handle cold starts, and integrate Lambda with the rest of the AWS ecosystem.',
            additional_context,
        ),
        llm=llm,
        allow_delegation=False,
        max_iter=3
    )


# ==========================================
# LANGUAGE SPECIALISTS
# ==========================================

def python_specialist(llm, additional_context: str | None = None):
    return Agent(
        role='Senior Python Developer',
        goal='Write idiomatic, well-tested Python code following PEP 8 and modern best practices.',
        backstory=_backstory(
            'You have written production Python for over a decade. You favor explicit over implicit, use type hints, and structure code into clean modules.',
            additional_context,
        ),
        llm=llm,
        allow_delegation=False,
        max_iter=3
    )


def javascript_specialist(llm, additional_context: str | None = None):
    return Agent(
        role='Senior JavaScript/TypeScript Developer',
        goal='Write modern JavaScript and TypeScript code following current best practices.',
        backstory=_backstory(
            'You build production Node.js services and frontend applications. You prefer TypeScript, async/await over callbacks, and modern ES module syntax.',
            additional_context,
        ),
        llm=llm,
        allow_delegation=False,
        max_iter=3
    )


def go_specialist(llm, additional_context: str | None = None):
    return Agent(
        role='Senior Go Developer',
        goal='Write idiomatic Go code following effective Go principles.',
        backstory=_backstory(
            'You write production Go services. You favor simplicity, explicit error handling, and standard library solutions over third-party dependencies.',
            additional_context,
        ),
        llm=llm,
        allow_delegation=False,
        max_iter=3
    )


# ==========================================
# REGISTRY
# ==========================================
# Maps short specialist ID → factory function.
# Add new specialists here.

# True  → specialist emits Terraform (HCL); the Terraform assembler should run.
# False → specialist emits application code; assembler is not needed.
SPECIALIST_PRODUCES_TERRAFORM = {
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
