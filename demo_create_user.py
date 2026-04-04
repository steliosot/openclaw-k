import requests

BASE = "http://127.0.0.1:8787"  # use VM IP if remote
TOKEN = "$$$$$$$"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

payload = {
    "username": "demo1",
    "port": 19111,
    # "config_file_path": "/Users/stelios/Desktop/openclaw-k/openclaw.json",  # optional
    "wait_timeout_seconds": 300
}

r = requests.post(f"{BASE}/v1/users", headers=HEADERS, json=payload, timeout=400)
print("status:", r.status_code)
data = r.json()
print(data)

if r.status_code == 201:
    print("\nOpen this in browser:")
    print(data["connect_link"])
