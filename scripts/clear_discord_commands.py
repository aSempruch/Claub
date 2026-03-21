"""One-time script to clear all registered Discord application commands."""

import asyncio
import os
import sys

import discord


async def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("DISCORD_BOT_TOKEN not set. Source your .envrc first.")
        sys.exit(1)

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        assert client.user
        print(f"Logged in as {client.user} ({client.user.id})")

        # Clear global commands
        tree = discord.app_commands.CommandTree(client)
        tree.clear_commands(guild=None)
        await tree.sync()
        print("Cleared global commands.")

        # Clear guild-specific commands
        for guild in client.guilds:
            tree.clear_commands(guild=guild)
            await tree.sync(guild=guild)
            print(f"Cleared commands for guild: {guild.name} ({guild.id})")

        print("Done. You can remove this script now.")
        await client.close()

    await client.start(token)


if __name__ == "__main__":
    asyncio.run(main())
