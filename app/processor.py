"""
Processing pipeline: fetch → convert → upload.

Supports two execution modes:
  1. Direct (USE_TEMPORAL_WORKFLOWS=false): inline pipeline.
  2. Temporal (USE_TEMPORAL_WORKFLOWS=true): durable workflow for ALL jobs,
     including direct file uploads. Every conversion appears in the
     Temporal dashboard.

For LOCAL uploads, the workflow skips the fetch step (file is already on
disk) and goes straight to convert → upload → cleanup.
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


def process_job(job: ConversionJob, local_file_path: str | None = None) -> ConversionResult:
    """
    Execute the full conversion pipeline for a single job.

    When Temporal is enabled, ALL jobs (including file uploads) route
    through Temporal for full dashboard visibility. The workflow skips
    the fetch step for LOCAL uploads since the file is already on disk.
    """
    if settings.use_temporal_workflows and settings.enable_temporal:
        return _process_via_temporal(job, local_file_path)

    return _process_direct(job, local_file_path)


def _process_via_temporal(
    job: ConversionJob, local_file_path: str | None = None
) -> ConversionResult:
    """Submit any job to Temporal – remote or local upload."""
    job_id = job.job_id or uuid.uuid4().hex[:12]
    job.job_id = job_id
    logger.info("Routing job %s through Temporal  type=%s  loc=%s  local_path=%s",
                job_id, job.document_type.value, job.location_type.value,
                "yes" if local_file_path else "no")

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


def _process_direct(
    job: ConversionJob, local_file_path: str | None = None
) -> ConversionResult:
    """Direct (inline) processing – fallback when Temporal is off or unreachable."""
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
