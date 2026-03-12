"""
Fetch a document from a URL (HTTP/HTTPS) to a local temporary file.
Supports Basic auth, Bearer token, or no auth.
"""

import logging
import os
import tempfile
from typing import Generator, Optional
from urllib.parse import urlparse

import requests

from config.settings import settings

logger = logging.getLogger(__name__)


def fetch_from_url(
    url: str,
    auth_type: str = "none",
    username: Optional[str] = None,
    password: Optional[str] = None,
    token: Optional[str] = None,
) -> Generator[str, None, None]:
    """
    Stream-download a URL to a tmp file, yield the path, then clean up.
    """
    headers = {}
    auth = None

    if auth_type == "basic" and username and password:
        auth = (username, password)
    elif auth_type == "bearer" and token:
        headers["Authorization"] = f"Bearer {token}"

    parsed = urlparse(url)
    ext = os.path.splitext(parsed.path)[1] or ""

    tmp = tempfile.NamedTemporaryFile(
        dir=settings.tmp_dir, suffix=ext, delete=False
    )
    try:
        logger.info("Downloading %s → %s", url, tmp.name)
        with requests.get(url, headers=headers, auth=auth, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=settings.chunk_size):
                if chunk:
                    tmp.write(chunk)
        tmp.flush()
        tmp.close()
        yield tmp.name
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
