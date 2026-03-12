"""
Image → Text converter (OCR via Tesseract).

Handles: JPEG, PNG, TIFF, BMP, WEBP.
Large images are down-scaled before OCR to limit memory.
"""

import logging
from typing import Generator

import pytesseract
from PIL import Image

logger = logging.getLogger(__name__)

MAX_DIM = 4000  # max px on either axis


def convert_to_text(file_path: str) -> Generator[str, None, None]:
    """
    OCR a single image file.  Yields the extracted text.
    For multi-frame TIFFs, each frame is OCR'd separately.
    """
    logger.info("Image OCR convert: %s", file_path)
    img = Image.open(file_path)

    # Handle multi-frame images (e.g. multi-page TIFF)
    n_frames = getattr(img, "n_frames", 1)
    for frame_idx in range(n_frames):
        if n_frames > 1:
            img.seek(frame_idx)

        working = img.copy()

        # Down-scale if too large
        w, h = working.size
        if max(w, h) > MAX_DIM:
            ratio = MAX_DIM / max(w, h)
            working = working.resize(
                (int(w * ratio), int(h * ratio)), Image.LANCZOS
            )

        # Convert to RGB if necessary (e.g. RGBA, palette)
        if working.mode not in ("L", "RGB"):
            working = working.convert("RGB")

        text = pytesseract.image_to_string(working).strip()
        del working

        if text:
            header = (
                f"--- Frame {frame_idx + 1}/{n_frames} ---\n"
                if n_frames > 1
                else ""
            )
            yield f"{header}{text}\n"

    img.close()
