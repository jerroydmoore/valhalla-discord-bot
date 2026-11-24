from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

import discord
from discord import app_commands

from . import metrics, render, valheim
from .config import Config
from .watchdog import ConnectionWatchdog

log = logging.getLogger("heimdall")

WATCHDOG_CHECK_SECONDS = 30


class Heimdall(discord.Client):
    def __init__(self, config: Config) -> None:
        # Slash commands only: no privileged intents (message content, members).
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.tree.on_error = self.on_app_command_error
        self.restart_lock: Optional[asyncio.Lock] = None
        self.tree.add_command(build_commands(self))
        self.watchdog = ConnectionWatchdog(config.disconnect_timeout)
        # Non-zero when the watchdog shut the bot down; main() exits with it
        # so systemd restarts the service.
        self.exit_code = 0
        self._watchdog_task: Optional[asyncio.Task] = None
        self._join_task: Optional[asyncio.Task] = None

    async def setup_hook(self) -> None:
        # Created here so it binds to the running event loop (needed on 3.8/3.9).
        self.restart_lock = asyncio.Lock()
        # Keep a reference: asyncio only holds tasks weakly.
        self._watchdog_task = asyncio.create_task(self.watch_connection())
        if self.config.notify_user_ids:
            self._join_task = asyncio.create_task(self.watch_joins())
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d command(s) to guild %s", len(synced), self.config.guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global command(s); may take up to an hour to appear", len(synced))

    async def on_ready(self) -> None:
        self.watchdog.connected()
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))

    async def on_resumed(self) -> None:
        self.watchdog.connected()
        log.info("Reconnected to Discord (session resumed)")

    async def on_disconnect(self) -> None:
        if self.watchdog.offline_for() == 0:
            log.warning("Disconnected from Discord; discord.py will try to reconnect")
        self.watchdog.disconnected()

    async def watch_connection(self) -> None:
        while not self.is_closed():
            await asyncio.sleep(WATCHDOG_CHECK_SECONDS)
            if not self.watchdog.overdue():
                continue
            if self.restart_lock is not None and self.restart_lock.locked():
                # Let a Valheim restart finish updating its announcement
                # (that goes over HTTP, which may still work).
                continue
            log.error(
                "Disconnected from Discord for %.0fs; exiting so systemd restarts Heimdall",
                self.watchdog.offline_for(),
            )
            self.exit_code = 1
            try:
                await asyncio.wait_for(self.close(), 15)
            except Exception:
                log.exception("Clean shutdown failed")
            if not self.is_closed():
                os._exit(1)
            return

    async def watch_joins(self) -> None:
        """DM everyone in NOTIFY_USER_IDS when players join Valheim.

        Checks the logs every NOTIFY_POLL_SECONDS and reports joins by their
        log timestamps, so someone who joins and leaves between checks is
        still reported. Joins from before Heimdall started aren't. Leaving
        isn't reported.
        """
        await self.wait_until_ready()
        reported_until = datetime.now(timezone.utc)
        log.info(
            "Will DM %d user(s) about Valheim joins, checking every %.0fs",
            len(self.config.notify_user_ids), self.config.notify_poll_seconds,
        )
        while not self.is_closed():
            await asyncio.sleep(self.config.notify_poll_seconds)
            try:
                reported_until = await self.report_joins(reported_until)
            except Exception:
                log.exception("Checking for Valheim joins failed")

    async def report_joins(self, since: datetime) -> datetime:
        """DM about joins logged after `since`; returns the new `since`."""
        read_at = datetime.now(timezone.utc)
        try:
            presence = await valheim.read_presence(
                self.config.docker_helper, self.config.valheim_container
            )
        except (metrics.DockerError, OSError) as exc:
            # e.g. Valheim is mid-restart. Try again next time, from `since`.
            log.warning("Couldn't read Valheim logs for joins: %s", exc)
            return since
        joins = presence.joins_after(since)
        # Lines logged while the read ran may already be in this batch.
        until = max([read_at] + [j.at for j in joins if j.at is not None])
        if not joins:
            return until
        check = await valheim.check_players(self.config.status_url)
        text = render.join_dm_text(joins, check, presence.online)
        log.info("Players joined: %s", ", ".join(j.name for j in joins))
        for user_id in self.config.notify_user_ids:
            try:
                user = self.get_user(user_id) or await self.fetch_user(user_id)
                await user.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                # e.g. DMs from server members are turned off, or no shared server
                log.warning("Couldn't DM user %s: %s", user_id, exc)
        return until

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
    @app_commands.describe(announce="Show the status to everyone in the channel (default: only you)")
    async def status(interaction: discord.Interaction, announce: bool = False) -> None:
        private = not announce
        # Sampling CPU takes ~1s and Docker ~2s; Discord wants a reply within 3s.
        # The deferred response decides who sees the reply.
        await interaction.response.defer(ephemeral=private, thinking=True)
        loop = asyncio.get_running_loop()
        host_future = loop.run_in_executor(None, metrics.collect_host)
        players_task = asyncio.create_task(valheim.check_players(bot.config.status_url))
        names_task = asyncio.create_task(online_names(bot, players_task))
        containers = None
        docker_error = None
        try:
            containers = await metrics.collect_containers(bot.config.docker_helper)
        except (metrics.DockerError, OSError) as exc:
            log.warning("Couldn't read Docker: %s", exc)
            docker_error = str(exc)
        host = await host_future
        players = await players_task
        embed = render.status_embed(
            host, containers, docker_error, bot.config.host_label,
            players=players, player_names=await names_task,
        )
        await interaction.followup.send(embed=embed, ephemeral=private)

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


async def online_names(bot: Heimdall, players_task: "asyncio.Task[valheim.PlayerCheck]") -> Optional[List[str]]:
    """Names of who's online, read from the logs only when someone is."""
    if (await players_task).player_count == 0:
        return None
    try:
        presence = await valheim.read_presence(bot.config.docker_helper, bot.config.valheim_container)
    except (metrics.DockerError, OSError) as exc:
        log.warning("Couldn't read Valheim logs for player names: %s", exc)
        return None
    return presence.online


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"
