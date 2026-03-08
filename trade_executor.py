"""Execute trades on Binance based on parsed Discord signals."""
import logging
from typing import Optional

import binance_client
import config
import supabase_client
from parser import TradeSignal, TradeAlert

logger = logging.getLogger(__name__)

# Symbols that exist on Binance spot (common ones)
# If a symbol is not found, we skip execution
_BINANCE_QUOTE = "USDT"


def _to_binance_symbol(asset: str) -> str:
    """Convert asset name to Binance symbol (e.g. ETH -> ETHUSDT)."""
    asset = asset.upper()
    if asset.endswith("USDT"):
        return asset
    return f"{asset}{_BINANCE_QUOTE}"


async def execute_trade_signal(trade: TradeSignal, channel_name: str, message_id: str):
    """Execute a new trade signal on Binance.

    Strategy:
    - Place limit orders at entry points (EP1, EP2)
    - Place stop-loss order once an entry fills
    - Track everything in Supabase
    """
    if not config.BINANCE_API_KEY:
        return

    symbol = _to_binance_symbol(trade.asset)

    # Verify symbol exists on Binance
    sym_info = await binance_client.get_symbol_info(symbol)
    if not sym_info:
        logger.warning("EXECUTOR: Symbol %s not found on Binance, skipping", symbol)
        return

    if sym_info.get("status") != "TRADING":
        logger.warning("EXECUTOR: Symbol %s not trading (status=%s)", symbol, sym_info.get("status"))
        return

    # Get current price for validation
    current_price = await binance_client.get_price(symbol)
    if not current_price:
        logger.error("EXECUTOR: Cannot get price for %s", symbol)
        return

    # Calculate position size
    trade_size_usdt = config.TRADE_SIZE_USDT
    usdt_balance = await binance_client.get_balance("USDT")
    if usdt_balance < trade_size_usdt:
        logger.warning(
            "EXECUTOR: Insufficient USDT balance (%.2f < %.2f) for %s",
            usdt_balance, trade_size_usdt, symbol,
        )
        return

    # Determine entry prices
    ep1_price = trade.entry_high  # First (closer) entry
    ep2_price = trade.entry_low if trade.entry_low != trade.entry_high else None

    if ep1_price is None:
        logger.error("EXECUTOR: No entry price for %s", symbol)
        return

    # Split size between entries
    if ep2_price:
        ep1_usdt = trade_size_usdt * 0.5
        ep2_usdt = trade_size_usdt * 0.5
    else:
        ep1_usdt = trade_size_usdt
        ep2_usdt = 0

    side = "BUY" if trade.direction == "LONG" else "SELL"

    # For SHORT on spot, we need to already hold the asset to sell
    # On demo, we'll handle LONG only for spot (BUY low, SELL high)
    if trade.direction == "SHORT":
        logger.info("EXECUTOR: SHORT signal on spot - skipping (spot only supports LONG)")
        return

    # Supabase row for tracking
    row = {
        "symbol": symbol,
        "side": trade.direction,
        "status": "open",
        "trader": trade.trader,
        "source_channel": channel_name,
        "discord_message_id": message_id,
        "ep1": ep1_price,
        "ep1_status": "waiting",
        "sl": trade.stop_loss,
        "sl_status": "waiting" if trade.stop_loss else None,
    }

    # Place EP1 limit order
    ep1_qty = ep1_usdt / ep1_price
    ep1_order = await binance_client.place_limit_order(symbol, side, ep1_qty, ep1_price)

    if ep1_order:
        row["ep1_id"] = str(ep1_order["orderId"])
        row["ep1_size_usdt"] = ep1_usdt
        ep1_qty_filled = float(ep1_order.get("executedQty", 0))
        row["ep1_size_crypto"] = float(ep1_order.get("origQty", ep1_qty))

        if ep1_order.get("status") == "FILLED":
            row["ep1_status"] = "filled"
            logger.info("EXECUTOR: EP1 immediately filled for %s", symbol)
        else:
            logger.info(
                "EXECUTOR: EP1 limit order placed for %s @ %s (orderId=%s)",
                symbol, ep1_price, ep1_order["orderId"],
            )
    else:
        logger.error("EXECUTOR: Failed to place EP1 order for %s", symbol)
        return

    # Place EP2 limit order if we have a second entry
    if ep2_price and ep2_usdt > 0:
        ep2_qty = ep2_usdt / ep2_price
        ep2_order = await binance_client.place_limit_order(symbol, side, ep2_qty, ep2_price)
        if ep2_order:
            row["ep2"] = ep2_price
            row["ep2_id"] = str(ep2_order["orderId"])
            row["ep2_status"] = "waiting"
            row["ep2_size_usdt"] = ep2_usdt
            row["ep2_size_crypto"] = float(ep2_order.get("origQty", ep2_qty))
            logger.info(
                "EXECUTOR: EP2 limit order placed for %s @ %s (orderId=%s)",
                symbol, ep2_price, ep2_order["orderId"],
            )

    # Insert trade in Supabase
    result = await supabase_client.insert_trade(row)
    if result:
        logger.info(
            "EXECUTOR: Trade #%s created - %s %s %s ep1=%s ep2=%s sl=%s",
            result.get("id"), symbol, trade.direction, trade.trader,
            ep1_price, ep2_price, trade.stop_loss,
        )


async def handle_alert(alert: TradeAlert):
    """Handle an active-alert by managing Binance orders accordingly.

    Actions:
    - Limit order filled -> place SL if not already placed
    - Stopped out / Closed -> cancel remaining orders, sell position
    - Stops moved to BE -> cancel old SL, place new SL at entry
    - Cancelled -> cancel all orders
    """
    if not config.BINANCE_API_KEY:
        return
    if not alert.traders:
        return

    symbol = _to_binance_symbol(alert.asset) if alert.asset else None
    if not symbol:
        return

    side = alert.direction
    if side == "SPOT":
        side = "LONG"

    for trader in alert.traders:
        existing = await supabase_client.find_open_trade(symbol, trader, side)
        if not existing:
            logger.debug(
                "EXECUTOR: No open trade found for %s %s %s (alert: %s)",
                symbol, side, trader, alert.action,
            )
            continue

        trade_id = existing["id"]

        # --- EP FILLED: Place SL order ---
        if alert.event_type == "ep_filled":
            await _handle_ep_filled(existing, symbol, trade_id)

        # --- CLOSED / STOPPED: Close position ---
        elif alert.new_status in ("closed", "cancelled"):
            await _handle_close(existing, symbol, trade_id, alert)

        # --- SL TO BE: Move stop to breakeven ---
        elif alert.event_type == "sl_to_be":
            await _handle_sl_to_be(existing, symbol, trade_id)

        # --- ENTRY UPDATED ---
        elif alert.event_type == "entry_updated" and alert.new_entry:
            logger.info(
                "EXECUTOR: Entry updated for trade #%s %s -> %s",
                trade_id, symbol, alert.new_entry,
            )


async def _handle_ep_filled(trade: dict, symbol: str, trade_id: int):
    """When an entry fills, place the stop-loss order."""
    sl_price = trade.get("sl")
    sl_id = trade.get("sl_id")

    if not sl_price or sl_id:
        # No SL needed or already placed
        return

    # Calculate total position size from filled entries
    total_qty = 0.0
    for ep in ("ep1", "ep2", "ep3"):
        if trade.get(f"{ep}_status") == "filled" or (ep == "ep1" and not trade.get("ep1_status")):
            qty = trade.get(f"{ep}_size_crypto")
            if qty:
                total_qty += float(qty)

    if total_qty <= 0:
        logger.warning("EXECUTOR: Cannot place SL - no filled quantity for trade #%s", trade_id)
        return

    # For LONG, SL is a SELL order
    sl_side = "SELL" if trade.get("side") == "LONG" else "BUY"
    sl_order = await binance_client.place_stop_loss_order(
        symbol, sl_side, total_qty, float(sl_price),
    )

    if sl_order:
        await supabase_client.update_trade(trade_id, {
            "sl_id": str(sl_order["orderId"]),
            "sl_status": "waiting",
        })
        logger.info(
            "EXECUTOR: SL order placed for trade #%s %s @ %s (orderId=%s)",
            trade_id, symbol, sl_price, sl_order["orderId"],
        )


async def _handle_close(trade: dict, symbol: str, trade_id: int, alert: TradeAlert):
    """Close a trade: cancel open orders and sell position."""
    # Cancel any open limit orders (unfilled entries)
    for ep in ("ep1", "ep2", "ep3"):
        if trade.get(f"{ep}_status") == "waiting" and trade.get(f"{ep}_id"):
            order_id = int(trade[f"{ep}_id"])
            await binance_client.cancel_order(symbol, order_id)
            logger.info("EXECUTOR: Cancelled %s order %s for trade #%s", ep, order_id, trade_id)

    # Cancel SL order if exists
    if trade.get("sl_id") and trade.get("sl_status") == "waiting":
        await binance_client.cancel_order(symbol, int(trade["sl_id"]))
        logger.info("EXECUTOR: Cancelled SL order for trade #%s", trade_id)

    # Sell any held position (for LONG trades)
    if trade.get("side") == "LONG":
        # Get actual balance of the asset
        base_asset = symbol.replace("USDT", "")
        balance = await binance_client.get_balance(base_asset)
        if balance > 0:
            sell_result = await binance_client.place_market_order(symbol, "SELL", quantity=balance)
            if sell_result:
                logger.info(
                    "EXECUTOR: Closed position for trade #%s - sold %s %s",
                    trade_id, sell_result.get("executedQty"), symbol,
                )

    # Update Supabase
    updates = {"status": alert.new_status or "closed"}
    if alert.close_reason:
        updates["close_reason"] = alert.close_reason
    await supabase_client.update_trade(trade_id, updates)
    logger.info(
        "EXECUTOR: Trade #%s closed - %s %s -> %s/%s",
        trade_id, symbol, trade.get("trader"),
        updates["status"], alert.close_reason,
    )


async def _handle_sl_to_be(trade: dict, symbol: str, trade_id: int):
    """Move stop-loss to breakeven (entry price)."""
    # Cancel existing SL
    if trade.get("sl_id") and trade.get("sl_status") == "waiting":
        await binance_client.cancel_order(symbol, int(trade["sl_id"]))

    # New SL at entry price (breakeven)
    be_price = trade.get("ep1")
    if not be_price:
        return

    total_qty = 0.0
    for ep in ("ep1", "ep2", "ep3"):
        if trade.get(f"{ep}_status") == "filled":
            qty = trade.get(f"{ep}_size_crypto")
            if qty:
                total_qty += float(qty)

    if total_qty <= 0:
        return

    sl_side = "SELL" if trade.get("side") == "LONG" else "BUY"
    sl_order = await binance_client.place_stop_loss_order(
        symbol, sl_side, total_qty, float(be_price),
    )

    if sl_order:
        await supabase_client.update_trade(trade_id, {
            "sl_id": str(sl_order["orderId"]),
            "sl_status": "waiting",
        })
        logger.info(
            "EXECUTOR: SL moved to BE for trade #%s %s @ %s",
            trade_id, symbol, be_price,
        )
