"""
Pydantic models describing the messages that arrive via any message bus or API.
"""

from __future__ import annotations

import enum
from typing import Optional
from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────────────────────────

class DocumentType(str, enum.Enum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    HTML = "html"
    RTF = "rtf"
    ODT = "odt"
    TXT = "txt"
    CSV = "csv"
    IMAGE = "image"          # jpg, png, tiff, bmp, webp


class LocationType(str, enum.Enum):
    S3 = "s3"
    URL = "url"
    FTP = "ftp"
    LOCAL = "local"          # for API file upload


class AuthType(str, enum.Enum):
    NONE = "none"
    BASIC = "basic"
    BEARER = "bearer"
    AWS_SIGV4 = "aws_sigv4"


# ── Message schema ───────────────────────────────────────────────────────────

class ConversionJob(BaseModel):
    """
    Canonical message schema accepted from every bus and the REST API.
    """
    job_id: Optional[str] = Field(None, description="Optional caller-supplied ID")
    document_type: DocumentType
    location_type: LocationType

    # ── S3 fields (when location_type == s3) ─────────────────────────────
    s3_bucket: Optional[str] = None
    s3_key: Optional[str] = None
    s3_endpoint_url: Optional[str] = None   # override for non-AWS S3

    # ── URL fields (when location_type == url) ───────────────────────────
    url: Optional[str] = None

    # ── FTP fields (when location_type == ftp) ───────────────────────────
    ftp_host: Optional[str] = None
    ftp_port: int = 21
    ftp_path: Optional[str] = None
    ftp_user: Optional[str] = None
    ftp_pass: Optional[str] = None

    # ── Auth (optional, mainly for URL) ──────────────────────────────────
    auth_type: AuthType = AuthType.NONE
    auth_username: Optional[str] = None
    auth_password: Optional[str] = None
    auth_token: Optional[str] = None

    # ── Output control ───────────────────────────────────────────────────
    output_s3_bucket: Optional[str] = None  # override default output bucket
    output_s3_key: Optional[str] = None     # override auto-generated key


class ConversionResult(BaseModel):
    job_id: Optional[str] = None
    success: bool
    output_bucket: Optional[str] = None
    output_key: Optional[str] = None
    error: Optional[str] = None
    pages_processed: int = 0
    characters_extracted: int = 0
