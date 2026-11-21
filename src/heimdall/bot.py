from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands

from . import metrics, render, valheim
from .config import Config

log = logging.getLogger("heimdall")


class Heimdall(discord.Client):
    def __init__(self, config: Config) -> None:
        # Slash commands only: no privileged intents (message content, members).
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.tree.on_error = self.on_app_command_error
        self.restart_lock: Optional[asyncio.Lock] = None
        self.tree.add_command(build_commands(self))

    async def setup_hook(self) -> None:
        # Created here so it binds to the running event loop (needed on 3.8/3.9).
        self.restart_lock = asyncio.Lock()
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d command(s) to guild %s", len(synced), self.config.guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global command(s); may take up to an hour to appear", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.exception("Command failed", exc_info=error)
        message = "⚠️ Something went wrong running that command. Check Heimdall's logs."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    def can_restart(self, user: discord.abc.User) -> bool:
        role_id = self.config.restart_role_id
        if role_id is None or not isinstance(user, discord.Member):
            return False
        return any(role.id == role_id for role in user.roles)


def build_commands(bot: Heimdall) -> app_commands.Group:
    group = app_commands.Group(name="valhalla", description="Check on and control the Valhalla server")

    @group.command(name="status", description="Show CPU, memory, disk, and app usage")
    async def status(interaction: discord.Interaction) -> None:
        # Sampling CPU takes ~1s and Docker ~2s; Discord wants a reply within 3s.
        await interaction.response.defer(thinking=True)
        loop = asyncio.get_running_loop()
        host_future = loop.run_in_executor(None, metrics.collect_host)
        containers = None
        docker_error = None
        try:
            containers = await metrics.collect_containers(bot.config.docker_helper)
        except (metrics.DockerError, OSError) as exc:
            log.warning("Couldn't read Docker: %s", exc)
            docker_error = str(exc)
        host = await host_future
        embed = render.status_embed(host, containers, docker_error, bot.config.host_label)
        await interaction.followup.send(embed=embed)

    @group.command(name="restart", description="Restart an app on Valhalla (only when nobody is playing)")
    @app_commands.describe(app="Which app to restart")
    @app_commands.choices(app=[app_commands.Choice(name="Valheim", value="valheim")])
    async def restart(interaction: discord.Interaction, app: app_commands.Choice[str]) -> None:
        if not bot.can_restart(interaction.user):
            if bot.config.restart_role_id is None:
                msg = "🚫 Restarting is turned off: no RESTART_ROLE_ID is configured."
            else:
                msg = f"🚫 You need the <@&{bot.config.restart_role_id}> role to restart apps."
            await interaction.response.send_message(
                msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
            return

        assert bot.restart_lock is not None
        if bot.restart_lock.locked():
            await interaction.response.send_message(
                "⏳ A restart is already in progress.", ephemeral=True
            )
            return

        async with bot.restart_lock:
            await interaction.response.defer(ephemeral=True, thinking=True)
            check = await valheim.check_players(bot.config.status_url)
            log.info("Player check before restart: %s", check)
            if check.verdict == "players":
                await interaction.edit_original_response(
                    content=(
                        f"🚫 Not restarting {app.name}: {plural(check.player_count, 'player')} "
                        "online. All players must disconnect before the server can be restarted."
                    )
                )
                return
            if check.verdict == "unknown":
                await interaction.edit_original_response(
                    content=(
                        f"🚫 Not restarting {app.name}: I couldn't check whether anyone is "
                        f"playing ({render.truncate(check.detail, 200)})."
                    )
                )
                return

            await interaction.edit_original_response(content="Restarting… see the announcement.")
            who = interaction.user.mention
            none = discord.AllowedMentions.none()
            announcement = await interaction.followup.send(
                f"🔄 {who} is restarting **{app.name}**…", wait=True, allowed_mentions=none
            )
            log.info(
                "%s (%s) restarting %s (%s)",
                interaction.user, interaction.user.id, app.value, check.verdict,
            )
            container = bot.config.valheim_container
            try:
                await metrics.restart_container(bot.config.docker_helper, container)
            except (metrics.DockerError, OSError) as exc:
                log.error("Restart of %s failed: %s", app.value, exc)
                await announcement.edit(
                    content=(
                        f"❌ {who} tried to restart **{app.name}**, but it failed: "
                        f"{render.truncate(str(exc), 300)}"
                    ),
                    allowed_mentions=none,
                )
                return

            await announcement.edit(
                content=(
                    f"🔄 {who} restarted **{app.name}**. Waiting for the game server "
                    "to come up (this usually takes a few minutes)…"
                ),
                allowed_mentions=none,
            )
            ready = await valheim.wait_until_ready(
                bot.config.docker_helper, container, bot.config.ready_timeout
            )
            if ready:
                text = (
                    f"✅ {who} restarted **{app.name}**. Restart completed; "
                    "the game server is ready for you to connect."
                )
            else:
                log.warning("Didn't see the ready line within %ss", bot.config.ready_timeout)
                text = (
                    f"⚠️ {who} restarted **{app.name}**, but I failed to detect if the "
                    "server is ready. Your mileage may vary."
                )
            await announcement.edit(content=text, allowed_mentions=none)

    return group


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"
