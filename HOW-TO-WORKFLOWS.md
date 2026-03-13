# How to Create & Run Workflows

Complete guide to uploading documents, triggering workflows, and getting results.

---

## Prerequisites

```bash
# Start the full stack (9 services)
docker compose up --build -d

# Wait ~60 seconds for all services to be healthy
docker compose ps

# Verify the API is running
curl http://localhost:8080/health
```

**UIs available:**
- API Test Console: http://localhost:8080
- Temporal Web UI:  http://localhost:8088
- RabbitMQ UI:      http://localhost:15672 (docconv/docconv)

---

## Method 1: Upload Files via REST API (Easiest)

Upload a file directly to the API. The file is converted and the text is stored in S3.
When Temporal is enabled, this also appears as a workflow in the Temporal dashboard
(the fetch step is skipped since the file is already on the server).

```bash
# Upload a PDF
curl -X POST http://localhost:8080/convert/upload \
  -F "file=@my-report.pdf" \
  -F "document_type=pdf"

# Upload a Word document
curl -X POST http://localhost:8080/convert/upload \
  -F "file=@document.docx" \
  -F "document_type=docx"

# Upload an image for OCR
curl -X POST http://localhost:8080/convert/upload \
  -F "file=@scan.png" \
  -F "document_type=image"

# Upload with custom output key
curl -X POST http://localhost:8080/convert/upload \
  -F "file=@report.pdf" \
  -F "document_type=pdf" \
  -F "output_s3_key=reports/2025/march.txt"
```

**Response:**
```json
{
  "job_id": "a1b2c3d4e5f6",
  "success": true,
  "output_bucket": "docconv-output",
  "output_key": "converted/a1b2c3d4e5f6.txt",
  "characters_extracted": 1256
}
```

**Download the result:**
```bash
aws --endpoint-url=http://localhost:4566 s3 cp \
  s3://docconv-output/converted/a1b2c3d4e5f6.txt -
```

---

## Method 2: Upload to S3, Then Convert via Workflow

This is the production pattern. Upload files to the S3 input bucket, then
trigger a Temporal workflow to convert them with full durability and retries.

### Step 1: Upload to S3

```bash
# Upload a PDF to the input bucket
aws --endpoint-url=http://localhost:4566 s3 cp \
  my-report.pdf s3://docconv-input/docs/report.pdf

# Upload multiple files
aws --endpoint-url=http://localhost:4566 s3 cp \
  ./documents/ s3://docconv-input/docs/ --recursive

# Verify
aws --endpoint-url=http://localhost:4566 s3 ls s3://docconv-input/docs/
```

### Step 2: Start a Conversion Workflow

```bash
# Basic conversion (S3 → text → S3)
curl -X POST http://localhost:8080/convert/job \
  -H "Content-Type: application/json" \
  -d '{
    "job_id": "my-pdf-001",
    "document_type": "pdf",
    "location_type": "s3",
    "s3_bucket": "docconv-input",
    "s3_key": "docs/report.pdf",
    "s3_endpoint_url": "http://localstack:4566"
  }'
```

### Step 3: Check Status (for Temporal workflows)

```bash
# Query workflow status
curl http://localhost:8080/workflow/docconv-my-pdf-001/status

# Response shows current step:
# {"status": "RUNNING", "custom_status": "CONVERTING_PDF", "current_step": "CONVERTING"}
```

### Step 4: Get the Result

```bash
# Download converted text
aws --endpoint-url=http://localhost:4566 s3 cp \
  s3://docconv-output/converted/my-pdf-001.txt -

# List all converted files
aws --endpoint-url=http://localhost:4566 s3 ls \
  s3://docconv-output/converted/ --recursive
```

---

## Method 3: Pipeline Workflow (Validate + Convert + Analyze + Metadata)

The most comprehensive workflow. Validates the file, converts it, analyzes the
text (word count, language, structure), generates a JSON metadata file, and
optionally sends a webhook.

### Start the Pipeline

```bash
curl -X POST http://localhost:8080/workflow/pipeline \
  -H "Content-Type: application/json" \
  -d '{
    "job_id": "pipeline-001",
    "document_type": "pdf",
    "location_type": "s3",
    "s3_bucket": "docconv-input",
    "s3_key": "docs/report.pdf",
    "s3_endpoint_url": "http://localstack:4566",
    "output_prefix": "pipeline/",
    "enable_validation": true,
    "enable_analytics": true,
    "enable_metadata_output": true
  }'
```

### Check Progress

```bash
curl http://localhost:8080/workflow/pipeline-pipeline-001/status
# {"status": "ANALYZING", "step": "ANALYZING"}
```

### View Outputs

```bash
# The pipeline produces two files:
aws --endpoint-url=http://localhost:4566 s3 ls \
  s3://docconv-output/pipeline/pipeline-001/

# pipeline/pipeline-001/text.txt       ← converted text
# pipeline/pipeline-001/metadata.json  ← analytics + job info

# View the metadata
aws --endpoint-url=http://localhost:4566 s3 cp \
  s3://docconv-output/pipeline/pipeline-001/metadata.json -
```

**Metadata JSON contains:**
```json
{
  "job_id": "pipeline-001",
  "document_type": "pdf",
  "generated_at": "2025-03-13T...",
  "text_output_key": "pipeline/pipeline-001/text.txt",
  "analytics": {
    "total_chars": 1256,
    "total_words": 203,
    "total_lines": 42,
    "total_pages": 2,
    "language_hint": "en",
    "has_tables": false,
    "has_images": false,
    "top_words": ["document", "conversion", "service", ...]
  }
}
```

---

## Method 4: S3 Folder Watch (Auto-Convert Entire Folders)

Drop files into an S3 "inbox/" prefix. The workflow scans, converts all files
in parallel, and moves originals to "processed/".

### Upload Files to Inbox

```bash
aws --endpoint-url=http://localhost:4566 s3 cp report.pdf  s3://docconv-input/inbox/
aws --endpoint-url=http://localhost:4566 s3 cp data.xlsx   s3://docconv-input/inbox/
aws --endpoint-url=http://localhost:4566 s3 cp scan.png    s3://docconv-input/inbox/
```

### Start the Watch (One-Shot)

```bash
curl -X POST http://localhost:8080/workflow/s3-watch \
  -H "Content-Type: application/json" \
  -d '{
    "bucket": "docconv-input",
    "prefix": "inbox/",
    "output_prefix": "converted/",
    "s3_endpoint_url": "http://localstack:4566",
    "move_processed_to": "processed/"
  }'
```

### Start with Hourly Cron Schedule

```bash
curl -X POST http://localhost:8080/workflow/s3-watch \
  -H "Content-Type: application/json" \
  -d '{
    "bucket": "docconv-input",
    "prefix": "inbox/",
    "s3_endpoint_url": "http://localstack:4566",
    "move_processed_to": "processed/",
    "workflow_id": "s3watch-hourly",
    "cron_schedule": "0 * * * *"
  }'
```

### After Processing

```bash
# Originals moved from inbox/ to processed/
aws --endpoint-url=http://localhost:4566 s3 ls s3://docconv-input/inbox/
# (empty)
aws --endpoint-url=http://localhost:4566 s3 ls s3://docconv-input/processed/
# report.pdf, data.xlsx, scan.png

# Converted text in output bucket
aws --endpoint-url=http://localhost:4566 s3 ls s3://docconv-output/converted/
# report.pdf.txt, data.xlsx.txt, scan.png.txt
```

---

## Method 5: Multi-Format Output (Text + Pages + Metadata)

Produces multiple artifacts from a single document:

```bash
curl -X POST http://localhost:8080/workflow/multi-format \
  -H "Content-Type: application/json" \
  -d '{
    "job_id": "multi-001",
    "document_type": "pdf",
    "location_type": "s3",
    "s3_bucket": "docconv-input",
    "s3_key": "docs/report.pdf",
    "s3_endpoint_url": "http://localstack:4566",
    "output_prefix": "multi/",
    "produce_full_text": true,
    "produce_pages": true,
    "produce_metadata_json": true,
    "produce_analytics": true
  }'
```

**Outputs:**
```
s3://docconv-output/multi/multi-001/full_text.txt      ← complete text
s3://docconv-output/multi/multi-001/pages/page_0001.txt ← page 1
s3://docconv-output/multi/multi-001/pages/page_0002.txt ← page 2
s3://docconv-output/multi/multi-001/metadata.json       ← analytics
```

---

## Method 6: Webhook Notifications

Get notified when conversion starts, succeeds, or fails:

```bash
curl -X POST http://localhost:8080/workflow/webhook-convert \
  -H "Content-Type: application/json" \
  -d '{
    "job_id": "webhook-001",
    "document_type": "pdf",
    "location_type": "s3",
    "s3_bucket": "docconv-input",
    "s3_key": "docs/report.pdf",
    "s3_endpoint_url": "http://localstack:4566",
    "on_start_webhook": "https://your-app.com/hooks/docconv",
    "on_complete_webhook": "https://your-app.com/hooks/docconv",
    "on_failure_webhook": "https://your-app.com/hooks/docconv",
    "webhook_auth_token": "your-bearer-token"
  }'
```

**Webhook payloads sent:**
```json
// on_start:
{"event": "conversion.started", "job_id": "webhook-001", "document_type": "pdf"}

// on_complete:
{"event": "conversion.completed", "job_id": "webhook-001",
 "output_bucket": "docconv-output", "output_key": "converted/webhook-001.txt",
 "total_chars": 1256}

// on_failure:
{"event": "conversion.failed", "job_id": "webhook-001", "error": "..."}
```

---

## Method 7: Retry Escalation (Low-Quality Scan Recovery)

For scanned PDFs where standard OCR might fail:

```bash
curl -X POST http://localhost:8080/workflow/retry-escalation \
  -H "Content-Type: application/json" \
  -d '{
    "job_id": "escalate-001",
    "document_type": "pdf",
    "location_type": "s3",
    "s3_bucket": "docconv-input",
    "s3_key": "docs/blurry-scan.pdf",
    "s3_endpoint_url": "http://localstack:4566",
    "min_chars_threshold": 100,
    "escalation_dpi": 400
  }'
```

**What happens:**
1. Standard conversion at 200 DPI → extracts 30 chars (below threshold of 100)
2. Escalates to enhanced OCR at 400 DPI with different Tesseract settings
3. Enhanced produces 450 chars → uses this better result
4. Uploads the best result to S3

---

## Method 8: Scheduled Maintenance

Run cleanup periodically:

```bash
# One-shot cleanup
curl -X POST http://localhost:8080/workflow/maintenance \
  -H "Content-Type: application/json" \
  -d '{"tmp_dir": "/tmp/docconv", "max_age_hours": 24}'

# Daily cron at 3 AM
curl -X POST http://localhost:8080/workflow/maintenance \
  -H "Content-Type: application/json" \
  -d '{
    "tmp_dir": "/tmp/docconv",
    "max_age_hours": 24,
    "workflow_id": "maintenance-daily",
    "cron_schedule": "0 3 * * *"
  }'
```

---

## Method 9: Send via Message Buses

Documents uploaded to S3 can be triggered from any message bus:

### SQS
```bash
aws --endpoint-url=http://localhost:4566 sqs send-message \
  --queue-url http://localhost:4566/000000000000/docconv-jobs \
  --message-body '{
    "document_type": "pdf",
    "location_type": "s3",
    "s3_bucket": "docconv-input",
    "s3_key": "docs/report.pdf",
    "s3_endpoint_url": "http://localstack:4566"
  }'
```

### RabbitMQ (Python)
```python
import pika, json
creds = pika.PlainCredentials("docconv", "docconv")
conn = pika.BlockingConnection(
    pika.ConnectionParameters("localhost", 5672, credentials=creds))
ch = conn.channel()
ch.basic_publish(exchange="", routing_key="docconv-jobs",
    body=json.dumps({
        "document_type": "pdf", "location_type": "s3",
        "s3_bucket": "docconv-input", "s3_key": "docs/report.pdf",
        "s3_endpoint_url": "http://localstack:4566"
    }))
conn.close()
```

### Kafka (Python)
```python
from confluent_kafka import Producer
import json
p = Producer({"bootstrap.servers": "localhost:9092"})
p.produce("docconv-jobs", value=json.dumps({
    "document_type": "pdf", "location_type": "s3",
    "s3_bucket": "docconv-input", "s3_key": "docs/report.pdf",
    "s3_endpoint_url": "http://localstack:4566"
}).encode())
p.flush()
```

---

## Method 10: Run the Full Demo Script

The automated demo script uploads sample files, runs every workflow, and shows results:

```bash
pip install boto3 requests reportlab python-docx openpyxl python-pptx Pillow

# Run all demos
python scripts/demo_workflows.py

# Run a specific demo
python scripts/demo_workflows.py --only pipeline
python scripts/demo_workflows.py --only s3watch
python scripts/demo_workflows.py --only multi

# List available demos
python scripts/demo_workflows.py --list
```

---

## Monitoring Workflows

### Temporal Web UI (http://localhost:8088)

- Click any workflow to see its full execution history
- Each activity shows input, output, duration, and retry count
- Use the Query tab to run `get_status` or `pipeline_status` on running workflows
- Use the Signal tab to send `cancel` to stop a workflow

### REST API

```bash
# List recent workflows
curl http://localhost:8080/workflows/recent?limit=20

# Query a specific workflow
curl http://localhost:8080/workflow/docconv-my-pdf-001/status

# Cancel a running workflow
curl -X POST http://localhost:8080/workflow/docconv-my-pdf-001/cancel
```

### Supported Document Types

| Type | Extensions | Converter |
|------|-----------|-----------|
| `pdf` | .pdf | Text extraction + embedded image OCR |
| `docx` | .docx, .doc | Paragraphs + tables |
| `xlsx` | .xlsx, .xls | Sheets with row/column data |
| `csv` | .csv | Comma-separated values |
| `pptx` | .pptx, .ppt | Slides + speaker notes |
| `html` | .html, .htm | Tag stripping |
| `rtf` | .rtf | RTF control word stripping |
| `odt` | .odt | OpenDocument text |
| `txt` | .txt | Encoding detection + passthrough |
| `image` | .jpg, .png, .tiff, .bmp, .webp | Tesseract OCR |
