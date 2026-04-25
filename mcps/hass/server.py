"""MCP server exposing a small, named slice of Home Assistant.

Not a general HASS bridge — each tool is a hand-written wrapper around a
specific entity or service. To expose more, write a new @mcp.tool() function.
This is deliberate: it keeps the surface tight and lets each tool shape its
response for the agent rather than dumping raw HASS state.

Requires: HASS_URL, HASS_TOKEN.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import httpx
from mcp.server.fastmcp import FastMCP

HASS_URL = os.environ.get("HASS_URL", "").rstrip("/")
HASS_TOKEN = os.environ.get("HASS_TOKEN")
if not HASS_URL or not HASS_TOKEN:
    raise RuntimeError("HASS_URL and HASS_TOKEN environment variables are required")

HEADERS = {"Authorization": f"Bearer {HASS_TOKEN}"}
HISTORY_MAX_HOURS = 168

mcp = FastMCP("hass")


async def _get_state(entity_id: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{HASS_URL}/api/states/{entity_id}", headers=HEADERS)
        resp.raise_for_status()
        return resp.json()


async def _get_history(entity_id: str, hours: int) -> list[dict]:
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{HASS_URL}/api/history/period/{start}",
            params={
                "filter_entity_id": entity_id,
                "minimal_response": "",
                "no_attributes": "",
            },
            headers=HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
    return data[0] if data else []


async def _call_service(domain: str, service: str, payload: dict) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{HASS_URL}/api/services/{domain}/{service}",
            json=payload,
            headers=HEADERS,
        )
        resp.raise_for_status()


@mcp.tool()
async def get_user_location() -> str:
    """Get the user's current location.

    Returns the zone he's in (e.g. "home", "not_home", or a custom zone name)
    along with GPS coordinates, accuracy in meters, the device tracker that's
    reporting, and when the zone last changed.
    """
    state = await _get_state("person.home_owner")
    attrs = state.get("attributes", {})
    return json.dumps(
        {
            "zone": state.get("state"),
            "latitude": attrs.get("latitude"),
            "longitude": attrs.get("longitude"),
            "gps_accuracy_m": attrs.get("gps_accuracy"),
            "source": attrs.get("source"),
            "last_changed": state.get("last_changed"),
        },
        indent=2,
    )


@mcp.tool()
async def get_user_location_history(hours: int = 24) -> str:
    """Get the user's zone history over the past N hours.

    Returns a list of zone transitions: each entry is the zone he was in and
    the timestamp he entered it. GPS coordinates are not included in history
    — use get_user_location for the current position.

    Args:
        hours: Lookback window in hours. 1-168 (one week max). Defaults to 24.
    """
    if hours <= 0 or hours > HISTORY_MAX_HOURS:
        return f"hours must be between 1 and {HISTORY_MAX_HOURS}"
    events = await _get_history("person.home_owner", hours)
    transitions = [
        {"zone": e.get("state"), "entered_at": e.get("last_changed")}
        for e in events
    ]
    return json.dumps(
        {"hours": hours, "transitions": transitions},
        indent=2,
    )


@mcp.tool()
async def get_user_screen_activity() -> str:
    """Check whether the user is watching the living room TV or playing on his gaming PC.

    Returns the current state of both screens together so the agent can see at a
    glance whether the user is engaged in entertainment.

    For each screen:
    - state: media_player state — "off", "idle", "paused", "playing", "on", etc.
              "off" means the screen is not in use.
    - last_changed: when state last transitioned (use to estimate session length).

    Living room TV adds:
    - title: what's currently playing (video/show/movie title)
    - artist: channel name or artist (e.g. YouTube channel)
    - app: streaming app driving the TV (e.g. "SmartTube", "Netflix")
    - duration_seconds / position_seconds: progress through current item

    Gaming PC adds:
    - running_game: title of the currently running game (null when nothing is running)
    - platform: "Steam", "Steam Shortcut", "Custom", etc.
    """
    tv = await _get_state("media_player.lr_tv")
    pc = await _get_state("media_player.gaming_pc")
    tv_attrs = tv.get("attributes", {})
    pc_attrs = pc.get("attributes", {})
    return json.dumps(
        {
            "tv": {
                "state": tv.get("state"),
                "title": tv_attrs.get("media_title"),
                "artist": tv_attrs.get("media_artist"),
                "app": tv_attrs.get("app_name"),
                "duration_seconds": tv_attrs.get("media_duration"),
                "position_seconds": tv_attrs.get("media_position"),
                "last_changed": tv.get("last_changed"),
            },
            "gaming_pc": {
                "state": pc.get("state"),
                "running_game": pc_attrs.get("running_game"),
                "platform": pc_attrs.get("running_platform"),
                "last_changed": pc.get("last_changed"),
            },
        },
        indent=2,
    )


@mcp.tool()
async def broadcast(message: str) -> str:
    """Speak a TTS message aloud on the home's smart speakers.

    Triggers script.broadcast in Home Assistant. The message plays on speakers
    around the home and interrupts whatever is playing. Real-world side effect
    — only use when explicitly asked or for time-sensitive notifications.
    Keep messages short and clear.
    """
    if not message.strip():
        return "Message cannot be empty"
    await _call_service("script", "broadcast", {"message": message})
    return f"Broadcast sent: {message!r}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
