#!/bin/bash
# Daily backup of the Rating DQ SQLite database.
# Runs as a systemd oneshot service (rating-backup.service / .timer).
set -euo pipefail

SRC="${RATING_DB:-/var/lib/rating/rating.db}"
DST="/var/backups/rating"
KEEP=30

if [ ! -f "$SRC" ]; then
  echo "source db not found: $SRC" >&2
  exit 1
fi

mkdir -p "$DST"
TS="$(date +%F_%H%M%S)"
cp "$SRC" "$DST/rating.db.$TS"
# integrity check (only if sqlite3 CLI is available)
if command -v sqlite3 >/dev/null 2>&1; then
  if ! sqlite3 "$DST/rating.db.$TS" "PRAGMA integrity_check;" >/dev/null 2>&1; then
    echo "backup failed integrity check" >&2
    rm -f "$DST/rating.db.$TS"
    exit 1
  fi
fi
# rotate: keep newest $KEEP
ls -1t "$DST"/rating.db.* | tail -n +$((KEEP+1)) | xargs -r rm -f
echo "backup ok: $DST/rating.db.$TS"
