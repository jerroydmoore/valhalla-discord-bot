from __future__ import annotations

import logging

import discord

from .bot import Heimdall
from .config import Config


def main() -> None:
    config = Config.from_env()
    discord.utils.setup_logging(level=logging.INFO)
    Heimdall(config).run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
