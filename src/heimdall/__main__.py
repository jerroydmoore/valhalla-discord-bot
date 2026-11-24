from __future__ import annotations

import logging
import sys

import discord

from .bot import Heimdall
from .config import Config


def main() -> None:
    config = Config.from_env()
    discord.utils.setup_logging(level=logging.INFO)
    bot = Heimdall(config)
    bot.run(config.token, log_handler=None)
    if bot.exit_code:
        sys.exit(bot.exit_code)


if __name__ == "__main__":
    main()
