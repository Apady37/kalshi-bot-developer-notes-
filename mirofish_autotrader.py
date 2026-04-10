"""
MiroFish Auto-Trader
────────────────────
Watches mirofish_verdicts.jsonl for new tradeable verdicts and places
orders on Kalshi using the same Kelly/EV sizing as main.py.

Usage:
    python3 mirofish_autotrader.py            # live trading
    python3 mirofish_autotrader.py --dry-run  # show trades without placing

Limits (configurable below):
    MAX_BET_DOLLARS   = 50      hard cap per trade
    MAX_OPEN          = 3       max concurrent MiroFish-sourced positions
    MIN_EDGE          = 0.08    only trade if crowd probability beats market by ≥ 8%
    MARKET_GAP_CENTS  = 8       don't trade if market ask is within 8c of crowd prob

Requires:
    - Kalshi API keys in .env (same as main.py)
    - mirofish_verdicts.jsonl produced by mirofish_terminal.py or news_watcher.py
"""

import sys
import os
import json
import time
import datetime
import csv
import base64
import argparse
from pathlib import Path

import requests
from dotenv import load_dotenv
from urllib.parse import urlparse
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

load_dotenv()

# ── Risk limits ───────────────────────────────────────────────────────────────

MAX_BET_DOLLARS  = 50       # hard cap per MiroFish-sourced trade
MAX_OPEN         = 3        # max concurrent MiroFish positions
MIN_EDGE         = 0.08     # require crowd edge ≥ 8% over market price
KELLY_FRACTION   = 0.25     # quarter-Kelly for MiroFish signals (more uncertain than BS)
MIN_BET_CENTS    = 10
POLL_INTERVAL    = 15       # seconds between checking for new verdicts

# ── Kalshi config (mirrors main.py) ──────────────────────────────────────────

KEY_ID   = os.getenv("KALSHI_KEY_ID")
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

VERDICTS_FILE   = Path(__file__).parent / "mirofish_verdicts.jsonl"
POSITIONS_FILE  = Path(__file__).parent / "mirofish_positions.json"
TRADES_FILE     = Path(__file__).parent / "mirofish_trades.csv"

console = Console()

# ── Kalshi auth (same as main.py) ─────────────────────────────────────────────

def _load_key():
    key_data = os.getenv("KALSHI_PRIVATE_KEY")
    if key_data:
        key_data = key_data.replace("\\n", "\n")
        return serialization.load_pem_private_key(key_data.encode(), password=None,
                                                   backend=default_backend())
    with open(Path(__file__).parent / "private_key.pem", "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None,
                                                   backend=default_backend())

def _headers(method: str, path: str) -> dict:
    ts  = str(int(datetime.datetime.now().timestamp() * 1000))
    pk  = _load_key()
    msg = f"{ts}{method}{urlparse(BASE_URL + path).path}".encode()
    sig = pk.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                    salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return {
        "KALSHI-ACCESS-KEY":       KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type":            "application/json",
    }

def kalshi_get(path: str) -> dict:
    r = requests.get(BASE_URL + path, headers=_headers("GET", path), timeout=6)
    r.raise_for_status()
    return r.json()

def kalshi_post(path: str, body: dict) -> dict:
    r = requests.post(BASE_URL + path, headers=_headers("POST", path),
                      json=body, timeout=6)
    r.raise_for_status()
    return r.json()

# ── Kelly / EV (same formulas as main.py) ────────────────────────────────────

def calc_ev(ask_cents: float, p_win: float) -> float:
    """EV = p_win × 100 − ask  (net profit per contract in cents)"""
    return round(p_win * 100 - ask_cents, 2)

def kelly_bet_size(ask_cents: float, p_win: float, balance_cents: float) -> int:
    """Quarter-Kelly for MiroFish signals."""
    net_win  = 100 - ask_cents
    net_lose = ask_cents
    p_lose   = 1.0 - p_win
    if net_win <= 0 or net_lose <= 0:
        return MIN_BET_CENTS
    full_kelly = (p_win * net_win - p_lose * net_lose) / net_win
    frac_kelly = full_kelly * KELLY_FRACTION
    if frac_kelly <= 0:
        return 0
    bet = int(balance_cents * frac_kelly)
    return max(MIN_BET_CENTS, min(MAX_BET_DOLLARS * 100, bet))

# ── Position tracking ─────────────────────────────────────────────────────────

def load_positions() -> list:
    if POSITIONS_FILE.exists():
        try:
            return json.loads(POSITIONS_FILE.read_text())
        except Exception:
            pass
    return []

def save_positions(p: list) -> None:
    POSITIONS_FILE.write_text(json.dumps(p, indent=2))

def log_trade(row: dict) -> None:
    new = not TRADES_FILE.exists()
    with open(TRADES_FILE, "a", newline="") as f:
        fields = ["time", "scenario", "ticker", "side", "ask_cents",
                  "contracts", "spend_cents", "ev", "p_win", "edge", "dry_run"]
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})

# ── Market search ─────────────────────────────────────────────────────────────

def find_kalshi_markets(scenario: str) -> list[dict]:
    """
    Search Kalshi for markets related to the scenario keywords.
    Returns list of {ticker, title, yes_ask, no_ask} dicts.
    """
    # Extract keywords: drop short/common words
    stop = {"the", "a", "an", "is", "are", "was", "were", "will", "be",
            "in", "at", "on", "for", "to", "of", "and", "or", "by"}
    words = [w.strip(".,;:!?\"'") for w in scenario.lower().split()
             if len(w) > 3 and w not in stop]
    query = " ".join(words[:5])

    try:
        data = kalshi_get(f"/markets?limit=20&status=open&search={query}")
        markets = data.get("markets", [])
        results = []
        for m in markets:
            yes_ask = m.get("yes_ask", 0)
            no_ask  = m.get("no_ask", 0)
            if yes_ask and no_ask:
                results.append({
                    "ticker":    m["ticker"],
                    "title":     m.get("title", ""),
                    "yes_ask":   yes_ask,
                    "no_ask":    no_ask,
                    "volume":    m.get("volume", 0),
                    "close_time": m.get("close_time", ""),
                })
        return results
    except Exception as e:
        console.print(f"[dim red]Market search error: {e}[/dim red]")
        return []

# ── Trade executor ────────────────────────────────────────────────────────────

def place_order(ticker: str, side: str, contracts: int,
                price_cents: int, dry_run: bool) -> bool:
    if dry_run:
        return True
    price_key = "yes_price" if side == "YES" else "no_price"
    expiry = (datetime.datetime.now(datetime.timezone.utc) +
              datetime.timedelta(seconds=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "ticker":          ticker,
        "client_order_id": f"mf_{int(time.time()*1000)}",
        "type":            "limit",
        "action":          "buy",
        "side":            side.lower(),
        "count":           contracts,
        "expiration_time": expiry,
        price_key:         int(price_cents),
    }
    try:
        data   = kalshi_post("/portfolio/orders", body)
        order  = data.get("order", {})
        filled = float(order.get("fill_count_fp", 0))
        if filled == 0:
            time.sleep(2)
            recheck = kalshi_get(f"/portfolio/orders/{order.get('order_id','')}")
            filled  = float(recheck.get("order", {}).get("fill_count_fp", 0))
        return filled > 0
    except Exception as e:
        console.print(f"[red]Order error: {e}[/red]")
        return False

def get_balance_cents() -> int:
    try:
        data = kalshi_get("/portfolio/balance")
        return int(data.get("balance", 0))
    except Exception:
        return 10_000   # fallback $100

# ── Verdict processor ─────────────────────────────────────────────────────────

def process_verdict(verdict: dict, dry_run: bool) -> None:
    scenario  = verdict["scenario"]
    direction = verdict["direction"]      # YES or NO
    crowd_p   = verdict["crowd_probability"]
    edge      = verdict["edge"]

    console.print(f"\n[bold cyan]▶ New verdict:[/bold cyan] {scenario[:70]}")
    console.print(f"  Direction: [bold]{direction}[/bold]  "
                  f"Crowd prob: {crowd_p*100:.1f}%  Edge: {edge*100:.1f}%")

    # Check open position limit
    positions = load_positions()
    open_count = sum(1 for p in positions if not p.get("closed"))
    if open_count >= MAX_OPEN:
        console.print(f"  [yellow]Skip — {open_count} MiroFish positions already open (limit {MAX_OPEN})[/yellow]")
        return

    # Find matching Kalshi markets
    markets = find_kalshi_markets(scenario)
    if not markets:
        console.print(f"  [yellow]Skip — no matching Kalshi markets found[/yellow]")
        return

    # Display candidates
    tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    tbl.add_column("Ticker", style="dim")
    tbl.add_column("Title")
    tbl.add_column("YES ask", justify="right")
    tbl.add_column("NO ask", justify="right")
    tbl.add_column("Gap", justify="right")
    tbl.add_column("Trade?", justify="center")

    best = None
    for m in markets[:5]:
        ask_cents = m["yes_ask"] if direction == "YES" else m["no_ask"]
        market_p  = ask_cents / 100
        gap       = crowd_p - market_p if direction == "YES" else (1 - crowd_p) - market_p
        tradeable = gap >= MIN_EDGE

        if tradeable and (best is None or gap > best["gap"]):
            best = {**m, "gap": gap, "ask_cents": ask_cents, "market_p": market_p}

        color = "green" if tradeable else "dim"
        tbl.add_row(
            m["ticker"],
            m["title"][:45],
            f"{m['yes_ask']}¢",
            f"{m['no_ask']}¢",
            f"[{color}]{gap*100:+.1f}%[/{color}]",
            "[green]YES[/green]" if tradeable else "[dim]no[/dim]",
        )
    console.print(tbl)

    if best is None:
        console.print(f"  [yellow]Skip — no market has ≥{MIN_EDGE*100:.0f}% crowd gap[/yellow]")
        return

    # Size position
    balance  = get_balance_cents()
    ask      = best["ask_cents"]
    bet      = kelly_bet_size(ask, crowd_p, balance)
    if bet <= 0:
        console.print(f"  [yellow]Skip — Kelly returned 0[/yellow]")
        return

    contracts = bet // ask
    if contracts < 1:
        contracts = 1
    spend = contracts * ask
    ev    = calc_ev(ask, crowd_p)

    console.print(
        f"\n  [bold green]Trade:[/bold green] BUY {contracts}× {direction} "
        f"on [bold]{best['ticker']}[/bold] @ {ask}¢\n"
        f"  Spend: ${spend/100:.2f}  EV/contract: {ev:.1f}¢  "
        f"Gap: {best['gap']*100:.1f}%"
        + ("  [bold yellow][DRY RUN][/bold yellow]" if dry_run else "")
    )

    filled = place_order(best["ticker"], direction, contracts, ask, dry_run)
    if filled or dry_run:
        pos = {
            "ticker":     best["ticker"],
            "side":       direction,
            "entry":      ask,
            "contracts":  contracts,
            "scenario":   scenario,
            "crowd_p":    crowd_p,
            "gap":        best["gap"],
            "time":       datetime.datetime.utcnow().isoformat(),
            "closed":     False,
            "dry_run":    dry_run,
        }
        positions.append(pos)
        save_positions(positions)
        log_trade({
            "time":        pos["time"],
            "scenario":    scenario[:80],
            "ticker":      best["ticker"],
            "side":        direction,
            "ask_cents":   ask,
            "contracts":   contracts,
            "spend_cents": spend,
            "ev":          ev,
            "p_win":       crowd_p,
            "edge":        best["gap"],
            "dry_run":     dry_run,
        })
        console.print("[green]✓ Order placed[/green]" if not dry_run
                      else "[yellow]✓ Dry-run logged[/yellow]")
    else:
        console.print("[red]✗ Order failed to fill[/red]")

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Show trades without placing orders")
    args = parser.parse_args()
    dry_run = args.dry_run

    console.print(Panel(
        "[bold cyan]MiroFish Auto-Trader[/bold cyan]"
        + ("  [bold yellow][DRY RUN][/bold yellow]" if dry_run else "  [bold green][LIVE][/bold green]") +
        f"\n[dim]Watching {VERDICTS_FILE.name} · max ${MAX_BET_DOLLARS}/trade · "
        f"max {MAX_OPEN} open · min edge {MIN_EDGE*100:.0f}%[/dim]",
        style="cyan"
    ))

    if not KEY_ID:
        console.print("[bold red]KALSHI_KEY_ID not set in .env — cannot trade.[/bold red]")
        return

    # Track which verdict timestamps we've already processed
    processed: set[str] = set()

    # Load any previously processed verdicts so we don't re-trade on restart
    if VERDICTS_FILE.exists():
        for line in VERDICTS_FILE.read_text().splitlines():
            try:
                v = json.loads(line)
                processed.add(v.get("timestamp", ""))
            except Exception:
                pass

    console.print(f"[dim]Skipping {len(processed)} already-processed verdicts[/dim]\n")

    while True:
        if not VERDICTS_FILE.exists():
            time.sleep(POLL_INTERVAL)
            continue

        for line in VERDICTS_FILE.read_text().splitlines():
            try:
                verdict = json.loads(line.strip())
            except Exception:
                continue

            ts = verdict.get("timestamp", "")
            if ts in processed:
                continue
            processed.add(ts)

            if not verdict.get("tradeable"):
                console.print(f"[dim]{ts[:16]} — skip (edge too thin): "
                               f"{verdict.get('scenario','')[:50]}[/dim]")
                continue

            process_verdict(verdict, dry_run)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
