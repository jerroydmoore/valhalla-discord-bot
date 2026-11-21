"""Valheim-specific checks: who's online, and when the server is ready again."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

import aiohttp

log = logging.getLogger("heimdall.valheim")

# status.json is refreshed every 10s; anything much older means the updater
# inside the container has stopped and the player count can't be trusted.
MAX_STATUS_AGE = 90


@dataclass(frozen=True)
class PlayerCheck:
    # "empty": safe to restart. "players": someone is online, refuse.
    # "not_answering": status.json works but the game server doesn't answer
    #   Steam queries (still starting, or hung), so nobody can be playing.
    # "unknown": status.json itself couldn't be read or is stale; refuse.
    verdict: str
    player_count: int = 0
    detail: str = ""


def classify_status(data: dict, now: Optional[datetime] = None) -> PlayerCheck:
    now = now or datetime.now(timezone.utc)
    updated = _parse_time(data.get("last_status_update"))
    if updated is None:
        return PlayerCheck("unknown", detail="status.json has no last_status_update")
    age = (now - updated).total_seconds()
    if age > MAX_STATUS_AGE:
        return PlayerCheck("unknown", detail=f"status.json is {age:.0f}s old")
    if data.get("error"):
        return PlayerCheck("not_answering", detail=str(data["error"]))
    count = data.get("player_count")
    if not isinstance(count, int):
        return PlayerCheck("unknown", detail="status.json has no player_count")
    return PlayerCheck("players" if count > 0 else "empty", player_count=count)


async def check_players(status_url: str, timeout: float = 5) -> PlayerCheck:
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(status_url) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        return PlayerCheck("unknown", detail=f"couldn't read {status_url}: {exc!r}")
    if not isinstance(data, dict):
        return PlayerCheck("unknown", detail="status.json isn't a JSON object")
    return classify_status(data)


def is_ready_line(line: str) -> bool:
    """Matches e.g.
    'Nov 20 23:21:21 supervisord: valheim-server 11/20/2026 23:21:21: Game server connected'
    Timestamps and the program name vary, so only the stable parts are checked.
    """
    return "supervisord" in line and "Game server connected" in line


async def wait_until_ready(
    helper: Sequence[str], container: str, timeout: float, retry_delay: float = 5
) -> bool:
    """Follow the container's logs (from its latest start) until the ready line
    appears. Returns False if it doesn't within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            if await asyncio.wait_for(_follow_once(helper, container), remaining):
                return True
        except asyncio.TimeoutError:
            return False
        except OSError as exc:
            log.warning("Couldn't follow %s logs: %s", container, exc)
        # The log stream ended (container stopped or crashed). Try again.
        await asyncio.sleep(min(retry_delay, max(0, deadline - time.monotonic())))


async def _follow_once(helper: Sequence[str], container: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        *helper,
        "logs",
        container,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        limit=1024 * 1024,  # Valheim can log very long lines
    )
    try:
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except ValueError:  # a single line over the limit; skip it
                continue
            if not raw:
                return False
            if is_ready_line(raw.decode(errors="replace")):
                return True
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


def _parse_time(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
