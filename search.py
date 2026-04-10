import requests
import os
import datetime
import base64
import time
import csv
import json
from dotenv import load_dotenv
from urllib.parse import urlparse
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

load_dotenv()

KEY_ID = os.getenv("KALSHI_KEY_ID")
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# ── SETTINGS ──────────────────────────────────────────────
BET_AMOUNT_CENTS = 100
MIN_EDGE = 10
DAILY_LOSS_LIMIT = 1000
SCAN_INTERVAL = 30
DRY_RUN = True
DEMO_BALANCE = 10000
# ──────────────────────────────────────────────────────────

MARKETS = {
    "KXBTC":  {"symbol": "BTC",  "source": "binance", "pair": "BTCUSDT"},
    "KXETH":  {"symbol": "ETH",  "source": "binance", "pair": "ETHUSDT"},
    "KXDOGE": {"symbol": "DOGE", "source": "binance", "pair": "DOGEUSDT"},
    "KXXRP":  {"symbol": "XRP",  "source": "binance", "pair": "XRPUSDT"},
    "KXWTI":  {"symbol": "OIL",  "source": "oil"},
}

demo_balance = DEMO_BALANCE
daily_pnl = 0
bets_placed = 0
start_of_day = datetime.date.today()
DEMO_FILE = "demo_trades.csv"
PENDING_FILE = "pending_bets.json"

def load_private_key():
    with open("private_key.pem", "rb") as f:
        return serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )

def sign_request(private_key, timestamp, method, path):
    path_without_query = path.split('?')[0]
    message = f"{timestamp}{method}{path_without_query}".encode('utf-8')
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode('utf-8')

def get_headers(method, path):
    timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
    private_key = load_private_key()
    sign_path = urlparse(BASE_URL + path).path
    signature = sign_request(private_key, timestamp, method, sign_path)
    return {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json"
    }

def get_all_prices():
    prices = {}
    try:
        pairs = ["BTCUSDT", "ETHUSDT", "DOGEUSDT", "XRPUSDT"]
        for pair in pairs:
            r = requests.get(f"https://api.binance.us/api/v3/ticker/price?symbol={pair}", timeout=5).json()
            prices[pair] = float(r["price"])
    except Exception as e:
        print(f"  ⚠️ Binance error: {e}")

    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/CL=F", timeout=5).json()
        oil_price = r["chart"]["result"][0]["meta"]["regularMarketPrice"]
        prices["OIL"] = float(oil_price)
    except Exception as e:
        print(f"  ⚠️ Oil price error: {e}")

    return prices

def get_kalshi_markets(series):
    path = f"/markets?limit=100&status=open&series_ticker={series}"
    headers = get_headers("GET", path)
    response = requests.get(BASE_URL + path, headers=headers)
    return response.json().get("markets", [])

def get_market_result(ticker):
    path = f"/markets/{ticker}"
    headers = get_headers("GET", path)
    response = requests.get(BASE_URL + path, headers=headers)
    data = response.json().get("market", {})
    return data.get("status", ""), data.get("result", "")

def load_pending_bets():
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, "r") as f:
            return json.load(f)
    return []

def save_pending_bets(bets):
    with open(PENDING_FILE, "w") as f:
        json.dump(bets, f, indent=2)

def log_trade(trade):
    file_exists = os.path.exists(DEMO_FILE)
    with open(DEMO_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "time", "ticker", "symbol", "bet", "ask",
            "edge", "threshold", "price_at_bet",
            "result", "pnl_cents", "balance_cents"
        ])
        if not file_exists:
            writer.writeheader()
        writer.writerow(trade)

def check_pending_bets():
    global demo_balance, daily_pnl
    pending = load_pending_bets()
    still_pending = []
    for bet in pending:
        status, result = get_market_result(bet["ticker"])
        if status == "finalized" and result:
            won = (bet["bet"] == "YES" and result == "yes") or \
                  (bet["bet"] == "NO" and result == "no")
            pnl = (100 - bet["ask"]) if won else -bet["ask"]
            demo_balance += pnl
            daily_pnl += pnl
            outcome = "✅ WON" if won else "❌ LOST"
            print(f"  {outcome} | {bet['ticker']} | {'+' if pnl > 0 else ''}{pnl}c | Balance: ${demo_balance/100:.2f}")
            log_trade({
                "time": bet["time"],
                "ticker": bet["ticker"],
                "symbol": bet["symbol"],
                "bet": bet["bet"],
                "ask": bet["ask"],
                "edge": bet["edge"],
                "threshold": bet["threshold"],
                "price_at_bet": bet["price_at_bet"],
                "result": "WIN" if won else "LOSS",
                "pnl_cents": pnl,
                "balance_cents": demo_balance
            })
        else:
            still_pending.append(bet)
    save_pending_bets(still_pending)
    return len(pending) - len(still_pending)

def find_opportunities(markets, current_price, symbol, series):
    opportunities = []
    for m in markets:
        ticker = m.get("ticker", "")
        yes_ask = m.get("yes_ask")
        no_ask = m.get("no_ask")
        if yes_ask is None or no_ask is None:
            continue
        try:
            parts = ticker.split("-")
            last = parts[-1]
            if last.startswith("T"):
                direction = "ABOVE"
                threshold = float(last[1:])
            elif last.startswith("B"):
                direction = "BELOW"
                threshold = float(last[1:])
            else:
                continue
        except:
            continue

        price_diff_pct = abs(current_price - threshold) / threshold * 100

        if direction == "ABOVE":
            if current_price > threshold:
                fair_value = min(95, 50 + price_diff_pct * 10)
                edge = fair_value - yes_ask
                bet = "YES"
                ask = yes_ask
            else:
                fair_value = min(95, 50 + price_diff_pct * 10)
                edge = fair_value - no_ask
                bet = "NO"
                ask = no_ask
        else:
            if current_price < threshold:
                fair_value = min(95, 50 + price_diff_pct * 10)
                edge = fair_value - yes_ask
                bet = "YES"
                ask = yes_ask
            else:
                fair_value = min(95, 50 + price_diff_pct * 10)
                edge = fair_value - no_ask
                bet = "NO"
                ask = no_ask

        if edge >= MIN_EDGE:
            opportunities.append({
                "symbol": symbol, "ticker": ticker,
                "direction": direction, "threshold": threshold,
                "current_price": current_price, "bet": bet,
                "ask": ask, "fair_value": round(fair_value, 1),
                "edge": round(edge, 1)
            })

    return sorted(opportunities, key=lambda x: x["edge"], reverse=True)

def place_demo_bet(opp):
    global demo_balance, bets_placed
    if demo_balance < BET_AMOUNT_CENTS:
        print("  ⚠️ Demo balance too low!")
        return False
    demo_balance -= opp["ask"]
    bets_placed += 1
    pending = load_pending_bets()
    pending.append({
        "time": datetime.datetime.now().isoformat(),
        "ticker": opp["ticker"],
        "symbol": opp["symbol"],
        "bet": opp["bet"],
        "ask": opp["ask"],
        "edge": opp["edge"],
        "threshold": opp["threshold"],
        "price_at_bet": opp["current_price"]
    })
    save_pending_bets(pending)
    print(f"  📝 DEMO BET: {opp['symbol']} {opp['bet']} {opp['ticker']}")
    print(f"     Edge: +{opp['edge']}c | Cost: {opp['ask']}c | Balance: ${demo_balance/100:.2f}")
    return True

def reset_daily_if_needed():
    global daily_pnl, bets_placed, start_of_day
    if datetime.date.today() > start_of_day:
        print("New day - resetting daily P&L tracker")
        daily_pnl = 0
        bets_placed = 0
        start_of_day = datetime.date.today()

def print_summary():
    pending = load_pending_bets()
    print(f"\n  📊 DEMO SUMMARY")
    print(f"     Balance: ${demo_balance/100:.2f} (started $100.00)")
    print(f"     Total bets placed: {bets_placed}")
    print(f"     Pending bets: {len(pending)}")
    print(f"     Daily P&L: ${daily_pnl/100:+.2f}")
    if os.path.exists(DEMO_FILE):
        print(f"     Full trade log: demo_trades.csv")

def run_bot():
    global daily_pnl
    print("🤖 Kalshi Multi-Market Arbitrage Bot (DEMO MODE)")
    print(f"   Markets: BTC, ETH, DOGE, XRP, OIL")
    print(f"   Starting balance: ${demo_balance/100:.2f}")
    print(f"   Bet size: ${BET_AMOUNT_CENTS/100:.2f}")
    print(f"   Min edge: {MIN_EDGE}c")
    print(f"   Daily loss limit: ${DAILY_LOSS_LIMIT/100:.2f}")
    print(f"   Scanning every {SCAN_INTERVAL} seconds\n")

    scan_count = 0

    while True:
        try:
            reset_daily_if_needed()

            if daily_pnl <= -DAILY_LOSS_LIMIT:
                print(f"⛔ Daily loss limit hit. Stopping for today.")
                print_summary()
                break

            scan_count += 1
            now = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"[{now}] Scan #{scan_count} | Balance: ${demo_balance/100:.2f} | Bets: {bets_placed}")

            resolved = check_pending_bets()
            if resolved > 0:
                print(f"  Resolved {resolved} pending bet(s)")

            prices = get_all_prices()
            price_display = []
            for series, info in MARKETS.items():
                if info["source"] == "binance":
                    p = prices.get(info["pair"])
                elif info["source"] == "oil":
                    p = prices.get("OIL")
                else:
                    p = None
                if p:
                    price_display.append(f"{info['symbol']}: ${p:,.4f}" if p < 10 else f"{info['symbol']}: ${p:,.2f}")
            print(f"  {' | '.join(price_display)}")

            all_opps = []
            for series, info in MARKETS.items():
                if info["source"] == "binance":
                    current_price = prices.get(info["pair"])
                elif info["source"] == "oil":
                    current_price = prices.get("OIL")
                else:
                    current_price = None

                if not current_price:
                    continue

                markets = get_kalshi_markets(series)
                opps = find_opportunities(markets, current_price, info["symbol"], series)
                all_opps.extend(opps)

            all_opps = sorted(all_opps, key=lambda x: x["edge"], reverse=True)

            if all_opps:
                print(f"  🎯 {len(all_opps)} opportunities found!")
                for opp in all_opps[:3]:
                    print(f"  → {opp['symbol']} {opp['bet']} {opp['ticker']} | Edge: +{opp['edge']}c")
                    place_demo_bet(opp)
            else:
                print(f"  No opportunities found")

            print(f"  Sleeping {SCAN_INTERVAL}s...\n")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            print("\n\nBot stopped by user.")
            print_summary()
            break
        except Exception as e:
            print(f"  ⚠️ Error: {e} - retrying in 30s")
            time.sleep(30)

run_bot()