# Claude Code Sandbox Investigation (2026-03-24)

Investigation into whether Claude Code's OS-level sandbox (bubblewrap) can run inside the Claub Docker container to protect agent credentials from prompt injection attacks.

## Background

The Claub bot spawns Claude Code CLI agent processes inside a Docker container. Each agent can execute Bash commands, read files, and fetch web content. The primary asset to protect is `~/.claude/.credentials.json` (OAuth tokens for Claude API).

Previously (pre-Docker era), the bot used Claude Code's sandbox feature on macOS with Seatbelt. The old config from `settings.json` (commit `9e2f20c`):

```json
{
  "sandbox": {
    "enabled": true,
    "autoAllowBashIfSandboxed": false,
    "allowUnsandboxedCommands": false,
    "filesystem": {
      "allowWrite": [
        "/private/tmp/claub",
        "/Users/you/.claub/workspaces",
        "/Users/you/.claub/home/.cache/uv",
        "/Users/you/.claub/home/.local/share/uv"
      ],
      "denyRead": [
        "/Users/you/Desktop",
        "/Users/you/Documents",
        "/Users/you/.claude",
        "/Users/you/.ssh",
        "/Users/you/.aws",
        "/Users/you/.gnupg",
        "/Users/you/.config/gh",
        "/Users/you/.netrc",
        "/Users/you/.npmrc",
        "/Users/you/.zshrc",
        "/Users/you/.zsh_history",
        "/Users/you/.bash_history",
        "/Users/you/Library/Keychains"
      ]
    }
  }
}
```

This was removed in commit `f389f9a` during Docker migration since Docker provides its own isolation.

## How Claude Code's Sandbox Works on Linux

- Uses **bubblewrap (bwrap)** to create mount/PID/network namespaces around tool command execution
- The CLI process itself reads credentials normally for API auth
- Tool commands (Bash, Read, Write) run inside the bwrap sandbox which restricts filesystem access
- Requires `bwrap` and `socat` binaries installed
- On macOS, uses Seatbelt instead (no bwrap needed)

Key distinction: the sandbox wraps **tool executions**, not the CLI process. So Claude CLI can authenticate with credentials, but an agent running `cat ~/.claude/.credentials.json` via Bash gets blocked.

## Docker Requirements for bwrap

bwrap needs Linux namespace and mount capabilities that Docker's defaults block. We tested all 16 combinations of four Docker security relaxations.

### Grid Search Results

| Configuration | bwrap (with --unshare-net) | bwrap (without --unshare-net) |
|---|:---:|:---:|
| No relaxations (baseline) | FAIL — `Creating new namespace failed` | — |
| SYS_ADMIN only | FAIL — `loopback: Failed RTM_NEWADDR` | — |
| SYS_ADMIN + seccomp:unconfined | FAIL — `loopback: Failed RTM_NEWADDR` | — |
| SYS_ADMIN + apparmor:unconfined | FAIL — `loopback: Failed RTM_NEWADDR` | — |
| SYS_ADMIN + seccomp:unconfined + apparmor:unconfined | FAIL — `loopback: Failed RTM_NEWADDR` | **PASS** |
| SYS_ADMIN + NET_ADMIN (no seccomp, no apparmor) | FAIL — `Failed to make / slave` | — |
| SYS_ADMIN + NET_ADMIN + seccomp:unconfined (no apparmor) | FAIL — `Failed to make / slave` | — |
| SYS_ADMIN + NET_ADMIN + apparmor:unconfined (no seccomp) | FAIL — `pivot_root: Operation not permitted` | — |
| SYS_ADMIN + NET_ADMIN + seccomp:unconfined + apparmor:unconfined | **PASS** | **PASS** |
| Custom seccomp + apparmor:unconfined + SYS_ADMIN + NET_ADMIN | **PASS** | **PASS** |

### What Each Relaxation Does

| Relaxation | Why bwrap Needs It |
|---|---|
| **SYS_ADMIN** | Kernel requires this capability for namespace creation (`clone` with `CLONE_NEWUSER`, `CLONE_NEWNS`, etc.) |
| **seccomp (custom or unconfined)** | Docker's default seccomp profile blocks `mount()` with `MS_SLAVE` propagation and `pivot_root()` |
| **apparmor:unconfined** | Colima VM's AppArmor profile independently blocks mount propagation (`--make-rslave`) |
| **NET_ADMIN** | Only needed for `--unshare-net` — bwrap's network namespace requires loopback interface setup |

### Minimal Working Config

All four are required. We built a custom seccomp profile (`seccomp-bwrap.json`) that only adds 8 syscalls instead of `seccomp:unconfined`:

```yaml
security_opt:
  - seccomp=seccomp-bwrap.json  # Docker defaults + clone, clone3, mount, pivot_root, setdomainname, sethostname, umount2, unshare
  - apparmor:unconfined
cap_add:
  - SYS_ADMIN
  - NET_ADMIN
```

## enableWeakerNestedSandbox

Claude Code has a setting specifically for Docker: `sandbox.enableWeakerNestedSandbox: true`. It skips `--proc /proc` (fresh proc mount) but still uses bwrap for everything else including `--unshare-net`. This means it provides no reduction in Docker privilege requirements — all four relaxations are still needed. The only thing it weakens is sandbox security (host `/proc` visible inside sandbox). **No benefit for our use case.**

## Sandbox Test Results (When Working)

With the full Docker relaxations and sandbox enabled, the following was verified:

| Test | Expected | Result |
|---|---|---|
| Write `./inside.txt` (workspace) | SUCCEED | **SUCCEED** |
| Write `/root/outside.txt` | BLOCKED | **BLOCKED** — "Output redirection blocked" |
| Write `/tmp/outside.txt` | BLOCKED | SUCCEED — `/tmp` is implicitly allowed by CLI |
| `cat /root/.claude/.credentials.json` | BLOCKED | **BLOCKED** — sandbox denied read |
| `ls /root/.claude/` | BLOCKED | **BLOCKED** — outside allowed directories |
| `cat /root/.claude/settings.json` | BLOCKED | **BLOCKED** |
| `uv init myproj` (in workspace) | SUCCEED | **SUCCEED** |
| Python `pathlib.Path("/root/esc.txt").write_text()` inside uv | BLOCKED | **BLOCKED** — "Read-only file system" |

The sandbox correctly enforces filesystem restrictions at the OS level. Bash commands, Python subprocesses, and even obfuscated path attempts are all blocked.

## Security Analysis: Two Options Compared

### Option A: No Sandbox, Tight Docker (CHOSEN)

```yaml
# No security_opt, no cap_add — Docker defaults
volumes:
  - claude-home:/root/.claude
  - ~/docker/claub:/claub
```

### Option B: Agent Sandbox, Loose Docker

```yaml
security_opt:
  - seccomp=seccomp-bwrap.json
  - apparmor:unconfined
cap_add:
  - SYS_ADMIN
  - NET_ADMIN
```

### Prompt Injection Threat Analysis

Assumes an agent is prompt-injected (e.g., via malicious content in WebFetch) and is actively malicious.

| Attack Vector | Option A (No Sandbox) | Option B (bwrap Sandbox) | Severity |
|---|---|---|---|
| **Credential exfiltration** (credentials.json) | VULNERABLE — `cat` bypasses `denyRead` | BLOCKED — bwrap enforces at OS level | Critical |
| **Env var leakage** (DISCORD_BOT_TOKEN) | VULNERABLE — `printenv` | VULNERABLE — env vars propagate into sandbox | High |
| **Read other agents' prompts/memory** | VULNERABLE — all workspaces accessible | BLOCKED — sandbox restricts to own workspace | Medium |
| **Write to other agents' workspaces** | VULNERABLE — cross-agent memory poisoning possible | BLOCKED — `allowWrite` restricted | High |
| **Modify config/settings files** | VULNERABLE | BLOCKED | High |
| **Network exfiltration** | VULNERABLE — unrestricted outbound | VULNERABLE — network not sandboxed (can exfil env vars but not credentials) | Critical/Medium |
| **OS-level persistence** (cron) | VULNERABLE | BLOCKED | Medium |
| **MCP schedule spoofing** | VULNERABLE — no auth on MCP server | VULNERABLE — same | Low |
| **Container escape** | Very unlikely — default Docker hardening | Unlikely but larger surface — SYS_ADMIN enables known escape techniques | Critical |

### Broader Security Threat Analysis

| Risk Domain | Option A | Option B |
|---|---|---|
| **Container escape** | Low — default seccomp/AppArmor block known techniques | Medium-High — SYS_ADMIN + mount + no AppArmor enables cgroup/namespace escapes |
| **Supply chain attacks** | Contained to container internals | Potential container escape to VM via SYS_ADMIN |
| **Future Docker/runc CVEs** | Many exploits blocked by missing capabilities | Capabilities provide exactly the primitives most exploits need |
| **Credential volume exposure** | Equal risk | Equal risk |
| **Network attack surface** | Minimal | Slightly larger (NET_ADMIN) |
| **Operational footguns** | Fewer — defaults are safe | More — capabilities propagate if more services added |
| **Inner agent sandboxing** | None | bwrap restricts agent filesystem access |

### Core Tension

- **Option B protects better against prompt injection** (high-probability, daily exposure via WebFetch)
- **Option A protects better against container escape and supply chain attacks** (low-probability but catastrophic)
- The Colima VM provides a meaningful additional boundary that reduces container escape impact

### Decision: Option A (No Sandbox)

Rationale: Avoid granting Bash permissions to agents entirely. Without Bash, the prompt injection credential exfiltration vector is eliminated at the application layer. The `denyRead` limitation (only blocks Read tool, not Bash) is irrelevant if Bash isn't allowed. Docker's default hardening is preserved, protecting against supply chain and runtime CVE threats.

## Recommendations Regardless of Option

These findings apply to both configurations and should be addressed:

### 1. Strip DISCORD_BOT_TOKEN from Agent Environment (Priority: Immediate)

In `claude_process.py`, the `_env()` method copies `os.environ` which includes the bot token. Fix:

```python
def _env(self) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("DISCORD_BOT_TOKEN", None)
    if self.agent_name:
        env["CLAUB_AGENT_NAME"] = self.agent_name
    return env
```

### 2. Add Auth to MCP Schedule Server (Priority: Short-term)

The MCP server on `localhost:9400` trusts the `X-Agent-Name` header with no validation. Any process can spoof agent identity. Generate a per-agent secret token at process startup and validate it server-side.

### 3. Restrict Network Egress (Priority: Medium-term)

Neither option restricts outbound network. Consider Docker network `internal: true` with a proxy for allowed traffic, or iptables rules.

### 4. Cross-Agent Memory Poisoning (Priority: Awareness)

Without sandbox, any agent can write to another agent's `memory/index.md`. The global CLAUDE.md mandates every agent reads this file on startup. This is a guaranteed injection chain if an agent is compromised. Mitigations:
- Don't grant Bash/Write access to untrusted agents
- Consider per-agent Linux users in the container for workspace isolation

## Reference: Custom Seccomp Profile

The custom seccomp profile (`seccomp-bwrap.json`) was built by taking Docker's default profile and adding one entry that unconditionally allows the 8 bwrap syscalls: `clone`, `clone3`, `mount`, `pivot_root`, `setdomainname`, `sethostname`, `umount2`, `unshare`. The `clone3` ERRNO entry from Docker's default was also removed to avoid conflict. This file is kept in the repo for reference but is not used in the current configuration.

## Reference: Useful Commands for Future Testing

```bash
# Test bwrap inside container
docker exec claude-claub-1 bwrap --ro-bind / / --dev /dev --proc /proc --unshare-net -- echo "OK"

# Run Claude CLI with sandbox settings interactively
docker exec -it claude-claub-1 bash -c 'cd /claub/workspaces/sandbox-test && claude --permission-mode acceptEdits --settings /tmp/sandbox-settings.json --no-session-persistence'

# One-shot sandbox test
docker exec claude-claub-1 bash -c 'cd /claub/workspaces/sandbox-test && claude -p "try to cat /root/.claude/.credentials.json" --permission-mode acceptEdits --settings /tmp/sandbox-settings.json --no-session-persistence'
```
