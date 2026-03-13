# Document Conversion Service

A production-style, Docker-based microservice that converts documents and images
to plain text.  It listens on **three message buses** (SQS, RabbitMQ, Kafka),
executes jobs as **Temporal.io durable workflows** with automatic retries and
crash recovery, and exposes an optional **REST API** – all controlled by
environment variables.

---

## Architecture

```
┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌────────────────┐
│    SQS    │ │ RabbitMQ  │ │   Kafka   │ │ REST API  │ │  Temporal.io   │
│  Queue    │ │  Queue    │ │  Topic    │ │ (FastAPI) │ │  Client SDK    │
└─────┬─────┘ └─────┬─────┘ └─────┬─────┘ └─────┬─────┘ └───────┬────────┘
      │             │             │             │               │
      └─────────────┴──────┬──────┴─────────────┘               │
                           ▼                                    ▼
           ┌──────────────────────────────────────────────────────────┐
           │              Processor (routing layer)                    │
           │  USE_TEMPORAL_WORKFLOWS=true  → Temporal Workflow         │
           │  USE_TEMPORAL_WORKFLOWS=false → Direct Pipeline           │
           └───────────────────────┬──────────────────────────────────┘
                                   ▼
          ┌─────────────────────────────────────────────────────┐
          │        DocumentConversionWorkflow (Temporal)         │
          │  Activity 1: fetch_document  (S3/URL/FTP → tmp)     │
          │  Child WF:   Convert         (per-type workflow)    │
          │  Activity 3: upload_text     (tmp → S3 bucket)      │
          │  Activity 4: cleanup         (remove tmp files)     │
          └─────────────────────────────────────────────────────┘
```

### Docker Services (9 containers)

| Service | Image | Ports | Purpose |
|---------|-------|-------|---------|
| localstack | `localstack/localstack:3.4` | 4566 | SQS queues + S3 buckets |
| rabbitmq | `rabbitmq:3.13-management-alpine` | 5672, 15672 (UI) | RabbitMQ message bus |
| kafka | `confluentinc/cp-kafka:7.6.1` | 9092 | Kafka message bus (KRaft, no ZooKeeper) |
| temporal-postgresql | `postgres:16-alpine` | — | Temporal persistence DB |
| temporal-server | `temporalio/auto-setup:latest` | 7233 | Temporal gRPC frontend |
| temporal-admin-tools | `temporalio/admin-tools:latest` | — | Temporal CLI tools |
| temporal-ui | `temporalio/ui:latest` | **8088** | Temporal Web UI |
| ftp | `fauria/vsftpd` | 21, 21100-21110 | Demo FTP server |
| docconv-app | *Built from Dockerfile* | **8080** | The conversion service |

---

## Quick Start

```bash
# 1. Enter the project
cd docconv-service

# 2. Make the init script executable
chmod +x scripts/init-localstack.sh

# 3. Build and start everything (9 services)
docker compose up --build -d

# 4. Open the API Test Console
open http://localhost:8080

# 5. Open the Temporal Web UI
open http://localhost:8088

# 6. Watch the logs
docker compose logs -f docconv-app

# 7. Run the demo (from host – needs pip packages)
pip install boto3 pika confluent-kafka requests
python scripts/demo.py
```

> **Note:** Temporal may take 30-40 seconds to start on first boot (database schema setup).
> The app waits for it to be healthy before starting.

---

## Configuration (Environment Variables)

### Feature Flags

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_API` | `true` | Start the FastAPI HTTP server |
| `ENABLE_SQS` | `true` | Start the SQS listener thread |
| `ENABLE_RABBITMQ` | `true` | Start the RabbitMQ listener |
| `ENABLE_KAFKA` | `true` | Start the Kafka listener |
| `ENABLE_TEMPORAL` | `true` | Start the Temporal worker thread |
| `USE_TEMPORAL_WORKFLOWS` | `true` | Route all jobs through Temporal (vs direct pipeline) |

Set any flag to `"false"` to disable that component at startup.

### Temporal Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `TEMPORAL_HOST` | `temporal-server:7233` | Temporal gRPC endpoint |
| `TEMPORAL_NAMESPACE` | `default` | Temporal namespace |
| `TEMPORAL_TASK_QUEUE` | `docconv-tasks` | Worker task queue name |
| `TEMPORAL_WORKFLOW_TIMEOUT` | `3600` | Max workflow duration (seconds) |
| `TEMPORAL_ACTIVITY_TIMEOUT` | `600` | Max single activity duration (seconds) |
| `TEMPORAL_RETRY_MAX_ATTEMPTS` | `3` | Retry attempts per activity on failure |

### Connection Settings

See `.env.example` for the full list of SQS, S3, RabbitMQ, Kafka, AWS, and
tuning variables.

---

## Temporal.io Durable Workflows

When `USE_TEMPORAL_WORKFLOWS=true` (the default), every conversion job – whether
submitted via SQS, RabbitMQ, Kafka, or the REST API – is executed as a Temporal
workflow.  This gives you:

- **Durable execution** – workflows survive worker crashes and restarts
- **Automatic retries** – each activity retries with exponential backoff (5s → 10s → 20s…)
- **Queryable state** – check status mid-execution via API or Temporal UI
- **Cancellation signals** – gracefully stop running workflows
- **Full audit trail** – every activity attempt recorded in the Temporal UI
- **Batch processing** – convert multiple documents in parallel

### Workflow Pipeline

```
DocumentConversionWorkflow (top-level, durable)
  │
  ├─ Activity: fetch_document     → downloads source to tmp file
  │
  ├─ Child Workflow: {Type}ConversionWorkflow
  │    └─ Activity: convert_{type} → converts to text
  │
  ├─ Activity: upload_text        → uploads text to S3
  │
  └─ Activity: cleanup_temp_files → removes tmp files
```

### Per-Document-Type Child Workflows

Each document type has its own child workflow for visibility and type-specific tuning:

| Document Type | Child Workflow | Activity |
|---------------|----------------|----------|
| PDF | `PDFConversionWorkflow` | `convert_pdf` |
| DOCX | `DOCXConversionWorkflow` | `convert_docx` |
| XLSX / CSV | `XLSXConversionWorkflow` | `convert_xlsx` |
| PPTX | `PPTXConversionWorkflow` | `convert_pptx` |
| HTML | `HTMLConversionWorkflow` | `convert_html` |
| RTF | `RTFConversionWorkflow` | `convert_rtf` |
| ODT | `ODTConversionWorkflow` | `convert_odt` |
| TXT | `TXTConversionWorkflow` | `convert_txt` |
| Image (OCR) | `ImageConversionWorkflow` | `convert_image` |

### Execution Modes

| Setting | Behavior |
|---------|----------|
| `USE_TEMPORAL_WORKFLOWS=true` | All jobs route through Temporal. Full durability. |
| `USE_TEMPORAL_WORKFLOWS=false` | Direct inline pipeline. Faster for dev/testing. |
| `ENABLE_TEMPORAL=false` | Worker doesn't start. All jobs use direct pipeline. |
| Temporal server down | Automatic fallback to direct pipeline with warning log. |

### Temporal Web UI

Open **http://localhost:8088** to:
- View all workflows with filtering by status, type, and ID
- Inspect execution history for each workflow (every activity attempt)
- Run queries (`get_status`, `get_step`) on running workflows
- Send cancellation signals
- View stack traces for failed activities

### Batch Processing

Convert multiple documents in parallel:

```python
handle = await client.start_workflow(
    "BatchConversionWorkflow",
    [input1, input2, input3],
    id="batch-001",
    task_queue="docconv-tasks",
)

# Query progress
progress = await handle.query("get_batch_progress")
# {"total": 3, "completed": 2, "failed": 0}
```

### Starting Workflows from Python

```python
from app.workflows.client import start_conversion_workflow_sync
from app.models import ConversionJob

job = ConversionJob(
    job_id="my-job-001",
    document_type="pdf",
    location_type="s3",
    s3_bucket="docconv-input",
    s3_key="docs/report.pdf",
)

result = start_conversion_workflow_sync(job)
print(f"Success: {result.success}")
print(f"Output: s3://{result.output_bucket}/{result.output_key}")
```

---

## Message Schema (JSON)

Every bus, the API, and Temporal workflows accept the same JSON schema:

```json
{
  "job_id": "optional-caller-id",
  "document_type": "pdf",
  "location_type": "s3",

  "s3_bucket": "my-bucket",
  "s3_key": "docs/report.pdf",
  "s3_endpoint_url": null,

  "url": null,

  "ftp_host": null,
  "ftp_port": 21,
  "ftp_path": null,
  "ftp_user": null,
  "ftp_pass": null,

  "auth_type": "none",
  "auth_username": null,
  "auth_password": null,
  "auth_token": null,

  "output_s3_bucket": null,
  "output_s3_key": null
}
```

### Supported Values

**document_type**: `pdf`, `docx`, `xlsx`, `pptx`, `html`, `rtf`, `odt`, `txt`, `csv`, `image`

**location_type**: `s3`, `url`, `ftp`, `local` (local = API upload only)

**auth_type**: `none`, `basic`, `bearer`, `aws_sigv4`

---

## REST API

### `GET /` – API Test Console

Opens the built-in web UI for testing all endpoints.

### `GET /health`

Returns service status including Temporal configuration:

```json
{
  "status": "ok",
  "api_enabled": true,
  "sqs_enabled": true,
  "rabbitmq_enabled": true,
  "kafka_enabled": true,
  "temporal_enabled": true,
  "temporal_workflows_active": true,
  "temporal_host": "temporal-server:7233",
  "temporal_task_queue": "docconv-tasks"
}
```

### `POST /convert/job`

Submit a conversion job (body = JSON matching the schema above).
When Temporal is enabled, this starts a durable workflow.

```bash
curl -X POST http://localhost:8080/convert/job \
  -H "Content-Type: application/json" \
  -d '{
    "document_type": "html",
    "location_type": "url",
    "url": "https://example.com"
  }'
```

### `POST /convert/upload`

Upload a file directly for conversion (multipart form):

```bash
curl -X POST http://localhost:8080/convert/upload \
  -F "file=@report.pdf" \
  -F "document_type=pdf"
```

### `GET /workflow/{workflow_id}/status`

Query the current status of a Temporal workflow:

```bash
curl http://localhost:8080/workflow/docconv-abc123/status
```

```json
{
  "workflow_id": "docconv-abc123",
  "status": "RUNNING",
  "custom_status": "CONVERTING_PDF",
  "current_step": "CONVERTING",
  "task_queue": "docconv-tasks"
}
```

### `POST /workflow/{workflow_id}/cancel`

Send a cancellation signal to a running workflow:

```bash
curl -X POST http://localhost:8080/workflow/docconv-abc123/cancel
```

### `GET /workflows/recent?limit=20`

List recent workflows from Temporal:

```bash
curl http://localhost:8080/workflows/recent?limit=5
```

### `GET /docs`

FastAPI auto-generated Swagger/OpenAPI documentation.

---

## Project Structure

```
docconv-service/
├── docker-compose.yml          # Full stack (9 services)
├── Dockerfile                  # App container image
├── requirements.txt            # Python dependencies
├── .env.example                # All configurable env vars
│
├── config/
│   └── settings.py             # Pydantic settings (single source of truth)
│
├── app/
│   ├── main.py                 # ★ Entry point – enables/disables components
│   ├── models.py               # Pydantic models (ConversionJob, Result)
│   ├── api.py                  # FastAPI REST + workflow status endpoints
│   ├── processor.py            # Routing: Temporal workflow or direct pipeline
│   ├── storage.py              # S3 output uploader
│   ├── static/index.html       # API Test Console (HTML UI)
│   │
│   ├── bus/                    # Message bus listeners
│   │   ├── sqs_listener.py
│   │   ├── rabbitmq_listener.py
│   │   └── kafka_listener.py
│   │
│   ├── fetchers/               # Download from remote sources
│   │   ├── dispatch.py         # Routes by location_type
│   │   ├── s3_fetcher.py
│   │   ├── url_fetcher.py
│   │   └── ftp_fetcher.py
│   │
│   ├── converters/             # One module per document type
│   │   ├── dispatch.py         # Routes by document_type
│   │   ├── pdf_converter.py    # PDF text + embedded image OCR
│   │   ├── docx_converter.py   # Word documents
│   │   ├── xlsx_converter.py   # Excel / CSV spreadsheets
│   │   ├── pptx_converter.py   # PowerPoint presentations
│   │   ├── html_converter.py   # HTML pages
│   │   ├── rtf_converter.py    # Rich Text Format
│   │   ├── odt_converter.py    # OpenDocument Text
│   │   ├── text_converter.py   # Plain text with encoding detection
│   │   └── image_converter.py  # Image OCR (JPEG/PNG/TIFF/BMP/WEBP)
│   │
│   └── workflows/              # ★ Temporal.io integration
│       ├── dataclasses.py      # Serializable I/O types for activities
│       ├── activities.py       # 12 Temporal activities (fetch, convert, upload, cleanup)
│       ├── document_workflows.py # 9 per-type child workflows
│       ├── conversion_workflow.py # Main workflow + BatchConversionWorkflow
│       ├── worker.py           # Temporal worker (runs in daemon thread)
│       └── client.py           # Start workflows from Python code
│
├── scripts/
│   ├── init-localstack.sh      # Creates SQS queue + S3 buckets
│   ├── demo.py                 # Send test jobs via every channel
│   └── generate_user_guide.py  # Build the DOCX user guide
│
└── tests/
    └── test_full_flow.py       # 48 integration tests
```

---

## Memory Efficiency

The service is designed for low memory usage on large documents:

1. **Fetchers** stream remote files to disk in chunks (`CHUNK_SIZE`, default 64 KB)
   using Python generators (`yield`).  The file is written to `/tmp/docconv` and
   auto-deleted after processing.

2. **Converters** yield text one page/chunk at a time — they never build the full
   text string in memory.

3. **The upload step** writes yielded chunks to a tmp file, then uploads that file
   to S3 — again avoiding holding the complete text in RAM.

4. **PDF image extraction** renders pages to tmp PNG files for OCR, deletes each
   one immediately after processing.

5. **Temporal activities** use the same generator-based converters.  Files persist
   across activity boundaries via durable temp paths that survive generator cleanup.

---

## Testing

Run the full integration test suite (48 tests):

```bash
cd docconv-service
pip install -r requirements.txt reportlab httpx temporalio
python -m tests.test_full_flow
```

| Section | Tests | Description |
|---------|-------|-------------|
| Converters | 10 | Each converter module independently |
| Dispatcher | 10 | DocumentType → converter routing |
| Pipeline | 10 | End-to-end: file → convert → output |
| Models | 4 | Pydantic schema validation |
| REST API | 7 | Health, upload (5 formats), validation |
| Temporal | 7 | Dataclasses, registries, input/output mapping, fallback |

---

## Disabling Components

```yaml
# Run as a pure Temporal worker (no API, no buses)
docconv-app:
  environment:
    ENABLE_API: "false"
    ENABLE_SQS: "false"
    ENABLE_RABBITMQ: "false"
    ENABLE_KAFKA: "false"
    ENABLE_TEMPORAL: "true"

# Run without Temporal (direct pipeline only)
docconv-app:
  environment:
    ENABLE_TEMPORAL: "false"
    USE_TEMPORAL_WORKFLOWS: "false"

# Run as API-only (no buses, no Temporal)
docconv-app:
  environment:
    ENABLE_API: "true"
    ENABLE_SQS: "false"
    ENABLE_RABBITMQ: "false"
    ENABLE_KAFKA: "false"
    ENABLE_TEMPORAL: "false"
    USE_TEMPORAL_WORKFLOWS: "false"
```

---

## Checking Output

Converted text lands in the `docconv-output` S3 bucket (LocalStack):

```bash
# List objects
aws --endpoint-url=http://localhost:4566 s3 ls s3://docconv-output/converted/ --recursive

# Download and view
aws --endpoint-url=http://localhost:4566 s3 cp s3://docconv-output/converted/demo-url-001.txt -
```

You can also monitor workflow execution in the Temporal UI at **http://localhost:8088**.
