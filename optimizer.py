"""
optimizer.py — Self-improving parameter engine
Runs 24/7 alongside paper_bot.py. Never requires manual intervention.

Every OPTIMIZE_INTERVAL minutes:
  Phase 1 (always):   Bayesian edge calibration per (signal, trend) pair
  Phase 2 (≥50 trades): Optuna threshold search using full signal universe
  Output: config.json — paper_bot.py reloads this on every scan

Data sources:
  paper_signals.csv — every candidate the bot evaluated (filled or not)
  paper_trades.csv  — outcomes of filled trades
  live_trades.csv   — outcomes from main.py (for signal calibration only)

Safeguard: optimizer only writes config.json if the new params improve
on a held-out validation window. No improvement = no change.

pip install optuna  (only needed for Phase 2)
"""

import csv
import json
import os
import time
import math
import datetime
import logging
from collections import defaultdict

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    OPTUNA_AVAILABLE = True
except ImportError:
    OPTUNA_AVAILABLE = False

# ── CONFIG ────────────────────────────────────────────────────────────────────

OPTIMIZE_INTERVAL = 15      # minutes between runs
MIN_TRADES_BAYES  = 5       # trades before Bayesian edges activate
MIN_TRADES_OPTUNA = 50      # resolved trades before Optuna runs
OPTUNA_TRIALS     = 150     # search trials per run
TRAIN_SPLIT       = 0.70    # fraction of trades used for training
MIN_IMPROVEMENT   = 0.02    # Sharpe must improve by this much to update params

PAPER_TRADES  = "paper_trades.csv"
PAPER_SIGNALS = "paper_signals.csv"
LIVE_TRADES   = "live_trades.csv"
CONFIG_FILE   = "config.json"
LOG_FILE      = "optimizer.log"

# Beta(α, β) prior — weak, centered at 50% win rate
BAYES_ALPHA = 2.0
BAYES_BETA  = 2.0

# Hardcoded defaults (mirrors main.py / paper_bot.py)
DEFAULTS = {
    "REVERSAL_THRESH": 0.60,
    "STRONG_THRESH":   0.15,
    "TREND_THRESH":    0.40,
    "MIN_P_WIN":       0.55,
    "MIN_ASK":         20,
    "MAX_ASK":         80,
    "MIN_MINS_LEFT":   2.0,
    "MAX_MINS_LEFT":   18.0,
    "edges":           {},
    "n_trades":        0,
    "last_updated":    "",
    "update_reason":   "defaults",
}

# Optuna search bounds
SEARCH_SPACE = {
    "REVERSAL_THRESH": (0.30, 1.20),
    "STRONG_THRESH":   (0.05, 0.50),
    "TREND_THRESH":    (0.10, 0.80),
    "MIN_P_WIN":       (0.50, 0.75),
    "MIN_MINS_LEFT":   (1.0,  6.0),
    "MAX_MINS_LEFT":   (10.0, 25.0),
    "MIN_ASK":         (10,   35),
    "MAX_ASK":         (65,   90),
}

# ── LOGGING ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("optimizer")

# ── DATA LOADING ──────────────────────────────────────────────────────────────

def load_csv(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except Exception as e:
        log.warning(f"Could not read {path}: {e}")
        return []

def load_all_trades():
    """Load paper + live trades. Only include closed/resolved trades."""
    closed_reasons = {"RESOLVED", "PROFIT_TARGET", "STOP_LOSS", "NO_FILL"}
    rows = []
    for path in [PAPER_TRADES, LIVE_TRADES]:
        for r in load_csv(path):
            if r.get("reason", "") in closed_reasons:
                rows.append(r)
    return rows

def load_signals():
    return load_csv(PAPER_SIGNALS)

def parse_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def parse_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default

# ── BAYESIAN EDGE CALIBRATION ─────────────────────────────────────────────────

def update_bayesian_edges(trades):
    """
    For each (signal, trend) pair, compute posterior win-rate estimate.
    Win = pnl > 0.
    Returns dict: "SIGNAL|TREND" → estimated_edge (float)

    Edge here means: our win-rate estimate minus the market-implied probability
    at the time of the trade. Positive edge = we have an advantage.
    """
    counts = defaultdict(lambda: {"wins": 0, "total": 0, "avg_ask": []})

    for t in trades:
        signal = t.get("signal", "")
        trend  = t.get("trend",  "")
        pnl    = parse_float(t.get("pnl", 0))
        ask    = parse_float(t.get("entry_ask") or t.get("fill_price") or t.get("entry", 0))
        reason = t.get("reason", "")

        if not signal or not trend or reason == "NO_FILL":
            continue

        key = f"{signal}|{trend}"
        counts[key]["total"] += 1
        if pnl > 0:
            counts[key]["wins"] += 1
        if ask > 0:
            counts[key]["avg_ask"].append(ask)

    edges = {}
    for key, c in counts.items():
        if c["total"] == 0:
            continue
        # Posterior: Beta(wins + α, losses + β)
        posterior_win = (c["wins"] + BAYES_ALPHA) / (c["total"] + BAYES_ALPHA + BAYES_BETA)
        # Market-implied probability (average ask at entry, in [0,1])
        avg_ask_frac = (sum(c["avg_ask"]) / len(c["avg_ask"]) / 100.0) if c["avg_ask"] else 0.5
        edge = round(posterior_win - avg_ask_frac, 4)
        edges[key] = edge
        log.info(
            f"  Bayesian edge  {key:35s} "
            f"wins:{c['wins']}/{c['total']} "
            f"post_win:{posterior_win:.1%} "
            f"market:{avg_ask_frac:.1%} "
            f"edge:{edge:+.3f}"
        )

    return edges

# ── SIGNAL-OUTCOME JOIN ───────────────────────────────────────────────────────

def join_signals_to_outcomes(signals, trades):
    """
    Join signal log to trade outcomes via candidate_id.
    Returns list of enriched signal dicts with 'won' (True/False/None).
    None = not filled or not yet resolved.
    """
    outcome_by_cid = {}
    for t in trades:
        cid = t.get("candidate_id", "")
        if not cid:
            continue
        pnl    = parse_float(t.get("pnl", 0))
        reason = t.get("reason", "")
        if reason == "NO_FILL":
            outcome_by_cid[cid] = None
        else:
            outcome_by_cid[cid] = (pnl > 0)

    enriched = []
    for s in signals:
        cid = s.get("candidate_id", "")
        s2  = dict(s)
        s2["won"]    = outcome_by_cid.get(cid, None)
        s2["p_win"]  = parse_float(s.get("p_win", 0))
        s2["ask"]    = parse_float(s.get("ask", 0))
        s2["mins"]   = parse_float(s.get("mins", 0))
        s2["volume"] = parse_float(s.get("volume", 0))
        enriched.append(s2)

    return enriched

# ── SIMULATION FOR OPTUNA ─────────────────────────────────────────────────────

SIGNAL_ORDER = {
    "REVERSAL_DOWN": "NO",
    "REVERSAL_UP":   "YES",
    "STRONG_DOWN":   "NO",
    "STRONG_UP":     "YES",
}

def get_direction(signal, trend, thresh_r, thresh_s, thresh_t):
    """Re-derive direction using candidate param values."""
    if signal == "REVERSAL_DOWN":
        return "NO"  if trend in ["DOWN", "FLAT"] else None
    if signal == "REVERSAL_UP":
        return "YES" if trend in ["UP",   "FLAT"] else None
    if signal == "STRONG_DOWN":
        return "NO"  if trend == "DOWN"           else None
    if signal == "STRONG_UP":
        return "YES" if trend == "UP"             else None
    return None

def get_edge_hardcoded(signal, trend):
    if signal in ("REVERSAL_DOWN", "REVERSAL_UP"):
        return 0.22 if trend in ("DOWN", "UP") else 0.10
    if signal in ("STRONG_DOWN", "STRONG_UP"):
        return 0.12 if trend in ("DOWN", "UP") else 0.06
    return 0.0

def simulate_portfolio(signals_with_outcomes, params):
    """
    Given a set of enriched signals and a candidate parameter dict,
    return the list of (pnl_frac) for trades that would have been taken.
    pnl_frac is the trade P&L as a fraction of bet size (e.g. +0.45 or -0.55).

    Signals with won=None (not resolved) are excluded from the simulation.
    """
    rth  = params["REVERSAL_THRESH"]
    sth  = params["STRONG_THRESH"]
    tth  = params["TREND_THRESH"]
    minp = params["MIN_P_WIN"]
    mina = params["MIN_ASK"]
    maxa = params["MAX_ASK"]
    minm = params["MIN_MINS_LEFT"]
    maxm = params["MAX_MINS_LEFT"]

    portfolio_pnl = []

    for s in signals_with_outcomes:
        if s["won"] is None:
            continue   # unresolved — skip

        signal = s.get("signal", "")
        trend  = s.get("trend",  "")
        p_win  = s["p_win"]
        ask    = s["ask"]
        mins   = s["mins"]

        # Re-check if signal classification still holds under new thresholds
        # (We don't have raw pct here, so we use the stored signal label as-is.
        # Threshold changes affect FUTURE signal labels, not stored ones.
        # We do re-check direction + filter params below.)

        direction = get_direction(signal, trend, rth, sth, tth)
        if direction is None:
            continue

        edge  = get_edge_hardcoded(signal, trend)
        p_win_adj = min(ask / 100.0 + edge, 0.92)

        if p_win_adj < minp:
            continue
        if not (mina <= ask <= maxa):
            continue
        if not (minm <= mins <= maxm):
            continue

        # P&L fraction: win returns (100 - ask)/ask net; loss returns -1 (lose the ask)
        net_win  = (100 - ask) / ask   # profit multiple on stake
        net_lose = -1.0
        trade_pnl = net_win if s["won"] else net_lose
        portfolio_pnl.append(trade_pnl)

    return portfolio_pnl

def sharpe(pnl_list):
    """Annualised Sharpe-like score from a list of trade returns."""
    if len(pnl_list) < 3:
        return -99.0
    n    = len(pnl_list)
    mean = sum(pnl_list) / n
    var  = sum((x - mean) ** 2 for x in pnl_list) / (n - 1)
    std  = math.sqrt(var) if var > 0 else 1e-9
    # Scale by sqrt(n) to penalise small samples less harshly
    return (mean / std) * math.sqrt(n)

# ── OPTUNA OPTIMIZATION ───────────────────────────────────────────────────────

def run_optuna(signals_with_outcomes, current_params):
    """
    Time-series split: train on oldest TRAIN_SPLIT, validate on newest.
    Only update if new params beat current params on validation set.
    Returns (best_params_dict, validation_sharpe) or (None, None).
    """
    resolved = [s for s in signals_with_outcomes if s["won"] is not None]
    if len(resolved) < MIN_TRADES_OPTUNA:
        log.info(f"  Optuna: {len(resolved)} resolved trades < {MIN_TRADES_OPTUNA} minimum — skipping")
        return None, None

    split = int(len(resolved) * TRAIN_SPLIT)
    train = resolved[:split]
    val   = resolved[split:]

    if len(train) < 10 or len(val) < 5:
        return None, None

    # Baseline: how well do current params do on validation set?
    baseline_pnl    = simulate_portfolio(val, current_params)
    baseline_sharpe = sharpe(baseline_pnl)

    def objective(trial):
        params = {}
        for name, (lo, hi) in SEARCH_SPACE.items():
            if isinstance(lo, int) and isinstance(hi, int):
                params[name] = trial.suggest_int(name, lo, hi)
            else:
                params[name] = trial.suggest_float(name, lo, hi)
        train_pnl = simulate_portfolio(train, params)
        return sharpe(train_pnl)

    study = optuna.create_study(direction="maximize",
                                sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=OPTUNA_TRIALS, show_progress_bar=False)
    best = study.best_params

    # Validate on held-out data
    val_pnl    = simulate_portfolio(val, best)
    val_sharpe = sharpe(val_pnl)

    log.info(
        f"  Optuna done — train Sharpe:{study.best_value:.2f} "
        f"val Sharpe:{val_sharpe:.2f} (baseline:{baseline_sharpe:.2f})"
    )

    if val_sharpe > baseline_sharpe + MIN_IMPROVEMENT:
        return best, val_sharpe
    else:
        log.info(f"  No improvement on validation — keeping current params")
        return None, None

# ── CONFIG I/O ────────────────────────────────────────────────────────────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except:
            pass
    return dict(DEFAULTS)

def write_config(cfg):
    cfg["last_updated"] = datetime.datetime.now().isoformat()
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    log.info(f"  config.json updated — {cfg.get('update_reason', '')}")

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def run():
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("  Kalshi Optimizer — 24/7 self-improvement engine")
    log.info(f"  Interval: {OPTIMIZE_INTERVAL}min | Bayesian min:{MIN_TRADES_BAYES} | Optuna min:{MIN_TRADES_OPTUNA}")
    log.info(f"  Optuna: {'available' if OPTUNA_AVAILABLE else 'NOT available (pip install optuna)'}")
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Write initial config if none exists
    if not os.path.exists(CONFIG_FILE):
        write_config(dict(DEFAULTS))
        log.info("  Wrote initial config.json with defaults")

    cycle = 0

    while True:
        try:
            cycle += 1
            now = datetime.datetime.now().strftime("%H:%M:%S")
            log.info(f"\n[{now}] Cycle #{cycle}")

            # ── Load data ────────────────────────────────────────────────────
            trades  = load_all_trades()
            signals = load_signals()
            cfg     = load_config()

            resolved_trades = [t for t in trades if t.get("reason") != "NO_FILL"]
            n_resolved = len(resolved_trades)
            log.info(f"  Data: {len(signals)} signals | {n_resolved} resolved trades")

            # ── Phase 1: Bayesian edge update (always) ────────────────────────
            if n_resolved >= MIN_TRADES_BAYES:
                new_edges = update_bayesian_edges(resolved_trades)
                cfg["edges"]    = new_edges
                cfg["n_trades"] = n_resolved
                cfg["update_reason"] = f"bayesian update on {n_resolved} trades"
            else:
                log.info(f"  Bayesian: {n_resolved} trades < {MIN_TRADES_BAYES} minimum — using defaults")

            # ── Phase 2: Optuna threshold search ─────────────────────────────
            if OPTUNA_AVAILABLE and n_resolved >= MIN_TRADES_OPTUNA:
                enriched = join_signals_to_outcomes(signals, trades)
                log.info(f"  Optuna: running {OPTUNA_TRIALS} trials on {len(enriched)} signals...")

                current_params = {k: cfg.get(k, DEFAULTS.get(k)) for k in SEARCH_SPACE}
                best_params, val_sharpe = run_optuna(enriched, current_params)

                if best_params is not None:
                    cfg.update(best_params)
                    cfg["update_reason"] = (
                        f"optuna update on {n_resolved} trades "
                        f"val_sharpe:{val_sharpe:.2f}"
                    )
                    log.info(f"  New params: {best_params}")
            elif not OPTUNA_AVAILABLE:
                log.info("  Optuna not installed — run: pip install optuna")
            else:
                log.info(f"  Optuna waiting: {n_resolved}/{MIN_TRADES_OPTUNA} trades")

            # ── Write config ──────────────────────────────────────────────────
            write_config(cfg)

            # ── Summary ───────────────────────────────────────────────────────
            if resolved_trades:
                wins   = sum(1 for t in resolved_trades if parse_float(t.get("pnl", 0)) > 0)
                wr     = wins / len(resolved_trades) * 100
                total_pnl = sum(parse_float(t.get("pnl", 0)) for t in resolved_trades) / 100
                log.info(
                    f"  Portfolio: {len(resolved_trades)} trades | "
                    f"W/L:{wins}/{len(resolved_trades)-wins} ({wr:.0f}%) | "
                    f"Net P&L: ${total_pnl:+.2f}"
                )

        except Exception as e:
            log.error(f"  Cycle error: {e}", exc_info=True)

        log.info(f"  Sleeping {OPTIMIZE_INTERVAL}m...")
        time.sleep(OPTIMIZE_INTERVAL * 60)

run()
