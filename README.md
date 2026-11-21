# Heimdall — valhalla-discord-bot

Heimdall is a Discord bot that runs on the a discord server and lets members of
the Discord server check on, and control, apps running on a server labelled Valhalla.

## Commands

| Command                     | Who                         | What it does                                          |
| --------------------------- | --------------------------- | ----------------------------------------------------- |
| `/valhalla status`          | Everyone                    | Host CPU, memory, disk, uptime, and per-container use |
| `/valhalla restart Valheim` | Members of the restart role | Restarts Valheim, but only when nobody is playing     |

### How a restart works

1. Heimdall reads the Valheim image's `/status.json`
   ([status web server](https://github.com/community-valheim-tools/valheim-server-docker#status-web-server)).
   - `player_count > 0`: the restart is refused, and the requester is told
     that all players must disconnect first.
   - `player_count == 0`: it restarts.
   - status.json works but reports an `error` (the game server isn't answering
     Steam queries, e.g. it's hung or still starting): nobody can be connected,
     so it restarts.
   - status.json can't be read, or is over 90s old: the restart is refused,
     since Heimdall can't tell whether anyone is playing.
2. It posts a public announcement and restarts the container. Docker waits up
   to the Compose `stop_grace_period` (2m) for the world to save.
3. It follows the container's logs for a line containing both `supervisord`
   and `Game server connected`, e.g.
   `Nov 20 23:21:21 supervisord: valheim-server 11/20/2026 23:21:21: Game server connected`.
   When it appears, the announcement becomes *"Restart completed; the game
   server is ready for you to connect."* If it doesn't appear within 8 minutes,
   it becomes *"failed to detect if the server is ready. Your mileage may vary."*

Only one restart can run at a time.

### What the Valheim container needs

The status web server is off by default in the image. In the Valheim Compose
service, turn it on and publish it on localhost only:

```yaml
    ports:
      - "2456-2457:2456-2457/udp"
      - "127.0.0.1:9080:80/tcp"   # status.json for Heimdall
    environment:
      STATUS_HTTP: "true"          # only works with SERVER_PUBLIC=true
```

The container name (`valheim-valheim-1` by default from Compose project
`valheim`, service `valheim`) must match `VALHEIM_CONTAINER` and
[`deploy/heimdall.sudoers`](deploy/heimdall.sudoers).

## How it works

- Python 3.8+, [discord.py](https://discordpy.readthedocs.io/) for Discord,
  [psutil](https://psutil.readthedocs.io/) for host metrics. Uses ~40–60 MB of RAM.
- Runs as a systemd service under its own `heimdall` user, not in Docker, so it
  can read host metrics directly.
- It talks to Docker only through [`deploy/heimdall-docker`](deploy/heimdall-docker),
  a root-owned script that allows four actions: list containers, read
  their stats, and restart or follow the logs of the Valheim container. A sudoers rule lets the `heimdall` user run
  only those. Heimdall is never in the `docker` group (that would make it root).

## One-time Discord setup

1. Go to <https://discord.com/developers/applications> → **New Application** → name it *Heimdall*.
2. **Bot** tab → **Reset Token** → copy it (this is `DISCORD_TOKEN`).
   No privileged intents are needed; leave them all off.
3. **OAuth2 → URL Generator** → scopes `bot` and `applications.commands`;
   bot permissions `Send Messages` and `Embed Links`. Open the URL and add the
   bot to your server.
4. In Discord, enable *Settings → Advanced → Developer Mode*, then:
   - right-click the server → **Copy Server ID** (`DISCORD_GUILD_ID`)
   - create a role such as *Valhalla Admin*, right-click it → **Copy Role ID**
     (`RESTART_ROLE_ID`)

## Install on Valhalla

As root on the server, from a checkout of this repo:

```sh
useradd --system --no-create-home --shell /usr/sbin/nologin heimdall

# Docker helper + sudoers rule
install -D -o root -g root -m 0755 deploy/heimdall-docker /usr/local/libexec/heimdall/heimdall-docker
visudo -cf deploy/heimdall.sudoers && install -o root -g root -m 0440 deploy/heimdall.sudoers /etc/sudoers.d/heimdall

# App
apt-get install -y python3-venv
python3 -m venv /opt/heimdall/venv
/opt/heimdall/venv/bin/pip install .

# Config (fill in the values)
install -D -o root -g heimdall -m 0640 .env.example /etc/heimdall/heimdall.env
$EDITOR /etc/heimdall/heimdall.env

# Service
install -o root -g root -m 0644 deploy/heimdall.service /etc/systemd/system/heimdall.service
systemctl daemon-reload
systemctl enable --now heimdall
journalctl -u heimdall -f
```

Check the helper works before relying on the bot:

```sh
sudo -u heimdall sudo -n /usr/local/libexec/heimdall/heimdall-docker ps
```

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

To run the bot locally against a test Discord server, put your settings in
`.env` (git-ignored) and point `DOCKER_HELPER` at something that works on your
machine, e.g. `DOCKER_HELPER=sh deploy/heimdall-docker`:

```sh
set -a; . ./.env; set +a
.venv/bin/heimdall
```
