"""
DocumentPipelineWorkflow – extended conversion pipeline.

Goes beyond basic conversion with:
  1. Fetch the document
  2. Validate (size, MIME type, corruption check)
  3. Convert to text (via per-type child workflow)
  4. Analyze text (word count, language, structure detection)
  5. Generate metadata JSON (analytics + job info → S3)
  6. Upload text to S3
  7. Send completion webhook
  8. Cleanup temp files

Each step is a durable, retryable activity.  The workflow is queryable
and supports cancellation.
"""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from config.settings import settings
    from app.workflows.dataclasses import (
        FetchInput, FetchOutput,
        ConvertInput, ConvertOutput,
        UploadInput, UploadOutput,
        CleanupInput,
    )
    from app.workflows.dataclasses_ext import (
        PipelineWorkflowInput, PipelineWorkflowOutput,
        ValidateInput, ValidateOutput,
        AnalyzeTextInput, AnalyzeTextOutput,
        WebhookInput,
    )
    from app.workflows.document_workflows import CHILD_WORKFLOW_MAP


def _retry():
    from temporalio.common import RetryPolicy
    return RetryPolicy(
        initial_interval=timedelta(seconds=5),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=60),
        maximum_attempts=3,
    )


@workflow.defn(name="DocumentPipelineWorkflow")
class DocumentPipelineWorkflow:
    """
    Extended document conversion pipeline with validation, analytics,
    metadata generation, and webhook notifications.
    """

    def __init__(self):
        self._status = "INITIALIZED"
        self._step = ""
        self._cancelled = False
        self._temp_files: list[str] = []

    @workflow.query(name="pipeline_status")
    def get_status(self) -> dict:
        return {"status": self._status, "step": self._step}

    @workflow.signal(name="cancel")
    async def cancel(self):
        self._cancelled = True
        self._status = "CANCELLING"

    @workflow.run
    async def run(self, inp: PipelineWorkflowInput) -> PipelineWorkflowOutput:
        workflow.logger.info("Pipeline started  job=%s  type=%s", inp.job_id, inp.document_type)
        self._status = "RUNNING"

        try:
            # ── 1. FETCH ─────────────────────────────────────────────────
            local_path = inp.local_file_path
            if not local_path:
                self._step = "FETCHING"
                self._status = "FETCHING"

                fetch_result: FetchOutput = await workflow.execute_activity(
                    "fetch_document",
                    FetchInput(
                        job_id=inp.job_id,
                        location_type=inp.location_type,
                        document_type=inp.document_type,
                        s3_bucket=inp.s3_bucket, s3_key=inp.s3_key,
                        s3_endpoint_url=inp.s3_endpoint_url,
                        url=inp.url,
                        ftp_host=inp.ftp_host, ftp_port=inp.ftp_port,
                        ftp_path=inp.ftp_path, ftp_user=inp.ftp_user,
                        ftp_pass=inp.ftp_pass,
                        auth_type=inp.auth_type,
                        auth_username=inp.auth_username,
                        auth_password=inp.auth_password,
                        auth_token=inp.auth_token,
                    ),
                    start_to_close_timeout=timedelta(seconds=settings.temporal_activity_timeout),
                    heartbeat_timeout=timedelta(seconds=120),
                    retry_policy=_retry(),
                )
                local_path = fetch_result.local_path
                self._temp_files.append(local_path)

            if self._cancelled:
                return self._fail(inp.job_id, "Cancelled")

            # ── 2. VALIDATE ──────────────────────────────────────────────
            if inp.enable_validation:
                self._step = "VALIDATING"
                self._status = "VALIDATING"

                val_result: ValidateOutput = await workflow.execute_activity(
                    "validate_document",
                    ValidateInput(
                        job_id=inp.job_id,
                        local_path=local_path,
                        document_type=inp.document_type,
                        max_file_size_mb=inp.max_file_size_mb,
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_retry(),
                )

                if not val_result.valid:
                    return self._fail(inp.job_id, f"Validation failed: {val_result.error}")

            if self._cancelled:
                return self._fail(inp.job_id, "Cancelled")

            # ── 3. CONVERT ───────────────────────────────────────────────
            self._step = "CONVERTING"
            self._status = f"CONVERTING_{inp.document_type.upper()}"

            child_cls = CHILD_WORKFLOW_MAP.get(inp.document_type)
            if not child_cls:
                return self._fail(inp.job_id, f"Unsupported type: {inp.document_type}")

            child_name = child_cls.__temporal_workflow_definition.name
            convert_result: ConvertOutput = await workflow.execute_child_workflow(
                child_name,
                ConvertInput(job_id=inp.job_id, local_path=local_path,
                             document_type=inp.document_type),
                id=f"pipeline-convert-{inp.job_id}",
                task_queue=settings.temporal_task_queue,
            )
            self._temp_files.append(convert_result.text_path)

            if self._cancelled:
                return self._fail(inp.job_id, "Cancelled")

            # ── 4. ANALYZE ───────────────────────────────────────────────
            analytics = None
            if inp.enable_analytics:
                self._step = "ANALYZING"
                self._status = "ANALYZING"

                analytics = await workflow.execute_activity(
                    "analyze_text",
                    AnalyzeTextInput(job_id=inp.job_id, text_path=convert_result.text_path),
                    start_to_close_timeout=timedelta(seconds=300),
                    heartbeat_timeout=timedelta(seconds=120),
                    retry_policy=_retry(),
                )

            # ── 5. UPLOAD TEXT ───────────────────────────────────────────
            self._step = "UPLOADING"
            self._status = "UPLOADING"

            bucket = inp.output_s3_bucket or settings.s3_output_bucket
            text_key = f"{inp.output_prefix}{inp.job_id}/text.txt"

            upload_result: UploadOutput = await workflow.execute_activity(
                "upload_text",
                UploadInput(job_id=inp.job_id, text_path=convert_result.text_path,
                            output_bucket=bucket, output_key=text_key),
                start_to_close_timeout=timedelta(seconds=settings.temporal_activity_timeout),
                heartbeat_timeout=timedelta(seconds=120),
                retry_policy=_retry(),
            )

            # ── 6. GENERATE METADATA JSON ────────────────────────────────
            metadata_key = None
            if inp.enable_metadata_output and analytics:
                self._step = "GENERATING_METADATA"
                self._status = "GENERATING_METADATA"

                meta_key = f"{inp.output_prefix}{inp.job_id}/metadata.json"
                metadata_key = await workflow.execute_activity(
                    "generate_metadata_json",
                    args=[inp.job_id, inp.document_type, analytics, text_key, bucket, meta_key],
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=_retry(),
                )

            # ── 7. WEBHOOK ───────────────────────────────────────────────
            if inp.on_complete_webhook:
                self._step = "WEBHOOK"
                self._status = "SENDING_WEBHOOK"

                await workflow.execute_activity(
                    "send_webhook",
                    WebhookInput(
                        url=inp.on_complete_webhook,
                        auth_token=inp.webhook_auth_token,
                        payload={
                            "job_id": inp.job_id,
                            "status": "completed",
                            "text_key": text_key,
                            "metadata_key": metadata_key,
                            "total_chars": convert_result.total_chars,
                        },
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_retry(),
                )

            # ── 8. CLEANUP ───────────────────────────────────────────────
            self._step = "CLEANUP"
            await workflow.execute_activity(
                "cleanup_temp_files",
                CleanupInput(paths=self._temp_files),
                start_to_close_timeout=timedelta(seconds=30),
            )

            self._status = "COMPLETED"
            self._step = "DONE"

            return PipelineWorkflowOutput(
                job_id=inp.job_id,
                success=True,
                text_bucket=bucket,
                text_key=text_key,
                metadata_key=metadata_key,
                analytics=analytics,
                total_chars=convert_result.total_chars,
                pages_processed=convert_result.pages_processed,
            )

        except Exception as exc:
            workflow.logger.exception("Pipeline failed  job=%s: %s", inp.job_id, exc)
            await self._cleanup()
            return self._fail(inp.job_id, str(exc))

    def _fail(self, job_id: str, error: str) -> PipelineWorkflowOutput:
        self._status = "FAILED"
        return PipelineWorkflowOutput(job_id=job_id, success=False, error=error)

    async def _cleanup(self):
        try:
            await workflow.execute_activity(
                "cleanup_temp_files", CleanupInput(paths=self._temp_files),
                start_to_close_timeout=timedelta(seconds=30),
            )
        except Exception:
            pass
