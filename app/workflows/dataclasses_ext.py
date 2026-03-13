"""
Extended data classes for advanced Temporal workflows.

Covers: S3 folder scanning, document pipeline, multi-format output,
webhook notification, text analytics, retry escalation, and scheduled tasks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ═════════════════════════════════════════════════════════════════════════════
# S3 Folder Watch / Scan
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class S3ScanInput:
    """Input for scanning an S3 prefix for files to convert."""
    bucket: str
    prefix: str                          # e.g. "inbox/"
    s3_endpoint_url: Optional[str] = None
    file_extensions: list[str] = field(  # filter by extension
        default_factory=lambda: [
            ".pdf", ".docx", ".xlsx", ".pptx", ".html",
            ".rtf", ".odt", ".txt", ".csv",
            ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp",
        ]
    )
    max_files: int = 100                 # safety limit per scan
    move_processed_to: Optional[str] = None  # e.g. "processed/" – move after done


@dataclass
class S3FileInfo:
    """Metadata about a single S3 object found during scan."""
    key: str
    size_bytes: int = 0
    last_modified: Optional[str] = None
    document_type: str = ""              # inferred from extension


@dataclass
class S3ScanOutput:
    """Output from the S3 scan activity."""
    files: list[S3FileInfo] = field(default_factory=list)
    total_found: int = 0
    skipped: int = 0


@dataclass
class S3FolderWatchInput:
    """Input for the S3FolderWatchWorkflow."""
    bucket: str
    prefix: str = "inbox/"
    output_bucket: Optional[str] = None
    output_prefix: str = "converted/"
    s3_endpoint_url: Optional[str] = None
    move_processed_to: Optional[str] = "processed/"
    max_files: int = 100


@dataclass
class S3FolderWatchOutput:
    """Output from the S3FolderWatchWorkflow."""
    total_scanned: int = 0
    total_converted: int = 0
    total_failed: int = 0
    results: list[dict] = field(default_factory=list)


# ═════════════════════════════════════════════════════════════════════════════
# Validation
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class ValidateInput:
    """Input for the file validation activity."""
    job_id: str
    local_path: str
    document_type: str
    max_file_size_mb: float = 500.0       # reject files over this size


@dataclass
class ValidateOutput:
    """Output from the file validation activity."""
    valid: bool
    file_size_bytes: int = 0
    mime_type: str = ""
    error: Optional[str] = None


# ═════════════════════════════════════════════════════════════════════════════
# Text Analytics
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class AnalyzeTextInput:
    """Input for the text analytics activity."""
    job_id: str
    text_path: str


@dataclass
class AnalyzeTextOutput:
    """Output from the text analytics activity."""
    total_chars: int = 0
    total_words: int = 0
    total_lines: int = 0
    total_pages: int = 0                  # from "--- Page N ---" markers
    language_hint: str = ""               # simple heuristic
    avg_words_per_line: float = 0.0
    has_tables: bool = False
    has_images: bool = False
    top_words: list[str] = field(default_factory=list)  # top 20 words


# ═════════════════════════════════════════════════════════════════════════════
# Webhook Notification
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class WebhookInput:
    """Input for sending a webhook notification."""
    url: str
    payload: dict = field(default_factory=dict)
    method: str = "POST"
    headers: dict = field(default_factory=dict)
    timeout_seconds: int = 30
    auth_token: Optional[str] = None


@dataclass
class WebhookOutput:
    """Output from a webhook call."""
    status_code: int = 0
    success: bool = False
    response_body: str = ""
    error: Optional[str] = None


@dataclass
class WebhookNotificationWorkflowInput:
    """Input for the WebhookNotificationWorkflow."""
    job_id: str
    document_type: str
    location_type: str

    # Source (same as ConversionWorkflowInput)
    s3_bucket: Optional[str] = None
    s3_key: Optional[str] = None
    s3_endpoint_url: Optional[str] = None
    url: Optional[str] = None
    ftp_host: Optional[str] = None
    ftp_port: int = 21
    ftp_path: Optional[str] = None
    ftp_user: Optional[str] = None
    ftp_pass: Optional[str] = None
    auth_type: str = "none"
    auth_username: Optional[str] = None
    auth_password: Optional[str] = None
    auth_token: Optional[str] = None

    # Output
    output_s3_bucket: Optional[str] = None
    output_s3_key: Optional[str] = None
    local_file_path: Optional[str] = None

    # Webhooks
    on_start_webhook: Optional[str] = None    # URL to call when starting
    on_complete_webhook: Optional[str] = None # URL to call on success
    on_failure_webhook: Optional[str] = None  # URL to call on failure
    webhook_auth_token: Optional[str] = None  # Bearer token for webhook calls


# ═════════════════════════════════════════════════════════════════════════════
# Document Pipeline (extended)
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class PipelineWorkflowInput:
    """Input for the DocumentPipelineWorkflow."""
    job_id: str
    document_type: str
    location_type: str

    # Source
    s3_bucket: Optional[str] = None
    s3_key: Optional[str] = None
    s3_endpoint_url: Optional[str] = None
    url: Optional[str] = None
    ftp_host: Optional[str] = None
    ftp_port: int = 21
    ftp_path: Optional[str] = None
    ftp_user: Optional[str] = None
    ftp_pass: Optional[str] = None
    auth_type: str = "none"
    auth_username: Optional[str] = None
    auth_password: Optional[str] = None
    auth_token: Optional[str] = None

    # Output
    output_s3_bucket: Optional[str] = None
    output_prefix: str = "pipeline/"
    local_file_path: Optional[str] = None

    # Pipeline flags
    enable_validation: bool = True
    enable_analytics: bool = True
    enable_metadata_output: bool = True     # write JSON metadata alongside text
    max_file_size_mb: float = 500.0
    on_complete_webhook: Optional[str] = None
    webhook_auth_token: Optional[str] = None


@dataclass
class PipelineWorkflowOutput:
    """Output from the DocumentPipelineWorkflow."""
    job_id: str
    success: bool
    text_bucket: Optional[str] = None
    text_key: Optional[str] = None
    metadata_key: Optional[str] = None       # S3 key for the JSON metadata
    analytics: Optional[AnalyzeTextOutput] = None
    error: Optional[str] = None
    total_chars: int = 0
    pages_processed: int = 0


# ═════════════════════════════════════════════════════════════════════════════
# Multi-Format Output
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class MultiFormatInput:
    """Input for the MultiFormatOutputWorkflow."""
    job_id: str
    document_type: str
    location_type: str

    # Source
    s3_bucket: Optional[str] = None
    s3_key: Optional[str] = None
    s3_endpoint_url: Optional[str] = None
    url: Optional[str] = None
    ftp_host: Optional[str] = None
    ftp_port: int = 21
    ftp_path: Optional[str] = None
    ftp_user: Optional[str] = None
    ftp_pass: Optional[str] = None
    auth_type: str = "none"
    auth_username: Optional[str] = None
    auth_password: Optional[str] = None
    auth_token: Optional[str] = None
    local_file_path: Optional[str] = None

    # Output
    output_s3_bucket: Optional[str] = None
    output_prefix: str = "multi/"

    # What to produce
    produce_full_text: bool = True
    produce_pages: bool = True               # one file per page
    produce_metadata_json: bool = True
    produce_analytics: bool = True


@dataclass
class MultiFormatOutput:
    """Output from the MultiFormatOutputWorkflow."""
    job_id: str
    success: bool
    outputs: dict = field(default_factory=dict)  # {"full_text": "s3://...", ...}
    error: Optional[str] = None


# ═════════════════════════════════════════════════════════════════════════════
# Retry Escalation
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class EscalationConvertInput:
    """Input for the enhanced OCR conversion activity."""
    job_id: str
    local_path: str
    document_type: str
    dpi: int = 400                           # higher than default 200
    psm: int = 6                             # Tesseract page segmentation mode
    oem: int = 3                             # Tesseract OCR engine mode


@dataclass
class RetryEscalationInput:
    """Input for the RetryEscalationWorkflow."""
    job_id: str
    document_type: str
    location_type: str

    # Source
    s3_bucket: Optional[str] = None
    s3_key: Optional[str] = None
    s3_endpoint_url: Optional[str] = None
    url: Optional[str] = None
    ftp_host: Optional[str] = None
    ftp_port: int = 21
    ftp_path: Optional[str] = None
    ftp_user: Optional[str] = None
    ftp_pass: Optional[str] = None
    auth_type: str = "none"
    auth_username: Optional[str] = None
    auth_password: Optional[str] = None
    auth_token: Optional[str] = None
    local_file_path: Optional[str] = None

    # Output
    output_s3_bucket: Optional[str] = None
    output_s3_key: Optional[str] = None

    # Escalation thresholds
    min_chars_threshold: int = 50            # if below this, escalate
    escalation_dpi: int = 400                # DPI for enhanced OCR retry


# ═════════════════════════════════════════════════════════════════════════════
# Scheduled Cleanup
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class ScheduledCleanupInput:
    """Input for the ScheduledCleanupWorkflow."""
    tmp_dir: str = "/tmp/docconv"
    max_age_hours: int = 24                  # delete files older than this
    s3_output_bucket: Optional[str] = None   # verify bucket health


@dataclass
class ScheduledCleanupOutput:
    """Output from the ScheduledCleanupWorkflow."""
    files_deleted: int = 0
    bytes_freed: int = 0
    s3_health_ok: bool = True
    errors: list[str] = field(default_factory=list)


# ═════════════════════════════════════════════════════════════════════════════
# S3 Move
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class S3MoveInput:
    """Input for moving (copy+delete) an S3 object."""
    bucket: str
    source_key: str
    dest_key: str
    s3_endpoint_url: Optional[str] = None
