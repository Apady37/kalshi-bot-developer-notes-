import requests
import os
import datetime
import base64
import json
import time
from dotenv import load_dotenv
from urllib.parse import urlparse
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

load_dotenv()

KEY_ID = os.getenv("KALSHI_KEY_ID")
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
PROPOSALS_FILE = "proposals.json"

def load_private_key():
    key_data = os.getenv("KALSHI_PRIVATE_KEY")
    if key_data:
        key_data = key_data.replace("\\n", "\n")
        return serialization.load_pem_private_key(
            key_data.encode(), password=None, backend=default_backend()
        )
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
    proposal["agent"] = "researcher"
    proposals.append(proposal)
    with open(PROPOSALS_FILE, "w") as f:
        json.dump(proposals, f, indent=2)
    print(f"  Proposal saved: {proposal['title']}")

def get_active_series():
    path = "/series?limit=200"
    headers = get_headers("GET", path)
    r = requests.get(BASE_URL + path, headers=headers, timeout=10).json()
    return r.get("series", [])

def get_series_markets(ticker):
    path = f"/markets?limit=20&status=open&series_ticker={ticker}"
    headers = get_headers("GET", path)
    r = requests.get(BASE_URL + path, headers=headers, timeout=5).json()
    markets = r.get("markets", [])
    # Check for real volume
    total_volume = sum(float(m.get("volume_fp", 0) or 0) for m in markets)
    priced = [m for m in markets
              if m.get("yes_ask_dollars") and
              20 <= float(m.get("yes_ask_dollars", 0)) * 100 <= 80]
    return markets, total_volume, priced

def research_new_markets():
    print("Market Research Agent starting...")
    current_series = ["KXBTC", "KXETH", "KXDOGE", "KXXRP"]

    # Categories worth scanning
    target_categories = [
        "Economics", "Politics", "Elections",
        "Science and Technology", "World"
    ]

    print("Fetching all series...")
    all_series = get_active_series()

    high_potential = []
    for s in all_series:
        ticker = s.get("ticker", "")
        category = s.get("category", "")
        title = s.get("title", "")

        if ticker in current_series:
            continue
        if category not in target_categories:
            continue

        try:
            markets, volume, priced = get_series_markets(ticker)
            if volume > 5000 and len(priced) > 0:
                high_potential.append({
                    "ticker": ticker,
                    "title": title,
                    "category": category,
                    "volume": volume,
                    "priced_markets": len(priced),
                    "sample_market": priced[0].get("title", "") if priced else ""
                })
                print(f"  Found: {ticker} | vol:{volume:.0f} | {title[:40]}")
        except:
            pass
        time.sleep(0.2)

    # Sort by volume
    high_potential.sort(key=lambda x: x["volume"], reverse=True)

    if high_potential:
        top = high_potential[:5]
        save_proposal({
            "title": f"Add {len(top)} new high-volume markets to bot",
            "description": f"Research found {len(high_potential)} markets outside current crypto coverage with real volume and contested prices.",
            "type": "new_markets",
            "data": top,
            "recommended_action": f"Add these series tickers to MARKETS dict in main.py: {[m['ticker'] for m in top]}",
            "estimated_impact": "More opportunities per scan, potentially higher trade frequency"
        })
    else:
        print("  No high-potential new markets found this scan")

    # Check if current crypto markets have changed significantly
    print("Checking current crypto market volume...")
    for series in current_series:
        markets, volume, priced = get_series_markets(series)
        if volume < 1000:
            save_proposal({
                "title": f"Low volume warning: {series}",
                "description": f"{series} has very low volume ({volume:.0f}). Consider temporarily removing from scan.",
                "type": "volume_warning",
                "data": {"series": series, "volume": volume},
                "recommended_action": f"Increase MIN_VOLUME for {series} or remove from MARKETS",
                "estimated_impact": "Reduces false signals from illiquid markets"
            })

    print(f"Research complete. Check proposals.json for recommendations.")

if __name__ == "__main__":
    research_new_markets()