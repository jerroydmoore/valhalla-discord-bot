import asyncio
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from heimdall.valheim import check_players, classify_status, is_ready_line, wait_until_ready

NOW = datetime(2026, 11, 20, 23, 0, 0, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(seconds=5)).isoformat()


def test_players_online_refuses():
    check = classify_status({"last_status_update": FRESH, "error": None, "player_count": 2}, NOW)
    assert check.verdict == "players"
    assert check.player_count == 2


def test_empty_server_allows():
    check = classify_status({"last_status_update": FRESH, "error": None, "player_count": 0}, NOW)
    assert check.verdict == "empty"


def test_query_error_means_not_answering():
    check = classify_status({"last_status_update": FRESH, "error": "timeout('timed out')"}, NOW)
    assert check.verdict == "not_answering"


def test_stale_or_malformed_status_is_unknown():
    stale = (NOW - timedelta(minutes=5)).isoformat()
    assert classify_status({"last_status_update": stale, "error": None, "player_count": 0}, NOW).verdict == "unknown"
    assert classify_status({"error": None, "player_count": 0}, NOW).verdict == "unknown"
    assert classify_status({"last_status_update": FRESH, "error": None}, NOW).verdict == "unknown"


def test_check_players_over_http():
    body = json.dumps({
        "last_status_update": datetime.now(timezone.utc).isoformat(),
        "error": None,
        "player_count": 1,
    }).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")  # busybox httpd
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/status.json"
        assert asyncio.run(check_players(url)).verdict == "players"
    finally:
        server.shutdown()


def test_check_players_unreachable_is_unknown():
    check = asyncio.run(check_players("http://127.0.0.1:1/status.json", timeout=2))
    assert check.verdict == "unknown"


def test_ready_line_matching():
    assert is_ready_line(
        "Nov 20 23:21:21 supervisord: valheim-server 11/20/2026 23:21:21: Game server connected"
    )
    assert is_ready_line("Dec 01 01:02:03 supervisord: valheim 12/01/2026 01:02:03: Game server connected\n")
    assert not is_ready_line("11/20/2026 23:21:21: Game server connected")  # no supervisord
    assert not is_ready_line("Nov 20 23:21:21 supervisord: valheim-server Game server disconnected")


def _fake_helper(script):
    # wait_until_ready appends "logs <container>"; python -c ignores the extra args.
    return [sys.executable, "-c", script]


def test_wait_until_ready_sees_line():
    script = (
        "import sys, time\n"
        "print('Nov 20 23:20:00 supervisord: valheim-server starting'); sys.stdout.flush()\n"
        "time.sleep(0.2)\n"
        "print('Nov 20 23:21:21 supervisord: valheim-server 11/20/2026 23:21:21: Game server connected'); sys.stdout.flush()\n"
        "time.sleep(30)\n"  # docker logs --follow never exits on its own
    )
    assert asyncio.run(wait_until_ready(_fake_helper(script), "valheim", timeout=10)) is True


def test_wait_until_ready_times_out():
    script = "import time; print('still loading', flush=True); time.sleep(30)"
    assert asyncio.run(wait_until_ready(_fake_helper(script), "valheim", timeout=1)) is False


def test_wait_until_ready_retries_when_stream_ends():
    # Each run exits without the line (like the container stopping); keeps retrying until timeout.
    script = "print('container stopped')"
    result = asyncio.run(wait_until_ready(_fake_helper(script), "valheim", timeout=1.5, retry_delay=0.2))
    assert result is False
