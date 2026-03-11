"""CryptoBot check-up script — run periodically to verify consistency.

This script is the safety net: it catches anything the real-time listener missed.
It parses recent Discord alerts, reconciles Supabase with Binance, and auto-corrects.
"""
import asyncio
import re
import os
import aiohttp
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone

load_dotenv()

import config

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = "1265052061976891454"
CHANNELS = {
    "trades": str(config.TRADES_THREAD_ID),
    "alerts": str(config.ALERTS_THREAD_ID),
    "futures": str(config.ACTIVE_FUTURES_ID),
    "spot": str(config.ACTIVE_SPOT_ID),
}
SUPA_URL = os.getenv("SUPABASE_URL")
SUPA_KEY = os.getenv("SUPABASE_ANON_KEY")


async def main():
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=35)
    headers = {"Authorization": TOKEN}
    supa_h = {
        "apikey": SUPA_KEY,
        "Authorization": f"Bearer {SUPA_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    import binance_client
    import parser as trade_parser

    async with aiohttp.ClientSession() as s:
        # ── 1. Role map ──
        async with s.get(
            f"https://discord.com/api/v9/guilds/{GUILD_ID}/roles", headers=headers
        ) as r:
            roles = await r.json()
        role_map = {}
        for role in roles:
            if role["name"].startswith("WWG: "):
                trader = role["name"].removeprefix("WWG: ").lstrip("-").strip()
                role_map[role["id"]] = trader

        def resolve(content):
            def repl(m):
                rid = m.group(1)
                return f"@WWG: {role_map[rid]}" if rid in role_map else m.group(0)
            return re.sub(r"<@&(\d+)>", repl, content or "")

        # ── 2. Fetch Discord messages ──
        discord_data = {}
        for name, ch_id in CHANNELS.items():
            url = f"https://discord.com/api/v9/channels/{ch_id}/messages?limit=20"
            async with s.get(url, headers=headers) as r:
                if r.status == 200:
                    msgs = await r.json()
                    recent = []
                    for m in msgs:
                        try:
                            t = datetime.fromisoformat(m["timestamp"])
                            if t > cutoff:
                                recent.append(m)
                        except Exception:
                            pass
                    discord_data[name] = {"all": msgs, "recent": recent}
                    print(f"Discord {name}: {len(recent)} new / {len(msgs)} total")

        # ── 3. Supabase open trades ──
        async with s.get(
            f"{SUPA_URL}/rest/v1/trades?status=eq.open&order=created_at.desc",
            headers=supa_h,
        ) as r:
            trades = await r.json()
        print(f"Supabase: {len(trades)} open trades")

        for t in trades:
            print(
                f"  #{t['id']} {t['symbol']} {t['side']} trader={t['trader']} "
                f"ep1={t.get('ep1')}({t.get('ep1_status')}) "
                f"ep2={t.get('ep2')}({t.get('ep2_status')}) "
                f"sl={t.get('sl')} sl_id={t.get('sl_id')}"
            )

        corrections = []

        # Helper to update Supabase
        async def supa_patch(trade_id, updates):
            async with s.patch(
                f"{SUPA_URL}/rest/v1/trades?id=eq.{trade_id}",
                headers=supa_h,
                json=updates,
            ) as r2:
                return await r2.json()

        # Helper to find matching open trade
        def find_trade(symbol, trader, side):
            for t in trades:
                if (t["symbol"] == symbol
                    and t.get("trader") == trader
                    and t.get("side") == side
                    and t.get("status") == "open"):
                    return t
            return None

        # ── 4. PROCESS RECENT ALERTS (the critical missing piece!) ──
        for msg in discord_data.get("alerts", {}).get("recent", []):
            # Pass RAW content (with <@&id> mentions) to the parser —
            # the parser expects role mention format, not resolved names
            content = msg.get("content", "")
            if not content:
                continue

            parsed_alerts = trade_parser.parse_alert_message(content)
            for alert in parsed_alerts:
                if not alert.traders or not alert.asset:
                    continue

                symbol = alert.asset.upper()
                if not symbol.endswith("USDT"):
                    symbol = f"{symbol}USDT"
                side = alert.direction
                if side == "SPOT":
                    side = "LONG"

                for trader in alert.traders:
                    existing = find_trade(symbol, trader, side)
                    if not existing:
                        continue

                    trade_id = existing["id"]
                    use_futures = "spot" not in (existing.get("source_channel") or "").lower()

                    # ── SL moved to BE ──
                    if alert.event_type == "sl_to_be":
                        be_price = existing.get("ep1")
                        if not be_price:
                            continue

                        # Cancel old SL on Binance (algo order for futures)
                        if existing.get("sl_id") and existing.get("sl_status") == "waiting":
                            try:
                                if use_futures:
                                    await binance_client.futures_cancel_algo_order(int(existing["sl_id"]))
                                else:
                                    await binance_client.cancel_order(symbol, int(existing["sl_id"]))
                            except Exception as e:
                                print(f"  Could not cancel old SL: {e}")

                        # Cancel unfilled EP orders (price moved past entry, don't want them filling later)
                        for ep in ("ep1", "ep2", "ep3"):
                            oid = existing.get(f"{ep}_id")
                            if oid and existing.get(f"{ep}_status") == "waiting":
                                try:
                                    if use_futures:
                                        await binance_client.futures_cancel_order(symbol, int(oid))
                                    else:
                                        await binance_client.cancel_order(symbol, int(oid))
                                    await supa_patch(trade_id, {f"{ep}_status": None, f"{ep}_id": None})
                                    corrections.append(
                                        f"Trade #{trade_id} {symbol} {ep} cancelled (SL→BE)"
                                    )
                                except Exception as e:
                                    print(f"  Could not cancel {ep}: {e}")

                        # Use actual position size from Binance (more accurate)
                        total_qty = 0.0
                        if use_futures:
                            pos = await binance_client.futures_get_position(symbol)
                            if pos:
                                total_qty = abs(float(pos.get("positionAmt", 0)))
                        if total_qty <= 0:
                            for ep in ("ep1", "ep2", "ep3"):
                                if existing.get(f"{ep}_status") == "filled":
                                    qty = existing.get(f"{ep}_size_crypto")
                                    if qty:
                                        total_qty += float(qty)

                        if total_qty > 0:
                            sl_side = "SELL" if side == "LONG" else "BUY"
                            if use_futures:
                                sl_order = await binance_client.futures_place_stop_loss_order(
                                    symbol, sl_side, total_qty, float(be_price),
                                )
                            else:
                                sl_order = await binance_client.place_stop_loss_order(
                                    symbol, sl_side, total_qty, float(be_price),
                                )

                            if sl_order:
                                await supa_patch(trade_id, {
                                    "sl": float(be_price),
                                    "sl_id": str(sl_order["orderId"]),
                                    "sl_status": "waiting",
                                })
                                corrections.append(
                                    f"Trade #{trade_id} {symbol} SL moved to BE @ {be_price}"
                                )
                        else:
                            # No position, just update Supabase
                            await supa_patch(trade_id, {
                                "sl": float(be_price),
                                "sl_status": "closed",
                            })
                            corrections.append(
                                f"Trade #{trade_id} {symbol} SL updated to BE (no position)"
                            )

                    # ── SL moved to specific price ──
                    elif alert.event_type == "sl_moved" and alert.new_sl_price:
                        if existing.get("sl_id") and existing.get("sl_status") == "waiting":
                            try:
                                if use_futures:
                                    await binance_client.futures_cancel_algo_order(int(existing["sl_id"]))
                                else:
                                    await binance_client.cancel_order(symbol, int(existing["sl_id"]))
                            except Exception as e:
                                print(f"  Could not cancel old SL: {e}")

                        total_qty = 0.0
                        if use_futures:
                            pos = await binance_client.futures_get_position(symbol)
                            if pos:
                                total_qty = abs(float(pos.get("positionAmt", 0)))
                        if total_qty <= 0:
                            for ep in ("ep1", "ep2", "ep3"):
                                if existing.get(f"{ep}_status") == "filled":
                                    qty = existing.get(f"{ep}_size_crypto")
                                    if qty:
                                        total_qty += float(qty)

                        if total_qty > 0:
                            sl_side = "SELL" if side == "LONG" else "BUY"
                            if use_futures:
                                sl_order = await binance_client.futures_place_stop_loss_order(
                                    symbol, sl_side, total_qty, alert.new_sl_price,
                                )
                            else:
                                sl_order = await binance_client.place_stop_loss_order(
                                    symbol, sl_side, total_qty, alert.new_sl_price,
                                )

                            if sl_order:
                                await supa_patch(trade_id, {
                                    "sl": alert.new_sl_price,
                                    "sl_id": str(sl_order["orderId"]),
                                    "sl_status": "waiting",
                                })
                                corrections.append(
                                    f"Trade #{trade_id} {symbol} SL moved to {alert.new_sl_price}"
                                )

                    # ── Trade closed (stopped out, profit, cancelled, etc.) ──
                    elif alert.new_status in ("closed", "cancelled"):
                        updates = {"status": alert.new_status}
                        if alert.close_reason:
                            updates["close_reason"] = alert.close_reason

                        # Cancel all open orders on Binance
                        for ep in ("ep1", "ep2", "ep3"):
                            oid = existing.get(f"{ep}_id")
                            if oid and existing.get(f"{ep}_status") == "waiting":
                                try:
                                    if use_futures:
                                        await binance_client.futures_cancel_order(symbol, int(oid))
                                    else:
                                        await binance_client.cancel_order(symbol, int(oid))
                                    updates[f"{ep}_status"] = "cancelled"
                                except Exception:
                                    pass

                        # Cancel SL (algo order for futures)
                        if existing.get("sl_id") and existing.get("sl_status") == "waiting":
                            try:
                                if use_futures:
                                    await binance_client.futures_cancel_algo_order(int(existing["sl_id"]))
                                else:
                                    await binance_client.cancel_order(symbol, int(existing["sl_id"]))
                                updates["sl_status"] = "cancelled"
                            except Exception:
                                pass

                        # Close position on Binance if any
                        pos = await binance_client.futures_get_position(symbol) if use_futures else None
                        if pos and float(pos.get("positionAmt", 0)) != 0:
                            amt = abs(float(pos["positionAmt"]))
                            close_side = "SELL" if float(pos["positionAmt"]) > 0 else "BUY"
                            if use_futures:
                                await binance_client.futures_place_market_order(symbol, close_side, amt, reduce_only=True)
                            corrections.append(
                                f"Trade #{trade_id} {symbol} position closed on Binance"
                            )

                        await supa_patch(trade_id, updates)
                        corrections.append(
                            f"Trade #{trade_id} {symbol} -> {alert.new_status} ({alert.close_reason})"
                        )

                    # ── EP filled ──
                    elif alert.event_type == "ep_filled":
                        for ep in ("ep1", "ep2", "ep3"):
                            if existing.get(f"{ep}_status") == "waiting":
                                updates = {f"{ep}_status": "filled"}
                                # Check actual order on Binance for fill details
                                oid = existing.get(f"{ep}_id")
                                if oid and use_futures:
                                    try:
                                        order = await binance_client.futures_get_order(symbol, int(oid))
                                        if order and order.get("status") == "FILLED":
                                            qty = float(order.get("executedQty", 0))
                                            price = float(order.get("avgPrice", 0)) or existing.get(ep)
                                            updates[f"{ep}_size_crypto"] = qty
                                            updates[f"{ep}_size_usdt"] = qty * price
                                    except Exception:
                                        pass
                                await supa_patch(trade_id, updates)
                                corrections.append(
                                    f"Trade #{trade_id} {symbol} {ep} -> filled (alert)"
                                )
                                break

                    # ── TP hit ──
                    elif alert.event_type == "tp_hit" and alert.tp_level:
                        tp_key = f"tp{alert.tp_level}"
                        # Skip if TP price doesn't exist in Supabase
                        if not existing.get(tp_key):
                            print(f"  SKIP TP{alert.tp_level} hit for #{trade_id} {symbol}: no {tp_key} price in DB")
                            continue
                        tp_status_key = f"tp{alert.tp_level}_status"
                        updates = {tp_status_key: "filled"}
                        # If combined with "stops moved to be"
                        if "stops moved to be" in (alert.action or "").lower():
                            be_price = existing.get("ep1")
                            if be_price:
                                # Cancel old SL (algo order) and place new one at BE
                                if existing.get("sl_id") and existing.get("sl_status") == "waiting":
                                    try:
                                        if use_futures:
                                            await binance_client.futures_cancel_algo_order(int(existing["sl_id"]))
                                        else:
                                            await binance_client.cancel_order(symbol, int(existing["sl_id"]))
                                    except Exception:
                                        pass

                                # Cancel unfilled EP orders
                                for ep in ("ep1", "ep2", "ep3"):
                                    oid = existing.get(f"{ep}_id")
                                    if oid and existing.get(f"{ep}_status") == "waiting":
                                        try:
                                            if use_futures:
                                                await binance_client.futures_cancel_order(symbol, int(oid))
                                            else:
                                                await binance_client.cancel_order(symbol, int(oid))
                                            await supa_patch(trade_id, {f"{ep}_status": None, f"{ep}_id": None})
                                            corrections.append(
                                                f"Trade #{trade_id} {symbol} {ep} cancelled (TP+BE)"
                                            )
                                        except Exception:
                                            pass

                                # Use actual position size from Binance
                                total_qty = 0.0
                                if use_futures:
                                    pos = await binance_client.futures_get_position(symbol)
                                    if pos:
                                        total_qty = abs(float(pos.get("positionAmt", 0)))
                                if total_qty <= 0:
                                    for ep in ("ep1", "ep2", "ep3"):
                                        if existing.get(f"{ep}_status") == "filled":
                                            qty = existing.get(f"{ep}_size_crypto")
                                            if qty:
                                                total_qty += float(qty)

                                if total_qty > 0:
                                    sl_side = "SELL" if side == "LONG" else "BUY"
                                    if use_futures:
                                        sl_order = await binance_client.futures_place_stop_loss_order(
                                            symbol, sl_side, total_qty, float(be_price),
                                        )
                                    else:
                                        sl_order = await binance_client.place_stop_loss_order(
                                            symbol, sl_side, total_qty, float(be_price),
                                        )
                                    if sl_order:
                                        updates["sl"] = float(be_price)
                                        updates["sl_id"] = str(sl_order["orderId"])
                                        updates["sl_status"] = "waiting"
                                        corrections.append(
                                            f"Trade #{trade_id} {symbol} SL moved to BE @ {be_price} (TP{alert.tp_level} hit)"
                                        )

                        await supa_patch(trade_id, updates)
                        corrections.append(
                            f"Trade #{trade_id} {symbol} TP{alert.tp_level} -> filled"
                        )

        # ── 5. Verify EP statuses on Binance ──
        for t in trades:
            if t.get("status") != "open":
                continue
            symbol = t["symbol"]
            for ep in ("ep1", "ep2", "ep3"):
                oid = t.get(f"{ep}_id")
                if not oid:
                    continue
                try:
                    use_futures = "spot" not in (t.get("source_channel") or "").lower()
                    if use_futures:
                        order = await binance_client.futures_get_order(symbol, int(oid))
                    else:
                        order = await binance_client.get_order(symbol, int(oid))
                    if not order:
                        continue
                    bs = order.get("status")
                    ss = t.get(f"{ep}_status")
                    if bs == "FILLED" and ss != "filled":
                        qty = float(order.get("executedQty", 0))
                        price = float(order.get("avgPrice", 0)) or t.get(ep)
                        updates = {
                            f"{ep}_status": "filled",
                            f"{ep}_size_crypto": qty,
                            f"{ep}_size_usdt": qty * price,
                        }
                        await supa_patch(t["id"], updates)
                        corrections.append(
                            f"Trade #{t['id']} {symbol} {ep} -> filled (qty={qty})"
                        )

                        # If EP just filled, check if SL needs to be placed
                        if not t.get("sl_id") and t.get("sl"):
                            sl_side = "SELL" if t.get("side") == "LONG" else "BUY"
                            if use_futures:
                                await binance_client.futures_setup_symbol(symbol, 5, "ISOLATED")
                                sl_order = await binance_client.futures_place_stop_loss_order(
                                    symbol, sl_side, qty, float(t["sl"]),
                                )
                            else:
                                sl_order = await binance_client.place_stop_loss_order(
                                    symbol, sl_side, qty, float(t["sl"]),
                                )
                            if sl_order:
                                await supa_patch(t["id"], {
                                    "sl_id": str(sl_order["orderId"]),
                                    "sl_status": "waiting",
                                })
                                corrections.append(
                                    f"Trade #{t['id']} {symbol} SL placed @ {t['sl']}"
                                )

                    elif bs == "CANCELED" and ss != "cancelled":
                        await supa_patch(t["id"], {f"{ep}_status": "cancelled"})
                        corrections.append(
                            f"Trade #{t['id']} {symbol} {ep} -> cancelled"
                        )
                except Exception as e:
                    print(f"  EP check error #{t['id']} {ep}: {e}")

        # ── 6. Check positions vs Supabase + fix leverage/margin ──
        symbols = set(t["symbol"] for t in trades if t.get("status") == "open")
        for symbol in symbols:
            pos = await binance_client.futures_get_position(symbol)
            amt = float(pos.get("positionAmt", 0)) if pos else 0
            leverage = pos.get("leverage") if pos else None
            margin = pos.get("marginType") if pos else None

            if amt != 0:
                print(
                    f"  Binance {symbol}: amt={amt} leverage={leverage} margin={margin}"
                )
                if (leverage and int(leverage) != 5) or (margin and margin != "isolated"):
                    await binance_client.futures_setup_symbol(symbol, 5, "ISOLATED")
                    corrections.append(
                        f"Trade {symbol}: fixed leverage/margin to 5x ISOLATED"
                    )
            else:
                print(f"  Binance {symbol}: no position")
                # Detect orphan trades: EPs filled but position = 0 → trade was closed
                for t in trades:
                    if t["symbol"] != symbol or t.get("status") != "open":
                        continue
                    has_filled = any(
                        t.get(f"{ep}_status") == "filled"
                        for ep in ("ep1", "ep2", "ep3")
                    )
                    if not has_filled:
                        continue
                    # This trade has filled EPs but no position — it was closed
                    # Cancel orphan SL if exists
                    if t.get("sl_id"):
                        try:
                            import hmac as _hmac2, hashlib as _hl2, time as _t2
                            _ts2 = str(int(_t2.time() * 1000))
                            _ak2 = os.getenv("BINANCE_API_KEY")
                            _as2 = os.getenv("BINANCE_SECRET_KEY")
                            _p2 = f'algoId={t["sl_id"]}&timestamp={_ts2}'
                            _sig2 = _hmac2.new(_as2.encode(), _p2.encode(), _hl2.sha256).hexdigest()
                            await s.delete(
                                f'https://testnet.binancefuture.com/fapi/v1/algoOrder?{_p2}&signature={_sig2}',
                                headers={"X-MBX-APIKEY": _ak2},
                            )
                        except Exception:
                            pass
                    await supa_patch(t["id"], {
                        "status": "closed",
                        "close_reason": "profit",
                        "sl_status": None,
                    })
                    corrections.append(
                        f"Trade #{t['id']} {symbol} closed (position=0, EPs were filled)"
                    )

        # ── 7. Check SL for filled positions — AUTO-PLACE if missing ──
        # Re-fetch trades since we may have updated them
        async with s.get(
            f"{SUPA_URL}/rest/v1/trades?status=eq.open&order=created_at.desc",
            headers=supa_h,
        ) as r:
            trades = await r.json()

        for t in trades:
            symbol = t["symbol"]
            use_futures = "spot" not in (t.get("source_channel") or "").lower()
            has_filled = any(
                t.get(f"{ep}_status") == "filled" for ep in ("ep1", "ep2", "ep3")
            )

            if not has_filled or not t.get("sl"):
                continue

            # Check if SL order exists on Binance
            # SL orders are algo/conditional orders — must query via /fapi/v1/algoOrder
            sl_exists = False
            if t.get("sl_id"):
                try:
                    if use_futures:
                        # Query algo order status directly
                        import hmac, hashlib, time as _time
                        _ts = str(int(_time.time() * 1000))
                        _ak = os.getenv("BINANCE_API_KEY")
                        _as = os.getenv("BINANCE_SECRET_KEY")
                        _params = f'algoId={t["sl_id"]}&timestamp={_ts}&recvWindow=5000'
                        _sig = hmac.new(_as.encode(), _params.encode(), hashlib.sha256).hexdigest()
                        _url = f'https://testnet.binancefuture.com/fapi/v1/algoOrder?{_params}&signature={_sig}'
                        async with s.get(_url, headers={"X-MBX-APIKEY": _ak}) as _r:
                            if _r.status == 200:
                                _algo = await _r.json()
                                _algo_status = _algo.get("algoStatus", "")
                                if _algo_status == "NEW":
                                    sl_exists = True
                                    print(f"  OK #{t['id']} {symbol} SL algoId={t['sl_id']} is active (algoStatus=NEW)")
                                elif _algo_status == "TRIGGERED":
                                    # SL was triggered — close the trade
                                    await supa_patch(t["id"], {
                                        "status": "closed",
                                        "close_reason": "stopped_out",
                                        "sl_status": "filled",
                                    })
                                    corrections.append(
                                        f"Trade #{t['id']} {symbol} SL triggered -> closed (stopped_out)"
                                    )
                                    continue
                                elif _algo_status in ("CANCELLED", "EXPIRED"):
                                    print(f"  WARN #{t['id']} {symbol} SL algoId={t['sl_id']} is {_algo_status}")
                                else:
                                    print(f"  #{t['id']} {symbol} SL algoId={t['sl_id']} status={_algo_status}")
                    else:
                        sl_order = await binance_client.get_order(symbol, int(t["sl_id"]))
                        if sl_order and sl_order.get("status") in ("NEW", "PARTIALLY_FILLED"):
                            sl_exists = True
                        elif sl_order and sl_order.get("status") == "FILLED":
                            await supa_patch(t["id"], {
                                "status": "closed",
                                "close_reason": "stopped_out",
                                "sl_status": "filled",
                            })
                            corrections.append(
                                f"Trade #{t['id']} {symbol} SL triggered -> closed (stopped_out)"
                            )
                            continue
                except Exception as e:
                    print(f"  WARN #{t['id']} {symbol} SL check error: {e}")

            if not sl_exists:
                # Check if there's actually a position
                pos = await binance_client.futures_get_position(symbol) if use_futures else None
                if pos and float(pos.get("positionAmt", 0)) != 0:
                    total_qty = abs(float(pos["positionAmt"]))
                    sl_side = "SELL" if t.get("side") == "LONG" else "BUY"
                    sl_price = float(t["sl"])

                    if use_futures:
                        sl_order = await binance_client.futures_place_stop_loss_order(
                            symbol, sl_side, total_qty, sl_price,
                        )
                    else:
                        sl_order = await binance_client.place_stop_loss_order(
                            symbol, sl_side, total_qty, sl_price,
                        )

                    if sl_order:
                        await supa_patch(t["id"], {
                            "sl_id": str(sl_order["orderId"]),
                            "sl_status": "waiting",
                        })
                        corrections.append(
                            f"Trade #{t['id']} {symbol} SL AUTO-PLACED @ {sl_price}"
                        )
                    else:
                        corrections.append(
                            f"WARN: Trade #{t['id']} {symbol} FAILED to place SL @ {sl_price}!"
                        )

        # ── 8. Check trader names ──
        for t in trades:
            if t.get("trader") == "Wealth Group":
                msg_id = t.get("discord_message_id")
                if msg_id:
                    ch_id = CHANNELS["trades"]
                    url = f"https://discord.com/api/v9/channels/{ch_id}/messages?around={msg_id}&limit=3"
                    async with s.get(url, headers=headers) as r:
                        msgs = await r.json()
                    for m in msgs:
                        if m["id"] == msg_id:
                            resolved = resolve(m.get("content", ""))
                            trader_match = re.search(r"@WWG:\s*(\w+)", resolved)
                            if trader_match:
                                new_name = trader_match.group(1)
                                await supa_patch(t["id"], {"trader": new_name})
                                corrections.append(
                                    f"Trade #{t['id']} trader: Wealth Group -> {new_name}"
                                )

        # ── 9. TP search in embeds + update Supabase ──
        # Build a map: discord_message_id -> trade (for precise matching)
        msg_id_to_trade = {}
        for t in trades:
            mid = t.get("discord_message_id")
            if mid:
                msg_id_to_trade[str(mid)] = t

        for msg in discord_data.get("trades", {}).get("all", []):
            if msg["author"]["username"] != "WG Bot":
                continue
            for embed in msg.get("embeds", []):
                desc = embed.get("description", "") or ""
                tp_match = re.search(r"\*\*TPs?:\*\*\s*(.+)", desc)
                if not tp_match:
                    continue
                dir_match = re.search(r":(Long|Short):\s*\*\*(?:LIMIT\s*\*\*\s*\*\*)?(\w+)\*\*", desc)
                if not dir_match:
                    continue

                tp_section = tp_match.group(1)
                prices, hits = [], []
                for part in tp_section.split("|"):
                    part = part.strip()
                    if not part:
                        continue
                    is_hit = "\u2713" in part or "\u2705" in part
                    num = re.search(r"([\d.]+)", part)
                    if num:
                        prices.append(float(num.group(1)))
                        hits.append(is_hit)

                if not prices:
                    continue

                # Match by discord_message_id first (precise)
                matched_trade = msg_id_to_trade.get(msg["id"])

                if not matched_trade:
                    # Fallback: check if this embed is an edit/update of an
                    # existing trade message (same message_id as a trade)
                    # Also check referenced messages
                    ref = msg.get("message_reference", {})
                    ref_id = ref.get("message_id")
                    if ref_id:
                        matched_trade = msg_id_to_trade.get(str(ref_id))

                if not matched_trade:
                    continue

                t = matched_trade
                tp_updates = {}
                for i, (price, hit) in enumerate(zip(prices, hits)):
                    tp_num = i + 1
                    if tp_num > 6:
                        break
                    key = f"tp{tp_num}"
                    if t.get(key) != price:
                        tp_updates[key] = price
                    if hit and t.get(f"{key}_status") != "filled":
                        tp_updates[f"{key}_status"] = "filled"
                    elif not hit and not t.get(f"{key}_status"):
                        tp_updates[f"{key}_status"] = "waiting"

                if tp_updates:
                    await supa_patch(t["id"], tp_updates)
                    corrections.append(
                        f"Trade #{t['id']} {t['symbol']} TPs updated: {tp_updates}"
                    )

        # ── 9b. Check if TP orders have been filled on Binance ──
        for t in trades:
            use_futures = "spot" not in (t.get("source_channel") or "").lower()
            if not use_futures:
                continue
            for i in range(1, 7):
                tp_id = t.get(f"tp{i}_id")
                tp_status = t.get(f"tp{i}_status")
                if tp_id and tp_status == "waiting":
                    try:
                        order = await binance_client.futures_get_order(
                            t["symbol"], int(tp_id)
                        )
                        if order and order.get("status") == "FILLED":
                            await supa_patch(t["id"], {f"tp{i}_status": "filled"})
                            corrections.append(
                                f"Trade #{t['id']} {t['symbol']} TP{i} filled on Binance"
                            )
                        elif order and order.get("status") in ("CANCELED", "EXPIRED"):
                            await supa_patch(t["id"], {
                                f"tp{i}_id": None,
                                f"tp{i}_status": "waiting" if t.get(f"tp{i}") else None,
                            })
                    except Exception:
                        pass

        # ── 9c. Place TP orders on Binance for trades with TPs but no orders ──
        # Re-fetch trades to get updated TP values
        async with s.get(
            f"{SUPA_URL}/rest/v1/trades?status=eq.open&order=created_at.desc",
            headers=supa_h,
        ) as r:
            trades = await r.json()

        # Build map of existing reduce-only qty per symbol (from open orders)
        existing_reduce_qty = {}
        for t in trades:
            symbol = t["symbol"]
            for i in range(1, 7):
                tp_id = t.get(f"tp{i}_id")
                tp_status = t.get(f"tp{i}_status")
                tp_price = t.get(f"tp{i}")
                if tp_id and tp_status == "waiting" and tp_price:
                    qty = 0.0
                    # Estimate qty from trade's EP allocation
                    ep_qty = sum(float(t.get(f"ep{e}_size_crypto") or 0) for e in (1,2,3) if t.get(f"ep{e}_status") == "filled")
                    n_tps = sum(1 for j in range(1,7) if t.get(f"tp{j}") and t.get(f"tp{j}_status") == "waiting")
                    if n_tps > 0:
                        qty = ep_qty / n_tps
                    existing_reduce_qty[symbol] = existing_reduce_qty.get(symbol, 0) + qty

        for t in trades:
            use_futures = "spot" not in (t.get("source_channel") or "").lower()
            if not use_futures:
                continue

            symbol = t["symbol"]

            # Check if trade has filled position
            total_qty = 0.0
            for ep in ("ep1", "ep2", "ep3"):
                if t.get(f"{ep}_status") == "filled":
                    qty = t.get(f"{ep}_size_crypto")
                    if qty:
                        total_qty += float(qty)
            if total_qty <= 0:
                continue

            # Collect TPs that need orders
            tp_prices = []
            for i in range(1, 7):
                tp = t.get(f"tp{i}")
                tp_id = t.get(f"tp{i}_id")
                tp_status = t.get(f"tp{i}_status")
                if tp and not tp_id and tp_status == "waiting":
                    tp_prices.append((i, float(tp)))

            if not tp_prices:
                continue

            # Check actual Binance position to avoid ReduceOnly rejection
            pos = await binance_client.futures_get_position(symbol)
            pos_qty = abs(float(pos.get("positionAmt", 0))) if pos else 0
            already_allocated = existing_reduce_qty.get(symbol, 0)
            available_for_tp = pos_qty - already_allocated
            if available_for_tp <= 0:
                continue

            tp_side = "SELL" if t.get("side") == "LONG" else "BUY"
            num_tps = len(tp_prices)
            # Cap total_qty to available
            total_qty = min(total_qty, available_for_tp)

            for idx, (tp_num, tp_price) in enumerate(tp_prices):
                if idx < num_tps - 1:
                    tp_qty = round(total_qty / num_tps, 8)
                else:
                    tp_qty = round(total_qty - round(total_qty / num_tps, 8) * (num_tps - 1), 8)

                if tp_qty <= 0:
                    continue

                tp_order = await binance_client.futures_place_tp_order(
                    t["symbol"], tp_side, tp_qty, tp_price,
                )
                if tp_order:
                    order_id = tp_order.get("orderId")
                    await supa_patch(t["id"], {f"tp{tp_num}_id": str(order_id)})
                    existing_reduce_qty[symbol] = existing_reduce_qty.get(symbol, 0) + tp_qty
                    corrections.append(
                        f"Trade #{t['id']} {t['symbol']} TP{tp_num} placed @ {tp_price} qty={tp_qty} (orderId={order_id})"
                    )

        # ── 9c. Rebalance: promote tracking trades close to price ──
        import trade_executor as te
        rebalance_actions = await te.rebalance_active_trades(use_futures=True)
        for ra in rebalance_actions:
            corrections.append(ra)

        # ── 10. Risk summary ──
        bal = await binance_client.futures_get_balance("USDT")
        tradeable = bal * 0.8
        risk_1r = tradeable * 0.01
        total_risk = sum(float(t.get("risk_usdt") or 0) for t in trades)

        print(
            f"\nRISK: Balance={bal:.0f} 1R={risk_1r:.2f} "
            f"TotalRisk={total_risk:.2f} ({total_risk / risk_1r:.1f}R)"
            if risk_1r > 0 else f"\nRISK: Balance={bal:.0f}"
        )

        if risk_1r > 0 and total_risk / risk_1r > 3:
            print("  WARNING: Total risk exceeds 3R!")

        if corrections:
            print(f"\n{len(corrections)} CORRECTIONS APPLIED:")
            for c in corrections:
                print(f"  - {c}")
        else:
            print("\nNO CORRECTIONS NEEDED - all consistent")

        await binance_client.close()


if __name__ == "__main__":
    asyncio.run(main())
