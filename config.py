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

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
