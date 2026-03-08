import logging
from typing import Optional

import aiohttp

import config

logger = logging.getLogger(__name__)

# Headers pour l'API REST Supabase
_HEADERS = {
    "apikey": config.SUPABASE_ANON_KEY,
    "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

_BASE_URL = f"{config.SUPABASE_URL}/rest/v1"
_session: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(headers=_HEADERS)
    return _session


async def insert_trade(trade_data: dict) -> Optional[dict]:
    """Insere un trade dans la table Supabase.

    trade_data doit contenir au minimum: symbol, side
    Peut aussi contenir: ep1, ep2, ep3, sl, tp1-tp6, trader, etc.
    """
    session = await _get_session()
    url = f"{_BASE_URL}/trades"

    try:
        async with session.post(url, json=trade_data) as resp:
            if resp.status == 201:
                result = await resp.json()
                row = result[0] if result else {}
                logger.info(
                    "Trade insere dans Supabase: id=%s %s %s",
                    row.get("id"), trade_data.get("symbol"), trade_data.get("side"),
                )
                return row
            else:
                error = await resp.text()
                logger.error("Erreur insertion Supabase (HTTP %d): %s", resp.status, error)
                return None
    except Exception as e:
        logger.error("Erreur connexion Supabase: %s", e)
        return None


async def update_trade(trade_id: int, updates: dict) -> Optional[dict]:
    """Met a jour un trade existant par son ID."""
    session = await _get_session()
    url = f"{_BASE_URL}/trades?id=eq.{trade_id}"

    try:
        async with session.patch(url, json=updates) as resp:
            if resp.status == 200:
                result = await resp.json()
                logger.info("Trade %d mis a jour: %s", trade_id, list(updates.keys()))
                return result[0] if result else {}
            else:
                error = await resp.text()
                logger.error("Erreur update Supabase (HTTP %d): %s", resp.status, error)
                return None
    except Exception as e:
        logger.error("Erreur connexion Supabase: %s", e)
        return None


async def find_open_trade(symbol: str, trader: str, side: str) -> Optional[dict]:
    """Trouve un trade ouvert par symbol, trader et side."""
    session = await _get_session()
    url = (
        f"{_BASE_URL}/trades"
        f"?symbol=eq.{symbol}&trader=eq.{trader}&side=eq.{side}"
        f"&status=eq.open&order=created_at.desc&limit=1"
    )
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                rows = await resp.json()
                return rows[0] if rows else None
            else:
                logger.error("Erreur recherche trade: %s", await resp.text())
                return None
    except Exception as e:
        logger.error("Erreur connexion Supabase: %s", e)
        return None


async def get_open_trades(symbol: Optional[str] = None) -> list:
    """Recupere les trades ouverts, optionnellement filtres par symbol."""
    session = await _get_session()
    url = f"{_BASE_URL}/trades?status=eq.open&order=created_at.desc"
    if symbol:
        url += f"&symbol=eq.{symbol}"

    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                logger.error("Erreur lecture Supabase: %s", await resp.text())
                return []
    except Exception as e:
        logger.error("Erreur connexion Supabase: %s", e)
        return []


async def insert_message(msg_data: dict) -> Optional[dict]:
    """Insere un message brut dans la table messages Supabase."""
    session = await _get_session()
    url = f"{_BASE_URL}/messages"

    try:
        async with session.post(url, json=msg_data) as resp:
            if resp.status == 201:
                result = await resp.json()
                return result[0] if result else {}
            elif resp.status == 409:
                return {}
            else:
                error = await resp.text()
                logger.error("Erreur insertion message Supabase (HTTP %d): %s", resp.status, error)
                return None
    except Exception as e:
        logger.error("Erreur connexion Supabase (message): %s", e)
        return None


async def close():
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None
