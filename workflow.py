#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Synthetic Monitoring Workflow using Nova Act CLI

Deploy with: act workflow deploy --source-dir . --entry-point workflow.py
"""

import logging
import sys
import os
import boto3
from datetime import datetime
from bedrock_agentcore.tools.browser_client import browser_session
from nova_act import NovaAct, Workflow
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

class JourneyResult(BaseModel):
    status: str
    steps_completed: list[str]
    steps_failed: list[str] = []
    duration_seconds: float = 0.0


def _get_region() -> str:
    return boto3.Session().region_name or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def send_sns_notification(subject: str, message: str) -> None:
    """Publish a failure alert to SNS. No hardcoded fallback ARN."""
    topic_arn = os.environ.get("SNS_TOPIC_ARN")
    if not topic_arn:
        logger.warning("SNS_TOPIC_ARN not set — skipping notification. Deploy with --email to enable alerts.")
        return
    try:
        region = _get_region()
        sns = boto3.client("sns", region_name=region)
        sns.publish(TopicArn=topic_arn, Subject=subject[:100], Message=message)
        logger.info(f"📧 SNS notification sent: {subject}")
    except Exception as e:
        logger.error(f"Failed to send SNS notification: {e}")


def _assert(nova: NovaAct, question: str, step_name: str) -> None:
    """Shared assertion helper using act_get() with boolean schema.
    Replaces all nova.act("Verify ...") calls which returned nothing checkable
    and allowed journeys to report success even when verification failed silently.
    """
    check = nova.act_get(question, schema={"type": "boolean"})
    if check.parsed_response is not True:
        raise AssertionError(f"Assertion failed at '{step_name}': {question}")
    logger.info(f"✅ Assertion passed: {step_name}")


# ── Journeys ─────────────────────────────────────────────────────────────────

def test_login_flow(target_url: str, workflow: Workflow) -> JourneyResult:
    """Basic login validation with valid credentials."""
    logger.info("🚀 STARTING LOGIN FLOW — %s", target_url)
    start_time = datetime.utcnow()

    try:
        region = _get_region()
        with browser_session(region) as client:
            ws_url, headers = client.generate_ws_headers()
            with NovaAct(
                starting_page=target_url,
                workflow=workflow,
                headless=True,
                tty=False,
                record_video=False,
                clone_user_data_dir=False,
                cdp_endpoint_url=ws_url,
                cdp_headers=headers,
            ) as nova:
                nova.act("Enter 'standard_user' in the username field")
                nova.act("Enter 'secret_sauce' in the password field")
                nova.act("Click the login button")
                _assert(nova, "Are products visible on the page?", "products_visible")

        duration = (datetime.utcnow() - start_time).total_seconds()
        logger.info("✅ LOGIN FLOW PASSED in %.2fs", duration)
        return JourneyResult(
            status="success",
            steps_completed=["login", "products_verification"],
            duration_seconds=duration,
        )

    except Exception as e:
        duration = (datetime.utcnow() - start_time).total_seconds()
        logger.error("❌ LOGIN FLOW FAILED: %s", e, exc_info=True)
        return JourneyResult(
            status="failed",
            steps_completed=[],
            steps_failed=["login"],
            duration_seconds=duration,
        )


def test_login_failure_flow(target_url: str, workflow: Workflow) -> JourneyResult:
    """
    Monitors that the login rejection mechanism is working correctly.

    Correct behavior:
    - Wrong credentials → error shown   → status="success" (rejection working as designed)
    - Wrong credentials → no error shown → status="failed" + SNS (auth bypass — real incident)
    """
    logger.info("🚀 STARTING LOGIN REJECTION MONITOR — %s", target_url)
    start_time = datetime.utcnow()

    try:
        region = _get_region()
        with browser_session(region) as client:
            ws_url, headers = client.generate_ws_headers()
            with NovaAct(
                starting_page=target_url,
                workflow=workflow,
                headless=True,
                tty=False,
                record_video=False,
                clone_user_data_dir=False,
                cdp_endpoint_url=ws_url,
                cdp_headers=headers,
            ) as nova:
                nova.act("Type 'invalid_user' into the username input box")
                nova.act("Type 'wrong_pass' into the password input box")
                nova.act("Click the login button")

                error_shown = nova.act_get(
                    "Is a login error or 'Username and password do not match' message visible?",
                    schema={"type": "boolean"},
                )

        duration = (datetime.utcnow() - start_time).total_seconds()

        rejection_works = error_shown.parsed_response is True

        if rejection_works:
            logger.info("✅ Login rejection working correctly")
            return JourneyResult(
                status="success",
                steps_completed=["login_attempt", "error_displayed"],
                duration_seconds=duration,
            )
        else:
            logger.error("❌ Wrong credentials did not produce an error — possible auth bypass")
            return JourneyResult(
                status="failed",
                steps_completed=["login_attempt"],
                steps_failed=["rejection_verification"],
                duration_seconds=duration,
            )

    except Exception as e:
        duration = (datetime.utcnow() - start_time).total_seconds()
        logger.error("❌ LOGIN REJECTION MONITOR ERRORED: %s", e, exc_info=True)
        return JourneyResult(
            status="failed",
            steps_completed=[],
            steps_failed=["login_attempt"],
            duration_seconds=duration,
        )


def test_ecommerce_workflow(target_url: str, workflow: Workflow) -> JourneyResult:
    """Full e-commerce flow: login → add to cart → checkout → order confirmation → logout."""
    logger.info("🚀 STARTING E-COMMERCE WORKFLOW — %s", target_url)
    start_time = datetime.utcnow()
    result = JourneyResult(status="success", steps_completed=[], steps_failed=[])

    try:
        region = _get_region()
        with browser_session(region) as client:
            ws_url, headers = client.generate_ws_headers()
            with NovaAct(
                starting_page=target_url,
                workflow=workflow,
                headless=True,
                tty=False,
                record_video=False,
                clone_user_data_dir=False,
                cdp_endpoint_url=ws_url,
                cdp_headers=headers,
            ) as nova:
                # Step 1: Login
                nova.act("Enter 'standard_user' in the username field")
                nova.act("Enter 'secret_sauce' in the password field")
                nova.act("Click the login button")
                _assert(nova, "Are products visible on the page?", "products_visible")
                result.steps_completed.append("login")

                # Step 2: Add products to cart
                nova.act("Select Sauce Labs Backpack")
                nova.act("Add Sauce Labs Backpack to the cart")
                nova.act("Navigate back to products page")
                nova.act("Select Sauce Labs Onesie")
                nova.act("Add Sauce Labs Onesie to the cart")
                nova.act("Navigate back to products page")
                result.steps_completed.append("shopping")

                # Step 3: Verify cart
                nova.act("Click cart and navigate to the cart page")
                _assert(nova, "Are exactly 2 items shown in the cart?", "cart_item_count")
                result.steps_completed.append("cart_verification")

                # Step 4: Checkout info
                nova.act("Click the Checkout button")
                nova.act("Enter 'John' in the First Name field")
                nova.act("Enter 'Doe' in the Last Name field")
                nova.act("Enter '12345' in the Zip/Postal Code field")
                nova.act("Click the Continue button")
                result.steps_completed.append("checkout_info")

                # Step 5: Order completion
                _assert(nova, "Is the Checkout Overview page displayed?", "checkout_overview")
                nova.act("Click the Finish button")
                _assert(
                    nova,
                    "Is 'THANK YOU FOR YOUR ORDER' visible on the page?",
                    "order_confirmation",
                )
                result.steps_completed.append("order_completion")

                # Step 6: Logout
                nova.act("Click the Back Home button")
                nova.act("Click the hamburger menu on the left")
                nova.act("Click the Logout link")
                _assert(nova, "Is the login page displayed?", "logged_out")
                result.steps_completed.append("logout")

        result.duration_seconds = (datetime.utcnow() - start_time).total_seconds()
        logger.info("✅ E-COMMERCE WORKFLOW PASSED in %.2fs", result.duration_seconds)
        return result

    except Exception as e:
        result.duration_seconds = (datetime.utcnow() - start_time).total_seconds()
        result.status = "failed"
        logger.error("❌ E-COMMERCE WORKFLOW FAILED: %s", e, exc_info=True)
        logger.error("   Steps completed: %s", result.steps_completed)
        return result


# ── Entry point ───────────────────────────────────────────────────────────────

def main(payload):
    logger.info("🚀 MAIN INVOKED — payload: %s", payload)

    # FIX 3: initialize before try block — the outer except references both,
    # causing NameError if an exception fires before assignment inside the try.
    journey_type = "unknown"
    target_url = "https://www.saucedemo.com/"

    os.environ.pop("NOVA_ACT_API_KEY", None)

    try:
        if isinstance(payload, dict):
            journey_type = payload.get("journey_type", "login")
            target_url = payload.get("target_url", target_url)

        logger.info("🎯 Journey: %s  Target: %s", journey_type, target_url)

        with Workflow(
            workflow_definition_name="synthetic-monitoring-workflow",
            model_id="nova-act-latest",
        ) as workflow:
            if journey_type == "ecommerce":
                result = test_ecommerce_workflow(target_url, workflow)
            elif journey_type == "login_failure":
                result = test_login_failure_flow(target_url, workflow)
            else:
                result = test_login_flow(target_url, workflow)

        if result.status == "failed":
            logger.error("❌ JOURNEY FAILED — sending SNS notification")
            send_sns_notification(
                subject=f"Synthetic Monitoring ALERT: {journey_type} failed",
                message=(
                    f"Journey: {journey_type}\n"
                    f"Target: {target_url}\n"
                    f"Duration: {result.duration_seconds:.2f}s\n"
                    f"Steps completed: {result.steps_completed}\n"
                    f"Steps failed: {result.steps_failed}"
                ),
            )

        return {
            "status": result.status,
            "journey_type": journey_type,
            "result": result.model_dump(),
        }

    except Exception as e:
        logger.error("❌ MAIN FAILED: %s", e, exc_info=True)
        # FIX 3: journey_type and target_url are now safe to reference here
        send_sns_notification(
            subject=f"Synthetic Monitoring ALERT: {journey_type} exception",
            message=f"Journey: {journey_type}\nTarget: {target_url}\nError: {str(e)}",
        )
        return {"status": "failed", "journey_type": journey_type, "error": str(e)}


if __name__ == "__main__":
    result = main({"journey_type": "login", "target_url": "https://www.saucedemo.com/"})
    print(f"\nResult: {result}")
