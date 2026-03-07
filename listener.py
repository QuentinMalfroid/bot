import asyncio
import logging
from pathlib import Path

import aiohttp
import discord

import config
import database
import parser as trade_parser
import supabase_client

logger = logging.getLogger(__name__)


class TradeListener(discord.Client):
    """Client Discord qui ecoute les messages et les stocke."""

    def __init__(self):
        # Pas d'intents privilegies pour un selfbot, on prend ce qui est dispo
        super().__init__()
        self._http_session: aiohttp.ClientSession | None = None

    async def on_ready(self):
        logger.info("Connecte en tant que: %s (ID: %s)", self.user, self.user.id)

        # Lister les serveurs surveilles
        monitored = []
        for guild in self.guilds:
            if not config.GUILD_IDS or guild.id in config.GUILD_IDS:
                monitored.append(guild)
                channels = [ch.name for ch in guild.text_channels]
                logger.info(
                    "Serveur: %s (%s) - %d channels texte: %s",
                    guild.name, guild.id, len(channels), ", ".join(channels[:10]),
                )

        if not monitored:
            logger.warning(
                "Aucun serveur surveille trouve! IDs configures: %s | Serveurs disponibles: %s",
                config.GUILD_IDS,
                [(g.name, g.id) for g in self.guilds],
            )

        logger.info("Listener pret. Surveillance de %d serveur(s).", len(monitored))

    async def on_message(self, message: discord.Message):
        # Ignorer nos propres messages
        if message.author.id == self.user.id:
            return

        # Filtrer par serveur si des IDs sont configures
        if message.guild and config.GUILD_IDS and message.guild.id not in config.GUILD_IDS:
            return

        await self._process_message(message)

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """Capturer les edits (les bots modifient parfois leurs messages)."""
        if after.author.id == self.user.id:
            return
        if after.guild and config.GUILD_IDS and after.guild.id not in config.GUILD_IDS:
            return

        logger.debug("Message edite dans #%s: %s", after.channel.name, after.content[:100])
        await self._process_message(after)

    async def _process_message(self, message: discord.Message):
        """Traite un message : sauvegarde brute + parsing + telechargement images."""
        guild_name = message.guild.name if message.guild else "DM"
        channel_name = getattr(message.channel, "name", "unknown")

        # 1. Sauvegarder le message brut
        msg_data = {
            "discord_message_id": str(message.id),
            "guild_id": str(message.guild.id) if message.guild else "0",
            "guild_name": guild_name,
            "channel_id": str(message.channel.id),
            "channel_name": channel_name,
            "author_id": str(message.author.id),
            "author_name": message.author.display_name,
            "is_bot": message.author.bot,
            "content": message.content,
            "embeds": [e.to_dict() for e in message.embeds],
            "attachments": [
                {"url": a.url, "filename": a.filename, "content_type": a.content_type}
                for a in message.attachments
            ],
            "created_at": message.created_at.isoformat(),
        }

        saved = await database.save_raw_message(msg_data)
        if not saved:
            return

        logger.debug(
            "[#%s] %s: %s",
            channel_name,
            message.author.display_name,
            message.content[:100] if message.content else "(embed/attachment)",
        )

        # 2. Tenter de parser le contenu comme signal de trade
        trade = trade_parser.parse_message(message.content, message.author.display_name)
        if trade:
            trade_dict = trade.to_dict()
            trade_dict["message_id"] = str(message.id)
            await database.save_parsed_trade(trade_dict)
            # Envoyer vers Supabase seulement si le channel n'est pas exclu
            if channel_name not in trade_parser.CHANNELS_EXCLUDED_FROM_SUPABASE:
                await self._push_to_supabase(trade, channel_name, str(message.id))

        # 3. Parser les embeds aussi
        for embed in message.embeds:
            embed_trade = trade_parser.parse_embed(embed.to_dict(), message.author.display_name)
            if embed_trade:
                embed_trade_dict = embed_trade.to_dict()
                embed_trade_dict["message_id"] = str(message.id)
                await database.save_parsed_trade(embed_trade_dict)
                if channel_name not in trade_parser.CHANNELS_EXCLUDED_FROM_SUPABASE:
                    await self._push_to_supabase(embed_trade, channel_name, str(message.id))

        # 4. Telecharger les images
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith("image/"):
                local_path = await self._download_image(attachment, str(message.id))
                await database.save_attachment({
                    "message_id": str(message.id),
                    "original_url": attachment.url,
                    "filename": attachment.filename,
                    "local_path": str(local_path) if local_path else None,
                    "content_type": attachment.content_type,
                })

    async def _push_to_supabase(self, trade: trade_parser.TradeSignal, channel_name: str, message_id: str):
        """Convertit un TradeSignal en row Supabase et l'insere."""
        # Construire le symbol au format XXXUSDT
        symbol = trade.asset
        if symbol and not symbol.endswith("USDT"):
            symbol = f"{symbol}USDT"

        row = {
            "symbol": symbol,
            "side": trade.direction,
            "status": "open",
            "trader": trade.trader,
            "source_channel": channel_name,
            "discord_message_id": message_id,
            "sl": trade.stop_loss,
            "sl_status": "waiting" if trade.stop_loss else None,
        }

        # Entry points : ep1 = entry_high, ep2 = entry_low (si different)
        if trade.entry_high is not None:
            row["ep1"] = trade.entry_high
            row["ep1_status"] = "waiting"

        if trade.entry_low is not None and trade.entry_low != trade.entry_high:
            row["ep2"] = trade.entry_low
            row["ep2_status"] = "waiting"

        # Take profit si disponible
        if trade.take_profit is not None:
            row["tp1"] = trade.take_profit
            row["tp1_status"] = "waiting"

        result = await supabase_client.insert_trade(row)
        if result:
            logger.info(
                "SUPABASE: Trade #%s insere - %s %s ep1=%s sl=%s",
                result.get("id"), symbol, trade.direction,
                row.get("ep1"), row.get("sl"),
            )

    async def _download_image(self, attachment: discord.Attachment, message_id: str) -> Path | None:
        """Telecharge une image localement."""
        try:
            if self._http_session is None:
                self._http_session = aiohttp.ClientSession()

            # Nommer le fichier avec l'ID du message pour eviter les doublons
            ext = Path(attachment.filename).suffix or ".png"
            filename = f"{message_id}_{attachment.filename}"
            local_path = config.IMAGES_DIR / filename

            if local_path.exists():
                return local_path

            async with self._http_session.get(attachment.url) as resp:
                if resp.status == 200:
                    local_path.write_bytes(await resp.read())
                    logger.debug("Image telechargee: %s", local_path)
                    return local_path
                else:
                    logger.warning("Echec telechargement image %s: HTTP %d", attachment.url, resp.status)
                    return None
        except Exception as e:
            logger.error("Erreur telechargement image: %s", e)
            return None

    async def close(self):
        if self._http_session:
            await self._http_session.close()
        await supabase_client.close()
        await super().close()
