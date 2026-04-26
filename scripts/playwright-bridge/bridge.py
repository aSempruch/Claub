#!/usr/bin/env python3
"""Playwright bridge daemon.

Spawns a per-agent @playwright/mcp child process on demand, each with its own
--user-data-dir so login state survives across restarts. The bot drives this
via on_start / on_stop lifecycle hooks in agents.yaml.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("playwright-bridge")

STATE: dict[str, subprocess.Popen] = {}
STATE_LOCK = threading.Lock()
CONFIG: dict = {}


def is_port_listening(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_port_listening(host, port):
            return True
        time.sleep(0.1)
    return False


def build_command(agent_cfg: dict) -> list[str]:
    template = CONFIG["command_template"]
    port = str(agent_cfg["port"])
    user_data_dir = agent_cfg["user_data_dir"]
    return [a.format(port=port, user_data_dir=user_data_dir) for a in template]


def spawn_agent(name: str) -> tuple[int, str]:
    agent_cfg = CONFIG.get("agents", {}).get(name)
    if not agent_cfg:
        log.info("start %s: no config, no-op", name)
        return 204, ""

    with STATE_LOCK:
        existing = STATE.get(name)
        if existing and is_port_listening("127.0.0.1", agent_cfg["port"]):
            log.info("start %s: already running pid=%d", name, existing.pid)
            return 200, json.dumps({"status": "already-running", "pid": existing.pid})

        Path(agent_cfg["user_data_dir"]).mkdir(parents=True, exist_ok=True)
        cmd = build_command(agent_cfg)
        log.info("start %s: spawning %s", name, " ".join(cmd))
        child = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
            start_new_session=True,
        )
        STATE[name] = child

    ok = wait_for_port("127.0.0.1", agent_cfg["port"], timeout=15.0)
    if not ok:
        log.warning("start %s: port %d did not come up", name, agent_cfg["port"])
        return 504, json.dumps({"status": "timeout", "pid": child.pid})
    log.info("start %s: ready on port %d (pid=%d)", name, agent_cfg["port"], child.pid)
    return 200, json.dumps(
        {"status": "started", "pid": child.pid, "port": agent_cfg["port"]}
    )


def stop_agent(name: str) -> tuple[int, str]:
    with STATE_LOCK:
        child = STATE.pop(name, None)
    if not child:
        return 200, json.dumps({"status": "not-running"})
    if child.poll() is not None:
        return 200, json.dumps({"status": "already-exited"})
    try:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
    except Exception:
        log.exception("stop %s: error", name)
    log.info("stop %s: terminated pid=%d", name, child.pid)
    return 200, json.dumps({"status": "stopped"})


def status() -> str:
    out: dict[str, dict] = {}
    with STATE_LOCK:
        for name, child in STATE.items():
            port = CONFIG.get("agents", {}).get(name, {}).get("port")
            alive = is_port_listening("127.0.0.1", port) if port else False
            out[name] = {
                "pid": child.pid,
                "alive": alive,
                "port": port,
            }
    return json.dumps(out)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str = "") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_POST(self) -> None:
        parts = self.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] in ("start", "stop"):
            action, name = parts
            code, body = spawn_agent(name) if action == "start" else stop_agent(name)
            self._send(code, body)
            return
        self._send(404, json.dumps({"error": "unknown path"}))

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/status":
            self._send(200, status())
            return
        self._send(404, json.dumps({"error": "unknown path"}))

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)


def shutdown(*_args) -> None:
    log.info("shutdown: killing %d children", len(STATE))
    with STATE_LOCK:
        for _, child in list(STATE.items()):
            if child.poll() is None:
                try:
                    child.terminate()
                except Exception:
                    pass
    sys.exit(0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="path to bridge config JSON")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    global CONFIG
    CONFIG = json.loads(Path(args.config).read_text())

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    host = CONFIG.get("listen_host", "127.0.0.1")
    port = int(CONFIG.get("listen_port", 9500))
    server = ThreadingHTTPServer((host, port), Handler)
    log.info("bridge listening on %s:%d", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
