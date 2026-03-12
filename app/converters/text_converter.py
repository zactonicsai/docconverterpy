"""
Plain Text → Text converter (TXT, CSV already handled elsewhere for structure).

Reads the file in chunks, detects encoding, and yields text.
"""

import logging
from typing import Generator

import chardet

from config.settings import settings

logger = logging.getLogger(__name__)


def _detect_encoding(file_path: str) -> str:
    """Sniff the first 64 KB to guess encoding."""
    with open(file_path, "rb") as f:
        raw = f.read(settings.chunk_size)
    result = chardet.detect(raw)
    return result.get("encoding") or "utf-8"


def convert_to_text(file_path: str) -> Generator[str, None, None]:
    logger.info("TXT convert: %s", file_path)
    encoding = _detect_encoding(file_path)

    with open(file_path, "r", encoding=encoding, errors="replace") as f:
        while True:
            chunk = f.read(settings.chunk_size)
            if not chunk:
                break
            yield chunk
