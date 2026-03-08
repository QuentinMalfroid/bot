import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Discord
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_IDS = [
    int(gid.strip())
    for gid in os.getenv("GUILD_IDS", "").split(",")
    if gid.strip()
]

# Paths
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
IMAGES_DIR = Path(os.getenv("IMAGES_DIR", str(DATA_DIR / "images")))
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(DATA_DIR / "cryptobot.db")))

# Creer les dossiers si necessaire
DATA_DIR.mkdir(exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# Thread/Channel IDs for WWG
TRADES_THREAD_ID = int(os.getenv("TRADES_THREAD_ID", "1301066783909744680"))
ALERTS_THREAD_ID = int(os.getenv("ALERTS_THREAD_ID", "1301074706656530474"))
ACTIVE_FUTURES_ID = int(os.getenv("ACTIVE_FUTURES_ID", "1304468260040740894"))
ACTIVE_SPOT_ID = int(os.getenv("ACTIVE_SPOT_ID", "1306633648984031263"))
MESSAGE_LOG_ID = int(os.getenv("MESSAGE_LOG_ID", "1320531777625915474"))

# All WWG channel IDs we care about
WWG_CHANNEL_IDS = {
    TRADES_THREAD_ID,
    ALERTS_THREAD_ID,
    ACTIVE_FUTURES_ID,
    ACTIVE_SPOT_ID,
    MESSAGE_LOG_ID,
}

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

# Binance
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://demo-api.binance.com")
BINANCE_FUTURES_URL = os.getenv("BINANCE_FUTURES_URL", "https://testnet.binancefuture.com")

# Risk management
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "1.0"))   # 1R = 1% of account
MAX_DAILY_DRAWDOWN_PCT = float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", "3.0"))  # Stop trading after 3% daily loss
MIN_RR_RATIO = float(os.getenv("MIN_RR_RATIO", "1.5"))              # Minimum reward:risk ratio
USDT_RESERVE_PCT = float(os.getenv("USDT_RESERVE_PCT", "20.0"))     # Keep 20% USDT untouched
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))  # Reduce to 0.5R after N losses
FUTURES_LEVERAGE = int(os.getenv("FUTURES_LEVERAGE", "5"))            # WWG: 5x isolated for swing
FUTURES_MARGIN_TYPE = os.getenv("FUTURES_MARGIN_TYPE", "ISOLATED")    # Always isolated per WWG rules

# Order monitor
ORDER_POLL_INTERVAL = int(os.getenv("ORDER_POLL_INTERVAL", "30"))     # Check Binance orders every 30s

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
