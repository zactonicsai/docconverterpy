"""
Upload converted text to S3 (output bucket).

Uses multipart upload so we can stream chunks without building the full
text string in memory.
"""

import logging
import tempfile
import os
from typing import Generator

import boto3

from config.settings import settings

logger = logging.getLogger(__name__)

# S3 multipart minimum part size is 5 MB; we buffer to that
MIN_PART_SIZE = 5 * 1024 * 1024  # 5 MB


def _get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_default_region,
    )


def upload_text_chunks(
    text_chunks: Generator[str, None, None],
    bucket: str | None = None,
    key: str | None = None,
    job_id: str | None = None,
) -> tuple[str, str, int]:
    """
    Stream text chunks to a local tmp file, then upload to S3.
    Returns (bucket, key, total_chars).

    We write to a tmp file first to avoid buffering potentially huge text
    in memory and to handle S3 upload retries gracefully.
    """
    bucket = bucket or settings.s3_output_bucket
    if not key:
        import uuid
        _id = job_id or uuid.uuid4().hex[:12]
        key = f"converted/{_id}.txt"

    tmp_path = os.path.join(settings.tmp_dir, f"_upload_{os.getpid()}.txt")
    total_chars = 0

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            for chunk in text_chunks:
                f.write(chunk)
                total_chars += len(chunk)

        client = _get_s3_client()
        logger.info(
            "Uploading %d chars → s3://%s/%s", total_chars, bucket, key
        )
        client.upload_file(tmp_path, bucket, key)
        return bucket, key, total_chars

    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
