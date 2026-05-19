# ◈ Halal Portfolio Sim — Deployment Guide

> ⚠ **SIMULATION ONLY** — No real money, no real trades, educational purposes only.

## Architecture

```
┌──────────────────┐     polls /api/state     ┌──────────────────┐
│   Vercel          │ ◄───── every 30s ────── │   Your Browser    │
│   (static HTML)   │                          │                   │
└──────────────────┘                          └──────────────────┘
         │
         │ fetches from
         ▼
┌──────────────────┐     fetches prices/news   ┌──────────────────┐
│   Railway         │ ──── every 15 min ─────► │   Finnhub API     │
│   (Python backend)│                           │   (free tier)     │
│   - Flask server  │                           └──────────────────┘
│   - BG scheduler  │
│   - State mgmt    │
└──────────────────┘
```

- **Railway** runs the Python backend 24/7. It fetches live prices from Finnhub, detects triggers, executes simulated buys at market open, and exposes a `/api/state` endpoint.
- **Vercel** serves the static HTML dashboard. It polls Railway every 30 seconds and renders the portfolio.
- You can close the browser anytime. Railway keeps running.

---

## Step 1: Deploy Backend to Railway

1. Create a GitHub repo and push the `railway-backend/` folder contents:
   ```
   server.py
   requirements.txt
   Procfile
   runtime.txt
   ```

2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub

3. Select your repo. Railway auto-detects Python.

4. Set environment variables in Railway dashboard:
   ```
   FINNHUB_API_KEY=your_finnhub_api_key
   ```
   Optional (for Claude AI decisions):
   ```
   ANTHROPIC_API_KEY=your_anthropic_key
   ```

5. Railway will build and deploy. Note your app URL:
   ```
   https://your-app-name.railway.app
   ```

6. Verify: visit `https://your-app-name.railway.app/health` — should return JSON.

### Railway Free Tier
- 500 hours/month execution (market hours only = ~8.5h/day × 22 days = ~187h ✓)
- 512 MB RAM (plenty for this app)
- The scheduler sleeps outside market hours, consuming minimal resources

---

## Step 2: Deploy Dashboard to Vercel

1. Create another GitHub repo (or folder) with the `vercel-dashboard/` contents:
   ```
   index.html
   vercel.json
   ```

2. Go to [vercel.com](https://vercel.com) → Import Git Repository

3. Select your repo. Vercel auto-detects static site.

4. Deploy. Note your dashboard URL:
   ```
   https://your-dashboard.vercel.app
   ```

5. Open the dashboard URL in your browser. Enter your Railway backend URL when prompted.

---

## Step 3: Use It

1. Open your Vercel dashboard URL
2. Enter your Railway backend URL (e.g., `https://your-app.railway.app`)
3. The dashboard connects and shows:
   - **Pre-market / Weekend**: Watchlist with last-known prices, news, countdown to open
   - **Market open (9:30 AM ET)**: Backend auto-buys at live prices, Holdings tab populates
   - **Intraday**: Prices refresh every 15 min, triggers fire on ±3% moves
   - **Post-market**: Positions locked, awaiting next trading day

### Actions: BUY or SELL Only
This simulation uses Wealthsimple TFSA constraints:
- No shorting, no margin, no options
- Only BUY (enter position) and SELL (exit position)
- Hold periods are duration labels, not trade direction

---

## API Endpoints (Railway)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check + status |
| `/api/state` | GET | Full portfolio state for dashboard |
| `/api/refresh` | POST | Trigger immediate price refresh |
| `/` | GET | API info page |

---

## Configuration

Edit `CFG` in `server.py` to change:
- `balance`: Starting simulation capital (default: $17,903.91 CAD)
- `refresh_min`: Price refresh interval (default: 15 minutes)
- `trigger_pct`: Price movement trigger threshold (default: 3%)
- `max_pos_pct`: Maximum single position size (default: 20%)
- `min_cash_pct`: Minimum cash reserve (default: 10%)

## Watchlist

Edit the `WATCHLIST` array in `server.py` to change which tickers the system monitors and buys. Each entry needs:
- `ticker`: Display symbol (e.g., `ENB.TO`)
- `fh`: Finnhub symbol (e.g., `ENB` — no .TO suffix)
- `ref`: Shariah reference index

---

## Costs

| Service | Cost |
|---------|------|
| Railway (free tier) | $0/month |
| Vercel (free tier) | $0/month |
| Finnhub (free tier) | $0/month |
| **Total** | **$0/month** |
