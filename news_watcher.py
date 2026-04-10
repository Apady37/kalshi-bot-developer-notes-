"""
MiroFish News Watcher
─────────────────────
Polls financial RSS feeds every 60s, filters for market-moving headlines,
and automatically triggers MiroFish simulations. Verdicts are written to
mirofish_verdicts.jsonl and printed to the terminal.

Usage:
    python3 news_watcher.py

Requires MiroFish running at http://localhost:5001 (docker compose up -d)
"""

import json
import time
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

# ── Config ────────────────────────────────────────────────────────────────────

POLL_INTERVAL = 60          # seconds between RSS checks
MIN_SIM_INTERVAL = 300      # don't re-simulate same topic within 5 min
VERDICTS_FILE = Path(__file__).parent / "mirofish_verdicts.jsonl"
MIROFISH_URL = "http://localhost:5001"

RSS_FEEDS = [
    {
        "name": "WSJ Markets",
        "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    },
    {
        "name": "Reuters Business",
        "url": "https://feeds.reuters.com/reuters/businessNews",
    },
    {
        "name": "Reuters Finance",
        "url": "https://feeds.reuters.com/reuters/financialNews",
    },
]

# Keywords that indicate a market-moving event worth simulating
TRIGGER_KEYWORDS = [
    "fed", "federal reserve", "fomc", "rate cut", "rate hike", "interest rate",
    "inflation", "cpi", "pce", "jobs", "unemployment", "nonfarm",
    "gdp", "recession", "powell",
    "bitcoin", "btc", "ethereum", "eth", "crypto",
    "sec", "etf", "approval", "ban",
    "tariff", "trade war", "sanctions",
    "earnings", "beats", "misses", "guidance",
    "acquisition", "merger", "bankruptcy",
    "oil", "opec", "energy",
    "kalshi", "polymarket", "prediction market",
]

console = Console()

# ── MiroFish pipeline (reused from mirofish_terminal.py) ─────────────────────

def run_mirofish_simulation(scenario: str) -> dict | None:
    """Run a full MiroFish pipeline and return the verdict dict, or None on failure."""
    import os
    import tempfile
    import textwrap

    try:
        # Build seed document
        seed = textwrap.dedent(f"""\
            # Breaking Market News — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}

            ## Headline

            {scenario}

            ## Context

            This headline just broke. Market participants across prediction markets,
            equity markets, and social media are processing the implications in real time.

            ## Simulation Requirement

            Simulate how 56 diverse market participants react to this development.
            Predict whether the crowd consensus will resolve YES or NO on the relevant
            prediction market contract, and estimate the probability with reasoning.
        """)

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md",
                                          prefix="news_", delete=False, encoding="utf-8")
        tmp.write(seed)
        tmp.close()

        s = requests.Session()

        # 1. Ontology
        with open(tmp.name, "rb") as f:
            r = requests.post(
                f"{MIROFISH_URL}/api/graph/ontology/generate",
                files={"files": ("scenario.md", f, "text/plain")},
                data={
                    "simulation_requirement": f"Predict crowd reaction and YES/NO probability for: {scenario}",
                    "project_name": f"news_{int(time.time())}",
                },
                timeout=60,
            )
        r.raise_for_status()
        onto = r.json()["data"]
        project_id = onto["project_id"]

        # 2. Build graph
        r = s.post(f"{MIROFISH_URL}/api/graph/build",
                   json={"project_id": project_id}, timeout=30)
        r.raise_for_status()
        build = r.json()["data"]
        task_id = build.get("task_id")

        # Poll graph task
        if task_id:
            for _ in range(60):
                r = s.get(f"{MIROFISH_URL}/api/graph/task/{task_id}", timeout=10)
                d = r.json().get("data", {})
                if d.get("status") in ("completed", "failed", "error"):
                    break
                time.sleep(2)

        graph_id = build.get("graph_id") or onto.get("graph_id")
        if not graph_id:
            r = s.get(f"{MIROFISH_URL}/api/graph/project/{project_id}", timeout=10)
            graph_id = r.json()["data"].get("graph_id")

        # 3. Create + prepare simulation
        r = s.post(f"{MIROFISH_URL}/api/simulation/create",
                   json={"project_id": project_id, "graph_id": graph_id,
                         "enable_twitter": True, "enable_reddit": True}, timeout=30)
        r.raise_for_status()
        simulation_id = r.json()["data"]["simulation_id"]

        s.post(f"{MIROFISH_URL}/api/simulation/prepare",
               json={"simulation_id": simulation_id, "use_llm_for_profiles": True,
                     "parallel_profile_count": 8}, timeout=30)

        # Poll prepare
        for _ in range(80):
            r = s.post(f"{MIROFISH_URL}/api/simulation/prepare/status",
                       json={"simulation_id": simulation_id}, timeout=10)
            if r.json().get("data", {}).get("status") == "completed":
                break
            time.sleep(3)

        # 4. Run simulation
        s.post(f"{MIROFISH_URL}/api/simulation/start",
               json={"simulation_id": simulation_id, "platform": "parallel",
                     "max_rounds": 8, "enable_graph_memory_update": True}, timeout=30)

        # Poll simulation
        for _ in range(150):
            r = s.get(f"{MIROFISH_URL}/api/simulation/{simulation_id}/run-status/detail",
                      timeout=10)
            d = r.json().get("data", {})
            if d.get("status") in ("completed", "finished", "done") or \
               d.get("current_round", 0) >= 8:
                break
            time.sleep(4)

        # 5. Generate + fetch report
        s.post(f"{MIROFISH_URL}/api/report/generate",
               json={"simulation_id": simulation_id}, timeout=30)

        report = {}
        for _ in range(75):
            r = s.get(f"{MIROFISH_URL}/api/report/by-simulation/{simulation_id}",
                      timeout=10)
            if r.status_code == 200:
                d = r.json().get("data", {})
                if d.get("status") in ("completed", "done", "finished"):
                    report = d
                    break
            time.sleep(4)

        # Parse verdict
        return _parse_verdict(report, scenario)

    except Exception as e:
        console.print(f"[red]Simulation error for '{scenario[:50]}': {e}[/red]")
        return None
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def _parse_verdict(report: dict, scenario: str) -> dict:
    text = (report.get("content") or report.get("markdown") or
            report.get("report_content") or "").lower()
    verdict_json = report.get("verdict") or {}

    probability = verdict_json.get("probability") or verdict_json.get("confidence")
    direction = verdict_json.get("direction") or verdict_json.get("outcome")

    if probability is None:
        bullish = ["positive", "bullish", "likely yes", "high probability",
                   "optimistic", "strong support", "majority agree", "consensus yes"]
        bearish = ["negative", "bearish", "likely no", "low probability",
                   "pessimistic", "majority oppose", "consensus no", "uncertain"]
        b = sum(text.count(w) for w in bullish)
        n = sum(text.count(w) for w in bearish)
        total = b + n or 1
        probability = b / total
        direction = "YES" if b >= n else "NO"

    if isinstance(probability, str):
        try:
            probability = float(probability.strip("%")) / 100
        except ValueError:
            probability = 0.5

    direction = str(direction or "").upper()
    if not direction or direction not in ("YES", "NO"):
        direction = "YES" if float(probability) >= 0.5 else "NO"

    edge = abs(float(probability) - 0.5)
    return {
        "direction": direction,
        "crowd_probability": round(float(probability), 4),
        "edge": round(edge, 4),
        "confidence": "HIGH" if edge > 0.2 else "MEDIUM" if edge > 0.1 else "LOW",
        "scenario": scenario,
        "timestamp": datetime.utcnow().isoformat(),
        "tradeable": edge > 0.07,
        "source": "news_watcher",
    }

# ── RSS feed fetcher ──────────────────────────────────────────────────────────

def fetch_headlines(feed_url: str) -> list[dict]:
    try:
        r = requests.get(feed_url, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0 MiroFish-NewsWatcher/1.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = []
        for item in root.iter("item"):
            title = item.findtext("title") or ""
            link  = item.findtext("link") or ""
            pub   = item.findtext("pubDate") or ""
            items.append({"title": title.strip(), "link": link, "pubDate": pub})
        return items
    except Exception as e:
        console.print(f"[dim red]Feed error {feed_url[:40]}: {e}[/dim red]")
        return []


def is_market_moving(headline: str) -> bool:
    h = headline.lower()
    return any(kw in h for kw in TRIGGER_KEYWORDS)


def headline_id(headline: str) -> str:
    return hashlib.md5(headline.lower().encode()).hexdigest()[:12]

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    console.print(Panel(
        "[bold cyan]MiroFish News Watcher[/bold cyan]  ·  "
        "[yellow]Auto-triggering simulations on market-moving headlines[/yellow]",
        style="cyan"
    ))
    console.print(f"[dim]Polling {len(RSS_FEEDS)} feeds every {POLL_INTERVAL}s  "
                  f"·  verdicts → {VERDICTS_FILE.name}[/dim]\n")

    # Check MiroFish
    try:
        r = requests.get(f"{MIROFISH_URL}/api/graph/project/list", timeout=3)
        if r.status_code >= 500:
            raise Exception(f"HTTP {r.status_code}")
        console.print("[green]✓ MiroFish connected[/green]\n")
    except Exception as e:
        console.print(f"[bold red]Cannot reach MiroFish: {e}[/bold red]\n"
                      "Start it:  cd ~/Desktop/mirofish && docker compose up -d\n")
        return

    seen: dict[str, float] = {}   # headline_id → timestamp last simulated

    while True:
        cycle_start = time.time()
        new_headlines = []

        for feed in RSS_FEEDS:
            for item in fetch_headlines(feed["url"]):
                title = item["title"]
                if not title or not is_market_moving(title):
                    continue
                hid = headline_id(title)
                last = seen.get(hid, 0)
                if time.time() - last < MIN_SIM_INTERVAL:
                    continue
                new_headlines.append({"title": title, "feed": feed["name"], "id": hid})

        if new_headlines:
            console.print(f"\n[bold]{datetime.utcnow().strftime('%H:%M:%S UTC')}[/bold] "
                          f"— {len(new_headlines)} new headline(s) to simulate\n")

            for h in new_headlines:
                seen[h["id"]] = time.time()
                console.print(f"[cyan]▶ {h['feed']}:[/cyan] {h['title']}")
                console.print(f"  [dim]Running MiroFish simulation…[/dim]")

                verdict = run_mirofish_simulation(h["title"])
                if verdict is None:
                    continue

                # Save verdict
                with open(VERDICTS_FILE, "a") as f:
                    f.write(json.dumps(verdict) + "\n")

                # Display result
                color = "green" if verdict["direction"] == "YES" else "red"
                icon  = "▲" if verdict["direction"] == "YES" else "▼"
                tradeable = "[bold green]TRADE[/bold green]" if verdict["tradeable"] else "[dim]skip[/dim]"

                tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
                tbl.add_column("k", style="dim")
                tbl.add_column("v", style="bold")
                tbl.add_row("Direction",   f"[{color}]{icon} {verdict['direction']}[/{color}]")
                tbl.add_row("Probability", f"{verdict['crowd_probability']*100:.1f}%")
                tbl.add_row("Edge",        f"{verdict['edge']*100:.1f}%")
                tbl.add_row("Signal",      tradeable)
                console.print(tbl)
        else:
            console.print(f"[dim]{datetime.utcnow().strftime('%H:%M:%S')} — "
                          f"no new market-moving headlines[/dim]")

        # Sleep until next poll
        elapsed = time.time() - cycle_start
        wait = max(0, POLL_INTERVAL - elapsed)
        time.sleep(wait)


if __name__ == "__main__":
    main()
