#!/bin/bash
# run_backup.sh — wrapper for daily Notion backup
# Logs output to backups/backup.log
# Called by launchd (or run manually: bash scripts/run_backup.sh)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="$PROJECT_DIR/backups/backup.log"

mkdir -p "$PROJECT_DIR/backups"

echo "──────────────────────────────────────" >> "$LOG_FILE"
echo "$(date '+%Y-%m-%d %H:%M:%S')  Starting backup" >> "$LOG_FILE"

/usr/bin/python3 "$SCRIPT_DIR/notion_backup.py" >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S')  ✅ Backup completed" >> "$LOG_FILE"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S')  ❌ Backup FAILED (exit $EXIT_CODE)" >> "$LOG_FILE"
fi

exit $EXIT_CODE
