"""Adversarial acceptance tests — assert the sandbox FAILS to escape.

Gated: set CLAUB_SANDBOX_INTEGRATION=1 and have (a) the claub-exec image built,
(b) the exec bridge running, (c) EXEC_BRIDGE_SECRET + EXEC_BRIDGE_URL exported
to point at it, and (d) the calling agent name (default leetcode-coach) in the
bridge allowlist with a real host workspace.

Run: CLAUB_SANDBOX_INTEGRATION=1 EXEC_BRIDGE_SECRET=... \
     uv run --extra dev pytest tests/test_sandbox_adversarial.py -v

Three tests are load-bearing — if any fails, the design is broken and the
sandbox must not ship: the .claude/settings.local.json write must be refused,
`run` must have no network reachability, and a command that merely mentions the
install shape must still get no network.

Two deviations from the original plan's assertions, both found by probing the
real sandbox rather than assuming:

  * Shell errors land on STDERR, not stdout. `echo x > /etc/probe 2>&1` does not
    capture them, because bash reports the redirection failure before the `2>&1`
    takes effect. These tests assert against combined output.
  * The network probe uses a raw socket via python3, not `curl`. curl is NOT
    installed in claub-exec, so a curl-based test passes with "command not
    found" whether or not the network is actually blocked — it proves nothing.
    The socket probe also covers 192.168.5.2 directly: Lima proxies
    host.docker.internal through that address, so name-based blocking alone
    would be insufficient (the design rejected that approach explicitly).
"""
import base64
import os

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("CLAUB_SANDBOX_INTEGRATION") != "1",
    reason="requires built image + running bridge",
)

AGENT = os.environ.get("CLAUB_SANDBOX_AGENT", "leetcode-coach")
URL = os.environ.get("EXEC_BRIDGE_URL", "http://127.0.0.1:9501").rstrip("/")
SECRET = os.environ.get("EXEC_BRIDGE_SECRET", "")


def _run(command: str, timeout: int = 60) -> dict:
    resp = httpx.post(f"{URL}/exec/{AGENT}", json={"command": command, "timeout": timeout},
                      headers={"X-Exec-Secret": SECRET}, timeout=120)
    resp.raise_for_status()
    return resp.json()


def _combined(out: dict) -> str:
    """stdout + stderr — shell redirection errors are reported on stderr."""
    return out.get("stdout", "") + out.get("stderr", "")


def _py(source: str) -> str:
    """Run python source in the sandbox without shell-quoting hazards."""
    b64 = base64.b64encode(source.encode()).decode()
    return f"echo {b64} | base64 -d | python3 -"


def _install(payload: dict) -> httpx.Response:
    return httpx.post(f"{URL}/install/{AGENT}", json=payload,
                      headers={"X-Exec-Secret": SECRET}, timeout=300)


def test_no_claude_credentials():
    out = _run("cat /root/.claude/.credentials.json")
    assert "No such file" in _combined(out)
    assert "sk-" not in _combined(out)


def test_no_other_agent_workspace():
    out = _run("ls /claub/workspaces/main")
    combined = _combined(out)
    assert "No such file" in combined or "cannot access" in combined


def test_no_bot_container_secrets_in_env():
    """The sandbox gets only the image ENV plus the bridge's six explicit -e
    flags — no .envrc value and nothing from the bot container.

    GPG_KEY is excluded deliberately: it is a python:3.12-slim image constant
    (the CPython release signing key), not a leaked credential, and it is the
    only thing a bare `env | grep -i key` matches.
    """
    out = _run("env | grep -iE 'token|key|secret' | grep -v '^GPG_KEY=' || true")
    assert out["stdout"].strip() == ""
    # Strongest form: the bridge's own secret must never reach the sandbox,
    # even though /install has network.
    if SECRET:
        assert SECRET not in _combined(_run("env"))


def test_cannot_write_outside_workspace():
    out = _run("echo x > /etc/probe")
    assert "Read-only file system" in _combined(out)


def test_cannot_write_claude_settings_local():
    # LOAD-BEARING: the self-escalation path. Must be a read-only filesystem error.
    out = _run(f"echo x > /claub/workspaces/{AGENT}/.claude/settings.local.json")
    assert "Read-only file system" in _combined(out)


NET_PROBE = """
import socket
targets = [("host.docker.internal", 9500), ("host.docker.internal", 9501),
           ("192.168.5.2", 9500), ("192.168.5.2", 9501), ("1.1.1.1", 53)]
for target in targets:
    s = socket.socket(); s.settimeout(5)
    try:
        s.connect(target); print("CONNECTED", target)
    except Exception as e:
        print("BLOCKED", target, type(e).__name__)
"""


def test_no_network_from_run():
    """LOAD-BEARING: run must not reach the playwright bridge, the exec bridge,
    any browser MCP port, the LAN, or the internet.

    Probes the raw Lima proxy IP as well as the hostname — binding host services
    to 127.0.0.1 does not protect them, so name-resolution failure alone would
    not prove containment.
    """
    out = _run(_py(NET_PROBE))
    assert "CONNECTED" not in out["stdout"]
    assert out["stdout"].count("BLOCKED") == 5


def test_exec_command_mentioning_install_shape_still_has_no_network():
    """LOAD-BEARING: asserts the network split lives in endpoint routing, not
    command inspection. A `"uv pip install " in command` check is defeated by
    exactly this string — it would take the networked path and reach the
    Playwright bridge.
    """
    out = _run("echo 'uv pip install '; " + _py(NET_PROBE))
    assert "CONNECTED" not in out["stdout"]
    assert "uv pip install" in out["stdout"]  # the decoy really did run


def test_install_ignores_a_command_field():
    """The networked endpoint must not accept anything executable. A `command`
    key is never read — only `packages`.
    """
    resp = _install({"packages": ["rich"],
                     "command": "echo PWNED > /claub/workspaces/%s/.pwned" % AGENT})
    assert resp.status_code == 200
    body = resp.json()
    assert "PWNED" not in body["stdout"]
    # And the side effect never happened.
    assert "No such file" in _combined(_run(f"cat /claub/workspaces/{AGENT}/.pwned"))


def test_install_rejects_flag_injection_bridge_side():
    """Rejected by BRIDGE-side validation — not merely by the MCP's copy."""
    resp = _install({"packages": ["--index-url=http://evil"]})
    assert resp.status_code == 400
    assert "invalid package name" in resp.text


# --- The /install network-egress class (two confirmed holes, now fixed) ---
#
# The invariant: no file writable by a network-none `run` may be executed by the
# networked `install`. Both tests below plant agent-authored code via `run`
# (which has no network) and then trigger `install` (which does), asserting the
# planted code never reaches the network.

NET_SETUP_PY = '''
import socket
r = "NET-FAIL"
try:
    s = socket.socket(); s.settimeout(5); s.connect(("192.168.5.2", 9501)); r = "NET-OK"
except Exception as e:
    r = "NET-FAIL-" + type(e).__name__
open("%s/INSTALL_NET_PROOF.txt", "w").write(r)
from setuptools import setup
setup(name="pwnpkg", version="0.0.0", py_modules=[])
'''


def test_install_local_build_flag_is_rejected():
    """Hole #1: `install(['-e', '.'])` would build the agent's own setup.py WITH
    network. The flag-shaped names must be rejected before any container runs.
    """
    ws = f"/claub/workspaces/{AGENT}"
    setup_b64 = base64.b64encode((NET_SETUP_PY % ws).encode()).decode()
    _run(f"rm -f {ws}/INSTALL_NET_PROOF.txt {ws}/setup.py; echo {setup_b64} | base64 -d > {ws}/setup.py")
    try:
        resp = _install({"packages": ["-e", "."]})
        assert resp.status_code == 400  # rejected, no container spawned
        # The setup.py never ran, so no proof file and no network contact.
        out = _run(f"cat {ws}/INSTALL_NET_PROOF.txt 2>&1 || echo NONE")
        assert "NET-OK" not in _combined(out)
    finally:
        _run(f"rm -f {ws}/setup.py {ws}/INSTALL_NET_PROOF.txt; rm -rf {ws}/pwnpkg.egg-info {ws}/build")


def test_install_does_not_execute_agent_venv_python():
    """Hole #2: a `.pth` in the agent-writable venv site-packages must NOT run
    during install. install uses the trusted system interpreter, so the venv
    (whose site-packages the agent controls) is never executed on the networked
    path.
    """
    ws = f"/claub/workspaces/{AGENT}"
    site = f"{ws}/.venv/lib/python3.12/site-packages"
    # Ensure a venv exists, then plant a network-phoning .pth in its site-packages.
    _install({"packages": ["rich"]})  # bootstraps the venv if absent
    pth_b64 = base64.b64encode(
        (f'import socket\n'
         f'try:\n'
         f'    s=socket.socket();s.settimeout(5);s.connect(("192.168.5.2",9501));open("{ws}/PTH_NET.txt","w").write("NET-OK")\n'
         f'except Exception as e:\n'
         f'    open("{ws}/PTH_NET.txt","w").write("NET-FAIL-"+type(e).__name__)\n').encode()
    ).decode()
    _run(f"rm -f {ws}/PTH_NET.txt; echo {pth_b64} | base64 -d > {site}/zzz_probe.pth")
    try:
        _install({"packages": ["networkx"]})  # networked install — must not run the .pth
        out = _run(f"cat {ws}/PTH_NET.txt 2>&1 || echo NONE")
        assert "NET-OK" not in _combined(out)
    finally:
        _run(f"rm -f {site}/zzz_probe.pth {ws}/PTH_NET.txt")


def test_workspace_is_writable():
    out = _run(f"echo ok > /claub/workspaces/{AGENT}/.sandbox-probe && "
               f"cat /claub/workspaces/{AGENT}/.sandbox-probe && "
               f"rm /claub/workspaces/{AGENT}/.sandbox-probe")
    assert out["stdout"].strip() == "ok"


def test_agent_authored_skills_still_writable_through_symlink():
    """The read-only .claude mount must not break skill authoring: .claude/skills
    is a symlink to ../.claude-skills on the writable parent mount.
    """
    out = _run(f"echo hi > /claub/workspaces/{AGENT}/.claude/skills/.probe && "
               f"cat /claub/workspaces/{AGENT}/.claude-skills/.probe && "
               f"rm /claub/workspaces/{AGENT}/.claude-skills/.probe")
    assert out["stdout"].strip() == "hi"


def test_compose_submount_is_replicated():
    """extra_mounts parity: shared-data exists inside the bot container via a
    compose bind, but the host workspace subdir is empty. Without the bridge's
    extra_mounts entry the sandbox silently sees nothing — a wrong-answer bug,
    not an error.
    """
    if AGENT != "leetcode-coach":
        pytest.skip("shared-data submount is leetcode-coach specific")
    out = _run(f"ls /claub/workspaces/{AGENT}/shared-data")
    assert "lps.yaml" in out["stdout"]


def test_bridge_rejects_missing_secret():
    resp = httpx.post(f"{URL}/exec/{AGENT}", json={"command": "echo x"}, timeout=30)
    assert resp.status_code == 401


def test_bridge_rejects_unknown_agent():
    resp = httpx.post(f"{URL}/exec/definitely-not-an-agent", json={"command": "echo x"},
                      headers={"X-Exec-Secret": SECRET}, timeout=30)
    assert resp.status_code == 404
