#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Combined test script that runs both ecommerce and login_failure journeys
against AgentCore Runtime sequentially.
"""

import boto3
import json
import os
import sys
import uuid
from datetime import datetime
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from urllib.parse import quote

REGION = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION", "us-east-1")
AGENT_ARN = os.environ.get("AGENT_RUNTIME_ARN")

if not AGENT_ARN:
    print("❌ AGENT_RUNTIME_ARN environment variable is not set.")
    print()
    print("   Set it after deploying with:")
    print("   export AGENT_RUNTIME_ARN=$(act workflow show --name synthetic-monitoring-workflow | grep 'Agent ARN' | awk '{print $NF}')")
    print()
    print("   Or copy the ARN from the deploy output and run:")
    print("   export AGENT_RUNTIME_ARN='arn:aws:bedrock-agentcore:us-east-1:YOUR_ACCOUNT:runtime/YOUR_WORKFLOW'")
    sys.exit(1)

JOURNEYS = [
    {"journey_type": "ecommerce", "target_url": "https://www.saucedemo.com/"},
    {"journey_type": "login_failure", "target_url": "https://www.saucedemo.com/"},
]


def invoke_journey(payload):
    """Invoke a single journey and return (success, status_code, body)."""
    journey = payload["journey_type"]
    session_id = f"test-{journey}-{uuid.uuid4()}"

    print(f"→ Journey: {journey}")
    print(f"→ Payload: {json.dumps(payload)}")
    print(f"→ Session ID: {session_id}")
    print()

    session = boto3.Session()
    credentials = session.get_credentials()
    if not credentials:
        print("❌ No AWS credentials found")
        return False, None, None

    frozen = credentials.get_frozen_credentials()
    endpoint = f"https://bedrock-agentcore.{REGION}.amazonaws.com"
    url = f"{endpoint}/runtimes/{quote(AGENT_ARN, safe='')}/invocations"
    body = json.dumps(payload)

    headers = {
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }

    req = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(frozen, "bedrock-agentcore", REGION).add_auth(req)

    print("→ Invoking AgentCore Runtime...")
    print(f"→ Started at: {datetime.now().strftime('%H:%M:%S')}")
    print("→ Typically takes 2-4 minutes. Timeout is 10 minutes.")
    print("→ Waiting for response...")
    print()

    try:
        resp = requests.post(url, headers=dict(req.headers), data=body, timeout=600)
        print(f"→ Status Code: {resp.status_code}")

        if resp.status_code == 200:
            print("✅ Invocation successful!")
            try:
                print(f"→ Response: {json.dumps(resp.json(), indent=2)}")
            except Exception:
                # Note: AgentCore Runtime returns a streaming response — resp.json() will
                # fail. Raw stream text printed below. Fix: replace with boto3 invoke_agent_runtime().
                print(f"→ Response (raw stream): {resp.text}")
            return True, resp.status_code, resp.text
        else:
            print(f"❌ Invocation failed: {resp.status_code}")
            print(f"   {resp.text}")
            return False, resp.status_code, resp.text

    except requests.exceptions.Timeout:
        print("❌ Timed out after 10 minutes")
        return False, None, None
    except Exception as e:
        print(f"❌ Error: {e}")
        return False, None, None


def main():
    print("=" * 80)
    print("🧪 RUNNING ALL JOURNEY TESTS")
    print("=" * 80)
    print(f"→ Agent ARN: {AGENT_ARN}")
    print(f"→ Journeys: {[j['journey_type'] for j in JOURNEYS]}")
    print()

    results = {}
    for payload in JOURNEYS:
        journey = payload["journey_type"]
        print("-" * 80)
        print(f"🚀 [{journey.upper()}]")
        print("-" * 80)
        success, status, body = invoke_journey(payload)
        results[journey] = success
        print()

    # Summary
    print("=" * 80)
    print("📊 RESULTS SUMMARY")
    print("=" * 80)
    all_passed = True
    for journey, passed in results.items():
        icon = "✅" if passed else "❌"
        print(f"  {icon} {journey}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("✅ ALL JOURNEYS PASSED")
    else:
        print("❌ SOME JOURNEYS FAILED")
    print("=" * 80)

    return all_passed


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
