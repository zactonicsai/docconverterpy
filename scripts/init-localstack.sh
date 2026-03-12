#!/bin/bash
set -euo pipefail

echo "=== Initializing LocalStack resources ==="

# Create the SQS queue for job messages
awslocal sqs create-queue --queue-name docconv-jobs
echo "✓ SQS queue 'docconv-jobs' created"

# Create the S3 output bucket for converted text
awslocal s3 mb s3://docconv-output
echo "✓ S3 bucket 'docconv-output' created"

# Create an S3 input bucket for demo / testing
awslocal s3 mb s3://docconv-input
echo "✓ S3 bucket 'docconv-input' created"

echo "=== LocalStack initialization complete ==="
