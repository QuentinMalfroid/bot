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

# Thread IDs for WWG channels
TRADES_THREAD_ID = int(os.getenv("TRADES_THREAD_ID", "1301066783909744680"))
ALERTS_THREAD_ID = int(os.getenv("ALERTS_THREAD_ID", "1301074706656530474"))

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

# Binance
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://demo-api.binance.com")

# Risk management
TRADE_SIZE_USDT = float(os.getenv("TRADE_SIZE_USDT", "100"))  # USDT per trade

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
