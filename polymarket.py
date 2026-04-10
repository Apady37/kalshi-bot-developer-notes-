import requests
import os
import datetime
import base64
import time
import concurrent.futures
from dotenv import load_dotenv
from urllib.parse import urlparse
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

load_dotenv()

KEY_ID = os.getenv("KALSHI_KEY_ID")
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

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

SKIP_PREFIXES = [
    "KXBTC", "KXETH", "KXDOGE", "KXXRP", "KXWTI",
    "KXNBA", "KXNFL", "KXMLB", "KXNHL", "KXNBAPA",
    "KXLEADER", "KXMLBWINS", "KXNFLFANTASY", "KXMVE"
]

def get_active_kalshi_markets():
    """Fetch open markets that actually have prices"""
    print("Fetching active Kalshi markets with prices...")
    all_markets = []
    cursor = None
    pages = 0

    while pages < 10:
        path = "/markets?limit=200&status=open"
        if cursor:
            path += f"&cursor={cursor}"
        headers = get_headers("GET", path)
        response = requests.get(BASE_URL + path, headers=headers, timeout=10).json()
        markets = response.get("markets", [])

        for m in markets:
            ticker = m.get("ticker", "")
            yes_ask = m.get("yes_ask")
            no_ask = m.get("no_ask")
            title = m.get("title", "")

            # Only keep markets with actual prices
            if yes_ask is None or no_ask is None:
                continue
            if yes_ask <= 2 or yes_ask >= 98:
                continue
            # Skip multi-leg parlays
            if "," in title:
                continue
            # Skip crypto and sports
            skip = any(ticker.startswith(p) for p in SKIP_PREFIXES)
            if skip:
                continue

            all_markets.append(m)

        cursor = response.get("cursor")
        pages += 1
        print(f"  Page {pages}: {len(all_markets)} active non-crypto/sports markets so far...")

        if not cursor or len(markets) == 0:
            break

    print(f"Found {len(all_markets)} active markets with prices")
    return all_markets

def get_polymarket_markets():
    print("Fetching Polymarket markets...")
    all_markets = []
    for offset in range(0, 1000, 100):
        try:
            r = requests.get(
                f"https://gamma-api.polymarket.com/markets?limit=100&active=true&closed=false&order=volume&ascending=false&offset={offset}",
                timeout=10
            ).json()
            if not r:
                break
            all_markets.extend(r)
        except:
            break
    print(f"Got {len(all_markets)} Polymarket markets")
    return all_markets

def extract_keywords(text):
    stop_words = {
        "will", "the", "a", "an", "be", "is", "in", "on", "at", "to",
        "of", "by", "for", "and", "or", "not", "win", "does", "get",
        "have", "has", "was", "are", "its", "that", "this", "with",
        "before", "after", "above", "below", "than", "more", "who",
        "what", "when", "which", "2026", "2025", "2024", "first",
        "next", "last", "new", "any", "all", "from", "into", "over",
        "price", "range", "least", "most", "best", "ever", "between"
    }
    words = text.lower()
    for char in "?.,!()[]{}\"'":
        words = words.replace(char, " ")
    words = words.split()
    return set(w for w in words if w not in stop_words and len(w) > 3)

def match_markets(kalshi_markets, poly_markets):
    matches = []

    for poly in poly_markets:
        poly_question = poly.get("question", "")
        poly_volume = float(poly.get("volume", 0))

        if poly_volume < 500:
            continue

        try:
            best_ask = float(poly.get("bestAsk") or poly.get("outcomePrices", ["0.5"])[0])
            if best_ask <= 0.03 or best_ask >= 0.97:
                continue
            poly_yes = round(best_ask * 100, 1)
        except:
            continue

        poly_keywords = extract_keywords(poly_question)
        if len(poly_keywords) < 2:
            continue

        for kalshi in kalshi_markets:
            kalshi_title = kalshi.get("title", "")
            kalshi_subtitle = kalshi.get("subtitle", "")
            kalshi_yes = kalshi.get("yes_ask")
            kalshi_no = kalshi.get("no_ask")

            combined = f"{kalshi_title} {kalshi_subtitle}"
            kalshi_keywords = extract_keywords(combined)
            overlap = poly_keywords & kalshi_keywords

            if len(overlap) >= 2:
                edge_yes = poly_yes - kalshi_yes
                edge_no = (100 - poly_yes) - kalshi_no

                if edge_yes >= 5:
                    matches.append({
                        "type": "BUY YES on Kalshi",
                        "edge": round(edge_yes, 1),
                        "kalshi_ticker": kalshi.get("ticker"),
                        "kalshi_price": kalshi_yes,
                        "poly_price": poly_yes,
                        "kalshi_title": combined.strip(),
                        "poly_question": poly_question,
                        "overlap": overlap,
                        "poly_volume": poly_volume
                    })
                elif edge_no >= 5:
                    matches.append({
                        "type": "BUY NO on Kalshi",
                        "edge": round(edge_no, 1),
                        "kalshi_ticker": kalshi.get("ticker"),
                        "kalshi_price": kalshi_no,
                        "poly_price": round(100 - poly_yes, 1),
                        "kalshi_title": combined.strip(),
                        "poly_question": poly_question,
                        "overlap": overlap,
                        "poly_volume": poly_volume
                    })

    return sorted(matches, key=lambda x: x["edge"], reverse=True)

# Main
kalshi_markets = get_active_kalshi_markets()
poly_markets = get_polymarket_markets()

print(f"\nMatching {len(kalshi_markets)} Kalshi vs {len(poly_markets)} Polymarket...\n")
matches = match_markets(kalshi_markets, poly_markets)

if matches:
    print(f"🎯 Found {len(matches)} opportunities!\n")
    for m in matches[:15]:
        print(f"Action  : {m['type']}")
        print(f"Edge    : +{m['edge']}c")
        print(f"Kalshi  : {m['kalshi_title']} | {m['kalshi_price']}c")
        print(f"Poly    : {m['poly_question']} | {m['poly_price']}c")
        print(f"Matched : {m['overlap']}")
        print(f"Volume  : ${m['poly_volume']:,.0f}")
        print()
else:
    print("No matches found")
    print(f"\nSample active Kalshi markets:")
    for m in kalshi_markets[:15]:
        print(f"  {m.get('ticker')} | {m.get('title')} | yes:{m.get('yes_ask')}c no:{m.get('no_ask')}c")
    print(f"\nSample Polymarket questions:")
    for m in poly_markets[:15]:
        try:
            ask = float(poly.get("bestAsk") or "0")
            print(f"  {round(ask*100,1)}c | {m.get('question')}")
        except:
            print(f"  {m.get('question')}")