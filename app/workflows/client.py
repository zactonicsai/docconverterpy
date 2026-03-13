"""
Temporal client helper.

Two call paths:
  - start_conversion_workflow()      – async, used by FastAPI endpoints
  - start_conversion_workflow_sync() – sync, used by bus listener threads
                                       (runs async code in a dedicated thread
                                        to avoid nesting event loops)
"""

import asyncio
import concurrent.futures
import logging
import uuid
from typing import Optional

from temporalio.client import Client

from config.settings import settings
from app.models import ConversionJob, ConversionResult
from app.workflows.dataclasses import (
    ConversionWorkflowInput,
    ConversionWorkflowOutput,
)

logger = logging.getLogger(__name__)

# Module-level cached client (one per event loop)
_client: Optional[Client] = None
# Thread pool for sync callers
_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)


async def _get_client() -> Client:
    """Get or create a Temporal client (cached per event loop)."""
    global _client
    if _client is None:
        _client = await Client.connect(
            settings.temporal_host,
            namespace=settings.temporal_namespace,
        )
    return _client


def _job_to_workflow_input(
    job: ConversionJob,
    local_file_path: Optional[str] = None,
) -> ConversionWorkflowInput:
    """Convert a ConversionJob model to a Temporal workflow input."""
    return ConversionWorkflowInput(
        job_id=job.job_id or uuid.uuid4().hex[:12],
        document_type=job.document_type.value,
        location_type=job.location_type.value,
        s3_bucket=job.s3_bucket,
        s3_key=job.s3_key,
        s3_endpoint_url=job.s3_endpoint_url,
        url=job.url,
        ftp_host=job.ftp_host,
        ftp_port=job.ftp_port,
        ftp_path=job.ftp_path,
        ftp_user=job.ftp_user,
        ftp_pass=job.ftp_pass,
        auth_type=job.auth_type.value if job.auth_type else "none",
        auth_username=job.auth_username,
        auth_password=job.auth_password,
        auth_token=job.auth_token,
        output_s3_bucket=job.output_s3_bucket,
        output_s3_key=job.output_s3_key,
        local_file_path=local_file_path,
    )


def _workflow_output_to_result(out: ConversionWorkflowOutput) -> ConversionResult:
    """Convert a Temporal workflow output to a ConversionResult."""
    return ConversionResult(
        job_id=out.job_id,
        success=out.success,
        output_bucket=out.output_bucket,
        output_key=out.output_key,
        error=out.error,
        pages_processed=out.pages_processed,
        characters_extracted=out.total_chars,
    )


# ═════════════════════════════════════════════════════════════════════════════
# ASYNC version – called from FastAPI endpoints (already in an event loop)
# ═════════════════════════════════════════════════════════════════════════════

async def start_conversion_workflow(
    job: ConversionJob,
    local_file_path: Optional[str] = None,
    wait_for_result: bool = True,
) -> ConversionResult:
    """
    Start a Temporal DocumentConversionWorkflow (async).

    Use this from FastAPI endpoints and any async context.
    """
    inp = _job_to_workflow_input(job, local_file_path)
    client = await _get_client()

    workflow_id = f"docconv-{inp.job_id}"

    logger.info(
        "Starting Temporal workflow  id=%s  type=%s  loc=%s",
        workflow_id, inp.document_type, inp.location_type,
    )

    handle = await client.start_workflow(
        "DocumentConversionWorkflow",
        inp,
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
    )

    if wait_for_result:
        output: ConversionWorkflowOutput = await handle.result()
        return _workflow_output_to_result(output)
    else:
        return ConversionResult(
            job_id=inp.job_id,
            success=True,  # pending
            error=None,
        )


# ═════════════════════════════════════════════════════════════════════════════
# SYNC version – called from bus listener threads (no event loop running)
# ═════════════════════════════════════════════════════════════════════════════

def _run_in_new_loop(coro):
    """Run an async coroutine in a brand-new event loop on the current thread."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def start_conversion_workflow_sync(
    job: ConversionJob,
    local_file_path: Optional[str] = None,
    wait_for_result: bool = True,
) -> ConversionResult:
    """
    Start a Temporal workflow (sync).

    Used by bus listener threads.  Detects whether an event loop is
    already running (e.g. uvloop from FastAPI) and avoids nesting:
      - If no loop running → creates a new loop (bus threads)
      - If loop already running → runs in a thread pool (safety fallback)
    """
    # Check if we're inside an existing event loop (FastAPI/uvloop)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    coro = start_conversion_workflow(job, local_file_path, wait_for_result)

    if loop is None:
        # No event loop running – safe to create one (bus listener threads)
        return _run_in_new_loop(coro)
    else:
        # Event loop already running (FastAPI) – offload to a thread
        # This should NOT normally happen because FastAPI should call
        # the async version directly, but this is a safety net.
        logger.debug("Event loop already running – offloading to thread pool")
        future = _thread_pool.submit(_run_in_new_loop, coro)
        return future.result(timeout=settings.temporal_workflow_timeout)
