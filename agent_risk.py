import os
import json
import time
import datetime
import csv

PROPOSALS_FILE = "proposals.json"
DEMO_FILE = "demo_trades.csv"
PENDING_FILE = "pending_bets.json"

ALERT_BALANCE_DROP_PCT = 0.15
ALERT_LOSS_STREAK = 4
ALERT_MAX_PENDING = 10

def load_proposals():
    if os.path.exists(PROPOSALS_FILE):
        with open(PROPOSALS_FILE, "r") as f:
            return json.load(f)
    return []

def save_proposal(proposal):
    proposals = load_proposals()
    proposal["id"] = f"prop_{int(time.time())}"
    proposal["timestamp"] = datetime.datetime.now().isoformat()
    proposal["status"] = "pending"
    proposal["agent"] = "risk_monitor"
    proposals.append(proposal)
    with open(PROPOSALS_FILE, "w") as f:
        json.dump(proposals, f, indent=2)
    print(f"  ALERT: {proposal['title']}")

def load_trades():
    trades = []
    if not os.path.exists(DEMO_FILE):
        return trades
    with open(DEMO_FILE, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append(row)
    return trades

def load_pending():
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, "r") as f:
            return json.load(f)
    return []

def monitor_risk():
    print("Risk Monitor Agent starting...")
    trades = load_trades()
    pending = load_pending()

    # Check balance trajectory
    if len(trades) >= 2:
        starting_balance = 10000
        current_balance = float(trades[-1].get("balance_cents", 10000))
        balance_drop = (starting_balance - current_balance) / starting_balance

        if balance_drop > ALERT_BALANCE_DROP_PCT:
            save_proposal({
                "title": f"RISK ALERT: Balance dropped {balance_drop*100:.1f}%",
                "description": f"Balance has fallen from $100 to ${current_balance/100:.2f} — a {balance_drop*100:.1f}% drop. This exceeds the {ALERT_BALANCE_DROP_PCT*100}% alert threshold.",
                "type": "risk_alert",
                "severity": "HIGH",
                "data": {
                    "starting_balance": starting_balance,
                    "current_balance": current_balance,
                    "drop_pct": round(balance_drop*100, 1)
                },
                "recommended_action": "Consider pausing bot and reviewing strategy. Check if market conditions have changed.",
                "estimated_impact": "Prevent further losses"
            })

    # Check for loss streak
    if len(trades) >= ALERT_LOSS_STREAK:
        recent = trades[-ALERT_LOSS_STREAK:]
        if all(t.get("result") == "LOSS" for t in recent):
            save_proposal({
                "title": f"RISK ALERT: {ALERT_LOSS_STREAK} consecutive losses",
                "description": f"Bot has lost {ALERT_LOSS_STREAK} trades in a row. This may indicate market conditions have changed or strategy needs adjustment.",
                "type": "risk_alert",
                "severity": "HIGH",
                "data": {
                    "loss_streak": ALERT_LOSS_STREAK,
                    "recent_trades": [{"ticker": t.get("ticker"), "pnl": t.get("pnl_cents")} for t in recent]
                },
                "recommended_action": "Review recent losing trades. Consider raising MIN_EDGE or pausing bot temporarily.",
                "estimated_impact": "Prevent continued losses during bad conditions"
            })

    # Check pending bets
    if len(pending) > ALERT_MAX_PENDING:
        save_proposal({
            "title": f"WARNING: {len(pending)} pending bets unresolved",
            "description": f"There are {len(pending)} bets still pending resolution. This is unusually high and may indicate a bug in the settlement checker.",
            "type": "warning",
            "severity": "MEDIUM",
            "data": {"pending_count": len(pending)},
            "recommended_action": "Check if pending_bets.json is being cleared properly. May need to manually clear old entries.",
            "estimated_impact": "Ensure accurate P&L tracking"
        })

    # Check for stale pending bets
    now = datetime.datetime.now()
    stale = []
    for bet in pending:
        try:
            bet_time = datetime.datetime.fromisoformat(bet.get("time", ""))
            age_hours = (now - bet_time).total_seconds() / 3600
            if age_hours > 24:
                stale.append({"ticker": bet.get("ticker"), "age_hours": round(age_hours, 1)})
        except:
            pass

    if stale:
        save_proposal({
            "title": f"WARNING: {len(stale)} stale pending bets (>24 hours old)",
            "description": f"These bets have been pending for over 24 hours and may never resolve. Could indicate cancelled markets or API issues.",
            "type": "warning",
            "severity": "MEDIUM",
            "data": {"stale_bets": stale},
            "recommended_action": "Manually check these tickers on Kalshi and remove from pending_bets.json if they're resolved or cancelled.",
            "estimated_impact": "Clean up P&L tracking"
        })

    # Daily summary
    if trades:
        total_pnl = sum(float(t.get("pnl_cents", 0)) for t in trades)
        wins = len([t for t in trades if t.get("result") == "WIN"])
        losses_count = len([t for t in trades if t.get("result") == "LOSS"])
        win_rate = wins / len(trades) if trades else 0

        today = now.date()
        today_trades = [t for t in trades if t.get("time", "").startswith(str(today))]
        today_pnl = sum(float(t.get("pnl_cents", 0)) for t in today_trades)

        save_proposal({
            "title": f"Daily Risk Summary — {today}",
            "description": f"Bot status report for {today}.",
            "type": "daily_summary",
            "severity": "INFO",
            "data": {
                "total_trades": len(trades),
                "win_rate": round(win_rate*100, 1),
                "total_pnl_dollars": round(total_pnl/100, 2),
                "today_trades": len(today_trades),
                "today_pnl_dollars": round(today_pnl/100, 2),
                "pending_bets": len(pending)
            },
            "recommended_action": "Review if needed.",
            "estimated_impact": "Informational"
        })

    print(f"  Risk check complete. {len(pending)} pending bets.")

if __name__ == "__main__":
    monitor_risk()