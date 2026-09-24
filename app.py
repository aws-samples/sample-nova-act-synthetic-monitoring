#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import aws_cdk as cdk
from aws_cdk import Aspects
from cdk_nag import AwsSolutionsChecks
from cdk_stack import SyntheticMonitorStack

app = cdk.App()
SyntheticMonitorStack(app, "SyntheticMonitorStack")

# CDK NAG — AwsSolutions rule pack runs at cdk synth / cdk deploy
Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
