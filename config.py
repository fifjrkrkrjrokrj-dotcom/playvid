import os
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

# Telegram API credentials
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

# Bot credentials
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")

# Assistant credentials
SESSION_STRING = os.getenv("SESSION_STRING")

# Database URI
MONGO_URI = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017/vplay_bot"

# Server port (Railway dynamically assigns PORT)
PORT = int(os.getenv("PORT", "27999"))

# Validation
def validate_config():
    missing = []
    if not API_ID:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not SESSION_STRING:
        missing.append("SESSION_STRING")
    if not MONGO_URI:
        missing.append("MONGO_URI")
        
    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}. "
            f"Please check your .env file or environment setup."
        )

    try:
        # API_ID must be an integer
        int(API_ID)
    except (TypeError, ValueError):
        raise ValueError("API_ID must be a valid integer.")
