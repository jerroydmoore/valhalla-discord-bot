import asyncio
from datetime import datetime, timezone

from heimdall import bot as bot_module
from heimdall import render
from heimdall.config import Config
from heimdall.valheim import Join, PlayerCheck, parse_presence

PREFIX = "Nov 24 12:00:00 supervisord: valheim-server 11/24/2026 12:00:00: "


def line(ts: str, text: str) -> str:
    return f"2026-11-24T{ts}.266974356Z {PREFIX}{text}"


LOGS = "\n".join([
    line("19:00:00", "Got handshake from client 76561198036035250"),
    line("19:00:05", "Got character ZDOID from Bjorn : 3550347729:1"),
    line("19:10:00", "Got character ZDOID from Astrid : 1111111111:1"),
    line("19:20:00", "Got character ZDOID from Bjorn : 0:0"),       # Bjorn died
    line("19:20:30", "Got character ZDOID from Bjorn : 3550347729:7"),  # respawned
    line("19:30:00", "Got character ZDOID from Ulf : 2222222222:1"),
    # Ulf disconnects (real lines from the server)
    line("20:13:26", "Destroying abandoned non persistent zdo 2222222222:3192 owner 2222222222"),
    line("20:13:26", "Destroying abandoned non persistent zdo 2222222222:3169 owner 2222222222"),
    line("20:13:26", "Closing socket 76561198036035251"),
])


def at(hms: str) -> datetime:
    return datetime.strptime(f"2026-11-24T{hms}", "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def test_presence_tracks_joins_deaths_and_leaves():
    presence = parse_presence(LOGS)
    assert presence.online == ["Bjorn", "Astrid"]
    # A respawn isn't a new join; Ulf's join still counts even though he left.
    assert [(j.name, j.at) for j in presence.joins] == [
        ("Bjorn", at("19:00:05")), ("Astrid", at("19:10:00")), ("Ulf", at("19:30:00")),
    ]
    assert [j.name for j in presence.joins_after(at("19:10:00"))] == ["Ulf"]


def test_names_with_spaces_and_untimestamped_lines():
    presence = parse_presence(PREFIX + "Got character ZDOID from Big Bjorn : 42:1")
    assert presence.online == ["Big Bjorn"]
    assert presence.joins == [Join("Big Bjorn", None)]


def test_rejoin_after_leaving_is_a_new_join():
    logs = "\n".join([
        line("19:00:00", "Got character ZDOID from Bjorn : 5:1"),
        line("19:05:00", "Destroying abandoned non persistent zdo 5:9 owner 5"),
        line("19:06:00", "Got character ZDOID from Bjorn : 6:1"),
    ])
    presence = parse_presence(logs)
    assert presence.online == ["Bjorn"]
    assert len(presence.joins) == 2


def test_players_text_with_names():
    three = PlayerCheck("players", 3)
    assert render.players_text(three, ["Bjorn", "Astrid", "Ulf"]) == "👥 **3** online: Bjorn, Astrid, Ulf"
    assert render.players_text(three, ["Bjorn", "Astrid"]) == "👥 **3** online: Bjorn, Astrid, and 1 more"
    assert render.players_text(three, ["Bjorn"]) == "👥 **3** online: Bjorn and 2 more"
    # More names than status.json counts: a leave was missed, so show the count only.
    assert render.players_text(PlayerCheck("players", 1), ["Bjorn", "Astrid"]) == "👥 **1** player online"
    assert render.players_text(three, []) == "👥 **3** players online"
    assert render.players_text(PlayerCheck("players", 1), ["*Bjorn*"]) == "👥 **1** online: \\*Bjorn\\*"


def test_join_dm_text():
    text = render.join_dm_text(
        [Join("Bjorn", at("19:00:05")), Join("Astrid", None)], PlayerCheck("players", 2), ["Bjorn", "Astrid"]
    )
    stamp = int(at("19:00:05").timestamp())
    assert text == f"🟢 Joined Valheim: **Bjorn** at <t:{stamp}:t>, **Astrid**\n👥 **2** online: Bjorn, Astrid"


def test_notify_ids_from_env(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "x")
    monkeypatch.setenv("NOTIFY_USER_IDS", "123, 456 789")
    config = Config.from_env()
    assert config.notify_user_ids == [123, 456, 789]
    assert config.notify_poll_seconds == 900


class FakeUser:
    def __init__(self):
        self.sent = []

    async def send(self, text, **kwargs):
        self.sent.append(text)


def test_report_joins_dms_only_new_joins(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "x")
    monkeypatch.setenv("NOTIFY_USER_IDS", "1")
    heimdall = bot_module.Heimdall(Config.from_env())
    user = FakeUser()
    monkeypatch.setattr(heimdall, "get_user", lambda user_id: user)

    async def read_presence(helper, container):
        return parse_presence(LOGS)

    async def check_players(url):
        return PlayerCheck("players", 2)

    monkeypatch.setattr(bot_module.valheim, "read_presence", read_presence)
    monkeypatch.setattr(bot_module.valheim, "check_players", check_players)

    async def scenario():
        first = await heimdall.report_joins(at("19:05:00"))
        again = await heimdall.report_joins(first)
        return first, again

    first, again = asyncio.run(scenario())
    assert len(user.sent) == 1  # the second check finds nothing new
    assert "**Astrid**" in user.sent[0] and "**Ulf**" in user.sent[0]
    assert "Bjorn**" not in user.sent[0].split("\n")[0]
    assert again >= first
