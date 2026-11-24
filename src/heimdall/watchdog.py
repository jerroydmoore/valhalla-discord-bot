"""Notices when Heimdall has been cut off from Discord for too long.

discord.py reconnects by itself, but its retry delay doubles each time, up
to a random wait of as much as ~17 minutes. After a network outage that can
leave the bot offline long after the network is back, and it can't recover
from other stuck states at all. Instead, once the bot has been disconnected
for `timeout` seconds, Heimdall exits and systemd restarts it with a short,
capped backoff (see heimdall.service).
"""
from __future__ import annotations

import time
from typing import Callable, Optional


class ConnectionWatchdog:
    def __init__(self, timeout: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.timeout = timeout
        self._clock = clock
        # Not connected yet: the startup connection counts too.
        self._disconnected_since: Optional[float] = clock()

    def connected(self) -> None:
        self._disconnected_since = None

    def disconnected(self) -> None:
        # Keep the first time: discord.py reports a disconnect for every
        # failed reconnect attempt.
        if self._disconnected_since is None:
            self._disconnected_since = self._clock()

    def offline_for(self) -> float:
        if self._disconnected_since is None:
            return 0.0
        return self._clock() - self._disconnected_since

    def overdue(self) -> bool:
        return self.offline_for() >= self.timeout
