#!/usr/bin/env python3
"""Test script: send Discord messages with per-agent names and avatars via webhooks.

Usage:
    cd bot && uv run python ../scripts/test_webhooks.py

Requires DISCORD_BOT_TOKEN in bot/.envrc (or env).
Sends a test message to each agent's channel with a custom name and avatar.
"""

import asyncio
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

# Load token from bot/.envrc
envrc = Path(__file__).resolve().parent.parent / "bot" / ".envrc"
load_dotenv(envrc)
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
if not TOKEN:
    print("Error: DISCORD_BOT_TOKEN not set. Check bot/.envrc")
    sys.exit(1)

# -- Configure your agents here --
# Map agent name -> (channel_id, display_name, avatar_url)
# avatar_url can be any public image URL, or None for default
AGENTS = {
    "main": {
        "channel_id": 1469300886605140080,
        "display_name": "Claude",
        "avatar_url": None,  # uses default webhook avatar
    },
    "journalist": {
        "channel_id": 1469707538659414201,
        "display_name": "The Journalist",
        "avatar_url": None,
    },
    "leetcode-coach": {
        "channel_id": 1469451880923926691,
        "display_name": "LeetCode Coach",
        "avatar_url": None,
    },
}

WEBHOOK_NAME = "claub-agent"  # reusable webhook name


async def get_or_create_webhook(
    channel: discord.TextChannel,
) -> discord.Webhook:
    """Find existing claub webhook or create one."""
    webhooks = await channel.webhooks()
    for wh in webhooks:
        if wh.name == WEBHOOK_NAME:
            return wh
    return await channel.create_webhook(name=WEBHOOK_NAME)


async def main():
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"Connected as {client.user}")
        print()

        for agent_name, cfg in AGENTS.items():
            channel = client.get_channel(cfg["channel_id"])
            if not channel or not isinstance(channel, discord.TextChannel):
                print(f"  [{agent_name}] Channel {cfg['channel_id']} not found, skipping")
                continue

            try:
                webhook = await get_or_create_webhook(channel)
                await webhook.send(
                    content=f"Hello from **{cfg['display_name']}**! This is a webhook test message.",
                    username=cfg["display_name"],
                    avatar_url=cfg["avatar_url"],
                )
                print(f"  [{agent_name}] Sent to #{channel.name} as '{cfg['display_name']}'")
            except discord.Forbidden:
                print(f"  [{agent_name}] Missing 'Manage Webhooks' permission in #{channel.name}")
            except Exception as e:
                print(f"  [{agent_name}] Error: {e}")

        print()
        print("Done! Check your Discord channels.")
        await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
