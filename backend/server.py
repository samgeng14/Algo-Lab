"""Flask server: JSON API for the dashboard plus the auto-trading loop."""

import logging
import os
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

from .config import ROOT, load_config
from .engine import Engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

FRONTEND_DIR = os.path.join(ROOT, "frontend")


def create_app() -> Flask:
    cfg = load_config()
    engine = Engine(cfg)
    app = Flask(__name__, static_folder=None)

    @app.get("/")
    def index():
        return send_from_directory(FRONTEND_DIR, "index.html")

    @app.get("/<path:name>")
    def assets(name):
        return send_from_directory(FRONTEND_DIR, name)

    @app.get("/api/state")
    def state():
        store = engine.store
        positions = engine.positions_with_marks()
        positions_value = sum(p["mark_cents"] for p in positions)
        cash = store.cash_cents()
        settled = store.settled_positions()
        realized = sum(p["pnl_cents"] or 0 for p in settled)
        wins = sum(1 for p in settled if (p["pnl_cents"] or 0) > 0)
        return jsonify({
            "mode": engine.mode,
            "auto_enabled": engine.auto_enabled,
            "cycle_minutes": cfg.get("cycle_minutes", 0),
            "starting_cents": store.starting_cents(),
            "cash_cents": cash,
            "positions_value_cents": positions_value,
            "total_cents": cash + positions_value,
            "realized_pnl_cents": realized,
            "unrealized_pnl_cents": sum(p["unrealized_cents"] for p in positions),
            "settled_count": len(settled),
            "win_count": wins,
            "positions": positions,
            "settled": settled,
            "trades": store.trades(),
            "equity": store.equity_curve(),
            "last_cycle": {k: v for k, v in engine.last_cycle.items() if k != "rows"},
        })

    @app.get("/api/markets")
    def markets():
        return jsonify({
            "at": engine.last_cycle.get("at"),
            "rows": engine.last_cycle.get("rows", []),
        })

    @app.post("/api/cycle")
    def cycle():
        result = engine.run_cycle()
        return jsonify({"ok": True, "at": result["at"],
                        "fills": len(result["signals"]), "errors": result["errors"]})

    @app.post("/api/auto")
    def toggle_auto():
        engine.auto_enabled = bool(request.get_json(force=True).get("enabled"))
        return jsonify({"ok": True, "auto_enabled": engine.auto_enabled})

    @app.post("/api/reset")
    def reset():
        if engine.mode != "paper":
            return jsonify({"ok": False, "error": "reset is only allowed in paper mode"}), 400
        engine.store.reset(round(cfg["starting_bankroll"] * 100))
        engine.last_cycle = {"at": None, "rows": [], "signals": [], "errors": []}
        return jsonify({"ok": True})

    def auto_loop():
        interval = max(cfg.get("cycle_minutes", 10), 1) * 60
        while True:
            if engine.auto_enabled:
                try:
                    engine.run_cycle()
                except Exception:
                    log.exception("auto cycle failed")
            time.sleep(interval)

    if cfg.get("cycle_minutes", 0) > 0:
        threading.Thread(target=auto_loop, daemon=True, name="auto-trader").start()

    app.config["ALGO_LAB"] = {"cfg": cfg, "engine": engine}
    return app


def main():
    app = create_app()
    cfg = app.config["ALGO_LAB"]["cfg"]
    server = cfg.get("server", {})
    mode = cfg["mode"].upper()
    banner = f"Algo-Lab weather trader — {mode} MODE"
    if mode == "LIVE":
        banner += "  *** REAL MONEY ***"
    log.info(banner)
    app.run(host=server.get("host", "127.0.0.1"), port=server.get("port", 8000))


if __name__ == "__main__":
    main()
