"""
WebhookNotificationWorkflow – conversion with webhook lifecycle events.

Wraps a standard conversion with HTTP callback notifications:
  - on_start:    POST when the workflow begins
  - on_complete: POST with output details when done
  - on_failure:  POST with error details on failure

Use case: Integrate with external systems (Slack, n8n, Zapier, custom apps)
that need to know when document processing starts, succeeds, or fails.
"""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from config.settings import settings
    from app.workflows.dataclasses import ConversionWorkflowInput, ConversionWorkflowOutput
    from app.workflows.dataclasses_ext import (
        WebhookNotificationWorkflowInput,
        WebhookInput,
    )


def _retry():
    from temporalio.common import RetryPolicy
    return RetryPolicy(
        initial_interval=timedelta(seconds=3),
        maximum_attempts=3,
    )


@workflow.defn(name="WebhookNotificationWorkflow")
class WebhookNotificationWorkflow:
    """
    Conversion + lifecycle webhook notifications.
    """

    def __init__(self):
        self._status = "INITIALIZED"

    @workflow.query(name="webhook_wf_status")
    def get_status(self) -> str:
        return self._status

    @workflow.run
    async def run(self, inp: WebhookNotificationWorkflowInput) -> ConversionWorkflowOutput:
        self._status = "STARTING"
        workflow.logger.info("WebhookNotification started  job=%s", inp.job_id)

        # ── on_start webhook ─────────────────────────────────────────────
        if inp.on_start_webhook:
            await self._send_webhook(inp.on_start_webhook, inp.webhook_auth_token, {
                "event": "conversion.started",
                "job_id": inp.job_id,
                "document_type": inp.document_type,
                "location_type": inp.location_type,
            })

        # ── Run the actual conversion as a child workflow ────────────────
        self._status = "CONVERTING"
        conversion_input = ConversionWorkflowInput(
            job_id=inp.job_id,
            document_type=inp.document_type,
            location_type=inp.location_type,
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
            output_s3_bucket=inp.output_s3_bucket,
            output_s3_key=inp.output_s3_key,
            local_file_path=inp.local_file_path,
        )

        try:
            result: ConversionWorkflowOutput = await workflow.execute_child_workflow(
                "DocumentConversionWorkflow",
                conversion_input,
                id=f"webhook-conv-{inp.job_id}",
                task_queue=settings.temporal_task_queue,
            )
        except Exception as exc:
            self._status = "FAILED"

            # ── on_failure webhook ───────────────────────────────────────
            if inp.on_failure_webhook:
                await self._send_webhook(inp.on_failure_webhook, inp.webhook_auth_token, {
                    "event": "conversion.failed",
                    "job_id": inp.job_id,
                    "error": str(exc),
                })

            return ConversionWorkflowOutput(
                job_id=inp.job_id, success=False, error=str(exc),
            )

        # ── on_complete or on_failure webhook ────────────────────────────
        if result.success and inp.on_complete_webhook:
            self._status = "NOTIFYING"
            await self._send_webhook(inp.on_complete_webhook, inp.webhook_auth_token, {
                "event": "conversion.completed",
                "job_id": inp.job_id,
                "output_bucket": result.output_bucket,
                "output_key": result.output_key,
                "total_chars": result.total_chars,
                "pages_processed": result.pages_processed,
            })
        elif not result.success and inp.on_failure_webhook:
            self._status = "NOTIFYING"
            await self._send_webhook(inp.on_failure_webhook, inp.webhook_auth_token, {
                "event": "conversion.failed",
                "job_id": inp.job_id,
                "error": result.error,
            })

        self._status = "COMPLETED" if result.success else "FAILED"
        return result

    async def _send_webhook(self, url: str, token: str | None, payload: dict):
        """Send a webhook, swallowing errors so they don't kill the workflow."""
        try:
            await workflow.execute_activity(
                "send_webhook",
                WebhookInput(url=url, payload=payload, auth_token=token),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_retry(),
            )
        except Exception as exc:
            workflow.logger.warning("Webhook to %s failed: %s", url, exc)
