import hashlib
import hmac
import logging
import time
import urllib.parse
from typing import Optional

import aiohttp

import config

logger = logging.getLogger(__name__)

_session: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(headers={
            "X-MBX-APIKEY": config.BINANCE_API_KEY,
        })
    return _session


def _sign(params: dict) -> str:
    """Generate HMAC-SHA256 signature for Binance API."""
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(
        config.BINANCE_SECRET_KEY.encode(),
        qs.encode(),
        hashlib.sha256,
    ).hexdigest()
    return qs + f"&signature={sig}"


async def _request(method: str, path: str, params: dict, signed: bool = True) -> Optional[dict]:
    """Make a request to the Binance API."""
    session = await _get_session()
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        qs = _sign(params)
    else:
        qs = urllib.parse.urlencode(params)

    url = f"{config.BINANCE_BASE_URL}{path}?{qs}"

    try:
        async with session.request(method, url) as resp:
            data = await resp.json()
            if resp.status == 200 or resp.status == 201:
                return data
            else:
                logger.error(
                    "Binance API error %s %s (HTTP %d): %s",
                    method, path, resp.status, data,
                )
                return None
    except Exception as e:
        logger.error("Binance connection error: %s", e)
        return None


# --- Account ---

async def get_account() -> Optional[dict]:
    """Get account info including balances."""
    return await _request("GET", "/api/v3/account", {})


async def get_balance(asset: str) -> float:
    """Get free balance for a specific asset."""
    acct = await get_account()
    if not acct:
        return 0.0
    for b in acct.get("balances", []):
        if b["asset"] == asset:
            return float(b["free"])
    return 0.0


async def get_price(symbol: str) -> Optional[float]:
    """Get current price for a symbol."""
    data = await _request("GET", "/api/v3/ticker/price", {"symbol": symbol}, signed=False)
    if data and "price" in data:
        return float(data["price"])
    return None


# --- Symbol info ---

async def get_symbol_info(symbol: str) -> Optional[dict]:
    """Get exchange info for a symbol (filters, lot size, etc.)."""
    data = await _request("GET", "/api/v3/exchangeInfo", {"symbol": symbol}, signed=False)
    if data and "symbols" in data:
        for s in data["symbols"]:
            if s["symbol"] == symbol:
                return s
    return None


def _get_lot_size(symbol_info: dict) -> dict:
    """Extract LOT_SIZE filter (minQty, maxQty, stepSize)."""
    for f in symbol_info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            return {
                "min_qty": float(f["minQty"]),
                "max_qty": float(f["maxQty"]),
                "step_size": float(f["stepSize"]),
            }
    return {"min_qty": 0, "max_qty": 999999, "step_size": 0.00001}


def _get_price_filter(symbol_info: dict) -> dict:
    """Extract PRICE_FILTER (minPrice, maxPrice, tickSize)."""
    for f in symbol_info.get("filters", []):
        if f["filterType"] == "PRICE_FILTER":
            return {
                "min_price": float(f["minPrice"]),
                "max_price": float(f["maxPrice"]),
                "tick_size": float(f["tickSize"]),
            }
    return {"min_price": 0, "max_price": 999999, "tick_size": 0.01}


def _get_notional_filter(symbol_info: dict) -> dict:
    """Extract NOTIONAL filter (minNotional)."""
    for f in symbol_info.get("filters", []):
        if f["filterType"] == "NOTIONAL":
            return {"min_notional": float(f.get("minNotional", "10"))}
    return {"min_notional": 10.0}


def _round_step(value: float, step: float) -> float:
    """Round a value down to the nearest step."""
    if step <= 0:
        return value
    precision = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
    return round(int(value / step) * step, precision)


def _round_price(price: float, tick: float) -> float:
    """Round price to tick size."""
    if tick <= 0:
        return price
    precision = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
    return round(round(price / tick) * tick, precision)


# --- Orders ---

async def place_limit_order(
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    time_in_force: str = "GTC",
) -> Optional[dict]:
    """Place a LIMIT order."""
    symbol_info = await get_symbol_info(symbol)
    if not symbol_info:
        logger.error("Symbol info not found for %s", symbol)
        return None

    lot = _get_lot_size(symbol_info)
    pf = _get_price_filter(symbol_info)

    qty = _round_step(quantity, lot["step_size"])
    px = _round_price(price, pf["tick_size"])

    if qty < lot["min_qty"]:
        logger.error("Quantity %s below min %s for %s", qty, lot["min_qty"], symbol)
        return None

    params = {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "timeInForce": time_in_force,
        "quantity": f"{qty}",
        "price": f"{px}",
    }
    result = await _request("POST", "/api/v3/order", params)
    if result:
        logger.info(
            "BINANCE: LIMIT %s %s qty=%s price=%s -> orderId=%s status=%s",
            side, symbol, qty, px, result.get("orderId"), result.get("status"),
        )
    return result


async def place_market_order(
    symbol: str,
    side: str,
    quantity: Optional[float] = None,
    quote_qty: Optional[float] = None,
) -> Optional[dict]:
    """Place a MARKET order. Use quantity for base asset or quote_qty for quote asset."""
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
    }

    if quantity is not None:
        symbol_info = await get_symbol_info(symbol)
        if symbol_info:
            lot = _get_lot_size(symbol_info)
            quantity = _round_step(quantity, lot["step_size"])
            if quantity < lot["min_qty"]:
                logger.error("Quantity %s below min %s for %s", quantity, lot["min_qty"], symbol)
                return None
        params["quantity"] = f"{quantity}"
    elif quote_qty is not None:
        params["quoteOrderQty"] = f"{quote_qty}"
    else:
        logger.error("place_market_order needs quantity or quote_qty")
        return None

    result = await _request("POST", "/api/v3/order", params)
    if result:
        logger.info(
            "BINANCE: MARKET %s %s qty=%s -> orderId=%s status=%s filled=%s",
            side, symbol,
            quantity or f"~{quote_qty} USDT",
            result.get("orderId"), result.get("status"), result.get("executedQty"),
        )
    return result


async def place_stop_loss_order(
    symbol: str,
    side: str,
    quantity: float,
    stop_price: float,
    price: Optional[float] = None,
) -> Optional[dict]:
    """Place a STOP_LOSS_LIMIT order (for SL protection)."""
    symbol_info = await get_symbol_info(symbol)
    if not symbol_info:
        return None

    lot = _get_lot_size(symbol_info)
    pf = _get_price_filter(symbol_info)

    qty = _round_step(quantity, lot["step_size"])
    sp = _round_price(stop_price, pf["tick_size"])
    # Limit price slightly worse than stop to ensure fill
    if price is None:
        if side == "SELL":
            price = sp * 0.995  # 0.5% below stop for sells
        else:
            price = sp * 1.005  # 0.5% above stop for buys
    px = _round_price(price, pf["tick_size"])

    params = {
        "symbol": symbol,
        "side": side,
        "type": "STOP_LOSS_LIMIT",
        "timeInForce": "GTC",
        "quantity": f"{qty}",
        "stopPrice": f"{sp}",
        "price": f"{px}",
    }
    result = await _request("POST", "/api/v3/order", params)
    if result:
        logger.info(
            "BINANCE: SL %s %s qty=%s stop=%s -> orderId=%s",
            side, symbol, qty, sp, result.get("orderId"),
        )
    return result


async def cancel_order(symbol: str, order_id: int) -> Optional[dict]:
    """Cancel an order."""
    result = await _request("DELETE", "/api/v3/order", {
        "symbol": symbol,
        "orderId": order_id,
    })
    if result:
        logger.info("BINANCE: Cancelled order %s on %s", order_id, symbol)
    return result


async def get_order(symbol: str, order_id: int) -> Optional[dict]:
    """Get order status."""
    return await _request("GET", "/api/v3/order", {
        "symbol": symbol,
        "orderId": order_id,
    })


async def get_open_orders(symbol: Optional[str] = None) -> list:
    """Get all open orders, optionally for a symbol."""
    params = {}
    if symbol:
        params["symbol"] = symbol
    result = await _request("GET", "/api/v3/openOrders", params)
    return result if isinstance(result, list) else []


async def close():
    """Close the HTTP session."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None
