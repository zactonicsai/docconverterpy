"""
HTML → Text converter.

Uses BeautifulSoup to strip tags and yield readable text in chunks.
"""

import logging
from typing import Generator

from bs4 import BeautifulSoup

from config.settings import settings

logger = logging.getLogger(__name__)


def convert_to_text(file_path: str) -> Generator[str, None, None]:
    logger.info("HTML convert: %s", file_path)
    chunk_size = settings.chunk_size

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()

    soup = BeautifulSoup(raw, "lxml")

    # Remove script/style elements
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)
    del soup, raw  # free memory

    # Yield in chunks
    for i in range(0, len(text), chunk_size):
        chunk = text[i : i + chunk_size]
        if chunk.strip():
            yield chunk
