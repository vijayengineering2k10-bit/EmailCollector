import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(
    dotenv_path=Path(__file__).resolve().parent / ".env",
    override=True
)

APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()
APP_SECRET = os.getenv("APP_SECRET", "")

if len(APP_PASSWORD) < 12:
    raise RuntimeError("Set APP_PASSWORD in .env to at least 12 characters.")

if len(APP_SECRET) < 32:
    raise RuntimeError("Set APP_SECRET in .env to a random secret of at least 32 characters.")

if __name__ == "__main__":
    serve(app, host="127.0.0.1", port=8000, threads=8)