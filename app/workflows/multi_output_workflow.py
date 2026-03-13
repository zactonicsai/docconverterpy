"""
MultiFormatOutputWorkflow – one document, multiple output artifacts.

Given a single source document, produces:
  - Full text file (s3://.../full_text.txt)
  - Per-page text files (s3://.../pages/page_0001.txt, ...)
  - Metadata JSON with analytics (s3://.../metadata.json)

Use case: Document ingestion pipelines that need structured output for
downstream search indexing, RAG systems, or analytics dashboards.
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
        MultiFormatInput, MultiFormatOutput,
        AnalyzeTextInput, AnalyzeTextOutput,
    )
    from app.workflows.document_workflows import CHILD_WORKFLOW_MAP


def _retry():
    from temporalio.common import RetryPolicy
    return RetryPolicy(
        initial_interval=timedelta(seconds=5),
        maximum_attempts=3,
    )


@workflow.defn(name="MultiFormatOutputWorkflow")
class MultiFormatOutputWorkflow:
    """
    Convert a document and produce multiple output artifacts in S3.
    """

    def __init__(self):
        self._status = "INITIALIZED"
        self._step = ""
        self._temp_files: list[str] = []

    @workflow.query(name="multi_format_status")
    def get_status(self) -> dict:
        return {"status": self._status, "step": self._step}

    @workflow.run
    async def run(self, inp: MultiFormatInput) -> MultiFormatOutput:
        workflow.logger.info("MultiFormat started  job=%s  type=%s", inp.job_id, inp.document_type)
        self._status = "RUNNING"
        outputs = {}

        try:
            # ── 1. FETCH ─────────────────────────────────────────────────
            local_path = inp.local_file_path
            if not local_path:
                self._step = "FETCHING"
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

            # ── 2. CONVERT ───────────────────────────────────────────────
            self._step = "CONVERTING"
            self._status = f"CONVERTING_{inp.document_type.upper()}"

            child_cls = CHILD_WORKFLOW_MAP.get(inp.document_type)
            if not child_cls:
                return MultiFormatOutput(
                    job_id=inp.job_id, success=False,
                    error=f"Unsupported type: {inp.document_type}",
                )

            child_name = child_cls.__temporal_workflow_definition.name
            convert_result: ConvertOutput = await workflow.execute_child_workflow(
                child_name,
                ConvertInput(job_id=inp.job_id, local_path=local_path,
                             document_type=inp.document_type),
                id=f"multi-convert-{inp.job_id}",
                task_queue=settings.temporal_task_queue,
            )
            self._temp_files.append(convert_result.text_path)

            bucket = inp.output_s3_bucket or settings.s3_output_bucket
            prefix = f"{inp.output_prefix}{inp.job_id}/"

            # ── 3. UPLOAD FULL TEXT ──────────────────────────────────────
            if inp.produce_full_text:
                self._step = "UPLOADING_FULL_TEXT"
                text_key = f"{prefix}full_text.txt"

                await workflow.execute_activity(
                    "upload_text",
                    UploadInput(job_id=inp.job_id, text_path=convert_result.text_path,
                                output_bucket=bucket, output_key=text_key),
                    start_to_close_timeout=timedelta(seconds=300),
                    heartbeat_timeout=timedelta(seconds=120),
                    retry_policy=_retry(),
                )
                outputs["full_text"] = f"s3://{bucket}/{text_key}"

            # ── 4. SPLIT BY PAGES ────────────────────────────────────────
            if inp.produce_pages:
                self._step = "SPLITTING_PAGES"
                page_prefix = f"{prefix}pages/"

                page_results = await workflow.execute_activity(
                    "split_text_by_pages",
                    args=[inp.job_id, convert_result.text_path, bucket, page_prefix],
                    start_to_close_timeout=timedelta(seconds=300),
                    heartbeat_timeout=timedelta(seconds=120),
                    retry_policy=_retry(),
                )
                outputs["pages"] = page_results

            # ── 5. ANALYZE TEXT ──────────────────────────────────────────
            analytics = None
            if inp.produce_analytics:
                self._step = "ANALYZING"
                analytics = await workflow.execute_activity(
                    "analyze_text",
                    AnalyzeTextInput(job_id=inp.job_id, text_path=convert_result.text_path),
                    start_to_close_timeout=timedelta(seconds=300),
                    heartbeat_timeout=timedelta(seconds=120),
                    retry_policy=_retry(),
                )
                outputs["analytics"] = {
                    "total_words": analytics.total_words,
                    "total_pages": analytics.total_pages,
                    "language": analytics.language_hint,
                    "has_tables": analytics.has_tables,
                    "has_images": analytics.has_images,
                    "top_words": analytics.top_words,
                }

            # ── 6. METADATA JSON ─────────────────────────────────────────
            if inp.produce_metadata_json and analytics:
                self._step = "GENERATING_METADATA"
                meta_key = f"{prefix}metadata.json"
                text_key_for_meta = outputs.get("full_text", "")

                await workflow.execute_activity(
                    "generate_metadata_json",
                    args=[inp.job_id, inp.document_type, analytics,
                          text_key_for_meta, bucket, meta_key],
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=_retry(),
                )
                outputs["metadata"] = f"s3://{bucket}/{meta_key}"

            # ── 7. CLEANUP ───────────────────────────────────────────────
            self._step = "CLEANUP"
            await workflow.execute_activity(
                "cleanup_temp_files",
                CleanupInput(paths=self._temp_files),
                start_to_close_timeout=timedelta(seconds=30),
            )

            self._status = "COMPLETED"
            self._step = "DONE"
            return MultiFormatOutput(job_id=inp.job_id, success=True, outputs=outputs)

        except Exception as exc:
            workflow.logger.exception("MultiFormat failed  job=%s: %s", inp.job_id, exc)
            try:
                await workflow.execute_activity(
                    "cleanup_temp_files", CleanupInput(paths=self._temp_files),
                    start_to_close_timeout=timedelta(seconds=30),
                )
            except Exception:
                pass
            self._status = "FAILED"
            return MultiFormatOutput(job_id=inp.job_id, success=False, error=str(exc))
