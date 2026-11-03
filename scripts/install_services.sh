#!/usr/bin/env bash
# Install/remove the launchd agents for the local deployment.
#   ./scripts/install_services.sh install|uninstall|status
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
LABELS=(org.raymap.api org.raymap.backup org.raymap.device)

install() {
  mkdir -p "$AGENTS" "$HOME/Library/Logs"
  for label in "${LABELS[@]}"; do
    cp "$ROOT/launchd/$label.plist" "$AGENTS/$label.plist"
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    # bootout returns before launchd finishes tearing the job down; bootstrap
    # right after it fails with "Input/output error", so retry briefly.
    for attempt in 1 2 3 4 5; do
      if launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist" 2>/dev/null; then
        echo "installed $label"
        break
      fi
      sleep 2
      [ "$attempt" -eq 5 ] && echo "FAILED $label — bootstrap it by hand: launchctl bootstrap $DOMAIN $AGENTS/$label.plist"
    done
  done
}

uninstall() {
  for label in "${LABELS[@]}"; do
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
    echo "removed $label"
  done
}

case "${1:-install}" in
  install) install ;;
  uninstall) uninstall ;;
  status) launchctl list | grep -i raymap || echo "no raymap services loaded" ;;
  *) echo "usage: $0 [install|uninstall|status]" >&2; exit 2 ;;
esac
