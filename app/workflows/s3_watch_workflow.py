"""
S3FolderWatchWorkflow – scan an S3 prefix and convert all matching files.

Use case: Drop documents into an S3 "inbox/" folder, and this workflow
picks them up, converts each one (as a parallel child workflow), and
optionally moves processed files to a "processed/" prefix.

Can be run on a schedule via Temporal's cron feature:
    await client.start_workflow(
        "S3FolderWatchWorkflow", input,
        id="s3-watch-hourly",
        task_queue="docconv-tasks",
        cron_schedule="0 * * * *",   # every hour
    )
"""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from config.settings import settings
    from app.workflows.dataclasses import ConversionWorkflowInput
    from app.workflows.dataclasses_ext import (
        S3FolderWatchInput, S3FolderWatchOutput,
        S3ScanInput, S3ScanOutput, S3FileInfo,
        S3MoveInput,
    )


def _retry():
    from temporalio.common import RetryPolicy
    return RetryPolicy(
        initial_interval=timedelta(seconds=5),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=60),
        maximum_attempts=3,
    )


@workflow.defn(name="S3FolderWatchWorkflow")
class S3FolderWatchWorkflow:
    """
    Scan an S3 prefix for documents, convert each one in parallel,
    and optionally move processed originals to an archive prefix.
    """

    def __init__(self):
        self._status = "INITIALIZED"
        self._scanned = 0
        self._converted = 0
        self._failed = 0

    @workflow.query(name="watch_progress")
    def get_progress(self) -> dict:
        return {
            "status": self._status,
            "scanned": self._scanned,
            "converted": self._converted,
            "failed": self._failed,
        }

    @workflow.run
    async def run(self, inp: S3FolderWatchInput) -> S3FolderWatchOutput:
        workflow.logger.info("S3 folder watch  bucket=%s  prefix=%s",
                             inp.bucket, inp.prefix)
        self._status = "SCANNING"

        # ── 1. Scan the S3 prefix ────────────────────────────────────────
        scan_result: S3ScanOutput = await workflow.execute_activity(
            "scan_s3_prefix",
            S3ScanInput(
                bucket=inp.bucket,
                prefix=inp.prefix,
                s3_endpoint_url=inp.s3_endpoint_url,
                max_files=inp.max_files,
            ),
            start_to_close_timeout=timedelta(seconds=120),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=_retry(),
        )

        self._scanned = scan_result.total_found
        workflow.logger.info("Found %d files to convert", self._scanned)

        if self._scanned == 0:
            self._status = "COMPLETED_EMPTY"
            return S3FolderWatchOutput(total_scanned=0)

        # ── 2. Launch parallel conversion child workflows ────────────────
        self._status = "CONVERTING"
        import asyncio

        tasks = []
        file_map = {}  # child_id → S3FileInfo

        for file_info in scan_result.files:
            if not file_info.document_type:
                self._failed += 1
                continue

            child_id = f"s3watch-{file_info.key.replace('/', '-')}"
            file_map[child_id] = file_info

            conversion_input = ConversionWorkflowInput(
                job_id=child_id,
                document_type=file_info.document_type,
                location_type="s3",
                s3_bucket=inp.bucket,
                s3_key=file_info.key,
                s3_endpoint_url=inp.s3_endpoint_url,
                output_s3_bucket=inp.output_bucket or settings.s3_output_bucket,
                output_s3_key=f"{inp.output_prefix}{file_info.key.split('/')[-1]}.txt",
            )

            task = workflow.execute_child_workflow(
                "DocumentConversionWorkflow",
                conversion_input,
                id=child_id,
                task_queue=settings.temporal_task_queue,
            )
            tasks.append((child_id, task))

        # ── 3. Collect results ───────────────────────────────────────────
        results = []
        for child_id, task in tasks:
            try:
                result = await task
                if result.success:
                    self._converted += 1

                    # Move processed file if configured
                    if inp.move_processed_to:
                        fi = file_map[child_id]
                        original_name = fi.key.split("/")[-1]
                        dest_key = f"{inp.move_processed_to}{original_name}"
                        try:
                            await workflow.execute_activity(
                                "move_s3_object",
                                S3MoveInput(
                                    bucket=inp.bucket,
                                    source_key=fi.key,
                                    dest_key=dest_key,
                                    s3_endpoint_url=inp.s3_endpoint_url,
                                ),
                                start_to_close_timeout=timedelta(seconds=30),
                                retry_policy=_retry(),
                            )
                        except Exception as move_err:
                            workflow.logger.warning(
                                "Failed to move %s: %s", fi.key, move_err
                            )
                else:
                    self._failed += 1

                results.append({
                    "key": file_map[child_id].key,
                    "success": result.success,
                    "output_key": result.output_key,
                    "chars": result.total_chars,
                    "error": result.error,
                })
            except Exception as exc:
                self._failed += 1
                results.append({
                    "key": file_map.get(child_id, S3FileInfo(key="?")).key,
                    "success": False,
                    "error": str(exc),
                })

        self._status = "COMPLETED"
        return S3FolderWatchOutput(
            total_scanned=self._scanned,
            total_converted=self._converted,
            total_failed=self._failed,
            results=results,
        )
