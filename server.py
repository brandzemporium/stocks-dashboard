"""
Shariah-Compliant Portfolio Simulation — Railway Backend
═══════════════════════════════════════════════════════════
⚠ SIMULATION ONLY — NO REAL MONEY — NO REAL TRADES

Runs on Railway.app as a Flask server with background scheduler.
Exposes /api/state for the Vercel dashboard to consume.

Deploy:
  1. Push to GitHub
  2. Connect repo to Railway
  3. Set env vars: FINNHUB_API_KEY, ANTHROPIC_API_KEY (optional)
  4. Railway auto-detects Python + starts via Procfile

Endpoints:
  GET  /api/state    → Full portfolio state JSON for dashboard
  POST /api/refresh  → Trigger immediate price refresh
  GET  /health       → Health check
"""

import os
import json
import time
import logging
import threading
from datetime import datetime, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS

# Optional: Claude AI for decisions
try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

import requests as http_requests

# ─── LOGGING ──────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)-7s │ %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("halal-sim")

# ─── CONFIG ───────────────────────────────────────────────────────────────
CFG = {
    "balance": 17903.91,
    "account": "TFSA",
    "brokerage": "Wealthsimple Trade",
    "currency": "CAD",
    "refresh_min": 15,
    "fx_fee": 0.015,
    "max_pos_pct": 0.20,
    "min_cash_pct": 0.10,
    "trigger_pct": 3.0,
    "drawdown_halt_pct": 0.05,
    "market_open_h": 9, "market_open_m": 30,
    "market_close_h": 16, "market_close_m": 0,
}

# Pre-screened Shariah watchlist — only definitions, all prices fetched LIVE
WATCHLIST = [
    {"ticker": "NVDA",   "fh": "NVDA", "name": "NVIDIA Corp",       "exchange": "NASDAQ", "sector": "Technology", "hold": "1W-3M",    "ref": "S&P 500 Shariah", "tgt": 1.07, "sl": 0.94},
    {"ticker": "MSFT",   "fh": "MSFT", "name": "Microsoft Corp",    "exchange": "NASDAQ", "sector": "Technology", "hold": "3M+",       "ref": "S&P 500 Shariah", "tgt": 1.08, "sl": 0.93},
    {"ticker": "AAPL",   "fh": "AAPL", "name": "Apple Inc",         "exchange": "NASDAQ", "sector": "Technology", "hold": "3M+",       "ref": "S&P 500 Shariah", "tgt": 1.07, "sl": 0.94},
    {"ticker": "AMZN",   "fh": "AMZN", "name": "Amazon.com",        "exchange": "NASDAQ", "sector": "Technology", "hold": "3M+",       "ref": "S&P 500 Shariah", "tgt": 1.08, "sl": 0.92},
    {"ticker": "SHOP",   "fh": "SHOP", "name": "Shopify Inc",       "exchange": "NYSE",   "sector": "Technology", "hold": "Intraday",  "ref": "S&P 500 Shariah", "tgt": 1.06, "sl": 0.95},
    {"ticker": "ENB.TO", "fh": "ENB",  "name": "Enbridge Inc",      "exchange": "TSX",    "sector": "Energy",     "hold": "3M+",       "ref": "BMI Shariah",     "tgt": 1.08, "sl": 0.93},
    {"ticker": "SU.TO",  "fh": "SU",   "name": "Suncor Energy",     "exchange": "TSX",    "sector": "Energy",     "hold": "1W-3M",     "ref": "BMI Shariah",     "tgt": 1.09, "sl": 0.93},
    {"ticker": "CNQ.TO", "fh": "CNQ",  "name": "Canadian Natural",  "exchange": "TSX",    "sector": "Energy",     "hold": "1W-3M",     "ref": "S&P UNLISTED",    "tgt": 1.08, "sl": 0.93},
    {"ticker": "CSU.TO", "fh": "CSU",  "name": "Constellation SW",  "exchange": "TSX",    "sector": "Technology", "hold": "3M+",       "ref": "BMI Shariah",     "tgt": 1.07, "sl": 0.93},
    {"ticker": "TOU.TO", "fh": "TOU",  "name": "Tourmaline Oil",    "exchange": "TSX",    "sector": "Energy",     "hold": "1W-3M",     "ref": "S&P UNLISTED",    "tgt": 1.09, "sl": 0.92},
]

# ─── STATE (in-memory, backed up to disk) ─────────────────────────────────
STATE = {
    "phase": "idle",           # idle | premarket | open | postmarket
    "cash": CFG["balance"],
    "deployed": False,
    "watchlist": [],           # Watchlist with live prices
    "portfolio": [],           # Active positions (populated at market open)
    "trades": [],              # Trade log
    "news": [],                # Live news
    "triggers": [],            # Active triggers
    "pv_history": [],          # Portfolio value over time
    "last_update": None,
    "last_refresh": None,
    "error": None,
    "sim_label": "SIMULATION — NO REAL MONEY — NO REAL TRADES",
    "config": CFG,
}
STATE_LOCK = threading.Lock()
STATE_FILE = "state_backup.json"

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(STATE, f, default=str)
    except Exception as e:
        log.warning(f"State backup failed: {e}")

def load_state():
    global STATE
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
                STATE.update(saved)
                log.info("Restored state from backup")
        except Exception:
            pass

# ─── MARKET HOURS ─────────────────────────────────────────────────────────
def et_now():
    """Current time in ET (approximate — Railway runs in UTC)."""
    from datetime import timezone as tz
    utc = datetime.now(tz.utc)
    # ET is UTC-4 (EDT) or UTC-5 (EST). Use -4 for summer.
    et = utc - timedelta(hours=4)
    return et

def is_market_open():
    now = et_now()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    open_mins = CFG["market_open_h"] * 60 + CFG["market_open_m"]
    close_mins = CFG["market_close_h"] * 60 + CFG["market_close_m"]
    return open_mins <= mins < close_mins

def market_phase():
    now = et_now()
    if now.weekday() >= 5:
        return "weekend"
    mins = now.hour * 60 + now.minute
    if mins < CFG["market_open_h"] * 60 + CFG["market_open_m"]:
        return "premarket"
    if mins < CFG["market_close_h"] * 60 + CFG["market_close_m"]:
        return "open"
    return "postmarket"

def et_time_str():
    return et_now().strftime("%H:%M")

# ─── FINNHUB API ──────────────────────────────────────────────────────────
FH_KEY = os.getenv("FINNHUB_API_KEY", "")

def fh_get(endpoint, params):
    if not FH_KEY:
        log.warning("No FINNHUB_API_KEY set")
        return None
    params["token"] = FH_KEY
    try:
        r = http_requests.get(f"https://finnhub.io/api/v1/{endpoint}", params=params, timeout=10)
        if r.status_code == 429:
            log.warning(f"FH rate limit on /{endpoint}, waiting 3s")
            time.sleep(3)
            r = http_requests.get(f"https://finnhub.io/api/v1/{endpoint}", params=params, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        log.warning(f"FH /{endpoint}: {e}")
        return None

def fh_quote(symbol):
    d = fh_get("quote", {"symbol": symbol})
    if d and d.get("c", 0) > 0:
        return {"current": d["c"], "open": d["o"], "high": d["h"], "low": d["l"], "prevClose": d["pc"], "change": d["d"], "changePct": d["dp"]}
    return None

def fh_news():
    items = []
    gen = fh_get("news", {"category": "general"})
    if gen and isinstance(gen, list):
        for i in gen[:8]:
            items.append({
                "headline": i.get("headline", ""),
                "source": i.get("source", ""),
                "time": datetime.fromtimestamp(i.get("datetime", 0)).strftime("%H:%M"),
                "ticker": "MACRO",
                "sentiment": _sentiment(i.get("headline", "")),
            })
    return items

def fh_company_news(symbol, ticker_label):
    today = datetime.now().strftime("%Y-%m-%d")
    yest = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    cn = fh_get("company-news", {"symbol": symbol, "from": yest, "to": today})
    items = []
    if cn and isinstance(cn, list):
        for i in cn[:3]:
            items.append({
                "headline": i.get("headline", ""),
                "source": i.get("source", ""),
                "time": datetime.fromtimestamp(i.get("datetime", 0)).strftime("%H:%M"),
                "ticker": ticker_label,
                "sentiment": _sentiment(i.get("headline", "")),
            })
    return items

def _sentiment(h):
    l = h.lower()
    p = sum(1 for w in ["beat","surge","rally","gain","rise","jump","record","upgrade","growth","profit","dividend","boost","strong","expand","approval","wins"] if w in l)
    n = sum(1 for w in ["miss","drop","fall","crash","plunge","loss","decline","cut","downgrade","layoff","fine","lawsuit","warning","weak","slash","delay"] if w in l)
    return "positive" if p > n else "negative" if n > p else "neutral"

# ─── CORE LOGIC ───────────────────────────────────────────────────────────
def refresh_watchlist():
    """Fetch live prices for all watchlist candidates."""
    log.info("Refreshing watchlist quotes...")
    wl = []
    for stk in WATCHLIST:
        q = fh_quote(stk["fh"])
        time.sleep(0.25)
        wl.append({**stk, "lastPrice": q["current"] if q else None, "quote": q})
        if q:
            log.info(f"  {stk['ticker']}: ${q['current']:.2f}")
    with STATE_LOCK:
        STATE["watchlist"] = wl
    return wl

def refresh_news():
    """Fetch general + company news."""
    log.info("Refreshing news...")
    items = fh_news()
    # Company news for top 3 tickers
    source = STATE["portfolio"] if STATE["portfolio"] else STATE["watchlist"]
    for p in source[:3]:
        time.sleep(0.25)
        cn = fh_company_news(p["fh"], p["ticker"])
        items.extend(cn)
    items.sort(key=lambda x: x.get("time", "00:00"), reverse=True)
    with STATE_LOCK:
        STATE["news"] = items
    log.info(f"  {len(items)} news items")
    return items

def execute_buys():
    """Execute BUY orders at market open. BUY/SELL only — no TRIM/ADD/HOLD."""
    if STATE["deployed"]:
        return
    log.info("═══ EXECUTING BUYS AT MARKET OPEN ═══")
    max_deploy = CFG["balance"] * (1 - CFG["min_cash_pct"])
    cost = 0
    positions = []
    trades = []
    ts = et_time_str()

    for stk in WATCHLIST:
        q = fh_quote(stk["fh"])
        time.sleep(0.2)
        if not q or q["current"] <= 0:
            log.warning(f"  Skip {stk['ticker']}: no quote")
            continue

        price = q["current"]
        alloc = min(max_deploy / len(WATCHLIST), CFG["balance"] * CFG["max_pos_pct"])
        shares = max(1, int(alloc / price))
        if cost + shares * price > max_deploy:
            shares = max(1, int((max_deploy - cost) / price))
            if shares <= 0:
                continue
        cost += shares * price

        is_usd = ".TO" not in stk["ticker"]
        value = round(price * shares, 2)
        is_unlisted = "UNLISTED" in stk["ref"]

        pos = {
            **stk,
            "shares": shares,
            "entryPrice": price,
            "currentPrice": price,
            "target": round(price * stk["tgt"], 2),
            "stopLoss": round(price * stk["sl"], 2),
            "change": 0, "changePct": 0,
            "value": value, "pl": 0,
            "fxFee": round(value * CFG["fx_fee"], 2) if is_usd else 0,
            "shStatus": "COMPLIANT",
            "shLabel": f"S&P UNLISTED (manual)" if is_unlisted else f"S&P COMPLIANT [{stk['ref']}]",
        }
        positions.append(pos)

        trade = {
            "time": ts, "ticker": stk["ticker"], "action": "BUY",
            "qty": shares, "price": price, "hold": stk["hold"],
            "rationale": f"Market open entry at live price. {pos['shLabel']}.",
        }
        trades.append(trade)
        log.info(f"  BUY {shares}x {stk['ticker']} @ ${price:.2f} = ${shares*price:.2f}")

    with STATE_LOCK:
        STATE["portfolio"] = positions
        STATE["trades"] = trades
        STATE["cash"] = round(CFG["balance"] - cost, 2)
        STATE["deployed"] = True
        tv = sum(p["value"] for p in positions) + STATE["cash"]
        STATE["pv_history"].append({"time": ts, "value": round(tv, 2)})

    log.info(f"  Deployed: ${cost:.2f} | Cash: ${STATE['cash']:.2f} | Positions: {len(positions)}")
    save_state()

def refresh_prices():
    """Refresh live prices for all held positions."""
    if not STATE["portfolio"]:
        return
    log.info("Refreshing portfolio prices...")
    triggers = []

    for p in STATE["portfolio"]:
        q = fh_quote(p["fh"])
        time.sleep(0.18)
        if q and q["current"] > 0:
            price = q["current"]
            p["currentPrice"] = price
            p["change"] = round(price - p["entryPrice"], 2)
            p["changePct"] = round((p["change"] / p["entryPrice"]) * 100, 2)
            p["value"] = round(price * p["shares"], 2)
            p["pl"] = round(p["change"] * p["shares"], 2)
            is_usd = ".TO" not in p["ticker"]
            p["fxFee"] = round(p["value"] * CFG["fx_fee"], 2) if is_usd else 0
            log.info(f"  {p['ticker']}: ${price:.2f} ({p['changePct']:+.2f}%)")

            # Trigger detection
            if abs(p["changePct"]) >= CFG["trigger_pct"]:
                triggers.append({"type": "PRICE", "ticker": p["ticker"], "detail": f"{p['changePct']:+.2f}% move"})
            if p["currentPrice"] <= p["stopLoss"]:
                triggers.append({"type": "STOP", "ticker": p["ticker"], "detail": f"${p['currentPrice']:.2f} hit stop ${p['stopLoss']:.2f}"})
            if p["currentPrice"] >= p["target"]:
                triggers.append({"type": "TARGET", "ticker": p["ticker"], "detail": f"${p['currentPrice']:.2f} hit target ${p['target']:.2f}"})

    with STATE_LOCK:
        STATE["triggers"] = triggers
        tv = sum(p["value"] for p in STATE["portfolio"]) + STATE["cash"]
        STATE["pv_history"].append({"time": et_time_str(), "value": round(tv, 2)})
        if len(STATE["pv_history"]) > 100:
            STATE["pv_history"] = STATE["pv_history"][-100:]
        STATE["last_refresh"] = datetime.now().isoformat()

    if triggers:
        for t in triggers:
            log.warning(f"  ⚡ TRIGGER: [{t['type']}] {t['ticker']} — {t['detail']}")

    save_state()

# ─── SCHEDULER (background thread) ───────────────────────────────────────
def scheduler_loop():
    """Background loop: runs every 60s, triggers actions based on market phase."""
    log.info("Scheduler started")
    last_refresh = 0
    initialized = False

    while True:
        try:
            phase = market_phase()
            now_ts = time.time()

            with STATE_LOCK:
                STATE["phase"] = phase
                STATE["last_update"] = datetime.now().isoformat()

            # Initial watchlist + news fetch (once on startup)
            if not initialized:
                refresh_watchlist()
                refresh_news()
                initialized = True
                save_state()

            # Market open: execute buys if not yet deployed
            if phase == "open" and not STATE["deployed"]:
                execute_buys()
                refresh_news()

            # Periodic refresh during market hours
            if phase == "open" and STATE["deployed"]:
                if now_ts - last_refresh >= CFG["refresh_min"] * 60:
                    refresh_prices()
                    refresh_news()
                    last_refresh = now_ts

            # Pre-market: refresh watchlist prices + news every 30 min
            if phase == "premarket":
                if now_ts - last_refresh >= 1800:
                    refresh_watchlist()
                    refresh_news()
                    last_refresh = now_ts

        except Exception as e:
            log.error(f"Scheduler error: {e}", exc_info=True)
            with STATE_LOCK:
                STATE["error"] = str(e)

        time.sleep(60)  # Check every 60 seconds

# ─── FLASK APP ────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)  # Allow Vercel dashboard to call this

@app.route("/health")
def health():
    return jsonify({"status": "ok", "phase": STATE["phase"], "deployed": STATE["deployed"],
                    "positions": len(STATE["portfolio"]), "uptime": datetime.now().isoformat()})

@app.route("/api/state")
def get_state():
    """Full state for dashboard consumption."""
    with STATE_LOCK:
        # Compute derived metrics server-side
        pf = STATE["portfolio"]
        tv = sum(p.get("value", 0) for p in pf) + STATE["cash"]
        tpl = sum(p.get("pl", 0) for p in pf)

        return jsonify({
            **STATE,
            "derived": {
                "totalValue": round(tv, 2),
                "totalPL": round(tpl, 2),
                "plPct": round((tpl / CFG["balance"]) * 100, 2) if pf else 0,
                "cashPct": round((STATE["cash"] / tv) * 100, 1) if tv > 0 else 100,
                "positions": len(pf),
                "trades": len(STATE["trades"]),
                "shariahScore": 100 if not pf else round(sum(1 for p in pf if p.get("shStatus") == "COMPLIANT") / len(pf) * 100),
                "totalFx": round(sum(p.get("fxFee", 0) for p in pf), 2),
                "marketPhase": market_phase(),
                "etTime": et_time_str(),
            }
        })

@app.route("/api/refresh", methods=["POST"])
def manual_refresh():
    """Trigger immediate refresh."""
    if STATE["deployed"]:
        threading.Thread(target=refresh_prices, daemon=True).start()
        threading.Thread(target=refresh_news, daemon=True).start()
        return jsonify({"status": "refreshing"})
    else:
        threading.Thread(target=refresh_watchlist, daemon=True).start()
        threading.Thread(target=refresh_news, daemon=True).start()
        return jsonify({"status": "refreshing watchlist"})

@app.route("/")
def index():
    return jsonify({
        "name": "Halal Portfolio Sim — Backend",
        "status": "running",
        "phase": STATE["phase"],
        "sim_notice": "⚠ SIMULATION ONLY — NO REAL MONEY",
        "endpoints": {
            "GET /api/state": "Full portfolio state for dashboard",
            "POST /api/refresh": "Trigger manual refresh",
            "GET /health": "Health check",
        }
    })

# ─── STARTUP ──────────────────────────────────────────────────────────────
# Load saved state and start scheduler at module load (works with gunicorn)
load_state()

_scheduler_started = False
def ensure_scheduler():
    global _scheduler_started
    if not _scheduler_started:
        _scheduler_started = True
        t = threading.Thread(target=scheduler_loop, daemon=True)
        t.start()
        log.info("Background scheduler started")

ensure_scheduler()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info(f"Starting dev server on port {port}")
    log.info("⚠ SIMULATION ONLY — NO REAL MONEY — NO REAL TRADES")
    app.run(host="0.0.0.0", port=port, debug=False)
