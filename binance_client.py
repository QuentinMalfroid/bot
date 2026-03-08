"""Binance API client supporting both Spot and Futures (demo/testnet).

Spot:   demo-api.binance.com  /api/v3/
Futures: testnet.binancefuture.com  /fapi/v1/
Same API keys work on both.
"""
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

# Base URLs
SPOT_BASE = config.BINANCE_BASE_URL  # https://demo-api.binance.com
FUTURES_BASE = config.BINANCE_FUTURES_URL  # https://testnet.binancefuture.com


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


async def _request(method: str, path: str, params: dict, signed: bool = True,
                   base_url: Optional[str] = None) -> Optional[dict]:
    """Make a request to the Binance API."""
    session = await _get_session()
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        qs = _sign(params)
    else:
        qs = urllib.parse.urlencode(params)

    url = f"{base_url or SPOT_BASE}{path}?{qs}"

    try:
        async with session.request(method, url) as resp:
            data = await resp.json()
            if resp.status in (200, 201):
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


# ============================================================
# SPOT
# ============================================================

async def get_account() -> Optional[dict]:
    """Get spot account info including balances."""
    return await _request("GET", "/api/v3/account", {})


async def get_balance(asset: str) -> float:
    """Get free spot balance for a specific asset."""
    acct = await get_account()
    if not acct:
        return 0.0
    for b in acct.get("balances", []):
        if b["asset"] == asset:
            return float(b["free"])
    return 0.0


async def get_price(symbol: str) -> Optional[float]:
    """Get current price for a symbol (spot)."""
    data = await _request("GET", "/api/v3/ticker/price", {"symbol": symbol}, signed=False)
    if data and "price" in data:
        return float(data["price"])
    return None


async def get_symbol_info(symbol: str) -> Optional[dict]:
    """Get spot exchange info for a symbol."""
    data = await _request("GET", "/api/v3/exchangeInfo", {"symbol": symbol}, signed=False)
    if data and "symbols" in data:
        for s in data["symbols"]:
            if s["symbol"] == symbol:
                return s
    return None


# ============================================================
# FUTURES
# ============================================================

async def futures_get_balance(asset: str = "USDT") -> float:
    """Get futures account balance for an asset."""
    data = await _request("GET", "/fapi/v2/balance", {}, base_url=FUTURES_BASE)
    if not data or not isinstance(data, list):
        return 0.0
    for b in data:
        if b["asset"] == asset:
            return float(b["availableBalance"])
    return 0.0


async def futures_get_price(symbol: str) -> Optional[float]:
    """Get current futures price."""
    data = await _request("GET", "/fapi/v1/ticker/price", {"symbol": symbol},
                          signed=False, base_url=FUTURES_BASE)
    if data and "price" in data:
        return float(data["price"])
    return None


async def futures_get_symbol_info(symbol: str) -> Optional[dict]:
    """Get futures exchange info for a symbol."""
    data = await _request("GET", "/fapi/v1/exchangeInfo", {},
                          signed=False, base_url=FUTURES_BASE)
    if data and "symbols" in data:
        for s in data["symbols"]:
            if s["symbol"] == symbol:
                return s
    return None


async def futures_place_limit_order(
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    time_in_force: str = "GTC",
) -> Optional[dict]:
    """Place a futures LIMIT order."""
    symbol_info = await futures_get_symbol_info(symbol)
    if not symbol_info:
        logger.error("Futures symbol info not found for %s", symbol)
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
    result = await _request("POST", "/fapi/v1/order", params, base_url=FUTURES_BASE)
    if result:
        logger.info(
            "BINANCE FUTURES: LIMIT %s %s qty=%s price=%s -> orderId=%s status=%s",
            side, symbol, qty, px, result.get("orderId"), result.get("status"),
        )
    return result


async def futures_place_market_order(
    symbol: str,
    side: str,
    quantity: float,
) -> Optional[dict]:
    """Place a futures MARKET order."""
    symbol_info = await futures_get_symbol_info(symbol)
    if symbol_info:
        lot = _get_lot_size(symbol_info)
        quantity = _round_step(quantity, lot["step_size"])
        if quantity < lot["min_qty"]:
            logger.error("Quantity %s below min %s for %s", quantity, lot["min_qty"], symbol)
            return None

    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": f"{quantity}",
    }
    result = await _request("POST", "/fapi/v1/order", params, base_url=FUTURES_BASE)
    if result:
        logger.info(
            "BINANCE FUTURES: MARKET %s %s qty=%s -> orderId=%s status=%s",
            side, symbol, quantity, result.get("orderId"), result.get("status"),
        )
    return result


async def futures_place_stop_loss_order(
    symbol: str,
    side: str,
    quantity: float,
    stop_price: float,
) -> Optional[dict]:
    """Place a futures STOP_MARKET order (SL protection)."""
    symbol_info = await futures_get_symbol_info(symbol)
    if not symbol_info:
        return None

    lot = _get_lot_size(symbol_info)
    pf = _get_price_filter(symbol_info)

    qty = _round_step(quantity, lot["step_size"])
    sp = _round_price(stop_price, pf["tick_size"])

    params = {
        "symbol": symbol,
        "side": side,
        "type": "STOP_MARKET",
        "stopPrice": f"{sp}",
        "quantity": f"{qty}",
        "closePosition": "false",
    }
    result = await _request("POST", "/fapi/v1/order", params, base_url=FUTURES_BASE)
    if result:
        logger.info(
            "BINANCE FUTURES: SL %s %s qty=%s stop=%s -> orderId=%s",
            side, symbol, qty, sp, result.get("orderId"),
        )
    return result


async def futures_cancel_order(symbol: str, order_id: int) -> Optional[dict]:
    """Cancel a futures order."""
    result = await _request("DELETE", "/fapi/v1/order", {
        "symbol": symbol,
        "orderId": order_id,
    }, base_url=FUTURES_BASE)
    if result:
        logger.info("BINANCE FUTURES: Cancelled order %s on %s", order_id, symbol)
    return result


async def futures_get_position(symbol: str) -> Optional[dict]:
    """Get current futures position for a symbol."""
    data = await _request("GET", "/fapi/v2/positionRisk", {"symbol": symbol},
                          base_url=FUTURES_BASE)
    if data and isinstance(data, list):
        for p in data:
            if p["symbol"] == symbol and float(p.get("positionAmt", 0)) != 0:
                return p
    return None


async def futures_get_open_orders(symbol: Optional[str] = None) -> list:
    """Get all open futures orders."""
    params = {}
    if symbol:
        params["symbol"] = symbol
    result = await _request("GET", "/fapi/v1/openOrders", params, base_url=FUTURES_BASE)
    return result if isinstance(result, list) else []


# ============================================================
# SPOT orders (unchanged)
# ============================================================

async def place_limit_order(
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    time_in_force: str = "GTC",
) -> Optional[dict]:
    """Place a spot LIMIT order."""
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
    """Place a spot MARKET order."""
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
    """Place a spot STOP_LOSS_LIMIT order."""
    symbol_info = await get_symbol_info(symbol)
    if not symbol_info:
        return None

    lot = _get_lot_size(symbol_info)
    pf = _get_price_filter(symbol_info)

    qty = _round_step(quantity, lot["step_size"])
    sp = _round_price(stop_price, pf["tick_size"])
    if price is None:
        if side == "SELL":
            price = sp * 0.995
        else:
            price = sp * 1.005
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
    """Cancel a spot order."""
    result = await _request("DELETE", "/api/v3/order", {
        "symbol": symbol,
        "orderId": order_id,
    })
    if result:
        logger.info("BINANCE: Cancelled order %s on %s", order_id, symbol)
    return result


async def get_order(symbol: str, order_id: int) -> Optional[dict]:
    """Get spot order status."""
    return await _request("GET", "/api/v3/order", {
        "symbol": symbol,
        "orderId": order_id,
    })


async def get_open_orders(symbol: Optional[str] = None) -> list:
    """Get all open spot orders."""
    params = {}
    if symbol:
        params["symbol"] = symbol
    result = await _request("GET", "/api/v3/openOrders", params)
    return result if isinstance(result, list) else []


# ============================================================
# Helpers
# ============================================================

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


async def close():
    """Close the HTTP session."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None
