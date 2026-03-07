"""Test du parser avec les messages reels captures de Discord."""
from parser import parse_message

tests = [
    (
        "Coinbase Buy",
        '\U0001f7e2 [**BTC-USD**](<https://www.coinbase.com/advanced-trade/spot/BTC-USD>) Buy Executed - **$28.72K** at **$66063.4**',
    ),
    (
        "Coinbase Large",
        '> ### \u26a0\ufe0f Large Trade \u26a0\ufe0f\n> \U0001f7e2 [**BTC-USD**](<https://www.coinbase.com/advanced-trade/spot/BTC-USD>) Buy Executed - **$104.95K** at **$66070**',
    ),
    (
        "WWG short",
        "@WWG: Woods long eth 1864-1834 stop 1763",
    ),
    (
        "WWG Eliz",
        "@WWG: Eliz long wld 399-38 stop 357 pnl: +20%",
    ),
    (
        "Structured",
        ":Long: LIMIT ETH | Entry: 1864 \u2013 1834 | SL: 1763 (< 5.42%)",
    ),
    (
        "Eth limit",
        "Eth limit 1864-1834 stop 1763",
    ),
    (
        "No trade",
        "I'm in a similar one :ChromaHandshake:",
    ),
    (
        "Message log",
        "## Message Update Detected\n\U0001f4e2 **Server:** some server",
    ),
]

for name, msg in tests:
    result = parse_message(msg, "TestUser")
    if result:
        sl = f" sl={result.stop_loss}" if result.stop_loss else ""
        print(
            f"\u2705 {name}: {result.direction} {result.asset} "
            f"entry={result.entry_low}-{result.entry_high}{sl} "
            f"trader={result.trader}"
        )
    else:
        print(f"\u2b1c {name}: (pas un signal de trade)")
