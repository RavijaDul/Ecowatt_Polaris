# 30_security_bad_mac.py
import base64, json, requests, time
PSK = "wrong-psk"   # <-- intentionally wrong
URL = "http://127.0.0.1:5000/api/device/upload"

payload = {
  "device_id": "EcoWatt-Dev-01",
  "ts_start": 0, "ts_end": 0, "seq": 0,
  "codec": "none", "order": [], "block_b64": "", "ts_list":[]
}
s = json.dumps(payload, separators=(",",":"))
p_b64 = base64.b64encode(s.encode()).decode()
nonce = int(time.time()*1000)
mac   = "deadbeef"  # bogus

envelope = {"nonce": nonce, "payload": p_b64, "mac": mac}
r = requests.post(URL, json=envelope, timeout=5)
print(r.status_code, r.text)
# Server wraps error: {"nonce":...,"payload":...,"mac":...} with inner {"error":"bad-mac-or-nonce"} :contentReference[oaicite:43]{index=43}
