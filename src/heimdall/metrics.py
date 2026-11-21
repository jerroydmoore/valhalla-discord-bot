"""Collects host and container metrics.

Host numbers come from psutil (/proc). Container numbers come from Docker via a
small root-owned helper script that Heimdall may run with sudo (see
deploy/heimdall-docker), so the bot never needs full access to the Docker socket.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import psutil


@dataclass(frozen=True)
class HostStats:
    cpu_percent: float
    cpu_count: int
    load_avg: Tuple[float, float, float]
    mem_used: int
    mem_total: int
    swap_used: int
    swap_total: int
    disk_used: int
    disk_total: int
    uptime_seconds: float

    @property
    def mem_percent(self) -> float:
        return _percent(self.mem_used, self.mem_total)

    @property
    def disk_percent(self) -> float:
        return _percent(self.disk_used, self.disk_total)


@dataclass(frozen=True)
class ContainerStats:
    name: str
    state: str  # "running", "exited", "restarting", ...
    status: str  # Docker's human text, e.g. "Up 3 days (healthy)"
    cpu_cores: Optional[float] = None  # 1.0 == one full core
    mem_used: Optional[int] = None


class DockerError(RuntimeError):
    pass


def collect_host(cpu_sample_seconds: float = 1.0, disk_path: str = "/") -> HostStats:
    """Blocking: samples CPU for `cpu_sample_seconds`. Run it in a thread."""
    cpu = psutil.cpu_percent(interval=cpu_sample_seconds)
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage(disk_path)
    return HostStats(
        cpu_percent=cpu,
        cpu_count=psutil.cpu_count() or 1,
        load_avg=os.getloadavg(),
        # "used" here means total minus available, i.e. what `free` shows as
        # not available. psutil's own `used` ignores reclaimable cache.
        mem_used=vm.total - vm.available,
        mem_total=vm.total,
        swap_used=swap.used,
        swap_total=swap.total,
        disk_used=disk.used,
        disk_total=disk.total,
        uptime_seconds=time.time() - psutil.boot_time(),
    )


async def run_helper(helper: Sequence[str], *args: str, timeout: float = 120) -> str:
    proc = await asyncio.create_subprocess_exec(
        *helper,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise DockerError(f"`{' '.join(args)}` timed out after {timeout:.0f}s")
    if proc.returncode != 0:
        message = err.decode(errors="replace").strip() or f"exit code {proc.returncode}"
        raise DockerError(message)
    return out.decode(errors="replace")


async def collect_containers(helper: Sequence[str]) -> List[ContainerStats]:
    ps_out, stats_out = await asyncio.gather(
        run_helper(helper, "ps", timeout=15),
        run_helper(helper, "stats", timeout=15),
    )
    return merge_container_info(ps_out, stats_out)


async def restart_container(helper: Sequence[str], name: str) -> None:
    # Compose sets a 2m stop_grace_period so the world can save; allow for it.
    await run_helper(helper, "restart", name, timeout=240)


def merge_container_info(ps_out: str, stats_out: str) -> List[ContainerStats]:
    """Combine `docker ps -a` and `docker stats --no-stream` JSON-lines output."""
    usage: Dict[str, Tuple[Optional[float], Optional[int]]] = {}
    for row in _json_lines(stats_out):
        name = row.get("Name", "")
        usage[name] = (
            parse_cpu_percent(row.get("CPUPerc", "")),
            parse_mem_usage(row.get("MemUsage", "")),
        )

    containers = []
    for row in _json_lines(ps_out):
        name = row.get("Names", "").split(",")[0]
        cpu, mem = usage.get(name, (None, None))
        containers.append(
            ContainerStats(
                name=name,
                state=row.get("State", "").lower(),
                status=row.get("Status", ""),
                cpu_cores=cpu,
                mem_used=mem,
            )
        )
    containers.sort(key=lambda c: (c.state != "running", -(c.mem_used or 0), c.name))
    return containers


def parse_cpu_percent(value: str) -> Optional[float]:
    """'18.71%' -> 0.1871 cores. Docker reports 100% per full core."""
    try:
        return float(value.strip().rstrip("%")) / 100
    except ValueError:
        return None


_UNITS = {
    "b": 1,
    "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4,
}
_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([a-zA-Z]+)\s*$")


def parse_size(value: str) -> Optional[int]:
    """'1.9GiB' -> bytes."""
    match = _SIZE_RE.match(value)
    if not match:
        return None
    factor = _UNITS.get(match.group(2).lower())
    if factor is None:
        return None
    return int(float(match.group(1)) * factor)


def parse_mem_usage(value: str) -> Optional[int]:
    """'1.9GiB / 5.79GiB' -> bytes used."""
    return parse_size(value.split("/")[0]) if value else None


def _json_lines(text: str) -> List[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _percent(used: int, total: int) -> float:
    return 100.0 * used / total if total else 0.0
