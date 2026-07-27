"""Unit tests for the sandbox MCP pure helpers (no bridge/Docker required)."""
import importlib.util
import os

import pytest

_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "mcps", "sandbox")
_spec = importlib.util.spec_from_file_location(
    "sandbox_helpers", os.path.join(_DIR, "helpers.py")
)
_h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_h)


# --- validate_packages ---

def test_validate_packages_accepts_plain_and_pinned():
    assert _h.validate_packages(["rich", "numpy==2.0.0"]) == ["rich", "numpy==2.0.0"]


def test_validate_packages_rejects_flag_injection():
    with pytest.raises(ValueError):
        _h.validate_packages(["--index-url=http://evil"])


@pytest.mark.parametrize("bad", ["-e", "--pre", ".", "-", "foo."])
def test_validate_packages_rejects_option_flags_and_local_build(bad):
    with pytest.raises(ValueError):
        _h.validate_packages([bad])


def test_validate_packages_rejects_space_smuggling():
    with pytest.raises(ValueError):
        _h.validate_packages(["rich --upgrade"])


def test_validate_packages_rejects_empty_list():
    with pytest.raises(ValueError):
        _h.validate_packages([])


def test_mcp_does_not_build_install_commands():
    # The MCP forwards NAMES to /install; the bridge builds the argv. If this
    # ever gains a build_install_command, the trust boundary has drifted.
    assert not hasattr(_h, "build_install_command")


# --- truncate_tail ---

def test_truncate_tail_under_limit_unchanged():
    assert _h.truncate_tail("hello", 4000) == "hello"


def test_truncate_tail_over_limit_keeps_tail_and_marks():
    text = "".join(str(i % 10) for i in range(5000))
    out = _h.truncate_tail(text, 4000)
    assert len(out) <= 4000 + 80          # marker adds a little
    assert out.endswith(text[-100:])      # tail preserved
    assert "truncated" in out.lower()


# --- server-level tests (bridge mocked; no Docker, no live bridge) ---

import json
import sys
from unittest.mock import MagicMock


def _load_server(monkeypatch, agent="leetcode-coach"):
    """Import mcps/sandbox/server.py fresh with env set and httpx stubbed."""
    monkeypatch.setenv("CLAUB_AGENT_NAME", agent)
    monkeypatch.setenv("EXEC_BRIDGE_SECRET", "s3cret")
    monkeypatch.setenv("EXEC_BRIDGE_URL", "http://bridge:9501")
    spec = importlib.util.spec_from_file_location(
        "sandbox_server", os.path.join(_DIR, "server.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sandbox_server"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_run_posts_command_and_returns_json(monkeypatch):
    srv = _load_server(monkeypatch)
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["payload"] = json
        captured["command"] = json["command"]
        captured["secret"] = headers.get("X-Exec-Secret")
        resp = MagicMock()
        resp.json.return_value = {"exit_code": 0, "stdout": "hi", "stdout_truncated": False,
                                  "stderr": "", "stderr_truncated": False,
                                  "timed_out": False, "duration_s": 0.1}
        resp.raise_for_status.return_value = None
        return resp

    monkeypatch.setattr(srv.httpx, "post", fake_post)
    out = json.loads(srv.run("echo hi"))
    assert out["exit_code"] == 0 and out["stdout"] == "hi"
    assert captured["url"].endswith("/exec/leetcode-coach")   # agent from env, not param
    assert captured["command"] == "echo hi"
    assert captured["secret"] == "s3cret"


def test_run_truncates_long_stdout(monkeypatch):
    srv = _load_server(monkeypatch)
    big = "z" * 9000

    def fake_post(url, json, headers, timeout):
        resp = MagicMock()
        resp.json.return_value = {"exit_code": 0, "stdout": big, "stdout_truncated": True,
                                  "stderr": "", "stderr_truncated": False,
                                  "timed_out": False, "duration_s": 0.1}
        resp.raise_for_status.return_value = None
        return resp

    monkeypatch.setattr(srv.httpx, "post", fake_post)
    out = json.loads(srv.run("flood"))
    assert len(out["stdout"]) < 9000 and "truncated" in out["stdout"].lower()


def test_run_bridge_down_gives_actionable_error(monkeypatch):
    srv = _load_server(monkeypatch)

    def fake_post(*a, **k):
        raise srv.httpx.ConnectError("refused")

    monkeypatch.setattr(srv.httpx, "post", fake_post)
    out = json.loads(srv.run("echo hi"))
    assert out["exit_code"] != 0
    assert "bridge" in out["error"].lower() and "not running" in out["error"].lower()


def test_install_rejects_flag_without_calling_bridge(monkeypatch):
    srv = _load_server(monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(srv.httpx, "post", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    out = json.loads(srv.install(["--index-url=http://evil"]))
    assert out["exit_code"] != 0 and called["n"] == 0
    assert "invalid" in out["error"].lower()


def test_install_posts_names_only_to_install_endpoint(monkeypatch):
    srv = _load_server(monkeypatch)
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["payload"] = json
        resp = MagicMock()
        resp.json.return_value = {"exit_code": 0, "stdout": "", "stdout_truncated": False,
                                  "stderr": "", "stderr_truncated": False,
                                  "timed_out": False, "duration_s": 0.1}
        resp.raise_for_status.return_value = None
        return resp

    monkeypatch.setattr(srv.httpx, "post", fake_post)
    srv.install(["rich"])
    assert captured["url"].endswith("/install/leetcode-coach")
    # NAMES ONLY — the MCP must never send a command on the networked path.
    assert captured["payload"] == {"packages": ["rich"]}
    assert "command" not in captured["payload"]


def test_missing_agent_name_raises(monkeypatch):
    monkeypatch.delenv("CLAUB_AGENT_NAME", raising=False)
    monkeypatch.setenv("EXEC_BRIDGE_SECRET", "s")
    spec = importlib.util.spec_from_file_location(
        "sandbox_server_noenv", os.path.join(_DIR, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    with pytest.raises(RuntimeError, match="CLAUB_AGENT_NAME"):
        spec.loader.exec_module(mod)
