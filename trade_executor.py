"""Execute trades on Binance based on parsed Discord signals.

Routing by channel:
- trades/active-alerts threads -> Futures (testnet.binancefuture.com)
- spot channels -> Spot (demo-api.binance.com)

Uses risk_manager for 1R-based dynamic position sizing.
"""
import logging
from typing import Optional

import binance_client
import candle_monitor
import config
import risk_manager
import supabase_client
from parser import TradeSignal, TradeAlert

logger = logging.getLogger(__name__)

_BINANCE_QUOTE = "USDT"

# Channel IDs that route to futures
_FUTURES_CHANNEL_IDS = {
    config.TRADES_THREAD_ID,      # trades thread
    config.ALERTS_THREAD_ID,      # active-alerts thread
    config.ACTIVE_FUTURES_ID,     # active-futures thread
}

# Channel IDs that route to spot
_SPOT_CHANNEL_IDS = {
    config.ACTIVE_SPOT_ID,        # active-spot thread
}


def _to_binance_symbol(asset: str) -> str:
    """Convert asset name to Binance symbol (e.g. ETH -> ETHUSDT)."""
    asset = asset.upper()
    if asset.endswith("USDT"):
        return asset
    return f"{asset}{_BINANCE_QUOTE}"


def _is_futures_channel(channel_id: Optional[int] = None, channel_name: str = "") -> bool:
    """Determine if a channel routes to futures or spot.

    trades/active-alerts -> futures
    Everything else -> spot
    """
    if channel_id and channel_id in _FUTURES_CHANNEL_IDS:
        return True
    # Fallback: channel name heuristic
    name_lower = channel_name.lower()
    if "spot" in name_lower:
        return False
    return False


async def execute_trade_signal(trade: TradeSignal, channel_name: str, message_id: str,
                               channel_id: int = 0):
    """Execute a new trade signal on Binance with proper 1R risk management."""
    if not config.BINANCE_API_KEY:
        return

    symbol = _to_binance_symbol(trade.asset)
    use_futures = _is_futures_channel(channel_id, channel_name)

    # Verify symbol exists
    if use_futures:
        sym_info = await binance_client.futures_get_symbol_info(symbol)
    else:
        sym_info = await binance_client.get_symbol_info(symbol)

    if not sym_info:
        logger.warning("EXECUTOR: Symbol %s not found on Binance, skipping", symbol)
        return

    if sym_info.get("status") != "TRADING":
        logger.warning("EXECUTOR: Symbol %s not trading (status=%s)", symbol, sym_info.get("status"))
        return

    # ONE TRADE PER SYMBOL: reject if another trade is already open on this symbol+side
    if await supabase_client.has_open_trade_on_symbol(symbol, trade.direction):
        logger.warning(
            "EXECUTOR: SKIPPED %s %s %s - another trade already open on %s %s",
            symbol, trade.direction, trade.trader, symbol, trade.direction,
        )
        return

    # Setup isolated margin + leverage for futures (WWG rules)
    if use_futures:
        await binance_client.futures_setup_symbol(
            symbol, config.FUTURES_LEVERAGE, config.FUTURES_MARGIN_TYPE,
        )

    # Get current price
    if use_futures:
        current_price = await binance_client.futures_get_price(symbol)
    else:
        current_price = await binance_client.get_price(symbol)

    if not current_price:
        logger.error("EXECUTOR: Cannot get price for %s", symbol)
        return

    # Determine entry prices
    ep1_price = trade.entry_high
    ep2_price = trade.entry_low if trade.entry_low and trade.entry_low != trade.entry_high else None

    if ep1_price is None:
        logger.error("EXECUTOR: No entry price for %s", symbol)
        return

    # Use average entry for risk calculation when we have EP1+EP2
    avg_entry = ep1_price
    if ep2_price:
        avg_entry = (ep1_price + ep2_price) / 2

    # --- RISK CHECK ---
    risk = await risk_manager.assess_trade(
        entry_price=avg_entry,
        stop_loss=trade.stop_loss,
        take_profit=trade.take_profit,
        direction=trade.direction,
        use_futures=use_futures,
    )

    if not risk.allowed:
        logger.warning(
            "EXECUTOR: Trade REJECTED by risk manager - %s %s: %s",
            symbol, trade.direction, risk.reason,
        )
        return

    # Split position between entries
    total_crypto = risk.position_size_crypto
    if ep2_price:
        ep1_crypto = total_crypto * 0.5
        ep2_crypto = total_crypto * 0.5
    else:
        ep1_crypto = total_crypto
        ep2_crypto = 0

    # For futures: LONG=BUY, SHORT=SELL (opening side)
    # For spot: always BUY (LONG only)
    order_side = "SELL" if trade.direction == "SHORT" else "BUY"
    market_type = "futures" if use_futures else "spot"

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
        "risk_usdt": risk.risk_capital,
        "r_multiplier": risk.r_multiplier,
        "rr_ratio": risk.rr_ratio if risk.rr_ratio > 0 else None,
    }

    # Place EP1 limit order
    if use_futures:
        ep1_order = await binance_client.futures_place_limit_order(symbol, order_side, ep1_crypto, ep1_price)
    else:
        ep1_order = await binance_client.place_limit_order(symbol, order_side, ep1_crypto, ep1_price)

    if ep1_order:
        row["ep1_id"] = str(ep1_order["orderId"])
        row["ep1_size_usdt"] = float(ep1_order.get("origQty", ep1_crypto)) * ep1_price
        row["ep1_size_crypto"] = float(ep1_order.get("origQty", ep1_crypto))

        if ep1_order.get("status") == "FILLED":
            row["ep1_status"] = "filled"
            logger.info("EXECUTOR: EP1 immediately filled for %s (%s)", symbol, market_type)
        else:
            logger.info(
                "EXECUTOR: EP1 %s limit order placed for %s @ %s qty=%s (orderId=%s)",
                market_type, symbol, ep1_price, ep1_order.get("origQty"), ep1_order["orderId"],
            )
    else:
        logger.error("EXECUTOR: Failed to place EP1 %s order for %s", market_type, symbol)
        return

    # Place EP2 limit order if we have a second entry
    if ep2_price and ep2_crypto > 0:
        if use_futures:
            ep2_order = await binance_client.futures_place_limit_order(symbol, order_side, ep2_crypto, ep2_price)
        else:
            ep2_order = await binance_client.place_limit_order(symbol, order_side, ep2_crypto, ep2_price)
        if ep2_order:
            row["ep2"] = ep2_price
            row["ep2_id"] = str(ep2_order["orderId"])
            row["ep2_status"] = "waiting"
            row["ep2_size_usdt"] = float(ep2_order.get("origQty", ep2_crypto)) * ep2_price
            row["ep2_size_crypto"] = float(ep2_order.get("origQty", ep2_crypto))
            logger.info(
                "EXECUTOR: EP2 %s limit order placed for %s @ %s qty=%s (orderId=%s)",
                market_type, symbol, ep2_price, ep2_order.get("origQty"), ep2_order["orderId"],
            )

    # Insert trade in Supabase
    result = await supabase_client.insert_trade(row)
    if result:
        trade_id = result.get("id")
        logger.info(
            "EXECUTOR: Trade #%s created [%s] - %s %s %s ep1=%s ep2=%s sl=%s "
            "risk=%.2f USDT (%.1fR x%.1f) R:R=%.2f",
            trade_id, market_type, symbol, trade.direction, trade.trader,
            ep1_price, ep2_price, trade.stop_loss,
            risk.risk_capital, config.RISK_PER_TRADE_PCT, risk.r_multiplier,
            risk.rr_ratio,
        )

        # Start candle monitor for 2x5m SL instead of placing STOP_MARKET
        if trade.sl_type == "candle_2x5m" and trade_id and trade.stop_loss:
            candle_monitor.add_watch(
                trade_id=trade_id,
                symbol=symbol,
                side=trade.direction,
                sl_price=trade.stop_loss,
            )


async def handle_alert(alert: TradeAlert, channel_id: int = 0):
    """Handle an active-alert by managing Binance orders accordingly."""
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

    # Alerts thread is always futures
    use_futures = _is_futures_channel(channel_id)

    for trader in alert.traders:
        existing = await supabase_client.find_open_trade(symbol, trader, side)
        if not existing:
            logger.debug(
                "EXECUTOR: No open trade found for %s %s %s (alert: %s)",
                symbol, side, trader, alert.action,
            )
            continue

        trade_id = existing["id"]

        if alert.event_type == "ep_filled":
            await _handle_ep_filled(existing, symbol, trade_id, use_futures)

        elif alert.new_status in ("closed", "cancelled"):
            await _handle_close(existing, symbol, trade_id, alert, use_futures)

        elif alert.event_type == "sl_to_be":
            await _handle_sl_to_be(existing, symbol, trade_id, use_futures)

        elif alert.event_type == "sl_moved" and alert.new_sl_price:
            await _handle_sl_move(existing, symbol, trade_id, alert.new_sl_price, use_futures)

        elif alert.event_type == "tp_hit":
            # If combined with "stops moved to be", move SL too
            if "stops moved to be" in (alert.action or "").lower():
                await _handle_sl_to_be(existing, symbol, trade_id, use_futures)
            logger.info(
                "EXECUTOR: TP%s hit for trade #%s %s",
                alert.tp_level, trade_id, symbol,
            )

        elif alert.event_type == "entry_updated" and alert.new_entry:
            logger.info(
                "EXECUTOR: Entry updated for trade #%s %s -> %s",
                trade_id, symbol, alert.new_entry,
            )


async def _handle_ep_filled(trade: dict, symbol: str, trade_id: int, use_futures: bool):
    """When an entry fills, place the stop-loss order (or start candle monitor)."""
    sl_price = trade.get("sl")
    sl_id = trade.get("sl_id")

    if not sl_price or sl_id:
        return

    total_qty = 0.0
    for ep in ("ep1", "ep2", "ep3"):
        if trade.get(f"{ep}_status") == "filled" or (ep == "ep1" and not trade.get("ep1_status")):
            qty = trade.get(f"{ep}_size_crypto")
            if qty:
                total_qty += float(qty)

    if total_qty <= 0:
        logger.warning("EXECUTOR: Cannot place SL - no filled quantity for trade #%s", trade_id)
        return

    # Candle-based SL: monitor 5m candles instead of placing STOP_MARKET
    # Check in-memory flag set during execute_trade_signal
    if candle_monitor.is_watched(trade_id):
        candle_monitor.add_watch(
            trade_id=trade_id,
            symbol=symbol,
            side=trade.get("side", "LONG"),
            sl_price=float(sl_price),
        )
        await supabase_client.update_trade(trade_id, {"sl_status": "watching"})
        logger.info(
            "EXECUTOR: Candle SL monitor started for trade #%s %s @ %s (2x 5m)",
            trade_id, symbol, sl_price,
        )
        return

    # Standard SL: place STOP_MARKET order
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
        await supabase_client.update_trade(trade_id, {
            "sl_id": str(sl_order["orderId"]),
            "sl_status": "waiting",
        })
        logger.info(
            "EXECUTOR: SL order placed for trade #%s %s @ %s (orderId=%s)",
            trade_id, symbol, sl_price, sl_order["orderId"],
        )


async def _handle_close(trade: dict, symbol: str, trade_id: int, alert: TradeAlert,
                         use_futures: bool):
    """Close a trade: cancel open orders, close position, track P&L."""
    cancel_fn = binance_client.futures_cancel_order if use_futures else binance_client.cancel_order

    # Cancel any open limit orders
    for ep in ("ep1", "ep2", "ep3"):
        if trade.get(f"{ep}_status") == "waiting" and trade.get(f"{ep}_id"):
            order_id = int(trade[f"{ep}_id"])
            await cancel_fn(symbol, order_id)
            logger.info("EXECUTOR: Cancelled %s order %s for trade #%s", ep, order_id, trade_id)

    # Cancel SL order if exists
    if trade.get("sl_id") and trade.get("sl_status") == "waiting":
        await cancel_fn(symbol, int(trade["sl_id"]))
        logger.info("EXECUTOR: Cancelled SL order for trade #%s", trade_id)

    realized_pnl = None

    if use_futures:
        # Close only THIS trade's quantity, not the entire position
        # (other trades on the same symbol may still be open)
        trade_qty = 0.0
        for ep in ("ep1", "ep2", "ep3"):
            if trade.get(f"{ep}_status") == "filled":
                qty = trade.get(f"{ep}_size_crypto")
                if qty:
                    trade_qty += float(qty)

        if trade_qty > 0:
            # Verify we don't sell more than the actual position
            position = await binance_client.futures_get_position(symbol)
            if position:
                pos_amt = abs(float(position.get("positionAmt", 0)))
                close_qty = min(trade_qty, pos_amt)
                if close_qty > 0:
                    close_side = "BUY" if trade.get("side") == "SHORT" else "SELL"
                    close_result = await binance_client.futures_place_market_order(symbol, close_side, close_qty)
                    if close_result:
                        # Estimate P&L from trade data
                        avg_entry = 0.0
                        total_cost = 0.0
                        for ep in ("ep1", "ep2", "ep3"):
                            if trade.get(f"{ep}_status") == "filled" and trade.get(ep):
                                qty = float(trade.get(f"{ep}_size_crypto", 0))
                                total_cost += float(trade[ep]) * qty
                        current_price = float(position.get("markPrice", 0))
                        if total_cost > 0 and current_price > 0:
                            current_value = close_qty * current_price
                            if trade.get("side") == "LONG":
                                realized_pnl = current_value - total_cost
                            else:
                                realized_pnl = total_cost - current_value
                        else:
                            realized_pnl = float(position.get("unRealizedProfit", 0))
                        risk_manager.record_trade_result(realized_pnl)
                        logger.info(
                            "EXECUTOR: Closed %s of %s for trade #%s (position has %s), P&L: %.2f USDT",
                            close_qty, symbol, trade_id, pos_amt, realized_pnl,
                        )
    else:
        # Close spot position (sell held asset)
        if trade.get("side") == "LONG":
            base_asset = symbol.replace("USDT", "")
            balance = await binance_client.get_balance(base_asset)
            if balance > 0:
                sell_result = await binance_client.place_market_order(symbol, "SELL", quantity=balance)
                if sell_result:
                    sold_qty = float(sell_result.get("executedQty", 0))
                    sell_value = 0
                    for fill in sell_result.get("fills", []):
                        sell_value += float(fill["price"]) * float(fill["qty"])
                    if sell_value == 0:
                        sell_price = await binance_client.get_price(symbol)
                        sell_value = sold_qty * (sell_price or 0)

                    cost_basis = 0
                    for ep in ("ep1", "ep2", "ep3"):
                        if trade.get(f"{ep}_status") == "filled" and trade.get(ep):
                            qty = trade.get(f"{ep}_size_crypto")
                            if qty:
                                cost_basis += float(trade[ep]) * float(qty)

                    if cost_basis > 0:
                        realized_pnl = sell_value - cost_basis
                        risk_manager.record_trade_result(realized_pnl)

                    logger.info(
                        "EXECUTOR: Closed spot position for trade #%s - sold %s %s, P&L: %s USDT",
                        trade_id, sold_qty, symbol,
                        f"{realized_pnl:.2f}" if realized_pnl is not None else "unknown",
                    )

    # Update Supabase
    updates = {"status": alert.new_status or "closed"}
    if alert.close_reason:
        updates["close_reason"] = alert.close_reason
    if realized_pnl is not None:
        updates["realized_pnl"] = round(realized_pnl, 2)
    await supabase_client.update_trade(trade_id, updates)

    logger.info(
        "EXECUTOR: Trade #%s closed - %s %s -> %s/%s pnl=%s",
        trade_id, symbol, trade.get("trader"),
        updates["status"], alert.close_reason,
        f"{realized_pnl:.2f}" if realized_pnl is not None else "n/a",
    )


async def _handle_sl_to_be(trade: dict, symbol: str, trade_id: int, use_futures: bool):
    """Move stop-loss to breakeven (entry price)."""
    cancel_fn = binance_client.futures_cancel_order if use_futures else binance_client.cancel_order

    if trade.get("sl_id") and trade.get("sl_status") == "waiting":
        await cancel_fn(symbol, int(trade["sl_id"]))

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

    if use_futures:
        sl_order = await binance_client.futures_place_stop_loss_order(
            symbol, sl_side, total_qty, float(be_price),
        )
    else:
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


async def _handle_sl_move(trade: dict, symbol: str, trade_id: int,
                           new_price: float, use_futures: bool):
    """Move stop-loss to a specific price."""
    cancel_fn = binance_client.futures_cancel_order if use_futures else binance_client.cancel_order

    if trade.get("sl_id") and trade.get("sl_status") == "waiting":
        await cancel_fn(symbol, int(trade["sl_id"]))

    total_qty = 0.0
    for ep in ("ep1", "ep2", "ep3"):
        if trade.get(f"{ep}_status") == "filled":
            qty = trade.get(f"{ep}_size_crypto")
            if qty:
                total_qty += float(qty)

    if total_qty <= 0:
        return

    sl_side = "SELL" if trade.get("side") == "LONG" else "BUY"

    if use_futures:
        sl_order = await binance_client.futures_place_stop_loss_order(
            symbol, sl_side, total_qty, new_price,
        )
    else:
        sl_order = await binance_client.place_stop_loss_order(
            symbol, sl_side, total_qty, new_price,
        )

    if sl_order:
        await supabase_client.update_trade(trade_id, {
            "sl": new_price,
            "sl_id": str(sl_order["orderId"]),
            "sl_status": "waiting",
        })
        logger.info(
            "EXECUTOR: SL moved to %s for trade #%s %s",
            new_price, trade_id, symbol,
        )
