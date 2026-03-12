"""
Converter dispatcher.

Maps DocumentType → converter module and calls convert_to_text(file_path).
Each converter module exposes:
    def convert_to_text(file_path: str) -> Generator[str, None, None]
"""

import logging
from typing import Generator

from app.models import DocumentType
from app.converters import (
    pdf_converter,
    docx_converter,
    xlsx_converter,
    pptx_converter,
    html_converter,
    rtf_converter,
    odt_converter,
    text_converter,
    image_converter,
)

logger = logging.getLogger(__name__)

_CONVERTER_MAP = {
    DocumentType.PDF: pdf_converter,
    DocumentType.DOCX: docx_converter,
    DocumentType.XLSX: xlsx_converter,
    DocumentType.CSV: xlsx_converter,       # CSV handled by xlsx module
    DocumentType.PPTX: pptx_converter,
    DocumentType.HTML: html_converter,
    DocumentType.RTF: rtf_converter,
    DocumentType.ODT: odt_converter,
    DocumentType.TXT: text_converter,
    DocumentType.IMAGE: image_converter,
}


def convert_document(file_path: str, doc_type: DocumentType) -> Generator[str, None, None]:
    """
    Select the right converter and yield text chunks.
    Raises ValueError for unsupported types.
    """
    module = _CONVERTER_MAP.get(doc_type)
    if module is None:
        raise ValueError(f"Unsupported document type: {doc_type}")

    logger.info("Dispatching %s → %s", doc_type.value, module.__name__)
    yield from module.convert_to_text(file_path)
