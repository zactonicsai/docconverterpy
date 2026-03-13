"""
RetryEscalationWorkflow – intelligent retry with quality escalation.

Strategy:
  1. Try standard conversion (fast, default settings)
  2. Check if the result meets a quality threshold (min characters)
  3. If not, escalate to enhanced OCR (higher DPI, different Tesseract settings)
  4. Upload the best result

Use case: Scanned PDFs and images where standard 200 DPI OCR may produce
garbage or too little text.  The escalation step uses 400 DPI and different
Tesseract page segmentation modes.
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
        RetryEscalationInput,
        EscalationConvertInput,
    )
    from app.workflows.document_workflows import CHILD_WORKFLOW_MAP


def _retry():
    from temporalio.common import RetryPolicy
    return RetryPolicy(
        initial_interval=timedelta(seconds=5),
        maximum_attempts=3,
    )


@workflow.defn(name="RetryEscalationWorkflow")
class RetryEscalationWorkflow:
    """
    Convert a document, check quality, escalate if needed.
    """

    def __init__(self):
        self._status = "INITIALIZED"
        self._attempt = 0
        self._escalated = False
        self._temp_files: list[str] = []

    @workflow.query(name="escalation_status")
    def get_status(self) -> dict:
        return {
            "status": self._status,
            "attempt": self._attempt,
            "escalated": self._escalated,
        }

    @workflow.run
    async def run(self, inp: RetryEscalationInput) -> ConvertOutput:
        workflow.logger.info("RetryEscalation started  job=%s  threshold=%d chars",
                             inp.job_id, inp.min_chars_threshold)
        self._status = "RUNNING"

        try:
            # ── 1. FETCH ─────────────────────────────────────────────────
            local_path = inp.local_file_path
            if not local_path:
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

            # ── 2. STANDARD CONVERSION (attempt 1) ───────────────────────
            self._attempt = 1
            self._status = "CONVERTING_STANDARD"

            child_cls = CHILD_WORKFLOW_MAP.get(inp.document_type)
            if not child_cls:
                from app.workflows.dataclasses import ConversionWorkflowOutput
                raise ValueError(f"Unsupported type: {inp.document_type}")

            child_name = child_cls.__temporal_workflow_definition.name
            standard_result: ConvertOutput = await workflow.execute_child_workflow(
                child_name,
                ConvertInput(job_id=inp.job_id, local_path=local_path,
                             document_type=inp.document_type),
                id=f"escalate-std-{inp.job_id}",
                task_queue=settings.temporal_task_queue,
            )
            self._temp_files.append(standard_result.text_path)

            workflow.logger.info(
                "Standard conversion: %d chars (threshold: %d)",
                standard_result.total_chars, inp.min_chars_threshold,
            )

            # ── 3. CHECK QUALITY ─────────────────────────────────────────
            best_result = standard_result

            if standard_result.total_chars < inp.min_chars_threshold:
                # Only escalate for types where OCR matters
                if inp.document_type in ("pdf", "image"):
                    self._attempt = 2
                    self._escalated = True
                    self._status = "ESCALATING_OCR"

                    workflow.logger.info(
                        "Below threshold (%d < %d) – escalating to enhanced OCR at %d DPI",
                        standard_result.total_chars, inp.min_chars_threshold,
                        inp.escalation_dpi,
                    )

                    enhanced_result: ConvertOutput = await workflow.execute_activity(
                        "enhanced_ocr_convert",
                        EscalationConvertInput(
                            job_id=inp.job_id,
                            local_path=local_path,
                            document_type=inp.document_type,
                            dpi=inp.escalation_dpi,
                            psm=6,  # assume uniform block of text
                            oem=3,  # default LSTM engine
                        ),
                        start_to_close_timeout=timedelta(seconds=900),
                        heartbeat_timeout=timedelta(seconds=120),
                        retry_policy=_retry(),
                    )
                    self._temp_files.append(enhanced_result.text_path)

                    workflow.logger.info(
                        "Enhanced OCR: %d chars (was %d)",
                        enhanced_result.total_chars, standard_result.total_chars,
                    )

                    # Use the result with more text
                    if enhanced_result.total_chars > standard_result.total_chars:
                        best_result = enhanced_result
                else:
                    workflow.logger.info(
                        "Below threshold but type=%s doesn't support OCR escalation",
                        inp.document_type,
                    )

            # ── 4. UPLOAD BEST RESULT ────────────────────────────────────
            self._status = "UPLOADING"
            bucket = inp.output_s3_bucket or settings.s3_output_bucket
            key = inp.output_s3_key or f"converted/{inp.job_id}.txt"

            await workflow.execute_activity(
                "upload_text",
                UploadInput(job_id=inp.job_id, text_path=best_result.text_path,
                            output_bucket=bucket, output_key=key),
                start_to_close_timeout=timedelta(seconds=300),
                heartbeat_timeout=timedelta(seconds=120),
                retry_policy=_retry(),
            )

            # ── 5. CLEANUP ───────────────────────────────────────────────
            await workflow.execute_activity(
                "cleanup_temp_files",
                CleanupInput(paths=self._temp_files),
                start_to_close_timeout=timedelta(seconds=30),
            )

            self._status = "COMPLETED"
            return best_result

        except Exception as exc:
            self._status = "FAILED"
            workflow.logger.exception("Escalation failed  job=%s: %s", inp.job_id, exc)
            try:
                await workflow.execute_activity(
                    "cleanup_temp_files", CleanupInput(paths=self._temp_files),
                    start_to_close_timeout=timedelta(seconds=30),
                )
            except Exception:
                pass
            return ConvertOutput(text_path="", total_chars=0)
