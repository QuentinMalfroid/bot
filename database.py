import aiosqlite
import json
import logging
from datetime import datetime
from pathlib import Path

import config

logger = logging.getLogger(__name__)


async def init_db():
    """Initialise la base de donnees et cree les tables."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS raw_messages (
                id INTEGER PRIMARY KEY,
                discord_message_id TEXT UNIQUE NOT NULL,
                guild_id TEXT NOT NULL,
                guild_name TEXT,
                channel_id TEXT NOT NULL,
                channel_name TEXT,
                author_id TEXT NOT NULL,
                author_name TEXT,
                is_bot INTEGER DEFAULT 0,
                content TEXT,
                embeds TEXT,
                attachments TEXT,
                created_at TEXT NOT NULL,
                captured_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS parsed_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL,
                trader TEXT,
                direction TEXT,
                asset TEXT,
                order_type TEXT,
                entry_low REAL,
                entry_high REAL,
                stop_loss REAL,
                take_profit REAL,
                risk_pct REAL,
                pnl_pct REAL,
                raw_signal TEXT,
                parsed_at TEXT NOT NULL,
                FOREIGN KEY (message_id) REFERENCES raw_messages(discord_message_id)
            );

            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL,
                original_url TEXT NOT NULL,
                filename TEXT NOT NULL,
                local_path TEXT,
                content_type TEXT,
                downloaded_at TEXT,
                FOREIGN KEY (message_id) REFERENCES raw_messages(discord_message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_raw_messages_channel
                ON raw_messages(channel_id);
            CREATE INDEX IF NOT EXISTS idx_raw_messages_created
                ON raw_messages(created_at);
            CREATE INDEX IF NOT EXISTS idx_parsed_trades_asset
                ON parsed_trades(asset);
            CREATE INDEX IF NOT EXISTS idx_parsed_trades_trader
                ON parsed_trades(trader);
        """)
        await db.commit()
        logger.info("Base de donnees initialisee: %s", config.DATABASE_PATH)


async def save_raw_message(msg_data: dict):
    """Sauvegarde un message brut dans la base."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        try:
            await db.execute(
                """INSERT OR IGNORE INTO raw_messages
                   (discord_message_id, guild_id, guild_name, channel_id,
                    channel_name, author_id, author_name, is_bot,
                    content, embeds, attachments, created_at, captured_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(msg_data["discord_message_id"]),
                    str(msg_data["guild_id"]),
                    msg_data.get("guild_name", ""),
                    str(msg_data["channel_id"]),
                    msg_data.get("channel_name", ""),
                    str(msg_data["author_id"]),
                    msg_data.get("author_name", ""),
                    1 if msg_data.get("is_bot") else 0,
                    msg_data.get("content", ""),
                    json.dumps(msg_data.get("embeds", []), ensure_ascii=False),
                    json.dumps(msg_data.get("attachments", []), ensure_ascii=False),
                    msg_data["created_at"],
                    datetime.utcnow().isoformat(),
                ),
            )
            await db.commit()
            return True
        except Exception as e:
            logger.error("Erreur sauvegarde message %s: %s", msg_data.get("discord_message_id"), e)
            return False


async def save_parsed_trade(trade_data: dict):
    """Sauvegarde un trade parse dans la base."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        try:
            await db.execute(
                """INSERT INTO parsed_trades
                   (message_id, trader, direction, asset, order_type,
                    entry_low, entry_high, stop_loss, take_profit,
                    risk_pct, pnl_pct, raw_signal, parsed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(trade_data["message_id"]),
                    trade_data.get("trader"),
                    trade_data.get("direction"),
                    trade_data.get("asset"),
                    trade_data.get("order_type"),
                    trade_data.get("entry_low"),
                    trade_data.get("entry_high"),
                    trade_data.get("stop_loss"),
                    trade_data.get("take_profit"),
                    trade_data.get("risk_pct"),
                    trade_data.get("pnl_pct"),
                    trade_data.get("raw_signal", ""),
                    datetime.utcnow().isoformat(),
                ),
            )
            await db.commit()
            return True
        except Exception as e:
            logger.error("Erreur sauvegarde trade: %s", e)
            return False


async def save_attachment(att_data: dict):
    """Sauvegarde les metadonnees d'une piece jointe."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        try:
            await db.execute(
                """INSERT INTO attachments
                   (message_id, original_url, filename, local_path,
                    content_type, downloaded_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(att_data["message_id"]),
                    att_data["original_url"],
                    att_data["filename"],
                    att_data.get("local_path"),
                    att_data.get("content_type"),
                    datetime.utcnow().isoformat(),
                ),
            )
            await db.commit()
        except Exception as e:
            logger.error("Erreur sauvegarde attachment: %s", e)
