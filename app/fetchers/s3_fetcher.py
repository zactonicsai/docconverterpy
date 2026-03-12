"""
Fetch a document from an S3 bucket to a local temporary file.
Uses streaming / chunked download to avoid loading the entire object in memory.
"""

import logging
import tempfile
from typing import Generator

import boto3

from config.settings import settings

logger = logging.getLogger(__name__)


def _get_s3_client(endpoint_url: str | None = None):
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url or settings.s3_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_default_region,
    )


def fetch_from_s3(
    bucket: str,
    key: str,
    endpoint_url: str | None = None,
) -> Generator[str, None, None]:
    """
    Download an S3 object in chunks and yield the local tmp path.

    Usage:
        for tmp_path in fetch_from_s3(bucket, key):
            process(tmp_path)
        # file is auto-deleted after the generator exits
    """
    client = _get_s3_client(endpoint_url)
    suffix = "." + key.rsplit(".", 1)[-1] if "." in key else ""

    tmp = tempfile.NamedTemporaryFile(
        dir=settings.tmp_dir, suffix=suffix, delete=False
    )
    try:
        logger.info("Downloading s3://%s/%s → %s", bucket, key, tmp.name)
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        while True:
            chunk = body.read(settings.chunk_size)
            if not chunk:
                break
            tmp.write(chunk)
        tmp.flush()
        tmp.close()
        yield tmp.name
    finally:
        import os
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
