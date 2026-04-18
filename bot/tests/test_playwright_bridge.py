import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[2] / "scripts" / "playwright-bridge" / "bridge.py"


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
        except Exception:
            time.sleep(0.1)
    raise TimeoutError(url)


def post(url: str, timeout: float = 20.0) -> tuple[int, str]:
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@pytest.fixture
def bridge(tmp_path: Path):
    """Yield a running bridge with a stub command. Cleans up on teardown."""
    bridge_port = free_port()
    stub_port = free_port()

    stub = (
        "import http.server, socketserver, sys, os\n"
        "p = int(sys.argv[sys.argv.index('--port')+1])\n"
        "u = sys.argv[sys.argv.index('--user-data-dir')+1]\n"
        "os.makedirs(u, exist_ok=True)\n"
        "open(os.path.join(u, 'spawned.txt'), 'w').write('yes')\n"
        "srv = socketserver.TCPServer(('127.0.0.1', p), http.server.BaseHTTPRequestHandler)\n"
        "srv.serve_forever()\n"
    )

    cfg = {
        "listen_host": "127.0.0.1",
        "listen_port": bridge_port,
        "command_template": [
            sys.executable, "-c", stub,
            "--port", "{port}",
            "--user-data-dir", "{user_data_dir}",
        ],
        "agents": {
            "test-agent": {
                "port": stub_port,
                "user_data_dir": str(tmp_path / "profile"),
            },
        },
    }
    cfg_path = tmp_path / "bridge.json"
    cfg_path.write_text(json.dumps(cfg))

    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE), "--config", str(cfg_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        wait_http(f"http://127.0.0.1:{bridge_port}/status")
        yield {"port": bridge_port, "stub_port": stub_port, "profile": tmp_path / "profile"}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_unknown_agent_is_noop(bridge):
    code, _ = post(f"http://127.0.0.1:{bridge['port']}/start/other")
    assert code == 204


def test_start_and_stop(bridge):
    code, body = post(f"http://127.0.0.1:{bridge['port']}/start/test-agent")
    assert code == 200, body
    assert json.loads(body)["status"] == "started"
    assert (bridge["profile"] / "spawned.txt").read_text() == "yes"
    with socket.create_connection(("127.0.0.1", bridge["stub_port"]), timeout=1):
        pass

    code, body = post(f"http://127.0.0.1:{bridge['port']}/stop/test-agent")
    assert code == 200
    assert json.loads(body)["status"] == "stopped"

    time.sleep(0.3)
    with pytest.raises(OSError):
        with socket.create_connection(("127.0.0.1", bridge["stub_port"]), timeout=0.5):
            pass


def test_double_start_is_idempotent(bridge):
    code1, body1 = post(f"http://127.0.0.1:{bridge['port']}/start/test-agent")
    assert code1 == 200
    pid1 = json.loads(body1)["pid"]

    code2, body2 = post(f"http://127.0.0.1:{bridge['port']}/start/test-agent")
    assert code2 == 200
    assert json.loads(body2)["pid"] == pid1

    post(f"http://127.0.0.1:{bridge['port']}/stop/test-agent")


def test_stop_when_not_running(bridge):
    code, body = post(f"http://127.0.0.1:{bridge['port']}/stop/test-agent")
    assert code == 200
    assert json.loads(body)["status"] == "not-running"
