# 31_security_good_mac.py
import base64, json, hmac, hashlib, requests, time
PSK = "ecowatt-demo-psk"  # must match device/server
URL = "http://127.0.0.1:5000/api/device/upload"

def wrap(psk:str, obj:dict)->dict:
    s = json.dumps(obj, separators=(",",":"))
    p_b64 = base64.b64encode(s.encode()).decode()
    nonce = int(time.time()*1000)
    mac = hmac.new(psk.encode(), f"{nonce}.{p_b64}".encode(), hashlib.sha256).hexdigest()
    return {"nonce":nonce,"payload":p_b64,"mac":mac}

payload = {
  "device_id":"EcoWatt-Dev-01",
  "ts_start":0,"ts_end":0,"seq":0,
  "codec":"none","order":[],"block_b64":"","ts_list":[]
}
r = requests.post(URL, json=wrap(PSK, payload), timeout=5)
print(r.status_code, r.json())
