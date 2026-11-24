"""Turns metrics into Discord embeds. Pure functions, no I/O."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

import discord

from .metrics import ContainerStats, HostStats
from .valheim import PlayerCheck

GREEN = discord.Colour(0x2ECC71)
YELLOW = discord.Colour(0xF1C40F)
RED = discord.Colour(0xE74C3C)

WARN_AT = 75.0
CRIT_AT = 90.0

APP_ICONS = {"valheim": "⚔️", "valheim-valheim-1": "⚔️"}


def status_embed(
    host: HostStats,
    containers: Optional[List[ContainerStats]],
    docker_error: Optional[str],
    host_label: str,
    now: Optional[datetime] = None,
    players: Optional[PlayerCheck] = None,
) -> discord.Embed:
    worst = max(host.cpu_percent, host.mem_percent, host.disk_percent)
    unhealthy = containers is None or any(c.state != "running" for c in containers)
    colour = level_colour(worst)
    if unhealthy and colour == GREEN:
        colour = YELLOW

    embed = discord.Embed(
        title=f"{level_dot(colour)} Valhalla status",
        colour=colour,
        timestamp=now or datetime.now(timezone.utc),
    )
    embed.add_field(
        name="🖥️ CPU",
        value=(
            f"{bar(host.cpu_percent)} **{host.cpu_percent:.0f}%** of {host.cpu_count} vCPU\n"
            "load {:.2f} · {:.2f} · {:.2f}".format(*host.load_avg)
        ),
        inline=False,
    )
    swap = (
        f"swap {fmt_bytes(host.swap_used)} / {fmt_bytes(host.swap_total)}"
        if host.swap_total
        else "no swap"
    )
    embed.add_field(
        name="🧠 Memory",
        value=(
            f"{bar(host.mem_percent)} **{fmt_bytes(host.mem_used)} / "
            f"{fmt_bytes(host.mem_total)}** ({host.mem_percent:.0f}%)\n{swap}"
        ),
        inline=False,
    )
    embed.add_field(
        name="💾 Disk /",
        value=(
            f"{bar(host.disk_percent)} **{fmt_bytes(host.disk_used)} / "
            f"{fmt_bytes(host.disk_total)}** ({host.disk_percent:.0f}%)"
        ),
        inline=False,
    )
    embed.add_field(name="⏱️ Uptime", value=fmt_duration(host.uptime_seconds), inline=False)
    if players is not None:
        embed.add_field(name="⚔️ Valheim", value=players_text(players), inline=False)
    embed.add_field(name="Apps", value=apps_text(containers, docker_error), inline=False)
    embed.set_footer(text=host_label)
    return embed


def players_text(check: PlayerCheck) -> str:
    if check.verdict in ("players", "empty"):
        n = check.player_count
        return f"👥 **{n}** player{'' if n == 1 else 's'} online"
    if check.verdict == "not_answering":
        return "⚠️ The game server isn't answering (still starting, or hung)"
    return f"⚠️ Couldn't read the player count: {truncate(check.detail, 200)}"


def apps_text(containers: Optional[List[ContainerStats]], docker_error: Optional[str]) -> str:
    if containers is None:
        return f"⚠️ Couldn't read Docker: {truncate(docker_error or 'unknown error', 200)}"
    if not containers:
        return "No containers found."
    lines = []
    for c in containers:
        icon = APP_ICONS.get(c.name, "📦")
        dot = "🟢" if c.state == "running" else ("🟡" if c.state == "restarting" else "🔴")
        parts = []
        if c.cpu_cores is not None and c.state == "running":
            parts.append(f"{c.cpu_cores:.2f} cores")
        if c.mem_used is not None and c.state == "running":
            parts.append(fmt_bytes(c.mem_used))
        parts.append(c.status or c.state)
        lines.append(f"{dot} {icon} **{c.name}** · " + " · ".join(parts))
    return truncate("\n".join(lines), 1024)


def level_colour(percent: float) -> discord.Colour:
    if percent >= CRIT_AT:
        return RED
    if percent >= WARN_AT:
        return YELLOW
    return GREEN


def level_dot(colour: discord.Colour) -> str:
    return {GREEN.value: "🟢", YELLOW.value: "🟡", RED.value: "🔴"}[colour.value]


def bar(percent: float, width: int = 10) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return "`" + "█" * filled + "░" * (width - filled) + "`"


def fmt_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit in ("B", "KiB", "MiB") else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
