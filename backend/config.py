"""Load and validate config.yaml."""

import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
DATA_DIR = os.path.join(ROOT, "data")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    cfg.setdefault("mode", "paper")
    if cfg["mode"] not in ("paper", "live"):
        raise ValueError(f"mode must be 'paper' or 'live', got {cfg['mode']!r}")

    kalshi = cfg.setdefault("kalshi", {})
    kalshi.setdefault("api_base", "https://api.elections.kalshi.com/trade-api/v2")
    kalshi["api_key_id"] = kalshi.get("api_key_id") or os.environ.get("KALSHI_API_KEY_ID", "")
    kalshi["private_key_path"] = (
        kalshi.get("private_key_path") or os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    )

    if cfg["mode"] == "live" and not (kalshi["api_key_id"] and kalshi["private_key_path"]):
        raise ValueError(
            "live mode requires kalshi.api_key_id and kalshi.private_key_path "
            "(or KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH env vars)"
        )

    os.makedirs(DATA_DIR, exist_ok=True)
    return cfg
