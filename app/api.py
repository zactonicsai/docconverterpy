"""
REST API for the document conversion service.

Endpoints:
  GET  /               – API Test Console (HTML UI)
  GET  /health         – liveness check
  POST /convert/job    – submit a ConversionJob (fetch from remote source)
  POST /convert/upload – upload a file directly for conversion
"""

import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from config.settings import settings
from app.models import ConversionJob, ConversionResult, DocumentType, LocationType
from app.processor import process_job

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Document Conversion Service",
    version="1.0.0",
    description="Convert documents and images to plain text, stored on S3.",
)

# ── CORS (allow the test console and external callers) ───────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static files (test console) ─────────────────────────────────────────────
_static_dir = Path(__file__).parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# ── Root → test console ─────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    index = _static_dir / "index.html"
    if index.exists():
        return FileResponse(str(index), media_type="text/html")
    return {"message": "Document Conversion Service", "docs": "/docs"}


# ── Health check ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "api_enabled": settings.enable_api,
        "sqs_enabled": settings.enable_sqs,
        "rabbitmq_enabled": settings.enable_rabbitmq,
        "kafka_enabled": settings.enable_kafka,
        "temporal_enabled": settings.enable_temporal,
        "temporal_workflows_active": settings.use_temporal_workflows,
        "temporal_host": settings.temporal_host if settings.enable_temporal else None,
        "temporal_task_queue": settings.temporal_task_queue if settings.enable_temporal else None,
    }


# ── Temporal workflow status ─────────────────────────────────────────────────

@app.get("/workflow/{workflow_id}/status")
async def get_workflow_status(workflow_id: str):
    """
    Query the current status of a Temporal workflow by ID.
    Returns the workflow status and current processing step.
    """
    if not settings.enable_temporal:
        raise HTTPException(400, "Temporal is not enabled")

    try:
        from temporalio.client import Client
        client = await Client.connect(
            settings.temporal_host,
            namespace=settings.temporal_namespace,
        )
        handle = client.get_workflow_handle(workflow_id)
        desc = await handle.describe()

        # Query custom state
        status_val = None
        step_val = None
        try:
            status_val = await handle.query("get_status")
        except Exception:
            pass
        try:
            step_val = await handle.query("get_step")
        except Exception:
            pass

        return {
            "workflow_id": workflow_id,
            "run_id": desc.run_id,
            "status": desc.status.name if desc.status else "UNKNOWN",
            "custom_status": status_val,
            "current_step": step_val,
            "start_time": str(desc.start_time) if desc.start_time else None,
            "close_time": str(desc.close_time) if desc.close_time else None,
            "task_queue": desc.task_queue,
        }
    except Exception as exc:
        raise HTTPException(404, f"Workflow not found or query failed: {exc}")


@app.post("/workflow/{workflow_id}/cancel")
async def cancel_workflow(workflow_id: str):
    """Send a cancellation signal to a running Temporal workflow."""
    if not settings.enable_temporal:
        raise HTTPException(400, "Temporal is not enabled")

    try:
        from temporalio.client import Client
        client = await Client.connect(
            settings.temporal_host,
            namespace=settings.temporal_namespace,
        )
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal("cancel")
        return {"workflow_id": workflow_id, "message": "Cancellation signal sent"}
    except Exception as exc:
        raise HTTPException(404, f"Failed to cancel workflow: {exc}")


@app.get("/workflows/recent")
async def list_recent_workflows(limit: int = 20):
    """List recent document conversion workflows from Temporal."""
    if not settings.enable_temporal:
        raise HTTPException(400, "Temporal is not enabled")

    try:
        from temporalio.client import Client
        client = await Client.connect(
            settings.temporal_host,
            namespace=settings.temporal_namespace,
        )
        workflows = []
        async for wf in client.list_workflows(
            query=f'TaskQueue="{settings.temporal_task_queue}"',
        ):
            workflows.append({
                "id": wf.id,
                "run_id": wf.run_id,
                "status": wf.status.name if wf.status else "UNKNOWN",
                "workflow_type": wf.workflow_type,
                "start_time": str(wf.start_time) if wf.start_time else None,
                "close_time": str(wf.close_time) if wf.close_time else None,
            })
            if len(workflows) >= limit:
                break
        return {"workflows": workflows, "count": len(workflows)}
    except Exception as exc:
        raise HTTPException(500, f"Failed to list workflows: {exc}")


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


# ═════════════════════════════════════════════════════════════════════════════
# Extended Workflow Endpoints
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/workflow/pipeline")
async def start_pipeline(body: dict):
    """
    Start a DocumentPipelineWorkflow (validate → convert → analyze → metadata → upload).
    Body fields: job_id, document_type, location_type, + source fields,
    enable_validation, enable_analytics, enable_metadata_output, on_complete_webhook.
    """
    if not settings.enable_temporal:
        raise HTTPException(400, "Temporal is not enabled")

    from temporalio.client import Client
    from app.workflows.dataclasses_ext import PipelineWorkflowInput

    job_id = body.get("job_id") or uuid.uuid4().hex[:12]
    inp = PipelineWorkflowInput(
        job_id=job_id,
        document_type=body["document_type"],
        location_type=body["location_type"],
        s3_bucket=body.get("s3_bucket"),
        s3_key=body.get("s3_key"),
        s3_endpoint_url=body.get("s3_endpoint_url"),
        url=body.get("url"),
        ftp_host=body.get("ftp_host"),
        ftp_port=body.get("ftp_port", 21),
        ftp_path=body.get("ftp_path"),
        ftp_user=body.get("ftp_user"),
        ftp_pass=body.get("ftp_pass"),
        auth_type=body.get("auth_type", "none"),
        auth_username=body.get("auth_username"),
        auth_password=body.get("auth_password"),
        auth_token=body.get("auth_token"),
        output_s3_bucket=body.get("output_s3_bucket"),
        output_prefix=body.get("output_prefix", "pipeline/"),
        enable_validation=body.get("enable_validation", True),
        enable_analytics=body.get("enable_analytics", True),
        enable_metadata_output=body.get("enable_metadata_output", True),
        max_file_size_mb=body.get("max_file_size_mb", 500.0),
        on_complete_webhook=body.get("on_complete_webhook"),
        webhook_auth_token=body.get("webhook_auth_token"),
    )

    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    handle = await client.start_workflow(
        "DocumentPipelineWorkflow", inp,
        id=f"pipeline-{job_id}",
        task_queue=settings.temporal_task_queue,
    )
    return {"workflow_id": handle.id, "run_id": handle.result_run_id, "job_id": job_id}


@app.post("/workflow/s3-watch")
async def start_s3_watch(body: dict):
    """
    Start an S3FolderWatchWorkflow – scan a prefix and convert all files found.
    Body fields: bucket, prefix, output_bucket, output_prefix, move_processed_to, max_files.
    Optional: cron_schedule (e.g. "0 * * * *" for hourly).
    """
    if not settings.enable_temporal:
        raise HTTPException(400, "Temporal is not enabled")

    from temporalio.client import Client
    from app.workflows.dataclasses_ext import S3FolderWatchInput

    inp = S3FolderWatchInput(
        bucket=body["bucket"],
        prefix=body.get("prefix", "inbox/"),
        output_bucket=body.get("output_bucket"),
        output_prefix=body.get("output_prefix", "converted/"),
        s3_endpoint_url=body.get("s3_endpoint_url"),
        move_processed_to=body.get("move_processed_to", "processed/"),
        max_files=body.get("max_files", 100),
    )

    workflow_id = body.get("workflow_id", f"s3watch-{uuid.uuid4().hex[:8]}")
    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)

    start_kwargs = {
        "id": workflow_id,
        "task_queue": settings.temporal_task_queue,
    }
    if body.get("cron_schedule"):
        start_kwargs["cron_schedule"] = body["cron_schedule"]

    handle = await client.start_workflow(
        "S3FolderWatchWorkflow", inp, **start_kwargs,
    )
    return {"workflow_id": handle.id, "run_id": handle.result_run_id}


@app.post("/workflow/webhook-convert")
async def start_webhook_conversion(body: dict):
    """
    Start a WebhookNotificationWorkflow – conversion + lifecycle webhooks.
    Body fields: same as /convert/job, plus on_start_webhook, on_complete_webhook,
    on_failure_webhook, webhook_auth_token.
    """
    if not settings.enable_temporal:
        raise HTTPException(400, "Temporal is not enabled")

    from temporalio.client import Client
    from app.workflows.dataclasses_ext import WebhookNotificationWorkflowInput

    job_id = body.get("job_id") or uuid.uuid4().hex[:12]
    inp = WebhookNotificationWorkflowInput(
        job_id=job_id,
        document_type=body["document_type"],
        location_type=body["location_type"],
        s3_bucket=body.get("s3_bucket"),
        s3_key=body.get("s3_key"),
        s3_endpoint_url=body.get("s3_endpoint_url"),
        url=body.get("url"),
        ftp_host=body.get("ftp_host"),
        ftp_port=body.get("ftp_port", 21),
        ftp_path=body.get("ftp_path"),
        ftp_user=body.get("ftp_user"),
        ftp_pass=body.get("ftp_pass"),
        auth_type=body.get("auth_type", "none"),
        output_s3_bucket=body.get("output_s3_bucket"),
        output_s3_key=body.get("output_s3_key"),
        on_start_webhook=body.get("on_start_webhook"),
        on_complete_webhook=body.get("on_complete_webhook"),
        on_failure_webhook=body.get("on_failure_webhook"),
        webhook_auth_token=body.get("webhook_auth_token"),
    )

    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    handle = await client.start_workflow(
        "WebhookNotificationWorkflow", inp,
        id=f"webhook-{job_id}",
        task_queue=settings.temporal_task_queue,
    )
    return {"workflow_id": handle.id, "run_id": handle.result_run_id, "job_id": job_id}


@app.post("/workflow/multi-format")
async def start_multi_format(body: dict):
    """
    Start a MultiFormatOutputWorkflow – produces full text, per-page files,
    metadata JSON, and analytics from a single document.
    Body fields: same as /convert/job, plus produce_full_text, produce_pages,
    produce_metadata_json, produce_analytics, output_prefix.
    """
    if not settings.enable_temporal:
        raise HTTPException(400, "Temporal is not enabled")

    from temporalio.client import Client
    from app.workflows.dataclasses_ext import MultiFormatInput

    job_id = body.get("job_id") or uuid.uuid4().hex[:12]
    inp = MultiFormatInput(
        job_id=job_id,
        document_type=body["document_type"],
        location_type=body["location_type"],
        s3_bucket=body.get("s3_bucket"),
        s3_key=body.get("s3_key"),
        s3_endpoint_url=body.get("s3_endpoint_url"),
        url=body.get("url"),
        ftp_host=body.get("ftp_host"),
        ftp_port=body.get("ftp_port", 21),
        ftp_path=body.get("ftp_path"),
        ftp_user=body.get("ftp_user"),
        ftp_pass=body.get("ftp_pass"),
        auth_type=body.get("auth_type", "none"),
        output_s3_bucket=body.get("output_s3_bucket"),
        output_prefix=body.get("output_prefix", "multi/"),
        produce_full_text=body.get("produce_full_text", True),
        produce_pages=body.get("produce_pages", True),
        produce_metadata_json=body.get("produce_metadata_json", True),
        produce_analytics=body.get("produce_analytics", True),
    )

    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    handle = await client.start_workflow(
        "MultiFormatOutputWorkflow", inp,
        id=f"multi-{job_id}",
        task_queue=settings.temporal_task_queue,
    )
    return {"workflow_id": handle.id, "run_id": handle.result_run_id, "job_id": job_id}


@app.post("/workflow/retry-escalation")
async def start_retry_escalation(body: dict):
    """
    Start a RetryEscalationWorkflow – standard conversion with automatic
    escalation to high-DPI OCR if initial output is too sparse.
    Body fields: same as /convert/job, plus min_chars_threshold (default 50),
    escalation_dpi (default 400).
    """
    if not settings.enable_temporal:
        raise HTTPException(400, "Temporal is not enabled")

    from temporalio.client import Client
    from app.workflows.dataclasses_ext import RetryEscalationInput

    job_id = body.get("job_id") or uuid.uuid4().hex[:12]
    inp = RetryEscalationInput(
        job_id=job_id,
        document_type=body["document_type"],
        location_type=body["location_type"],
        s3_bucket=body.get("s3_bucket"),
        s3_key=body.get("s3_key"),
        s3_endpoint_url=body.get("s3_endpoint_url"),
        url=body.get("url"),
        ftp_host=body.get("ftp_host"),
        ftp_port=body.get("ftp_port", 21),
        ftp_path=body.get("ftp_path"),
        ftp_user=body.get("ftp_user"),
        ftp_pass=body.get("ftp_pass"),
        auth_type=body.get("auth_type", "none"),
        output_s3_bucket=body.get("output_s3_bucket"),
        output_s3_key=body.get("output_s3_key"),
        min_chars_threshold=body.get("min_chars_threshold", 50),
        escalation_dpi=body.get("escalation_dpi", 400),
    )

    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    handle = await client.start_workflow(
        "RetryEscalationWorkflow", inp,
        id=f"escalate-{job_id}",
        task_queue=settings.temporal_task_queue,
    )
    return {"workflow_id": handle.id, "run_id": handle.result_run_id, "job_id": job_id}


@app.post("/workflow/maintenance")
async def start_maintenance(body: dict = {}):
    """
    Start a ScheduledMaintenanceWorkflow – cleanup old tmp files, check S3 health.
    Optional body: tmp_dir, max_age_hours, s3_output_bucket, cron_schedule.
    """
    if not settings.enable_temporal:
        raise HTTPException(400, "Temporal is not enabled")

    from temporalio.client import Client
    from app.workflows.dataclasses_ext import ScheduledCleanupInput

    inp = ScheduledCleanupInput(
        tmp_dir=body.get("tmp_dir", settings.tmp_dir),
        max_age_hours=body.get("max_age_hours", 24),
        s3_output_bucket=body.get("s3_output_bucket", settings.s3_output_bucket),
    )

    workflow_id = body.get("workflow_id", f"maintenance-{uuid.uuid4().hex[:8]}")
    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)

    start_kwargs = {
        "id": workflow_id,
        "task_queue": settings.temporal_task_queue,
    }
    if body.get("cron_schedule"):
        start_kwargs["cron_schedule"] = body["cron_schedule"]

    handle = await client.start_workflow(
        "ScheduledMaintenanceWorkflow", inp, **start_kwargs,
    )
    return {"workflow_id": handle.id, "run_id": handle.result_run_id}

