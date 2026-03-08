import re
import logging
from dataclasses import dataclass, asdict
from typing import Optional, List

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    trader: Optional[str] = None
    direction: Optional[str] = None  # LONG / SHORT
    asset: Optional[str] = None
    order_type: Optional[str] = None  # MARKET / LIMIT
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_pct: Optional[float] = None
    pnl_pct: Optional[float] = None
    raw_signal: str = ""

    def to_dict(self):
        return asdict(self)

    def is_valid(self):
        """Un signal est valide s'il a au moins un asset et une direction."""
        return self.asset is not None and self.direction is not None


def parse_message(content: str, author_name: str = "") -> Optional[TradeSignal]:
    """Tente de parser un message Discord en signal de trade.

    Essaie chaque pattern dans l'ordre. Retourne None si aucun pattern ne matche.
    """
    if not content:
        return None

    for parser_fn in _PARSERS:
        try:
            result = parser_fn(content, author_name)
            if result and result.is_valid():
                result.raw_signal = content
                logger.info("Trade parse: %s %s %s", result.direction, result.asset, result.trader)
                return result
        except Exception as e:
            logger.debug("Parser %s echoue: %s", parser_fn.__name__, e)

    return None


def _parse_wwg_short_format(content: str, author_name: str) -> Optional[TradeSignal]:
    """Parse le format court WWG.

    Exemples:
        @WWG: Woods long eth 1864-1834 stop 1763
        @WWG: Eliz long wld 399-38 stop 357 pnl: +20%
    """
    pattern = r"@WWG:\s*(-?\w+)\s+(long|short)\s+(\w+)\s+([\d.]+)(?:[-–]([\d.]+))?\s+stop\s+([\d.]+)(?:\s+pnl:\s*([+-]?[\d.]+)%)?"
    match = re.search(pattern, content, re.IGNORECASE)
    if not match:
        return None

    trader, direction, asset, price1, price2, stop, pnl = match.groups()
    trader = trader.lstrip("-").strip()
    p1 = float(price1)
    p2 = float(price2) if price2 else p1

    return TradeSignal(
        trader=trader,
        direction=direction.upper(),
        asset=asset.upper(),
        order_type="LIMIT",
        entry_low=min(p1, p2),
        entry_high=max(p1, p2),
        stop_loss=float(stop),
        pnl_pct=float(pnl) if pnl else None,
    )


def _parse_structured_format(content: str, author_name: str) -> Optional[TradeSignal]:
    """Parse le format structure avec separateurs |.

    Exemples:
        :Long: LIMIT ETH | Entry: 1864 – 1834 | SL: 1763 (< 5.42%)
        :Short: MARKET BTC | Entry: 65000 | SL: 67000 (< 3.1%)
    """
    pattern = (
        r":(Long|Short):\s*(LIMIT|MARKET)\s+(\w+)"
        r"\s*\|\s*Entry:\s*([\d.,]+)\s*(?:[-–]\s*([\d.,]+))?"
        r"\s*\|\s*SL:\s*([\d.,]+)"
        r"(?:\s*\(<?\s*([\d.]+)%\))?"
    )
    match = re.search(pattern, content, re.IGNORECASE)
    if not match:
        return None

    direction, order_type, asset, entry1, entry2, sl, risk = match.groups()

    e1 = float(entry1.replace(",", ""))
    e2 = float(entry2.replace(",", "")) if entry2 else e1

    # Extraire le trader depuis @WWG: Nom en debut de message
    trader_match = re.search(r"@WWG:\s*(-?\w+)", content)
    trader = trader_match.group(1).lstrip("-").strip() if trader_match else None

    return TradeSignal(
        trader=trader,
        direction=direction.upper(),
        asset=asset.upper(),
        order_type=order_type.upper(),
        entry_low=min(e1, e2),
        entry_high=max(e1, e2),
        stop_loss=float(sl.replace(",", "")),
        risk_pct=float(risk) if risk else None,
    )


def _parse_simple_long_short(content: str, author_name: str) -> Optional[TradeSignal]:
    """Parse un format plus generique long/short.

    Exemples:
        long eth 1864-1834 stop 1763
        short btc 65000 stop 67000
    """
    pattern = r"\b(long|short)\s+(\w+)\s+([\d.,]+)(?:\s*[-–]\s*([\d.,]+))?\s+stop\s+([\d.,]+)"
    match = re.search(pattern, content, re.IGNORECASE)
    if not match:
        return None

    direction, asset, price1, price2, stop = match.groups()
    p1 = float(price1.replace(",", ""))
    p2 = float(price2.replace(",", "")) if price2 else p1

    return TradeSignal(
        trader=author_name or None,
        direction=direction.upper(),
        asset=asset.upper(),
        entry_low=min(p1, p2),
        entry_high=max(p1, p2),
        stop_loss=float(stop.replace(",", "")),
    )


def _parse_coinbase_format(content: str, author_name: str) -> Optional[TradeSignal]:
    """Parse le format Coinbase du channel #Coinbase.

    Exemples:
        🟢 [**BTC-USD**](<url>) Buy Executed - **$28.72K** at **$66063.4**
        🔴 [**ETH-USD**](<url>) Sell Executed - **$15.5K** at **$3421.2**
    """
    pattern = (
        r"\[\*\*(\w+)-USD\*\*\]"
        r".*?(Buy|Sell)\s+Executed"
        r"\s*-\s*\*\*\$([\d.,]+)K?\*\*"
        r"\s+at\s+\*\*\$([\d.,]+)\*\*"
    )
    match = re.search(pattern, content, re.IGNORECASE)
    if not match:
        return None

    asset, side, amount_str, price_str = match.groups()

    # Convertir le montant (peut avoir un K pour milliers)
    amount_str_clean = amount_str.replace(",", "")
    amount = float(amount_str_clean)
    if "K" in content[match.start():match.end()+5]:
        amount *= 1000

    price = float(price_str.replace(",", ""))
    direction = "LONG" if side.lower() == "buy" else "SHORT"

    return TradeSignal(
        trader="Coinbase",
        direction=direction,
        asset=asset.upper(),
        order_type="MARKET",
        entry_low=price,
        entry_high=price,
    )


def _parse_eth_limit_format(content: str, author_name: str) -> Optional[TradeSignal]:
    """Parse le format ETH/crypto limit du screenshot.

    Exemples:
        Eth limit 1864-1834 stop 1763
        Btc limit 65000-64500 stop 63000
    """
    pattern = r"(\w+)\s+limit\s+([\d.,]+)\s*[-–]\s*([\d.,]+)\s+stop\s+([\d.,]+)"
    match = re.search(pattern, content, re.IGNORECASE)
    if not match:
        return None

    asset, price1, price2, stop = match.groups()
    p1 = float(price1.replace(",", ""))
    p2 = float(price2.replace(",", ""))
    sl = float(stop.replace(",", ""))

    # Determiner la direction : si stop < entry => long, sinon short
    avg_entry = (p1 + p2) / 2
    direction = "LONG" if sl < avg_entry else "SHORT"

    # Extraire le trader depuis @WWG: Nom
    trader_match = re.search(r"@WWG:\s*(-?\w+)", content)
    trader = trader_match.group(1).lstrip("-").strip() if trader_match else author_name or None

    return TradeSignal(
        trader=trader,
        direction=direction,
        asset=asset.upper(),
        order_type="LIMIT",
        entry_low=min(p1, p2),
        entry_high=max(p1, p2),
        stop_loss=sl,
    )


def parse_embed(embed_data: dict, author_name: str = "") -> Optional[TradeSignal]:
    """Parse un embed Discord (les bots utilisent souvent des embeds)."""
    parts = []
    if embed_data.get("title"):
        parts.append(embed_data["title"])
    if embed_data.get("description"):
        parts.append(embed_data["description"])
    for field in embed_data.get("fields", []):
        parts.append(f"{field.get('name', '')}: {field.get('value', '')}")

    combined = " | ".join(parts)
    return parse_message(combined, author_name)


# Liste ordonnee des parsers (du plus specifique au plus generique)
_PARSERS = [
    _parse_wwg_short_format,
    _parse_structured_format,
    _parse_eth_limit_format,
    _parse_simple_long_short,
]

# Parsers secondaires (captures en local mais pas envoyees vers Supabase)
_PARSERS_LOCAL_ONLY = [
    _parse_coinbase_format,
]

# Channels a exclure de l'envoi vers Supabase
CHANNELS_EXCLUDED_FROM_SUPABASE = {"Coinbase"}


# --- Active Alerts parsing ---

# Map des actions vers (status, close_reason)
_ACTION_MAP = {
    "limit order filled": ("open", None, "ep_filled"),
    "stopped out": ("closed", "stopped_out", None),
    "stopped be": ("closed", "breakeven", None),
    "closed be": ("closed", "breakeven", None),
    "closed in profits": ("closed", "profit", None),
    "closed in small profit": ("closed", "small_profit", None),
    "closed in loss": ("closed", "loss", None),
    "limit order cancelled": ("cancelled", "cancelled", None),
    "stops moved to be": ("open", None, "sl_to_be"),
}


@dataclass
class TradeAlert:
    direction: Optional[str] = None  # LONG / SHORT / SPOT
    asset: Optional[str] = None
    action: Optional[str] = None  # raw action text
    traders: Optional[List[str]] = None
    # Derived from action
    new_status: Optional[str] = None  # open / closed / cancelled
    close_reason: Optional[str] = None
    event_type: Optional[str] = None  # ep_filled / sl_to_be / None
    # For "Updated Average Entry to X"
    new_entry: Optional[float] = None
    # Link to original trades message (for TP extraction)
    linked_message_id: Optional[str] = None
    linked_channel_id: Optional[str] = None

    def to_dict(self):
        return asdict(self)


@dataclass
class TPInfo:
    """Take profit levels parsed from WG Bot embeds."""
    prices: List[float] = None  # TP prices in order
    hit: List[bool] = None       # Whether each TP was hit (✓)

    def __post_init__(self):
        if self.prices is None:
            self.prices = []
        if self.hit is None:
            self.hit = []


def parse_embed_tps(embed: dict) -> Optional[TPInfo]:
    """Extract TP levels from a WG Bot embed description.

    Format: ... | **TPs:** ✓ 64607.6 | ✓ 65634.4 | 68100 | 70473.2
    ✓ = hit, no ✓ = not yet hit
    """
    desc = embed.get("description", "")
    if not desc:
        return None

    # Find TPs section
    tp_match = re.search(r"\*\*TPs?:\*\*\s*(.+)", desc)
    if not tp_match:
        return None

    tp_section = tp_match.group(1)
    prices = []
    hit = []

    # Split by | and parse each TP
    for part in tp_section.split("|"):
        part = part.strip()
        if not part:
            continue
        is_hit = "✓" in part or "✅" in part
        # Extract number
        num_match = re.search(r"([\d.]+)", part)
        if num_match:
            prices.append(float(num_match.group(1)))
            hit.append(is_hit)

    if prices:
        return TPInfo(prices=prices, hit=hit)
    return None


def extract_message_link(content: str) -> Optional[tuple]:
    """Extract Discord message link (guild_id, channel_id, message_id) from content."""
    match = re.search(
        r"https://discord\.com/channels/(\d+)/(\d+)/(\d+)",
        content,
    )
    if match:
        return match.group(1), match.group(2), match.group(3)
    return None


def parse_alert_message(content: str) -> List[TradeAlert]:
    """Parse un message du channel active-alerts.

    Chaque ligne du message peut contenir une alerte.
    Format: :<Side>: <ASSET> ...trades: <action> @WWG: <trader>[, @WWG: <trader2>]
    """
    if not content:
        return []

    alerts = []
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue

        alert = _parse_single_alert(line)
        if alert:
            alerts.append(alert)

    return alerts


def _parse_single_alert(line: str) -> Optional[TradeAlert]:
    """Parse une seule ligne d'alerte active."""
    # Pattern: :<Side>: <ASSET> ...trades: <action> @WWG: <trader>
    pattern = r":(Long|Short|Spot):\s*(\w+)\s.*?trades:\s*(.+?)(?:\s*@WWG:\s*(.+))$"
    match = re.search(pattern, line, re.IGNORECASE)
    if not match:
        return None

    direction, asset, action_raw, traders_raw = match.groups()
    action = action_raw.strip()
    action_lower = action.lower()

    # Parse traders (peut etre "trader1, @WWG: trader2, @WWG: trader3")
    traders = []
    if traders_raw:
        for t in re.split(r",\s*(?:@WWG:\s*)?", traders_raw):
            name = t.strip().lstrip("-").strip()
            if name:
                traders.append(name)

    # Determiner le status et close_reason
    new_status = None
    close_reason = None
    event_type = None
    new_entry = None

    for key, (status, reason, evt) in _ACTION_MAP.items():
        if action_lower.startswith(key):
            new_status = status
            close_reason = reason
            event_type = evt
            break

    # Cas special: Updated Average Entry to X
    entry_match = re.search(r"updated average entry to ([\d.]+)", action_lower)
    if entry_match:
        new_entry = float(entry_match.group(1))
        new_status = "open"
        event_type = "entry_updated"

    if not new_status and not new_entry:
        logger.warning("Action non reconnue dans active-alerts: %s", action)
        return None

    # Extract linked message ID from Discord URL in the line
    linked_msg_id = None
    linked_ch_id = None
    link = extract_message_link(line)
    if link:
        linked_ch_id = link[1]
        linked_msg_id = link[2]

    return TradeAlert(
        direction=direction.upper(),
        asset=asset.upper(),
        action=action,
        traders=traders,
        new_status=new_status,
        close_reason=close_reason,
        event_type=event_type,
        new_entry=new_entry,
        linked_message_id=linked_msg_id,
        linked_channel_id=linked_ch_id,
    )
