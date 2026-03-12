# Document Conversion Service

A production-style, Docker-based microservice that converts documents and images
to plain text.  It listens on **three message buses** (SQS, RabbitMQ, Kafka) and
exposes an optional **REST API** – all controlled by environment variables.

---

## Architecture

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│   SQS Queue  │  │  RabbitMQ Q  │  │  Kafka Topic │  │   REST API   │
│ (LocalStack) │  │              │  │              │  │  (FastAPI)   │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │                 │
       └────────────────┬┘─────────────────┘                 │
                        ▼                                    ▼
              ┌─────────────────────────────────────────────────┐
              │              Conversion Job Router               │
              │  (validates message → selects fetcher)           │
              └──────────────────┬──────────────────────────────┘
                                 ▼
              ┌──────────────────────────────────────┐
              │          Document Fetcher             │
              │  S3  │  HTTP/URL  │  FTP  │  Local   │
              └──────────────────┬───────────────────┘
                                 ▼  (tmp file on disk)
              ┌──────────────────────────────────────┐
              │        Converter Dispatcher           │
              │  PDF │ DOCX │ XLSX │ PPTX │ HTML │…  │
              │  RTF │ ODT  │ TXT  │ CSV  │ IMAGE    │
              └──────────────────┬───────────────────┘
                                 ▼  (text chunks via yield)
              ┌──────────────────────────────────────┐
              │      S3 Output Storage                │
              │  streams text → s3://docconv-output   │
              └──────────────────────────────────────┘
```

### Docker Services

| Service      | Image                              | Ports              | Purpose                    |
|------------- |------------------------------------|--------------------|----------------------------|
| localstack   | `localstack/localstack:3.4`        | 4566               | SQS queues + S3 buckets    |
| rabbitmq     | `rabbitmq:3.13-management-alpine`  | 5672, 15672 (UI)   | RabbitMQ message bus       |
| zookeeper    | `confluentinc/cp-zookeeper:7.6.1`  | 2181               | Kafka coordination         |
| kafka        | `confluentinc/cp-kafka:7.6.1`      | 9092               | Kafka message bus          |
| ftp          | `fauria/vsftpd`                    | 21, 21100-21110    | Demo FTP server            |
| docconv-app  | *Built from Dockerfile*            | 8080               | The conversion service     |

---

## Quick Start

```bash
# 1. Clone / enter the project
cd docconv-service

# 2. Make the init script executable
chmod +x scripts/init-localstack.sh

# 3. Build and start everything
docker compose up --build -d

# 4. Watch the logs
docker compose logs -f docconv-app

# 5. Run the demo (from host – needs pip packages)
pip install boto3 pika confluent-kafka requests
python scripts/demo.py
```

---

## Configuration (Environment Variables)

### Feature Flags

| Variable           | Default | Description                       |
|--------------------|---------|-----------------------------------|
| `ENABLE_API`       | `true`  | Start the FastAPI HTTP server     |
| `ENABLE_SQS`       | `true`  | Start the SQS listener thread    |
| `ENABLE_RABBITMQ`  | `true`  | Start the RabbitMQ listener       |
| `ENABLE_KAFKA`     | `true`  | Start the Kafka listener          |

Set any flag to `"false"` to disable that component at startup.

### Connection Settings

See `.env.example` for the full list of SQS, S3, RabbitMQ, Kafka, and
tuning variables.

---

## Message Schema (JSON)

Every bus and the API accept the same JSON schema:

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

### `GET /health`

Returns service status and which components are enabled.

### `POST /convert/job`

Submit a conversion job (body = JSON matching the schema above).

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

---

## Project Structure

```
docconv-service/
├── docker-compose.yml          # Full infrastructure stack
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
│   ├── api.py                  # FastAPI REST endpoints
│   ├── processor.py            # Pipeline: fetch → convert → upload
│   ├── storage.py              # S3 output uploader
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
│   └── converters/             # One module per document type
│       ├── dispatch.py         # Routes by document_type
│       ├── pdf_converter.py    # PDF text + embedded image OCR
│       ├── docx_converter.py   # Word documents
│       ├── xlsx_converter.py   # Excel / CSV spreadsheets
│       ├── pptx_converter.py   # PowerPoint presentations
│       ├── html_converter.py   # HTML pages
│       ├── rtf_converter.py    # Rich Text Format
│       ├── odt_converter.py    # OpenDocument Text
│       ├── text_converter.py   # Plain text with encoding detection
│       └── image_converter.py  # Image OCR (JPEG/PNG/TIFF/BMP/WEBP)
│
└── scripts/
    ├── init-localstack.sh      # Creates SQS queue + S3 buckets
    └── demo.py                 # Send test jobs via every channel
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

---

## Disabling the API

To run as a pure bus-consumer with no HTTP endpoint:

```yaml
# docker-compose.yml override
docconv-app:
  environment:
    ENABLE_API: "false"
```

The process will still run, consuming from whichever buses are enabled,
and will block on a simple sleep loop instead of the Uvicorn server.

---

## Checking Output

Converted text lands in the `docconv-output` S3 bucket (LocalStack):

```bash
# List objects
aws --endpoint-url=http://localhost:4566 s3 ls s3://docconv-output/converted/ --recursive

# Download and view
aws --endpoint-url=http://localhost:4566 s3 cp s3://docconv-output/converted/demo-url-001.txt -
```
"# docconverterpy" 
"# docconverterpy" 
