"""
RTF → Text converter.

Uses striprtf to remove RTF control words and yield plain text.
"""

import logging
from typing import Generator

from striprtf.striprtf import rtf_to_text

from config.settings import settings

logger = logging.getLogger(__name__)


def convert_to_text(file_path: str) -> Generator[str, None, None]:
    logger.info("RTF convert: %s", file_path)
    chunk_size = settings.chunk_size

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()

    text = rtf_to_text(raw)
    del raw

    for i in range(0, len(text), chunk_size):
        chunk = text[i : i + chunk_size]
        if chunk.strip():
            yield chunk
