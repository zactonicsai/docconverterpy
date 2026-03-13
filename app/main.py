"""
main.py – Entry point for the Document Conversion Service.

Reads ENABLE_* environment variables and starts only the requested
components:
  • REST API      (FastAPI + Uvicorn)   – ENABLE_API
  • SQS listener  (long-poll thread)    – ENABLE_SQS
  • RabbitMQ listener (pika thread)     – ENABLE_RABBITMQ
  • Kafka listener (confluent thread)   – ENABLE_KAFKA

Each message bus listener runs in a daemon thread so the process shuts
down cleanly when the main thread (API or event loop) exits.
"""

import logging
import os
import sys
import threading
import time

# ── Bootstrap logging ────────────────────────────────────────────────────────

from config.settings import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("docconv.main")

# ── Ensure tmp dir exists ────────────────────────────────────────────────────

os.makedirs(settings.tmp_dir, exist_ok=True)


# ── Banner ───────────────────────────────────────────────────────────────────

def _banner():
    flags = {
        "API":      settings.enable_api,
        "SQS":      settings.enable_sqs,
        "RabbitMQ": settings.enable_rabbitmq,
        "Kafka":    settings.enable_kafka,
        "Temporal": settings.enable_temporal,
        "→ Workflows": settings.use_temporal_workflows,
    }
    logger.info("=" * 60)
    logger.info("  Document Conversion Service")
    logger.info("=" * 60)
    for name, enabled in flags.items():
        status = "ENABLED" if enabled else "disabled"
        logger.info("  %-12s %s", name, status)
    logger.info("  tmp_dir      %s", settings.tmp_dir)
    logger.info("  output       s3://%s", settings.s3_output_bucket)
    logger.info("=" * 60)


# ── Thread launchers ─────────────────────────────────────────────────────────

def _start_thread(name: str, target):
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    logger.info("Started thread: %s", name)
    return t


def main():
    _banner()
    threads: list[threading.Thread] = []

    # ── Bootstrap AWS resources (SQS queues, S3 buckets) ─────────────────
    # This creates missing resources on every startup, making the service
    # independent of the LocalStack init script (which can fail on macOS).
    from app.bootstrap import bootstrap_aws_resources
    bootstrap_aws_resources()

    # ── SQS ──────────────────────────────────────────────────────────────
    if settings.enable_sqs:
        from app.bus.sqs_listener import run_sqs_listener
        threads.append(_start_thread("sqs-listener", run_sqs_listener))

    # ── RabbitMQ ─────────────────────────────────────────────────────────
    if settings.enable_rabbitmq:
        from app.bus.rabbitmq_listener import run_rabbitmq_listener
        threads.append(_start_thread("rabbitmq-listener", run_rabbitmq_listener))

    # ── Kafka ────────────────────────────────────────────────────────────
    if settings.enable_kafka:
        from app.bus.kafka_listener import run_kafka_listener
        threads.append(_start_thread("kafka-listener", run_kafka_listener))

    # ── Temporal Worker ──────────────────────────────────────────────────
    if settings.enable_temporal:
        from app.workflows.worker import run_temporal_worker
        threads.append(_start_thread("temporal-worker", run_temporal_worker))

    # ── REST API (runs on the main thread) ───────────────────────────────
    if settings.enable_api:
        import uvicorn
        logger.info("Starting API server on 0.0.0.0:%d", settings.api_port)
        uvicorn.run(
            "app.api:app",
            host="0.0.0.0",
            port=settings.api_port,
            log_level=settings.log_level.lower(),
        )
    else:
        # No API – keep the process alive for bus listeners
        logger.info("API disabled – running bus listeners only")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Shutting down…")


if __name__ == "__main__":
    main()
