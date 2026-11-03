#!/usr/bin/env bash
# Install/remove the launchd agents for the local deployment.
#   ./scripts/install_services.sh install|uninstall|status
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
LABELS=(org.raymap.api org.raymap.backup)

install() {
  mkdir -p "$AGENTS" "$HOME/Library/Logs"
  for label in "${LABELS[@]}"; do
    cp "$ROOT/launchd/$label.plist" "$AGENTS/$label.plist"
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist"
    echo "installed $label"
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
