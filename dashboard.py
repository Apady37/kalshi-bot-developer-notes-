import json
import os
import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

PROPOSALS_FILE = "proposals.json"

def load_proposals():
    if os.path.exists(PROPOSALS_FILE):
        with open(PROPOSALS_FILE, "r") as f:
            return json.load(f)
    return []

def save_proposals(proposals):
    with open(PROPOSALS_FILE, "w") as f:
        json.dump(proposals, f, indent=2)

def get_dashboard_html():
    proposals = load_proposals()
    pending = [p for p in proposals if p.get("status") == "pending"]
    approved = [p for p in proposals if p.get("status") == "approved"]
    rejected = [p for p in proposals if p.get("status") == "rejected"]

    severity_colors = {
        "HIGH": "#ff4444",
        "MEDIUM": "#ff8800",
        "INFO": "#4444ff",
        None: "#888888"
    }

    type_icons = {
        "risk_alert": "🚨",
        "warning": "⚠️",
        "parameter_change": "⚙️",
        "new_markets": "📈",
        "remove_market": "🗑️",
        "daily_summary": "📊",
        "info": "ℹ️"
    }

    cards = ""
    for p in sorted(pending, key=lambda x: x.get("timestamp", ""), reverse=True):
        color = severity_colors.get(p.get("severity"))
        icon = type_icons.get(p.get("type", ""), "💡")
        data_str = json.dumps(p.get("data", {}), indent=2)
        cards += f"""
        <div class="card" style="border-left: 4px solid {color}">
            <div class="card-header">
                <span class="icon">{icon}</span>
                <span class="title">{p.get('title', 'Untitled')}</span>
                <span class="agent">by {p.get('agent', 'unknown')} agent</span>
                <span class="time">{p.get('timestamp', '')[:16]}</span>
            </div>
            <div class="description">{p.get('description', '')}</div>
            <div class="action"><strong>Recommended Action:</strong> {p.get('recommended_action', '')}</div>
            <div class="impact"><strong>Estimated Impact:</strong> {p.get('estimated_impact', '')}</div>
            {"<div class='code'><strong>Code Change:</strong> <code>" + p.get('code_change', '') + "</code></div>" if p.get('code_change') else ""}
            <div class="data-section">
                <details>
                    <summary>View Data</summary>
                    <pre>{data_str}</pre>
                </details>
            </div>
            <div class="buttons">
                <a href="/approve/{p.get('id')}" class="btn approve">✅ Approve</a>
                <a href="/reject/{p.get('id')}" class="btn reject">❌ Reject</a>
            </div>
        </div>
        """

    history = ""
    for p in sorted(approved + rejected,
                    key=lambda x: x.get("timestamp", ""), reverse=True)[:10]:
        status_icon = "✅" if p.get("status") == "approved" else "❌"
        history += f"""
        <div class="history-item">
            {status_icon} <strong>{p.get('title', '')}</strong>
            <span class="time">{p.get('timestamp', '')[:16]}</span>
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Kalshi Bot — Agent Dashboard</title>
        <meta http-equiv="refresh" content="30">
        <style>
            body {{ font-family: -apple-system, sans-serif; background: #0a0a0a; color: #eee; margin: 0; padding: 20px; }}
            h1 {{ color: #00ff88; }}
            h2 {{ color: #888; font-size: 14px; text-transform: uppercase; letter-spacing: 2px; margin-top: 30px; }}
            .stats {{ display: flex; gap: 20px; margin: 20px 0; }}
            .stat {{ background: #1a1a1a; padding: 15px 25px; border-radius: 8px; text-align: center; }}
            .stat .num {{ font-size: 32px; font-weight: bold; color: #00ff88; }}
            .stat .label {{ font-size: 12px; color: #888; }}
            .card {{ background: #1a1a1a; border-radius: 8px; padding: 20px; margin: 15px 0; }}
            .card-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }}
            .icon {{ font-size: 20px; }}
            .title {{ font-size: 16px; font-weight: bold; flex: 1; }}
            .agent {{ font-size: 11px; color: #888; background: #333; padding: 2px 8px; border-radius: 10px; }}
            .time {{ font-size: 11px; color: #555; }}
            .description {{ color: #aaa; margin: 8px 0; line-height: 1.5; }}
            .action {{ background: #222; padding: 8px 12px; border-radius: 4px; margin: 8px 0; font-size: 13px; }}
            .impact {{ font-size: 13px; color: #888; margin: 4px 0; }}
            .code {{ background: #111; padding: 8px 12px; border-radius: 4px; margin: 8px 0; }}
            code {{ color: #00ff88; font-family: monospace; }}
            .data-section {{ margin: 8px 0; }}
            details summary {{ cursor: pointer; color: #888; font-size: 12px; }}
            pre {{ background: #111; padding: 10px; border-radius: 4px; font-size: 11px; overflow-x: auto; color: #aaa; }}
            .buttons {{ display: flex; gap: 10px; margin-top: 15px; }}
            .btn {{ padding: 8px 20px; border-radius: 6px; text-decoration: none; font-weight: bold; font-size: 13px; }}
            .approve {{ background: #00ff88; color: #000; }}
            .reject {{ background: #333; color: #ff4444; border: 1px solid #ff4444; }}
            .history-item {{ padding: 8px 0; border-bottom: 1px solid #222; display: flex; justify-content: space-between; font-size: 13px; }}
            .empty {{ color: #555; text-align: center; padding: 40px; }}
        </style>
    </head>
    <body>
        <h1>🤖 Kalshi Bot — Agent Dashboard</h1>
        <div class="stats">
            <div class="stat"><div class="num">{len(pending)}</div><div class="label">Pending</div></div>
            <div class="stat"><div class="num">{len(approved)}</div><div class="label">Approved</div></div>
            <div class="stat"><div class="num">{len(rejected)}</div><div class="label">Rejected</div></div>
            <div class="stat"><div class="num">{len(proposals)}</div><div class="label">Total</div></div>
        </div>
        <h2>⏳ Pending Proposals</h2>
        {cards if cards else '<div class="empty">No pending proposals. Agents are running...</div>'}
        <h2>📋 Recent History</h2>
        {history if history else '<div class="empty">No history yet.</div>'}
        <p style="color:#333; font-size:11px; margin-top:40px;">Auto-refreshes every 30 seconds</p>
    </body>
    </html>
    """

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress access logs

    def do_GET(self):
        if self.path == "/" or self.path == "/dashboard":
            html = get_dashboard_html()
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())

        elif self.path.startswith("/approve/"):
            prop_id = self.path.split("/approve/")[1]
            proposals = load_proposals()
            for p in proposals:
                if p.get("id") == prop_id:
                    p["status"] = "approved"
                    p["approved_at"] = datetime.datetime.now().isoformat()
            save_proposals(proposals)
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()

        elif self.path.startswith("/reject/"):
            prop_id = self.path.split("/reject/")[1]
            proposals = load_proposals()
            for p in proposals:
                if p.get("id") == prop_id:
                    p["status"] = "rejected"
                    p["rejected_at"] = datetime.datetime.now().isoformat()
            save_proposals(proposals)
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    print(f"Dashboard running at http://localhost:{port}")
    print("Agents will post proposals here for your approval.")
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    server.serve_forever()