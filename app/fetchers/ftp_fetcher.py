"""
Fetch a document from an FTP server to a local temporary file.
Uses stdlib ftplib with chunked retrieval.
"""

import ftplib
import logging
import os
import tempfile
from typing import Generator, Optional

from config.settings import settings

logger = logging.getLogger(__name__)


def fetch_from_ftp(
    host: str,
    path: str,
    port: int = 21,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Generator[str, None, None]:
    """
    Download a file via FTP in binary mode, yield the local tmp path.
    """
    ext = os.path.splitext(path)[1] or ""
    tmp = tempfile.NamedTemporaryFile(
        dir=settings.tmp_dir, suffix=ext, delete=False
    )

    try:
        logger.info("FTP download ftp://%s:%d%s → %s", host, port, path, tmp.name)
        ftp = ftplib.FTP()
        ftp.connect(host, port, timeout=60)
        ftp.login(username or "anonymous", password or "")
        ftp.retrbinary(
            f"RETR {path}",
            callback=tmp.write,
            blocksize=settings.chunk_size,
        )
        ftp.quit()
        tmp.flush()
        tmp.close()
        yield tmp.name
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
