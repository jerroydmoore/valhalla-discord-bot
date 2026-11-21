from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: Optional[int]
    restart_role_id: Optional[int]
    valheim_container: str
    docker_helper: List[str]
    host_label: str
    status_url: str
    ready_timeout: float

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise SystemExit("DISCORD_TOKEN is not set")
        return cls(
            token=token,
            guild_id=_optional_int("DISCORD_GUILD_ID"),
            restart_role_id=_optional_int("RESTART_ROLE_ID"),
            valheim_container=os.environ.get("VALHEIM_CONTAINER", "valheim-valheim-1"),
            docker_helper=shlex.split(
                os.environ.get(
                    "DOCKER_HELPER",
                    "sudo -n /usr/local/libexec/heimdall/heimdall-docker",
                )
            ),
            host_label=os.environ.get("HOST_LABEL", "valhalla"),
            status_url=os.environ.get("STATUS_URL", "http://127.0.0.1:9080/status.json"),
            ready_timeout=float(os.environ.get("READY_TIMEOUT_SECONDS", "480")),
        )


def _optional_int(name: str) -> Optional[int]:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {value!r}")
