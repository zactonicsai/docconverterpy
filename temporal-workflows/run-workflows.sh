#!/bin/bash
set -euo pipefail

###############################################################################
# run-workflows.sh – Start all document conversion workflows in Temporal
#
# Usage:
#   # Start the Docker stack first
#   docker compose up -d
#
#   # Option A: Run from the Temporal admin container (CLI available)
#   docker compose exec temporal-admin-tools bash /workflows/run-workflows.sh
#
#   # Option B: Run from host using the REST API (curl)
#   bash temporal-workflows/run-workflows.sh --api
#
#   # Option C: Run individual workflows
#   bash temporal-workflows/run-workflows.sh --api --only pipeline
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPORAL_HOST="${TEMPORAL_HOST:-temporal-server:7233}"
TEMPORAL_NAMESPACE="${TEMPORAL_NAMESPACE:-default}"
TASK_QUEUE="${TEMPORAL_TASK_QUEUE:-docconv-tasks}"
API_BASE="${API_BASE:-http://localhost:8080}"
USE_API=false
ONLY=""

# Parse args
while [[ $# -gt 0 ]]; do
  case $1 in
    --api) USE_API=true; shift ;;
    --only) ONLY="$2"; shift 2 ;;
    --api-base) API_BASE="$2"; shift 2 ;;
    --temporal-host) TEMPORAL_HOST="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

G='\033[92m'; R='\033[91m'; C='\033[96m'; B='\033[1m'; N='\033[0m'
ok()   { echo -e "  ${G}✓${N} $1"; }
fail() { echo -e "  ${R}✗${N} $1"; }
head() { echo -e "\n${B}${C}═══════════════════════════════════════════════════${N}"; echo -e "  ${B}$1${N}"; echo -e "${B}${C}═══════════════════════════════════════════════════${N}\n"; }

should_run() { [[ -z "$ONLY" ]] || [[ "$ONLY" == "$1" ]]; }

# ─────────────────────────────────────────────────────────────────────────────
# Helper: start workflow via Temporal CLI
# ─────────────────────────────────────────────────────────────────────────────
start_via_cli() {
  local wf_type="$1"
  local wf_id="$2"
  local input_file="$3"

  temporal workflow start \
    --address "$TEMPORAL_HOST" \
    --namespace "$TEMPORAL_NAMESPACE" \
    --task-queue "$TASK_QUEUE" \
    --type "$wf_type" \
    --workflow-id "$wf_id" \
    --input "$(cat "$input_file")" \
    2>&1 && ok "$wf_type → $wf_id" || fail "$wf_type → $wf_id"
}

# ─────────────────────────────────────────────────────────────────────────────
# Helper: start workflow via REST API
# ─────────────────────────────────────────────────────────────────────────────
start_via_api() {
  local endpoint="$1"
  local input_file="$2"
  local label="$3"

  local resp
  resp=$(curl -s -w "\n%{http_code}" -X POST "${API_BASE}${endpoint}" \
    -H "Content-Type: application/json" \
    -d @"$input_file" 2>&1)

  local body=$(echo "$resp" | head -n -1)
  local code=$(echo "$resp" | tail -n 1)

  if [[ "$code" =~ ^2 ]]; then
    ok "$label  (HTTP $code)"
    echo "     $body" | head -c 200
    echo
  else
    fail "$label  (HTTP $code)"
    echo "     $body" | head -c 200
    echo
  fi
}

###############################################################################
#  WORKFLOWS
###############################################################################

head "Document Conversion Temporal Workflows"

echo "Mode:      $(if $USE_API; then echo "REST API ($API_BASE)"; else echo "Temporal CLI ($TEMPORAL_HOST)"; fi)"
echo "Namespace: $TEMPORAL_NAMESPACE"
echo "Queue:     $TASK_QUEUE"
echo ""

# ═══════════════════════════════════════════════════════════════════════════════
# 1. Basic Conversion (URL → text)
# ═══════════════════════════════════════════════════════════════════════════════
if should_run "basic"; then
  head "1. Basic Conversion (URL → text)"
  if $USE_API; then
    start_via_api "/convert/job" "$SCRIPT_DIR/01-basic-conversion.json" "DocumentConversionWorkflow"
  else
    start_via_cli "DocumentConversionWorkflow" "docconv-demo-conv-001" "$SCRIPT_DIR/01-basic-conversion.json"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# 2. S3-Sourced Conversion
# ═══════════════════════════════════════════════════════════════════════════════
if should_run "s3"; then
  head "2. S3-Sourced Conversion"
  # Upload a test file to LocalStack first
  echo "This is a test document for S3 conversion." | \
    aws --endpoint-url=http://localhost:4566 s3 cp - s3://docconv-input/docs/report.pdf 2>/dev/null && \
    ok "Uploaded test file to s3://docconv-input/docs/report.pdf" || \
    fail "Could not upload test file (LocalStack may not be accessible)"

  if $USE_API; then
    start_via_api "/convert/job" "$SCRIPT_DIR/02-s3-conversion.json" "DocumentConversionWorkflow (S3)"
  else
    start_via_cli "DocumentConversionWorkflow" "docconv-demo-s3-001" "$SCRIPT_DIR/02-s3-conversion.json"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# 3. Batch Conversion (3 documents in parallel)
# ═══════════════════════════════════════════════════════════════════════════════
if should_run "batch"; then
  head "3. Batch Conversion (3 documents in parallel)"
  if $USE_API; then
    echo "  (Batch not available via REST – using Temporal CLI)"
  fi
  start_via_cli "BatchConversionWorkflow" "batch-demo-001" "$SCRIPT_DIR/03-batch-conversion.json"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# 4. Document Pipeline (validate → convert → analyze → metadata)
# ═══════════════════════════════════════════════════════════════════════════════
if should_run "pipeline"; then
  head "4. Document Pipeline (validate → convert → analyze → metadata)"
  if $USE_API; then
    start_via_api "/workflow/pipeline" "$SCRIPT_DIR/04-pipeline.json" "DocumentPipelineWorkflow"
  else
    start_via_cli "DocumentPipelineWorkflow" "pipeline-demo-001" "$SCRIPT_DIR/04-pipeline.json"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# 5. S3 Folder Watch (scan inbox/ and convert all files)
# ═══════════════════════════════════════════════════════════════════════════════
if should_run "s3watch"; then
  head "5. S3 Folder Watch (scan inbox/ and convert all)"
  # Upload test files to inbox/
  for fname in test1.txt test2.html; do
    echo "Content of $fname for folder watch demo." | \
      aws --endpoint-url=http://localhost:4566 s3 cp - "s3://docconv-input/inbox/$fname" 2>/dev/null && \
      ok "Uploaded inbox/$fname" || fail "Upload failed"
  done

  if $USE_API; then
    start_via_api "/workflow/s3-watch" "$SCRIPT_DIR/05-s3-folder-watch.json" "S3FolderWatchWorkflow"
  else
    start_via_cli "S3FolderWatchWorkflow" "s3watch-demo-001" "$SCRIPT_DIR/05-s3-folder-watch.json"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# 6. Webhook Notification Conversion
# ═══════════════════════════════════════════════════════════════════════════════
if should_run "webhook"; then
  head "6. Webhook Notification Conversion"
  if $USE_API; then
    start_via_api "/workflow/webhook-convert" "$SCRIPT_DIR/06-webhook-notification.json" "WebhookNotificationWorkflow"
  else
    start_via_cli "WebhookNotificationWorkflow" "webhook-demo-001" "$SCRIPT_DIR/06-webhook-notification.json"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# 7. Multi-Format Output (full text + pages + metadata + analytics)
# ═══════════════════════════════════════════════════════════════════════════════
if should_run "multi"; then
  head "7. Multi-Format Output"
  if $USE_API; then
    start_via_api "/workflow/multi-format" "$SCRIPT_DIR/07-multi-format-output.json" "MultiFormatOutputWorkflow"
  else
    start_via_cli "MultiFormatOutputWorkflow" "multi-demo-001" "$SCRIPT_DIR/07-multi-format-output.json"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# 8. Retry Escalation (standard → high-DPI OCR)
# ═══════════════════════════════════════════════════════════════════════════════
if should_run "escalation"; then
  head "8. Retry Escalation"
  # Upload a test file
  echo "Sparse content" | \
    aws --endpoint-url=http://localhost:4566 s3 cp - s3://docconv-input/scans/blurry-scan.pdf 2>/dev/null && \
    ok "Uploaded test scan" || fail "Upload failed"

  if $USE_API; then
    start_via_api "/workflow/retry-escalation" "$SCRIPT_DIR/08-retry-escalation.json" "RetryEscalationWorkflow"
  else
    start_via_cli "RetryEscalationWorkflow" "escalate-demo-001" "$SCRIPT_DIR/08-retry-escalation.json"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# 9. Scheduled Maintenance (one-shot)
# ═══════════════════════════════════════════════════════════════════════════════
if should_run "maintenance"; then
  head "9. Scheduled Maintenance (one-shot)"
  if $USE_API; then
    start_via_api "/workflow/maintenance" "$SCRIPT_DIR/09-maintenance.json" "ScheduledMaintenanceWorkflow"
  else
    start_via_cli "ScheduledMaintenanceWorkflow" "maintenance-demo-001" "$SCRIPT_DIR/09-maintenance.json"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# 10. Cron Schedules (via Temporal CLI only)
# ═══════════════════════════════════════════════════════════════════════════════
if should_run "cron"; then
  head "10. Cron Scheduled Workflows"

  echo "  Setting up S3 inbox watch (every hour)..."
  temporal workflow start \
    --address "$TEMPORAL_HOST" \
    --namespace "$TEMPORAL_NAMESPACE" \
    --task-queue "$TASK_QUEUE" \
    --type "S3FolderWatchWorkflow" \
    --workflow-id "s3watch-hourly" \
    --cron-schedule "0 * * * *" \
    --input "$(cat "$SCRIPT_DIR/05-s3-folder-watch.json")" \
    2>&1 && ok "S3FolderWatchWorkflow (hourly cron)" || fail "S3FolderWatchWorkflow cron"

  echo ""
  echo "  Setting up maintenance (daily at 3 AM)..."
  temporal workflow start \
    --address "$TEMPORAL_HOST" \
    --namespace "$TEMPORAL_NAMESPACE" \
    --task-queue "$TASK_QUEUE" \
    --type "ScheduledMaintenanceWorkflow" \
    --workflow-id "maintenance-daily" \
    --cron-schedule "0 3 * * *" \
    --input "$(cat "$SCRIPT_DIR/09-maintenance.json")" \
    2>&1 && ok "ScheduledMaintenanceWorkflow (daily 3AM cron)" || fail "Maintenance cron"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════════
head "Done!"
echo "  View workflows in the Temporal UI:  http://localhost:8088"
echo "  Query status via API:               curl http://localhost:8080/workflows/recent"
echo ""
echo "  To check a specific workflow:"
echo "    curl http://localhost:8080/workflow/docconv-demo-conv-001/status"
echo ""
echo "  To cancel a running workflow:"
echo "    curl -X POST http://localhost:8080/workflow/docconv-demo-conv-001/cancel"
echo ""
