"""
ScheduledMaintenanceWorkflow – periodic housekeeping.

Performs maintenance tasks on a cron schedule:
  1. Purge temp files older than N hours
  2. Verify S3 output bucket is reachable
  3. Report summary stats

Start with cron scheduling:
    await client.start_workflow(
        "ScheduledMaintenanceWorkflow", input,
        id="maintenance-daily",
        task_queue="docconv-tasks",
        cron_schedule="0 3 * * *",   # 3 AM daily
    )
"""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from config.settings import settings
    from app.workflows.dataclasses_ext import (
        ScheduledCleanupInput, ScheduledCleanupOutput,
    )


@workflow.defn(name="ScheduledMaintenanceWorkflow")
class ScheduledMaintenanceWorkflow:
    """
    Periodic maintenance: cleanup old files and verify infrastructure health.
    """

    def __init__(self):
        self._last_result = {}

    @workflow.query(name="maintenance_last_result")
    def get_last_result(self) -> dict:
        return self._last_result

    @workflow.run
    async def run(self, inp: ScheduledCleanupInput) -> ScheduledCleanupOutput:
        workflow.logger.info("Maintenance starting  tmp=%s  max_age=%dh",
                             inp.tmp_dir, inp.max_age_hours)

        # ── 1. Purge old temp files ──────────────────────────────────────
        cleanup_result: ScheduledCleanupOutput = await workflow.execute_activity(
            "scheduled_tmp_cleanup",
            inp,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=_retry(),
        )

        workflow.logger.info(
            "Cleanup: %d files deleted, %d bytes freed",
            cleanup_result.files_deleted, cleanup_result.bytes_freed,
        )

        # ── 2. Check S3 health ───────────────────────────────────────────
        bucket = inp.s3_output_bucket or settings.s3_output_bucket

        s3_ok: bool = await workflow.execute_activity(
            "check_s3_health",
            args=[bucket],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_retry(),
        )

        cleanup_result.s3_health_ok = s3_ok
        if not s3_ok:
            cleanup_result.errors.append(f"S3 bucket {bucket} health check failed")

        self._last_result = {
            "files_deleted": cleanup_result.files_deleted,
            "bytes_freed": cleanup_result.bytes_freed,
            "s3_health_ok": s3_ok,
            "errors": cleanup_result.errors,
        }

        workflow.logger.info("Maintenance complete  s3_ok=%s", s3_ok)
        return cleanup_result


def _retry():
    from temporalio.common import RetryPolicy
    return RetryPolicy(
        initial_interval=timedelta(seconds=5),
        maximum_attempts=2,
    )
