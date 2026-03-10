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

    # ONE TRADE PER SYMBOL: if another trade is already open, track without placing orders
    if await supabase_client.has_open_trade_on_symbol(symbol, trade.direction):
        # Still insert into Supabase for tracking/analytics (no Binance orders)
        tracking_row = {
            "symbol": symbol,
            "side": trade.direction,
            "status": "open",
            "trader": trade.trader,
            "source_channel": "tracking",
            "discord_message_id": message_id,
            "ep1": trade.entry_high,
            "ep1_status": "waiting",
            "sl": trade.stop_loss,
            "sl_status": "waiting" if trade.stop_loss else None,
        }
        if trade.entry_low and trade.entry_low != trade.entry_high:
            tracking_row["ep2"] = trade.entry_low
            tracking_row["ep2_status"] = "waiting"
        result = await supabase_client.insert_trade(tracking_row)
        if result:
            logger.info(
                "EXECUTOR: TRACKING %s %s %s (trade #%s) - not executing, another %s %s already open",
                symbol, trade.direction, trade.trader, result.get("id"),
                symbol, trade.direction,
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
            "EXECUTOR: Trade REJECTED by risk manager - %s %s: %s (saving as tracking)",
            symbol, trade.direction, risk.reason,
        )
        # Save as tracking so it can be promoted later when risk allows
        tracking_row = {
            "symbol": symbol,
            "side": trade.direction,
            "status": "open",
            "trader": trade.trader,
            "source_channel": "tracking",
            "discord_message_id": message_id,
            "ep1": ep1_price,
            "ep1_status": "waiting",
            "sl": trade.stop_loss,
            "sl_status": "waiting" if trade.stop_loss else None,
        }
        if ep2_price:
            tracking_row["ep2"] = ep2_price
            tracking_row["ep2_status"] = "waiting"
        result = await supabase_client.insert_trade(tracking_row)
        if result:
            logger.info(
                "EXECUTOR: TRACKING %s %s %s (trade #%s) - risk rejected: %s",
                symbol, trade.direction, trade.trader, result.get("id"),
                risk.reason,
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

        # For tracking trades: only handle ep_filled (promotion) and close events
        if existing.get("source_channel") == "tracking":
            if alert.event_type == "ep_filled":
                logger.info(
                    "EXECUTOR: ep_filled alert for TRACKING trade #%s %s %s — attempting promotion",
                    trade_id, symbol, trader,
                )
                await _promote_tracking_on_fill(existing, symbol, trade_id, side, use_futures)
                continue
            elif alert.new_status in ("closed", "cancelled"):
                # Just close in Supabase, no Binance action needed
                updates = {"status": alert.new_status or "closed"}
                if alert.close_reason:
                    updates["close_reason"] = alert.close_reason
                await supabase_client.update_trade(trade_id, updates)
                logger.info(
                    "EXECUTOR: Tracking trade #%s closed — %s/%s",
                    trade_id, alert.new_status, alert.close_reason,
                )
                continue
            else:
                logger.info(
                    "EXECUTOR: Alert %s for tracking trade #%s %s %s — no Binance action",
                    alert.event_type, trade_id, symbol, trader,
                )
                continue

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
            # Take partial profit on Binance
            await _handle_tp_hit(existing, symbol, trade_id, alert.tp_level, use_futures)

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
    if candle_monitor.is_watched(trade_id) or trade.get("sl_type") == "candle_2x5m":
        candle_monitor.add_watch(
            trade_id=trade_id,
            symbol=symbol,
            side=trade.get("side", "LONG"),
            sl_price=float(sl_price),
        )
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

    # Check if a tracking trade on the same symbol can now be promoted
    await _promote_tracking_trade(symbol, trade.get("side", "LONG"), use_futures)


async def _handle_sl_to_be(trade: dict, symbol: str, trade_id: int, use_futures: bool):
    """Move stop-loss to breakeven (entry price). Also cancel unfilled EP orders."""
    cancel_fn = binance_client.futures_cancel_order if use_futures else binance_client.cancel_order

    # Cancel existing SL order (if standard SL, not candle-based)
    if trade.get("sl_id") and trade.get("sl_status") == "waiting":
        await cancel_fn(symbol, int(trade["sl_id"]))

    # Cancel any unfilled EP limit orders (price moved up, don't want them filling later)
    for ep in ("ep1", "ep2", "ep3"):
        if trade.get(f"{ep}_status") == "waiting" and trade.get(f"{ep}_id"):
            order_id = int(trade[f"{ep}_id"])
            await cancel_fn(symbol, order_id)
            # Use None instead of "cancelled" (Supabase constraint)
            await supabase_client.update_trade(trade_id, {f"{ep}_status": None, f"{ep}_id": None})
            logger.info("EXECUTOR: Cancelled %s order %s (SL→BE) for trade #%s", ep, order_id, trade_id)

    be_price = trade.get("ep1")
    if not be_price:
        return

    # For candle-based SL: update the candle monitor price instead of placing STOP_MARKET
    if candle_monitor.is_watched(trade_id) or trade.get("sl_type") == "candle_2x5m":
        candle_monitor.add_watch(
            trade_id=trade_id,
            symbol=symbol,
            side=trade.get("side", "LONG"),
            sl_price=float(be_price),
        )
        await supabase_client.update_trade(trade_id, {"sl": float(be_price)})
        logger.info(
            "EXECUTOR: Candle SL moved to BE for trade #%s %s @ %s",
            trade_id, symbol, be_price,
        )
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
            "sl": float(be_price),
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


async def _handle_tp_hit(trade: dict, symbol: str, trade_id: int,
                          tp_level: int, use_futures: bool):
    """Take partial profit when a TP level is hit.

    Strategy: close 50% of remaining position on each TP hit.
    This ensures we lock in profits progressively while letting the rest ride.
    """
    # Calculate remaining position quantity
    total_qty = 0.0
    for ep in ("ep1", "ep2", "ep3"):
        if trade.get(f"{ep}_status") == "filled":
            qty = trade.get(f"{ep}_size_crypto")
            if qty:
                total_qty += float(qty)

    if total_qty <= 0:
        logger.warning("EXECUTOR: TP%s hit but no filled qty for trade #%s", tp_level, trade_id)
        return

    # Check actual position on Binance
    if use_futures:
        position = await binance_client.futures_get_position(symbol)
        if not position or float(position.get("positionAmt", 0)) == 0:
            logger.warning("EXECUTOR: TP%s hit but no position on Binance for trade #%s", tp_level, trade_id)
            return
        pos_qty = abs(float(position["positionAmt"]))
    else:
        base_asset = symbol.replace("USDT", "")
        pos_qty = await binance_client.get_balance(base_asset)
        if pos_qty <= 0:
            logger.warning("EXECUTOR: TP%s hit but no balance for trade #%s", tp_level, trade_id)
            return

    # Close 50% of current position
    close_qty = pos_qty * 0.5

    # Get symbol info for precision
    if use_futures:
        sym_info = await binance_client.futures_get_symbol_info(symbol)
    else:
        sym_info = await binance_client.get_symbol_info(symbol)

    if sym_info:
        # Round to symbol's step size
        step_size = None
        for f in sym_info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                step_size = float(f["stepSize"])
                break
        if step_size and step_size > 0:
            close_qty = int(close_qty / step_size) * step_size

    if close_qty <= 0:
        logger.info("EXECUTOR: TP%s close qty too small for trade #%s", tp_level, trade_id)
        return

    close_side = "SELL" if trade.get("side") == "LONG" else "BUY"

    if use_futures:
        result = await binance_client.futures_place_market_order(symbol, close_side, close_qty)
    else:
        result = await binance_client.place_market_order(symbol, close_side, quantity=close_qty)

    if result:
        remaining = pos_qty - close_qty
        logger.info(
            "EXECUTOR: TP%s TAKEN for trade #%s %s — closed %s, remaining %s",
            tp_level, trade_id, symbol, close_qty, remaining,
        )

        # If position fully closed, close the trade
        if remaining <= 0:
            await supabase_client.update_trade(trade_id, {
                "status": "closed",
                "close_reason": "profit",
            })
            # Stop candle monitor if active
            candle_monitor.remove_watch(trade_id)
        else:
            # Update SL order quantity to match remaining position
            # (candle monitor handles this automatically since it reads position from Binance)
            if not candle_monitor.is_watched(trade_id) and trade.get("sl_id"):
                # For standard SL: cancel and re-place with new qty
                cancel_fn = binance_client.futures_cancel_order if use_futures else binance_client.cancel_order
                await cancel_fn(symbol, int(trade["sl_id"]))

                sl_price = trade.get("sl")
                if sl_price:
                    sl_side = "SELL" if trade.get("side") == "LONG" else "BUY"
                    if use_futures:
                        sl_order = await binance_client.futures_place_stop_loss_order(
                            symbol, sl_side, remaining, float(sl_price),
                        )
                    else:
                        sl_order = await binance_client.place_stop_loss_order(
                            symbol, sl_side, remaining, float(sl_price),
                        )
                    if sl_order:
                        await supabase_client.update_trade(trade_id, {
                            "sl_id": str(sl_order["orderId"]),
                        })
                        logger.info(
                            "EXECUTOR: SL re-placed for remaining %s of trade #%s",
                            remaining, trade_id,
                        )
    else:
        logger.error("EXECUTOR: Failed to take TP%s for trade #%s %s", tp_level, trade_id, symbol)


async def _promote_tracking_trade(symbol: str, side: str, use_futures: bool):
    """Check if a tracking trade exists on this symbol+side and promote it to active.

    Called when an active trade is closed, freeing the symbol for a new trade.
    Finds the oldest tracking trade, validates it's still viable, places orders on Binance,
    and updates Supabase to convert it from tracking to active.
    """
    # Find tracking trades on this symbol+side
    session = await supabase_client._get_session()
    url = (
        f"{supabase_client._BASE_URL}/trades"
        f"?symbol=eq.{symbol}&side=eq.{side}&status=eq.open"
        f"&source_channel=eq.tracking&order=created_at.asc&limit=1"
    )
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return
            rows = await resp.json()
    except Exception:
        return

    if not rows:
        return

    tracking = rows[0]
    trade_id = tracking["id"]
    trader = tracking.get("trader", "?")

    logger.info(
        "EXECUTOR: Found tracking trade #%s %s %s %s — attempting to promote",
        trade_id, symbol, side, trader,
    )

    # Check if the entry prices are still reachable
    if use_futures:
        current_price = await binance_client.futures_get_price(symbol)
    else:
        current_price = await binance_client.get_price(symbol)

    if not current_price:
        logger.warning("EXECUTOR: Cannot get price for %s, skipping promotion", symbol)
        return

    ep1_price = tracking.get("ep1")
    ep2_price = tracking.get("ep2")
    sl_price = tracking.get("sl")

    if not ep1_price:
        logger.warning("EXECUTOR: Tracking trade #%s has no EP1, skipping", trade_id)
        return

    # Check if entry is still viable (price hasn't blown past entries)
    if side == "LONG":
        # For LONG: current price should be near or above entries (limit buy below)
        # If price is way below SL, the trade setup is invalidated
        if sl_price and current_price < float(sl_price) * 0.98:
            logger.info(
                "EXECUTOR: Tracking trade #%s invalidated — price %.2f below SL %.2f",
                trade_id, current_price, float(sl_price),
            )
            await supabase_client.update_trade(trade_id, {"status": "closed", "close_reason": "invalidated"})
            return
    else:  # SHORT
        if sl_price and current_price > float(sl_price) * 1.02:
            logger.info(
                "EXECUTOR: Tracking trade #%s invalidated — price %.2f above SL %.2f",
                trade_id, current_price, float(sl_price),
            )
            await supabase_client.update_trade(trade_id, {"status": "closed", "close_reason": "invalidated"})
            return

    # Setup symbol on Binance
    if use_futures:
        await binance_client.futures_setup_symbol(symbol, config.FUTURES_LEVERAGE, config.FUTURES_MARGIN_TYPE)

    # Risk assessment
    avg_entry = float(ep1_price)
    if ep2_price:
        avg_entry = (float(ep1_price) + float(ep2_price)) / 2

    risk = await risk_manager.assess_trade(
        entry_price=avg_entry,
        stop_loss=float(sl_price) if sl_price else None,
        take_profit=None,
        direction=side,
        use_futures=use_futures,
    )

    if not risk.allowed:
        logger.warning(
            "EXECUTOR: Tracking trade #%s promotion REJECTED by risk manager: %s",
            trade_id, risk.reason,
        )
        return

    # Place orders
    order_side = "SELL" if side == "SHORT" else "BUY"
    total_crypto = risk.position_size_crypto

    if ep2_price:
        ep1_crypto = total_crypto * 0.5
        ep2_crypto = total_crypto * 0.5
    else:
        ep1_crypto = total_crypto
        ep2_crypto = 0

    updates = {
        "source_channel": "promoted",
        "risk_usdt": risk.risk_capital,
        "r_multiplier": risk.r_multiplier,
        "rr_ratio": risk.rr_ratio if risk.rr_ratio > 0 else None,
    }

    # Place EP1
    if use_futures:
        ep1_order = await binance_client.futures_place_limit_order(symbol, order_side, ep1_crypto, float(ep1_price))
    else:
        ep1_order = await binance_client.place_limit_order(symbol, order_side, ep1_crypto, float(ep1_price))

    if ep1_order:
        updates["ep1_id"] = str(ep1_order["orderId"])
        updates["ep1_status"] = "filled" if ep1_order.get("status") == "FILLED" else "waiting"
        updates["ep1_size_crypto"] = float(ep1_order.get("origQty", ep1_crypto))
        updates["ep1_size_usdt"] = float(ep1_order.get("origQty", ep1_crypto)) * float(ep1_price)
        logger.info(
            "EXECUTOR: Promoted trade #%s EP1 order placed %s @ %s qty=%s (orderId=%s)",
            trade_id, symbol, ep1_price, ep1_order.get("origQty"), ep1_order["orderId"],
        )
    else:
        logger.error("EXECUTOR: Failed to place EP1 for promoted trade #%s", trade_id)
        return

    # Place EP2 if exists
    if ep2_price and ep2_crypto > 0:
        if use_futures:
            ep2_order = await binance_client.futures_place_limit_order(symbol, order_side, ep2_crypto, float(ep2_price))
        else:
            ep2_order = await binance_client.place_limit_order(symbol, order_side, ep2_crypto, float(ep2_price))
        if ep2_order:
            updates["ep2_id"] = str(ep2_order["orderId"])
            updates["ep2_status"] = "filled" if ep2_order.get("status") == "FILLED" else "waiting"
            updates["ep2_size_crypto"] = float(ep2_order.get("origQty", ep2_crypto))
            updates["ep2_size_usdt"] = float(ep2_order.get("origQty", ep2_crypto)) * float(ep2_price)
            logger.info(
                "EXECUTOR: Promoted trade #%s EP2 order placed %s @ %s (orderId=%s)",
                trade_id, symbol, ep2_price, ep2_order["orderId"],
            )

    await supabase_client.update_trade(trade_id, updates)
    logger.info(
        "EXECUTOR: Trade #%s PROMOTED from tracking to active — %s %s %s risk=%.2f USDT",
        trade_id, symbol, side, trader, risk.risk_capital,
    )


async def _promote_tracking_on_fill(trade: dict, symbol: str, trade_id: int,
                                      side: str, use_futures: bool):
    """Promote a tracking trade when its 'limit order filled' alert arrives.

    If risk budget is exceeded, first demote the furthest unfilled active trade
    to make room, then promote this one.
    """
    ep1_price = trade.get("ep1")
    sl_price = trade.get("sl")
    if not ep1_price or not sl_price:
        logger.warning("EXECUTOR: Cannot promote #%s — missing EP1 or SL", trade_id)
        return

    # Check if risk allows promotion; if not, try to free up by demoting furthest trade
    avg_entry = float(ep1_price)
    ep2_price = trade.get("ep2")
    if ep2_price:
        avg_entry = (float(ep1_price) + float(ep2_price)) / 2

    risk = await risk_manager.assess_trade(
        entry_price=avg_entry,
        stop_loss=float(sl_price),
        take_profit=None,
        direction=side,
        use_futures=use_futures,
    )

    if not risk.allowed:
        logger.info(
            "EXECUTOR: Promotion of #%s blocked by risk (%s) — trying to demote furthest trade",
            trade_id, risk.reason,
        )
        demoted = await _demote_furthest_trade(symbol, side, trade_id, use_futures)
        if not demoted:
            logger.warning(
                "EXECUTOR: Cannot promote #%s — no trade to demote and risk exceeded",
                trade_id,
            )
            return
        # Re-check risk after demotion
        risk = await risk_manager.assess_trade(
            entry_price=avg_entry,
            stop_loss=float(sl_price),
            take_profit=None,
            direction=side,
            use_futures=use_futures,
        )
        if not risk.allowed:
            logger.warning(
                "EXECUTOR: Still cannot promote #%s after demotion — %s",
                trade_id, risk.reason,
            )
            return

    # Setup symbol
    if use_futures:
        await binance_client.futures_setup_symbol(symbol, config.FUTURES_LEVERAGE, config.FUTURES_MARGIN_TYPE)

    # Place orders
    order_side = "SELL" if side == "SHORT" else "BUY"
    total_crypto = risk.position_size_crypto

    if ep2_price:
        ep1_crypto = total_crypto * 0.5
        ep2_crypto = total_crypto * 0.5
    else:
        ep1_crypto = total_crypto
        ep2_crypto = 0

    updates = {
        "source_channel": "promoted",
        "risk_usdt": risk.risk_capital,
        "r_multiplier": risk.r_multiplier,
        "rr_ratio": risk.rr_ratio if risk.rr_ratio > 0 else None,
    }

    if use_futures:
        ep1_order = await binance_client.futures_place_limit_order(symbol, order_side, ep1_crypto, float(ep1_price))
    else:
        ep1_order = await binance_client.place_limit_order(symbol, order_side, ep1_crypto, float(ep1_price))

    if ep1_order:
        updates["ep1_id"] = str(ep1_order["orderId"])
        updates["ep1_status"] = "filled" if ep1_order.get("status") == "FILLED" else "waiting"
        updates["ep1_size_crypto"] = float(ep1_order.get("origQty", ep1_crypto))
        updates["ep1_size_usdt"] = float(ep1_order.get("origQty", ep1_crypto)) * float(ep1_price)
    else:
        logger.error("EXECUTOR: Failed EP1 for promotion of #%s", trade_id)
        return

    if ep2_price and ep2_crypto > 0:
        if use_futures:
            ep2_order = await binance_client.futures_place_limit_order(symbol, order_side, ep2_crypto, float(ep2_price))
        else:
            ep2_order = await binance_client.place_limit_order(symbol, order_side, ep2_crypto, float(ep2_price))
        if ep2_order:
            updates["ep2_id"] = str(ep2_order["orderId"])
            updates["ep2_status"] = "filled" if ep2_order.get("status") == "FILLED" else "waiting"
            updates["ep2_size_crypto"] = float(ep2_order.get("origQty", ep2_crypto))
            updates["ep2_size_usdt"] = float(ep2_order.get("origQty", ep2_crypto)) * float(ep2_price)

    await supabase_client.update_trade(trade_id, updates)
    logger.info(
        "EXECUTOR: Trade #%s PROMOTED on fill alert — %s %s %s risk=%.2f USDT",
        trade_id, symbol, side, trade.get("trader", "?"), risk.risk_capital,
    )


async def _demote_furthest_trade(exclude_symbol: str, exclude_side: str,
                                   exclude_id: int, use_futures: bool) -> bool:
    """Demote the active trade whose entry is furthest from current price.

    Cancels its Binance orders and sets source_channel=tracking.
    Skips the trade being promoted (exclude_id) and filled positions.
    Returns True if a trade was successfully demoted.
    """
    trades = await supabase_client.get_open_trades()
    # Filter to active (non-tracking) trades with unfilled EPs only
    candidates = []
    for t in trades:
        if t["id"] == exclude_id:
            continue
        if t.get("source_channel") == "tracking":
            continue
        # Skip trades that have filled positions (we don't want to close positions)
        has_filled = any(
            t.get(f"ep{i}_status") == "filled" for i in range(1, 4)
        )
        if has_filled:
            continue
        # Must have entry price
        if not t.get("ep1"):
            continue
        candidates.append(t)

    if not candidates:
        logger.info("EXECUTOR: No candidate trades to demote")
        return False

    # Get current prices and find furthest
    furthest = None
    max_distance = 0

    for t in candidates:
        symbol = t["symbol"]
        ep1 = float(t["ep1"])
        if use_futures:
            price = await binance_client.futures_get_price(symbol)
        else:
            price = await binance_client.get_price(symbol)
        if not price:
            continue
        distance_pct = abs(price - ep1) / price * 100
        if distance_pct > max_distance:
            max_distance = distance_pct
            furthest = t

    if not furthest:
        return False

    fid = furthest["id"]
    fsym = furthest["symbol"]
    logger.info(
        "EXECUTOR: Demoting trade #%s %s %s (%.1f%% from price) to make room",
        fid, fsym, furthest.get("trader", "?"), max_distance,
    )

    # Cancel Binance orders
    cancel_fn = binance_client.futures_cancel_order if use_futures else binance_client.cancel_order
    for ep in ("ep1", "ep2", "ep3"):
        oid = furthest.get(f"{ep}_id")
        if oid and furthest.get(f"{ep}_status") == "waiting":
            await cancel_fn(fsym, int(oid))
            logger.info("EXECUTOR: Cancelled %s order %s for demoted #%s", ep, oid, fid)

    # Update Supabase
    await supabase_client.update_trade(fid, {
        "source_channel": "tracking",
        "risk_usdt": None,
        "r_multiplier": None,
        "rr_ratio": None,
        "ep1_id": None,
        "ep2_id": None,
        "ep3_id": None,
    })

    logger.info("EXECUTOR: Trade #%s demoted to tracking", fid)
    return True


async def rebalance_active_trades(use_futures: bool = True) -> list[str]:
    """Rebalance active vs tracking trades based on proximity to current price.

    For each symbol+side: if a tracking trade's entry is closer to price than
    an active trade's entry, swap them (demote active, promote tracking).

    Also: if we have unused risk budget and a tracking trade is close to price
    (within PROMOTE_THRESHOLD_PCT), promote it directly.

    Returns a list of actions taken (for reporting).
    """
    SWAP_ADVANTAGE_PCT = 1.0  # tracking must be at least 1% closer to justify swap
    PROMOTE_THRESHOLD_PCT = 3.0  # auto-promote tracking trades within 3% of price

    actions = []
    all_trades = await supabase_client.get_open_trades()

    if not all_trades:
        return actions

    # Separate active (has orders on Binance) vs tracking
    active_trades = []
    tracking_trades = []
    for t in all_trades:
        if t.get("source_channel") == "tracking":
            tracking_trades.append(t)
        else:
            # Only consider unfilled active trades (no position yet)
            has_filled = any(t.get(f"ep{i}_status") == "filled" for i in range(1, 4))
            if has_filled:
                continue  # Don't touch trades with open positions
            active_trades.append(t)

    if not tracking_trades:
        return actions

    # Get current prices for all relevant symbols
    symbols = set()
    for t in active_trades + tracking_trades:
        symbols.add(t["symbol"])

    prices = {}
    for sym in symbols:
        if use_futures:
            p = await binance_client.futures_get_price(sym)
        else:
            p = await binance_client.get_price(sym)
        if p:
            prices[sym] = p

    # Calculate distance for each trade
    def get_distance(trade):
        sym = trade["symbol"]
        if sym not in prices:
            return 999.0
        price = prices[sym]
        ep1 = float(trade.get("ep1", 0))
        if ep1 <= 0:
            return 999.0
        return abs(price - ep1) / price * 100

    # === PHASE 1: Swap — for each symbol+side, check if tracking beats active ===
    for track in tracking_trades[:]:  # copy since we may modify
        sym = track["symbol"]
        side = track.get("side", "LONG")
        track_dist = get_distance(track)

        if track_dist >= 999:
            continue

        # Find active trades on same symbol+side
        matching_active = [
            a for a in active_trades
            if a["symbol"] == sym and a.get("side") == side
        ]

        for act in matching_active:
            act_dist = get_distance(act)
            advantage = act_dist - track_dist

            if advantage >= SWAP_ADVANTAGE_PCT:
                logger.info(
                    "REBALANCE: Swapping #%s %s %s (%.1f%% away) with tracking #%s %s (%.1f%% away) — %.1f%% closer",
                    act["id"], sym, act.get("trader"), act_dist,
                    track["id"], track.get("trader"), track_dist,
                    advantage,
                )

                # Demote the active trade
                cancel_fn = binance_client.futures_cancel_order if use_futures else binance_client.cancel_order
                for ep in ("ep1", "ep2", "ep3"):
                    oid = act.get(f"{ep}_id")
                    if oid and act.get(f"{ep}_status") == "waiting":
                        await cancel_fn(sym, int(oid))

                await supabase_client.update_trade(act["id"], {
                    "source_channel": "tracking",
                    "risk_usdt": None, "r_multiplier": None, "rr_ratio": None,
                    "ep1_id": None, "ep2_id": None, "ep3_id": None,
                })

                # Promote the tracking trade
                promoted = await _promote_single_trade(track, sym, side, use_futures)
                if promoted:
                    actions.append(
                        f"SWAP: demoted #{act['id']} {act.get('trader')} ({act_dist:.1f}%), "
                        f"promoted #{track['id']} {track.get('trader')} ({track_dist:.1f}%)"
                    )
                    # Update our local lists
                    active_trades.remove(act)
                    tracking_trades.remove(track)
                else:
                    actions.append(
                        f"SWAP PARTIAL: demoted #{act['id']} but failed to promote #{track['id']}"
                    )
                    active_trades.remove(act)
                break  # one swap per tracking trade

    # === PHASE 2: Promote — if risk budget allows, promote close tracking trades ===
    for track in tracking_trades[:]:
        track_dist = get_distance(track)
        if track_dist > PROMOTE_THRESHOLD_PCT:
            continue

        sym = track["symbol"]
        side = track.get("side", "LONG")

        # Check if there's already an active trade on same symbol+side
        has_active = any(
            a["symbol"] == sym and a.get("side") == side
            for a in active_trades
        )
        if has_active:
            continue  # Already handled in phase 1 or already covered

        # Try to promote within risk budget
        promoted = await _promote_single_trade(track, sym, side, use_futures)
        if promoted:
            actions.append(
                f"PROMOTE: #{track['id']} {sym} {side} {track.get('trader')} ({track_dist:.1f}% from price)"
            )
            tracking_trades.remove(track)

    if not actions:
        logger.info("REBALANCE: No changes needed")

    return actions


async def _promote_single_trade(trade: dict, symbol: str, side: str,
                                  use_futures: bool) -> bool:
    """Promote a single tracking trade to active. Returns True on success."""
    ep1_price = trade.get("ep1")
    ep2_price = trade.get("ep2")
    sl_price = trade.get("sl")
    trade_id = trade["id"]

    if not ep1_price or not sl_price:
        return False

    avg_entry = float(ep1_price)
    if ep2_price:
        avg_entry = (float(ep1_price) + float(ep2_price)) / 2

    risk = await risk_manager.assess_trade(
        entry_price=avg_entry,
        stop_loss=float(sl_price),
        take_profit=None,
        direction=side,
        use_futures=use_futures,
    )

    if not risk.allowed:
        logger.info("REBALANCE: Cannot promote #%s — %s", trade_id, risk.reason)
        return False

    if use_futures:
        await binance_client.futures_setup_symbol(symbol, config.FUTURES_LEVERAGE, config.FUTURES_MARGIN_TYPE)

    order_side = "SELL" if side == "SHORT" else "BUY"
    total_crypto = risk.position_size_crypto

    if ep2_price:
        ep1_crypto = total_crypto * 0.5
        ep2_crypto = total_crypto * 0.5
    else:
        ep1_crypto = total_crypto
        ep2_crypto = 0

    updates = {
        "source_channel": "promoted",
        "risk_usdt": risk.risk_capital,
        "r_multiplier": risk.r_multiplier,
        "rr_ratio": risk.rr_ratio if risk.rr_ratio > 0 else None,
    }

    if use_futures:
        ep1_order = await binance_client.futures_place_limit_order(symbol, order_side, ep1_crypto, float(ep1_price))
    else:
        ep1_order = await binance_client.place_limit_order(symbol, order_side, ep1_crypto, float(ep1_price))

    if not ep1_order:
        logger.error("REBALANCE: Failed EP1 for #%s", trade_id)
        return False

    updates["ep1_id"] = str(ep1_order["orderId"])
    updates["ep1_status"] = "filled" if ep1_order.get("status") == "FILLED" else "waiting"
    updates["ep1_size_crypto"] = float(ep1_order.get("origQty", ep1_crypto))
    updates["ep1_size_usdt"] = float(ep1_order.get("origQty", ep1_crypto)) * float(ep1_price)

    if ep2_price and ep2_crypto > 0:
        if use_futures:
            ep2_order = await binance_client.futures_place_limit_order(symbol, order_side, ep2_crypto, float(ep2_price))
        else:
            ep2_order = await binance_client.place_limit_order(symbol, order_side, ep2_crypto, float(ep2_price))
        if ep2_order:
            updates["ep2_id"] = str(ep2_order["orderId"])
            updates["ep2_status"] = "filled" if ep2_order.get("status") == "FILLED" else "waiting"
            updates["ep2_size_crypto"] = float(ep2_order.get("origQty", ep2_crypto))
            updates["ep2_size_usdt"] = float(ep2_order.get("origQty", ep2_crypto)) * float(ep2_price)

    await supabase_client.update_trade(trade_id, updates)
    logger.info(
        "REBALANCE: Trade #%s promoted — %s %s %s risk=%.2f USDT",
        trade_id, symbol, side, trade.get("trader", "?"), risk.risk_capital,
    )
    return True
