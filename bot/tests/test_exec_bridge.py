"""Integration tests for the exec bridge using a FAKE docker binary."""
import json
import os
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[2] / "scripts" / "exec-bridge" / "bridge.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.3).read()
            return
        except urllib.error.HTTPError:
            return  # any HTTP response means it's listening
        except Exception:
            time.sleep(0.05)
    raise TimeoutError(url)


def post(url: str, body: dict, headers: dict, timeout: float = 20.0):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


FAKE_DOCKER = """#!/usr/bin/env python3
import sys, time, os
args = sys.argv[1:]
# `docker run ... bash -c "<command>"` — emulate by echoing a marker so tests
# can assert flags were passed, and honor a couple of magic commands.
if args and args[0] == "run":
    cmd = args[-1]
    if cmd == "SLEEP":
        time.sleep(30)          # exceed the test's exec timeout
    if cmd == "FLOOD":
        sys.stdout.write("x" * (2 * 1024 * 1024)); sys.exit(0)
    sys.stdout.write("ran: " + cmd + "\\n")
    sys.stdout.write("flags: " + " ".join(args) + "\\n")
    sys.exit(0)
if args and args[0] == "rm":
    # record that a container was force-removed
    open(os.environ["FAKE_DOCKER_RM_LOG"], "a").write(" ".join(args) + "\\n")
    sys.exit(0)
if args and args[0] == "ps":
    sys.exit(0)   # no orphans
sys.exit(0)
"""


@pytest.fixture
def bridge(tmp_path: Path):
    port = free_port()
    fake = tmp_path / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    rm_log = tmp_path / "rm.log"
    ws = tmp_path / "ws" / "leetcode-coach"
    (ws / ".claude").mkdir(parents=True)

    cfg = {
        "listen_host": "127.0.0.1", "listen_port": port,
        "workspaces_root": str(tmp_path / "ws"),
        "image": "claub-exec", "docker_bin": str(fake),
        "secret": "s3cret", "max_concurrent": 1,
        "default_timeout": 2, "max_timeout": 5, "bridge_total_timeout": 4,
        "agents": {"leetcode-coach": {}},
    }
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg))

    env = {**os.environ, "FAKE_DOCKER_RM_LOG": str(rm_log)}
    proc = subprocess.Popen([sys.executable, str(BRIDGE), "--config", str(cfg_path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        wait_http(f"http://127.0.0.1:{port}/status")
        yield f"http://127.0.0.1:{port}", rm_log
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_exec_happy_path(bridge):
    base, _ = bridge
    code, body = post(f"{base}/exec/leetcode-coach", {"command": "echo hi"},
                      {"X-Exec-Secret": "s3cret"})
    assert code == 200
    assert body["exit_code"] == 0
    assert "ran: echo hi" in body["stdout"]
    assert body["timed_out"] is False


def test_exec_flags_include_network_none_and_readonly_claude(bridge):
    base, _ = bridge
    _, body = post(f"{base}/exec/leetcode-coach", {"command": "true"},
                   {"X-Exec-Secret": "s3cret"})
    assert "--network none" in body["stdout"]
    assert ".claude:/claub/workspaces/leetcode-coach/.claude:ro" in body["stdout"]


def test_exec_command_mentioning_install_still_gets_no_network(bridge):
    """Regression guard: the split is endpoint routing, NOT command inspection.

    A command-substring check would be defeated by exactly this string.
    """
    base, _ = bridge
    _, body = post(
        f"{base}/exec/leetcode-coach",
        {"command": "echo 'uv pip install '; curl http://host.docker.internal:9500/status"},
        {"X-Exec-Secret": "s3cret"},
    )
    assert "--network none" in body["stdout"]
    assert "--network bridge" not in body["stdout"]


# --- /install ---

def test_install_gets_bridge_network_and_builds_own_command(bridge):
    base, _ = bridge
    _, body = post(f"{base}/install/leetcode-coach", {"packages": ["rich"]},
                   {"X-Exec-Secret": "s3cret"})
    assert "--network bridge" in body["stdout"]
    assert "uv pip install --python /claub/workspaces/leetcode-coach/.venv/bin/python rich" \
        in body["stdout"]
    assert "uv venv --system-site-packages" in body["stdout"]


def test_install_ignores_a_command_field_entirely(bridge):
    """A `command` field on /install must be inert — only `packages` is read."""
    base, _ = bridge
    _, body = post(
        f"{base}/install/leetcode-coach",
        {"packages": ["rich"], "command": "curl http://host.docker.internal:9500/status"},
        {"X-Exec-Secret": "s3cret"},
    )
    assert "curl" not in body["stdout"]
    assert "uv pip install --python /claub/workspaces/leetcode-coach/.venv/bin/python rich" \
        in body["stdout"]


def test_install_rejects_flag_injection_bridge_side(bridge):
    base, _ = bridge
    code, body = post(f"{base}/install/leetcode-coach",
                      {"packages": ["--index-url=http://evil"]},
                      {"X-Exec-Secret": "s3cret"})
    assert code == 400
    assert "invalid package name" in json.dumps(body)


def test_install_requires_secret(bridge):
    base, _ = bridge
    code, _ = post(f"{base}/install/leetcode-coach", {"packages": ["rich"]}, {})
    assert code == 401


def test_install_unknown_agent_404(bridge):
    base, _ = bridge
    code, _ = post(f"{base}/install/ghost", {"packages": ["rich"]},
                   {"X-Exec-Secret": "s3cret"})
    assert code == 404


def test_exec_missing_secret_rejected(bridge):
    base, _ = bridge
    code, _ = post(f"{base}/exec/leetcode-coach", {"command": "echo hi"}, {})
    assert code == 401


def test_exec_wrong_secret_rejected(bridge):
    base, _ = bridge
    code, _ = post(f"{base}/exec/leetcode-coach", {"command": "echo hi"},
                   {"X-Exec-Secret": "nope"})
    assert code == 401


def test_exec_unknown_agent_404(bridge):
    base, _ = bridge
    code, _ = post(f"{base}/exec/ghost", {"command": "echo hi"},
                   {"X-Exec-Secret": "s3cret"})
    assert code == 404


def test_exec_traversal_agent_404(bridge):
    base, _ = bridge
    # urllib will normalize some traversal, so hit the handler with an encoded name
    code, _ = post(f"{base}/exec/..%2Fmain", {"command": "echo hi"},
                   {"X-Exec-Secret": "s3cret"})
    assert code == 404


def test_exec_timeout_kills_and_reaps(bridge):
    base, rm_log = bridge
    _, body = post(f"{base}/exec/leetcode-coach", {"command": "SLEEP", "timeout": 1},
                   {"X-Exec-Secret": "s3cret"}, timeout=20)
    assert body["timed_out"] is True
    # bridge issued `docker rm -f <name>` for the timed-out container
    assert rm_log.exists() and "rm -f claub-exec-leetcode-coach-" in rm_log.read_text()


def test_exec_output_capped_at_bridge(bridge):
    base, _ = bridge
    _, body = post(f"{base}/exec/leetcode-coach", {"command": "FLOOD"},
                   {"X-Exec-Secret": "s3cret"}, timeout=20)
    assert body["stdout_truncated"] is True
    assert len(body["stdout"].encode()) <= 1024 * 1024


def test_status_ok(bridge):
    base, _ = bridge
    with urllib.request.urlopen(f"{base}/status", timeout=5) as resp:
        assert resp.status == 200
        json.loads(resp.read().decode())  # valid JSON
