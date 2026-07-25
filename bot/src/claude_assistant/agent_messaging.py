"""FastMCP servers exposing agent-to-agent messaging tools.

Each agent gets its *own* server, mounted at ``/agents/{name}/mcp``, exposing
one ``message_agent_{peer}`` tool per agent it can reach. The sender's identity
comes from the mount path (not a header), and the peer roster is visible in the
tool list — no lookup call needed before messaging. All servers share one
wait-for graph so cycle detection spans the whole instance.

The ``deliver`` / ``get_live_process`` callables are injected so the Discord bot
and the debug CLI can wire different backends (production process registry +
SessionStore vs. isolated debug processes).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import fastmcp
from starlette.applications import Starlette
from starlette.routing import Mount

from claude_assistant.claude_process import AuthenticationError
from claude_assistant.config import reachable_agents

log = logging.getLogger(__name__)

DeliverFn = Callable[[str, str], Awaitable[str]]
# Returns the live AgentProcess (typed loosely — only awaiting_agent_reply is touched)
GetProcessFn = Callable[[str], Any]

MAX_WAIT_S = 900.0        # sender-side cap on waiting for the receiver's reply
MAX_CHAIN_DEPTH = 3       # max edges in a blocking A->B->C... chain


def _upstream_depth(
    waiting_on: dict[str, str], node: str, _seen: frozenset[str] = frozenset()
) -> int:
    """Longest chain of senders transitively waiting on *node*."""
    parents = [s for s, t in waiting_on.items() if t == node and s not in _seen]
    if not parents:
        return 0
    return 1 + max(_upstream_depth(waiting_on, p, _seen | {p}) for p in parents)


def _downstream_length(waiting_on: dict[str, str], node: str) -> int:
    """Length of the wait chain starting at *node* (who it waits on, etc.)."""
    length = 0
    seen: set[str] = set()
    while node in waiting_on and node not in seen:
        seen.add(node)
        node = waiting_on[node]
        length += 1
    return length


def check_wait_graph(
    waiting_on: dict[str, str],
    sender: str,
    target: str,
    max_depth: int = MAX_CHAIN_DEPTH,
) -> str | None:
    """Return an error string if sender->target would deadlock or nest too deep."""
    node: str | None = target
    visited: set[str] = set()
    while node is not None and node not in visited:
        if node == sender:
            return (
                f"Error: cannot message {target!r} — they are waiting on a reply "
                f"from you (directly or through a chain of agents). Answer the "
                f"pending message instead of sending a new one."
            )
        visited.add(node)
        node = waiting_on.get(node)
    depth = (
        _upstream_depth(waiting_on, sender)
        + 1
        + _downstream_length(waiting_on, target)
    )
    if depth > max_depth:
        return f"Error: message chain would be {depth} agents deep (max {max_depth})."
    return None


def _consume_result(task: asyncio.Task) -> None:
    """Log-and-swallow callback for deliveries whose result nobody awaits."""
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        log.warning("background agent delivery failed: %s", exc)
    else:
        log.info("background agent delivery completed (result discarded)")


async def _send_message(
    sender: str,
    to: str,
    message: str,
    expect_reply: bool,
    *,
    agent_groups: dict[str, list[str]],
    waiting_on: dict[str, str],
    deliver: DeliverFn,
    get_live_process: GetProcessFn,
    max_wait_s: float,
) -> str:
    reachable = reachable_agents(agent_groups, sender)
    if not reachable:
        return (
            f"Error: agent {sender!r} is not in any agent group — "
            f"agent messaging is not enabled for you."
        )
    if to == sender:
        return "Error: cannot send a message to yourself."
    if to not in reachable:
        return f"Error: {to!r} is not reachable. You can message: {sorted(reachable)}."
    graph_error = check_wait_graph(waiting_on, sender, to)
    if graph_error:
        return graph_error

    if not expect_reply:
        content = f"[notification from agent {sender} — no reply expected] {message}"
        task = asyncio.ensure_future(deliver(to, content))
        task.add_done_callback(_consume_result)
        log.info("agent notification %s -> %s (len=%d)", sender, to, len(message))
        return f"Message sent to {to} (no reply requested)."

    content = f"[message from agent {sender}] {message}"
    waiting_on[sender] = to
    sender_process = get_live_process(sender)
    if sender_process is not None:
        sender_process.awaiting_agent_reply = True
    log.info("agent message %s -> %s (len=%d)", sender, to, len(message))
    delivery = asyncio.ensure_future(deliver(to, content))
    try:
        # shield: never cancel a send mid-stream — that would desync the
        # receiver's stream-json pipe. On timeout it finishes in background.
        return await asyncio.wait_for(asyncio.shield(delivery), timeout=max_wait_s)
    except asyncio.TimeoutError:
        delivery.add_done_callback(_consume_result)
        return (
            f"Error: {to} did not reply within {int(max_wait_s // 60)} minutes. "
            f"The message was delivered and they may still be working, but the "
            f"reply has been discarded."
        )
    except AuthenticationError:
        return f"Error: {to} could not run — Claude authentication expired."
    except Exception as exc:
        return f"Error: delivering to {to} failed: {exc}"
    finally:
        waiting_on.pop(sender, None)
        if sender_process is not None:
            sender_process.awaiting_agent_reply = False


def _tool_description(target: str, description: str) -> str:
    """Docstring for ``message_agent_{target}``, seeded with who *target* is."""
    about = f"\n{description.strip()}\n" if description.strip() else ""
    return (
        f"Send a message to the {target} agent and wait for its reply.\n"
        f"{about}"
        "\nThe receiving agent processes your message in its own session (with "
        "full memory and context) and its response is returned to you. This can "
        "take minutes if the agent is busy or the task is involved.\n"
        "\nArgs:\n"
        "    message: The message. Include all context the agent needs — it "
        "cannot see your conversation.\n"
        "    expect_reply: When False, deliver as a fire-and-forget notification "
        "and return immediately (the agent's response is discarded).\n"
    )


def create_messaging_server(
    sender: str,
    agent_groups: dict[str, list[str]],
    deliver: DeliverFn,
    get_live_process: GetProcessFn,
    descriptions: dict[str, str] | None = None,
    max_wait_s: float = MAX_WAIT_S,
    waiting_on: dict[str, str] | None = None,
) -> fastmcp.FastMCP:
    """Build *sender*'s messaging server: one tool per agent it can reach.

    An agent in no group gets a server with no tools — still mounted, so its
    MCP connection succeeds instead of 404ing at handshake.

    ``deliver(agent, content)`` must implement get-or-start, restart-and-retry,
    and (in production) session persistence — see AssistantBot / debug CLI.
    ``waiting_on`` is the shared wait-for graph; pass the same dict to every
    agent's server so cycle detection sees the whole instance.
    """
    mcp = fastmcp.FastMCP(name=f"claub-agent-messaging-{sender}")
    graph = {} if waiting_on is None else waiting_on
    descs = descriptions or {}

    def _make_tool(target: str):
        async def message_agent(message: str, expect_reply: bool = True) -> str:
            return await _send_message(
                sender, target, message, expect_reply,
                agent_groups=agent_groups, waiting_on=graph,
                deliver=deliver, get_live_process=get_live_process,
                max_wait_s=max_wait_s,
            )

        return message_agent

    for target in sorted(reachable_agents(agent_groups, sender)):
        mcp.tool(
            _make_tool(target),
            name=f"message_agent_{target}",
            description=_tool_description(target, descs.get(target, "")),
        )
    return mcp


def create_messaging_servers(
    agent_names: Iterable[str],
    agent_groups: dict[str, list[str]],
    deliver: DeliverFn,
    get_live_process: GetProcessFn,
    descriptions: dict[str, str] | None = None,
    max_wait_s: float = MAX_WAIT_S,
) -> dict[str, fastmcp.FastMCP]:
    """One messaging server per agent, all sharing a single wait-for graph."""
    waiting_on: dict[str, str] = {}
    return {
        name: create_messaging_server(
            name, agent_groups, deliver, get_live_process,
            descriptions, max_wait_s, waiting_on,
        )
        for name in agent_names
    }


def combine_mcp_apps(schedules_app, messaging_apps: dict[str, Any]) -> Starlette:
    """Serve schedules at /mcp and each agent's messaging app at /agents/{name}/mcp.

    ``schedules_app`` may be None (the debug CLI runs messaging on its own).
    """
    apps = [*messaging_apps.values()] + ([schedules_app] if schedules_app else [])

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with contextlib.AsyncExitStack() as stack:
            for sub in apps:
                await stack.enter_async_context(sub.router.lifespan_context(sub))
            yield

    routes = [
        Mount(f"/agents/{name}", app=sub) for name, sub in messaging_apps.items()
    ]
    if schedules_app:
        routes.append(Mount("/", app=schedules_app))
    return Starlette(routes=routes, lifespan=lifespan)
