"""
Processing pipeline: fetch → convert → upload.

Accepts a ConversionJob, orchestrates the three stages, and returns
a ConversionResult.  All stages use generators / tmp files to stay
memory-efficient.
"""

import logging
import os
import uuid

from app.models import ConversionJob, ConversionResult, LocationType
from app.fetchers.dispatch import fetch_document
from app.converters.dispatch import convert_document
from app.storage import upload_text_chunks

logger = logging.getLogger(__name__)


def process_job(job: ConversionJob, local_file_path: str | None = None) -> ConversionResult:
    """
    Execute the full conversion pipeline for a single job.

    Parameters
    ----------
    job : ConversionJob
        The job descriptor (what to fetch, what type, where to put it).
    local_file_path : str | None
        If the file is already on disk (API upload), pass the path here
        and set job.location_type to LOCAL.
    """
    job_id = job.job_id or uuid.uuid4().hex[:12]
    logger.info("Processing job %s  type=%s  loc=%s", job_id, job.document_type, job.location_type)

    try:
        if job.location_type == LocationType.LOCAL and local_file_path:
            # File already on disk (API upload path)
            text_gen = convert_document(local_file_path, job.document_type)
            bucket, key, total_chars = upload_text_chunks(
                text_gen,
                bucket=job.output_s3_bucket,
                key=job.output_s3_key,
                job_id=job_id,
            )
            # Clean up the uploaded file
            try:
                os.unlink(local_file_path)
            except OSError:
                pass
        else:
            # Fetch from remote source → convert → upload
            for tmp_path in fetch_document(job):
                text_gen = convert_document(tmp_path, job.document_type)
                bucket, key, total_chars = upload_text_chunks(
                    text_gen,
                    bucket=job.output_s3_bucket,
                    key=job.output_s3_key,
                    job_id=job_id,
                )

        logger.info("Job %s complete → s3://%s/%s  (%d chars)", job_id, bucket, key, total_chars)
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
