"""Binance order monitor — polls open orders and syncs with Supabase.

Detects EP fills on Binance, places SL automatically, and updates Supabase.
This ensures we don't rely solely on Discord alerts for order management.
"""
import asyncio
import logging
from typing import Optional

import binance_client
import config
import risk_manager
import supabase_client

logger = logging.getLogger(__name__)

_running = False


async def start():
    """Start the order monitoring loop."""
    global _running
    if _running:
        return
    _running = True
    logger.info("ORDER_MONITOR: Started (polling every %ds)", config.ORDER_POLL_INTERVAL)
    while _running:
        try:
            await _check_open_trades()
        except Exception as e:
            logger.error("ORDER_MONITOR: Error during check: %s", e)
        await asyncio.sleep(config.ORDER_POLL_INTERVAL)


def stop():
    """Stop the monitoring loop."""
    global _running
    _running = False
    logger.info("ORDER_MONITOR: Stopped")


async def _check_open_trades():
    """Check all open trades in Supabase against Binance order status."""
    open_trades = await supabase_client.get_open_trades()
    if not open_trades:
        return

    for trade in open_trades:
        try:
            await _sync_trade(trade)
        except Exception as e:
            logger.error(
                "ORDER_MONITOR: Error syncing trade #%s %s: %s",
                trade.get("id"), trade.get("symbol"), e,
            )


async def _sync_trade(trade: dict):
    """Sync a single trade's Binance orders with Supabase state."""
    trade_id = trade["id"]
    symbol = trade.get("symbol")
    if not symbol:
        return

    # Skip tracking-only trades (no Binance orders to sync)
    if trade.get("source_channel") == "tracking":
        return

    # Determine if futures or spot based on source_channel
    use_futures = _is_futures_trade(trade)
    updates = {}
    newly_filled_eps = []

    # Check each entry point order
    for ep in ("ep1", "ep2", "ep3"):
        order_id = trade.get(f"{ep}_id")
        current_status = trade.get(f"{ep}_status")

        if not order_id or current_status != "waiting":
            continue

        # Query Binance for actual order status
        if use_futures:
            binance_order = await binance_client.futures_get_order(symbol, int(order_id))
        else:
            binance_order = await binance_client.get_order(symbol, int(order_id))

        if not binance_order:
            continue

        binance_status = binance_order.get("status")

        if binance_status == "FILLED":
            updates[f"{ep}_status"] = "filled"
            filled_qty = float(binance_order.get("executedQty", 0))
            filled_price = float(binance_order.get("avgPrice", 0)) or trade.get(ep)
            if filled_qty > 0:
                updates[f"{ep}_size_crypto"] = filled_qty
                updates[f"{ep}_size_usdt"] = filled_qty * filled_price
            newly_filled_eps.append(ep)
            logger.info(
                "ORDER_MONITOR: %s FILLED for trade #%s %s — qty=%s @ %s (Binance orderId=%s)",
                ep.upper(), trade_id, symbol, filled_qty, filled_price, order_id,
            )

        elif binance_status == "CANCELED":
            updates[f"{ep}_status"] = None  # constraint only allows waiting/filled/null
            updates[f"{ep}_id"] = None
            logger.info(
                "ORDER_MONITOR: %s CANCELLED on Binance for trade #%s %s",
                ep.upper(), trade_id, symbol,
            )

        elif binance_status == "PARTIALLY_FILLED":
            filled_qty = float(binance_order.get("executedQty", 0))
            logger.info(
                "ORDER_MONITOR: %s PARTIALLY FILLED for trade #%s %s — %s filled",
                ep.upper(), trade_id, symbol, filled_qty,
            )

    # Check SL order status
    sl_id = trade.get("sl_id")
    sl_status = trade.get("sl_status")
    if sl_id and sl_status == "waiting":
        if use_futures:
            sl_order = await binance_client.futures_get_order(symbol, int(sl_id))
        else:
            sl_order = await binance_client.get_order(symbol, int(sl_id))

        if sl_order:
            sl_binance_status = sl_order.get("status")
            if sl_binance_status == "FILLED":
                updates["sl_status"] = "filled"
                updates["status"] = "closed"
                updates["close_reason"] = "sl_hit"
                # Calculate P&L from SL fill
                sl_fill_price = float(sl_order.get("avgPrice", 0)) or float(trade.get("sl", 0))
                sl_qty = float(sl_order.get("executedQty", 0))
                pnl = _calculate_pnl(trade, sl_fill_price, sl_qty)
                if pnl is not None:
                    updates["realized_pnl"] = round(pnl, 2)
                    risk_manager.record_trade_result(pnl)
                logger.info(
                    "ORDER_MONITOR: SL HIT for trade #%s %s — P&L: %s USDT",
                    trade_id, symbol,
                    f"{pnl:.2f}" if pnl is not None else "unknown",
                )
            elif sl_binance_status == "CANCELED":
                updates["sl_status"] = None
                updates["sl_id"] = None
                logger.warning(
                    "ORDER_MONITOR: SL CANCELLED on Binance for trade #%s %s!",
                    trade_id, symbol,
                )
        else:
            # SL order not found — likely an algo order (STOP_MARKET via algoOrder API)
            # which can't be queried on testnet. Check position instead.
            has_filled = any(trade.get(f"{ep}_status") == "filled" for ep in ("ep1", "ep2", "ep3"))
            if has_filled and use_futures:
                pos = await binance_client.futures_get_position(symbol)
                if not pos or float(pos.get("positionAmt", 0)) == 0:
                    # Position gone = SL was triggered
                    updates["sl_status"] = "filled"
                    updates["status"] = "closed"
                    updates["close_reason"] = "sl_hit"
                    sl_price = float(trade.get("sl", 0))
                    total_qty = sum(
                        float(trade.get(f"{ep}_size_crypto") or 0)
                        for ep in ("ep1", "ep2", "ep3")
                        if trade.get(f"{ep}_status") == "filled"
                    )
                    pnl = _calculate_pnl(trade, sl_price, total_qty)
                    if pnl is not None:
                        updates["realized_pnl"] = round(pnl, 2)
                        risk_manager.record_trade_result(pnl)
                    logger.info(
                        "ORDER_MONITOR: SL HIT (position gone) for trade #%s %s — P&L: %s USDT",
                        trade_id, symbol,
                        f"{pnl:.2f}" if pnl is not None else "unknown",
                    )

    # If EPs just filled and no SL order exists yet, place it
    if newly_filled_eps and not trade.get("sl_id"):
        await _place_sl_for_trade(trade, updates, symbol, trade_id, use_futures)

    # Apply updates to Supabase
    if updates:
        await supabase_client.update_trade(trade_id, updates)
        logger.info(
            "ORDER_MONITOR: Trade #%s %s synced — %s",
            trade_id, symbol, list(updates.keys()),
        )

        # If trade just closed, check for promotable tracking trades
        if updates.get("status") == "closed":
            import trade_executor
            await trade_executor._promote_tracking_trade(
                symbol, trade.get("side", "LONG"), use_futures,
            )


async def _place_sl_for_trade(trade: dict, updates: dict, symbol: str,
                               trade_id: int, use_futures: bool):
    """Place SL order after detecting EP fill on Binance."""
    sl_price = trade.get("sl")
    if not sl_price:
        logger.warning("ORDER_MONITOR: No SL price for trade #%s, cannot place SL", trade_id)
        return

    # Calculate total filled quantity
    total_qty = 0.0
    for ep in ("ep1", "ep2", "ep3"):
        # Check both existing state and pending updates
        ep_status = updates.get(f"{ep}_status", trade.get(f"{ep}_status"))
        if ep_status == "filled":
            qty = updates.get(f"{ep}_size_crypto", trade.get(f"{ep}_size_crypto"))
            if qty:
                total_qty += float(qty)

    if total_qty <= 0:
        return

    sl_side = "SELL" if trade.get("side") == "LONG" else "BUY"

    if use_futures:
        sl_order = await binance_client.futures_place_stop_loss_order(
            symbol, sl_side, total_qty, float(sl_price),
        )
    else:
        sl_order = await binance_client.place_stop_loss_order(
            symbol, sl_side, total_qty, float(sl_price),
        )

    if sl_order:
        updates["sl_id"] = str(sl_order["orderId"])
        updates["sl_status"] = "waiting"
        logger.info(
            "ORDER_MONITOR: SL placed for trade #%s %s @ %s qty=%s (orderId=%s)",
            trade_id, symbol, sl_price, total_qty, sl_order["orderId"],
        )


def _is_futures_trade(trade: dict) -> bool:
    """Determine if a trade is futures based on source channel or side."""
    channel = trade.get("source_channel", "")
    if "spot" in channel.lower():
        return False
    # SHORT is always futures
    if trade.get("side") == "SHORT":
        return True
    return True  # Default to futures for WWG trades


def _calculate_pnl(trade: dict, close_price: float, close_qty: float) -> Optional[float]:
    """Calculate realized P&L from trade data."""
    direction = trade.get("side", "LONG")

    # Calculate cost basis from filled EPs
    total_cost = 0.0
    total_qty = 0.0
    for ep in ("ep1", "ep2", "ep3"):
        if trade.get(f"{ep}_status") == "filled" and trade.get(ep):
            qty = trade.get(f"{ep}_size_crypto")
            if qty:
                total_cost += float(trade[ep]) * float(qty)
                total_qty += float(qty)

    if total_qty <= 0:
        return None

    avg_entry = total_cost / total_qty
    close_value = close_price * close_qty

    if direction == "LONG":
        return close_value - (avg_entry * close_qty)
    else:  # SHORT
        return (avg_entry * close_qty) - close_value
