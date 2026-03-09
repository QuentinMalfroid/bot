"""One-shot checkup script — do NOT import into the bot."""
import asyncio, aiohttp, re, json
from datetime import datetime, timezone, timedelta
import config, binance_client

GUILD_ID = "1265052061976891454"
CUTOFF = datetime.now(timezone.utc) - timedelta(minutes=35)

async def checkup():
    discord_h = {"Authorization": config.DISCORD_TOKEN}
    supa_h = {
        "apikey": config.SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    supa = f"{config.SUPABASE_URL}/rest/v1"

    async with aiohttp.ClientSession() as s:
        # --- Role map ---
        async with s.get(f"https://discord.com/api/v9/guilds/{GUILD_ID}/roles", headers=discord_h) as r:
            roles = await r.json()
        rmap = {}
        for role in roles:
            if role["name"].startswith("WWG: "):
                rmap[role["id"]] = role["name"].replace("WWG: ", "").lstrip("-").strip()

        def resolve(text):
            if not text:
                return text
            for rid, name in rmap.items():
                text = text.replace(f"<@&{rid}>", f"@{name}")
            return text

        # ===== 1. DISCORD =====
        print("=" * 60)
        print("1. DISCORD - Recent Messages")
        print("=" * 60)
        channels = {
            "trades": str(config.TRADES_THREAD_ID),
            "alerts": str(config.ALERTS_THREAD_ID),
            "futures": str(config.ACTIVE_FUTURES_ID),
            "spot": str(config.ACTIVE_SPOT_ID),
        }
        discord_msgs = {}
        for name, cid in channels.items():
            async with s.get(f"https://discord.com/api/v9/channels/{cid}/messages?limit=20", headers=discord_h) as r:
                msgs = await r.json()
            recent = [m for m in msgs if datetime.fromisoformat(m["timestamp"]) > CUTOFF]
            discord_msgs[name] = msgs
            print(f"  {name}: {len(recent)} new / {len(msgs)} total")
            for m in recent:
                content = resolve(m.get("content", ""))[:150]
                print(f"    [{m['id']}] {content}")
                for emb in m.get("embeds", []):
                    desc = resolve(emb.get("description", ""))
                    if desc:
                        print(f"      EMBED: {desc[:150]}")

        # ===== 2. SUPABASE =====
        print()
        print("=" * 60)
        print("2. SUPABASE - Open Trades")
        print("=" * 60)
        async with s.get(f"{supa}/trades?status=eq.open&order=created_at.desc", headers=supa_h) as r:
            open_trades = await r.json()
        print(f"  Open trades: {len(open_trades)}")
        for t in open_trades:
            eps = []
            for ep in ("ep1", "ep2", "ep3"):
                if t.get(ep):
                    eps.append(f"{ep}={t[ep]}({t.get(f'{ep}_status', '?')})")
            print(f"  #{t['id']} {t['symbol']} {t['side']} by {t['trader']} | {' '.join(eps)} | SL={t.get('sl')} ({t.get('sl_status')}) | sl_type={t.get('sl_type')}")

        # ===== 3. BINANCE =====
        print()
        print("=" * 60)
        print("3. BINANCE - Orders & Positions")
        print("=" * 60)
        bal = await binance_client.futures_get_balance()
        print(f"  Futures balance: ${bal:.2f}")

        for t in open_trades:
            symbol = t["symbol"]
            print(f"\n  --- {symbol} (Trade #{t['id']}) ---")

            # Check EP orders
            for ep in ("ep1", "ep2", "ep3"):
                if not t.get(f"{ep}_id"):
                    continue
                order_id = int(t[f"{ep}_id"])
                order = await binance_client.futures_get_order(symbol, order_id)
                if order:
                    bstat = order.get("status", "")
                    sstat = t.get(f"{ep}_status", "")
                    filled_qty = float(order.get("executedQty", 0))
                    avg_price = float(order.get("avgPrice", 0))

                    if bstat == "FILLED" and sstat != "filled":
                        print(f"    FIX: {ep} FILLED on Binance but '{sstat}' in Supabase")
                        updates = {f"{ep}_status": "filled", f"{ep}_size_crypto": filled_qty, f"{ep}_size_usdt": round(filled_qty * avg_price, 2)}
                        if avg_price > 0:
                            updates[ep] = avg_price
                        async with s.patch(f"{supa}/trades?id=eq.{t['id']}", json=updates, headers=supa_h) as r:
                            print(f"    -> Updated: HTTP {r.status}")
                    elif bstat in ("CANCELED", "EXPIRED") and sstat == "waiting":
                        print(f"    FIX: {ep} {bstat} on Binance")
                        async with s.patch(f"{supa}/trades?id=eq.{t['id']}", json={f"{ep}_status": "cancelled"}, headers=supa_h) as r:
                            print(f"    -> Updated: HTTP {r.status}")
                    else:
                        print(f"    {ep}: Binance={bstat}, Supabase={sstat} - OK")
                else:
                    print(f"    {ep}: order {order_id} not found on Binance")

            # Check position
            pos = await binance_client.futures_get_position(symbol)
            if pos and float(pos.get("positionAmt", 0)) != 0:
                pos_amt = float(pos["positionAmt"])
                lev = pos.get("leverage", "?")
                mtype = pos.get("marginType", "?")
                entry = float(pos.get("entryPrice", 0))
                upnl = float(pos.get("unRealizedProfit", 0))
                print(f"    Position: {pos_amt} @ {entry} | {lev}x {mtype} | uPnL: {upnl:.2f}")

                if str(lev) != "5":
                    print(f"    FIX: Leverage {lev}x -> 5x")
                    await binance_client.futures_setup_symbol(symbol, 5, "ISOLATED")
                if mtype.lower() != "isolated":
                    print(f"    FIX: Margin {mtype} -> ISOLATED")

                has_filled = any(t.get(f"{ep}_status") == "filled" for ep in ("ep1", "ep2", "ep3"))
                if has_filled and not t.get("sl_id") and t.get("sl_type") != "candle_2x5m":
                    print(f"    WARNING: No SL order! Should place at {t.get('sl')}")
                elif t.get("sl_type") == "candle_2x5m":
                    print(f"    SL: Candle-based (2x 5m < {t.get('sl')}) - VPS monitored")

                supa_qty = sum(float(t.get(f"{ep}_size_crypto") or 0) for ep in ("ep1", "ep2", "ep3") if t.get(f"{ep}_status") == "filled")
                if supa_qty > 0 and abs(abs(pos_amt) - supa_qty) > 0.0001:
                    print(f"    WARNING: Size mismatch! Binance={abs(pos_amt)}, Supabase={supa_qty}")
            else:
                has_filled = any(t.get(f"{ep}_status") == "filled" for ep in ("ep1", "ep2", "ep3"))
                if has_filled:
                    print(f"    WARNING: Filled EPs but NO position on Binance!")
                else:
                    print(f"    No position (EPs not filled) - OK")

        # Open orders
        oo = await binance_client.futures_get_open_orders()
        print(f"\n  Open orders on Binance: {len(oo or [])}")
        for o in (oo or []):
            print(f"    {o['symbol']} {o['side']} {o['type']} qty={o['origQty']} @ {o['price']}")

        # ===== 4. RISK =====
        print()
        print("=" * 60)
        print("4. RISK MANAGEMENT")
        print("=" * 60)
        one_r = bal * 0.80 * 0.01
        total_risk = sum(float(t.get("risk_usdt") or 0) for t in open_trades)
        max_dd = bal * 0.03
        print(f"  Balance: ${bal:.2f} | 1R = ${one_r:.2f}")
        print(f"  Open risk: ${total_risk:.2f} ({total_risk/one_r:.1f}R) | Max DD (3%): ${max_dd:.2f}")
        for t in open_trades:
            risk = float(t.get("risk_usdt") or 0)
            rm = risk / one_r if one_r > 0 else 0
            flag = "OK" if 0.5 <= rm <= 1.5 else "WARNING"
            print(f"    #{t['id']} {t['symbol']}: ${risk:.2f} ({rm:.2f}R) {flag}")
        print(f"  {'ALERT: Over max DD!' if total_risk > max_dd else 'Within limits'}")

        # ===== 5. TAKE PROFITS =====
        print()
        print("=" * 60)
        print("5. TAKE PROFITS")
        print("=" * 60)
        tp_pat = re.compile(r"\*\*TPs?:\*\*\s*(.*?)(?:\n|$)", re.IGNORECASE)
        tp_price_pat = re.compile(r"(\u2713)?\s*([\d.]+)")

        for t in open_trades:
            msg_id = t.get("discord_message_id")
            sym_short = t["symbol"].replace("USDT", "")
            print(f"  Trade #{t['id']} {t['symbol']} (msg={msg_id})")

            found_tps = []
            # First look in already-fetched messages
            target_msg = None
            for msg in discord_msgs.get("trades", []):
                if msg["id"] == msg_id:
                    target_msg = msg
                    break
            # If not found in batch, fetch specifically by ID
            if not target_msg and msg_id:
                try:
                    async with s.get(
                        f"https://discord.com/api/v9/channels/{channels['trades']}/messages?around={msg_id}&limit=3",
                        headers=discord_h,
                    ) as r:
                        around = await r.json()
                    for msg in around:
                        if msg["id"] == msg_id:
                            target_msg = msg
                            break
                except Exception:
                    pass
            if target_msg:
                for emb in target_msg.get("embeds", []):
                    desc = resolve(emb.get("description", "") or "")
                    tp_match = tp_pat.search(desc)
                    if tp_match:
                        for m in tp_price_pat.finditer(tp_match.group(1)):
                            found_tps.append((float(m.group(2)), m.group(1) == "\u2713"))

            if found_tps:
                updates = {}
                for i, (price, filled) in enumerate(found_tps[:6]):
                    k = f"tp{i+1}"
                    if t.get(k) != price:
                        updates[k] = price
                    st = "filled" if filled else "waiting"
                    if t.get(f"{k}_status") != st:
                        updates[f"{k}_status"] = st
                if updates:
                    print(f"    Updating TPs: {updates}")
                    async with s.patch(f"{supa}/trades?id=eq.{t['id']}", json=updates, headers=supa_h) as r:
                        print(f"    -> HTTP {r.status}")
                else:
                    print(f"    TPs up to date")
            else:
                print(f"    No TPs in Discord embeds")

        # ===== 6. RAPPORT =====
        print()
        print("=" * 60)
        print("6. CHECKUP COMPLETE")
        print("=" * 60)

asyncio.run(checkup())
