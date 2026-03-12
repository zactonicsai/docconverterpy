"""
PDF → Text converter.

Strategy:
  1. Extract text from each page with pdfplumber (handles selectable text).
  2. For each page, also extract embedded images and run OCR via Tesseract
     to capture text in scanned regions.
  3. Yield text one page at a time to keep memory flat.
"""

import logging
import os
import tempfile
from typing import Generator

import pdfplumber
from pdf2image import convert_from_path
import pytesseract
from PIL import Image

from config.settings import settings

logger = logging.getLogger(__name__)

# Maximum image dimension before OCR (prevents OOM on huge scans)
MAX_OCR_DIM = 4000


def _ocr_image(image: Image.Image) -> str:
    """Run Tesseract OCR on a PIL Image, resizing if needed."""
    w, h = image.size
    if max(w, h) > MAX_OCR_DIM:
        ratio = MAX_OCR_DIM / max(w, h)
        image = image.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    return pytesseract.image_to_string(image)


def _extract_images_from_page(page) -> Generator[str, None, None]:
    """
    Extract images embedded in a single pdfplumber page object.
    Yields OCR text for each image found.
    """
    try:
        for img_info in page.images:
            # Crop the image region from the page
            bbox = (
                img_info["x0"],
                img_info["top"],
                img_info["x1"],
                img_info["bottom"],
            )
            cropped = page.within_bbox(bbox).to_image(resolution=200)
            # Convert to PIL
            pil_img = cropped.original
            text = _ocr_image(pil_img)
            if text.strip():
                yield text.strip()
    except Exception as exc:
        logger.debug("Image extraction from page failed: %s", exc)


def _ocr_full_page(pdf_path: str, page_num: int) -> str:
    """
    Render a full PDF page as an image and OCR it.
    Used as fallback when pdfplumber extracts no selectable text.
    Writes the rendered image to a tmp file to save memory.
    """
    tmp_img_path = None
    try:
        images = convert_from_path(
            pdf_path,
            first_page=page_num + 1,
            last_page=page_num + 1,
            dpi=200,
            fmt="png",
            output_folder=settings.tmp_dir,
        )
        if not images:
            return ""
        pil_img = images[0]
        text = _ocr_image(pil_img)

        # Explicitly save to tmp and free memory
        tmp_img_path = os.path.join(
            settings.tmp_dir, f"_ocrpage_{page_num}.png"
        )
        pil_img.save(tmp_img_path)
        del pil_img, images
        return text.strip()
    except Exception as exc:
        logger.warning("Full-page OCR failed for page %d: %s", page_num, exc)
        return ""
    finally:
        if tmp_img_path and os.path.exists(tmp_img_path):
            os.unlink(tmp_img_path)


def convert_to_text(file_path: str) -> Generator[str, None, None]:
    """
    Yield text one page at a time from a PDF.

    For each page:
      - First try native text extraction (fast, no OCR).
      - If no text found, fall back to full-page OCR.
      - Also extract embedded images and OCR them.
    """
    logger.info("PDF convert: %s", file_path)
    with pdfplumber.open(file_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            parts: list[str] = []

            # 1) Native text
            native_text = (page.extract_text() or "").strip()

            if native_text:
                parts.append(native_text)
            else:
                # 2) Full-page OCR fallback
                ocr_text = _ocr_full_page(file_path, page_num)
                if ocr_text:
                    parts.append(ocr_text)

            # 3) Embedded-image OCR (always attempt)
            for img_text in _extract_images_from_page(page):
                parts.append(f"[IMAGE TEXT] {img_text}")

            page_text = "\n".join(parts)
            if page_text:
                yield f"--- Page {page_num + 1} ---\n{page_text}\n"
