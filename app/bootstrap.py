"""
Bootstrap AWS resources (SQS queues, S3 buckets) on app startup.

This runs inside the app container and creates any missing resources.
It's idempotent – safe to run on every startup. This eliminates the
dependency on the LocalStack init shell script, which can fail on
macOS Docker Desktop due to lost execute permissions or CRLF issues.
"""

import logging
import time

import boto3
from botocore.exceptions import ClientError

from config.settings import settings

logger = logging.getLogger(__name__)


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_default_region,
    )


def _sqs_client():
    return boto3.client(
        "sqs",
        endpoint_url=settings.sqs_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_default_region,
    )


def _ensure_s3_bucket(client, bucket_name: str):
    """Create an S3 bucket if it doesn't exist."""
    try:
        client.head_bucket(Bucket=bucket_name)
        logger.debug("S3 bucket '%s' already exists", bucket_name)
    except ClientError:
        try:
            client.create_bucket(Bucket=bucket_name)
            logger.info("Created S3 bucket: %s", bucket_name)
        except ClientError as e:
            # Bucket may have been created between head and create (race)
            if "BucketAlreadyOwnedByYou" in str(e) or "BucketAlreadyExists" in str(e):
                logger.debug("S3 bucket '%s' created by another process", bucket_name)
            else:
                raise


def _ensure_sqs_queue(client, queue_name: str):
    """Create an SQS queue if it doesn't exist."""
    try:
        client.get_queue_url(QueueName=queue_name)
        logger.debug("SQS queue '%s' already exists", queue_name)
    except ClientError:
        try:
            client.create_queue(QueueName=queue_name)
            logger.info("Created SQS queue: %s", queue_name)
        except ClientError as e:
            if "QueueAlreadyExists" in str(e):
                logger.debug("SQS queue '%s' created by another process", queue_name)
            else:
                raise


def bootstrap_aws_resources(max_retries: int = 30, retry_delay: float = 3.0):
    """
    Create all required SQS queues and S3 buckets.

    Retries on connection failures (LocalStack may still be starting).
    Idempotent – safe to call on every app startup.
    """
    logger.info("Bootstrapping AWS resources...")

    for attempt in range(1, max_retries + 1):
        try:
            # S3 buckets
            s3 = _s3_client()
            _ensure_s3_bucket(s3, settings.s3_output_bucket)
            _ensure_s3_bucket(s3, "docconv-input")  # input bucket for demos

            # SQS queue
            if settings.enable_sqs:
                sqs = _sqs_client()
                _ensure_sqs_queue(sqs, settings.sqs_queue_name)

            logger.info("AWS resources ready  (S3 buckets + SQS queue)")
            return True

        except Exception as exc:
            if attempt < max_retries:
                logger.debug(
                    "AWS bootstrap attempt %d/%d failed: %s  (retrying in %.0fs)",
                    attempt, max_retries, exc, retry_delay,
                )
                time.sleep(retry_delay)
            else:
                logger.error(
                    "Failed to bootstrap AWS resources after %d attempts: %s",
                    max_retries, exc,
                )
                return False
