"""Test du parser avec les messages reels captures de Discord."""
from parser import parse_message, parse_alert_message

print("=" * 60)
print("TEST 1: Parsing des signaux de trade (thread trades)")
print("=" * 60)

trade_tests = [
    ("WWG Woods range", "@WWG: Woods long eth 1864-1834 stop 1763"),
    ("WWG Tareeq single entry", "@WWG: -Tareeq long eth 1827 stop 1787"),
    ("WWG Muzzagin range+pnl", "@WWG: -Muzzagin long fartcoin 148-142 stop 1377"),
    ("WWG Woods short", "@WWG: Woods short eth 2030 stop 2113"),
    ("WWG JDrip pnl", "@WWG: -JDrip long eth 1990-1965 stop 1935 pnl: +11%"),
    ("WWG Eliz", "@WWG: Eliz long btc 68600-68000 stop 66200"),
    ("WWG Johnny short", "@WWG: Johnny short btc 72400-72900 stop 74355 pnl: +9%"),
    ("WWG Woods sol", "@WWG: Woods long sol 82.78-81.78 stop 78.36"),
    ("WWG Muzzagin btc", "@WWG: -Muzzagin long btc 67800-62700 stop 65900 pnl: +16%"),
    ("Pas un trade", "Hey guys, what do you think about ETH?"),
]

for name, msg in trade_tests:
    result = parse_message(msg, "WG Bot")
    if result:
        sl = f" sl={result.stop_loss}" if result.stop_loss else ""
        print(
            f"  OK {name}: {result.direction} {result.asset} "
            f"entry={result.entry_low}-{result.entry_high}{sl} "
            f"trader={result.trader}"
        )
    else:
        print(f"  -- {name}: (pas un signal)")

print()
print("=" * 60)
print("TEST 2: Parsing des alertes (thread active-alerts)")
print("=" * 60)

alert_tests = [
    (
        "Limit filled",
        ":Long: BTC \u2581\u2581\u2581trades: Limit order filled @WWG: Johnny",
    ),
    (
        "Updated entry",
        ":Long: BTC \u2581\u2581\u2581trades: Updated Average Entry to 68757 @WWG: -Muzzagin",
    ),
    (
        "Closed small profit",
        ":Long: BTC \u2581\u2581\u2581trades: Closed in small profit @WWG: -Muzzagin",
    ),
    (
        "Stops to BE",
        ":Long: BTC \u2581\u2581\u2581trades: Stops moved to BE @WWG: Johnny",
    ),
    (
        "Stopped BE",
        ":Long: BTC \u2581\u2581\u2581trades: Stopped BE @WWG: Johnny",
    ),
    (
        "Closed BE",
        ":Long: BTC \u2581\u2581\u2581trades: Closed BE @WWG: Eliz",
    ),
    (
        "Cancelled",
        ":Long: BTC \u2581\u2581\u2581trades: Limit order cancelled @WWG: Woods",
    ),
    (
        "Stopped out",
        ":Long: BTC \u2581\u2581\u2581trades: Stopped out @WWG: -Tareeq",
    ),
    (
        "Closed in profits",
        ":Long: BTC \u2581\u2581\u2581trades: Closed in profits @WWG: -Tareeq",
    ),
    (
        "Closed in loss",
        ":Long: SOL \u2581\u2581\u2581trades: Closed in loss @WWG: -JDrip",
    ),
    (
        "Spot stopped",
        ":Spot: ENA \u2581\u2581\u2581trades: Stopped out @WWG: -Bryce, @WWG: Eliz",
    ),
    (
        "Multi traders BE",
        ":Long: TAO \u2581\u2581\u2581trades: Stops moved to BE @WWG: -Tareeq, @WWG: -Dieta, @WWG: -Bryce",
    ),
    (
        "Multi message",
        ":Long: BTC \u2581\u2581\u2581trades: Closed in profits @WWG: -Tareeq\n"
        ":Long: HYPE \u2581\u2581\u2581trades: Closed in profits @WWG: -Tareeq\n"
        ":Long: ETH \u2581\u2581\u2581trades: Closed in profits @WWG: -Tareeq",
    ),
]

for name, msg in alert_tests:
    alerts = parse_alert_message(msg)
    if alerts:
        for a in alerts:
            print(
                f"  OK {name}: {a.direction} {a.asset} -> "
                f"status={a.new_status} reason={a.close_reason} "
                f"event={a.event_type} entry={a.new_entry} "
                f"traders={a.traders}"
            )
    else:
        print(f"  FAIL {name}: AUCUNE ALERTE PARSEE")

print()
print("=" * 60)
print("TEST 3: Test Supabase insert + update + delete")
print("=" * 60)

import asyncio
import supabase_client
import config

async def test_supabase():
    if not config.SUPABASE_URL:
        print("  SKIP: SUPABASE_URL non configure")
        return

    # Insert test trade
    test_row = {
        "symbol": "TESTUSDT",
        "side": "LONG",
        "status": "open",
        "trader": "TestBot",
        "source_channel": "test",
        "ep1": 100.0,
        "ep1_status": "waiting",
        "sl": 90.0,
        "sl_status": "waiting",
    }
    result = await supabase_client.insert_trade(test_row)
    if not result:
        print("  FAIL: Insert echoue")
        await supabase_client.close()
        return

    trade_id = result["id"]
    print(f"  OK Insert: trade #{trade_id} cree")

    # Find it
    found = await supabase_client.find_open_trade("TESTUSDT", "TestBot", "LONG")
    if found and found["id"] == trade_id:
        print(f"  OK Find: trade #{trade_id} retrouve")
    else:
        print(f"  FAIL: Find n'a pas retrouve le trade")

    # Update it
    update_result = await supabase_client.update_trade(trade_id, {
        "status": "closed",
        "close_reason": "profit",
        "ep1_status": "filled",
    })
    if update_result:
        print(f"  OK Update: trade #{trade_id} -> closed/profit")
    else:
        print("  FAIL: Update echoue")

    # Cleanup - delete test trade
    session = await supabase_client._get_session()
    url = f"{supabase_client._BASE_URL}/trades?id=eq.{trade_id}"
    async with session.delete(url) as resp:
        if resp.status == 200 or resp.status == 204:
            print(f"  OK Delete: trade #{trade_id} supprime (cleanup)")
        else:
            print(f"  WARN: Cleanup echoue (HTTP {resp.status}), supprime manuellement id={trade_id}")

    await supabase_client.close()
    print()
    print("Tous les tests Supabase OK!")

asyncio.run(test_supabase())
