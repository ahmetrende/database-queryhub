#!/bin/bash
# Periodic publisher for the product-metrics dashboard.
#
# Flow:
#   1. Re-generate metrics_dashboard.html from the bot DB (uses the
#      same /etc/slackbot/env credentials the bot itself uses).
#   2. Upload to s3://${METRICS_DASHBOARD_BUCKET}/${METRICS_DASHBOARD_KEY}
#      with a short Cache-Control so refreshes propagate quickly.
#   3. Log the size and timestamp; non-zero exit on failure so the
#      systemd timer / cron alarms surface in journalctl.
#
# Auth: relies on the EC2 instance's attached IAM role (preferred)
# or a standard AWS credentials chain. No keys baked in.
#
# Env file expected: /etc/slackbot/dashboard.env
#   METRICS_DASHBOARD_BUCKET   the S3 bucket name (DevOps provisions)
#   METRICS_DASHBOARD_KEY      object key (default: dba-metrics/index.html)
#   AWS_REGION                 e.g. eu-central-1 (optional if instance metadata is reachable)

set -euo pipefail

# Repo dir defaults to "the directory above this script" so we don't
# hard-code a deploy-time path. Override via REPO_DIR if running from
# outside the standard layout.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-"$(cd "$SCRIPT_DIR/.." && pwd)"}"
DASHBOARD_HTML="${REPO_DIR}/metrics_dashboard.html"

# Bot DB connection + master.key path come from the standard env file.
set -a
[ -f /etc/slackbot/env ] && . /etc/slackbot/env
[ -f /etc/slackbot/dashboard.env ] && . /etc/slackbot/dashboard.env
set +a

: "${METRICS_DASHBOARD_BUCKET:?METRICS_DASHBOARD_BUCKET must be set (see /etc/slackbot/dashboard.env)}"
METRICS_DASHBOARD_KEY="${METRICS_DASHBOARD_KEY:-dba-metrics/index.html}"

cd "$REPO_DIR"

# 1) Re-generate the HTML. Fails loudly if the DB connection breaks
#    or any view query errors out — we'd rather miss an upload than
#    publish a stale or partial dashboard.
echo "[publish_metrics_dashboard] regenerating HTML..."
.venv/bin/python scripts/build_metrics_dashboard.py

# 2) Upload. Short max-age on the HTML so admin sees fresh data on
#    refresh; the must-revalidate keeps intermediate caches honest.
#    Static assets (logo, favicon) get a longer max-age — they only
#    change when the brand is updated, not every 15 minutes.
echo "[publish_metrics_dashboard] uploading to s3://${METRICS_DASHBOARD_BUCKET}/${METRICS_DASHBOARD_KEY}..."
aws s3 cp "$DASHBOARD_HTML" \
    "s3://${METRICS_DASHBOARD_BUCKET}/${METRICS_DASHBOARD_KEY}" \
    --content-type "text/html; charset=utf-8" \
    --cache-control "max-age=300, must-revalidate" \
    --only-show-errors

# Brand assets live next to the HTML. The HTML references them with
# relative URLs (queryhub-mark.svg as header logo + favicon),
# so the object key is the same dirname as METRICS_DASHBOARD_KEY.
ASSET_DIR_KEY="$(dirname "$METRICS_DASHBOARD_KEY")"
for asset in queryhub-mark.svg; do
    asset_path="${REPO_DIR}/QueryHubWeb/brand/svg/queryhub-avatar-green.svg"
    if [ -f "$asset_path" ]; then
        aws s3 cp "$asset_path" \
            "s3://${METRICS_DASHBOARD_BUCKET}/${ASSET_DIR_KEY}/${asset}" \
            --content-type "image/svg+xml" \
            --cache-control "max-age=86400" \
            --only-show-errors
    fi
done

bytes=$(stat -c %s "$DASHBOARD_HTML")
echo "[publish_metrics_dashboard] published $(date -u +%FT%TZ) — ${bytes} bytes (html + logo asset)"
