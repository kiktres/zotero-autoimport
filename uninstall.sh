#!/bin/sh
# Remove the launchd agent. The drop folder and the log are left alone.
P="$HOME/Library/LaunchAgents/local.zotero-autoimport.plist"
launchctl bootout "gui/$(id -u)" "$P" 2>/dev/null || true
rm -f "$P"
echo "removed"
