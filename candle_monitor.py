"""Monitor 5-minute candles for conditional stop-loss triggers.

When a trade has sl_type="candle_2x5m", instead of placing a STOP_MARKET order,
we monitor 5-minute candle closes. If 2 consecutive candles close beyond the SL
price, we trigger the stop-loss via a market order.

For LONG: 2 consecutive 5m candle closes BELOW sl_price → sell
For SHORT: 2 consecutive 5m candle closes ABOVE sl_price → sell

A small buffer (0.05%) is added to the SL price to ensure the stop truly triggers
above/below the level, not exactly at it.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

import binance_client
import config
import supabase_client

logger = logging.getLogger(__name__)

# Buffer: 0.05% beyond the SL price to confirm the candle truly closed past it
SL_BUFFER_PCT = 0.0005

@dataclass
class CandleSLWatch:
    trade_id: int
    symbol: str
    side: str  # LONG or SHORT
    sl_price: float
    consecutive_count: int = 0  # how many consecutive candles closed past SL


# Active watches: keyed by trade_id
_watches: dict[int, CandleSLWatch] = {}
_monitor_task: Optional[asyncio.Task] = None


def add_watch(trade_id: int, symbol: str, side: str, sl_price: float):
    """Register a candle-based SL watch for a trade."""
    if trade_id in _watches:
        logger.info("CANDLE_MON: Updating watch for trade #%s %s sl=%s", trade_id, symbol, sl_price)
        _watches[trade_id].sl_price = sl_price
        _watches[trade_id].consecutive_count = 0
        return

    _watches[trade_id] = CandleSLWatch(
        trade_id=trade_id,
        symbol=symbol,
        side=side,
        sl_price=sl_price,
    )
    logger.info(
        "CANDLE_MON: Added watch for trade #%s %s %s sl=%s (2x 5m candle)",
        trade_id, symbol, side, sl_price,
    )

    # Ensure monitor loop is running
    _ensure_monitor_running()


def is_watched(trade_id: int) -> bool:
    """Check if a trade has an active candle SL watch."""
    return trade_id in _watches


def remove_watch(trade_id: int):
    """Remove a candle-based SL watch."""
    if trade_id in _watches:
        w = _watches.pop(trade_id)
        logger.info("CANDLE_MON: Removed watch for trade #%s %s", trade_id, w.symbol)


def _ensure_monitor_running():
    """Start the candle monitoring loop if not already running."""
    global _monitor_task
    if _monitor_task is None or _monitor_task.done():
        try:
            _monitor_task = asyncio.create_task(_monitor_loop())
            logger.info("CANDLE_MON: Monitor loop started")
        except RuntimeError:
            # No running event loop (e.g. called from sync context)
            pass


async def _monitor_loop():
    """Main monitoring loop — checks candle closes every 30 seconds.

    We poll rather than use websockets for simplicity and reliability.
    At each 5-minute boundary (when a candle closes), we check the close price.
    """
    while _watches:
        try:
            await _check_candles()
        except Exception as e:
            logger.error("CANDLE_MON: Error in monitor loop: %s", e)

        # Sleep 30 seconds — we'll catch each 5min boundary within 30s
        await asyncio.sleep(30)

    logger.info("CANDLE_MON: Monitor loop stopped (no active watches)")


async def _check_candles():
    """Check the latest closed 5m candle for all watched symbols."""
    if not _watches:
        return

    # Group watches by symbol to minimize API calls
    symbols = set(w.symbol for w in _watches.values())

    for symbol in symbols:
        try:
            candle = await _get_latest_closed_5m_candle(symbol)
            if not candle:
                continue

            close_price = candle["close"]
            close_time = candle["close_time"]

            # Check each watch for this symbol
            triggered = []
            for trade_id, watch in list(_watches.items()):
                if watch.symbol != symbol:
                    continue

                # Skip if we already checked this candle
                last_check = getattr(watch, "_last_check_time", 0)
                if close_time <= last_check:
                    continue
                watch._last_check_time = close_time

                # Check if candle closed past the SL price
                sl_with_buffer = watch.sl_price
                if watch.side == "LONG":
                    # For LONG: candle must close BELOW sl_price (with small buffer)
                    sl_with_buffer = watch.sl_price * (1 - SL_BUFFER_PCT)
                    is_triggered = close_price <= sl_with_buffer
                else:
                    # For SHORT: candle must close ABOVE sl_price (with small buffer)
                    sl_with_buffer = watch.sl_price * (1 + SL_BUFFER_PCT)
                    is_triggered = close_price >= sl_with_buffer

                if is_triggered:
                    watch.consecutive_count += 1
                    logger.info(
                        "CANDLE_MON: Trade #%s %s candle closed at %s (SL=%s) — %d/2 consecutive",
                        trade_id, symbol, close_price, watch.sl_price, watch.consecutive_count,
                    )

                    if watch.consecutive_count >= 2:
                        triggered.append(trade_id)
                else:
                    if watch.consecutive_count > 0:
                        logger.info(
                            "CANDLE_MON: Trade #%s %s candle closed at %s (SL=%s) — reset to 0/2",
                            trade_id, symbol, close_price, watch.sl_price,
                        )
                    watch.consecutive_count = 0

            # Execute SL for triggered trades
            for trade_id in triggered:
                await _execute_candle_sl(trade_id)

        except Exception as e:
            logger.error("CANDLE_MON: Error checking %s: %s", symbol, e)


async def _get_latest_closed_5m_candle(symbol: str) -> Optional[dict]:
    """Get the most recently CLOSED 5-minute candle from Binance."""
    session = await binance_client._get_session()
    url = f"{binance_client.FUTURES_BASE}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": "5m", "limit": "2"}

    try:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        if not data or len(data) < 2:
            return None

        # data[0] is the previous closed candle, data[1] is the current (still open)
        # Use data[0] — the last fully closed candle
        candle = data[0]
        return {
            "open_time": candle[0],
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4]),
            "close_time": candle[6],
        }
    except Exception as e:
        logger.error("CANDLE_MON: Error fetching klines for %s: %s", symbol, e)
        return None


async def _execute_candle_sl(trade_id: int):
    """Execute the stop-loss via market order when 2 consecutive candles triggered."""
    watch = _watches.get(trade_id)
    if not watch:
        return

    logger.warning(
        "CANDLE_MON: TRIGGERING SL for trade #%s %s %s — 2 consecutive 5m candles past %s!",
        trade_id, watch.symbol, watch.side, watch.sl_price,
    )

    # Get the trade from Supabase
    trade = None
    trades = await supabase_client.get_open_trades(watch.symbol)
    for t in trades:
        if t["id"] == trade_id:
            trade = t
            break

    if not trade:
        logger.error("CANDLE_MON: Trade #%s not found in Supabase, removing watch", trade_id)
        remove_watch(trade_id)
        return

    # Cancel any remaining limit orders (EP2, EP3 still waiting)
    for ep in ("ep1", "ep2", "ep3"):
        if trade.get(f"{ep}_status") == "waiting" and trade.get(f"{ep}_id"):
            order_id = int(trade[f"{ep}_id"])
            await binance_client.futures_cancel_order(watch.symbol, order_id)
            logger.info("CANDLE_MON: Cancelled %s order %s for trade #%s", ep, order_id, trade_id)

    # Calculate total filled quantity
    total_qty = 0.0
    for ep in ("ep1", "ep2", "ep3"):
        if trade.get(f"{ep}_status") == "filled":
            qty = trade.get(f"{ep}_size_crypto")
            if qty:
                total_qty += float(qty)

    if total_qty <= 0:
        logger.warning("CANDLE_MON: Trade #%s has no filled quantity, removing watch", trade_id)
        remove_watch(trade_id)
        return

    # Verify position exists on Binance
    position = await binance_client.futures_get_position(watch.symbol)
    if not position or float(position.get("positionAmt", 0)) == 0:
        logger.warning("CANDLE_MON: No position on Binance for %s, closing trade in Supabase", watch.symbol)
        await supabase_client.update_trade(trade_id, {
            "status": "closed",
            "close_reason": "stopped_out",
            "sl_status": "filled",
        })
        remove_watch(trade_id)
        return

    # Close position with market order (only our trade's quantity)
    pos_amt = abs(float(position["positionAmt"]))
    close_qty = min(total_qty, pos_amt)
    close_side = "SELL" if watch.side == "LONG" else "BUY"

    result = await binance_client.futures_place_market_order(watch.symbol, close_side, close_qty)

    if result:
        # Calculate P&L
        total_cost = 0.0
        for ep in ("ep1", "ep2", "ep3"):
            if trade.get(f"{ep}_status") == "filled" and trade.get(ep):
                qty = float(trade.get(f"{ep}_size_crypto", 0))
                total_cost += float(trade[ep]) * qty

        mark_price = float(position.get("markPrice", 0))
        if total_cost > 0 and mark_price > 0:
            current_value = close_qty * mark_price
            if watch.side == "LONG":
                realized_pnl = current_value - total_cost
            else:
                realized_pnl = total_cost - current_value
        else:
            realized_pnl = 0.0

        await supabase_client.update_trade(trade_id, {
            "status": "closed",
            "close_reason": "stopped_out",
            "sl_status": "filled",
            "realized_pnl": round(realized_pnl, 2),
        })

        logger.warning(
            "CANDLE_MON: Trade #%s %s STOPPED OUT via candle SL — closed %s at market, P&L: %.2f",
            trade_id, watch.symbol, close_qty, realized_pnl,
        )

        # Check if a tracking trade on the same symbol can now be promoted
        import trade_executor
        await trade_executor._promote_tracking_trade(watch.symbol, watch.side, True)
    else:
        logger.error("CANDLE_MON: Failed to close position for trade #%s %s!", trade_id, watch.symbol)

    remove_watch(trade_id)


async def restore_watches():
    """On startup, restore candle watches for open trades that have candle-based SL.

    Detects candle SL trades by checking sl_type="candle_2x5m" in Supabase.
    Only watches trades that have at least one filled entry point.
    """
    trades = await supabase_client.get_open_trades()
    for trade in trades:
        if trade.get("sl_type") == "candle_2x5m" and trade.get("sl"):
            has_filled = any(
                trade.get(f"{ep}_status") == "filled" for ep in ("ep1", "ep2", "ep3")
            )
            if has_filled:
                add_watch(
                    trade_id=trade["id"],
                    symbol=trade["symbol"],
                    side=trade["side"],
                    sl_price=float(trade["sl"]),
                )
    if _watches:
        logger.info("CANDLE_MON: Restored %d candle SL watches from Supabase", len(_watches))
