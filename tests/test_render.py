from heimdall import render
from heimdall.metrics import ContainerStats, HostStats

GiB = 1024**3


def host(cpu=38.0, mem_used=int(1.42 * GiB), disk_used=int(11.8 * GiB), swap_total=0):
    return HostStats(
        cpu_percent=cpu,
        cpu_count=2,
        load_avg=(0.74, 0.81, 0.66),
        mem_used=mem_used,
        mem_total=int(5.8 * GiB),
        swap_used=0,
        swap_total=swap_total,
        disk_used=disk_used,
        disk_total=int(19.3 * GiB),
        uptime_seconds=12 * 86400 + 4 * 3600 + 31 * 60,
    )


VALHEIM = ContainerStats("valheim", "running", "Up 3 days", cpu_cores=0.19, mem_used=2 * GiB)


def fields(embed):
    return {f.name: f.value for f in embed.fields}


def test_healthy_status_is_green():
    embed = render.status_embed(host(), [VALHEIM], None, "valhalla")
    assert embed.colour == render.GREEN
    assert embed.title.startswith("🟢")
    f = fields(embed)
    assert "38%" in f["🖥️ CPU"]
    assert "no swap" in f["🧠 Memory"]
    assert f["⏱️ Uptime"] == "12d 4h 31m"
    assert "**valheim** · 0.19 cores · 2.00 GiB · Up 3 days" in f["Apps"]


def test_high_memory_is_red():
    embed = render.status_embed(host(mem_used=int(5.5 * GiB)), [VALHEIM], None, "valhalla")
    assert embed.colour == render.RED


def test_stopped_container_turns_green_to_yellow():
    stopped = ContainerStats("valheim", "exited", "Exited (137) 5 minutes ago")
    embed = render.status_embed(host(), [stopped], None, "valhalla")
    assert embed.colour == render.YELLOW
    assert "🔴" in fields(embed)["Apps"]
    assert "cores" not in fields(embed)["Apps"]


def test_docker_error_is_shown_not_raised():
    embed = render.status_embed(host(), None, "sudo: a password is required", "valhalla")
    assert embed.colour == render.YELLOW
    assert "Couldn't read Docker: sudo: a password is required" in fields(embed)["Apps"]


def test_bar_and_formatting():
    assert render.bar(0) == "`░░░░░░░░░░`"
    assert render.bar(100) == "`██████████`"
    assert render.bar(38) == "`████░░░░░░`"
    assert render.fmt_bytes(500 * 1024**2) == "500 MiB"
    assert render.fmt_duration(90) == "1m"
