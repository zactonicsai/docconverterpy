"""
Dispatcher: given a ConversionJob, select the right fetcher and yield the
path to the downloaded tmp file.
"""

import logging
from typing import Generator

from app.models import ConversionJob, LocationType
from app.fetchers.s3_fetcher import fetch_from_s3
from app.fetchers.url_fetcher import fetch_from_url
from app.fetchers.ftp_fetcher import fetch_from_ftp

logger = logging.getLogger(__name__)


def fetch_document(job: ConversionJob) -> Generator[str, None, None]:
    """
    Yield the local tmp file path for the document described by *job*.
    The caller must consume the generator inside a for-loop so the
    cleanup logic in each fetcher runs.
    """
    if job.location_type == LocationType.S3:
        if not job.s3_bucket or not job.s3_key:
            raise ValueError("s3_bucket and s3_key required for S3 location")
        yield from fetch_from_s3(
            bucket=job.s3_bucket,
            key=job.s3_key,
            endpoint_url=job.s3_endpoint_url,
        )

    elif job.location_type == LocationType.URL:
        if not job.url:
            raise ValueError("url required for URL location")
        yield from fetch_from_url(
            url=job.url,
            auth_type=job.auth_type.value,
            username=job.auth_username,
            password=job.auth_password,
            token=job.auth_token,
        )

    elif job.location_type == LocationType.FTP:
        if not job.ftp_host or not job.ftp_path:
            raise ValueError("ftp_host and ftp_path required for FTP location")
        yield from fetch_from_ftp(
            host=job.ftp_host,
            path=job.ftp_path,
            port=job.ftp_port,
            username=job.ftp_user,
            password=job.ftp_pass,
        )

    elif job.location_type == LocationType.LOCAL:
        # LOCAL is for API file uploads – path is set by the API handler
        raise ValueError("LOCAL location type is handled by the API layer")

    else:
        raise ValueError(f"Unknown location_type: {job.location_type}")
