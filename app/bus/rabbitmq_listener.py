"""
RabbitMQ message bus listener.

Connects via pika, declares the queue, and consumes messages.
Each message is deserialized into a ConversionJob and handed to the processor.
"""

import json
import logging
import time

import pika
from pika.exceptions import AMQPConnectionError

from config.settings import settings
from app.models import ConversionJob
from app.processor import process_job

logger = logging.getLogger(__name__)


def _connect() -> pika.BlockingConnection:
    credentials = pika.PlainCredentials(settings.rabbitmq_user, settings.rabbitmq_pass)
    params = pika.ConnectionParameters(
        host=settings.rabbitmq_host,
        port=settings.rabbitmq_port,
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300,
    )
    return pika.BlockingConnection(params)


def _on_message(ch, method, _properties, body):
    """Callback for each consumed message."""
    try:
        data = json.loads(body)
        job = ConversionJob(**data)
        result = process_job(job)
        logger.info("RabbitMQ job result: %s", result.model_dump_json())
    except Exception as exc:
        logger.exception("Failed to process RabbitMQ message: %s", exc)
    finally:
        ch.basic_ack(delivery_tag=method.delivery_tag)


def run_rabbitmq_listener():
    """
    Blocking consumer loop.  Retries on connection failure.
    Designed to run in its own thread.
    """
    logger.info("RabbitMQ listener starting  queue=%s", settings.rabbitmq_queue)

    while True:
        try:
            connection = _connect()
            channel = connection.channel()
            channel.queue_declare(queue=settings.rabbitmq_queue, durable=True)
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(
                queue=settings.rabbitmq_queue,
                on_message_callback=_on_message,
            )
            logger.info("RabbitMQ listener connected – consuming…")
            channel.start_consuming()
        except AMQPConnectionError as exc:
            logger.warning("RabbitMQ connection failed, retrying in 5s: %s", exc)
            time.sleep(5)
        except Exception as exc:
            logger.exception("RabbitMQ listener error: %s", exc)
            time.sleep(5)
