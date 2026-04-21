#!/usr/bin/env bash
# decommission_mac.sh — Shut off the legacy Mac plumbing.
#
# Run this AFTER verifying Render is serving the dashboard correctly
# (scripts/verify_render.sh passing). It:
#
#   1. Unloads the two fitness launchd jobs (Oura + Rides sync)
#   2. Does NOT touch the MCP server or Tailscale (work tools still use those)
#   3. Does NOT delete any code or secrets
#   4. Leaves old generated HTML files alone (harmless)
#
# Reversible: you can re-`launchctl load` the plists any time to re-enable
# the Mac pipeline as a rollback.
#
# Usage:  scripts/decommission_mac.sh
#         scripts/decommission_mac.sh --dry-run

set -eu
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

run() {
  if [ $DRY_RUN -eq 1 ]; then
    echo "  DRY: $*"
  else
    "$@" 2>&1 || echo "  (non-zero exit, continuing)"
  fi
}

echo "Sneha.OS Mac decommission"
echo "=========================="
[ $DRY_RUN -eq 1 ] && echo "(DRY RUN — no changes will be made)"
echo

echo "1. Unload fitness launchd jobs"
for LABEL in com.sneha.oura-sync com.sneha.rides-sync; do
  PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
  if [ -f "$PLIST" ]; then
    echo "  -> $LABEL"
    run launchctl unload "$PLIST"
  else
    echo "  -> $LABEL (not installed, skipping)"
  fi
done

echo
echo "2. Confirm Tailscale + MCP server still running (work tools intact)"
launchctl list 2>/dev/null | grep -E "com\.sneha\.(mcp-server|tailscaled)" \
  || echo "  (neither is active — that's fine if you're done with work tools too)"

echo
echo "3. Note: I am NOT deleting:"
echo "   - sheets.py / sheet_writer.py / sheet_reader.py (legacy, gated by USE_DB_RIDES etc.)"
echo "   - oura_sheets_sync.py (superseded by sync.py)"
echo "   - rides_cache.json (still written by old Strava sync if you manually run it)"
echo "   - the .plist files themselves (so you can re-load to rollback)"
echo
echo "Rollback: launchctl load ~/Library/LaunchAgents/com.sneha.oura-sync.plist"
echo
echo "DONE."
