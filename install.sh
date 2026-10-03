#!/bin/sh
# Install (or reinstall) the launchd agent that watches the drop folder.
# Usage: ./install.sh [DROP_FOLDER]      default: ~/Downloads/to-zotero
set -e
cd "$(dirname "$0")"
LABEL=local.zotero-autoimport
INBOX="${1:-$HOME/Downloads/to-zotero}"
PYTHON="$(command -v python3)"
for t in claude pdftotext pdftoppm pdfinfo; do
  command -v "$t" >/dev/null || { echo "missing: $t (see README)"; exit 1; }
done
# the agent gets the directories of the tools found now, plus the system ones
DIRS=""
for t in claude pdftotext djvutxt; do
  p="$(command -v "$t" 2>/dev/null)" && DIRS="$DIRS:$(dirname "$p")"
done
AGENT_PATH="$(echo "${DIRS#:}:/usr/bin:/bin:/usr/sbin:/sbin" | tr ':' '\n' | awk '!s[$0]++' | paste -sd: -)"
mkdir -p "$INBOX"
P="$HOME/Library/LaunchAgents/$LABEL.plist"
sed -e "s|@LABEL@|$LABEL|" -e "s|@PYTHON@|$PYTHON|" -e "s|@SCRIPT@|$PWD/zotero_autoimport.py|" \
    -e "s|@INBOX@|$INBOX|g" -e "s|@PATH@|$AGENT_PATH|" -e "s|@HOME@|$HOME|g" \
    launchd.plist.in > "$P"
launchctl bootout "gui/$(id -u)" "$P" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$P"
echo "watching $INBOX; log: ~/Library/Logs/zotero-autoimport.log"
