import asyncio
import logging
import re
from pathlib import Path

import aiohttp
import discord

import binance_client
import config
import database
import order_monitor
import parser as trade_parser
import supabase_client
import trade_executor

logger = logging.getLogger(__name__)


class TradeListener(discord.Client):
    """Client Discord qui ecoute les messages et les stocke."""

    def __init__(self):
        super().__init__()
        self._http_session: aiohttp.ClientSession | None = None
        self._role_map: dict[int, str] = {}  # role_id -> trader name

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

        # Build role map for WWG trader name resolution
        await self._build_role_map(monitored)

        # Join WWG threads so we receive their messages
        await self._join_wwg_threads()

        logger.info(
            "Listener pret. Surveillance de %d serveur(s). "
            "trades=%s, alerts=%s, futures=%s, spot=%s",
            len(monitored), config.TRADES_THREAD_ID, config.ALERTS_THREAD_ID,
            config.ACTIVE_FUTURES_ID, config.ACTIVE_SPOT_ID,
        )

        # Start Binance order monitor + restore candle SL watches
        if config.BINANCE_API_KEY:
            asyncio.create_task(order_monitor.start())
            import candle_monitor
            asyncio.create_task(candle_monitor.restore_watches())

    async def _build_role_map(self, guilds: list):
        """Build a mapping of role IDs to trader names from WWG roles."""
        for guild in guilds:
            for role in guild.roles:
                if role.name.startswith("WWG: "):
                    trader = role.name.removeprefix("WWG: ").lstrip("-").strip()
                    self._role_map[role.id] = trader
        logger.info("Role map built: %d WWG traders", len(self._role_map))

    def _resolve_roles(self, content: str) -> str:
        """Replace Discord role mentions <@&id> with @WWG: TraderName."""
        def replacer(match):
            role_id = int(match.group(1))
            trader = self._role_map.get(role_id)
            if trader:
                return f"@WWG: {trader}"
            return match.group(0)
        return re.sub(r"<@&(\d+)>", replacer, content)

    async def _join_wwg_threads(self):
        """Join all WWG threads so we receive their messages and edits."""
        thread_ids = [
            config.TRADES_THREAD_ID,
            config.ALERTS_THREAD_ID,
            config.ACTIVE_FUTURES_ID,
            config.ACTIVE_SPOT_ID,
        ]
        for tid in thread_ids:
            try:
                thread = self.get_channel(tid)
                if thread is None:
                    thread = await self.fetch_channel(tid)
                if thread and hasattr(thread, "join"):
                    await thread.join()
                    logger.info("Joined thread: %s (%s)", getattr(thread, "name", "?"), tid)
                elif thread:
                    logger.info("Channel found (not a thread): %s (%s)", getattr(thread, "name", "?"), tid)
                else:
                    logger.warning("Could not find thread/channel %s", tid)
            except Exception as e:
                logger.warning("Could not join thread %s: %s", tid, e)

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

        channel_id = after.channel.id
        channel_name = getattr(after.channel, "name", "DM")
        logger.debug("Message edite dans #%s: %s", channel_name, after.content[:100])

        # When WG Bot edits a trades message, check for TP updates in embeds
        if channel_id == config.TRADES_THREAD_ID and after.embeds:
            await self._process_tp_update_from_edit(after)

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

        # Only save WWG channel messages to Supabase
        if channel_id in config.WWG_CHANNEL_IDS:
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

        # 2. Router selon le thread/channel (WWG only)
        if channel_id in (config.ALERTS_THREAD_ID, config.ACTIVE_FUTURES_ID):
            await self._process_alerts(message)
        elif channel_id in (config.TRADES_THREAD_ID, config.ACTIVE_SPOT_ID):
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
        content = self._resolve_roles(message.content) if message.content else ""
        trade = trade_parser.parse_message(content, message.author.display_name)
        if trade:
            trade_dict = trade.to_dict()
            trade_dict["message_id"] = str(message.id)
            await database.save_parsed_trade(trade_dict)
            if channel_name not in trade_parser.CHANNELS_EXCLUDED_FROM_SUPABASE:
                await trade_executor.execute_trade_signal(
                    trade, channel_name, str(message.id), channel_id=message.channel.id,
                )

        for embed in message.embeds:
            embed_trade = trade_parser.parse_embed(embed.to_dict(), message.author.display_name)
            if embed_trade:
                embed_trade_dict = embed_trade.to_dict()
                embed_trade_dict["message_id"] = str(message.id)
                await database.save_parsed_trade(embed_trade_dict)
                if channel_name not in trade_parser.CHANNELS_EXCLUDED_FROM_SUPABASE:
                    await trade_executor.execute_trade_signal(
                        embed_trade, channel_name, str(message.id), channel_id=message.channel.id,
                    )

    async def _process_alerts(self, message: discord.Message):
        """Parse les alertes active-alerts, met a jour Supabase et gere les ordres Binance."""
        content = self._resolve_roles(message.content) if message.content else ""
        alerts = trade_parser.parse_alert_message(content)
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

                if alert.event_type in ("sl_to_be", "sl_moved"):
                    updates["sl_status"] = "closed"
                    if alert.new_sl_price:
                        updates["sl"] = alert.new_sl_price

                if alert.event_type == "tp_hit" and alert.tp_level:
                    tp_key = f"tp{alert.tp_level}_status"
                    if existing.get(tp_key) is not None or alert.tp_level <= 6:
                        updates[tp_key] = "filled"
                    # If TP + stops moved to BE (combined action)
                    if "stops moved to be" in (alert.action or "").lower():
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

                # Fetch TPs from linked trades message if available
                if alert.linked_message_id and alert.linked_channel_id:
                    await self._fetch_and_update_tps(
                        alert.linked_channel_id, alert.linked_message_id,
                        trade_id, existing,
                    )

            # Execute Binance actions for this alert
            await trade_executor.handle_alert(alert, channel_id=message.channel.id)

    async def _process_tp_update_from_edit(self, message: discord.Message):
        """When a trades message is edited, check embeds for TP updates."""
        for embed in message.embeds:
            embed_dict = embed.to_dict()
            desc = embed_dict.get("description", "")
            if not desc:
                continue

            # Extract direction and asset from embed: ":Long: **BTC** | ..."
            match = re.search(r":(Long|Short):\s*\*\*(\w+)\*\*", desc)
            if not match:
                continue

            direction = match.group(1).upper()
            asset = match.group(2).upper()
            symbol = f"{asset}USDT" if not asset.endswith("USDT") else asset

            tp_info = trade_parser.parse_embed_tps(embed_dict)
            if not tp_info or not tp_info.prices:
                continue

            # Find the matching open trade in Supabase
            # Try with common trader names
            trades = await supabase_client.get_open_trades(symbol)
            for trade in trades:
                if trade.get("side") != direction:
                    continue

                trade_id = trade["id"]
                tp_updates = {}
                for i, (price, hit) in enumerate(zip(tp_info.prices, tp_info.hit)):
                    tp_num = i + 1
                    if tp_num > 6:
                        break
                    tp_key = f"tp{tp_num}"
                    tp_updates[tp_key] = price
                    current_status = trade.get(f"{tp_key}_status")
                    if hit and current_status != "filled":
                        tp_updates[f"{tp_key}_status"] = "filled"
                    elif not hit and not current_status:
                        tp_updates[f"{tp_key}_status"] = "waiting"

                if tp_updates:
                    await supabase_client.update_trade(trade_id, tp_updates)
                    logger.info(
                        "TP_UPDATE: Trade #%s %s updated from edit: %s",
                        trade_id, symbol, tp_updates,
                    )

    async def _fetch_and_update_tps(self, channel_id: str, message_id: str,
                                     trade_id: int, existing: dict):
        """Fetch linked trades message via Discord API and extract TP levels."""
        try:
            channel = self.get_channel(int(channel_id))
            if channel is None:
                channel = await self.fetch_channel(int(channel_id))
            if channel is None:
                return

            # Fetch messages around the target to find it
            async for msg in channel.history(around=discord.Object(id=int(message_id)), limit=5):
                if str(msg.id) == message_id:
                    # Parse TPs from embeds
                    for embed in msg.embeds:
                        tp_info = trade_parser.parse_embed_tps(embed.to_dict())
                        if tp_info and tp_info.prices:
                            tp_updates = {}
                            for i, (price, hit) in enumerate(zip(tp_info.prices, tp_info.hit)):
                                tp_num = i + 1
                                if tp_num > 6:
                                    break
                                tp_key = f"tp{tp_num}"
                                tp_updates[tp_key] = price
                                if hit:
                                    tp_updates[f"{tp_key}_status"] = "filled"
                                elif not existing.get(f"{tp_key}_status"):
                                    tp_updates[f"{tp_key}_status"] = "waiting"

                            if tp_updates:
                                await supabase_client.update_trade(trade_id, tp_updates)
                                logger.info(
                                    "ALERT: TPs updated for trade #%s from linked message: %s",
                                    trade_id, tp_updates,
                                )
                    return
        except Exception as e:
            logger.warning("ALERT: Could not fetch TPs from message %s: %s", message_id, e)

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
        order_monitor.stop()
        if self._http_session:
            await self._http_session.close()
        await binance_client.close()
        await supabase_client.close()
        await super().close()
