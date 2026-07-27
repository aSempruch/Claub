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
