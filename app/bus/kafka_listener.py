"""
Kafka message bus listener.

Uses confluent-kafka consumer to poll the configured topic.
"""

import json
import logging
import time

from confluent_kafka import Consumer, KafkaError, KafkaException

from config.settings import settings
from app.models import ConversionJob
from app.processor import process_job

logger = logging.getLogger(__name__)


def _create_consumer() -> Consumer:
    conf = {
        "bootstrap.servers": settings.kafka_bootstrap_servers,
        "group.id": settings.kafka_group_id,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    }
    return Consumer(conf)


def run_kafka_listener():
    """
    Blocking consumer loop for Kafka.
    Designed to run in its own thread.
    """
    logger.info(
        "Kafka listener starting  topic=%s  servers=%s",
        settings.kafka_topic,
        settings.kafka_bootstrap_servers,
    )

    consumer = None
    while True:
        try:
            if consumer is None:
                consumer = _create_consumer()
                consumer.subscribe([settings.kafka_topic])
                logger.info("Kafka listener subscribed to %s", settings.kafka_topic)

            msg = consumer.poll(timeout=5.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                logger.error("Kafka error: %s", msg.error())
                raise KafkaException(msg.error())

            try:
                data = json.loads(msg.value().decode("utf-8"))
                job = ConversionJob(**data)
                result = process_job(job)
                logger.info("Kafka job result: %s", result.model_dump_json())
            except Exception as exc:
                logger.exception("Failed to process Kafka message: %s", exc)
            finally:
                consumer.commit(asynchronous=False)

        except (KafkaException, Exception) as exc:
            logger.warning("Kafka listener error, reconnecting in 5s: %s", exc)
            if consumer:
                try:
                    consumer.close()
                except Exception:
                    pass
                consumer = None
            time.sleep(5)
