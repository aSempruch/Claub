#!/bin/zsh
# Service control helper for claub launchd service.
set -euo pipefail

SCRIPT_NAME="${0:t}"
REPO_DIR="${0:a:h:h}"
SERVICE_ID="com.asempruch.claub"
PLIST="$HOME/Library/LaunchAgents/$SERVICE_ID.plist"
LOG_DIR="$HOME/Library/Logs/claub"

usage() {
    echo "Usage: $0 {install|uninstall|start|stop|restart|status|logs}"
    exit 1
}

cmd_install() {
    local src="$REPO_DIR/scripts/$SERVICE_ID.plist"

    mkdir -p "$LOG_DIR"

    if [[ ! -f "$src" ]]; then
        echo "error: plist not found at $src" >&2
        exit 1
    fi

    # Template the plist with actual paths
    sed "s|__REPO_DIR__|$REPO_DIR|g; s|__LOG_DIR__|$LOG_DIR|g" "$src" > "$PLIST"

    echo "Installed $PLIST"
    echo "Run '$SCRIPT_NAME start' to start the service."
}

cmd_uninstall() {
    cmd_stop 2>/dev/null || true
    rm -f "$PLIST"
    echo "Uninstalled $SERVICE_ID"
}

cmd_start() {
    local domain="gui/$(id -u)"
    if launchctl bootstrap "$domain" "$PLIST" 2>/dev/null; then
        echo "Started $SERVICE_ID"
    elif launchctl kickstart -k "$domain/$SERVICE_ID" 2>/dev/null; then
        echo "Restarted $SERVICE_ID"
    else
        echo "error: failed to start $SERVICE_ID" >&2
        return 1
    fi
}

cmd_stop() {
    local domain="gui/$(id -u)"
    if launchctl bootout "$domain/$SERVICE_ID" 2>/dev/null; then
        echo "Stopped $SERVICE_ID"
    else
        echo "$SERVICE_ID is not running"
    fi
}

cmd_restart() {
    cmd_stop 2>/dev/null || true
    sleep 1
    cmd_start
}

cmd_status() {
    launchctl print "gui/$(id -u)/$SERVICE_ID" 2>&1 || echo "$SERVICE_ID is not loaded"
}

cmd_logs() {
    local follow="${1:-}"
    if [[ "$follow" == "-f" ]]; then
        tail -f "$LOG_DIR"/stdout.log "$LOG_DIR"/stderr.log
    else
        echo "=== stdout ==="
        tail -50 "$LOG_DIR/stdout.log" 2>/dev/null || echo "(empty)"
        echo ""
        echo "=== stderr ==="
        tail -50 "$LOG_DIR/stderr.log" 2>/dev/null || echo "(empty)"
        echo ""
        echo "(use '$SCRIPT_NAME logs -f' to follow)"
    fi
}

case "${1:-}" in
    install)   cmd_install ;;
    uninstall) cmd_uninstall ;;
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    restart)   cmd_restart ;;
    status)    cmd_status ;;
    logs)      cmd_logs "${2:-}" ;;
    *)         usage ;;
esac
