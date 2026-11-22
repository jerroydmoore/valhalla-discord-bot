import asyncio
import json
import sys
import time

import pytest

from heimdall.metrics import (
    DockerError,
    run_helper,
    merge_container_info,
    parse_cpu_percent,
    parse_mem_usage,
    parse_size,
)


def test_parse_cpu_percent():
    assert parse_cpu_percent("18.71%") == pytest.approx(0.1871)
    assert parse_cpu_percent("--") is None


def test_parse_size_units():
    assert parse_size("512B") == 512
    assert parse_size("1.5KiB") == 1536
    assert parse_size("2MB") == 2_000_000
    assert parse_size("1GiB") == 1024**3
    assert parse_size("nonsense") is None


def test_parse_mem_usage_takes_used_side():
    assert parse_mem_usage("1.9GiB / 5.79GiB") == int(1.9 * 1024**3)
    assert parse_mem_usage("") is None


def _lines(*rows):
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def test_merge_running_and_stopped_containers():
    ps = _lines(
        {"Names": "caddy", "State": "running", "Status": "Up 2 hours"},
        {"Names": "valheim", "State": "running", "Status": "Up 3 days (healthy)"},
        {"Names": "old", "State": "exited", "Status": "Exited (0) 2 days ago"},
    )
    stats = _lines(
        {"Name": "valheim", "CPUPerc": "62.00%", "MemUsage": "1.9GiB / 5.79GiB"},
        {"Name": "caddy", "CPUPerc": "0.10%", "MemUsage": "20MiB / 5.79GiB"},
    )
    containers = merge_container_info(ps, stats)

    # Running first, biggest memory first; stopped last.
    assert [c.name for c in containers] == ["valheim", "caddy", "old"]
    valheim = containers[0]
    assert valheim.state == "running"
    assert valheim.cpu_cores == pytest.approx(0.62)
    assert valheim.mem_used == int(1.9 * 1024**3)
    assert containers[2].cpu_cores is None


def test_run_helper_timeout_does_not_hang_on_orphaned_child():
    # Like sudo -> docker: the helper's child keeps stdout open after the helper
    # itself is stopped. Python 3.12's Process.wait() waits for that pipe, so
    # Heimdall must give up rather than hang (the stuck-announcement bug).
    script = (
        "import subprocess, time\n"
        "subprocess.Popen(['sleep', '15'])\n"  # inherits stdout
        "time.sleep(15)\n"
    )
    started = time.monotonic()
    with pytest.raises(DockerError, match="timed out"):
        asyncio.run(run_helper([sys.executable, "-c", script], "logs", "valheim", timeout=0.5))
    assert time.monotonic() - started < 8
