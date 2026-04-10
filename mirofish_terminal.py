"""
MiroFish Trading Terminal
─────────────────────────
Feeds market scenarios to MiroFish's 56-agent swarm, reads crowd sentiment,
and converts the verdict into Kalshi trade signals.

Usage:
    python mirofish_terminal.py

Requires MiroFish running at http://localhost:5001
    cd ~/Desktop/mirofish && docker compose up -d
"""

import os
import sys
import json
import time
import tempfile
import textwrap
import threading
from datetime import datetime
from pathlib import Path

import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.live import Live
from rich.layout import Layout
from rich.text import Text
from rich.prompt import Prompt
from rich import box

MIROFISH_URL = "http://localhost:5001"
console = Console()

# ─── MiroFish API Client ──────────────────────────────────────────────────────

class MiroFishClient:
    def __init__(self, base_url: str = MIROFISH_URL):
        self.base = base_url
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def ping(self) -> bool:
        try:
            r = self.session.get(f"{self.base}/api/graph/project/list", timeout=3)
            return r.status_code < 500
        except Exception:
            return False

    # ── Graph / ontology ──────────────────────────────────────────────────────

    def generate_ontology(self, scenario_file_path: str, requirement: str, project_name: str) -> dict:
        with open(scenario_file_path, "rb") as f:
            r = requests.post(
                f"{self.base}/api/graph/ontology/generate",
                files={"files": (Path(scenario_file_path).name, f, "text/plain")},
                data={"simulation_requirement": requirement, "project_name": project_name},
            )
        r.raise_for_status()
        return r.json()["data"]

    def build_graph(self, project_id: str) -> dict:
        r = self.session.post(f"{self.base}/api/graph/build", json={"project_id": project_id})
        r.raise_for_status()
        return r.json()["data"]

    def poll_task(self, task_id: str, timeout: int = 120) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.session.get(f"{self.base}/api/graph/task/{task_id}")
            r.raise_for_status()
            data = r.json()["data"]
            status = data.get("status", "")
            if status in ("completed", "failed", "error"):
                return data
            time.sleep(2)
        raise TimeoutError(f"Task {task_id} timed out after {timeout}s")

    # ── Simulation lifecycle ───────────────────────────────────────────────────

    def create_simulation(self, project_id: str, graph_id: str) -> dict:
        r = self.session.post(
            f"{self.base}/api/simulation/create",
            json={"project_id": project_id, "graph_id": graph_id,
                  "enable_twitter": True, "enable_reddit": True},
        )
        r.raise_for_status()
        return r.json()["data"]

    def prepare_simulation(self, simulation_id: str) -> dict:
        r = self.session.post(
            f"{self.base}/api/simulation/prepare",
            json={"simulation_id": simulation_id, "use_llm_for_profiles": True,
                  "parallel_profile_count": 8},
        )
        r.raise_for_status()
        return r.json()["data"]

    def poll_prepare(self, simulation_id: str, timeout: int = 180) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.session.post(
                f"{self.base}/api/simulation/prepare/status",
                json={"simulation_id": simulation_id},
            )
            r.raise_for_status()
            data = r.json().get("data", {})
            status = data.get("status", "")
            if status == "completed":
                return
            if status in ("failed", "error"):
                raise RuntimeError(f"Prepare failed: {data.get('message','')}")
            time.sleep(3)
        raise TimeoutError("Simulation prepare timed out")

    def start_simulation(self, simulation_id: str, max_rounds: int = 10) -> dict:
        r = self.session.post(
            f"{self.base}/api/simulation/start",
            json={"simulation_id": simulation_id, "platform": "parallel",
                  "max_rounds": max_rounds, "enable_graph_memory_update": True},
        )
        r.raise_for_status()
        return r.json()["data"]

    def get_run_status(self, simulation_id: str) -> dict:
        r = self.session.get(f"{self.base}/api/simulation/{simulation_id}/run-status/detail")
        r.raise_for_status()
        return r.json().get("data", {})

    def get_agent_stats(self, simulation_id: str) -> dict:
        r = self.session.get(f"{self.base}/api/simulation/{simulation_id}/agent-stats")
        r.raise_for_status()
        return r.json().get("data", {})

    def poll_simulation(self, simulation_id: str, max_rounds: int,
                        on_update=None, timeout: int = 600) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                status = self.get_run_status(simulation_id)
                current_round = status.get("current_round", 0)
                if on_update:
                    on_update(status, current_round)
                if status.get("status") in ("completed", "finished", "done") or \
                   current_round >= max_rounds:
                    return
            except Exception:
                pass
            time.sleep(4)
        raise TimeoutError("Simulation timed out")

    # ── Report ────────────────────────────────────────────────────────────────

    def generate_report(self, simulation_id: str) -> dict:
        r = self.session.post(
            f"{self.base}/api/report/generate",
            json={"simulation_id": simulation_id},
        )
        r.raise_for_status()
        return r.json()["data"]

    def poll_report(self, simulation_id: str, timeout: int = 300) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.session.get(f"{self.base}/api/report/by-simulation/{simulation_id}")
            if r.status_code == 200:
                data = r.json().get("data", {})
                if data.get("status") in ("completed", "done", "finished"):
                    return data
            time.sleep(4)
        raise TimeoutError("Report generation timed out")

    def chat_with_report(self, simulation_id: str, message: str,
                          history: list | None = None) -> str:
        r = self.session.post(
            f"{self.base}/api/report/chat",
            json={"simulation_id": simulation_id, "message": message,
                  "chat_history": history or []},
        )
        r.raise_for_status()
        return r.json()["data"].get("response", "")

# ─── Verdict parser ───────────────────────────────────────────────────────────

def parse_verdict(report_data: dict, scenario: str) -> dict:
    """
    Extracts YES/NO directional bias and confidence from the MiroFish report.
    Returns a Kalshi-ready signal dict.
    """
    report_text = (
        report_data.get("content", "") or
        report_data.get("markdown", "") or
        report_data.get("report_content", "") or ""
    ).lower()

    verdict_json = report_data.get("verdict", {}) or {}

    # Try structured verdict first
    probability = verdict_json.get("probability") or verdict_json.get("confidence")
    direction = verdict_json.get("direction") or verdict_json.get("outcome")

    # Fallback: count sentiment keywords in report text
    if probability is None:
        bullish_words = ["positive", "bullish", "likely yes", "high probability",
                         "optimistic", "upward", "confident", "strong support",
                         "majority agree", "consensus yes"]
        bearish_words = ["negative", "bearish", "likely no", "low probability",
                         "pessimistic", "downward", "uncertain", "mixed reaction",
                         "majority oppose", "consensus no"]
        b = sum(report_text.count(w) for w in bullish_words)
        n = sum(report_text.count(w) for w in bearish_words)
        total = b + n or 1
        probability = b / total
        direction = "YES" if b > n else "NO"

    if isinstance(probability, str):
        try:
            probability = float(probability.strip("%")) / 100
        except ValueError:
            probability = 0.5

    if direction is None:
        direction = "YES" if probability >= 0.5 else "NO"

    direction = str(direction).upper()
    if "yes" in direction or "positive" in direction or "bullish" in direction:
        direction = "YES"
    elif "no" in direction or "negative" in direction or "bearish" in direction:
        direction = "NO"
    else:
        direction = "YES" if float(probability) >= 0.5 else "NO"

    # Edge = how far the crowd is from 50/50
    if direction == "YES":
        edge = float(probability) - 0.5
    else:
        edge = 0.5 - float(probability)

    confidence_label = (
        "HIGH" if edge > 0.2 else
        "MEDIUM" if edge > 0.1 else
        "LOW"
    )

    return {
        "direction": direction,
        "crowd_probability": float(probability),
        "edge": round(edge, 4),
        "confidence": confidence_label,
        "scenario": scenario,
        "timestamp": datetime.utcnow().isoformat(),
        "tradeable": edge > 0.07,  # Only signal if crowd-vs-market edge > 7%
    }


def build_scenario_document(scenario: str, context: str = "") -> str:
    """Builds the seed markdown document that MiroFish will use to spawn agents."""
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return textwrap.dedent(f"""\
        # Market Scenario Report — {timestamp}

        ## Breaking Development

        {scenario.strip()}

        ## Context

        {context.strip() if context.strip() else
         "This scenario has just broken in financial markets. Market participants "
         "are processing the implications across prediction markets, equity markets, "
         "and sentiment platforms."}

        ## Relevant Market Background

        - This scenario has immediate implications for near-term prediction markets
        - Retail traders, institutional desks, and quant funds are all reacting
        - Social media is beginning to show early sentiment signals
        - Analysts are issuing initial interpretations
        - Options and prediction markets will reprice as the crowd forms consensus

        ## Simulation Requirement

        Simulate how 56 diverse market participants — spanning retail traders,
        quant analysts, macro fund managers, news commentators, and crypto-native
        traders — react to this development over the next 24 hours.

        Predict whether the crowd consensus will resolve YES or NO on the relevant
        prediction market contract, and estimate the probability with reasoning.
    """)

# ─── Rich terminal UI ─────────────────────────────────────────────────────────

def render_header() -> Panel:
    t = Text()
    t.append("  MiroFish ", style="bold cyan")
    t.append("Trading Terminal", style="bold white")
    t.append("  ·  ", style="dim")
    t.append("56-Agent Swarm Sentiment Engine", style="italic yellow")
    t.append(f"  ·  {datetime.utcnow().strftime('%H:%M:%S UTC')}", style="dim")
    return Panel(t, style="cyan", padding=(0, 1))


def render_verdict_panel(verdict: dict, report: dict) -> Panel:
    direction = verdict["direction"]
    prob = verdict["crowd_probability"]
    edge = verdict["edge"]
    conf = verdict["confidence"]
    tradeable = verdict["tradeable"]

    color = "green" if direction == "YES" else "red"
    icon = "▲" if direction == "YES" else "▼"

    t = Text()
    t.append(f"\n  {icon} CROWD SAYS: ", style=f"bold {color}")
    t.append(f"{direction}  ", style=f"bold {color}")
    t.append(f"({prob*100:.1f}% probability)\n\n", style=color)
    t.append(f"  Edge vs 50/50:    ", style="dim")
    t.append(f"+{edge*100:.1f}%\n", style="bold yellow")
    t.append(f"  Confidence:       ", style="dim")
    t.append(f"{conf}\n", style="bold")
    t.append(f"  Signal:           ", style="dim")
    if tradeable:
        t.append(f"TRADE  ← crowd edge is sufficient\n", style="bold green")
    else:
        t.append(f"SKIP   ← crowd edge too thin\n", style="bold red")

    # Show any summary excerpt from the report
    summary = (report.get("summary") or report.get("content", ""))[:400]
    if summary:
        t.append(f"\n  ─── Agent Summary ───\n", style="dim")
        for line in textwrap.wrap(summary, width=68):
            t.append(f"  {line}\n", style="italic")

    return Panel(t, title="[bold]Verdict[/bold]", border_style=color, padding=(0, 1))


def render_signal_table(verdict: dict) -> Table:
    tbl = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
    tbl.add_column("Field", style="dim", width=22)
    tbl.add_column("Value", style="bold")

    color = "green" if verdict["direction"] == "YES" else "red"
    tbl.add_row("Scenario", textwrap.shorten(verdict["scenario"], width=50))
    tbl.add_row("Direction", f"[{color}]{verdict['direction']}[/{color}]")
    tbl.add_row("Crowd Probability", f"{verdict['crowd_probability']*100:.1f}%")
    tbl.add_row("Edge Over Market", f"{verdict['edge']*100:.1f}%")
    tbl.add_row("Confidence", verdict["confidence"])
    tbl.add_row("Signal", "[green]TRADE[/green]" if verdict["tradeable"] else "[red]SKIP[/red]")
    tbl.add_row("Generated", verdict["timestamp"])
    return tbl

# ─── Main pipeline ────────────────────────────────────────────────────────────

def run_pipeline(client: MiroFishClient, scenario: str,
                 context: str = "", max_rounds: int = 10) -> tuple[dict, dict]:
    """
    Full MiroFish pipeline: scenario → agents → report → verdict.
    Returns (verdict_dict, report_data).
    """
    # 1. Write seed document
    seed_text = build_scenario_document(scenario, context)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md",
                                      prefix="mirofish_scenario_",
                                      delete=False, encoding="utf-8")
    tmp.write(seed_text)
    tmp.close()
    seed_path = tmp.name

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=30),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:

            # Step 1: Ontology
            t1 = progress.add_task("[cyan]Extracting entities & ontology…", total=None)
            requirement = (
                f"Predict crowd reaction and YES/NO probability for: {scenario}"
            )
            onto = client.generate_ontology(seed_path, requirement,
                                             f"trade_{int(time.time())}")
            project_id = onto["project_id"]
            progress.update(t1, description="[green]✓ Ontology extracted",
                            completed=1, total=1)

            # Step 2: Graph build
            t2 = progress.add_task("[cyan]Building knowledge graph…", total=None)
            build = client.build_graph(project_id)
            task_id = build.get("task_id")
            if task_id:
                client.poll_task(task_id, timeout=120)
            # get graph_id
            graph_id = build.get("graph_id") or onto.get("graph_id")
            if not graph_id:
                # fetch it from project
                r = client.session.get(f"{MIROFISH_URL}/api/graph/project/{project_id}")
                r.raise_for_status()
                graph_id = r.json()["data"].get("graph_id")
            progress.update(t2, description="[green]✓ Knowledge graph built",
                            completed=1, total=1)

            # Step 3: Simulation create + prepare
            t3 = progress.add_task("[cyan]Spawning 56 agents…", total=None)
            sim = client.create_simulation(project_id, graph_id)
            simulation_id = sim["simulation_id"]
            client.prepare_simulation(simulation_id)
            client.poll_prepare(simulation_id, timeout=240)
            progress.update(t3, description="[green]✓ 56 agents initialized",
                            completed=1, total=1)

            # Step 4: Run simulation
            t4 = progress.add_task(
                f"[cyan]Running {max_rounds}-round simulation…", total=max_rounds
            )
            client.start_simulation(simulation_id, max_rounds=max_rounds)

            def on_update(status, current_round):
                progress.update(t4, completed=min(current_round, max_rounds),
                                description=f"[yellow]Round {current_round}/{max_rounds} "
                                            f"— {status.get('action_count', 0)} actions")

            client.poll_simulation(simulation_id, max_rounds,
                                   on_update=on_update, timeout=600)
            progress.update(t4, description="[green]✓ Simulation complete",
                            completed=max_rounds)

            # Step 5: Report
            t5 = progress.add_task("[cyan]Generating verdict report…", total=None)
            client.generate_report(simulation_id)
            report = client.poll_report(simulation_id, timeout=240)
            progress.update(t5, description="[green]✓ Report ready",
                            completed=1, total=1)

        # Parse verdict
        verdict = parse_verdict(report, scenario)
        return verdict, report

    finally:
        os.unlink(seed_path)

# ─── REPL loop ────────────────────────────────────────────────────────────────

def main():
    console.print(render_header())
    console.print()

    client = MiroFishClient()

    # Health check
    with console.status("[cyan]Connecting to MiroFish…"):
        alive = client.ping()

    if not alive:
        console.print(Panel(
            "[bold red]Cannot reach MiroFish at http://localhost:5001[/bold red]\n\n"
            "Start it with:\n"
            "  [yellow]cd ~/Desktop/mirofish && docker compose up -d[/yellow]\n\n"
            "Then wait ~30s for the containers to initialize.",
            title="Connection Error",
            border_style="red"
        ))
        sys.exit(1)

    console.print("[green]✓ Connected to MiroFish[/green]\n")

    history: list[dict] = []  # For multi-turn chat with report agent

    console.print(
        "[dim]Type a market scenario and press Enter to run a 56-agent simulation.\n"
        "Commands:  [bold]/chat[/bold]  — follow-up question to last report\n"
        "           [bold]/history[/bold] — show past verdicts\n"
        "           [bold]/quit[/bold]  — exit\n[/dim]"
    )

    last_simulation_id: str | None = None
    verdicts: list[dict] = []

    while True:
        console.print()
        try:
            scenario = Prompt.ask("[bold cyan]Scenario[/bold cyan]").strip()
        except (KeyboardInterrupt, EOFError):
            break

        if not scenario:
            continue
        if scenario.lower() in ("/quit", "/exit", "quit", "exit"):
            break

        # ── /history ──────────────────────────────────────────────────────────
        if scenario.lower() == "/history":
            if not verdicts:
                console.print("[dim]No verdicts yet.[/dim]")
                continue
            tbl = Table(box=box.SIMPLE, show_header=True)
            tbl.add_column("#", style="dim")
            tbl.add_column("Scenario")
            tbl.add_column("Direction", justify="center")
            tbl.add_column("Prob", justify="right")
            tbl.add_column("Edge", justify="right")
            tbl.add_column("Signal", justify="center")
            for i, v in enumerate(verdicts, 1):
                color = "green" if v["direction"] == "YES" else "red"
                tbl.add_row(
                    str(i),
                    textwrap.shorten(v["scenario"], width=45),
                    f"[{color}]{v['direction']}[/{color}]",
                    f"{v['crowd_probability']*100:.0f}%",
                    f"{v['edge']*100:.1f}%",
                    "[green]TRADE[/green]" if v["tradeable"] else "[red]SKIP[/red]",
                )
            console.print(tbl)
            continue

        # ── /chat ─────────────────────────────────────────────────────────────
        if scenario.lower().startswith("/chat"):
            if not last_simulation_id:
                console.print("[dim]Run a simulation first.[/dim]")
                continue
            question = scenario[5:].strip()
            if not question:
                question = Prompt.ask("[bold]Ask the ReportAgent[/bold]")
            with console.status("[cyan]Asking ReportAgent…"):
                try:
                    answer = client.chat_with_report(last_simulation_id, question, history)
                    history.append({"role": "user", "content": question})
                    history.append({"role": "assistant", "content": answer})
                    console.print(Panel(answer, title="[bold]ReportAgent[/bold]",
                                        border_style="cyan"))
                except Exception as e:
                    console.print(f"[red]Chat error: {e}[/red]")
            continue

        # ── Full simulation pipeline ───────────────────────────────────────────
        console.print(f"\n[bold]Running MiroFish simulation:[/bold] {scenario}\n")

        try:
            verdict, report = run_pipeline(client, scenario, max_rounds=10)
        except Exception as e:
            console.print(f"[bold red]Pipeline error:[/bold red] {e}")
            continue

        last_simulation_id = report.get("simulation_id")
        history = []  # reset chat history for new sim
        verdicts.append(verdict)

        # Display results
        console.print()
        console.print(render_verdict_panel(verdict, report))
        console.print(render_signal_table(verdict))

        # Save verdict JSON
        out_path = Path(__file__).parent / "mirofish_verdicts.jsonl"
        with open(out_path, "a") as f:
            f.write(json.dumps(verdict) + "\n")
        console.print(f"[dim]Verdict saved → {out_path}[/dim]")

        # Suggest Kalshi context
        if verdict["tradeable"]:
            console.print(
                f"\n[bold green]Trade signal:[/bold green] "
                f"Buy [bold]{verdict['direction']}[/bold] on the relevant Kalshi contract.\n"
                f"  Search Kalshi for markets related to: [italic]{scenario[:60]}[/italic]\n"
                f"  Crowd probability: {verdict['crowd_probability']*100:.1f}% — "
                f"enter if market prices it < {max(5, int(verdict['crowd_probability']*100)-8)}c "
                f"or > {min(95, int(verdict['crowd_probability']*100)+8)}c\n"
            )
        else:
            console.print(
                "\n[yellow]No trade:[/yellow] crowd edge ({:.1f}%) below 7% threshold.\n".format(
                    verdict["edge"] * 100
                )
            )

    console.print("\n[dim]Goodbye.[/dim]")


if __name__ == "__main__":
    main()
