from heimdall.watchdog import ConnectionWatchdog


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_startup_counts_as_disconnected():
    clock = FakeClock()
    dog = ConnectionWatchdog(300, clock=clock)
    clock.now += 299
    assert not dog.overdue()
    clock.now += 1
    assert dog.overdue()


def test_connected_resets():
    clock = FakeClock()
    dog = ConnectionWatchdog(300, clock=clock)
    dog.connected()
    clock.now += 10_000
    assert dog.offline_for() == 0
    assert not dog.overdue()


def test_outage_measured_from_first_disconnect():
    clock = FakeClock()
    dog = ConnectionWatchdog(300, clock=clock)
    dog.connected()
    dog.disconnected()
    clock.now += 200
    dog.disconnected()  # each failed reconnect reports another disconnect
    clock.now += 100
    assert dog.offline_for() == 300
    assert dog.overdue()


def test_reconnect_within_timeout_is_fine():
    clock = FakeClock()
    dog = ConnectionWatchdog(300, clock=clock)
    dog.connected()
    dog.disconnected()
    clock.now += 250
    dog.connected()
    clock.now += 250
    assert not dog.overdue()


def _bot(timeout):
    from heimdall.bot import Heimdall
    from heimdall.config import Config

    config = Config(
        token="x", guild_id=None, restart_role_id=None, valheim_container="valheim",
        docker_helper=["true"], host_label="test", status_url="http://127.0.0.1:1/",
        ready_timeout=1, disconnect_timeout=timeout,
    )
    return Heimdall(config)


def test_bot_exits_nonzero_when_offline_too_long(monkeypatch):
    import asyncio

    from heimdall import bot as bot_module

    monkeypatch.setattr(bot_module, "WATCHDOG_CHECK_SECONDS", 0.01)

    async def run():
        bot = _bot(timeout=0)
        bot.restart_lock = asyncio.Lock()
        await asyncio.wait_for(bot.watch_connection(), 5)
        return bot

    bot = asyncio.run(run())
    assert bot.exit_code == 1
    assert bot.is_closed()


def test_bot_waits_for_valheim_restart_before_exiting(monkeypatch):
    import asyncio

    from heimdall import bot as bot_module

    monkeypatch.setattr(bot_module, "WATCHDOG_CHECK_SECONDS", 0.01)

    async def run():
        bot = _bot(timeout=0)
        bot.restart_lock = asyncio.Lock()
        await bot.restart_lock.acquire()  # a /valhalla restart is in progress
        task = asyncio.create_task(bot.watch_connection())
        await asyncio.sleep(0.1)
        still_running = not task.done() and bot.exit_code == 0
        bot.restart_lock.release()
        await asyncio.wait_for(task, 5)
        return still_running, bot

    still_running, bot = asyncio.run(run())
    assert still_running
    assert bot.exit_code == 1
