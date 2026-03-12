"""
SQS message bus listener.

Long-polls the configured SQS queue, deserializes ConversionJob messages,
and hands them to the processor pipeline.
"""

import json
import logging
import time

import boto3
from botocore.exceptions import ClientError

from config.settings import settings
from app.models import ConversionJob
from app.processor import process_job

logger = logging.getLogger(__name__)


def _get_sqs_client():
    return boto3.client(
        "sqs",
        endpoint_url=settings.sqs_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_default_region,
    )


def _get_queue_url(client) -> str:
    resp = client.get_queue_url(QueueName=settings.sqs_queue_name)
    return resp["QueueUrl"]


def run_sqs_listener():
    """
    Blocking loop that long-polls SQS and processes messages.
    Designed to be run in its own thread.
    """
    logger.info("SQS listener starting  queue=%s", settings.sqs_queue_name)
    client = _get_sqs_client()

    # Retry getting queue URL (LocalStack may still be initializing)
    queue_url = None
    for attempt in range(30):
        try:
            queue_url = _get_queue_url(client)
            break
        except ClientError:
            logger.debug("Waiting for SQS queue... attempt %d", attempt + 1)
            time.sleep(2)

    if not queue_url:
        logger.error("Could not find SQS queue %s – listener exiting", settings.sqs_queue_name)
        return

    logger.info("SQS listener connected → %s", queue_url)

    while True:
        try:
            response = client.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=settings.sqs_poll_interval,
            )

            messages = response.get("Messages", [])
            for msg in messages:
                receipt = msg["ReceiptHandle"]
                try:
                    body = json.loads(msg["Body"])
                    job = ConversionJob(**body)
                    result = process_job(job)
                    logger.info("SQS job result: %s", result.model_dump_json())
                except Exception as exc:
                    logger.exception("Failed to process SQS message: %s", exc)
                finally:
                    # Always delete the message to avoid reprocessing
                    client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)

        except Exception as exc:
            logger.exception("SQS poll error: %s", exc)
            time.sleep(5)
