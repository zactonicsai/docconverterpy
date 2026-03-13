"""
Temporal worker for the document conversion service.

Registers ALL workflows and activities (core + extended), connects to
the Temporal server, and polls for tasks.
"""

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from config.settings import settings

# ── Core workflows & activities ──────────────────────────────────────────────
from app.workflows.activities import ALL_ACTIVITIES
from app.workflows.activities_ext import ALL_EXTENDED_ACTIVITIES
from app.workflows.document_workflows import ALL_CHILD_WORKFLOWS
from app.workflows.conversion_workflow import (
    DocumentConversionWorkflow,
    BatchConversionWorkflow,
)

# ── Extended workflows ───────────────────────────────────────────────────────
from app.workflows.pipeline_workflow import DocumentPipelineWorkflow
from app.workflows.s3_watch_workflow import S3FolderWatchWorkflow
from app.workflows.webhook_workflow import WebhookNotificationWorkflow
from app.workflows.multi_output_workflow import MultiFormatOutputWorkflow
from app.workflows.retry_escalation_workflow import RetryEscalationWorkflow
from app.workflows.scheduled_workflow import ScheduledMaintenanceWorkflow

logger = logging.getLogger(__name__)

# ── Combined registries ─────────────────────────────────────────────────────

ALL_WORKFLOWS = [
    # Core
    DocumentConversionWorkflow,
    BatchConversionWorkflow,
    *ALL_CHILD_WORKFLOWS,
    # Extended
    DocumentPipelineWorkflow,
    S3FolderWatchWorkflow,
    WebhookNotificationWorkflow,
    MultiFormatOutputWorkflow,
    RetryEscalationWorkflow,
    ScheduledMaintenanceWorkflow,
]

COMBINED_ACTIVITIES = ALL_ACTIVITIES + ALL_EXTENDED_ACTIVITIES


async def _run_worker():
    """Async entry point: connect to Temporal and run the worker."""
    logger.info(
        "Temporal worker connecting  host=%s  namespace=%s  queue=%s",
        settings.temporal_host,
        settings.temporal_namespace,
        settings.temporal_task_queue,
    )

    client = None
    for attempt in range(30):
        try:
            client = await Client.connect(
                settings.temporal_host,
                namespace=settings.temporal_namespace,
            )
            logger.info("Connected to Temporal server")
            break
        except Exception as exc:
            logger.debug("Temporal connection attempt %d failed: %s", attempt + 1, exc)
            await asyncio.sleep(3)

    if client is None:
        logger.error("Could not connect to Temporal – worker exiting")
        return

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=ALL_WORKFLOWS,
        activities=COMBINED_ACTIVITIES,
    )

    logger.info(
        "Temporal worker running  workflows=%d  activities=%d",
        len(ALL_WORKFLOWS),
        len(COMBINED_ACTIVITIES),
    )
    await worker.run()


def run_temporal_worker():
    """Blocking entry point for the Temporal worker thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_worker())
    except Exception as exc:
        logger.exception("Temporal worker crashed: %s", exc)
    finally:
        loop.close()
