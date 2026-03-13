# Temporal Workflow Definitions

JSON input files for starting each document conversion workflow via the
Temporal CLI, REST API, or Python SDK.

## Quick Start

```bash
# Start the full stack
docker compose up -d

# Option A: Start workflows via REST API (from host)
bash temporal-workflows/run-workflows.sh --api

# Option B: Start workflows via Temporal CLI (from admin container)
docker compose exec temporal-admin-tools bash
temporal workflow start \
  --address temporal-server:7233 \
  --task-queue docconv-tasks \
  --type DocumentPipelineWorkflow \
  --workflow-id pipeline-test-001 \
  --input "$(cat /workflows/04-pipeline.json)"

# Option C: Start a single workflow type via API
bash temporal-workflows/run-workflows.sh --api --only pipeline

# View results in Temporal UI
open http://localhost:8088
```

## Workflow Files

| # | File | Workflow | Description |
|---|------|----------|-------------|
| 01 | `01-basic-conversion.json` | `DocumentConversionWorkflow` | URL → text (simplest case) |
| 02 | `02-s3-conversion.json` | `DocumentConversionWorkflow` | S3 object → text |
| 03 | `03-batch-conversion.json` | `BatchConversionWorkflow` | 3 documents in parallel |
| 04 | `04-pipeline.json` | `DocumentPipelineWorkflow` | Validate → convert → analyze → metadata → upload |
| 05 | `05-s3-folder-watch.json` | `S3FolderWatchWorkflow` | Scan inbox/ prefix, convert all, move to processed/ |
| 06 | `06-webhook-notification.json` | `WebhookNotificationWorkflow` | Conversion + start/complete/failure webhooks |
| 07 | `07-multi-format-output.json` | `MultiFormatOutputWorkflow` | Full text + per-page splits + metadata JSON + analytics |
| 08 | `08-retry-escalation.json` | `RetryEscalationWorkflow` | Standard OCR → escalate to 400 DPI if sparse output |
| 09 | `09-maintenance.json` | `ScheduledMaintenanceWorkflow` | Purge old tmp files + S3 health check |

## Starting via REST API (curl)

```bash
# Basic conversion
curl -X POST http://localhost:8080/convert/job \
  -H "Content-Type: application/json" \
  -d @temporal-workflows/01-basic-conversion.json

# Pipeline workflow
curl -X POST http://localhost:8080/workflow/pipeline \
  -H "Content-Type: application/json" \
  -d @temporal-workflows/04-pipeline.json

# S3 folder watch
curl -X POST http://localhost:8080/workflow/s3-watch \
  -H "Content-Type: application/json" \
  -d @temporal-workflows/05-s3-folder-watch.json

# S3 folder watch with hourly cron schedule
curl -X POST http://localhost:8080/workflow/s3-watch \
  -H "Content-Type: application/json" \
  -d '{"bucket":"docconv-input","prefix":"inbox/",
       "s3_endpoint_url":"http://localstack:4566",
       "cron_schedule":"0 * * * *"}'

# Webhook notification conversion
curl -X POST http://localhost:8080/workflow/webhook-convert \
  -H "Content-Type: application/json" \
  -d @temporal-workflows/06-webhook-notification.json

# Multi-format output
curl -X POST http://localhost:8080/workflow/multi-format \
  -H "Content-Type: application/json" \
  -d @temporal-workflows/07-multi-format-output.json

# Retry escalation
curl -X POST http://localhost:8080/workflow/retry-escalation \
  -H "Content-Type: application/json" \
  -d @temporal-workflows/08-retry-escalation.json

# Scheduled maintenance (one-shot)
curl -X POST http://localhost:8080/workflow/maintenance \
  -H "Content-Type: application/json" \
  -d @temporal-workflows/09-maintenance.json

# Scheduled maintenance (daily cron)
curl -X POST http://localhost:8080/workflow/maintenance \
  -H "Content-Type: application/json" \
  -d '{"tmp_dir":"/tmp/docconv","max_age_hours":24,
       "cron_schedule":"0 3 * * *"}'
```

## Starting via Temporal CLI

```bash
# From inside the temporal-admin-tools container:
docker compose exec temporal-admin-tools bash

# Basic conversion
temporal workflow start \
  --address temporal-server:7233 \
  --task-queue docconv-tasks \
  --type DocumentConversionWorkflow \
  --workflow-id docconv-test-001 \
  --input '{"job_id":"test-001","document_type":"html","location_type":"url","url":"https://example.com"}'

# Pipeline
temporal workflow start \
  --address temporal-server:7233 \
  --task-queue docconv-tasks \
  --type DocumentPipelineWorkflow \
  --workflow-id pipeline-test-001 \
  --input '{"job_id":"pipe-001","document_type":"html","location_type":"url","url":"https://example.com","enable_analytics":true}'

# Hourly S3 watch (cron)
temporal workflow start \
  --address temporal-server:7233 \
  --task-queue docconv-tasks \
  --type S3FolderWatchWorkflow \
  --workflow-id s3watch-hourly \
  --cron-schedule "0 * * * *" \
  --input '{"bucket":"docconv-input","prefix":"inbox/","s3_endpoint_url":"http://localstack:4566"}'

# Query a workflow
temporal workflow query \
  --address temporal-server:7233 \
  --workflow-id pipeline-test-001 \
  --name pipeline_status

# Cancel a workflow
temporal workflow signal \
  --address temporal-server:7233 \
  --workflow-id pipeline-test-001 \
  --name cancel

# List recent workflows
temporal workflow list \
  --address temporal-server:7233 \
  --query 'TaskQueue="docconv-tasks"'
```

## Mounting into the Admin Container

To access these JSON files from inside the admin container, add a volume
mount to docker-compose.yml:

```yaml
temporal-admin-tools:
  volumes:
    - "./temporal-workflows:/workflows:ro"
```

Then: `docker compose exec temporal-admin-tools bash`
and files are at `/workflows/*.json`.
