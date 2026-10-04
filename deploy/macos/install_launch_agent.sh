#!/bin/bash
# Makes the parking monitor start by itself on this Mac: when you log in,
# after a reboot, and again within seconds if it ever crashes or exits.
# It also keeps auto-restarting after code changes, like before.
#
#   bash deploy/macos/install_launch_agent.sh            install / reinstall
#   bash deploy/macos/install_launch_agent.sh uninstall  remove it again
#
# Output goes to ~/Library/Logs/parking-lot-monitor.log
#   tail -f ~/Library/Logs/parking-lot-monitor.log
# Restart it by hand:
#   launchctl kickstart -k gui/$(id -u)/com.parkinglot.monitor
set -euo pipefail

LABEL="com.parkinglot.monitor"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/parking-lot-monitor.log"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "uninstall" ]; then
  rm -f "$PLIST"
  echo "Removed. The monitor will no longer start by itself."
  exit 0
fi

# macOS won't let background services read Documents, Desktop or Downloads
# (they fail with "Operation not permitted"), so the project must live elsewhere.
case "$REPO" in
  "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*)
    echo "The project is in $REPO."
    echo "macOS blocks background services from reading that folder. Move it first, e.g.:"
    echo "  mv \"$REPO\" \"$HOME/$(basename "$REPO")\""
    exit 1 ;;
esac

PYTHON="$REPO/.venv/bin/python3"
[ -x "$PYTHON" ] || { echo "Can't find $PYTHON (the project's .venv)"; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$REPO/scripts/run_dev_server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"
echo "Installed. The monitor is starting in the background."
echo "Log: $LOG"
