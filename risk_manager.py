"""Risk management module implementing 1R methodology from Wealth Group training.

Rules:
- 1R = RISK_PER_TRADE_PCT% of tradeable balance
- Tradeable balance = total USDT * (1 - USDT_RESERVE_PCT/100)
- Position size = risk_capital / |entry - stop_loss|
- Min R:R ratio check before entering
- Daily drawdown limit (stop trading if exceeded)
- Consecutive loss counter (reduce to 0.5R after N losses)
- Spot only (no shorts)
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import binance_client
import config
import supabase_client

logger = logging.getLogger(__name__)

# In-memory state for the current session
_daily_pnl_usdt: float = 0.0
_daily_date: str = ""
_consecutive_losses: int = 0


@dataclass
class RiskCheck:
    """Result of a risk assessment before trade entry."""
    allowed: bool
    reason: str = ""
    position_size_usdt: float = 0.0
    position_size_crypto: float = 0.0
    risk_capital: float = 0.0
    rr_ratio: float = 0.0
    r_multiplier: float = 1.0  # 1.0 normal, 0.5 after consecutive losses


async def assess_trade(
    entry_price: float,
    stop_loss: float,
    take_profit: Optional[float],
    direction: str,
) -> RiskCheck:
    """Evaluate whether a trade should be taken and calculate position size.

    Returns a RiskCheck with allowed=True and sizing info, or allowed=False with reason.
    """
    global _daily_pnl_usdt, _daily_date, _consecutive_losses

    # --- Spot only ---
    if direction == "SHORT":
        return RiskCheck(allowed=False, reason="SHORT not supported on spot")

    if not stop_loss or stop_loss <= 0:
        return RiskCheck(allowed=False, reason="No stop-loss provided")

    if not entry_price or entry_price <= 0:
        return RiskCheck(allowed=False, reason="No entry price provided")

    # --- Validate SL direction ---
    if direction == "LONG" and stop_loss >= entry_price:
        return RiskCheck(allowed=False, reason=f"SL ({stop_loss}) must be below entry ({entry_price}) for LONG")

    # --- R:R ratio check ---
    risk_per_unit = abs(entry_price - stop_loss)
    if risk_per_unit == 0:
        return RiskCheck(allowed=False, reason="Entry equals stop-loss")

    rr_ratio = 0.0
    if take_profit:
        reward_per_unit = abs(take_profit - entry_price)
        rr_ratio = reward_per_unit / risk_per_unit
        if rr_ratio < config.MIN_RR_RATIO:
            return RiskCheck(
                allowed=False,
                reason=f"R:R ratio {rr_ratio:.2f} below minimum {config.MIN_RR_RATIO}",
                rr_ratio=rr_ratio,
            )

    # --- Daily drawdown check ---
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _daily_date != today:
        _daily_date = today
        _daily_pnl_usdt = await _load_daily_pnl(today)

    total_balance = await binance_client.get_balance("USDT")
    usdc_balance = await binance_client.get_balance("USDC")
    total_account = total_balance + usdc_balance

    if total_account <= 0:
        return RiskCheck(allowed=False, reason="No USDT/USDC balance")

    max_daily_loss = total_account * (config.MAX_DAILY_DRAWDOWN_PCT / 100)
    if _daily_pnl_usdt <= -max_daily_loss:
        return RiskCheck(
            allowed=False,
            reason=f"Daily drawdown limit reached: {_daily_pnl_usdt:.2f} USDT (max -{max_daily_loss:.2f})",
        )

    # --- Tradeable balance (exclude reserve) ---
    tradeable = total_balance * (1 - config.USDT_RESERVE_PCT / 100)
    if tradeable <= 0:
        return RiskCheck(allowed=False, reason="No tradeable balance after reserve")

    # --- R multiplier (consecutive losses) ---
    r_multiplier = 1.0
    if _consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
        r_multiplier = 0.5
        logger.warning(
            "RISK: %d consecutive losses -> reducing to 0.5R", _consecutive_losses,
        )

    # --- Position sizing: 1R calculation ---
    risk_pct = config.RISK_PER_TRADE_PCT / 100 * r_multiplier
    risk_capital = tradeable * risk_pct  # Dollar amount we're willing to lose

    # risk_per_unit = |entry - SL| per 1 unit of crypto
    # position_size_crypto = risk_capital / risk_per_unit
    position_size_crypto = risk_capital / risk_per_unit
    position_size_usdt = position_size_crypto * entry_price

    # Sanity: don't exceed tradeable balance
    if position_size_usdt > tradeable:
        position_size_usdt = tradeable
        position_size_crypto = tradeable / entry_price
        risk_capital = position_size_crypto * risk_per_unit

    logger.info(
        "RISK: account=%.2f tradeable=%.2f 1R=%.2f (%.1f%% x%.1f) "
        "risk/unit=%.4f pos=%.4f crypto (%.2f USDT) R:R=%.2f consec_losses=%d",
        total_account, tradeable, risk_capital, config.RISK_PER_TRADE_PCT, r_multiplier,
        risk_per_unit, position_size_crypto, position_size_usdt,
        rr_ratio, _consecutive_losses,
    )

    return RiskCheck(
        allowed=True,
        position_size_usdt=position_size_usdt,
        position_size_crypto=position_size_crypto,
        risk_capital=risk_capital,
        rr_ratio=rr_ratio,
        r_multiplier=r_multiplier,
    )


def record_trade_result(pnl_usdt: float):
    """Record a trade close result for drawdown and streak tracking."""
    global _daily_pnl_usdt, _consecutive_losses

    _daily_pnl_usdt += pnl_usdt

    if pnl_usdt < 0:
        _consecutive_losses += 1
        logger.info("RISK: Loss recorded (%.2f USDT). Consecutive losses: %d", pnl_usdt, _consecutive_losses)
    else:
        if _consecutive_losses > 0:
            logger.info("RISK: Win recorded (%.2f USDT). Resetting consecutive losses from %d to 0", pnl_usdt, _consecutive_losses)
        _consecutive_losses = 0

    logger.info("RISK: Daily P&L: %.2f USDT", _daily_pnl_usdt)


async def _load_daily_pnl(today: str) -> float:
    """Load today's realized P&L from Supabase closed trades."""
    try:
        closed_today = await supabase_client.get_trades_closed_today(today)
        total = 0.0
        for t in closed_today:
            pnl = t.get("realized_pnl")
            if pnl is not None:
                total += float(pnl)
        return total
    except Exception as e:
        logger.warning("RISK: Could not load daily P&L: %s", e)
        return 0.0


def get_status() -> dict:
    """Return current risk manager state for diagnostics."""
    return {
        "daily_date": _daily_date,
        "daily_pnl_usdt": _daily_pnl_usdt,
        "consecutive_losses": _consecutive_losses,
        "risk_per_trade_pct": config.RISK_PER_TRADE_PCT,
        "max_daily_drawdown_pct": config.MAX_DAILY_DRAWDOWN_PCT,
        "min_rr_ratio": config.MIN_RR_RATIO,
        "usdt_reserve_pct": config.USDT_RESERVE_PCT,
    }
