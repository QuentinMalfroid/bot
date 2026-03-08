import asyncio
import logging
from pathlib import Path

import aiohttp
import discord

import binance_client
import config
import database
import parser as trade_parser
import supabase_client
import trade_executor

logger = logging.getLogger(__name__)


class TradeListener(discord.Client):
    """Client Discord qui ecoute les messages et les stocke."""

    def __init__(self):
        super().__init__()
        self._http_session: aiohttp.ClientSession | None = None

    async def on_ready(self):
        logger.info("Connecte en tant que: %s (ID: %s)", self.user, self.user.id)

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

        logger.info(
            "Listener pret. Surveillance de %d serveur(s). Threads: trades=%s, alerts=%s",
            len(monitored), config.TRADES_THREAD_ID, config.ALERTS_THREAD_ID,
        )

    async def on_message(self, message: discord.Message):
        if message.author.id == self.user.id:
            return

        if message.guild and config.GUILD_IDS and message.guild.id not in config.GUILD_IDS:
            return

        await self._process_message(message)

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.author.id == self.user.id:
            return
        if after.guild and config.GUILD_IDS and after.guild.id not in config.GUILD_IDS:
            return

        channel_name = getattr(after.channel, "name", "DM")
        logger.debug("Message edite dans #%s: %s", channel_name, after.content[:100])
        await self._process_message(after)

    async def _process_message(self, message: discord.Message):
        """Traite un message : sauvegarde brute + parsing + telechargement images."""
        guild_name = message.guild.name if message.guild else "DM"
        channel_name = getattr(message.channel, "name", "unknown")
        channel_id = message.channel.id

        # 1. Sauvegarder le message brut
        msg_data = {
            "discord_message_id": str(message.id),
            "guild_id": str(message.guild.id) if message.guild else "0",
            "guild_name": guild_name,
            "channel_id": str(channel_id),
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

        # Envoyer le message brut vers Supabase
        supabase_msg = {
            "discord_message_id": str(message.id),
            "guild_id": str(message.guild.id) if message.guild else "0",
            "guild_name": guild_name,
            "channel_id": str(channel_id),
            "channel_name": channel_name,
            "author_id": str(message.author.id),
            "author_name": message.author.display_name,
            "is_bot": message.author.bot,
            "content": message.content or "",
            "embeds": [e.to_dict() for e in message.embeds],
            "attachments": [
                {"url": a.url, "filename": a.filename, "content_type": a.content_type}
                for a in message.attachments
            ],
            "created_at": message.created_at.isoformat(),
        }
        await supabase_client.insert_message(supabase_msg)

        logger.debug(
            "[#%s] %s: %s",
            channel_name,
            message.author.display_name,
            message.content[:100] if message.content else "(embed/attachment)",
        )

        # 2. Router selon le thread/channel
        if channel_id == config.ALERTS_THREAD_ID:
            await self._process_alerts(message)
        elif channel_id == config.TRADES_THREAD_ID:
            await self._process_trade_signal(message, channel_name)
        else:
            # Autres channels: tenter le parsing generique
            await self._process_trade_signal(message, channel_name)

        # 3. Telecharger les images
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

    async def _process_trade_signal(self, message: discord.Message, channel_name: str):
        """Parse et insere un nouveau signal de trade, puis execute sur Binance."""
        trade = trade_parser.parse_message(message.content, message.author.display_name)
        if trade:
            trade_dict = trade.to_dict()
            trade_dict["message_id"] = str(message.id)
            await database.save_parsed_trade(trade_dict)
            if channel_name not in trade_parser.CHANNELS_EXCLUDED_FROM_SUPABASE:
                await trade_executor.execute_trade_signal(trade, channel_name, str(message.id))

        for embed in message.embeds:
            embed_trade = trade_parser.parse_embed(embed.to_dict(), message.author.display_name)
            if embed_trade:
                embed_trade_dict = embed_trade.to_dict()
                embed_trade_dict["message_id"] = str(message.id)
                await database.save_parsed_trade(embed_trade_dict)
                if channel_name not in trade_parser.CHANNELS_EXCLUDED_FROM_SUPABASE:
                    await trade_executor.execute_trade_signal(embed_trade, channel_name, str(message.id))

    async def _process_alerts(self, message: discord.Message):
        """Parse les alertes active-alerts, met a jour Supabase et gere les ordres Binance."""
        alerts = trade_parser.parse_alert_message(message.content)
        if not alerts:
            return

        for alert in alerts:
            if not alert.traders:
                continue

            symbol = alert.asset
            if symbol and not symbol.endswith("USDT"):
                symbol = f"{symbol}USDT"

            side = alert.direction
            if side == "SPOT":
                side = "LONG"

            for trader in alert.traders:
                existing = await supabase_client.find_open_trade(symbol, trader, side)
                if not existing:
                    logger.warning(
                        "ALERT: Trade ouvert non trouve pour %s %s %s - action: %s",
                        symbol, side, trader, alert.action,
                    )
                    continue

                trade_id = existing["id"]
                updates = {"updated_at": "now()"}

                if alert.new_status and alert.new_status != "open":
                    updates["status"] = alert.new_status
                if alert.close_reason:
                    updates["close_reason"] = alert.close_reason

                if alert.event_type == "ep_filled":
                    if existing.get("ep1_status") == "waiting":
                        updates["ep1_status"] = "filled"
                    elif existing.get("ep2_status") == "waiting":
                        updates["ep2_status"] = "filled"

                if alert.event_type == "sl_to_be":
                    updates["sl_status"] = "closed"

                if alert.new_entry:
                    updates["ep1"] = alert.new_entry
                    updates["ep1_status"] = "filled"

                result = await supabase_client.update_trade(trade_id, updates)
                if result:
                    logger.info(
                        "ALERT: Trade #%s mis a jour - %s %s %s -> %s",
                        trade_id, symbol, trader, alert.action,
                        {k: v for k, v in updates.items() if k != "updated_at"},
                    )

            # Execute Binance actions for this alert
            await trade_executor.handle_alert(alert)

    async def _download_image(self, attachment: discord.Attachment, message_id: str) -> Path | None:
        try:
            if self._http_session is None:
                self._http_session = aiohttp.ClientSession()

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
        await binance_client.close()
        await supabase_client.close()
        await super().close()
