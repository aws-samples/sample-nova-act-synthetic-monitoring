# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Deploying with CDK — Production Infrastructure
================================================
EventBridge Scheduler invokes AgentCore Runtime directly via the
universal target (no Lambda bridge needed). This stack adds CloudWatch
alarms, a dead-letter queue, and repeatable multi-region deployment
on top of what deploy.py provides for exploration.

Deploy with:
    cdk deploy --context agentRuntimeArn=arn:aws:bedrock-agentcore:REGION:ACCOUNT:runtime/NAME
    cdk deploy --context agentRuntimeArn=... --context alertEmail=oncall@example.com

Note: This stack uses rate(5 minutes) for critical journey monitoring.
deploy.py uses rate(10 minutes) for exploration. If switching from deploy.py
to CDK, the monitoring cadence doubles — delete the deploy.py schedule first.
"""

import json
from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_iam as iam,
    aws_scheduler as scheduler,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_sqs as sqs,
)
from cdk_nag import NagSuppressions
from constructs import Construct

SCHEDULE_NAME = "synthetic-monitoring-checkout"


class SyntheticMonitorStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # Require the AgentCore Runtime ARN as a CDK context value
        agent_runtime_arn = self.node.try_get_context("agentRuntimeArn")
        if not agent_runtime_arn:
            raise ValueError(
                "CDK context key 'agentRuntimeArn' is required. "
                "Pass with: cdk deploy --context agentRuntimeArn=arn:aws:bedrock-agentcore:..."
            )

        # SNS topic for failure alerts
        alert_topic = sns.Topic(self, "SyntheticMonitorAlerts")

        # Only subscribe if alertEmail is provided — avoids silent broken subscription
        alert_email = self.node.try_get_context("alertEmail")
        if alert_email:
            alert_topic.add_subscription(subs.EmailSubscription(alert_email))
        else:
            CfnOutput(
                self,
                "AlertTopicManualSubscribe",
                value=f"aws sns subscribe --topic-arn {alert_topic.topic_arn} --protocol email --notification-endpoint YOUR_EMAIL",
                description="No alertEmail provided — subscribe manually with this command",
            )

        # Dead-letter queue for failed scheduler invocations
        dlq = sqs.Queue(self, "MonitorDLQ", retention_period=Duration.days(7))

        # IAM role for EventBridge Scheduler to invoke AgentCore Runtime
        # FIX 2: aws:SourceAccount condition to prevent confused deputy attacks
        scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )

        # Least-privilege: only InvokeAgentRuntime on the specific agent ARN
        scheduler_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[agent_runtime_arn],
            )
        )

        # EventBridge schedule: invoke AgentCore directly via universal target
        # FIX 1: explicit name= so CloudWatch alarm dimension matches
        scheduler.CfnSchedule(
            self,
            "CheckoutMonitorSchedule",
            name=SCHEDULE_NAME,
            schedule_expression="rate(5 minutes)",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="OFF"
            ),
            target=scheduler.CfnSchedule.TargetProperty(
                arn="arn:aws:scheduler:::aws-sdk:bedrockagentcore:invokeAgentRuntime",
                role_arn=scheduler_role.role_arn,
                input=json.dumps({
                    "AgentRuntimeArn": agent_runtime_arn,
                    "Payload": json.dumps({"journey_type": "ecommerce", "target_url": "https://www.saucedemo.com/"})
                }),
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_retry_attempts=1,
                    maximum_event_age_in_seconds=300,
                ),
                dead_letter_config=scheduler.CfnSchedule.DeadLetterConfigProperty(
                    arn=dlq.queue_arn,
                ),
            ),
        )

        # Grant Scheduler role permission to send to DLQ
        dlq.grant_send_messages(scheduler_role)

        # CloudWatch alarm: DLQ depth (failed invocations accumulating)
        dlq_alarm = cw.Alarm(
            self,
            "MonitorDLQAlarm",
            metric=dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5)
            ),
            threshold=1,
            evaluation_periods=1,
            alarm_description="Failed monitoring invocations in DLQ — check EventBridge delivery logs",
        )
        dlq_alarm.add_alarm_action(cw_actions.SnsAction(alert_topic))

        # CloudWatch alarm: missing scheduled invocations
        # FIX 1: ScheduleName dimension matches the explicit name= on the schedule
        missing_alarm = cw.Alarm(
            self,
            "MonitorMissingAlarm",
            metric=cw.Metric(
                namespace="AWS/Scheduler",
                metric_name="InvocationAttemptCount",
                dimensions_map={"ScheduleName": SCHEDULE_NAME},
                period=Duration.minutes(10),
                statistic="Sum",
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.BREACHING,
            alarm_description="Synthetic monitor has not run in the expected window",
        )
        missing_alarm.add_alarm_action(cw_actions.SnsAction(alert_topic))

        # Outputs for wiring to AgentCore agent configuration
        CfnOutput(
            self,
            "AlertTopicArn",
            value=alert_topic.topic_arn,
            description="Set SNS_TOPIC_ARN in the AgentCore Runtime agent config to this value",
        )
        CfnOutput(
            self,
            "DLQUrl",
            value=dlq.queue_url,
            description="Dead-letter queue for failed scheduler invocations",
        )
        CfnOutput(
            self,
            "SchedulerRoleArn",
            value=scheduler_role.role_arn,
            description="EventBridge Scheduler execution role ARN",
        )

        # CDK NAG suppressions — documented justifications for accepted deviations
        NagSuppressions.add_resource_suppressions(alert_topic, [
            {
                "id": "AwsSolutions-SNS2",
                "reason": "Alert topic carries monitoring notifications only — no PII or sensitive data requiring encryption at rest.",
            },
            {
                "id": "AwsSolutions-SNS3",
                "reason": "Email subscription is intentional; recipients confirm via email before alerts are delivered.",
            },
        ])
        NagSuppressions.add_resource_suppressions(dlq, [
            {
                "id": "AwsSolutions-SQS3",
                "reason": "This queue IS the dead-letter queue (terminal sink for failed scheduler invocations) — a DLQ does not itself require a further DLQ.",
            },
            {
                "id": "AwsSolutions-SQS4",
                "reason": "DLQ holds EventBridge invocation metadata only — no sensitive data requiring SSE.",
            },
        ])
