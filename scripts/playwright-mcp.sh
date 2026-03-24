#!/bin/bash
# Install/manage the Playwright MCP HTTP server as a launchd service.
# Usage: playwright-mcp.sh [install|start|stop|restart|status|logs|uninstall]

set -e

PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/com.asempruch.playwright-mcp.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.asempruch.playwright-mcp.plist"
LABEL="com.asempruch.playwright-mcp"
LOG_DIR="$HOME/Library/Logs/playwright-mcp"

case "${1:-status}" in
    install)
        mkdir -p "$LOG_DIR"
        cp "$PLIST_SRC" "$PLIST_DST"
        echo "Installed $PLIST_DST"
        echo "Run: $0 start"
        ;;
    start)
        launchctl load "$PLIST_DST" 2>/dev/null || true
        echo "Started $LABEL"
        ;;
    stop)
        launchctl unload "$PLIST_DST" 2>/dev/null || true
        echo "Stopped $LABEL"
        ;;
    restart)
        launchctl unload "$PLIST_DST" 2>/dev/null || true
        launchctl load "$PLIST_DST"
        echo "Restarted $LABEL"
        ;;
    status)
        launchctl list "$LABEL" 2>/dev/null && echo "Running" || echo "Not running"
        ;;
    logs)
        shift
        if [ "$1" = "-f" ]; then
            tail -f "$LOG_DIR/stdout.log" "$LOG_DIR/stderr.log"
        else
            tail -50 "$LOG_DIR/stdout.log" "$LOG_DIR/stderr.log"
        fi
        ;;
    uninstall)
        launchctl unload "$PLIST_DST" 2>/dev/null || true
        rm -f "$PLIST_DST"
        echo "Uninstalled $LABEL"
        ;;
    *)
        echo "Usage: $0 [install|start|stop|restart|status|logs|uninstall]"
        exit 1
        ;;
esac
