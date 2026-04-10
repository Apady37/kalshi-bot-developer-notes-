"""
paper_bot.py — Kalshi Paper Trading Simulator
Uses real Kalshi market data + signal logic identical to main.py.
No real orders are placed.

Fill model (pessimistic, mirrors live conditions):
  Entry:  filled at ask + SLIPPAGE_CENTS with FILL_PROB probability
  Exit:   filled at bid - SLIPPAGE_CENTS with EXIT_FILL_PROB probability
  Missed exit: position stays open — loss can grow, stop does not guarantee exit
  Missed entry: trade is skipped, logged as NO_FILL

Run alongside main.py in demo before going live. If demo is not profitable
here, it will not be profitable live.
"""

import requests
import os
import random
import uuid
import datetime
import base64
import time
import csv
import json
import math
import pytz
from dotenv import load_dotenv
from urllib.parse import urlparse
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

load_dotenv()

KEY_ID   = os.getenv("KALSHI_KEY_ID")
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
ET       = pytz.timezone("America/New_York")

# ── PAPER CONFIG ──────────────────────────────────────────────────────────────
# Keep signal params identical to main.py so comparisons are valid

VIRTUAL_BALANCE   = 10_000    # $100.00 in cents — matches a small live account
KELLY_FRACTION    = 0.5
MIN_BET_CENTS     = 10
MAX_BET_CENTS     = 10
MIN_P_WIN         = 0.55
MIN_ASK           = 20
MAX_ASK           = 80
MIN_MINS_LEFT     = 2.0
MAX_MINS_LEFT     = 18.0
MIN_VOLUME        = 200
MAX_POSITIONS     = 8
DAILY_LOSS_LIMIT  = 1000      # $10 in cents
BALANCE_FLOOR_PCT = 0.80
REVERSAL_THRESH   = 0.60
STRONG_THRESH     = 0.15
TREND_THRESH      = 0.40

# ── FILL SIMULATION ───────────────────────────────────────────────────────────
# These model the gap between quoted price and what live trading actually gets.

ENTRY_SLIPPAGE_CENTS  = 1     # buy 1c worse than ask
EXIT_SLIPPAGE_CENTS   = 1     # sell 1c worse than bid
FILL_PROBABILITY      = 0.80  # 20% of entries don't fill (thin book, timing)
EXIT_FILL_PROBABILITY = 0.70  # exits are harder — 30% stay open

SERIES = {
    "KXBTC15M": {"symbol": "BTC", "product": "BTC-USD"},
    "KXETH15M": {"symbol": "ETH", "product": "ETH-USD"},
    "KXXRP15M": {"symbol": "XRP", "product": "XRP-USD"},
    "KXSOL15M": {"symbol": "SOL", "product": "SOL-USD"},
}

POSITIONS_FILE = "paper_positions.json"
TRADES_FILE    = "paper_trades.csv"
SIGNALS_FILE   = "paper_signals.csv"
STATE_FILE     = "paper_state.json"
CONFIG_FILE    = "config.json"

_price_cache = {}
_price_time  = {}

# ── AUTH (read-only — same as main.py, no orders placed) ─────────────────────

def load_private_key():
    key_data = os.getenv("KALSHI_PRIVATE_KEY")
    if key_data:
        key_data = key_data.replace("\\n", "\n")
        return serialization.load_pem_private_key(key_data.encode(), password=None, backend=default_backend())
    with open("private_key.pem", "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def sign_request(pk, ts, method, path):
    msg = f"{ts}{method}{path.split('?')[0]}".encode()
    sig = pk.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return base64.b64encode(sig).decode()

def get_headers(method, path):
    ts  = str(int(datetime.datetime.now().timestamp() * 1000))
    pk  = load_private_key()
    sig = sign_request(pk, ts, method, urlparse(BASE_URL + path).path)
    return {
        "KALSHI-ACCESS-KEY":       KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type":            "application/json",
    }

def kalshi_get(path):
    return requests.get(BASE_URL + path, headers=get_headers("GET", path), timeout=6).json()

# ── KALSHI MARKET DATA ────────────────────────────────────────────────────────

def get_market(ticker):
    try:
        m = kalshi_get(f"/markets/{ticker}").get("market", {})
        def cents(key):
            v = m.get(key)
            return round(float(v) * 100, 1) if v else None
        return {
            "yes_ask": cents("yes_ask_dollars"),
            "no_ask":  cents("no_ask_dollars"),
            "yes_bid": cents("yes_bid_dollars"),
            "no_bid":  cents("no_bid_dollars"),
            "status":  m.get("status", ""),
            "result":  m.get("result", ""),
            "mins":    _mins_left(m.get("close_time", "")),
        }
    except:
        return {}

def get_markets_for_series(series):
    try:
        return kalshi_get(f"/markets?limit=5&status=open&series_ticker={series}").get("markets", [])
    except:
        return []

def _mins_left(close_time):
    try:
        ct  = datetime.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0, (ct - now).total_seconds() / 60)
    except:
        return 0

# ── COINBASE CANDLES ──────────────────────────────────────────────────────────

def get_candles(product, limit=12):
    now = time.time()
    key = f"{product}_{limit}"
    if key in _price_cache and now - _price_time.get(key, 0) < 8:
        return _price_cache[key]
    try:
        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{product}/candles?granularity=60&limit={limit}",
            timeout=5
        ).json()
        if isinstance(r, list) and len(r) >= 3:
            result = list(reversed(r))
            _price_cache[key] = result
            _price_time[key]  = now
            return result
    except:
        pass
    return []

def _norm_cdf(x):
    return 0.5 * math.erfc(-x * math.sqrt(0.5))

def get_current_price(product):
    candles = get_candles(product, 3)
    return float(candles[-1][4]) if candles else None

def estimate_volatility(product, periods=10):
    candles = get_candles(product, periods + 1)
    if len(candles) < 3:
        return 0.80
    closes   = [float(c[4]) for c in candles]
    log_rets = [math.log(closes[i+1] / closes[i]) for i in range(len(closes)-1) if closes[i] > 0]
    if len(log_rets) < 2:
        return 0.80
    n    = len(log_rets)
    mean = sum(log_rets) / n
    var  = sum((r - mean) ** 2 for r in log_rets) / (n - 1)
    return max(0.05, min(10.0, math.sqrt(var) * math.sqrt(525600)))

def bs_binary_probability(S, K, T_minutes, sigma_annual, direction="YES"):
    if K <= 0 or S <= 0 or T_minutes <= 0 or sigma_annual <= 0:
        return None
    T = T_minutes / 525600.0
    try:
        d2 = (math.log(S / K) - 0.5 * sigma_annual ** 2 * T) / (sigma_annual * math.sqrt(T))
    except (ValueError, ZeroDivisionError):
        return None
    p_above = _norm_cdf(d2)
    return p_above if direction == "YES" else (1.0 - p_above)

# ── SIGNAL LOGIC (identical to main.py) ──────────────────────────────────────

def get_momentum(product):
    candles = get_candles(product, 5)
    if len(candles) < 4:
        return 0.0, "NEUTRAL"
    completed = candles[:-1]
    open_p  = float(completed[0][3])
    close_p = float(completed[-1][4])
    pct = (close_p - open_p) / open_p * 100
    if   pct <= -REVERSAL_THRESH: return round(pct, 3), "REVERSAL_DOWN"
    elif pct >= +REVERSAL_THRESH: return round(pct, 3), "REVERSAL_UP"
    elif pct <= -STRONG_THRESH:   return round(pct, 3), "STRONG_DOWN"
    elif pct >= +STRONG_THRESH:   return round(pct, 3), "STRONG_UP"
    else:                         return round(pct, 3), "NEUTRAL"

def get_trend(product):
    candles = get_candles(product, 12)
    if len(candles) < 10:
        return "FLAT"
    completed = candles[:10]
    open_p  = float(completed[0][3])
    close_p = float(completed[-1][4])
    pct = (close_p - open_p) / open_p * 100
    if   pct >  TREND_THRESH: return "UP"
    elif pct < -TREND_THRESH: return "DOWN"
    else:                      return "FLAT"

def get_direction(signal, trend):
    if signal == "REVERSAL_DOWN":
        return "NO" if trend in ["DOWN", "FLAT"] else None
    if signal == "REVERSAL_UP":
        return "YES" if trend in ["UP", "FLAT"] else None
    if signal == "STRONG_DOWN":
        return "NO" if trend == "DOWN" else None
    if signal == "STRONG_UP":
        return "YES" if trend == "UP" else None
    return None

def get_edge(signal, trend):
    if signal in ["REVERSAL_DOWN", "REVERSAL_UP"]:
        return 0.22 if trend in ["DOWN", "UP"] else 0.10
    if signal in ["STRONG_DOWN", "STRONG_UP"]:
        return 0.12 if trend in ["DOWN", "UP"] else 0.06
    return 0.0

def calc_ev(ask_cents, p_win):
    return round(p_win * 100 - ask_cents, 2)

def kelly_bet_size(ask_cents, p_win, balance_cents):
    net_win  = 100 - ask_cents
    net_lose = ask_cents
    p_lose   = 1.0 - p_win
    if net_win <= 0 or net_lose <= 0:
        return MIN_BET_CENTS
    full_kelly = (p_win * net_win - p_lose * net_lose) / net_win
    half_kelly = full_kelly * KELLY_FRACTION
    if half_kelly <= 0:
        return 0
    bet_cents = int(balance_cents * half_kelly)
    return max(MIN_BET_CENTS, min(MAX_BET_CENTS, bet_cents))

# ── FILL SIMULATION ───────────────────────────────────────────────────────────

def simulate_entry(ask_cents):
    """
    Returns fill price (ask + slippage) or None if the order didn't fill.
    20% of entries don't fill — order arrives late, book moves, spread widens.
    """
    if random.random() > FILL_PROBABILITY:
        return None
    return min(ask_cents + ENTRY_SLIPPAGE_CENTS, 99)

def simulate_exit(bid_cents):
    """
    Returns fill price (bid - slippage) or None if the exit didn't fill.
    30% of exits don't fill — position stays open, loss can grow.
    Uses bid (not ask/mid) — this is the price you actually sell at.
    """
    if random.random() > EXIT_FILL_PROBABILITY:
        return None
    return max(bid_cents - EXIT_SLIPPAGE_CENTS, 1)

# ── PERSISTENCE ───────────────────────────────────────────────────────────────

def load_positions():
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE) as f:
                return json.load(f)
        except:
            pass
    return []

def save_positions(p):
    with open(POSITIONS_FILE, "w") as f:
        json.dump(p, f, indent=2)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
            if s.get("date") == str(datetime.date.today()):
                return s
        except:
            pass
    return {
        "balance":    VIRTUAL_BALANCE,
        "starting":   VIRTUAL_BALANCE,
        "daily_pnl":  0,
        "wins":       0,
        "losses":     0,
        "no_fills":   0,   # entries that didn't fill
        "stuck":      0,   # exits that didn't fill (stayed open)
        "total":      0,
        "date":       str(datetime.date.today()),
    }

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)

def log_trade(row):
    new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="") as f:
        fields = ["time", "symbol", "side", "entry_ask", "fill_price",
                  "exit_bid", "exit_fill", "contracts", "pnl",
                  "ev", "p_win", "kelly_f", "mins", "reason",
                  "signal", "trend", "balance", "candidate_id"]
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})

def log_signal(row):
    """Log every evaluated candidate (filled or not) for the optimizer."""
    new = not os.path.exists(SIGNALS_FILE)
    with open(SIGNALS_FILE, "a", newline="") as f:
        fields = ["time", "symbol", "ticker", "signal", "trend",
                  "p_win", "ask", "mins", "volume", "ev", "candidate_id"]
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})

# ── CONFIG / OPTIMIZER BRIDGE ─────────────────────────────────────────────────

def load_config():
    """Read optimizer output. Returns {} if not yet generated."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}

def apply_config(cfg):
    """Override module-level params in place from config.json values."""
    global REVERSAL_THRESH, STRONG_THRESH, TREND_THRESH
    global MIN_P_WIN, MIN_ASK, MAX_ASK, MIN_MINS_LEFT, MAX_MINS_LEFT
    if "REVERSAL_THRESH" in cfg: REVERSAL_THRESH = cfg["REVERSAL_THRESH"]
    if "STRONG_THRESH"   in cfg: STRONG_THRESH   = cfg["STRONG_THRESH"]
    if "TREND_THRESH"    in cfg: TREND_THRESH     = cfg["TREND_THRESH"]
    if "MIN_P_WIN"       in cfg: MIN_P_WIN        = cfg["MIN_P_WIN"]
    if "MIN_ASK"         in cfg: MIN_ASK          = cfg["MIN_ASK"]
    if "MAX_ASK"         in cfg: MAX_ASK          = cfg["MAX_ASK"]
    if "MIN_MINS_LEFT"   in cfg: MIN_MINS_LEFT    = cfg["MIN_MINS_LEFT"]
    if "MAX_MINS_LEFT"   in cfg: MAX_MINS_LEFT    = cfg["MAX_MINS_LEFT"]

def get_bayesian_edge(signal, trend, cfg):
    """
    Use empirically calibrated edge if optimizer has enough data,
    else fall back to hardcoded get_edge().
    """
    key = f"{signal}|{trend}"
    edges = cfg.get("edges", {})
    if key in edges and cfg.get("n_trades", 0) >= 20:
        return edges[key]
    return get_edge(signal, trend)

# ── BLACKLIST ─────────────────────────────────────────────────────────────────

_blacklist      = set()
_window_tickers = set()

def _update_window_tickers():
    global _window_tickers, _blacklist
    try:
        current = set()
        for series in SERIES:
            mkts = get_markets_for_series(series)
            if mkts:
                current.add(mkts[0].get("ticker", ""))
        if current and current != _window_tickers and _window_tickers:
            print(f"  New window — blacklist cleared")
            _blacklist.clear()
        if current:
            _window_tickers = current
    except:
        pass

# ── ENTRY SCANNING ────────────────────────────────────────────────────────────

def scan_entries(state):
    # ── Reload optimizer output at the start of every scan ────────────────────
    cfg = load_config()
    apply_config(cfg)

    positions    = load_positions()
    open_tickers = {(p["ticker"], p["side"]) for p in positions}

    if len(positions) >= MAX_POSITIONS:
        return state

    candidates = []

    for series, info in SERIES.items():
        symbol  = info["symbol"]
        product = info["product"]

        pct, signal = get_momentum(product)
        if signal == "NEUTRAL":
            continue

        trend = get_trend(product)
        side  = get_direction(signal, trend)
        if side is None:
            continue

        e = get_bayesian_edge(signal, trend, cfg)   # Bayesian or hardcoded
        if e == 0:
            continue

        markets = get_markets_for_series(series)
        for m in markets:
            ticker  = m.get("ticker", "")
            volume  = float(m.get("volume_fp", 0) or 0)
            yes_raw = m.get("yes_ask_dollars")
            no_raw  = m.get("no_ask_dollars")
            status  = m.get("status", "")
            close_t = m.get("close_time", "")

            if status not in ["active", "open"]:
                continue
            if volume < MIN_VOLUME:
                continue
            if yes_raw is None or no_raw is None:
                continue
            if ticker in _blacklist:
                continue
            if (ticker, side) in open_tickers:
                continue

            yes_ask = round(float(yes_raw) * 100, 1)
            no_ask  = round(float(no_raw)  * 100, 1)
            ask     = yes_ask if side == "YES" else no_ask
            mins    = _mins_left(close_t)

            if not (MIN_ASK <= ask <= MAX_ASK):
                continue
            if not (MIN_MINS_LEFT <= mins <= MAX_MINS_LEFT):
                continue

            strike_raw = m.get("floor_strike") or m.get("cap_strike")
            bs_p_win   = None
            if strike_raw:
                try:
                    S     = get_current_price(product)
                    sigma = estimate_volatility(product)
                    if S and sigma:
                        bs_p_win = bs_binary_probability(float(S), float(strike_raw), mins, sigma, side)
                except Exception:
                    pass

            if bs_p_win is not None:
                model_edge = bs_p_win - (ask / 100.0)
                if model_edge < 0.04:
                    continue
                p_win = bs_p_win
                e     = model_edge
            else:
                p_win = min((ask / 100.0) + e, 0.92)

            ev = calc_ev(ask, p_win)
            if ev <= 0:
                continue
            if p_win < MIN_P_WIN:
                continue

            bet_cents = kelly_bet_size(ask, p_win, state["balance"])
            if bet_cents == 0:
                continue

            contracts     = max(1, int(bet_cents // ask))
            max_contracts = max(1, int(volume * 0.005))
            contracts     = min(contracts, max_contracts)

            net_win    = 100 - ask
            full_kelly = (p_win * net_win - (1 - p_win) * ask) / net_win
            cid        = str(uuid.uuid4())[:8]

            # Log every candidate the optimizer can see — filled or not
            log_signal({
                "time": datetime.datetime.now().isoformat(),
                "symbol": symbol, "ticker": ticker,
                "signal": signal, "trend": trend,
                "p_win": round(p_win, 3), "ask": ask,
                "mins": round(mins, 1), "volume": volume,
                "ev": ev, "candidate_id": cid,
            })

            candidates.append({
                "ticker":       ticker,
                "symbol":       symbol,
                "side":         side,
                "ask":          ask,
                "contracts":    contracts,
                "signal":       signal,
                "trend":        trend,
                "pct":          pct,
                "mins":         round(mins, 1),
                "volume":       volume,
                "ev":           ev,
                "p_win":        round(p_win, 3),
                "kelly_f":      round(full_kelly * KELLY_FRACTION, 4),
                "target":       min(ask + 8, 95),
                "stop":         max(ask - 5, 2),
                "candidate_id": cid,
            })

    candidates.sort(key=lambda x: x["ev"], reverse=True)

    for c in candidates:
        if len(load_positions()) >= MAX_POSITIONS:
            break

        cost = c["ask"] * c["contracts"]
        if state["balance"] < cost:
            continue

        # ── SIMULATE FILL ─────────────────────────────────────────────────────
        fill_price = simulate_entry(c["ask"])
        if fill_price is None:
            state["no_fills"] += 1
            print(f"  ✗ NO FILL: {c['symbol']} {c['side']} @ {c['ask']}c — order didn't execute")
            _blacklist.add(c["ticker"])
            log_trade({
                "time": datetime.datetime.now().isoformat(),
                "symbol": c["symbol"], "side": c["side"],
                "entry_ask": c["ask"], "fill_price": "NO_FILL",
                "exit_bid": "", "exit_fill": "",
                "contracts": c["contracts"], "pnl": 0,
                "ev": c["ev"], "p_win": c["p_win"], "kelly_f": c["kelly_f"],
                "mins": c["mins"], "reason": "NO_FILL",
                "signal": c["signal"], "trend": c["trend"],
                "balance": state["balance"],
            })
            continue

        # Fill succeeded — deduct at actual fill price (ask + slippage)
        actual_cost = fill_price * c["contracts"]
        state["balance"] -= actual_cost
        state["total"]   += 1

        pos = {
            "ticker":       c["ticker"],
            "symbol":       c["symbol"],
            "side":         c["side"],
            "entry":        fill_price,   # what we actually paid (ask + slippage)
            "quoted_ask":   c["ask"],     # what was quoted
            "contracts":    c["contracts"],
            "target":       c["target"],
            "stop":         c["stop"],
            "signal":       c["signal"],
            "trend":        c["trend"],
            "ev":           c["ev"],
            "p_win":        c["p_win"],
            "kelly_f":      c["kelly_f"],
            "candidate_id": c["candidate_id"],
            "entry_time":   datetime.datetime.now().isoformat(),
        }

        positions = load_positions()
        positions.append(pos)
        save_positions(positions)
        save_state(state)

        slip_note = f"+{fill_price - c['ask']}c slip" if fill_price != c["ask"] else ""
        conf = "⭐" if c["trend"] in ["UP", "DOWN"] else ""
        print(
            f"  📝 PAPER BUY {c['side']}: {c['symbol']} @ {fill_price}c ({slip_note}) x{c['contracts']} {conf}"
            f" | EV:+{c['ev']}c p_win:{c['p_win']:.0%}"
            f" | Kelly:{c['kelly_f']:.1%} cost:${actual_cost/100:.2f}"
            f" | {c['signal']} trend:{c['trend']}({c['pct']:+.3f}%)"
            f" | {c['mins']}m"
        )

    return state

# ── EXIT MANAGEMENT ───────────────────────────────────────────────────────────

def manage_exits(state):
    positions  = load_positions()
    still_open = []

    for pos in positions:
        m         = get_market(pos["ticker"])
        status    = m.get("status", "")
        contracts = pos["contracts"]
        entry     = pos["entry"]
        hold_mins = (
            datetime.datetime.now()
            - datetime.datetime.fromisoformat(pos["entry_time"])
        ).total_seconds() / 60

        # ── RESOLVED ──────────────────────────────────────────────────────────
        if status == "finalized":
            result = m.get("result", "").lower()
            won    = bool(result) and (result == pos["side"].lower())
            pnl    = ((100 - entry) if won else -entry) * contracts
            state["balance"]   += (100 * contracts if won else 0)
            state["daily_pnl"] += pnl
            if won:
                state["wins"] += 1
                print(f"  ✅ WIN:  {pos['symbol']} {pos['side']} paid $1 x{contracts} +${pnl/100:.2f} | bal:${state['balance']/100:.2f}")
            else:
                state["losses"] += 1
                print(f"  ❌ LOSS: {pos['symbol']} {pos['side']} paid $0 x{contracts} -${abs(pnl)/100:.2f} | bal:${state['balance']/100:.2f}")
            log_trade({
                "time": pos["entry_time"], "symbol": pos["symbol"], "side": pos["side"],
                "entry_ask": pos.get("quoted_ask", entry), "fill_price": entry,
                "exit_bid": 100 if won else 0, "exit_fill": 100 if won else 0,
                "contracts": contracts, "pnl": pnl,
                "ev": pos.get("ev", 0), "p_win": pos.get("p_win", 0),
                "kelly_f": pos.get("kelly_f", 0), "mins": round(hold_mins, 1),
                "reason": "RESOLVED", "signal": pos.get("signal", ""),
                "trend": pos.get("trend", ""), "balance": state["balance"],
                "candidate_id": pos.get("candidate_id", ""),
            })
            continue

        # ── LIVE MARKET: use BID for exits ────────────────────────────────────
        current_ask = m.get("yes_ask") if pos["side"] == "YES" else m.get("no_ask")
        current_bid = m.get("yes_bid") if pos["side"] == "YES" else m.get("no_bid")

        if current_ask is None or current_bid is None:
            still_open.append(pos)
            continue

        # ── PROFIT TARGET ─────────────────────────────────────────────────────
        if current_ask >= pos["target"]:
            exit_fill = simulate_exit(current_bid)
            if exit_fill is None:
                # Exit didn't fill — stay in position (common in live trading)
                state["stuck"] += 1
                print(f"  ⚠ STUCK:  {pos['symbol']} {pos['side']} target hit @ {current_ask}c but exit didn't fill (bid:{current_bid}c)")
                still_open.append(pos)
                continue
            pnl = (exit_fill - entry) * contracts
            state["balance"]   += exit_fill * contracts
            state["daily_pnl"] += pnl
            state["wins"]      += 1
            slip = current_bid - exit_fill
            print(f"  ✅ TARGET: {pos['symbol']} {pos['side']} {entry}→{exit_fill}c (bid:{current_bid}c slip:{slip}c) x{contracts} +${pnl/100:.2f} | bal:${state['balance']/100:.2f}")
            log_trade({
                "time": pos["entry_time"], "symbol": pos["symbol"], "side": pos["side"],
                "entry_ask": pos.get("quoted_ask", entry), "fill_price": entry,
                "exit_bid": current_bid, "exit_fill": exit_fill,
                "contracts": contracts, "pnl": pnl,
                "ev": pos.get("ev", 0), "p_win": pos.get("p_win", 0),
                "kelly_f": pos.get("kelly_f", 0), "mins": round(hold_mins, 1),
                "reason": "PROFIT_TARGET", "signal": pos.get("signal", ""),
                "trend": pos.get("trend", ""), "balance": state["balance"],
                "candidate_id": pos.get("candidate_id", ""),
            })

        # ── STOP LOSS ─────────────────────────────────────────────────────────
        elif current_ask <= pos["stop"]:
            _blacklist.add(pos["ticker"])
            exit_fill = simulate_exit(current_bid)
            if exit_fill is None:
                # Stop didn't fill — position stays open, can lose more
                state["stuck"] += 1
                print(f"  ⚠ STUCK:  {pos['symbol']} {pos['side']} stop triggered @ {current_ask}c but exit didn't fill — holding")
                still_open.append(pos)
                continue
            pnl = (exit_fill - entry) * contracts
            state["balance"]   += exit_fill * contracts
            state["daily_pnl"] += pnl
            state["losses"]    += 1
            slip = current_bid - exit_fill
            print(f"  🔴 STOP:  {pos['symbol']} {pos['side']} {entry}→{exit_fill}c (bid:{current_bid}c slip:{slip}c) x{contracts} -${abs(pnl)/100:.2f} | bal:${state['balance']/100:.2f}")
            log_trade({
                "time": pos["entry_time"], "symbol": pos["symbol"], "side": pos["side"],
                "entry_ask": pos.get("quoted_ask", entry), "fill_price": entry,
                "exit_bid": current_bid, "exit_fill": exit_fill,
                "contracts": contracts, "pnl": pnl,
                "ev": pos.get("ev", 0), "p_win": pos.get("p_win", 0),
                "kelly_f": pos.get("kelly_f", 0), "mins": round(hold_mins, 1),
                "reason": "STOP_LOSS", "signal": pos.get("signal", ""),
                "trend": pos.get("trend", ""), "balance": state["balance"],
                "candidate_id": pos.get("candidate_id", ""),
            })

        else:
            still_open.append(pos)

    save_positions(still_open)
    return state

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def run():
    state = load_state()
    floor = state["starting"] * BALANCE_FLOOR_PCT
    scan  = 0

    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  Kalshi Paper Simulator — ${state['balance']/100:.2f} virtual")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  Signal:   Identical to live bot (momentum + trend + BS)")
    print(f"  Fill model:")
    print(f"    Entry:  ask + {ENTRY_SLIPPAGE_CENTS}c slippage | {FILL_PROBABILITY:.0%} fill rate")
    print(f"    Exit:   bid - {EXIT_SLIPPAGE_CENTS}c slippage | {EXIT_FILL_PROBABILITY:.0%} fill rate")
    print(f"    Missed exit: position stays open — loss can grow")
    print(f"  Series:   {', '.join(SERIES.keys())}")
    print(f"  Risk:     ${DAILY_LOSS_LIMIT/100:.0f}/day | Floor: ${floor/100:.2f}")
    print(f"  NOTE: No real money. No real orders. Realistic fill simulation.")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()

    save_positions([])

    while True:
        try:
            today = str(datetime.date.today())
            if state.get("date") != today:
                print("  New day — reset daily P&L")
                state["daily_pnl"] = 0
                state["date"]      = today
                _blacklist.clear()

            _update_window_tickers()
            scan += 1

            if state["daily_pnl"] <= -DAILY_LOSS_LIMIT:
                print("  Daily loss limit hit. Done for today.")
                break

            if state["balance"] < floor:
                print(f"  Balance floor hit (${state['balance']/100:.2f}). Stopping.")
                break

            positions = load_positions()
            if positions:
                state = manage_exits(state)
                positions = load_positions()

            total  = state["wins"] + state["losses"]
            wr     = f"{round(state['wins']/total*100,1)}%" if total else "N/A"
            bal_pct = round(state["balance"] / state["starting"] * 100, 1)
            daily_str = f"+${state['daily_pnl']/100:.2f}" if state["daily_pnl"] >= 0 else f"-${abs(state['daily_pnl'])/100:.2f}"
            now_et = datetime.datetime.now(ET).strftime("%H:%M:%S ET")

            signals = []
            for info in SERIES.values():
                pct, sig = get_momentum(info["product"])
                trend    = get_trend(info["product"])
                signals.append(f"{info['symbol']}:{sig}({pct:+.2f}%)|{trend}")

            print(
                f"[{now_et}] #{scan} | ${state['balance']/100:.2f}({bal_pct}%) | "
                f"Day:{daily_str} | W/L:{state['wins']}/{state['losses']}({wr}) | "
                f"Open:{len(positions)} | NoFill:{state['no_fills']} | Stuck:{state['stuck']}"
            )
            print(f"  {' | '.join(signals)}")

            state = scan_entries(state)
            save_state(state)

            time.sleep(1)

        except KeyboardInterrupt:
            total = state["wins"] + state["losses"]
            wr    = f"{round(state['wins']/total*100,1)}%" if total else "N/A"
            print(f"\n  Stopped.")
            print(f"  Final: ${state['balance']/100:.2f} | W/L:{state['wins']}/{state['losses']}({wr})")
            print(f"  No-fills: {state['no_fills']} | Stuck exits: {state['stuck']} | Total: {state['total']}")
            save_state(state)
            break
        except Exception as e:
            print(f"  Error: {e}")
            time.sleep(3)

run()
