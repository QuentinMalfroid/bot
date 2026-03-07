import asyncio
import logging
import sys

import discord

import config
import database
from listener import TradeListener

# Configuration du logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)),
        logging.FileHandler(config.DATA_DIR / "cryptobot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("cryptobot")


async def main():
    if not config.DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN non configure! Copie .env.example vers .env et remplis ton token.")
        sys.exit(1)

    logger.info("Demarrage de CryptoBot...")
    logger.info("Serveurs surveilles: %s", config.GUILD_IDS or "TOUS")

    # Initialiser la base de donnees
    await database.init_db()

    # Lancer le listener Discord
    client = TradeListener()
    try:
        await client.start(config.DISCORD_TOKEN)
    except discord.LoginFailure:
        logger.error("Token Discord invalide! Verifie ton .env")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Arret demande par l'utilisateur.")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
