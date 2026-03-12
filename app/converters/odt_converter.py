"""
ODT (OpenDocument Text) → Text converter.

Uses odfpy to read paragraphs from .odt files.
"""

import logging
from typing import Generator

from odf.opendocument import load
from odf.text import P
from odf import teletype

logger = logging.getLogger(__name__)

CHUNK_PARAGRAPHS = 50


def convert_to_text(file_path: str) -> Generator[str, None, None]:
    logger.info("ODT convert: %s", file_path)
    doc = load(file_path)
    paragraphs = doc.getElementsByType(P)
    buffer: list[str] = []

    for para in paragraphs:
        text = teletype.extractText(para).strip()
        if text:
            buffer.append(text)
        if len(buffer) >= CHUNK_PARAGRAPHS:
            yield "\n".join(buffer) + "\n"
            buffer.clear()

    if buffer:
        yield "\n".join(buffer) + "\n"
