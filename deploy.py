#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Synthetic Monitoring Deployment Script
Uses Nova Act CLI for automated deployment

Usage:
    python deploy.py                         # Deploy/update the workflow
    python deploy.py --email you@example.com # Deploy with SNS failure alerts
    python deploy.py --cleanup               # Remove all deployed resources
"""

import subprocess
import sys
import os
import argparse
import json
import time
import re
import shutil

# Resolve tool paths once at import — fixes Bandit B607 (partial executable paths)
AWS = shutil.which("aws")
DOCKER = shutil.which("docker")


def run_command(cmd, check=True):
    print(f"→ Executing: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr and check:
        print(result.stderr, file=sys.stderr)
    if check and result.returncode != 0:
        print(f"✗ Command failed with exit code {result.returncode}")
        sys.exit(result.returncode)
    return result


def check_prerequisites():
    print("=" * 80)
    print("🚀 DEPLOYING SYNTHETIC MONITORING")
    print("=" * 80)
    print()
    print("📋 Checking prerequisites...")
    print()

    print("→ Checking Python installation...")
    try:
        result = subprocess.run([sys.executable, "--version"], capture_output=True, text=True)
        print(f"✅ Python found: {result.stdout.strip()}")
    except Exception as e:
        print(f"❌ Python check failed: {e}")
        sys.exit(1)

    print("→ Checking Docker...")
    if not DOCKER:
        print("❌ Docker not found. Please install Docker")
        sys.exit(1)
    result = subprocess.run([DOCKER, "ps"], capture_output=True, text=True)
    if result.returncode == 0:
        print("✅ Docker is running")
    else:
        print("❌ Docker not running. Please start Docker")
        sys.exit(1)

    print("→ Checking AWS CLI...")
    if not AWS:
        print("❌ AWS CLI not found. Please install AWS CLI")
        sys.exit(1)
    print("✅ AWS CLI found")

    print("→ Verifying AWS credentials...")
    try:
        result = subprocess.run([AWS, "sts", "get-caller-identity"], capture_output=True, text=True)
        if result.returncode == 0:
            identity = json.loads(result.stdout)
            account_id = identity.get("Account")
            print("✅ AWS credentials configured")
            print(f"   Account ID: {account_id}")

            region = (
                os.environ.get("AWS_DEFAULT_REGION")
                or subprocess.run(
                    [AWS, "configure", "get", "region"],
                    capture_output=True, text=True
                ).stdout.strip()
                or "us-east-1"
            )
            print(f"   Region: {region}")
            return account_id, region
        else:
            print("❌ AWS credentials not configured. Run 'aws configure'")
            sys.exit(1)
    except Exception as e:
        print(f"❌ AWS credentials check failed: {e}")
        sys.exit(1)


def install_dependencies():
    print()
    print("=" * 80)
    print("📦 INSTALLING DEPENDENCIES")
    print("=" * 80)
    print()

    venv_path = ".venv"
    if not os.path.exists(venv_path):
        print(f"→ Creating virtual environment at {venv_path}...")
        run_command([sys.executable, "-m", "venv", venv_path])
        print("✅ Virtual environment created")
    else:
        print("✅ Virtual environment exists")

    if sys.platform == "win32":
        pip_path = os.path.join(venv_path, "Scripts", "pip")
        python_path = os.path.join(venv_path, "Scripts", "python")
    else:
        pip_path = os.path.join(venv_path, "bin", "pip")
        python_path = os.path.join(venv_path, "bin", "python")

    run_command([pip_path, "install", "--upgrade", "pip", "-q"])
    run_command([pip_path, "install", "-r", "requirements.txt", "-q"])
    print("✅ Dependencies installed")

    return python_path, pip_path


# FIX 1: create_sns_topic runs BEFORE deploy_workflow so the ARN can be
# injected as an env var into the container at deploy time.
def create_sns_topic(email):
    print()
    print("=" * 80)
    print("📧 CREATING/UPDATING SNS TOPIC")
    print("=" * 80)
    print()

    # Validate email before passing to subprocess
    if not re.fullmatch(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", email):
        print(f"❌ Invalid email address: {email}")
        sys.exit(1)

    topic_name = "synthetic-monitoring-alerts"
    print(f"→ Topic name: {topic_name}")
    print(f"→ Subscriber email: {email}")
    print()

    result = subprocess.run(
        [AWS, "sns", "create-topic", "--name", topic_name, "--query", "TopicArn", "--output", "text"],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        print(f"❌ Failed to create SNS topic: {result.stderr}")
        return None, topic_name

    topic_arn = result.stdout.strip()
    print(f"✅ SNS topic ARN: {topic_arn}")

    sub_result = subprocess.run(
        [AWS, "sns", "subscribe",
         "--topic-arn", topic_arn,
         "--protocol", "email",
         "--notification-endpoint", email],
        capture_output=True, text=True,
    )

    if sub_result.returncode == 0:
        print(f"✅ Subscription request sent. Confirm via email before alerts can be delivered.")
    else:
        print(f"⚠️  Subscription may already exist or failed: {sub_result.stderr}")

    return topic_arn, topic_name


def deploy_workflow(python_path, sns_topic_arn=None):
    print()
    print("=" * 80)
    print("🚀 DEPLOYING WITH NOVA ACT CLI")
    print("=" * 80)
    print()

    if sys.platform == "win32":
        act_path = os.path.join(".venv", "Scripts", "act")
    else:
        act_path = os.path.join(".venv", "bin", "act")

    result = subprocess.run([act_path, "workflow", "list"], capture_output=True, text=True)

    if "synthetic-monitoring-workflow" not in result.stdout:
        run_command([act_path, "workflow", "create", "--name", "synthetic-monitoring-workflow"])
        print("✅ Workflow configuration created")
    else:
        print("✅ Workflow configuration exists")

    print()
    print("→ Deploying workflow...")
    if sns_topic_arn:
        print(f"   Injecting SNS_TOPIC_ARN={sns_topic_arn}")

    deploy_cmd = [
        act_path, "workflow", "deploy",
        "--name", "synthetic-monitoring-workflow",
        "--source-dir", ".",
        "--entry-point", "workflow.py",
    ]

    # FIX 1 (continued): inject SNS ARN so workflow.py can publish alerts
    if sns_topic_arn:
        deploy_cmd += ["--env", f"SNS_TOPIC_ARN={sns_topic_arn}"]

    run_command(deploy_cmd)

    # FIX 2: extract agent ARN and execution role name from CLI output
    # instead of hardcoding the role name.
    print()
    print("→ Retrieving AgentCore Runtime ARN and execution role...")
    show_result = subprocess.run(
        [act_path, "workflow", "show", "--name", "synthetic-monitoring-workflow"],
        capture_output=True, text=True
    )

    agent_arn = None
    execution_role_name = None

    for line in show_result.stdout.split("\n"):
        if agent_arn is None and ("Agent ARN:" in line or "runtime/" in line):
            for token in line.split():
                if token.startswith("arn:aws:bedrock-agentcore"):
                    agent_arn = token
                    break
        if execution_role_name is None:
            role_match = re.search(r"arn:aws:iam::\d+:role/([\w+=,.@-]+)", line)
            if role_match:
                execution_role_name = role_match.group(1)

    if agent_arn:
        print(f"✅ AgentCore Runtime ARN: {agent_arn}")
    else:
        print("⚠️  Could not extract AgentCore Runtime ARN")

    if execution_role_name:
        print(f"✅ Execution role: {execution_role_name}")
    else:
        print("⚠️  Could not extract execution role name")

    return agent_arn, execution_role_name


def grant_sns_publish(execution_role_name, sns_topic_arn):
    print()
    print(f"→ Granting sns:Publish to role '{execution_role_name}'...")
    sns_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AllowSNSPublishForAlerts",
            "Effect": "Allow",
            "Action": "sns:Publish",
            "Resource": sns_topic_arn
        }]
    }

    result = subprocess.run([
        AWS, "iam", "put-role-policy",
        "--role-name", execution_role_name,
        "--policy-name", "SNSPublish",
        "--policy-document", json.dumps(sns_policy)
    ], capture_output=True, text=True)

    if result.returncode == 0:
        print("✅ SNS publish permission granted")
    else:
        print(f"⚠️  Failed to grant SNS publish: {result.stderr}")
        print(f"   Manually attach sns:Publish on {sns_topic_arn} to role '{execution_role_name}'")


def create_eventbridge_schedule(agent_arn, account_id):
    print()
    print("=" * 80)
    print("📅 CREATING/UPDATING EVENTBRIDGE SCHEDULE")
    print("=" * 80)
    print()

    schedule_name = "synthetic-monitoring-workflow-schedule"
    role_name = "EventBridgeSchedulerSyntheticMonitoringRole"

    check_result = subprocess.run(
        [AWS, "scheduler", "get-schedule", "--name", schedule_name],
        capture_output=True, text=True
    )

    schedule_exists = check_result.returncode == 0

    if schedule_exists:
        subprocess.run(
            [AWS, "scheduler", "delete-schedule", "--name", schedule_name],
            capture_output=True, text=True
        )
        print("✅ Existing schedule deleted for re-creation")

    print(f"→ Checking IAM role '{role_name}'...")
    role_check = subprocess.run(
        [AWS, "iam", "get-role", "--role-name", role_name],
        capture_output=True, text=True
    )

    if role_check.returncode != 0:
        # FIX 3: add aws:SourceAccount condition to prevent confused deputy attacks
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "scheduler.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id}
                }
            }]
        }

        subprocess.run([
            AWS, "iam", "create-role",
            "--role-name", role_name,
            "--assume-role-policy-document", json.dumps(trust_policy)
        ], capture_output=True, text=True)
        print("✅ IAM role created")
        print("→ Waiting 10 seconds for IAM role propagation...")
        time.sleep(10)
    else:
        print("✅ IAM role exists")

    # FIX 4: scheduler role needs only InvokeAgentRuntime on the specific agent ARN.
    # StartBrowserSession/StopRuntimeSession/UpdateBrowserStream are called by the
    # workflow execution role at runtime — not by the scheduler.
    # FIX 5: Resource scoped to agent_arn instead of "*".
    print(f"→ Updating IAM policy (least-privilege, scoped to {agent_arn})...")
    policy_document = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "InvokeAgentRuntimeOnly",
            "Effect": "Allow",
            "Action": "bedrock-agentcore:InvokeAgentRuntime",
            "Resource": agent_arn
        }]
    }

    subprocess.run([
        AWS, "iam", "put-role-policy",
        "--role-name", role_name,
        "--policy-name", "InvokeAgentCore",
        "--policy-document", json.dumps(policy_document)
    ], capture_output=True, text=True)
    print("✅ IAM policy updated")

    payload = {"journey_type": "ecommerce", "target_url": "https://www.saucedemo.com/"}
    schedule_input = {"AgentRuntimeArn": agent_arn, "Payload": json.dumps(payload)}
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

    # FIX 6: reduce retry from 185→1 and age from 86400→300.
    # The next 10-minute interval handles persistent failures — 185 retries
    # risks duplicate concurrent agent runs.
    create_result = subprocess.run([
        AWS, "scheduler", "create-schedule",
        "--name", schedule_name,
        "--schedule-expression", "rate(10 minutes)",
        "--flexible-time-window", "Mode=OFF",
        "--description", "Triggers synthetic monitoring e-commerce tests every 10 minutes",
        "--target", json.dumps({
            "Arn": "arn:aws:scheduler:::aws-sdk:bedrockagentcore:invokeAgentRuntime",
            "RoleArn": role_arn,
            "Input": json.dumps(schedule_input),
            "RetryPolicy": {
                "MaximumEventAgeInSeconds": 300,
                "MaximumRetryAttempts": 1
            }
        })
    ], capture_output=True, text=True)

    if create_result.returncode == 0:
        print(f"✅ Schedule {'updated' if schedule_exists else 'created'}: {schedule_name}")
        return True
    else:
        print(f"⚠️  Failed to create schedule: {create_result.stderr}")
        return False


def cleanup_resources(account_id, region):
    print()
    print("=" * 80)
    print("🧹 CLEANING UP RESOURCES")
    print("=" * 80)
    print()

    response = input("⚠️  This will delete all deployed resources. Continue? (yes/no): ")
    if response.lower() != "yes":
        print("Cleanup cancelled")
        return

    if sys.platform == "win32":
        act_path = os.path.join(".venv", "Scripts", "act")
    else:
        act_path = os.path.join(".venv", "bin", "act")

    print("Deleting EventBridge schedule...")
    result = subprocess.run([
        AWS, "scheduler", "delete-schedule",
        "--name", "synthetic-monitoring-workflow-schedule"
    ], capture_output=True, text=True)
    print("✅ Schedule deleted" if result.returncode == 0 else "⚠️  Schedule not found")

    subprocess.run([
        AWS, "iam", "delete-role-policy",
        "--role-name", "EventBridgeSchedulerSyntheticMonitoringRole",
        "--policy-name", "InvokeAgentCore"
    ], capture_output=True, text=True)

    print("Deleting IAM role...")
    result = subprocess.run([
        AWS, "iam", "delete-role",
        "--role-name", "EventBridgeSchedulerSyntheticMonitoringRole"
    ], capture_output=True, text=True)
    print("✅ IAM role deleted" if result.returncode == 0 else "⚠️  IAM role not found")

    print("Deleting workflow...")
    result = subprocess.run([
        act_path, "workflow", "delete",
        "--name", "synthetic-monitoring-workflow"
    ], capture_output=True, text=True)
    print("✅ Workflow deleted" if result.returncode == 0 else "⚠️  Workflow not found")

    # FIX 7: delete SNS topic — was missing entirely from original cleanup
    topic_arn = f"arn:aws:sns:{region}:{account_id}:synthetic-monitoring-alerts"
    print("Deleting SNS topic...")
    result = subprocess.run([
        AWS, "sns", "delete-topic", "--topic-arn", topic_arn
    ], capture_output=True, text=True)
    print("✅ SNS topic deleted" if result.returncode == 0 else "⚠️  SNS topic not found")

    # Delete SNS inline policy from workflow execution role
    print("Deleting SNSPublish policy from workflow execution role...")
    result = subprocess.run([
        AWS, "iam", "delete-role-policy",
        "--role-name", "nova-act-synthetic-monitoring-workflow-role",
        "--policy-name", "SNSPublish"
    ], capture_output=True, text=True)
    print("✅ SNSPublish policy deleted" if result.returncode == 0 else "⚠️  SNSPublish policy not found")

    # Delete workflow execution role (created by act CLI during deploy)
    print("Deleting workflow execution role...")
    result = subprocess.run([
        AWS, "iam", "delete-role",
        "--role-name", "nova-act-synthetic-monitoring-workflow-role"
    ], capture_output=True, text=True)
    print("✅ Workflow execution role deleted" if result.returncode == 0 else "⚠️  Workflow execution role not found")

    # Delete ECR repository and images
    print("Deleting ECR repository...")
    result = subprocess.run([
        AWS, "ecr", "delete-repository",
        "--repository-name", "nova-act-cli-default", "--force"
    ], capture_output=True, text=True)
    print("✅ ECR repository deleted" if result.returncode == 0 else "⚠️  ECR repository not found")

    print()
    print("=" * 80)
    print("✅ CLEANUP COMPLETE!")
    print("=" * 80)
    print()


def print_next_steps():
    print()
    print("=" * 80)
    print("✅ DEPLOYMENT COMPLETE!")
    print("=" * 80)
    print()
    print("📝 What was deployed:")
    print("  ✅ AgentCore Runtime with workflow")
    print("  ✅ EventBridge Schedule (runs every 10 minutes)")
    print("  ✅ IAM roles and permissions")
    print()
    print("📝 Next Steps:")
    print()
    print("1. Test the workflow manually:")
    print("   python test_all_journeys.py")
    print()
    print("2. View scheduled runs in AWS Console:")
    print("   https://console.aws.amazon.com/scheduler/home")
    print()
    print("3. View workflow execution history:")
    print("   https://console.aws.amazon.com/nova-act/workflows")
    print()
    print("4. Monitor AgentCore Runtime:")
    print("   https://console.aws.amazon.com/bedrock-agentcore/runtimes")
    print()
    print("To remove all resources, run:")
    print("   python deploy.py --cleanup")
    print()


def main():
    parser = argparse.ArgumentParser(description="Deploy or cleanup synthetic monitoring")
    parser.add_argument("--cleanup", action="store_true", help="Remove all deployed resources")
    parser.add_argument("--email", type=str, help="Email address for SNS failure notifications")
    args = parser.parse_args()

    try:
        account_id, region = check_prerequisites()

        if args.cleanup:
            # FIX 7 (continued): pass account_id and region so cleanup can
            # construct the SNS topic ARN
            cleanup_resources(account_id, region)
            return

        python_path, pip_path = install_dependencies()

        # FIX 1: SNS topic created BEFORE workflow deploy so ARN is available to inject
        sns_topic_arn = None
        if args.email:
            sns_topic_arn, _ = create_sns_topic(args.email)

        agent_arn, execution_role_name = deploy_workflow(python_path, sns_topic_arn)

        if sns_topic_arn and execution_role_name:
            grant_sns_publish(execution_role_name, sns_topic_arn)
        elif sns_topic_arn and not execution_role_name:
            print(f"\n⚠️  Could not determine execution role name.")
            print(f"   Manually attach sns:Publish on {sns_topic_arn} to the workflow execution role.")

        if agent_arn:
            create_eventbridge_schedule(agent_arn, account_id)

        print_next_steps()

    except KeyboardInterrupt:
        print("\n\n❌ Operation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Operation failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
