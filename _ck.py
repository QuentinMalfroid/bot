import asyncio,os,hmac,hashlib,time,httpx,base64,json
from dotenv import load_dotenv
load_dotenv("c:/Users/quema/CryptoBot/.env")
DT=os.getenv("DISCORD_TOKEN");AK=os.getenv("BINANCE_API_KEY");AS=os.getenv("BINANCE_SECRET_KEY")
SU=os.getenv("SUPABASE_URL");SK=os.getenv("SUPABASE_ANON_KEY");OAK=os.getenv("OPENAI_API_KEY")
B="https://testnet.binancefuture.com";G="1265052061976891454"
TT="1301066783909744680";AL="1301074706656530474";AF="1304468260040740894";SP="1306633648984031263"
dh={"Authorization":DT,"Content-Type":"application/json"}
sh={"apikey":SK,"Authorization":f"Bearer {SK}","Content-Type":"application/json","Prefer":"return=representation"}
def sg(p):
    q="&".join(f"{k}={v}" for k,v in p.items());return q+f"&signature={hmac.new(AS.encode(),q.encode(),hashlib.sha256).hexdigest()}"
from datetime import datetime,timedelta,timezone
ct=datetime.now(timezone.utc)-timedelta(minutes=30);cs=int((ct.timestamp()-1420070400)*1000)<<22

GPT_PROMPT = """This is a crypto trading chart for a {direction} {symbol} position.
I already know: Entry = {entry}, Stop Loss = {sl}.
The chart uses European number formatting (dots = thousands separator, comma = decimal).
So "71.604,89" on the axis means 71604.89 USD.
Find the TAKE PROFIT level(s). For a LONG: the green zone ABOVE entry, read the price label at TOP of green zone.
For a SHORT: the green zone BELOW entry. There may be 1 or more TP levels.
Return ONLY valid JSON: {{"take_profits": [number, ...]}}
Prices must be in full USD (e.g. 71604.89 not 71.604)."""

async def gpt_read_chart(c, img_url, symbol, direction, entry, sl):
    """Download chart image and ask GPT-4o Mini for TPs"""
    try:
        r = await c.get(img_url, timeout=10)
        if r.status_code != 200:
            return None
        img_b64 = base64.b64encode(r.content).decode()
        # Save image for Claude verification
        with open(f"c:/Users/quema/CryptoBot/_chart_{symbol}.png", "wb") as f:
            f.write(r.content)
        prompt = GPT_PROMPT.format(direction=direction, symbol=symbol, entry=entry, sl=sl)
        r2 = await c.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OAK}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
                ]}],
                "max_tokens": 200
            }, timeout=30
        )
        data = r2.json()
        if "choices" not in data:
            return None
        resp = data["choices"][0]["message"]["content"]
        clean = resp
        if "```" in clean:
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        parsed = json.loads(clean.strip())
        cost = 0
        if "usage" in data:
            u = data["usage"]
            cost = u.get("prompt_tokens", 0) * 0.15 / 1e6 + u.get("completion_tokens", 0) * 0.6 / 1e6
        return {"take_profits": parsed.get("take_profits", []), "cost": cost, "raw": resp}
    except Exception as e:
        return {"error": str(e)}

async def main():
    fx=[]
    async with httpx.AsyncClient(timeout=15) as c:
        r=await c.get(f"https://discord.com/api/v10/guilds/{G}/roles",headers=dh);rl=r.json()
        ww={str(x["id"]):x["name"].replace("WWG: ","") for x in rl if x["name"].startswith("WWG: ")} if isinstance(rl,list) else {}
        print("=== 1. DISCORD ===")
        for n,ci in {"trades":TT,"alerts":AL,"futures":AF,"spot":SP}.items():
            await asyncio.sleep(0.4)
            try:
                r=await c.get(f"https://discord.com/api/v10/channels/{ci}/messages",headers=dh,params={"after":str(cs),"limit":50});ms=r.json()
                if not isinstance(ms,list):print(f"  {n}: err");continue
                if not ms:print(f"  {n}: (none)");continue
                for m in reversed(ms):
                    x=m.get("content","")
                    for ri,tn in ww.items():x=x.replace(f"<@&{ri}>",f"@{tn}")
                    print(f"  {n}: [{m['timestamp'][:19]}] {x[:250]}")
                    for e in m.get("embeds",[]):
                        d=e.get("description","")
                        if d:
                            for ri,tn in ww.items():d=d.replace(f"<@&{ri}>",f"@{tn}")
                            print(f"    EMB: {d[:300]}")
            except:print(f"  {n}: ERR")
        print("\n=== 2. SUPABASE ===")
        r=await c.get(f"{SU}/rest/v1/trades?status=eq.open&order=id.asc",headers=sh);trades=r.json()
        for t in trades:print(f"  #{t['id']} {t['symbol']} {t['side']} {t['trader']} [{t.get('source_channel')}] EP1={t['ep1']}({t['ep1_status']}) SL={t['sl']}({t['sl_status']})")
        print("\n=== 3. BINANCE ===")
        bh={"X-MBX-APIKEY":AK}
        r=await c.get(f"{B}/fapi/v2/balance?{sg({'timestamp':int(time.time()*1000),'recvWindow':5000})}",headers=bh);ub=0
        for b in r.json():
            if b["asset"]=="USDT":ub=float(b["balance"]);print(f"  USDT: ${ub:.2f}")
        r=await c.get(f"{B}/fapi/v2/positionRisk?{sg({'timestamp':int(time.time()*1000),'recvWindow':5000})}",headers=bh);pos={}
        for p in r.json():
            if float(p.get("positionAmt",0))!=0:pos[p["symbol"]]=p;print(f"  Pos {p['symbol']}: {p['positionAmt']} @ {p['entryPrice']} uPnL=${float(p['unRealizedProfit']):.2f} {p['leverage']}x {p['marginType']}")
        if not pos:print("  (no pos)")
        r=await c.get(f"{B}/fapi/v1/openOrders?{sg({'timestamp':int(time.time()*1000),'recvWindow':5000})}",headers=bh);ob={}
        for o in r.json():ob.setdefault(o["symbol"],[]).append(o);print(f"  Ord {o['symbol']}: {o['side']} {o['type']} qty={o['origQty']} price={o.get('price')} id={o['orderId']}")
        if not r.json():print("  (no ord)")
        print("\n=== 4. CHECK ===")
        tr=0
        for t in trades:
            ti=t["id"];sy=t["symbol"];sc=t.get("source_channel","")
            if sc=="tracking":print(f"  #{ti} {sy}: tracking");continue
            if t.get("ep1_status")=="filled":
                p=pos.get(sy)
                if p:
                    hs=any(o["type"]in("STOP_MARKET","STOP")for o in ob.get(sy,[]))
                    if not hs:
                        si=t.get("sl_id")
                        if si:
                            r2=await c.get(f"{B}/fapi/v1/algoOrder?{sg({'algoId':si,'timestamp':int(time.time()*1000),'recvWindow':5000})}",headers=bh);a=r2.json()
                            if a.get("algoStatus")in("NEW","WORKING"):print(f"  #{ti} {sy}: pos OK, SL algo ({a.get('triggerPrice')})")
                            else:print(f"  #{ti} {sy}: SL expired!");fx.append(f"#{ti} SL expired")
                        else:print(f"  #{ti} {sy}: NO SL!");fx.append(f"#{ti} no SL")
                    else:print(f"  #{ti} {sy}: pos+SL OK")
                else:print(f"  #{ti} {sy}: NO pos!");fx.append(f"#{ti} no pos")
            elif t.get("ep1_status")=="waiting":
                if sy in pos:
                    a2=abs(float(pos[sy]["positionAmt"]));e2=float(pos[sy]["entryPrice"])
                    r2=await c.patch(f"{SU}/rest/v1/trades?id=eq.{ti}",headers=sh,json={"ep1_status":"filled","ep1_size_crypto":str(a2),"ep1_size_usdt":str(a2*e2)})
                    print(f"  #{ti} {sy}: EP->filled ({r2.status_code})");fx.append(f"#{ti} EP fix")
                else:print(f"  #{ti} {sy}: EPs wait, {len([o for o in ob.get(sy,[])if o['type']=='LIMIT'])} lim OK")
            ri=t.get("risk_usdt")
            if ri and sc!="tracking":tr+=float(ri)
        print(f"\n=== 5. RISK ===\n  ${ub:.2f} | Risk: ${tr:.2f} ({tr/ub*100:.2f}%) | Max: ${ub*0.03:.2f} | Rem: ${ub*0.03-tr:.2f} | {'EXCEEDED'if tr>ub*0.03 else'OK'}")
        for t in trades:
            ri=t.get("risk_usdt")
            if ri and t.get("source_channel")!="tracking":print(f"  #{t['id']} {t['symbol']}: {float(ri)/(ub*0.01):.2f}R")

        # === 6. TPs - Text embeds + GPT image reading ===
        print("\n=== 6. TPs ===");await asyncio.sleep(0.5)
        r=await c.get(f"https://discord.com/api/v10/channels/{TT}/messages",headers=dh,params={"limit":100});tm=r.json()if isinstance(r.json(),list)else[]
        for t in trades:
            mi=t.get("discord_message_id");fo=False
            has_tp = t.get("tp1") is not None

            # First check text embeds for TPs
            for m in tm:
                if m["id"]==mi:
                    for e in m.get("embeds",[]):
                        d=e.get("description","")
                        if "TP"in d:fo=True;print(f"  #{t['id']} {t['symbol']}: {d[:200]}")
                    break

            if fo or has_tp:
                if has_tp and not fo:
                    print(f"  #{t['id']} {t['symbol']}: TP1={t.get('tp1')} ({t.get('tp1_status')})")
                continue

            # No TPs found in text - try reading chart image with GPT-4o Mini
            if not mi or t.get("source_channel") == "tracking":
                print(f"  #{t['id']} {t['symbol']}: no TPs")
                continue

            # Find the message and check for chart image
            img_url = None
            for m in tm:
                if m["id"] == mi:
                    for e in m.get("embeds", []):
                        if e.get("image"):
                            img_url = e["image"]["url"]
                            break
                    break

            if not img_url:
                # Try fetching nearby messages - image is often in the message just BEFORE
                await asyncio.sleep(0.3)
                try:
                    r3 = await c.get(f"https://discord.com/api/v10/channels/{TT}/messages",
                        headers=dh, params={"around": mi, "limit": 5})
                    nearby = r3.json() if isinstance(r3.json(), list) else []
                    # Check all nearby messages for chart images (sorted by ID desc)
                    for m in nearby:
                        # Check the stored message itself AND the one just before it
                        for e in m.get("embeds", []):
                            if e.get("image"):
                                # Verify it's for the same symbol by checking embed description
                                desc = e.get("description", "")
                                sym_short = t["symbol"].replace("USDT", "")
                                if sym_short.upper() in desc.upper():
                                    img_url = e["image"]["url"]
                                    break
                        if img_url:
                            break
                except:
                    pass

            if img_url and OAK:
                print(f"  #{t['id']} {t['symbol']}: no text TPs, reading chart image...")
                result = await gpt_read_chart(c, img_url, t["symbol"], t["side"],
                    t.get("ep1", ""), t.get("sl", ""))
                if result and "take_profits" in result and result["take_profits"]:
                    tps = result["take_profits"]
                    print(f"    GPT-4o Mini found TPs: {tps} (cost: ${result.get('cost',0):.4f})")
                    # Save chart for Claude verification
                    print(f"    Chart saved: _chart_{t['symbol']}.png (verify with Claude)")
                    # Update Supabase with TPs
                    update = {}
                    for i, tp in enumerate(tps[:6], 1):
                        update[f"tp{i}"] = tp
                        update[f"tp{i}_status"] = "waiting"
                    r4 = await c.patch(f"{SU}/rest/v1/trades?id=eq.{t['id']}", headers=sh, json=update)
                    if r4.status_code == 200:
                        print(f"    Updated Supabase: {update}")
                        fx.append(f"#{t['id']} TPs added from chart: {tps}")
                    else:
                        print(f"    Supabase update failed: {r4.status_code}")
                elif result and "error" in result:
                    print(f"    GPT error: {result['error']}")
                else:
                    print(f"    GPT could not extract TPs")
            else:
                print(f"  #{t['id']} {t['symbol']}: no TPs")

        # === 7. REBALANCE — swap active/tracking based on proximity ===
        print("\n=== 7. REBALANCE ===")
        try:
            import sys;sys.path.insert(0,"c:/Users/quema/CryptoBot")
            import trade_executor as te
            actions = await te.rebalance_active_trades(use_futures=True)
            if actions:
                for a in actions:print(f"  {a}");fx.append(a)
            else:
                print("  No rebalance needed")
            # Close aiohttp sessions used by trade_executor
            import binance_client as bc2, supabase_client as sc2
            await bc2.close();await sc2.close()
        except Exception as e:
            print(f"  Rebalance error: {e}")

        if fx:print(f"\n=== FIXES ===");[print(f"  - {f}")for f in fx]
        else:print("\n=== ALL OK ===")
asyncio.run(main())
