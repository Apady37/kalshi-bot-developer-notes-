import requests
import os
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

DRY_RUN           = True
BALANCE_FLOOR_PCT = 0.80
DAILY_LOSS_LIMIT  = 1000
MAX_POSITIONS     = 8
MIN_MINS_LEFT     = 2.0
MAX_MINS_LEFT     = 18.0
MIN_ASK           = 20
MAX_ASK           = 80
MIN_P_WIN         = 0.55    # Never enter below 55% win probability
REVERSAL_THRESH   = 0.60
STRONG_THRESH     = 0.15    # momentum threshold for STRONG signals (was hardcoded 0.20)
TREND_THRESH      = 0.40
MIN_VOLUME        = 200
KELLY_FRACTION    = 0.5
MIN_BET_CENTS       = 10
MAX_BET_CENTS       = 200     # Kelly can size up to $2/trade; was 10 (flat)
MAX_SPEND_CENTS     = 1000    # Hard stop: pause after $10 deployed — awaiting manual review
TEMPORAL_BIAS_FILE  = "temporal_bias.json"
BAYES_PRIORS_FILE   = "bayes_priors.json"
TEMPORAL_BIAS_MIN   = 0.65    # Only trade windows with ≥65% directional bias
VOL_SPIKE_MULT      = 1.4     # Skip if 24h vol > 1.4× rolling baseline
BIAS_LOOKBACK_DAYS  = 720     # 24 months of hourly data for bias analysis

SERIES = {
    "KXBTC15M": {"symbol": "BTC", "product": "BTC-USD"},
    "KXETH15M": {"symbol": "ETH", "product": "ETH-USD"},
    "KXXRP15M": {"symbol": "XRP", "product": "XRP-USD"},
    "KXSOL15M": {"symbol": "SOL", "product": "SOL-USD"},
}

POSITIONS_FILE = "open_positions.json"
TRADES_FILE    = "live_trades.csv"

_price_cache   = {}
_price_time    = {}
_temporal_bias = {}   # {symbol: {hour: {"direction": "UP"|"DOWN", "bias": float, "n": int}}}
_bayes_priors  = {}   # {"symbol_hour_signal": {"alpha": int, "beta": int}}
_vol_base      = {}   # product → baseline hourly vol
_vol_base_time = {}   # product → last refresh timestamp


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

def kalshi_post(path, body):
    return requests.post(BASE_URL + path, json=body, headers=get_headers("POST", path), timeout=6).json()

def kalshi_delete(path):
    return requests.delete(BASE_URL + path, headers=get_headers("DELETE", path), timeout=6)

def get_balance():
    try:
        return int(float(kalshi_get("/portfolio/balance").get("balance", 0)))
    except:
        return _balance

def cancel_all_resting():
    if DRY_RUN:
        return
    try:
        orders = kalshi_get("/portfolio/orders?status=resting&limit=100").get("orders", [])
        for o in orders:
            oid = o.get("order_id", "")
            if oid:
                kalshi_delete(f"/portfolio/orders/{oid}")
        if orders:
            print(f"  Cancelled {len(orders)} resting orders on startup")
    except:
        pass

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
            "result":  m.get("result", ""),   # "yes" or "no" once finalized
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
    """Standard normal CDF: Phi(x) = 0.5 * erfc(-x / sqrt(2))."""
    return 0.5 * math.erfc(-x * math.sqrt(0.5))

def get_current_price(product):
    """Latest 1-min close price from Coinbase."""
    candles = get_candles(product, 3)
    return float(candles[-1][4]) if candles else None

def estimate_volatility(product, periods=10):
    """
    Annualized realized volatility from recent 1-minute candles.
    σ_annual = σ_per_minute × √525600 (minutes per year).
    Falls back to 0.80 (80% ann. vol) if insufficient data.
    """
    candles = get_candles(product, periods + 1)
    if len(candles) < 3:
        return 0.80
    closes = [float(c[4]) for c in candles]
    log_rets = [
        math.log(closes[i + 1] / closes[i])
        for i in range(len(closes) - 1)
        if closes[i] > 0
    ]
    if len(log_rets) < 2:
        return 0.80
    n    = len(log_rets)
    mean = sum(log_rets) / n
    var  = sum((r - mean) ** 2 for r in log_rets) / (n - 1)
    return max(0.05, min(10.0, math.sqrt(var) * math.sqrt(525600)))

def bs_binary_probability(S, K, T_minutes, sigma_annual, direction="YES"):
    """
    Black-Scholes probability for a cash-or-nothing binary option.

    For a YES contract (pays $1 if S_T > K):
        P = N(d2),  d2 = (ln(S/K) - 0.5·σ²·T) / (σ·√T)

    For a NO contract (pays $1 if S_T < K):
        P = N(-d2) = 1 - N(d2)

    T_minutes : time to expiry in minutes
    Returns None if inputs are invalid.
    """
    if K <= 0 or S <= 0 or T_minutes <= 0 or sigma_annual <= 0:
        return None
    T = T_minutes / 525600.0   # minutes → years
    try:
        d2 = (math.log(S / K) - 0.5 * sigma_annual ** 2 * T) / (
            sigma_annual * math.sqrt(T)
        )
    except (ValueError, ZeroDivisionError):
        return None
    p_above = _norm_cdf(d2)
    return p_above if direction == "YES" else (1.0 - p_above)


# ── Temporal bias ─────────────────────────────────────────────────────────────

def _get_hourly_candles_paged(product, n_hours):
    """
    Pages backward through Coinbase to fetch up to n_hours of 1h candles.
    Rate-limited to ~3 req/s. Only called once per week (cached to disk).
    """
    end   = int(time.time())
    start = end - n_hours * 3600
    chunk = 300
    all_candles = []
    t_end = end
    while t_end > start:
        t_start = max(start, t_end - chunk * 3600)
        try:
            r = requests.get(
                f"https://api.exchange.coinbase.com/products/{product}/candles"
                f"?granularity=3600&start={t_start}&end={t_end}",
                timeout=10
            ).json()
            if isinstance(r, list):
                all_candles.extend(r)
        except Exception:
            pass
        t_end = t_start - 1
        time.sleep(0.35)
    all_candles.sort(key=lambda c: c[0])
    seen, deduped = set(), []
    for c in all_candles:
        if c[0] not in seen:
            seen.add(c[0])
            deduped.append(c)
    return deduped


def compute_temporal_bias():
    """
    Pulls 24 months of hourly candles per symbol, groups by UTC hour-of-day,
    and identifies hours with ≥TEMPORAL_BIAS_MIN directional bias.
    Saves result to temporal_bias.json. Only runs when file is stale (>7 days).
    """
    print("  Computing 24-month temporal bias — this runs once per week (~60s)...")
    result = {}
    for series, info in SERIES.items():
        symbol  = info["symbol"]
        product = info["product"]
        print(f"    {symbol}: fetching {BIAS_LOOKBACK_DAYS}d of hourly data...")
        candles = _get_hourly_candles_paged(product, BIAS_LOOKBACK_DAYS * 24)
        if len(candles) < 100:
            print(f"    {symbol}: insufficient data ({len(candles)} candles)")
            continue
        hourly = {}
        for c in candles:
            ts     = c[0]
            open_  = float(c[3])
            close_ = float(c[4])
            if open_ <= 0:
                continue
            h = datetime.datetime.utcfromtimestamp(ts).hour
            if h not in hourly:
                hourly[h] = {"up": 0, "total": 0}
            hourly[h]["up"]    += int(close_ > open_)
            hourly[h]["total"] += 1
        windows = {}
        for h, counts in hourly.items():
            n    = counts["total"]
            if n < 30:
                continue
            up_p = counts["up"] / n
            if up_p >= TEMPORAL_BIAS_MIN:
                windows[h] = {"direction": "UP",   "bias": round(up_p, 4),       "n": n}
            elif (1 - up_p) >= TEMPORAL_BIAS_MIN:
                windows[h] = {"direction": "DOWN",  "bias": round(1 - up_p, 4), "n": n}
        result[symbol] = windows
        print(f"    {symbol}: {len(windows)}/24 high-bias hours found")
    out = {"computed_at": datetime.datetime.utcnow().isoformat(), "windows": result}
    with open(TEMPORAL_BIAS_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"  Temporal bias saved → {TEMPORAL_BIAS_FILE}")
    return result


def load_temporal_bias():
    """Loads temporal_bias.json; recomputes if missing or >7 days old."""
    global _temporal_bias
    if os.path.exists(TEMPORAL_BIAS_FILE):
        try:
            with open(TEMPORAL_BIAS_FILE) as f:
                data = json.load(f)
            age = (datetime.datetime.utcnow() -
                   datetime.datetime.fromisoformat(data["computed_at"])).days
            if age < 7:
                _temporal_bias = data["windows"]
                total = sum(len(v) for v in _temporal_bias.values())
                print(f"  Temporal bias loaded: {total} high-bias windows ({age}d old)")
                return
        except Exception:
            pass
    _temporal_bias = compute_temporal_bias()


# ── Volatility filter ──────────────────────────────────────────────────────────

def _hourly_vol(candles):
    """Per-period realized vol from hourly log-returns."""
    closes = [float(c[4]) for c in candles if float(c[4]) > 0]
    if len(closes) < 5:
        return 0.0
    log_rets = [math.log(closes[i+1] / closes[i])
                for i in range(len(closes) - 1) if closes[i] > 0]
    if len(log_rets) < 2:
        return 0.0
    mean = sum(log_rets) / len(log_rets)
    var  = sum((r - mean) ** 2 for r in log_rets) / (len(log_rets) - 1)
    return math.sqrt(var)


def vol_filter(product):
    """
    Returns True (skip this trade) if 24h realized hourly vol > VOL_SPIKE_MULT
    times the rolling 300-hour baseline vol.
    Baseline refreshes once per day. 24h vol is computed fresh per call.
    """
    now = time.time()
    # Refresh baseline (~12.5 day window) once per day
    if product not in _vol_base or now - _vol_base_time.get(product, 0) > 86400:
        try:
            r = requests.get(
                f"https://api.exchange.coinbase.com/products/{product}/candles"
                f"?granularity=3600&limit=300",
                timeout=10
            ).json()
            baseline = _hourly_vol(sorted(r, key=lambda c: c[0])) if isinstance(r, list) else 0.0
        except Exception:
            baseline = 0.0
        _vol_base[product]      = baseline
        _vol_base_time[product] = now
    baseline = _vol_base.get(product, 0.0)
    if baseline == 0.0:
        return False
    # Compute current 24h vol
    try:
        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{product}/candles"
            f"?granularity=3600&limit=25",
            timeout=8
        ).json()
        vol_24h = _hourly_vol(sorted(r, key=lambda c: c[0])[-25:]) if isinstance(r, list) else 0.0
    except Exception:
        return False
    spiked = vol_24h > VOL_SPIKE_MULT * baseline
    if spiked:
        print(f"  VOL SPIKE {product}: 24h={vol_24h:.5f} > {VOL_SPIKE_MULT}×base={baseline:.5f}")
    return spiked


# ── Bayesian self-updating ─────────────────────────────────────────────────────

def load_bayes_priors():
    global _bayes_priors
    if os.path.exists(BAYES_PRIORS_FILE):
        try:
            with open(BAYES_PRIORS_FILE) as f:
                _bayes_priors = json.load(f)
            print(f"  Bayesian priors loaded: {len(_bayes_priors)} windows")
            return
        except Exception:
            pass
    _bayes_priors = {}


def save_bayes_priors():
    with open(BAYES_PRIORS_FILE, "w") as f:
        json.dump(_bayes_priors, f, indent=2)


def bayes_posterior(key):
    """
    Returns (p_win_posterior, n_observations) from Beta(α, β) posterior.
    Prior: Beta(1, 1) = uniform (no opinion before any data).
    Each win increments α, each loss increments β.
    Posterior mean = α / (α + β).
    """
    p     = _bayes_priors.get(key, {})
    alpha = p.get("alpha", 1)
    beta  = p.get("beta",  1)
    n     = (alpha - 1) + (beta - 1)   # observations excluding uninformative prior
    return alpha / (alpha + beta), n


def update_bayes_prior(key, won):
    """Called after every settled trade to sharpen the prior for this window."""
    if not key:
        return
    if key not in _bayes_priors:
        _bayes_priors[key] = {"alpha": 1, "beta": 1}
    if won:
        _bayes_priors[key]["alpha"] += 1
    else:
        _bayes_priors[key]["beta"]  += 1
    save_bayes_priors()


def confidence_scale(bias, bayes_key):
    """
    Returns a confidence multiplier ∈ [0.2, 1.0] for Kelly fraction scaling.

    Two components:
      bias_conf   = how far the temporal bias is above the 0.65 threshold
                    → 0.375 at bias=0.65, 1.0 at bias=0.9
      bayes_conf  = how decisive the Bayesian posterior is
                    → blended in with 40% weight once n≥10 observations

    Higher confidence → larger Kelly fraction → bigger bet.
    Lower confidence → smaller fraction → smaller bet.
    """
    bias_conf  = (bias - 0.5) / 0.40       # 0.375 at min bias, 1.0 at 0.90
    b_p, n     = bayes_posterior(bayes_key)
    if n >= 10:
        bayes_conf = min(1.0, abs(b_p - 0.5) / 0.25)  # 0 at 50/50, 1.0 at 75%+
        combined   = 0.6 * bias_conf + 0.4 * bayes_conf
    else:
        combined   = bias_conf
    return max(0.2, min(1.0, combined))


def blend_p_win(p_win_model, bayes_key):
    """
    Blends model p_win with Bayesian posterior.
    With <10 observations: model dominates entirely.
    At 50 observations: Bayesian gets 40% weight.
    This prevents the Bayesian from overriding a good model on sparse data.
    """
    b_p, n = bayes_posterior(bayes_key)
    if n < 10:
        return p_win_model
    blend = min(0.40, n / 125)          # ramps from 0 → 0.40 over 0–50 trades
    return (1 - blend) * p_win_model + blend * b_p


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

def get_day_pct():
    """BTC % change over last 60 minutes — proxy for market conditions."""
    try:
        candles = get_candles("BTC-USD", 60)
        if len(candles) < 50:
            return 0.0
        open_p  = float(candles[0][3])
        close_p = float(candles[-1][4])
        return (close_p - open_p) / open_p * 100
    except:
        return 0.0

def is_bad_market():
    return False  # No market-condition suspension — trade 24/7

def get_direction(signal, trend):
    if signal == "REVERSAL_DOWN":
        if trend in ["DOWN", "FLAT"]: return "NO"
        return None
    if signal == "REVERSAL_UP":
        if trend in ["UP", "FLAT"]:   return "YES"
        return None
    if signal == "STRONG_DOWN":
        if trend == "DOWN": return "NO"    # FLAT removed — require confirming trend
        return None
    if signal == "STRONG_UP":
        if trend == "UP":   return "YES"   # FLAT removed — require confirming trend
        return None
    return None

def get_edge(signal, trend):
    if signal in ["REVERSAL_DOWN", "REVERSAL_UP"]:
        if trend in ["DOWN", "UP"]: return 0.22
        if trend == "FLAT":         return 0.10
    if signal in ["STRONG_DOWN", "STRONG_UP"]:
        if trend in ["DOWN", "UP"]: return 0.12
        if trend == "FLAT":         return 0.06
    return 0.0

def calc_ev(ask_cents, p_win):
    """
    Expected net profit per contract (in cents).
    EV = p_win × (100 − ask) + p_lose × (−ask) = p_win × 100 − ask
    """
    return round(p_win * 100 - ask_cents, 2)

def kelly_bet_size(ask_cents, p_win, balance_cents, confidence=1.0):
    """
    Fractional Kelly scaled by statistical confidence.

    f* = (p·net_win − q·net_lose) / net_win   [full Kelly]
    bet = balance × f* × KELLY_FRACTION × confidence

    confidence ∈ [0.2, 1.0]: derived from temporal bias strength and
    Bayesian posterior certainty. High conviction → larger fraction.
    Never flat-sizes: every bet is mathematically weighted.
    """
    net_win  = 100 - ask_cents
    net_lose = ask_cents
    p_lose   = 1.0 - p_win

    if net_win <= 0 or net_lose <= 0:
        return MIN_BET_CENTS

    full_kelly     = (p_win * net_win - p_lose * net_lose) / net_win
    effective_frac = KELLY_FRACTION * max(0.2, min(1.0, confidence))
    scaled_kelly   = full_kelly * effective_frac

    if scaled_kelly <= 0:
        return 0

    bet_cents = int(balance_cents * scaled_kelly)
    return max(MIN_BET_CENTS, min(MAX_BET_CENTS, bet_cents))


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

def log_trade(row):
    new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="") as f:
        fields = ["time","symbol","side","entry","exit","contracts",
                  "pnl","ev","p_win","kelly_f","mins","reason","balance","signal","trend"]
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(row)

def place_buy(ticker, side, contracts, price_cents):
    if DRY_RUN:
        return contracts
    price_key = "yes_price" if side == "YES" else "no_price"
    expiry    = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "ticker":          ticker,
        "client_order_id": f"bot_{int(time.time()*1000)}",
        "type":            "limit",
        "action":          "buy",
        "side":            side.lower(),
        "count":           contracts,
        "expiration_time": expiry,
        price_key:         int(price_cents),
    }
    data     = kalshi_post("/portfolio/orders", body)
    order    = data.get("order", {})
    order_id = order.get("order_id", "")
    filled   = float(order.get("fill_count_fp", 0))

    # If not immediately filled, wait for Kalshi to match or auto-expire (3s)
    if filled == 0 and order_id:
        time.sleep(2)
        try:
            recheck = kalshi_get(f"/portfolio/orders/{order_id}")
            order   = recheck.get("order", {})
            filled  = float(order.get("fill_count_fp", 0))
            status  = order.get("status", "")
        except:
            status = ""

        # If still resting after expiry window, cancel explicitly
        if filled == 0:
            try:
                kalshi_delete(f"/portfolio/orders/{order_id}")
            except:
                pass
            return 0

    return int(filled)

def place_sell(ticker, side, contracts, price_cents):
    if DRY_RUN:
        return True
    price_key = "yes_price" if side == "YES" else "no_price"
    expiry    = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "ticker":          ticker,
        "client_order_id": f"bot_sell_{int(time.time()*1000)}",
        "type":            "limit",
        "action":          "sell",
        "side":            side.lower(),
        "count":           contracts,
        "expiration_time": expiry,
        price_key:         int(price_cents),
    }
    data     = kalshi_post("/portfolio/orders", body)
    order    = data.get("order", {})
    order_id = order.get("order_id", "")
    filled   = float(order.get("fill_count_fp", 0))

    if filled == 0 and order_id:
        time.sleep(2)
        try:
            recheck = kalshi_get(f"/portfolio/orders/{order_id}")
            filled  = float(recheck.get("order", {}).get("fill_count_fp", 0))
        except:
            pass
        if filled == 0:
            try:
                kalshi_delete(f"/portfolio/orders/{order_id}")
            except:
                pass

    return filled > 0


_balance        = get_balance() if not DRY_RUN else 10000
_starting       = _balance
_daily_pnl      = 0
_wins           = 0
_losses         = 0
_total          = 0
_total_spent    = 0   # cumulative capital deployed this session (cents)
_start_of_day   = datetime.date.today()
_blacklist      = set()
_window_tickers = set()


def _reset_daily():
    global _daily_pnl, _start_of_day, _blacklist
    if datetime.date.today() > _start_of_day:
        _daily_pnl    = 0
        _start_of_day = datetime.date.today()
        _blacklist.clear()
        print("  New day — reset")

def _is_trading_hours():
    return True  # Trade 24/7

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


def scan_entries():
    global _balance, _total, _total_spent

    positions    = load_positions()
    open_tickers = {(p["ticker"], p["side"]) for p in positions}

    if len(positions) >= MAX_POSITIONS:
        return

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

        e = get_edge(signal, trend)
        if e == 0:
            continue

        # ── 1. Temporal bias filter ───────────────────────────────────────────
        # Only trade UTC hours with ≥65% historical directional bias.
        # Bias direction must agree with the momentum signal direction.
        now_hour   = datetime.datetime.utcnow().hour
        bias_info  = _temporal_bias.get(symbol, {}).get(now_hour)
        if bias_info is None:
            print(f"  SKIP {symbol}: hour {now_hour}h UTC not a high-bias window")
            continue
        bias_dir = "YES" if bias_info["direction"] == "UP" else "NO"
        if bias_dir != side:
            print(f"  SKIP {symbol}: temporal bias={bias_dir} conflicts with signal={side}")
            continue
        bias_val = bias_info["bias"]   # e.g. 0.72

        # ── 2. Volatility filter ──────────────────────────────────────────────
        # Skip if 24h realized vol > 1.4× rolling baseline. Don't fire in chaos.
        if vol_filter(product):
            print(f"  SKIP {symbol}: volatility spike exceeds {VOL_SPIKE_MULT}× baseline")
            continue

        # Bayesian key for this window
        bayes_key = f"{symbol}_{now_hour}_{signal}"

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
                print(f"  SKIP {symbol}: vol {volume:.0f} < {MIN_VOLUME}")
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
                print(f"  SKIP {symbol}: ask {ask}c outside {MIN_ASK}-{MAX_ASK}c range")
                continue
            if not (MIN_MINS_LEFT <= mins <= MAX_MINS_LEFT):
                print(f"  SKIP {symbol}: {mins:.1f}m outside {MIN_MINS_LEFT}-{MAX_MINS_LEFT}m window")
                continue

            # ── Probability: Black-Scholes where strike is available ──────────
            # Kalshi markets expose floor_strike (e.g. BTC > $83000).
            # If present, compute N(d2) from realized vol — a genuine model
            # edge vs market-implied probability. Fall back to hardcoded edge.
            strike_raw = m.get("floor_strike") or m.get("cap_strike")
            bs_p_win   = None
            if strike_raw:
                try:
                    S     = get_current_price(product)
                    sigma = estimate_volatility(product)
                    if S and sigma:
                        bs_p_win = bs_binary_probability(
                            S, float(strike_raw), mins, sigma, side
                        )
                except Exception:
                    pass

            if bs_p_win is not None:
                model_edge = bs_p_win - (ask / 100.0)
                if model_edge < 0.04:
                    print(f"  SKIP {symbol}: BS edge {model_edge:.1%} < 4% required")
                    continue
                p_win = bs_p_win
                e     = model_edge
            else:
                p_win = min((ask / 100.0) + e, 0.92)

            # ── 3. Bayesian blend ─────────────────────────────────────────────
            # Blend model p_win with Bayesian posterior for this (symbol, hour,
            # signal) window. Posterior weight ramps from 0 → 40% over 50 trades.
            p_win = blend_p_win(p_win, bayes_key)

            # ── EV = expected net profit per contract (cents) ─────────────────
            ev = calc_ev(ask, p_win)
            if ev <= 0:
                print(f"  SKIP {symbol}: EV {ev}c negative")
                continue

            # Hard floor — never enter below 55% win probability
            if p_win < MIN_P_WIN:
                print(f"  SKIP {symbol}: p_win {p_win:.0%} below {MIN_P_WIN:.0%} minimum")
                continue

            # ── 4. Confidence-scaled Kelly ────────────────────────────────────
            # confidence ∈ [0.2, 1.0]: function of temporal bias strength and
            # Bayesian certainty. Drives bet variation — never flat-sizes.
            conf      = confidence_scale(bias_val, bayes_key)
            bet_cents = kelly_bet_size(ask, p_win, _balance, confidence=conf)
            if bet_cents == 0:
                continue

            contracts     = max(1, int(bet_cents // ask))
            max_contracts = max(1, int(volume * 0.005))
            contracts     = min(contracts, max_contracts)

            net_win    = 100 - ask
            full_kelly = (p_win * net_win - (1 - p_win) * ask) / net_win

            candidates.append({
                "ticker":    ticker,
                "symbol":    symbol,
                "side":      side,
                "ask":       ask,
                "contracts": contracts,
                "bet_cents": bet_cents,
                "signal":    signal,
                "trend":     trend,
                "pct":       pct,
                "mins":      round(mins, 1),
                "volume":    volume,
                "ev":        ev,
                "p_win":     round(p_win, 3),
                "kelly_f":   round(full_kelly * KELLY_FRACTION * conf, 4),
                "target":    min(ask + 8, 95),
                "stop":      max(ask - 5, 2),
                "bias":      round(bias_val, 3),
                "conf":      round(conf, 3),
                "bayes_key": bayes_key,
            })

    candidates.sort(key=lambda x: x["ev"], reverse=True)

    for c in candidates:
        if len(load_positions()) >= MAX_POSITIONS:
            break

        cost = c["ask"] * c["contracts"]
        if _balance < cost:
            continue

        if _total_spent + cost > MAX_SPEND_CENTS:
            remaining = MAX_SPEND_CENTS - _total_spent
            print(f"  ⛔ Spend cap: ${MAX_SPEND_CENTS/100:.2f} limit reached (${_total_spent/100:.2f} deployed). Awaiting manual review.")
            break

        filled = place_buy(c["ticker"], c["side"], c["contracts"], c["ask"])
        if filled == 0:
            print(f"  No fill — skipping {c['symbol']}")
            continue

        _total       += 1
        _total_spent += cost
        if not DRY_RUN:
            _balance = get_balance()   # sync exact Kalshi balance after every fill
        else:
            _balance -= cost

        pos = {
            "ticker":     c["ticker"],
            "symbol":     c["symbol"],
            "side":       c["side"],
            "entry":      c["ask"],
            "contracts":  filled,
            "target":     c["target"],
            "stop":       c["stop"],
            "signal":     c["signal"],
            "trend":      c["trend"],
            "ev":         c["ev"],
            "p_win":      c["p_win"],
            "kelly_f":    c["kelly_f"],
            "bias":       c.get("bias", 0),
            "conf":       c.get("conf", 0),
            "bayes_key":  c.get("bayes_key", ""),
            "entry_time": datetime.datetime.now().isoformat(),
        }

        positions = load_positions()
        positions.append(pos)
        save_positions(positions)

        star = "⭐" if c["trend"] in ["UP", "DOWN"] else ""
        print(
            f"  🟢 BUY {c['side']}: {c['symbol']} @ {c['ask']}c x{filled} {star}"
            f" | EV:+{c['ev']}c p_win:{c['p_win']:.0%}"
            f" | Kelly:{c['kelly_f']:.1%} bet:${cost/100:.2f} conf:{c.get('conf',0):.0%}"
            f" | bias:{c.get('bias',0):.0%} {c['signal']} trend:{c['trend']}({c['pct']:+.3f}%)"
            f" | {c['mins']}m vol:{c['volume']:.0f}"
        )


def manage_exits():
    global _balance, _daily_pnl, _wins, _losses

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

        # ── RESOLVED ──────────────────────────────────────
        if status == "finalized":
            # result is "yes" or "no" — compare against our position side
            result = m.get("result", "").lower()
            won    = bool(result) and (result == pos["side"].lower())
            pnl        = ((100 - entry) if won else -entry) * contracts
            _daily_pnl += pnl
            if not DRY_RUN:
                _balance = get_balance()
            else:
                _balance += (100 if won else 0) * contracts
            if won:
                _wins += 1
                print(f"  ✅ WIN:  {pos['symbol']} {pos['side']} paid $1 x{contracts} +${pnl/100:.2f} | bal:${_balance/100:.2f}")
            else:
                _losses += 1
                print(f"  ❌ LOSS: {pos['symbol']} {pos['side']} paid $0 x{contracts} -${abs(pnl)/100:.2f} | bal:${_balance/100:.2f}")
            log_trade({
                "time": pos["entry_time"], "symbol": pos["symbol"],
                "side": pos["side"], "entry": entry,
                "exit": 100 if pnl > 0 else 0,
                "contracts": contracts, "pnl": pnl,
                "ev": pos.get("ev", 0), "p_win": pos.get("p_win", 0),
                "kelly_f": pos.get("kelly_f", 0),
                "mins": round(hold_mins, 1), "reason": "RESOLVED",
                "balance": _balance, "signal": pos.get("signal", ""),
                "trend": pos.get("trend", ""),
            })
            # ── Bayesian update ───────────────────────────────────────────────
            # Sharpen the prior for this (symbol, hour, signal) window.
            # Every settled trade makes the next trade smarter.
            update_bayes_prior(pos.get("bayes_key", ""), won)
            continue

        current_ask = m.get("yes_ask") if pos["side"] == "YES" else m.get("no_ask")
        current_bid = m.get("yes_bid") if pos["side"] == "YES" else m.get("no_bid")

        if current_ask is None:
            still_open.append(pos)
            continue

        pnl = (current_ask - entry) * contracts

        # ── PROFIT TARGET ─────────────────────────────────
        if current_ask >= pos["target"]:
            sell_at = current_bid if (current_bid and current_bid > 2) else current_ask
            sold    = place_sell(pos["ticker"], pos["side"], contracts, sell_at)
            if sold or DRY_RUN:
                actual_pnl  = (sell_at - entry) * contracts
                _daily_pnl += actual_pnl
                if not DRY_RUN:
                    _balance = get_balance()
                else:
                    _balance += sell_at * contracts
                _wins      += 1
                print(f"  ✅ TARGET: {pos['symbol']} {pos['side']} {entry}→{sell_at}c x{contracts} +${actual_pnl/100:.2f} | bal:${_balance/100:.2f}")
                log_trade({
                    "time": pos["entry_time"], "symbol": pos["symbol"],
                    "side": pos["side"], "entry": entry, "exit": sell_at,
                    "contracts": contracts, "pnl": actual_pnl,
                    "ev": pos.get("ev", 0), "p_win": pos.get("p_win", 0),
                    "kelly_f": pos.get("kelly_f", 0),
                    "mins": round(hold_mins, 1), "reason": "PROFIT_TARGET",
                    "balance": _balance, "signal": pos.get("signal", ""),
                    "trend": pos.get("trend", ""),
                })
            else:
                still_open.append(pos)

        # ── STOP LOSS ─────────────────────────────────────
        elif current_ask <= pos["stop"]:
            _blacklist.add(pos["ticker"])
            sell_at = current_bid if (current_bid and current_bid > 1) else current_ask
            sold    = place_sell(pos["ticker"], pos["side"], contracts, sell_at)
            actual_pnl  = (sell_at - entry) * contracts
            _daily_pnl += actual_pnl
            if not DRY_RUN:
                _balance = get_balance()
            else:
                _balance += sell_at * contracts
            _losses    += 1
            print(f"  🔴 STOP:  {pos['symbol']} {pos['side']} {entry}→{sell_at}c x{contracts} -${abs(actual_pnl)/100:.2f} | {'sold ✓' if sold else 'placed'} | bal:${_balance/100:.2f}")
            log_trade({
                "time": pos["entry_time"], "symbol": pos["symbol"],
                "side": pos["side"], "entry": entry, "exit": sell_at,
                "contracts": contracts, "pnl": actual_pnl,
                "ev": pos.get("ev", 0), "p_win": pos.get("p_win", 0),
                "kelly_f": pos.get("kelly_f", 0),
                "mins": round(hold_mins, 1), "reason": "STOP_LOSS",
                "balance": _balance, "signal": pos.get("signal", ""),
                "trend": pos.get("trend", ""),
            })

        else:
            still_open.append(pos)

    save_positions(still_open)


def run():
    global _balance

    floor = _starting * BALANCE_FLOOR_PCT
    mode  = "LIVE" if not DRY_RUN else "DEMO"

    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  Kalshi 15-Min Scalper [{mode}] — Adaptive Kelly")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  Temporal bias: ≥{TEMPORAL_BIAS_MIN:.0%} | Lookback: {BIAS_LOOKBACK_DAYS}d")
    print(f"  Vol filter:    skip if 24h vol > {VOL_SPIKE_MULT}× rolling baseline")
    print(f"  Kelly:         confidence-scaled {KELLY_FRACTION:.0%} fractional")
    print(f"                 Min ${MIN_BET_CENTS/100:.2f} | Max ${MAX_BET_CENTS/100:.2f}")
    print(f"  Min p_win:     {MIN_P_WIN:.0%} | Bayesian self-updating per window")
    print(f"  Price:         {MIN_ASK}c–{MAX_ASK}c | Vol: {MIN_VOLUME}+ | Time: {MIN_MINS_LEFT}-{MAX_MINS_LEFT}m")
    print(f"  Exits:         +8c profit target | -5c stop loss")
    print(f"  Risk:          ${DAILY_LOSS_LIMIT/100:.0f}/day max | Floor: ${floor/100:.2f}")
    print(f"  ⛔ Spend cap:  ${MAX_SPEND_CENTS/100:.2f} — pauses after this deployed")
    print(f"  Balance:       ${_balance/100:.2f}")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()

    # Load persistent intelligence layers
    load_temporal_bias()   # 24-month hourly bias windows (recomputes if >7d stale)
    load_bayes_priors()    # per-window win/loss counts for Bayesian p_win blending

    save_positions([])
    cancel_all_resting()

    scan         = 0
    last_profile = None

    while True:
        try:
            _reset_daily()
            _update_window_tickers()

            if not DRY_RUN:
                _balance = get_balance()

            if _total_spent >= MAX_SPEND_CENTS:
                print(f"\n  ⛔ SPEND CAP HIT: ${MAX_SPEND_CENTS/100:.2f} deployed this session.")
                print(f"  Account balance: ${_balance/100:.2f} | W/L: {_wins}/{_losses} | PnL: ${_daily_pnl/100:.2f}")
                print(f"  Bot paused — awaiting manual review before resuming.")
                break

            if _daily_pnl <= -DAILY_LOSS_LIMIT:
                print("  Daily loss limit hit. Done for today.")
                break

            if _balance < floor:
                print(f"  Balance floor hit (${_balance/100:.2f}). Done for today.")
                break

            if not _is_trading_hours():
                if scan % 60 == 0:
                    now_et = datetime.datetime.now(ET).strftime("%H:%M ET")
                    print(f"  [{now_et}] Waiting for 9am ET...")
                time.sleep(60)
                scan += 1
                continue

            # Suspend evening sessions on crash days
            if is_bad_market():
                if scan % 60 == 0:
                    day_pct = get_day_pct()
                    now_et  = datetime.datetime.now(ET).strftime("%H:%M ET")
                    print(f"  [{now_et}] Bad market day (BTC {day_pct:+.1f}%) — suspended until 9am")
                time.sleep(60)
                scan += 1
                continue

            h       = datetime.datetime.now(ET).hour
            profile = "PEAK" if 9 <= h < 16 else "AFTER_HOURS" if h < 20 else "EVENING"
            if profile != last_profile:
                print(f"\n  >>> {profile} session <<<\n")
                last_profile = profile

            scan += 1
            now_et = datetime.datetime.now(ET).strftime("%H:%M:%S ET")

            positions = load_positions()
            if positions:
                manage_exits()
                positions = load_positions()

            total   = _wins + _losses
            wr      = f"{round(_wins/total*100,1)}%" if total else "N/A"
            bal_pct = round(_balance / _starting * 100, 1)

            signals = []
            for info in SERIES.values():
                pct, sig = get_momentum(info["product"])
                trend    = get_trend(info["product"])
                signals.append(f"{info['symbol']}:{sig}({pct:+.2f}%)|{trend}")

            print(f"[{now_et}] #{scan} | ${_balance/100:.2f}({bal_pct}%) | W/L:{_wins}/{_losses}({wr}) | Open:{len(positions)} | Total:{_total}")
            print(f"  {' | '.join(signals)}")

            scan_entries()

            time.sleep(1)

        except KeyboardInterrupt:
            print("\n  Stopped.")
            total = _wins + _losses
            wr    = f"{round(_wins/total*100,1)}%" if total else "N/A"
            print(f"  Final: ${_balance/100:.2f} | W/L:{_wins}/{_losses}({wr}) | Total:{_total}")
            break
        except Exception as e:
            print(f"  Error: {e}")
            time.sleep(3)

run()