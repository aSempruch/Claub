"""Tests for the agent-to-agent messaging MCP server."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import fastmcp
import pytest

from claude_assistant.agent_messaging import (
    _send_message,
    check_wait_graph,
    combine_mcp_apps,
    create_messaging_server,
    create_messaging_servers,
)
from claude_assistant.claude_process import AuthenticationError

GROUPS = {"household": ["main", "journalist", "career"]}


# --- check_wait_graph ---


def test_empty_graph_allows():
    assert check_wait_graph({}, "a", "b") is None


def test_direct_cycle_rejected():
    # b is waiting on a; a's turn (processing b's message) tries to message b back
    err = check_wait_graph({"b": "a"}, "a", "b")
    assert err is not None and "Error" in err


def test_transitive_cycle_rejected():
    # a -> b -> c already in flight; c tries to message a
    err = check_wait_graph({"a": "b", "b": "c"}, "c", "a")
    assert err is not None and "Error" in err


def test_unrelated_edges_allowed():
    assert check_wait_graph({"x": "y"}, "a", "b") is None


def test_chain_at_depth_cap_allowed():
    # a -> b -> c in flight; c -> d makes a 3-edge chain: allowed at cap 3
    assert check_wait_graph({"a": "b", "b": "c"}, "c", "d", max_depth=3) is None


def test_chain_beyond_depth_cap_rejected():
    # a -> b -> c -> d in flight; d -> e would be 4 edges
    err = check_wait_graph({"a": "b", "b": "c", "c": "d"}, "d", "e", max_depth=3)
    assert err is not None and "deep" in err


def test_downstream_of_target_counts_toward_depth():
    # target b is already waiting on c and c on d (b -> c -> d);
    # adding a -> b makes 3 edges: allowed; with e -> a upstream it's 4: rejected
    assert check_wait_graph({"b": "c", "c": "d"}, "a", "b", max_depth=3) is None
    err = check_wait_graph({"b": "c", "c": "d", "e": "a"}, "a", "b", max_depth=3)
    assert err is not None


def _deps(**overrides):
    """Default kwargs for _send_message with a recording deliver stub."""
    calls: list[tuple[str, str]] = []

    async def deliver(name: str, content: str) -> str:
        calls.append((name, content))
        return f"reply from {name}"

    deps = dict(
        agent_groups=GROUPS,
        waiting_on={},
        deliver=deliver,
        get_live_process=lambda name: None,
        max_wait_s=5.0,
    )
    deps.update(overrides)
    return deps, calls


# --- authorization ---


@pytest.mark.asyncio
async def test_send_not_in_any_group():
    deps, _ = _deps()
    result = await _send_message("shopping", "main", "hi", True, **deps)
    assert result.startswith("Error") and "group" in result


@pytest.mark.asyncio
async def test_send_target_not_reachable():
    deps, _ = _deps()
    result = await _send_message("main", "shopping", "hi", True, **deps)
    assert result.startswith("Error") and "not reachable" in result
    assert "journalist" in result  # error lists reachable agents


@pytest.mark.asyncio
async def test_send_to_self_rejected():
    deps, _ = _deps()
    result = await _send_message("main", "main", "hi", True, **deps)
    assert result.startswith("Error")


# --- happy path ---


@pytest.mark.asyncio
async def test_send_returns_reply_and_formats_header():
    deps, calls = _deps()
    result = await _send_message("main", "journalist", "what's news?", True, **deps)
    assert result == "reply from journalist"
    assert calls == [("journalist", "[message from agent main] what's news?")]


@pytest.mark.asyncio
async def test_send_sets_and_clears_waiting_state():
    observed: dict[str, object] = {}
    proc = SimpleNamespace(awaiting_agent_reply=False)
    waiting: dict[str, str] = {}

    async def deliver(name: str, content: str) -> str:
        observed["waiting"] = dict(waiting)
        observed["flag"] = proc.awaiting_agent_reply
        return "ok"

    deps, _ = _deps(deliver=deliver, waiting_on=waiting,
                    get_live_process=lambda name: proc if name == "main" else None)
    await _send_message("main", "journalist", "hi", True, **deps)
    assert observed == {"waiting": {"main": "journalist"}, "flag": True}
    assert waiting == {} and proc.awaiting_agent_reply is False


@pytest.mark.asyncio
async def test_send_cycle_rejected_without_delivery():
    deps, calls = _deps(waiting_on={"journalist": "main"})
    result = await _send_message("main", "journalist", "hi", True, **deps)
    assert result.startswith("Error") and calls == []


# --- timeout ---


@pytest.mark.asyncio
async def test_send_timeout_returns_error_but_delivery_completes():
    done = asyncio.Event()

    async def slow_deliver(name: str, content: str) -> str:
        await asyncio.sleep(0.1)
        done.set()
        return "late"

    deps, _ = _deps(deliver=slow_deliver, max_wait_s=0.01)
    result = await _send_message("main", "journalist", "hi", True, **deps)
    assert result.startswith("Error") and "did not reply" in result
    await asyncio.wait_for(done.wait(), timeout=1.0)  # shielded: still completed
    assert deps["waiting_on"] == {}


# --- failures ---


@pytest.mark.asyncio
async def test_send_delivery_failure_returns_error():
    async def bad_deliver(name: str, content: str) -> str:
        raise RuntimeError("boom")

    deps, _ = _deps(deliver=bad_deliver)
    result = await _send_message("main", "journalist", "hi", True, **deps)
    assert result.startswith("Error") and "boom" in result
    assert deps["waiting_on"] == {}


@pytest.mark.asyncio
async def test_send_auth_failure_returns_error():
    async def auth_fail(name: str, content: str) -> str:
        raise AuthenticationError("expired")

    deps, _ = _deps(deliver=auth_fail)
    result = await _send_message("main", "journalist", "hi", True, **deps)
    assert result.startswith("Error") and "authentication" in result.lower()


# --- fire and forget ---


@pytest.mark.asyncio
async def test_notification_returns_immediately_and_delivers_in_background():
    started = asyncio.Event()

    async def deliver(name: str, content: str) -> str:
        started.set()
        assert content.startswith("[notification from agent main — no reply expected]")
        return "ignored"

    deps, _ = _deps(deliver=deliver)
    result = await _send_message("main", "journalist", "fyi", False, **deps)
    assert "sent" in result.lower() and "no reply" in result.lower()
    await asyncio.wait_for(started.wait(), timeout=1.0)


# --- per-agent tool surface ---


def _noop_deps():
    async def deliver(name: str, content: str) -> str:
        return f"reply from {name}"

    return dict(deliver=deliver, get_live_process=lambda name: None)


async def _tool_names(server) -> set[str]:
    async with fastmcp.Client(server) as client:
        return {t.name for t in await client.list_tools()}


@pytest.mark.asyncio
async def test_server_exposes_one_tool_per_reachable_agent():
    server = create_messaging_server("main", GROUPS, **_noop_deps())
    assert await _tool_names(server) == {
        "message_agent_journalist",
        "message_agent_career",
    }


@pytest.mark.asyncio
async def test_ungrouped_agent_gets_a_server_with_no_tools():
    server = create_messaging_server("shopping", GROUPS, **_noop_deps())
    assert await _tool_names(server) == set()


@pytest.mark.asyncio
async def test_tool_description_carries_target_agent_description():
    server = create_messaging_server(
        "main", GROUPS, descriptions={"journalist": "news hound"}, **_noop_deps()
    )
    async with fastmcp.Client(server) as client:
        tool = next(
            t for t in await client.list_tools() if t.name == "message_agent_journalist"
        )
    assert "news hound" in tool.description
    assert set(tool.inputSchema["properties"]) == {"message", "expect_reply"}


@pytest.mark.asyncio
async def test_tool_delivers_to_its_own_target():
    calls: list[tuple[str, str]] = []

    async def deliver(name: str, content: str) -> str:
        calls.append((name, content))
        return f"reply from {name}"

    server = create_messaging_server(
        "main", GROUPS, deliver=deliver, get_live_process=lambda name: None
    )
    async with fastmcp.Client(server) as client:
        result = await client.call_tool(
            "message_agent_journalist", {"message": "what's news?"}
        )
    assert result.content[0].text == "reply from journalist"
    assert calls == [("journalist", "[message from agent main] what's news?")]


@pytest.mark.asyncio
async def test_servers_share_one_wait_graph():
    """journalist replying to main must not be able to message main back."""
    reentrant: list[str] = []

    async def deliver(name: str, content: str) -> str:
        async with fastmcp.Client(servers["journalist"]) as client:
            reply = await client.call_tool("message_agent_main", {"message": "back?"})
        reentrant.append(reply.content[0].text)
        return "done"

    servers = create_messaging_servers(
        ["main", "journalist", "career"],
        GROUPS,
        deliver=deliver,
        get_live_process=lambda name: None,
    )
    async with fastmcp.Client(servers["main"]) as client:
        await client.call_tool("message_agent_journalist", {"message": "hi"})
    assert len(reentrant) == 1
    assert reentrant[0].startswith("Error") and "waiting on a reply" in reentrant[0]


# --- mounting ---


@pytest.mark.asyncio
async def test_combine_mcp_apps_mounts_schedules_and_each_agent(tmp_path):
    """Schedules stays at /mcp; each agent's messaging app is at /agents/{name}/mcp."""
    import httpx
    from unittest.mock import MagicMock

    from claude_assistant.mcp_server import create_mcp_server
    from claude_assistant.schedule_store import ScheduleStore

    schedules = create_mcp_server(ScheduleStore(tmp_path / "s.json"), MagicMock())
    servers = create_messaging_servers(["main", "journalist"], GROUPS, **_noop_deps())
    app = combine_mcp_apps(
        schedules.http_app(path="/mcp"),
        {n: s.http_app(path="/mcp") for n, s in servers.items()},
    )

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            # A bare GET is rejected by the MCP transport, but only a route
            # that exists gets far enough to reject it — 404 means unmounted.
            assert (await client.get("/mcp")).status_code != 404
            assert (await client.get("/agents/main/mcp")).status_code != 404
            assert (await client.get("/agents/journalist/mcp")).status_code != 404
            assert (await client.get("/agents/nope/mcp")).status_code == 404
            assert (await client.get("/agents/mcp")).status_code == 404


@pytest.mark.asyncio
async def test_start_debug_messaging_sets_port_env_and_serves(tmp_path, monkeypatch):
    import os

    import httpx

    from claude_assistant.config import AgentConfig, AssistantConfig
    from claude_assistant.debug_agent import start_debug_messaging

    monkeypatch.delenv("CLAUB_MSG_PORT", raising=False)
    config = AssistantConfig(
        agents={"main": AgentConfig(channel_id="1"), "journalist": AgentConfig(channel_id="2")},
        agent_groups={"g": ["main", "journalist"]},
    )

    def fake_build(name):  # never actually spawns claude in this test
        raise AssertionError("no delivery in this test")

    server, task, registry = await start_debug_messaging(config, None, fake_build, live={})
    try:
        port = int(os.environ["CLAUB_MSG_PORT"])
        # The endpoint exists (any non-404 response proves it is mounted and
        # serving; MCP itself needs a proper handshake we don't do here).
        # Must use an async client — a blocking one would stall the loop
        # that uvicorn is serving on.
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"http://127.0.0.1:{port}/agents/main/mcp")
        assert response.status_code != 404
        assert registry == {}
    finally:
        server.should_exit = True
        await task
