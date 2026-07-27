#!/usr/bin/env python3
"""Exec bridge daemon.

Runs commands inside a throwaway `claub-exec` container per request, mounting
only the calling agent's workspace. Mirrors scripts/playwright-bridge/. The
Docker socket never enters the bot container — this daemon holds that authority
on the host, gated by a shared secret.

TWO ENDPOINTS, and the split is the trust boundary:

  /exec/{agent}     arbitrary command, ALWAYS --network none
  /install/{agent}  package names only, network — no command is ever accepted

Do not merge these into one endpoint with a network flag (that moves the
decision into the bot container) or a command-shape check (spoofable by
`echo 'uv pip install '; curl ...`). The networked path must be structurally
incapable of running an arbitrary command, not merely checked for it.
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import (
    build_docker_argv,
    build_install_command,
    cap_stream,
    clamp_timeout,
    validate_agent,
)

log = logging.getLogger("exec-bridge")

CONFIG: dict = {}
SEM = threading.Semaphore(1)
RUNNING: dict[str, int] = {}
RUNNING_LOCK = threading.Lock()
STREAM_CAP = 1024 * 1024  # 1 MiB per stream — hard ceiling against OOM


def _read_capped(pipe, limit: int) -> tuple[bytes, bool]:
    def chunks():
        while True:
            data = pipe.read(65536)
            if not data:
                return
            yield data
    return cap_stream(chunks(), limit)


def run_container(argv: list[str], name: str, exec_timeout: int, docker_bin: str) -> dict:
    """Run the container, streaming output under a byte cap; kill on timeout."""
    start = time.monotonic()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out_holder: dict = {}

    def reader(pipe, key):
        out_holder[key] = _read_capped(pipe, STREAM_CAP)

    t_out = threading.Thread(target=reader, args=(proc.stdout, "out"))
    t_err = threading.Thread(target=reader, args=(proc.stderr, "err"))
    t_out.start(); t_err.start()

    timed_out = False
    try:
        proc.wait(timeout=exec_timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # Kill the CONTAINER, not just the docker CLI: `docker rm -f` on the name.
        subprocess.run([docker_bin, "rm", "-f", name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    t_out.join(); t_err.join()
    stdout, out_trunc = out_holder.get("out", (b"", False))
    stderr, err_trunc = out_holder.get("err", (b"", False))
    return {
        "exit_code": proc.returncode,
        "stdout": stdout.decode(errors="replace"),
        "stdout_truncated": out_trunc,
        "stderr": stderr.decode(errors="replace"),
        "stderr_truncated": err_trunc,
        "timed_out": timed_out,
        "duration_s": round(time.monotonic() - start, 2),
    }


def _dispatch(agent: str, command: str, network: str, requested_timeout: int | None) -> tuple[int, dict]:
    """Queue, spawn, and reap one container.

    `network` is supplied by the CALLING HANDLER (i.e. by which endpoint the
    request hit) and is never inferred from `command`.
    """
    exec_timeout = clamp_timeout(requested_timeout, CONFIG["default_timeout"], CONFIG["max_timeout"])
    # Queue wait is bounded separately: bridge_total - exec, so a queued call
    # fails fast with "sandbox busy" rather than blocking to the outer timeout.
    queue_wait = max(1, CONFIG.get("bridge_total_timeout", 540) - exec_timeout)
    if not SEM.acquire(timeout=queue_wait):
        with RUNNING_LOCK:
            ahead = sum(RUNNING.values())
        return 503, {"error": f"sandbox busy, {ahead} ahead — try again shortly"}

    name = f"claub-exec-{agent}-{uuid.uuid4().hex[:12]}"
    argv = build_docker_argv(agent, command, CONFIG, network, name)
    with RUNNING_LOCK:
        RUNNING[agent] = RUNNING.get(agent, 0) + 1
    try:
        result = run_container(argv, name, exec_timeout, CONFIG.get("docker_bin", "docker"))
        return 200, result
    finally:
        with RUNNING_LOCK:
            RUNNING[agent] = max(0, RUNNING.get(agent, 1) - 1)
        SEM.release()


def _check_agent(agent: str) -> tuple[int, dict] | None:
    try:
        validate_agent(agent, list(CONFIG.get("agents", {}).keys()))
    except ValueError:
        return 404, {"error": "unknown agent"}
    return None


def handle_exec(agent: str, payload: dict) -> tuple[int, dict]:
    """/exec — arbitrary command, ALWAYS --network none. No exceptions, no
    inspection of the command to decide otherwise."""
    bad = _check_agent(agent)
    if bad:
        return bad
    return _dispatch(agent, payload.get("command", ""), "none", payload.get("timeout"))


def handle_install(agent: str, payload: dict) -> tuple[int, dict]:
    """/install — package names ONLY. The networked path never accepts a
    command: any `command` key in the payload is simply never read, and the
    bridge builds the argv itself from validated names."""
    bad = _check_agent(agent)
    if bad:
        return bad
    try:
        command = build_install_command(agent, payload.get("packages") or [])
    except ValueError as e:
        return 400, {"error": str(e)}
    return _dispatch(agent, command, "bridge", CONFIG.get("max_timeout"))


def reap_orphans() -> None:
    docker = CONFIG.get("docker_bin", "docker")
    try:
        out = subprocess.check_output(
            [docker, "ps", "-aq", "--filter", "name=claub-exec-"], text=True)
        ids = [i for i in out.split() if i]
        if ids:
            subprocess.run([docker, "rm", "-f", *ids],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("reaped %d orphaned container(s)", len(ids))
    except Exception as e:
        log.warning("orphan reap failed: %s", e)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        # Paths are /exec/{agent} and /install/{agent}; unquote so encoded
        # traversal is caught by the agent-name regex.
        from urllib.parse import unquote
        parts = [unquote(p) for p in self.path.strip("/").split("/")]
        handlers = {"exec": handle_exec, "install": handle_install}
        if len(parts) == 2 and parts[0] in handlers:
            if not hmac.compare_digest(
                self.headers.get("X-Exec-Secret", ""), CONFIG.get("secret", "")
            ):
                self._send(401, {"error": "missing or invalid secret"})
                return
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            code, body = handlers[parts[0]](parts[1], payload)
            self._send(code, body)
            return
        self._send(404, {"error": "unknown path"})

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/status":
            with RUNNING_LOCK:
                self._send(200, {"running": dict(RUNNING)})
            return
        self._send(404, {"error": "unknown path"})

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    global CONFIG, SEM
    CONFIG = json.loads(Path(args.config).read_text())
    SEM = threading.Semaphore(int(CONFIG.get("max_concurrent", 1)))
    reap_orphans()
    host = CONFIG.get("listen_host", "127.0.0.1")
    port = int(CONFIG.get("listen_port", 9501))
    server = ThreadingHTTPServer((host, port), Handler)
    log.info("exec bridge listening on %s:%d", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
