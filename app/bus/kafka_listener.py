"""
Kafka message bus listener.

Uses confluent-kafka consumer to poll the configured topic.
Pre-creates the topic via AdminClient if it doesn't exist, since
KRaft mode auto-creation only triggers on produce, not subscribe.
"""

import json
import logging
import time

from confluent_kafka import Consumer, KafkaError, KafkaException
from confluent_kafka.admin import AdminClient, NewTopic

from config.settings import settings
from app.models import ConversionJob
from app.processor import process_job

logger = logging.getLogger(__name__)


def _ensure_topic_exists():
    """
    Create the Kafka topic if it doesn't already exist.
    KRaft-mode auto.create.topics only fires on produce, not on subscribe,
    so we need to create it explicitly for consumers.
    """
    admin = AdminClient({"bootstrap.servers": settings.kafka_bootstrap_servers})

    # Check if topic exists
    metadata = admin.list_topics(timeout=10)
    if settings.kafka_topic in metadata.topics:
        logger.info("Kafka topic '%s' already exists", settings.kafka_topic)
        return

    logger.info("Creating Kafka topic '%s'", settings.kafka_topic)
    new_topic = NewTopic(
        settings.kafka_topic,
        num_partitions=1,
        replication_factor=1,
    )
    futures = admin.create_topics([new_topic])
    for topic, future in futures.items():
        try:
            future.result()  # block until created
            logger.info("Kafka topic '%s' created", topic)
        except Exception as exc:
            # Topic may already exist if created between check and create
            if "TOPIC_ALREADY_EXISTS" in str(exc):
                logger.info("Kafka topic '%s' already exists (race)", topic)
            else:
                logger.warning("Failed to create topic '%s': %s", topic, exc)


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

    # Wait for Kafka to be ready, then ensure topic exists
    for attempt in range(30):
        try:
            _ensure_topic_exists()
            break
        except Exception as exc:
            logger.debug("Waiting for Kafka (attempt %d): %s", attempt + 1, exc)
            time.sleep(3)

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
                err_code = msg.error().code()

                # Partition EOF is normal – just means we're caught up
                if err_code == KafkaError._PARTITION_EOF:
                    continue

                # Topic not yet ready – wait and retry (don't crash)
                if err_code == KafkaError.UNKNOWN_TOPIC_OR_PART:
                    logger.debug("Topic not ready yet, waiting...")
                    time.sleep(3)
                    continue

                # Other errors – log and reconnect
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
