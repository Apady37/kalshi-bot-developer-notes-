import requests, os, datetime, base64
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
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def sign_request(pk, ts, method, path):
    msg = f"{ts}{method}{path.split('?')[0]}".encode()
    sig = pk.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return base64.b64encode(sig).decode()

def get_headers(method, path):
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    pk = load_private_key()
    return {"KALSHI-ACCESS-KEY": KEY_ID, "KALSHI-ACCESS-SIGNATURE": sign_request(pk, ts, method, urlparse(BASE_URL + path).path), "KALSHI-ACCESS-TIMESTAMP": ts, "Content-Type": "application/json"}

def parse_expiry(close_time):
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    try:
        ct = datetime.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        return max(0, (ct - now_utc).total_seconds() / 60)
    except:
        return 0

now_utc = datetime.datetime.now(datetime.timezone.utc)
print(f"UTC: {now_utc.strftime('%H:%M:%S')}")
print()

for series in ["KXBTC15M", "KXETH15M", "KXXRP15M", "KXSOL15M"]:
    path = f"/markets?limit=5&status=open&series_ticker={series}"
    r = requests.get(BASE_URL + path, headers=get_headers("GET", path), timeout=10).json()
    markets = r.get("markets", [])
    for m in markets:
        yes_raw = m.get("yes_ask_dollars")
        no_raw = m.get("no_ask_dollars")
        volume = float(m.get("volume_fp", 0) or 0)
        if yes_raw is None or no_raw is None:
            continue
        yes_c = round(float(yes_raw)*100, 1)
        no_c = round(float(no_raw)*100, 1)
        total = yes_c + no_c
        mins_left = parse_expiry(m.get("close_time", ""))
        fair_yes = 100 - no_c
        fair_no = 100 - yes_c
        yes_edge = fair_yes - yes_c
        no_edge = fair_no - no_c
        print(f"{m.get('ticker')} | yes:{yes_c}c no:{no_c}c | sum:{total}c | vol:{volume:.0f} | {mins_left:.1f}m")
        print(f"  fair_YES:{fair_yes}c edge:{yes_edge:+.1f}c | fair_NO:{fair_no}c edge:{no_edge:+.1f}c")
        if yes_edge >= 3:
            print(f"  ✅ BUY YES — {yes_edge}c edge")
        elif no_edge >= 3:
            print(f"  ✅ BUY NO — {no_edge}c edge")
        else:
            print(f"  ❌ No edge (Kalshi taking {total-100:.1f}c cut)")
        print()