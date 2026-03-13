"""
DocumentConversionWorkflow – top-level durable workflow.

Orchestrates the full pipeline:
  1. Fetch the source document (activity)
  2. Convert to text via a document-type-specific child workflow
  3. Upload text to S3 (activity)
  4. Cleanup temp files (activity)

Features:
  - Durable execution: survives worker crashes and restarts
  - Automatic retries with exponential backoff on each step
  - Queryable state: callers can query current status mid-execution
  - Signals: supports cancellation signal
  - Full audit trail visible in the Temporal UI
"""

from datetime import timedelta
from typing import Optional

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from config.settings import settings
    from app.workflows.dataclasses import (
        ConversionWorkflowInput,
        ConversionWorkflowOutput,
        FetchInput, FetchOutput,
        ConvertInput, ConvertOutput,
        UploadInput, UploadOutput,
        CleanupInput,
    )
    from app.workflows.document_workflows import CHILD_WORKFLOW_MAP


@workflow.defn(name="DocumentConversionWorkflow")
class DocumentConversionWorkflow:
    """
    Top-level durable workflow for document conversion.

    Usage from the Temporal client:
        handle = await client.start_workflow(
            DocumentConversionWorkflow.run,
            ConversionWorkflowInput(...),
            id=f"docconv-{job_id}",
            task_queue="docconv-tasks",
        )
        result = await handle.result()
    """

    def __init__(self):
        self._status = "INITIALIZED"
        self._cancelled = False
        self._current_step = ""
        self._temp_files: list[str] = []

    # ── Queryable state ──────────────────────────────────────────────────

    @workflow.query(name="get_status")
    def get_status(self) -> str:
        return self._status

    @workflow.query(name="get_step")
    def get_step(self) -> str:
        return self._current_step

    # ── Cancellation signal ──────────────────────────────────────────────

    @workflow.signal(name="cancel")
    async def cancel(self):
        self._cancelled = True
        self._status = "CANCELLING"

    # ── Retry policy ─────────────────────────────────────────────────────

    @staticmethod
    def _retry_policy():
        from temporalio.common import RetryPolicy
        return RetryPolicy(
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(seconds=120),
            maximum_attempts=settings.temporal_retry_max_attempts,
        )

    # ── Main run ─────────────────────────────────────────────────────────

    @workflow.run
    async def run(self, inp: ConversionWorkflowInput) -> ConversionWorkflowOutput:
        workflow.logger.info(
            "DocumentConversionWorkflow started  job=%s  type=%s  loc=%s",
            inp.job_id, inp.document_type, inp.location_type,
        )
        self._status = "RUNNING"

        try:
            # ── Step 1: Fetch ────────────────────────────────────────────
            if self._cancelled:
                return self._cancelled_result(inp.job_id)

            local_path: Optional[str] = inp.local_file_path

            # Track the local file for cleanup (applies to API uploads)
            if local_path:
                self._temp_files.append(local_path)

            if not local_path:
                # No local file → must fetch from remote source
                # Validate that LOCAL without a file path is an error
                if inp.location_type == "local":
                    return ConversionWorkflowOutput(
                        job_id=inp.job_id,
                        success=False,
                        error="LOCAL location requires a local_file_path",
                    )

                # Validate required fields for remote sources
                if inp.location_type == "s3" and (not inp.s3_bucket or not inp.s3_key):
                    return ConversionWorkflowOutput(
                        job_id=inp.job_id,
                        success=False,
                        error="s3_bucket and s3_key are required for S3 location",
                    )
                if inp.location_type == "url" and not inp.url:
                    return ConversionWorkflowOutput(
                        job_id=inp.job_id,
                        success=False,
                        error="url is required for URL location",
                    )
                if inp.location_type == "ftp" and (not inp.ftp_host or not inp.ftp_path):
                    return ConversionWorkflowOutput(
                        job_id=inp.job_id,
                        success=False,
                        error="ftp_host and ftp_path are required for FTP location",
                    )

                self._current_step = "FETCHING"
                self._status = "FETCHING"

                fetch_input = FetchInput(
                    job_id=inp.job_id,
                    location_type=inp.location_type,
                    document_type=inp.document_type,
                    s3_bucket=inp.s3_bucket,
                    s3_key=inp.s3_key,
                    s3_endpoint_url=inp.s3_endpoint_url,
                    url=inp.url,
                    ftp_host=inp.ftp_host,
                    ftp_port=inp.ftp_port,
                    ftp_path=inp.ftp_path,
                    ftp_user=inp.ftp_user,
                    ftp_pass=inp.ftp_pass,
                    auth_type=inp.auth_type,
                    auth_username=inp.auth_username,
                    auth_password=inp.auth_password,
                    auth_token=inp.auth_token,
                )

                fetch_result: FetchOutput = await workflow.execute_activity(
                    "fetch_document",
                    fetch_input,
                    start_to_close_timeout=timedelta(
                        seconds=settings.temporal_activity_timeout
                    ),
                    heartbeat_timeout=timedelta(seconds=120),
                    retry_policy=self._retry_policy(),
                )
                local_path = fetch_result.local_path
                self._temp_files.append(local_path)

                workflow.logger.info(
                    "Fetch complete  job=%s  path=%s  size=%d",
                    inp.job_id, local_path, fetch_result.file_size_bytes,
                )

            # ── Step 2: Convert via child workflow ───────────────────────
            if self._cancelled:
                return self._cancelled_result(inp.job_id)

            self._current_step = "CONVERTING"
            self._status = f"CONVERTING_{inp.document_type.upper()}"

            child_cls = CHILD_WORKFLOW_MAP.get(inp.document_type)
            if child_cls is None:
                return ConversionWorkflowOutput(
                    job_id=inp.job_id,
                    success=False,
                    error=f"Unsupported document type: {inp.document_type}",
                )

            convert_input = ConvertInput(
                job_id=inp.job_id,
                local_path=local_path,
                document_type=inp.document_type,
            )

            # Execute the type-specific child workflow
            child_workflow_name = child_cls.__temporal_workflow_definition.name  # type: ignore
            convert_result: ConvertOutput = await workflow.execute_child_workflow(
                child_workflow_name,
                convert_input,
                id=f"docconv-convert-{inp.job_id}",
                task_queue=settings.temporal_task_queue,
            )

            self._temp_files.append(convert_result.text_path)
            workflow.logger.info(
                "Convert complete  job=%s  chars=%d  pages=%d",
                inp.job_id, convert_result.total_chars, convert_result.pages_processed,
            )

            # ── Step 3: Upload to S3 ─────────────────────────────────────
            if self._cancelled:
                return self._cancelled_result(inp.job_id)

            self._current_step = "UPLOADING"
            self._status = "UPLOADING"

            upload_input = UploadInput(
                job_id=inp.job_id,
                text_path=convert_result.text_path,
                output_bucket=inp.output_s3_bucket,
                output_key=inp.output_s3_key,
            )

            upload_result: UploadOutput = await workflow.execute_activity(
                "upload_text",
                upload_input,
                start_to_close_timeout=timedelta(
                    seconds=settings.temporal_activity_timeout
                ),
                heartbeat_timeout=timedelta(seconds=120),
                retry_policy=self._retry_policy(),
            )

            workflow.logger.info(
                "Upload complete  job=%s  s3://%s/%s",
                inp.job_id, upload_result.bucket, upload_result.key,
            )

            # ── Step 4: Cleanup ──────────────────────────────────────────
            self._current_step = "CLEANUP"
            self._status = "CLEANUP"

            await workflow.execute_activity(
                "cleanup_temp_files",
                CleanupInput(paths=self._temp_files),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=self._retry_policy(),
            )

            # ── Done ─────────────────────────────────────────────────────
            self._status = "COMPLETED"
            self._current_step = "DONE"

            return ConversionWorkflowOutput(
                job_id=inp.job_id,
                success=True,
                output_bucket=upload_result.bucket,
                output_key=upload_result.key,
                total_chars=convert_result.total_chars,
                pages_processed=convert_result.pages_processed,
                images_extracted=convert_result.images_extracted,
            )

        except Exception as exc:
            self._status = "FAILED"
            self._current_step = f"ERROR: {exc}"
            workflow.logger.exception("Workflow failed  job=%s: %s", inp.job_id, exc)

            # Best-effort cleanup
            try:
                await workflow.execute_activity(
                    "cleanup_temp_files",
                    CleanupInput(paths=self._temp_files),
                    start_to_close_timeout=timedelta(seconds=30),
                )
            except Exception:
                pass

            return ConversionWorkflowOutput(
                job_id=inp.job_id,
                success=False,
                error=str(exc),
            )

    # ── Helpers ──────────────────────────────────────────────────────────

    def _cancelled_result(self, job_id: str) -> ConversionWorkflowOutput:
        self._status = "CANCELLED"
        return ConversionWorkflowOutput(
            job_id=job_id,
            success=False,
            error="Workflow cancelled by signal",
        )


# ═════════════════════════════════════════════════════════════════════════════
# Batch workflow – process multiple documents in parallel
# ═════════════════════════════════════════════════════════════════════════════

@workflow.defn(name="BatchConversionWorkflow")
class BatchConversionWorkflow:
    """
    Process multiple documents in parallel using child workflows.

    Usage:
        handle = await client.start_workflow(
            BatchConversionWorkflow.run,
            [input1, input2, input3],
            id="batch-001",
            task_queue="docconv-tasks",
        )
    """

    def __init__(self):
        self._total = 0
        self._completed = 0
        self._failed = 0

    @workflow.query(name="get_batch_progress")
    def get_progress(self) -> dict:
        return {
            "total": self._total,
            "completed": self._completed,
            "failed": self._failed,
        }

    @workflow.run
    async def run(
        self, inputs: list[ConversionWorkflowInput]
    ) -> list[ConversionWorkflowOutput]:
        self._total = len(inputs)
        workflow.logger.info("BatchConversionWorkflow started  count=%d", self._total)

        # Launch all child workflows in parallel
        import asyncio
        tasks = []
        for i, inp in enumerate(inputs):
            child_id = f"docconv-batch-{inp.job_id or i}"
            task = workflow.execute_child_workflow(
                "DocumentConversionWorkflow",
                inp,
                id=child_id,
                task_queue=settings.temporal_task_queue,
            )
            tasks.append(task)

        results: list[ConversionWorkflowOutput] = []
        for coro in asyncio.as_completed(tasks):
            try:
                result = await coro
                results.append(result)
                if result.success:
                    self._completed += 1
                else:
                    self._failed += 1
            except Exception as exc:
                self._failed += 1
                results.append(ConversionWorkflowOutput(
                    job_id="unknown",
                    success=False,
                    error=str(exc),
                ))

        workflow.logger.info(
            "Batch complete  total=%d  ok=%d  fail=%d",
            self._total, self._completed, self._failed,
        )
        return results
