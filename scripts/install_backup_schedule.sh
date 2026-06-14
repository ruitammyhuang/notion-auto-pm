#!/bin/bash
# install_backup_schedule.sh
# Run this ONCE to install the daily 6am Notion backup.
# After this, launchd will automatically run the backup every day.

PLIST_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/com.rui.notion-backup.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.rui.notion-backup.plist"

# Make the backup script executable
chmod +x "$(dirname "$PLIST_SRC")/run_backup.sh"

# Copy plist and load it
cp "$PLIST_SRC" "$PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null  # unload if already installed
launchctl load "$PLIST_DST"

echo "✅  Notion backup scheduled daily at 6:00 AM"
echo "   To run a manual backup now: python3 scripts/notion_backup.py"
echo "   To uninstall: launchctl unload ~/Library/LaunchAgents/com.rui.notion-backup.plist"
