"""
REST API for the document conversion service.

Endpoints:
  POST /convert/job       – submit a ConversionJob (fetch from remote source)
  POST /convert/upload    – upload a file directly for conversion
  GET  /health            – liveness check
"""

import logging
import os
import tempfile
import uuid
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse

from config.settings import settings
from app.models import ConversionJob, ConversionResult, DocumentType, LocationType
from app.processor import process_job

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Document Conversion Service",
    version="1.0.0",
    description="Convert documents and images to plain text, stored on S3.",
)


# ── Health check ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "api_enabled": settings.enable_api,
        "sqs_enabled": settings.enable_sqs,
        "rabbitmq_enabled": settings.enable_rabbitmq,
        "kafka_enabled": settings.enable_kafka,
    }


# ── Submit a job (remote source) ────────────────────────────────────────────

@app.post("/convert/job", response_model=ConversionResult)
async def submit_job(job: ConversionJob):
    """
    Accept a ConversionJob JSON body.  The service will fetch the document
    from the specified location (S3, URL, FTP), convert it, and upload
    the text to the output S3 bucket.
    """
    if job.location_type == LocationType.LOCAL:
        raise HTTPException(400, "Use /convert/upload for local file uploads")

    result = process_job(job)
    status = 200 if result.success else 500
    return JSONResponse(content=result.model_dump(), status_code=status)


# ── Direct file upload ──────────────────────────────────────────────────────

@app.post("/convert/upload", response_model=ConversionResult)
async def upload_file(
    file: UploadFile = File(...),
    document_type: DocumentType = Form(...),
    output_s3_bucket: Optional[str] = Form(None),
    output_s3_key: Optional[str] = Form(None),
):
    """
    Upload a file directly.  The service saves it to a tmp file, converts
    it, and uploads the text to S3.
    """
    job_id = uuid.uuid4().hex[:12]
    ext = os.path.splitext(file.filename or "")[1] or ""
    tmp_path = os.path.join(settings.tmp_dir, f"{job_id}{ext}")

    # Stream uploaded file to disk in chunks (low memory)
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = await file.read(settings.chunk_size)
                if not chunk:
                    break
                out.write(chunk)
    except Exception as exc:
        raise HTTPException(500, f"Failed to save upload: {exc}")

    job = ConversionJob(
        job_id=job_id,
        document_type=document_type,
        location_type=LocationType.LOCAL,
        output_s3_bucket=output_s3_bucket,
        output_s3_key=output_s3_key,
    )

    result = process_job(job, local_file_path=tmp_path)
    status = 200 if result.success else 500
    return JSONResponse(content=result.model_dump(), status_code=status)
