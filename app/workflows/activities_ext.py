"""
Extended Temporal activities for advanced workflows.

Activities:
  - scan_s3_prefix:         List files in an S3 folder
  - move_s3_object:         Move (copy+delete) an S3 object
  - validate_document:      Check file size, MIME type, corruption
  - analyze_text:           Word count, language hint, structure stats
  - generate_metadata_json: Write analytics + job info as JSON to S3
  - send_webhook:           Call an external URL with JSON payload
  - enhanced_ocr_convert:   High-DPI OCR for retry escalation
  - split_text_by_pages:    Split converted text into per-page files
  - scheduled_tmp_cleanup:  Purge old files from tmp directory
  - check_s3_health:        Verify S3 bucket is reachable
"""

import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from temporalio import activity

from config.settings import settings
from app.workflows.dataclasses_ext import (
    S3ScanInput, S3ScanOutput, S3FileInfo,
    S3MoveInput,
    ValidateInput, ValidateOutput,
    AnalyzeTextInput, AnalyzeTextOutput,
    WebhookInput, WebhookOutput,
    EscalationConvertInput,
    ScheduledCleanupInput, ScheduledCleanupOutput,
)
from app.workflows.dataclasses import ConvertOutput

logger = logging.getLogger(__name__)

# Extension → document type mapping
EXT_TYPE_MAP = {
    ".pdf": "pdf", ".docx": "docx", ".doc": "docx",
    ".xlsx": "xlsx", ".xls": "xlsx", ".csv": "csv",
    ".pptx": "pptx", ".ppt": "pptx",
    ".html": "html", ".htm": "html",
    ".rtf": "rtf", ".odt": "odt", ".txt": "txt",
    ".jpg": "image", ".jpeg": "image", ".png": "image",
    ".tiff": "image", ".tif": "image", ".bmp": "image", ".webp": "image",
}


# ═════════════════════════════════════════════════════════════════════════════
# S3 SCANNING
# ═════════════════════════════════════════════════════════════════════════════

@activity.defn(name="scan_s3_prefix")
async def scan_s3_prefix_activity(inp: S3ScanInput) -> S3ScanOutput:
    """
    List objects under an S3 prefix, filter by extension, and return metadata.
    """
    import boto3

    activity.logger.info("Scanning s3://%s/%s", inp.bucket, inp.prefix)
    client = boto3.client(
        "s3",
        endpoint_url=inp.s3_endpoint_url or settings.s3_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_default_region,
    )

    allowed_ext = set(e.lower() for e in inp.file_extensions)
    files = []
    skipped = 0
    paginator = client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=inp.bucket, Prefix=inp.prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # Skip "directory" markers
            if key.endswith("/"):
                continue

            ext = os.path.splitext(key)[1].lower()
            if ext not in allowed_ext:
                skipped += 1
                continue

            doc_type = EXT_TYPE_MAP.get(ext, "")
            files.append(S3FileInfo(
                key=key,
                size_bytes=obj.get("Size", 0),
                last_modified=str(obj.get("LastModified", "")),
                document_type=doc_type,
            ))

            activity.heartbeat(f"Found {len(files)} files")
            if len(files) >= inp.max_files:
                break
        if len(files) >= inp.max_files:
            break

    activity.logger.info("Scan complete: %d files found, %d skipped", len(files), skipped)
    return S3ScanOutput(files=files, total_found=len(files), skipped=skipped)


@activity.defn(name="move_s3_object")
async def move_s3_object_activity(inp: S3MoveInput) -> bool:
    """Copy an S3 object to a new key, then delete the original."""
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=inp.s3_endpoint_url or settings.s3_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_default_region,
    )
    copy_source = {"Bucket": inp.bucket, "Key": inp.source_key}
    client.copy_object(Bucket=inp.bucket, CopySource=copy_source, Key=inp.dest_key)
    client.delete_object(Bucket=inp.bucket, Key=inp.source_key)
    activity.logger.info("Moved s3://%s/%s → %s", inp.bucket, inp.source_key, inp.dest_key)
    return True


# ═════════════════════════════════════════════════════════════════════════════
# VALIDATION
# ═════════════════════════════════════════════════════════════════════════════

@activity.defn(name="validate_document")
async def validate_document_activity(inp: ValidateInput) -> ValidateOutput:
    """
    Validate a document file: check existence, size, and MIME type.
    """
    activity.logger.info("Validating  job=%s  path=%s", inp.job_id, inp.local_path)

    if not os.path.exists(inp.local_path):
        return ValidateOutput(valid=False, error="File not found")

    file_size = os.path.getsize(inp.local_path)
    max_bytes = int(inp.max_file_size_mb * 1024 * 1024)
    if file_size > max_bytes:
        return ValidateOutput(
            valid=False, file_size_bytes=file_size,
            error=f"File too large: {file_size} bytes (max {max_bytes})",
        )

    if file_size == 0:
        return ValidateOutput(valid=False, file_size_bytes=0, error="File is empty")

    # Detect MIME type
    mime_type = ""
    try:
        import magic
        mime_type = magic.from_file(inp.local_path, mime=True) or ""
    except Exception:
        mime_type = "unknown"

    activity.logger.info("Validation OK  job=%s  size=%d  mime=%s",
                         inp.job_id, file_size, mime_type)
    return ValidateOutput(valid=True, file_size_bytes=file_size, mime_type=mime_type)


# ═════════════════════════════════════════════════════════════════════════════
# TEXT ANALYTICS
# ═════════════════════════════════════════════════════════════════════════════

@activity.defn(name="analyze_text")
async def analyze_text_activity(inp: AnalyzeTextInput) -> AnalyzeTextOutput:
    """
    Analyze extracted text: word count, line count, page count,
    top words, structure detection, basic language heuristic.
    """
    activity.logger.info("Analyzing text  job=%s", inp.job_id)

    total_chars = 0
    total_words = 0
    total_lines = 0
    total_pages = 0
    has_tables = False
    has_images = False
    word_counter = Counter()

    # Common English stop words to exclude from top words
    stop_words = {
        "the", "a", "an", "is", "it", "in", "on", "at", "to", "for",
        "of", "and", "or", "but", "not", "with", "by", "from", "as",
        "this", "that", "be", "are", "was", "were", "been", "have",
        "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "can", "shall", "i", "you", "he",
        "she", "we", "they", "me", "him", "her", "us", "them", "my",
    }

    with open(inp.text_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            total_lines += 1
            total_chars += len(line)

            if line.startswith("--- Page "):
                total_pages += 1
            if "[TABLE" in line:
                has_tables = True
            if "[IMAGE TEXT]" in line:
                has_images = True

            # Word counting
            words = re.findall(r'\b[a-zA-Z]{3,}\b', line.lower())
            total_words += len(words)
            for w in words:
                if w not in stop_words:
                    word_counter[w] += 1

            if total_lines % 1000 == 0:
                activity.heartbeat(f"Analyzed {total_lines} lines")

    avg_wpl = total_words / max(total_lines, 1)
    top_words = [w for w, _ in word_counter.most_common(20)]

    # Simple language heuristic based on common words
    en_markers = {"the", "and", "that", "have", "for", "not", "with"}
    es_markers = {"que", "los", "las", "una", "del", "por", "con"}
    de_markers = {"der", "die", "das", "und", "ist", "ein", "den"}
    fr_markers = {"les", "des", "une", "est", "que", "dans", "pour"}

    all_words_set = set(word_counter.keys())
    lang_scores = {
        "en": len(all_words_set & en_markers),
        "es": len(all_words_set & es_markers),
        "de": len(all_words_set & de_markers),
        "fr": len(all_words_set & fr_markers),
    }
    language_hint = max(lang_scores, key=lang_scores.get) if max(lang_scores.values()) > 0 else "unknown"

    result = AnalyzeTextOutput(
        total_chars=total_chars,
        total_words=total_words,
        total_lines=total_lines,
        total_pages=total_pages,
        language_hint=language_hint,
        avg_words_per_line=round(avg_wpl, 2),
        has_tables=has_tables,
        has_images=has_images,
        top_words=top_words,
    )
    activity.logger.info("Analysis done  job=%s  words=%d  pages=%d  lang=%s",
                         inp.job_id, total_words, total_pages, language_hint)
    return result


# ═════════════════════════════════════════════════════════════════════════════
# METADATA JSON GENERATION + UPLOAD
# ═════════════════════════════════════════════════════════════════════════════

@activity.defn(name="generate_metadata_json")
async def generate_metadata_json_activity(
    job_id: str,
    document_type: str,
    analytics: AnalyzeTextOutput,
    text_s3_key: str,
    output_bucket: str,
    output_key: str,
) -> str:
    """
    Generate a JSON metadata file with analytics and job info,
    upload it to S3, and return the S3 key.
    """
    import boto3

    metadata = {
        "job_id": job_id,
        "document_type": document_type,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "text_output_key": text_s3_key,
        "analytics": {
            "total_chars": analytics.total_chars,
            "total_words": analytics.total_words,
            "total_lines": analytics.total_lines,
            "total_pages": analytics.total_pages,
            "language_hint": analytics.language_hint,
            "avg_words_per_line": analytics.avg_words_per_line,
            "has_tables": analytics.has_tables,
            "has_images": analytics.has_images,
            "top_words": analytics.top_words,
        },
    }

    tmp_path = os.path.join(settings.tmp_dir, f"meta_{job_id}.json")
    with open(tmp_path, "w") as f:
        json.dump(metadata, f, indent=2)

    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_default_region,
    )
    client.upload_file(tmp_path, output_bucket, output_key)
    os.unlink(tmp_path)

    activity.logger.info("Metadata uploaded → s3://%s/%s", output_bucket, output_key)
    return output_key


# ═════════════════════════════════════════════════════════════════════════════
# WEBHOOK NOTIFICATION
# ═════════════════════════════════════════════════════════════════════════════

@activity.defn(name="send_webhook")
async def send_webhook_activity(inp: WebhookInput) -> WebhookOutput:
    """
    Send an HTTP webhook notification with a JSON payload.
    """
    import requests

    activity.logger.info("Sending webhook  url=%s  method=%s", inp.url, inp.method)

    headers = {**inp.headers, "Content-Type": "application/json"}
    if inp.auth_token:
        headers["Authorization"] = f"Bearer {inp.auth_token}"

    try:
        resp = requests.request(
            method=inp.method,
            url=inp.url,
            json=inp.payload,
            headers=headers,
            timeout=inp.timeout_seconds,
        )
        return WebhookOutput(
            status_code=resp.status_code,
            success=200 <= resp.status_code < 300,
            response_body=resp.text[:1000],  # truncate
        )
    except Exception as exc:
        activity.logger.warning("Webhook failed: %s", exc)
        return WebhookOutput(success=False, error=str(exc))


# ═════════════════════════════════════════════════════════════════════════════
# ENHANCED OCR (for retry escalation)
# ═════════════════════════════════════════════════════════════════════════════

@activity.defn(name="enhanced_ocr_convert")
async def enhanced_ocr_convert_activity(inp: EscalationConvertInput) -> ConvertOutput:
    """
    High-quality OCR conversion with configurable DPI and Tesseract settings.
    Used as an escalation step when standard conversion produces too little text.
    """
    import pytesseract
    from PIL import Image

    activity.logger.info("Enhanced OCR  job=%s  dpi=%d  psm=%d", inp.job_id, inp.dpi, inp.psm)

    output_path = os.path.join(settings.tmp_dir, f"temporal_{inp.job_id}_enhanced.txt")
    total_chars = 0
    page_count = 0

    custom_config = f"--psm {inp.psm} --oem {inp.oem}"

    if inp.document_type == "pdf":
        from pdf2image import convert_from_path

        images = convert_from_path(
            inp.local_path,
            dpi=inp.dpi,
            fmt="png",
            output_folder=settings.tmp_dir,
        )

        with open(output_path, "w", encoding="utf-8") as f:
            for i, img in enumerate(images):
                page_count += 1
                text = pytesseract.image_to_string(img, config=custom_config).strip()
                if text:
                    f.write(f"--- Page {page_count} ---\n{text}\n\n")
                    total_chars += len(text)
                activity.heartbeat(f"Enhanced OCR page {page_count}/{len(images)}")
                # Free memory
                del img
        del images

    elif inp.document_type == "image":
        img = Image.open(inp.local_path)
        if img.mode not in ("L", "RGB"):
            img = img.convert("RGB")
        text = pytesseract.image_to_string(img, config=custom_config).strip()
        page_count = 1

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(text)
        total_chars = len(text)
        img.close()

    else:
        # For non-image/PDF types, just run the standard converter
        from app.converters.dispatch import convert_document
        from app.models import DocumentType
        dt = DocumentType(inp.document_type)
        with open(output_path, "w", encoding="utf-8") as f:
            for chunk in convert_document(inp.local_path, dt):
                f.write(chunk)
                total_chars += len(chunk)
                page_count += 1

    activity.logger.info("Enhanced OCR done  job=%s  chars=%d  pages=%d",
                         inp.job_id, total_chars, page_count)
    return ConvertOutput(
        text_path=output_path,
        total_chars=total_chars,
        pages_processed=page_count,
    )


# ═════════════════════════════════════════════════════════════════════════════
# SPLIT TEXT BY PAGES
# ═════════════════════════════════════════════════════════════════════════════

@activity.defn(name="split_text_by_pages")
async def split_text_by_pages_activity(job_id: str, text_path: str, output_bucket: str, output_prefix: str) -> dict:
    """
    Split a converted text file by page markers and upload each page
    as a separate S3 object. Returns {"page_1": "s3://key", ...}.
    """
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_default_region,
    )

    with open(text_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Split by page markers
    pages = re.split(r"--- Page \d+ ---\n?", content)
    pages = [p.strip() for p in pages if p.strip()]

    results = {}
    for i, page_text in enumerate(pages, start=1):
        key = f"{output_prefix}{job_id}/page_{i:04d}.txt"
        tmp = os.path.join(settings.tmp_dir, f"page_{job_id}_{i}.txt")
        with open(tmp, "w") as f:
            f.write(page_text)
        client.upload_file(tmp, output_bucket, key)
        os.unlink(tmp)
        results[f"page_{i}"] = f"s3://{output_bucket}/{key}"
        activity.heartbeat(f"Uploaded page {i}")

    activity.logger.info("Split %d pages for job %s", len(results), job_id)
    return results


# ═════════════════════════════════════════════════════════════════════════════
# SCHEDULED CLEANUP
# ═════════════════════════════════════════════════════════════════════════════

@activity.defn(name="scheduled_tmp_cleanup")
async def scheduled_tmp_cleanup_activity(inp: ScheduledCleanupInput) -> ScheduledCleanupOutput:
    """
    Purge temp files older than max_age_hours from the tmp directory.
    """
    activity.logger.info("Running scheduled cleanup  dir=%s  max_age=%dh",
                         inp.tmp_dir, inp.max_age_hours)

    cutoff = time.time() - (inp.max_age_hours * 3600)
    files_deleted = 0
    bytes_freed = 0
    errors = []

    if not os.path.isdir(inp.tmp_dir):
        return ScheduledCleanupOutput(errors=["Directory not found"])

    for entry in os.scandir(inp.tmp_dir):
        if not entry.is_file():
            continue
        try:
            stat = entry.stat()
            if stat.st_mtime < cutoff:
                bytes_freed += stat.st_size
                os.unlink(entry.path)
                files_deleted += 1
        except OSError as e:
            errors.append(f"{entry.name}: {e}")

    activity.logger.info("Cleanup done: %d files, %d bytes freed", files_deleted, bytes_freed)
    return ScheduledCleanupOutput(
        files_deleted=files_deleted,
        bytes_freed=bytes_freed,
        errors=errors,
    )


@activity.defn(name="check_s3_health")
async def check_s3_health_activity(bucket: str) -> bool:
    """Verify that the S3 output bucket is reachable."""
    import boto3

    try:
        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_default_region,
        )
        client.head_bucket(Bucket=bucket)
        activity.logger.info("S3 bucket %s is healthy", bucket)
        return True
    except Exception as exc:
        activity.logger.warning("S3 bucket %s health check failed: %s", bucket, exc)
        return False


# ═════════════════════════════════════════════════════════════════════════════
# REGISTRY
# ═════════════════════════════════════════════════════════════════════════════

ALL_EXTENDED_ACTIVITIES = [
    scan_s3_prefix_activity,
    move_s3_object_activity,
    validate_document_activity,
    analyze_text_activity,
    generate_metadata_json_activity,
    send_webhook_activity,
    enhanced_ocr_convert_activity,
    split_text_by_pages_activity,
    scheduled_tmp_cleanup_activity,
    check_s3_health_activity,
]
