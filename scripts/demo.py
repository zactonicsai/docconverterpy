#!/usr/bin/env python3
"""
demo.py – Send test conversion jobs through every supported channel.

Usage:
    # Start the stack first
    docker compose up -d

    # Wait ~30s for services to be healthy, then:
    pip install boto3 pika confluent-kafka requests

    python scripts/demo.py              # run all demos
    python scripts/demo.py --sqs        # SQS only
    python scripts/demo.py --rabbitmq   # RabbitMQ only
    python scripts/demo.py --kafka      # Kafka only
    python scripts/demo.py --api        # REST API only
"""

import argparse
import json
import sys
import time

# ── Sample job payloads ──────────────────────────────────────────────────────

SAMPLE_S3_JOB = {
    "job_id": "demo-s3-001",
    "document_type": "pdf",
    "location_type": "s3",
    "s3_bucket": "docconv-input",
    "s3_key": "samples/test.pdf",
    "s3_endpoint_url": "http://localhost:4566",
}

SAMPLE_URL_JOB = {
    "job_id": "demo-url-001",
    "document_type": "html",
    "location_type": "url",
    "url": "https://example.com",
    "auth_type": "none",
}

SAMPLE_FTP_JOB = {
    "job_id": "demo-ftp-001",
    "document_type": "txt",
    "location_type": "ftp",
    "ftp_host": "localhost",
    "ftp_port": 21,
    "ftp_path": "/test.txt",
    "ftp_user": "docconv",
    "ftp_pass": "docconv",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def upload_sample_to_s3():
    """Create a tiny test PDF in the input bucket via LocalStack."""
    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url="http://localhost:4566",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
    )
    # Create a minimal text file as stand-in (real PDF would need reportlab)
    content = b"This is a test document for the conversion service demo."
    s3.put_object(Bucket="docconv-input", Key="samples/test.txt", Body=content)
    print("[setup] Uploaded test file to s3://docconv-input/samples/test.txt")

    # Update sample job to use txt
    SAMPLE_S3_JOB["document_type"] = "txt"
    SAMPLE_S3_JOB["s3_key"] = "samples/test.txt"


# ── Channel senders ──────────────────────────────────────────────────────────

def send_sqs(job: dict):
    import boto3

    print(f"\n{'='*60}")
    print("Sending job via SQS")
    print(f"{'='*60}")

    sqs = boto3.client(
        "sqs",
        endpoint_url="http://localhost:4566",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
    )
    queue_url = sqs.get_queue_url(QueueName="docconv-jobs")["QueueUrl"]
    resp = sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(job))
    print(f"  MessageId: {resp['MessageId']}")
    print(f"  Payload:   {json.dumps(job, indent=2)}")


def send_rabbitmq(job: dict):
    import pika

    print(f"\n{'='*60}")
    print("Sending job via RabbitMQ")
    print(f"{'='*60}")

    credentials = pika.PlainCredentials("docconv", "docconv")
    connection = pika.BlockingConnection(
        pika.ConnectionParameters("localhost", 5672, credentials=credentials)
    )
    channel = connection.channel()
    channel.queue_declare(queue="docconv-jobs", durable=True)
    channel.basic_publish(
        exchange="",
        routing_key="docconv-jobs",
        body=json.dumps(job),
        properties=pika.BasicProperties(delivery_mode=2),
    )
    connection.close()
    print(f"  Payload: {json.dumps(job, indent=2)}")


def send_kafka(job: dict):
    from confluent_kafka import Producer

    print(f"\n{'='*60}")
    print("Sending job via Kafka")
    print(f"{'='*60}")

    producer = Producer({"bootstrap.servers": "localhost:9092"})
    producer.produce(
        "docconv-jobs",
        value=json.dumps(job).encode("utf-8"),
    )
    producer.flush()
    print(f"  Payload: {json.dumps(job, indent=2)}")


def send_api_job(job: dict):
    import requests

    print(f"\n{'='*60}")
    print("Sending job via REST API (POST /convert/job)")
    print(f"{'='*60}")

    resp = requests.post("http://localhost:8080/convert/job", json=job, timeout=120)
    print(f"  Status:   {resp.status_code}")
    print(f"  Response: {json.dumps(resp.json(), indent=2)}")


def send_api_upload():
    import requests

    print(f"\n{'='*60}")
    print("Uploading file via REST API (POST /convert/upload)")
    print(f"{'='*60}")

    # Create a tiny sample file
    content = b"Hello from the upload API!\nThis is line 2.\nLine 3 here."
    files = {"file": ("sample.txt", content, "text/plain")}
    data = {"document_type": "txt"}

    resp = requests.post(
        "http://localhost:8080/convert/upload", files=files, data=data, timeout=120
    )
    print(f"  Status:   {resp.status_code}")
    print(f"  Response: {json.dumps(resp.json(), indent=2)}")


def check_health():
    import requests

    print(f"\n{'='*60}")
    print("Health check (GET /health)")
    print(f"{'='*60}")
    resp = requests.get("http://localhost:8080/health", timeout=10)
    print(f"  {json.dumps(resp.json(), indent=2)}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="DocConv demo sender")
    parser.add_argument("--sqs", action="store_true")
    parser.add_argument("--rabbitmq", action="store_true")
    parser.add_argument("--kafka", action="store_true")
    parser.add_argument("--api", action="store_true")
    args = parser.parse_args()

    run_all = not (args.sqs or args.rabbitmq or args.kafka or args.api)

    upload_sample_to_s3()
    time.sleep(1)

    if run_all or args.api:
        check_health()
        send_api_upload()
        send_api_job(SAMPLE_URL_JOB)

    if run_all or args.sqs:
        send_sqs(SAMPLE_S3_JOB)

    if run_all or args.rabbitmq:
        send_rabbitmq(SAMPLE_URL_JOB)

    if run_all or args.kafka:
        send_kafka(SAMPLE_URL_JOB)

    print(f"\n{'='*60}")
    print("Demo messages sent!  Watch logs with:  docker compose logs -f docconv-app")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
