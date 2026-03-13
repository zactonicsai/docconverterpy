"""
Processing pipeline: fetch → convert → upload.

Two entry points:
  - process_job_async() – for FastAPI endpoints (uses await)
  - process_job()       – for bus listener threads (sync)

When Temporal is enabled, ALL jobs route through Temporal workflows
for full dashboard visibility, including direct file uploads.
"""

import logging
import os
import uuid

from app.models import ConversionJob, ConversionResult, LocationType
from app.fetchers.dispatch import fetch_document
from app.converters.dispatch import convert_document
from app.storage import upload_text_chunks
from config.settings import settings

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# ASYNC entry point – called by FastAPI endpoints
# ═════════════════════════════════════════════════════════════════════════════

async def process_job_async(
    job: ConversionJob, local_file_path: str | None = None
) -> ConversionResult:
    """
    Async entry point for FastAPI.  Routes through Temporal when enabled.
    """
    if settings.use_temporal_workflows and settings.enable_temporal:
        return await _process_via_temporal_async(job, local_file_path)

    return _process_direct(job, local_file_path)


async def _process_via_temporal_async(
    job: ConversionJob, local_file_path: str | None = None
) -> ConversionResult:
    """Submit to Temporal using await (no event loop conflict)."""
    job_id = job.job_id or uuid.uuid4().hex[:12]
    job.job_id = job_id
    logger.info("Routing job %s through Temporal (async)  type=%s  loc=%s",
                job_id, job.document_type.value, job.location_type.value)

    try:
        from app.workflows.client import start_conversion_workflow
        return await start_conversion_workflow(
            job, local_file_path=local_file_path, wait_for_result=True
        )
    except Exception as exc:
        logger.warning(
            "Temporal routing failed for job %s, falling back to direct: %s",
            job_id, exc,
        )
        return _process_direct(job, local_file_path)


# ═════════════════════════════════════════════════════════════════════════════
# SYNC entry point – called by bus listener threads
# ═════════════════════════════════════════════════════════════════════════════

def process_job(
    job: ConversionJob, local_file_path: str | None = None
) -> ConversionResult:
    """
    Sync entry point for bus listeners (SQS, RabbitMQ, Kafka threads).
    """
    if settings.use_temporal_workflows and settings.enable_temporal:
        return _process_via_temporal_sync(job, local_file_path)

    return _process_direct(job, local_file_path)


def _process_via_temporal_sync(
    job: ConversionJob, local_file_path: str | None = None
) -> ConversionResult:
    """Submit to Temporal from a sync thread context."""
    job_id = job.job_id or uuid.uuid4().hex[:12]
    job.job_id = job_id
    logger.info("Routing job %s through Temporal (sync)  type=%s  loc=%s",
                job_id, job.document_type.value, job.location_type.value)

    try:
        from app.workflows.client import start_conversion_workflow_sync
        return start_conversion_workflow_sync(
            job, local_file_path=local_file_path, wait_for_result=True
        )
    except Exception as exc:
        logger.warning(
            "Temporal routing failed for job %s, falling back to direct: %s",
            job_id, exc,
        )
        return _process_direct(job, local_file_path)


# ═════════════════════════════════════════════════════════════════════════════
# DIRECT pipeline – fallback when Temporal is off or unreachable
# ═════════════════════════════════════════════════════════════════════════════

def _process_direct(
    job: ConversionJob, local_file_path: str | None = None
) -> ConversionResult:
    """Direct (inline) processing – no Temporal."""
    job_id = job.job_id or uuid.uuid4().hex[:12]
    logger.info("Processing job %s directly  type=%s  loc=%s",
                job_id, job.document_type.value, job.location_type.value)

    try:
        if job.location_type == LocationType.LOCAL and local_file_path:
            text_gen = convert_document(local_file_path, job.document_type)
            bucket, key, total_chars = upload_text_chunks(
                text_gen,
                bucket=job.output_s3_bucket,
                key=job.output_s3_key,
                job_id=job_id,
            )
            try:
                os.unlink(local_file_path)
            except OSError:
                pass
        else:
            for tmp_path in fetch_document(job):
                text_gen = convert_document(tmp_path, job.document_type)
                bucket, key, total_chars = upload_text_chunks(
                    text_gen,
                    bucket=job.output_s3_bucket,
                    key=job.output_s3_key,
                    job_id=job_id,
                )

        logger.info("Job %s complete → s3://%s/%s  (%d chars)",
                     job_id, bucket, key, total_chars)
        return ConversionResult(
            job_id=job_id,
            success=True,
            output_bucket=bucket,
            output_key=key,
            characters_extracted=total_chars,
        )

    except Exception as exc:
        logger.exception("Job %s failed: %s", job_id, exc)
        return ConversionResult(
            job_id=job_id,
            success=False,
            error=str(exc),
        )
