# app.py
import os, time, base64, json, sqlite3, pathlib, datetime, glob, hmac, io, csv, hashlib
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from typing import List, Tuple, Optional
from flask import Flask, request, jsonify, g, Response, redirect, url_for
from werkzeug.utils import secure_filename
import requests


# ---- Config via env ----
AUTH_KEYS_B64 = [k.strip() for k in os.getenv("AUTH_KEYS_B64", "").split(",") if k.strip()]
REQUIRE_AUTH  = bool(AUTH_KEYS_B64)
DB_PATH       = os.getenv("SQLITE_PATH", "ecowatt.db")
LOG_DIR       = os.getenv("LOG_DIR", "logs")
PSK           = os.getenv("PSK", "ecowatt-demo-psk")
USE_B64       = bool(int(os.getenv("USE_B64", "1")))  # 1=use base64 envelope

# Track what we last served, so we can estimate "written"
# device_id -> {"version":str, "size":int, "chunk_size":int, "next":int, "written":int, "last_served_manifest": version, "cycles_without_progress": 0}
LAST_FOTA = {}

DDL = """
CREATE TABLE IF NOT EXISTS uploads (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id    TEXT    NOT NULL,
  ts_start     INTEGER NOT NULL,
  ts_end       INTEGER NOT NULL,
  seq          INTEGER,
  codec        TEXT    NOT NULL,
  order_json   TEXT    NOT NULL,
  ts_list_json TEXT,
  orig_samples INTEGER,
  orig_bytes   INTEGER,
  received_at  INTEGER NOT NULL,
  block        BLOB    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_dev_ts ON uploads (device_id, ts_start, ts_end);

CREATE TABLE IF NOT EXISTS fota_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT DEFAULT CURRENT_TIMESTAMP,
  device TEXT,
  kind TEXT,          -- 'manifest','chunk','verify_ok','verify_fail','boot_ok','boot_rollback','corruption_detected','rollback'
  detail TEXT
);

CREATE TABLE IF NOT EXISTS fota_progress(
  device TEXT PRIMARY KEY,
  version TEXT,
  size INTEGER,
  written INTEGER,
  percent INTEGER,
  status TEXT DEFAULT 'pending',  -- 'pending','downloading','verify_ok','verify_failed','boot_ok','boot_rollback'
  updated TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fota_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device TEXT NOT NULL,
  version TEXT NOT NULL,
  size INTEGER,
  hash TEXT,
  status TEXT,  -- 'manifest_received','downloading','verify_ok','verify_failed','boot_ok','boot_rollback'
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(device, version)
);
CREATE INDEX IF NOT EXISTS idx_fota_versions_device ON fota_versions(device, updated_at DESC);

CREATE TABLE IF NOT EXISTS power_stats (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  device        TEXT    NOT NULL,
  received_at   INTEGER NOT NULL,
  t_sleep_ms    INTEGER NOT NULL,
  t_manual_sleep_ms INTEGER DEFAULT 0,
  t_auto_sleep_ms   INTEGER DEFAULT 0,
  idle_budget_ms INTEGER NOT NULL,
  t_uplink_ms   INTEGER NOT NULL,
  uplink_bytes  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_power_stats_dev_time ON power_stats(device, received_at);

CREATE TABLE IF NOT EXISTS buffer_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    dropped_samples INTEGER DEFAULT 0,
    acq_failures INTEGER DEFAULT 0,
    transport_failures INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_buffer_stats_dev_time ON buffer_stats(device, received_at);

CREATE TABLE IF NOT EXISTS device_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device      TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  event       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dev_events ON device_events(device, received_at);

CREATE TABLE IF NOT EXISTS sim_faults (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device TEXT NOT NULL,
  error_type TEXT NOT NULL,  -- 'EXCEPTION', 'CRC_ERROR', 'CORRUPT', 'PACKET_DROP', 'DELAY', 'exception', 'timeout', 'malformed_response'
  exception_code INTEGER DEFAULT 0,
  delay_ms INTEGER DEFAULT 0,
  description TEXT DEFAULT '',  -- For device-reported faults
  status TEXT DEFAULT 'queued',  -- 'queued', 'triggered', 'acknowledged', 'reported'
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  triggered_at TEXT,
  acknowledged_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sim_faults_device ON sim_faults(device, created_at DESC);

"""

app = Flask(__name__)

# ------------ auth ------------
def _auth_ok(header: str) -> bool:
    if not REQUIRE_AUTH: return True
    if not header: return False
    token = header.strip()
    if token.lower().startswith("basic "):
        token = token[6:].strip()
    return token in AUTH_KEYS_B64

# ------------ DB open + migration ------------
def _migrate(db: sqlite3.Connection) -> None:
    cols = {row[1] for row in db.execute("PRAGMA table_info(uploads)").fetchall()}
    changed = False
    if "ts_list_json" not in cols:
        db.execute("ALTER TABLE uploads ADD COLUMN ts_list_json TEXT"); changed = True
    if "orig_samples" not in cols:
        db.execute("ALTER TABLE uploads ADD COLUMN orig_samples INTEGER"); changed = True
    if "orig_bytes" not in cols:
        db.execute("ALTER TABLE uploads ADD COLUMN orig_bytes INTEGER"); changed = True
    if changed: db.commit()
    
    # Migrate fota_progress table to add status column if it doesn't exist
    try:
        cols = {row[1] for row in db.execute("PRAGMA table_info(fota_progress)").fetchall()}
        if "status" not in cols:
            db.execute("ALTER TABLE fota_progress ADD COLUMN status TEXT DEFAULT 'pending'")
            db.commit()
    except Exception as e:
        print(f"[MIGRATE] fota_progress status column check: {e}")
    
    # Create fota_versions table if it doesn't exist
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS fota_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device TEXT NOT NULL,
                version TEXT NOT NULL,
                size INTEGER,
                hash TEXT,
                status TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(device, version)
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_fota_versions_device ON fota_versions(device, updated_at DESC)")
        db.commit()
    except Exception as e:
        print(f"[MIGRATE] fota_versions table check: {e}")
    
    # Add description column to sim_faults if it doesn't exist
    try:
        cols = {row[1] for row in db.execute("PRAGMA table_info(sim_faults)").fetchall()}
        if "description" not in cols:
            db.execute("ALTER TABLE sim_faults ADD COLUMN description TEXT DEFAULT ''")
            db.commit()
    except Exception as e:
        print(f"[MIGRATE] sim_faults description column check: {e}")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        g.db.execute("PRAGMA journal_mode=WAL;")
        g.db.execute("PRAGMA synchronous=NORMAL;")
        g.db.executescript(DDL)
        _migrate(g.db)
    return g.db

@app.teardown_appcontext
def close_db(_=None):
    db = g.pop("db", None)
    if db is not None: db.close()
def log_fota(device, kind, detail=""):
    db = get_db()
    db.execute("INSERT INTO fota_events(device,kind,detail) VALUES(?,?,?)",
               (device, kind, detail))
    db.commit()

def upsert_progress(device, version, size, written, status="pending"):
    pct = int((written*100)//size) if size else 0
    db = get_db()
    db.execute("""
      INSERT INTO fota_progress(device,version,size,written,percent,status)
      VALUES(?,?,?,?,?,?)
      ON CONFLICT(device) DO UPDATE SET
        version=excluded.version, size=excluded.size,
        written=excluded.written, percent=excluded.percent, status=excluded.status, updated=CURRENT_TIMESTAMP
    """, (device, version, size, written, pct, status))
    db.commit()

def upsert_fota_version(device, version, size, hash_hex, status):
    """Track all FOTA versions and their update status"""
    db = get_db()
    db.execute("""
      INSERT INTO fota_versions(device, version, size, hash, status)
      VALUES(?,?,?,?,?)
      ON CONFLICT(device, version) DO UPDATE SET
        status=excluded.status, updated_at=CURRENT_TIMESTAMP
    """, (device, version, size, hash_hex, status))
    db.commit()

# ---- SIM Fault Injection ----
SIM_API_BASE = "http://20.15.114.131:8080"

def queue_sim_fault(device, error_type, exception_code=0, delay_ms=0):
    """Queue a fault for the next Inverter SIM request from a device"""
    db = get_db()
    db.execute("""
      INSERT INTO sim_faults(device, error_type, exception_code, delay_ms, status)
      VALUES(?,?,?,?,'queued')
    """, (device, error_type, exception_code, delay_ms))
    db.commit()

def get_queued_fault(device):
    """Get next queued fault for a device"""
    db = get_db()
    row = db.execute("""
      SELECT id, error_type, exception_code, delay_ms 
      FROM sim_faults 
      WHERE device=? AND status='queued'
      LIMIT 1
    """, (device,)).fetchone()
    return row

def mark_fault_triggered(fault_id):
    """Mark fault as triggered when device requests it"""
    db = get_db()
    db.execute("UPDATE sim_faults SET status='triggered', triggered_at=CURRENT_TIMESTAMP WHERE id=?", (fault_id,))
    db.commit()

def trigger_sim_fault_at_inverter(error_type, exception_code=0, delay_ms=0, slave_addr=1, func_code=3):
    """Call the Inverter SIM API to inject the fault
    
    Uses /api/user/error-flag/add to set a persistent flag that triggers on device's next request.
    Falls back to /api/inverter/error for immediate testing.
    """
    sim_key = os.getenv("SIM_KEY_B64", "")
    
    # Method 1: Set persistent error flag (preferred - device will see it on next read)
    try:
        url = f"{SIM_API_BASE}/api/user/error-flag/add"
        payload = {
            "errorType": error_type,
            "exceptionCode": exception_code,
            "delayMs": delay_ms
        }
        headers = {
            "Authorization": sim_key,
            "Content-Type": "application/json"
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=5)
        if resp.status_code == 200:
            print(f"[SIM] Persistent error flag set via /api/user/error-flag/add: {error_type}", flush=True)
            return True
        else:
            print(f"[SIM] /api/user/error-flag/add returned {resp.status_code}: {resp.text[:100]}", flush=True)
    except Exception as e:
        print(f"[SIM] Error calling /api/user/error-flag/add: {e}", flush=True)
    
    # Method 2: Fallback - immediate error frame (for testing)
    try:
        url = f"{SIM_API_BASE}/api/inverter/error"
        payload = {
            "slaveAddress": slave_addr,
            "functionCode": func_code,
            "errorType": error_type,
            "exceptionCode": exception_code,
            "delayMs": delay_ms
        }
        resp = requests.post(url, json=payload, timeout=5)
        if resp.status_code == 200:
            print(f"[SIM] Immediate error frame generated via /api/inverter/error: {error_type}", flush=True)
            return True
        else:
            print(f"[SIM] /api/inverter/error returned {resp.status_code}: {resp.text[:100]}", flush=True)
    except Exception as e:
        print(f"[SIM] Error calling /api/inverter/error: {e}", flush=True)
    
    return False

def get_sim_fault_history(device, limit=50):
    """Get recent SIM faults for a device"""
    db = get_db()
    rows = db.execute("""
      SELECT id, error_type, exception_code, delay_ms, status, created_at, triggered_at
      FROM sim_faults
      WHERE device=?
      ORDER BY created_at DESC
      LIMIT ?
    """, (device, limit)).fetchall()
    return rows

# ---- scaling helpers ----
GAIN = {
    "vac1": 10.0, "iac1": 10.0, "fac1": 100.0,
    "vpv1": 10.0, "vpv2": 10.0, "ipv1": 10.0, "ipv2": 10.0,
    "temp": 10.0, "export_percent": 1.0, "pac": 1.0
}

def decode_delta_rle_v1(block: bytes, order: List[str]) -> Tuple[List[List[int]], str]:
    def u16(b, o): return b[o] | (b[o+1]<<8)
    def s16(b, o):
        v = u16(b,o)
        return v-0x10000 if v & 0x8000 else v

    if len(block) < 12: return [], "short"
    pos=0
    ver=block[pos]; pos+=1
    nf =block[pos]; pos+=1
    n  =u16(block,pos); pos+=2
    pos+=4
    if ver!=1 or nf!=len(order) or n==0: return [], "header mismatch or empty"
    if len(block) < pos + nf*2 + 4: return [], "truncated"

    last=[u16(block,pos+2*i) for i in range(nf)]
    pos+=nf*2
    fields=[[0]*n for _ in range(nf)]
    for f in range(nf):
        fields[f][0]=last[f]; produced=0
        while produced < n-1:
            if pos >= len(block)-4: return [], "early EOF"
            op = block[pos]; pos+=1
            if op==0x00:
                if pos>=len(block)-4: return [], "EOF len"
                rep=block[pos]; pos+=1
                for _ in range(rep): fields[f][1+produced]=last[f]; produced+=1
            elif op==0x01:
                if pos+2>len(block)-4: return [], "EOF delta"
                d=s16(block,pos); pos+=2
                cur=(last[f]+d)&0xFFFF
                fields[f][1+produced]=cur; last[f]=cur; produced+=1
            else:
                return [], "bad op"

    rows=[[fields[f][i] for f in range(nf)] for i in range(n)]
    return rows, "ok"

def _fmt_row(order: List[str], raw: List[int]) -> str:
    parts=[]
    for name, val in zip(order, raw):
        g = GAIN.get(name, 1.0)
        if name == "fac1": parts.append(f"{name}={val/g:.2f}Hz")
        elif name in ("vac1","vpv1","vpv2"): parts.append(f"{name}={val/g:.1f}V")
        elif name in ("iac1","ipv1","ipv2"):  parts.append(f"{name}={val/g:.1f}A")
        elif name == "temp": parts.append(f"{name}={val/g:.1f}C")
        elif name == "export_percent": parts.append(f"{name}={int(val)}%")
        elif name == "pac": parts.append(f"{name}={int(val)}W")
        else: parts.append(f"{name}={val}")
    return " ".join(parts)

def _looks_epoch_ms(v: int) -> bool: return v >= 1_000_000_000_000

def _device_ms_list(n: int, ts0: int, ts1: int):
    if n <= 1 or ts0 == ts1: return [ts0]*max(n,1)
    return [int(round(ts0 + i * (ts1 - ts0) / (n - 1))) for i in range(n)]

def _epoch_ms_list(n: int, ts0: int, ts1: int, recv: int, ts_list_opt: Optional[List[int]]):
    if ts_list_opt and len(ts_list_opt) >= n:
        xs = [int(v) for v in ts_list_opt[:n]]
        if all(_looks_epoch_ms(v) for v in xs): return xs
        out=[]
        for x in xs:
            if ts1 == ts0: out.append(recv)
            else:
                frac=(x-ts0)/(ts1-ts0); out.append(int(recv-(1.0-frac)*(ts1-ts0)))
        return out
    devs=_device_ms_list(n, ts0, ts1)
    out=[]
    for d in devs:
        if ts1 == ts0: out.append(recv)
        else:
            frac=(d-ts0)/(ts1-ts0); out.append(int(recv-(1.0-frac)*(ts1-ts0)))
    return out

HTML_HEAD = """<!doctype html><html><head><meta charset="utf-8">
<title>EcoWatt Admin</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,Segoe UI,Arial,sans-serif;line-height:1.35;background:#f5f5f5}
nav{background:#1a1a1a;color:#fff;padding:0;margin:0;box-shadow:0 2px 4px rgba(0,0,0,0.1);position:sticky;top:0;z-index:100}
nav ul{list-style:none;display:flex;flex-wrap:wrap;align-items:center;margin:0;padding:0}
nav li{margin:0}
nav a{display:block;color:#fff;text-decoration:none;padding:12px 16px;transition:background 0.2s;font-size:0.95em}
nav a:hover{background:#333}
nav .brand{font-weight:bold;font-size:1.15em;padding:12px 20px;margin-right:auto;color:#4CAF50}
nav .divider{width:1px;height:30px;background:#444;margin:0 4px}
.container{max-width:1200px;margin:0 auto;padding:20px}
code,pre{font-family:ui-monospace,Consolas,monospace}
.table{border-collapse:collapse;margin-top:12px;width:100%;background:#fff;border:1px solid #ddd}
.table th,.table td{border:1px solid #ddd;padding:8px 12px;text-align:left}
.table th{background:#f0f0f0;font-weight:bold}
.table tr:nth-child(even){background:#fafafa}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px;white-space:pre}
.small{color:#666}
h2{margin-top:24px;margin-bottom:12px;color:#1a1a1a}
h3{margin-top:16px;margin-bottom:8px;color:#333}
a{color:#0066cc;text-decoration:none}
a:hover{text-decoration:underline}
.form-group{margin-bottom:12px}
label{display:block;margin-bottom:4px;font-weight:bold;color:#333}
input,select,textarea{padding:8px;border:1px solid #ccc;border-radius:4px;font-family:inherit;font-size:14px}
input[type="file"],input[type="text"],select,textarea{width:100%}
button{padding:10px 16px;background:#0066cc;color:#fff;border:none;border-radius:4px;cursor:pointer;font-weight:bold;margin-right:8px}
button:hover{background:#0052a3}
.info{background:#e3f2fd;border-left:4px solid #2196f3;padding:12px;margin:12px 0}
.success{background:#e8f5e9;border-left:4px solid #4caf50;padding:12px;margin:12px 0}
.error{background:#ffebee;border-left:4px solid #f44336;padding:12px;margin:12px 0}
</style></head><body>
<nav>
  <ul>
    <li class="brand">⚡ EcoWatt</li>
    <li class="divider"></li>
    <li><a href="/">Home</a></li>
    <li class="divider"></li>
    <li><a href="/admin">Uploads</a></li>
    <li class="divider"></li>
    <li><a href="/admin/fota">FOTA</a></li>
    <li class="divider"></li>
    <li><a href="/admin/power">Power</a></li>
    <li class="divider"></li>
    <li><a href="/admin/controls">Controls</a></li>
    <li class="divider"></li>
    <li><a href="/admin/sim-fault">SIM Fault</a></li>
    <li class="divider"></li>
  </ul>
</nav>
<div class="container">
"""
HTML_TAIL = """</div></body></html>"""

# ---------------- Security envelope ----------------
def _hmac_hex(key: str, msg: str) -> str:
    return hmac.new(key.encode(), msg.encode(), "sha256").hexdigest()

def _try_unwrap_envelope(raw: dict) -> Optional[dict]:
    # If envelope-like, verify and extract payload
    if all(k in raw for k in ("nonce", "payload", "mac")):
        nonce = str(raw.get("nonce"))
        payload = raw.get("payload", "")
        mac     = raw.get("mac", "")
        calc    = _hmac_hex(PSK, f"{nonce}.{payload}")
        if not hmac.compare_digest(mac, calc):
            return None
        try:
            if USE_B64:
                payload = base64.b64decode(payload)
                return json.loads(payload.decode())
            else:
                return json.loads(payload)
        except Exception:
            return None
    # Not an envelope → treat as plain JSON body
    return raw

def _wrap_envelope(obj: dict) -> dict:
    s = json.dumps(obj, separators=(",", ":"))
    payload = base64.b64encode(s.encode()).decode() if USE_B64 else s
    nonce = int(time.time()*1000)
    mac   = _hmac_hex(PSK, f"{nonce}.{payload}")
    return {"nonce": nonce, "payload": payload, "mac": mac}

# ---------------- API ----------------

@app.get("/api/health")
def health():
    return jsonify({"ok": True})
@app.get("/")
def index():
    html = f"""
    {HTML_HEAD}
    <h2> EcoWatt Admin Dashboard</h2>
    <p>Select a section below to manage your EcoWatt devices:</p>
    <div style="margin-top:20px">
      <h3> <a href="/admin">Uploads</a></h3>
      <p>Browse recent uploads and drill into details per device</p>
      
      <h3> <a href="/admin/fota">FOTA</a></h3>
      <p>View firmware-over-the-air update progress, event history, and version tracking</p>
      
      <h3> <a href="/admin/power">Power</a></h3>
      <p>Sleep/uplink timing statistics and power consumption analysis per device</p>
      
      <h3> <a href="/admin/controls">Controls</a></h3>
      <p>Device configurations and command execution</p>
      
      <h3> <a href="/admin/sim-fault">SIM Fault</a></h3>
      <p>Query the Inverter SIM API for fault diagnostics</p>
    </div>
    {HTML_TAIL}
    """
    return Response(html, mimetype="text/html")

@app.post("/api/device/upload")
def device_upload():
    # print("**********AUTH HEADER:", request.headers.get("Authorization"))

    if not _auth_ok(request.headers.get("Authorization")):
        print("⚠️ Unauthorized request")
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    raw = request.get_json(force=True, silent=True)
    if not isinstance(raw, dict):
        return jsonify({"ok": False, "error": "invalid-json"}), 400

    inner = _try_unwrap_envelope(raw)
    if inner is None:
        return jsonify(_wrap_envelope({"error": "bad-mac-or-nonce"})), 400

    body = inner
    # --- after `body = inner` validations and before preparing reply ---
    fota_in = body.get("fota") or {}
    if isinstance(fota_in, dict):
        dev = str(body["device_id"])
        # progress / next_chunk
        if "progress" in fota_in or "next_chunk" in fota_in:
            # (optional) if you also include written/total from the device, use them directly.
            # otherwise estimate written via next_chunk*chunk_size if we know the manifest.
            mf = LAST_FOTA.get(dev) or {}
            chunk_size = int(mf.get("chunk_size") or 0)
            next_chunk = int(fota_in.get("next_chunk") or 0)
            written = next_chunk * chunk_size if chunk_size else 0
            version = mf.get("version") or ""
            size    = int(mf.get("size") or 0)
            upsert_progress(dev, version, size, written, status="downloading")   # <-- writes fota_progress table
            log_fota(dev, "progress", f"next={next_chunk} written={written}")
        # verify/apply outcomes
        if "verify" in fota_in:
            log_fota(dev, "verify_ok" if fota_in["verify"] == "ok" else "verify_fail", "")
        if "apply" in fota_in:
            log_fota(dev, "apply_ok" if fota_in["apply"] == "ok" else "apply_fail", "")
        # Handle FOTA failures (corruption or boot rollback)
        if "failure" in fota_in:
            failure_info = fota_in.get("failure", {})
            reason = failure_info.get("reason", "unknown")
            version = failure_info.get("version", "unknown")
            log_fota(dev, "corruption_failed" if reason == "corruption_detected" else "rollback", f"version={version} reason={reason}")
            upsert_progress(dev, version, 0, 0, status="verify_failed" if reason == "corruption_detected" else "boot_rollback")
            upsert_fota_version(dev, version, 0, "", "verify_failed" if reason == "corruption_detected" else "boot_rollback")
            print(f"[FOTA] FAILURE for {dev}: {reason} on version {version}", flush=True)
        if fota_in.get("boot_ok") is True:
            log_fota(dev, "boot_ok", "")
            # --- cleanup old FOTA files after successful update ---
            try:
                man_path = os.path.join(LOG_DIR, "fota_manifest.json")
                # Only remove artifacts if manifest exists and matches the version we served to this device
                if os.path.exists(man_path) and dev in LAST_FOTA:
                    try:
                        mf = json.loads(open(man_path, "r").read())
                    except Exception:
                        mf = None
                    served = LAST_FOTA.get(dev, {})
                    served_ver = served.get("version")
                    mf_ver = mf.get("version") if isinstance(mf, dict) else None
                    mf_size = int(mf.get("size") or 0) if isinstance(mf, dict) else 0

                    # Check recorded progress to ensure we finished
                    db = get_db()
                    row = db.execute("SELECT written, size, percent FROM fota_progress WHERE device=?", (dev,)).fetchone()
                    written = int(row[0]) if row and row[0] is not None else 0
                    total   = int(row[1]) if row and row[1] is not None else mf_size

                    if mf_ver and served_ver and mf_ver == served_ver and (total == 0 or written >= total):
                        # safe to delete manifest + chunks for this manifest
                        try:
                            os.remove(man_path)
                        except Exception:
                            pass
                        for p in glob.glob(os.path.join(LOG_DIR, "fota_chunk_*.b64")):
                            try:
                                os.remove(p)
                            except Exception:
                                pass
                        # remove LAST_FOTA for this device
                        if dev in LAST_FOTA:
                            del LAST_FOTA[dev]
                        # mark progress as complete (100%) and keep a final event
                        upsert_progress(dev, mf_ver, total, total, status="boot_ok")
                        upsert_fota_version(dev, mf_ver, total, "", "boot_ok")
                        log_fota(dev, "boot_cleanup", f"cleaned {mf_ver}")
                        print(f"[FOTA] Cleanup done after boot_ok for {dev} manifest={mf_ver}", flush=True)
                    else:
                        print(f"[FOTA] Boot OK for {dev} but manifest mismatch or incomplete progress (mf_ver={mf_ver} served_ver={served_ver} written={written} total={total}); skipping cleanup", flush=True)
                else:
                    # no manifest present or no record of serving this device
                    if os.path.exists(man_path):
                        print(f"[FOTA] Boot OK for {dev} but no LAST_FOTA entry; not deleting {man_path}", flush=True)
                    else:
                        print(f"[FOTA] Boot OK for {dev} but no manifest file present", flush=True)
            except Exception as e:
                print(f"[FOTA] Cleanup error: {e}", flush=True)

    # --- SIM Fault reporting from device ---
    sim_fault_in = body.get("sim_fault") or {}
    if isinstance(sim_fault_in, dict) and sim_fault_in:
        fault_type = sim_fault_in.get("type", "unknown")
        exc_code = int(sim_fault_in.get("exception_code") or 0)
        description = sim_fault_in.get("description", "")
        try:
            db = get_db()
            db.execute("""
              INSERT INTO sim_faults(device, error_type, exception_code, description, status)
              VALUES(?,?,?,?,'reported')
            """, (dev, fault_type, exc_code, description))
            db.commit()
            print(f"[SIM-FAULT] device={dev} type={fault_type} exc=0x{exc_code:02x} desc={description}", flush=True)
        except Exception as e:
            print(f"[SIM-FAULT] insert error: {e}", flush=True)

    for f in ("device_id","ts_start","ts_end","codec","order","block_b64"):
        if f not in body:
            return jsonify(_wrap_envelope({"error": "missing-fields"})), 400

    try:
        blob = base64.b64decode(body["block_b64"], validate=True)
    except Exception:
        return jsonify(_wrap_envelope({"error": "bad-base64"})), 400

    dev   = str(body["device_id"])
    ts0   = int(body["ts_start"])
    ts1   = int(body["ts_end"])
    seq   = int(body.get("seq", 0))
    codec = str(body["codec"])
    order = list(body["order"])
    ts_list = body.get("ts_list")
    now_ms = int(time.time() * 1000)
    # ---- Device events (optional, best-effort) ----
    evs = body.get("events")
    if isinstance(evs, list) and evs:
        try:
            db = get_db()
            db.executemany(
                "INSERT INTO device_events(device, received_at, event) VALUES(?,?,?)",
                [(dev, now_ms, str(e)) for e in evs]
            )
            db.commit()
        except Exception as e:
            print(f"[EVT] insert error: {e}", flush=True)

    # --- Power stats (optional, device-added) ---
    ps = body.get("power_stats")
    if isinstance(ps, dict):
        try:
            t_sleep  = int(ps.get("t_sleep_ms") or 0)          # total sleep (manual + auto)
            t_manual = int(ps.get("t_manual_sleep_ms") or 0)   # manual light-sleep
            t_auto   = int(ps.get("t_auto_sleep_ms") or 0)     # auto light-sleep (estimated)
            t_uplink = int(ps.get("t_uplink_ms") or 0)
            ubytes   = int(ps.get("uplink_bytes") or 0)
            idle_b   = int(ps.get("idle_budget_ms") or 0)
            db = get_db()
            db.execute(
                "INSERT INTO power_stats(device, received_at, t_sleep_ms, t_manual_sleep_ms, t_auto_sleep_ms, t_uplink_ms, uplink_bytes, idle_budget_ms) VALUES(?,?,?,?,?,?,?,?)",
                (dev, now_ms, t_sleep, t_manual, t_auto, t_uplink, ubytes, idle_b)
            )
            db.commit()
            print(f"[PWR] dev={dev} idle={idle_b}ms sleep={t_sleep}ms (manual={t_manual}ms auto={t_auto}ms) uplink={t_uplink}ms bytes={ubytes}", flush=True)
        except Exception as e:
            print(f"[PWR] insert error: {e}", flush=True)

    # --- Diagnostic counters (buffer/transport visibility) ---
    diag = body.get("diag")
    if isinstance(diag, dict):
        try:
            dropped = int(diag.get("dropped_samples") or 0)
            acqf = int(diag.get("acq_failures") or 0)
            tf = int(diag.get("transport_failures") or 0)
            db = get_db()
            db.execute(
                "INSERT INTO buffer_stats(device, received_at, dropped_samples, acq_failures, transport_failures) VALUES(?,?,?,?,?)",
                (dev, now_ms, dropped, acqf, tf)
            )
            db.commit()
        except Exception as e:
            print(f"[DIAG] insert error: {e}", flush=True)

    # --------- Store upload ---------
    db = get_db()
    cur = db.cursor()
    cur.execute("""INSERT INTO uploads
                   (device_id, ts_start, ts_end, seq, codec, order_json, ts_list_json,
                    orig_samples, orig_bytes, received_at, block)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (dev, ts0, ts1, seq, codec, json.dumps(order),
                 json.dumps(ts_list) if ts_list is not None else None,
                 body.get("orig_samples"), body.get("orig_bytes"),
                 now_ms, blob))
    db.commit()
    rowid = cur.lastrowid

    # Console print per-sample with SCALING
    if codec == "delta_rle_v1":
        rows, _ = decode_delta_rle_v1(blob, order)
        n = len(rows)
        dev_ms_list = _device_ms_list(n, ts0, ts1)
        epoch_ms_list = _epoch_ms_list(n, ts0, ts1, now_ms, ts_list if isinstance(ts_list, list) else None)
        for i in range(n):
            t_local = datetime.datetime.fromtimestamp(epoch_ms_list[i]/1000.0).strftime("%Y-%m-%d %H:%M:%S")
            scaled  = _fmt_row(order, rows[i])
            print(f"[DECODE] {t_local} dev={dev} dev_ms={dev_ms_list[i]}  {scaled}", flush=True)

    # --------- Prepare cloud → device reply ---------
    pathlib.Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    reply = {}

    # config_update (one-shot)
    cfg_path = os.path.join(LOG_DIR, "config_update.json")
    if os.path.exists(cfg_path):
        try:
            cu = json.loads(open(cfg_path, "r").read())
            reply["config_update"] = cu
            os.remove(cfg_path)
            print(f"[QUEUE] Sent config_update -> {cu}", flush=True)
        except Exception as e:
            print("[API] bad config_update:", e)

    # command (one-shot)
    cmd_path = os.path.join(LOG_DIR, "command.json")
    if os.path.exists(cmd_path):
        try:
            cmd = json.loads(open(cmd_path, "r").read())
            reply["command"] = cmd
            os.remove(cmd_path)
            print(f"[QUEUE] Sent command -> {cmd}", flush=True)
        except Exception as e:
            print("[API] bad command:", e)

    # fota manifest (sticky until chunks done) — only send if NEW or STALLED
    man_path = os.path.join(LOG_DIR, "fota_manifest.json")
    if os.path.exists(man_path):
        try:
            mf = json.loads(open(man_path, "r").read())
            mf_version = mf.get('version')
            
            # Check if we already served this manifest to this device
            last_info = LAST_FOTA.get(dev, {})
            last_served_version = last_info.get("last_served_manifest")
            cycles_without_progress = last_info.get("cycles_without_progress", 0)
            last_next_chunk = last_info.get("next", 0)
            
            # Decide if we should resend manifest:
            # 1. New manifest (version changed)
            # 2. Download is stalled (no progress for 3+ cycles)
            # 3. Device just connected (no record yet)
            should_send_manifest = (
                last_served_version is None or  # First time
                last_served_version != mf_version or  # New version available
                cycles_without_progress >= 3  # Stalled, retry
            )
            
            if should_send_manifest:
                reply.setdefault("fota", {})["manifest"] = mf
                print(f"[QUEUE] FOTA manifest available -> version={mf.get('version')} size={mf.get('size')} chunk={mf.get('chunk_size')}", flush=True)
                # Reset stall counter since we're resending
                LAST_FOTA[dev] = LAST_FOTA.get(dev, {})
                LAST_FOTA[dev]["last_served_manifest"] = mf_version
                LAST_FOTA[dev]["cycles_without_progress"] = 0
            else:
                # Check if progress was made since last cycle
                fota_in = body.get("fota") if isinstance(body, dict) else None
                current_next = 0
                if isinstance(fota_in, dict) and "next_chunk" in fota_in:
                    try:
                        current_next = int(fota_in["next_chunk"])
                    except Exception:
                        pass
                
                if current_next > last_next_chunk:
                    # Progress made, reset stall counter
                    LAST_FOTA[dev]["cycles_without_progress"] = 0
                    print(f"[FOTA] Progress: chunk {last_next_chunk} → {current_next}", flush=True)
                else:
                    # No progress, increment stall counter
                    LAST_FOTA[dev]["cycles_without_progress"] = cycles_without_progress + 1
                    if cycles_without_progress + 1 == 2:
                        print(f"[FOTA] Stalled at chunk {current_next} for {cycles_without_progress + 1} cycles", flush=True)
        except Exception as e:
            print("[API] bad manifest:", e)

    # --- FOTA chunk serving with next_chunk awareness ---
    want_next = None
    fota_in = body.get("fota") if isinstance(body, dict) else None
    if isinstance(fota_in, dict) and "next_chunk" in fota_in:
        try:
            want_next = int(fota_in["next_chunk"])
        except Exception:
            pass

    # Remember manifest we just served to this device (only log new ones)
    if "fota" in reply and "manifest" in reply["fota"]:
        mf = reply["fota"]["manifest"]
        # Update or initialize tracking
        if dev not in LAST_FOTA:
            LAST_FOTA[dev] = {}
        
        LAST_FOTA[dev].update({
            "version": mf.get("version"),
            "size": int(mf.get("size") or 0),
            "chunk_size": int(mf.get("chunk_size") or 0),
            "next": int(want_next or 0),
            "written": int((want_next or 0) * int(mf.get("chunk_size") or 0))
        })
        
        # Log and track in DB (only if it's a NEW manifest delivery)
        if LAST_FOTA[dev].get("last_served_manifest") != mf.get("version"):
            log_fota(dev, "manifest",
                    f"v={mf.get('version')} size={mf.get('size')} cs={mf.get('chunk_size')}")
            # Track this version in the FOTA versions table
            upsert_fota_version(dev, mf.get("version"), int(mf.get("size") or 0), mf.get("hash", ""), "manifest_received")

    # Determine which chunk to send
    chunk_num = want_next
    if chunk_num is None:
        # fallback to smallest available chunk_####.b64
        files = sorted(glob.glob(os.path.join(LOG_DIR, "fota_chunk_*.b64")))
        if files:
            chunk_num = int(os.path.splitext(os.path.basename(files[0]))[0].split("_")[-1])

    if chunk_num is not None:
        p = os.path.join(LOG_DIR, f"fota_chunk_{chunk_num:04d}.b64")
        if os.path.exists(p):
            data = open(p, "r").read().strip()
            fobj = reply.setdefault("fota", {})
            fobj["chunk_number"] = chunk_num
            fobj["data"] = data
            # keep file for retry/resume
            print(f"[FOTA] Served exact chunk {chunk_num:04d} ({len(data)} b64 bytes)", flush=True)
            if dev in LAST_FOTA:
                LAST_FOTA[dev]["next"] = chunk_num + 1
                LAST_FOTA[dev]["written"] = (
                    (chunk_num + 1) * LAST_FOTA[dev].get("chunk_size", 0)
                )
                upsert_progress(
                    dev,
                    LAST_FOTA[dev]["version"],
                    LAST_FOTA[dev]["size"],
                    LAST_FOTA[dev]["written"],
                    status="downloading"
                )

    return jsonify(_wrap_envelope(reply)), 200

# ---- Admin: list + detail (with SCALED column) ----
@app.get("/admin")
def admin_home():
    cur = get_db().cursor()
    cur.execute("SELECT id, device_id, ts_start, ts_end, codec, received_at FROM uploads ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    out = [HTML_HEAD, "<h2>Recent uploads</h2><table class='table'><tr><th>ID</th><th>Device</th><th>Dev ms</th><th>Codec</th><th>Received (server)</th></tr>"]
    for (id_, dev, ts0, ts1, codec, recv) in rows:
        recvt = datetime.datetime.fromtimestamp(recv/1000.0).strftime("%Y-%m-%d %H:%M:%S")
        out.append(f"<tr><td><a href='/admin/upload/{id_}'>{id_}</a></td>"
                   f"<td>{dev}</td><td>{ts0} → {ts1}</td><td>{codec}</td><td>{recvt}</td></tr>")    
    out.append("</table>" + HTML_TAIL)
    return Response("".join(out), mimetype="text/html")

@app.get("/admin/uploads")
def admin_uploads():
    cur = get_db().cursor()
    cur.execute("""SELECT id, device_id, ts_start, ts_end, codec, received_at 
                   FROM uploads ORDER BY id DESC LIMIT 200""")
    rows = cur.fetchall()
    out = [HTML_HEAD, "<h2>Recent Data Uploads</h2>"]
    out.append("<table class='table'>")
    out.append("<tr><th>ID</th><th>Device</th><th>Time Range</th><th>Codec</th><th>Received</th></tr>")
    for (id_, dev, ts0, ts1, codec, recv) in rows:
        recvt = datetime.datetime.fromtimestamp(recv/1000.0).strftime("%Y-%m-%d %H:%M:%S")
        out.append(f"<tr>")
        out.append(f"  <td><a href='/admin/upload/{id_}'>{id_}</a></td>")
        out.append(f"  <td>{dev}</td>")
        out.append(f"  <td>{ts0} → {ts1}</td>")
        out.append(f"  <td>{codec}</td>")
        out.append(f"  <td>{recvt}</td>")
        out.append(f"</tr>")
    out.append("</table>")
    out.append(HTML_TAIL)
    return Response("".join(out), mimetype="text/html")

@app.get("/admin/upload/<int:rowid>")
def admin_view(rowid: int):
    cur = get_db().cursor()
    cur.execute("""SELECT device_id, ts_start, ts_end, seq, codec, order_json, ts_list_json,
                          orig_samples, orig_bytes, received_at, block
                   FROM uploads WHERE id=?""", (rowid,))
    row = cur.fetchone()
    if not row:
        return Response(HTML_HEAD + "<h3>Not found</h3>" + HTML_TAIL, mimetype="text/html")
    dev, ts0, ts1, seq, codec, order_json, ts_list_json, orig_samples, orig_bytes, recv, blob = row
    order = json.loads(order_json)
    ts_list = json.loads(ts_list_json) if ts_list_json else None
    recv_h = datetime.datetime.fromtimestamp(recv/1000.0).strftime("%Y-%m-%d %H:%M:%S")

    rows = []; note = ""
    if codec == "delta_rle_v1":
        rows, note = decode_delta_rle_v1(blob, order)

    out = [HTML_HEAD, "<h2>Upload detail</h2><pre class='mono'>"]
    out.append(f"Upload ID      : {rowid}\n")
    out.append(f"Device ID      : {dev}\n")
    out.append(f"Device ms range: {ts0} -> {ts1}  (ms since boot)\n")
    out.append(f"Server received: {recv_h} (local time)\n")
    out.append(f"Codec          : {codec}\n")
    out.append(f"Compressed size: {len(blob)} bytes\n")
    out.append(f"Order          : {', '.join(order)}\n")
    if ts_list:
        out.append(f"ts_list        : {len(ts_list)} items (exact per-sample timestamps)\n")
    out.append(f"\n[Decoded {len(rows)} samples — ok{' | using ts_list' if ts_list else ''}]\n")
    out.append("</pre>")

    if rows:
        out.append("<table class='table'><tr><th>#</th><th>dev_ms</th><th>time</th><th>raw</th><th>scaled</th></tr>")
        n = len(rows)
        dev_ms_list = _device_ms_list(n, ts0, ts1)
        epoch_ms_list = _epoch_ms_list(n, ts0, ts1, recv, ts_list)
        for i, raw in enumerate(rows):
            t_local = datetime.datetime.fromtimestamp(epoch_ms_list[i]/1000.0).strftime("%Y-%m-%d %H:%M:%S")
            out.append(f"<tr><td>{i}</td><td>{dev_ms_list[i]}</td><td>{t_local}</td>"
                       f"<td class='mono'>{' '.join(str(x) for x in raw)}</td>"
                       f"<td>{_fmt_row(order, raw)}</td></tr>")
        out.append("</table>")

    # hex dump
    hexrows = []
    for i in range(0, len(blob), 16):
        chunk = blob[i:i+16]
        hexrows.append(f"{i:04X}: " + " ".join(f"{x:02X}" for x in chunk))
    out.append("<h3>Compressed block (hex dump)</h3><pre class='mono'>")
    out.append("\n".join(hexrows))
    out.append("</pre>" + HTML_TAIL)
    return Response("".join(out), mimetype="text/html")

# Optional JSON view (raw + scaled) for tooling
@app.get("/api/upload/<int:rowid>.json")
def api_decoded(rowid: int):
    cur = get_db().cursor()
    cur.execute("SELECT device_id, ts_start, ts_end, codec, order_json, ts_list_json, received_at, block FROM uploads WHERE id=?", (rowid,))
    row = cur.fetchone()
    if not row: return jsonify({"ok": False, "error": "not-found"}), 404
    dev, ts0, ts1, codec, order_json, ts_list_json, recv, blob = row
    order = json.loads(order_json)
    ts_list = json.loads(ts_list_json) if ts_list_json else None

    rows, note = decode_delta_rle_v1(blob, order) if codec=="delta_rle_v1" else ([], "unsupported")
    scaled = [_fmt_row(order, r) for r in rows]
    n = len(rows)
    device_ms = _device_ms_list(n, ts0, ts1)
    times_ms  = _epoch_ms_list(n, ts0, ts1, recv, ts_list)

    return jsonify({
        "ok": True,
        "device_id": dev,
        "codec": codec,
        "order": order,
        "rows_raw": rows,
        "rows_scaled": scaled,
        "device_ms": device_ms,
        "times_ms": times_ms,
        "decode_note": note,
        "received_at_ms": recv
    })

@app.get("/admin/fota")
def admin_fota():
    db = get_db()
    prog = db.execute("""
      SELECT device, version, size, written, percent, status, updated
      FROM fota_progress
      ORDER BY updated DESC
    """).fetchall()
    events = db.execute("""
      SELECT ts, device, kind, detail
      FROM fota_events
      ORDER BY ts DESC, id DESC
      LIMIT 200
    """).fetchall()

    out = [HTML_HEAD, "<h2>FOTA – Upload New Firmware</h2>"]
    out.append("""
    <div style="background:#f5f5f5; padding:20px; border-radius:5px; margin-bottom:30px;">
      <form id="fota-upload-form" enctype="multipart/form-data">
        <div style="margin-bottom:15px;">
          <label for="fota-file"><strong>Binary File (.bin):</strong></label><br/>
          <input type="file" id="fota-file" name="file" accept=".bin" required style="padding:8px; width:400px;">
        </div>
        <div style="margin-bottom:15px;">
          <label for="fota-version"><strong>Firmware Version:</strong></label><br/>
          <input type="text" id="fota-version" name="version" placeholder="e.g., 1.0.8" required style="padding:8px; width:200px;">
        </div>
        <div style="margin-bottom:15px;">
          <label for="fota-chunk"><strong>Chunk Size (bytes):</strong></label><br/>
          <input type="number" id="fota-chunk" name="chunk_size" value="8192" min="512" max="65536" style="padding:8px; width:150px;">
        </div>
        <button type="button" onclick="uploadFotaBinary()" style="padding:10px 20px; background:#0066cc; color:white; border:none; border-radius:3px; cursor:pointer; font-size:14px;">
          Upload & Generate Chunks
        </button>
        <span id="upload-status" style="margin-left:20px; font-weight:bold;"></span>
      </form>
      <div id="upload-result" style="margin-top:20px; padding:10px; background:white; border:1px solid #ddd; border-radius:3px; display:none;">
        <strong id="result-title">Upload Result</strong>
        <pre id="result-text" style="margin-top:10px; white-space:pre-wrap; word-wrap:break-word;"></pre>
      </div>
    </div>

    <script>
    async function uploadFotaBinary() {
      const fileInput = document.getElementById('fota-file');
      const versionInput = document.getElementById('fota-version');
      const chunkInput = document.getElementById('fota-chunk');
      const statusSpan = document.getElementById('upload-status');
      const resultDiv = document.getElementById('upload-result');
      const resultText = document.getElementById('result-text');
      
      if (!fileInput.files.length) {
        statusSpan.textContent = '❌ Please select a file';
        statusSpan.style.color = 'red';
        return;
      }
      
      if (!versionInput.value.trim()) {
        statusSpan.textContent = '❌ Please enter a version';
        statusSpan.style.color = 'red';
        return;
      }
      
      statusSpan.textContent = '⏳ Uploading...';
      statusSpan.style.color = 'orange';
      resultDiv.style.display = 'none';
      
      const formData = new FormData();
      formData.append('file', fileInput.files[0]);
      formData.append('version', versionInput.value);
      formData.append('chunk_size', chunkInput.value);
      
      try {
        const response = await fetch('/api/fota/upload', {
          method: 'POST',
          body: formData
        });
        const data = await response.json();
        
        if (data.ok) {
          statusSpan.textContent = '✅ Upload successful!';
          statusSpan.style.color = 'green';
          resultDiv.style.display = 'block';
          resultDiv.style.background = '#e8f5e9';
          resultText.textContent = 
            'Version: ' + data.version + '\\n' +
            'Size: ' + data.size + ' bytes\\n' +
            'Hash: ' + data.hash + '\\n' +
            'Chunks: ' + data.num_chunks + '\\n' +
            'Chunk Size: ' + data.chunk_size + ' bytes\\n' +
            'Manifest: ' + data.manifest_path + '\\n\\n' +
            'Ready to serve to devices!';
          fileInput.value = '';
          versionInput.value = '';
        } else {
          statusSpan.textContent = '❌ Error: ' + (data.error || 'Unknown error');
          statusSpan.style.color = 'red';
          resultDiv.style.display = 'block';
          resultDiv.style.background = '#ffebee';
          resultText.textContent = JSON.stringify(data, null, 2);
        }
      } catch (err) {
        statusSpan.textContent = '❌ Error: ' + err.message;
        statusSpan.style.color = 'red';
        resultDiv.style.display = 'block';
        resultDiv.style.background = '#ffebee';
        resultText.textContent = err.message;
      }
    }
    </script>

    <h2>FOTA – Progress (all devices)</h2>""")
    out.append("<table class='table'><tr><th>Device</th><th>Version</th><th>Written</th><th>Size</th><th>Percent</th><th>Status</th><th>Updated</th></tr>")
    for d,v,sz,wr,pct,status,upd in prog:
        status_display = status or "pending"
        # Color-code status
        if status_display == "boot_ok":
            status_color = "style='color:green;font-weight:bold'"
        elif status_display in ("verify_failed", "boot_rollback"):
            status_color = "style='color:red;font-weight:bold'"
        elif status_display == "downloading":
            status_color = "style='color:orange'"
        else:
            status_color = ""
        out.append(
            f"<tr>"
            f"<td><a href='/admin/fota/{d}'>{d}</a></td>"
            f"<td>{v}</td>"
            f"<td>{wr}</td>"
            f"<td>{sz}</td>"
            f"<td>{pct}%</td>"
            f"<td {status_color}>{status_display}</td>"
            f"<td>{upd}</td>"
            f"</tr>"
        )
    out.append("</table>")

    out.append("<h2>Recent FOTA Events</h2>")
    out.append("<table class='table'><tr><th>Time</th><th>Device</th><th>Kind</th><th>Detail</th></tr>")
    for ts, dev, kind, detail in events:
        # Color-code event types
        if kind in ("corruption_failed", "rollback", "verify_fail"):
            event_color = "style='color:red;font-weight:bold'"
        elif kind in ("verify_ok", "boot_ok"):
            event_color = "style='color:green;font-weight:bold'"
        else:
            event_color = ""
        out.append(f"<tr><td>{ts}</td><td>{dev}</td><td {event_color}>{kind}</td><td class='mono'>{detail}</td></tr>")
    out.append("</table>" + HTML_TAIL)
    return Response("".join(out), mimetype="text/html")

@app.get("/admin/fota/<device>")
def admin_fota_device(device: str):
    db = get_db()
    prog = db.execute("""
      SELECT device, version, size, written, percent, status, updated
      FROM fota_progress WHERE device=?
    """, (device,)).fetchone()
    events = db.execute("""
      SELECT ts, device, kind, detail
      FROM fota_events
      WHERE device=?
      ORDER BY ts DESC, id DESC
      LIMIT 500
    """, (device,)).fetchall()
    
    # Get version history
    versions = db.execute("""
      SELECT version, size, status, created_at, updated_at
      FROM fota_versions
      WHERE device=?
      ORDER BY updated_at DESC
      LIMIT 50
    """, (device,)).fetchall()

    out = [HTML_HEAD, f"<h2>FOTA – {device}</h2>"]
    out.append("<h3>Current progress</h3>")
    if prog:
        d,v,sz,wr,pct,status,upd = prog
        status_display = status or "pending"
        out.append("<table class='table'><tr><th>Version</th><th>Written</th><th>Size</th><th>Percent</th><th>Status</th><th>Updated</th></tr>")
        out.append(f"<tr><td>{v}</td><td>{wr}</td><td>{sz}</td><td>{pct}%</td><td><strong>{status_display}</strong></td><td>{upd}</td></tr></table>")
    else:
        out.append("<p>No progress recorded.</p>")

    if versions:
        out.append("<h3>Version History</h3>")
        out.append("<table class='table'><tr><th>Version</th><th>Size</th><th>Status</th><th>Created</th><th>Updated</th></tr>")
        for ver, size, status, created, updated in versions:
            # Color-code status
            if status == "boot_ok":
                status_color = "style='color:green;font-weight:bold'"
            elif status in ("verify_failed", "boot_rollback"):
                status_color = "style='color:red;font-weight:bold'"
            elif status == "downloading":
                status_color = "style='color:orange'"
            else:
                status_color = ""
            out.append(f"<tr><td>{ver}</td><td>{size}</td><td {status_color}>{status or 'unknown'}</td><td>{created}</td><td>{updated}</td></tr>")
        out.append("</table>")

    out.append("<h3>Event timeline</h3>")
    out.append("<table class='table'><tr><th>Time</th><th>Kind</th><th>Detail</th></tr>")
    for ts, _dev, kind, detail in events:
        # Color-code event types
        if kind in ("corruption_failed", "rollback", "verify_fail"):
            event_color = "style='color:red;font-weight:bold'"
        elif kind in ("verify_ok", "boot_ok"):
            event_color = "style='color:green;font-weight:bold'"
        else:
            event_color = ""
        out.append(f"<tr><td>{ts}</td><td {event_color}>{kind}</td><td class='mono'>{detail}</td></tr>")
    out.append("</table>" + HTML_TAIL)
    return Response("".join(out), mimetype="text/html")


@app.route("/admin/fota/cleanup", methods=["POST"])
def admin_fota_cleanup():
    # Manual cleanup: deletes manifest and chunk files if present.
    # Body/form: optional 'version' to restrict deletion to a specific manifest version.
    ver = request.form.get("version") or request.args.get("version")
    man_path = os.path.join(LOG_DIR, "fota_manifest.json")
    if not os.path.exists(man_path):
        return jsonify({"ok": False, "error": "no-manifest"}), 404
    try:
        mf = json.loads(open(man_path, "r").read())
    except Exception as e:
        return jsonify({"ok": False, "error": "bad-manifest", "detail": str(e)}), 400
    mf_ver = mf.get("version")
    if ver and ver != mf_ver:
        return jsonify({"ok": False, "error": "version-mismatch", "manifest_version": mf_ver}), 400
    # delete manifest + chunks
    try:
        os.remove(man_path)
    except Exception:
        pass
    removed = 0
    for p in glob.glob(os.path.join(LOG_DIR, "fota_chunk_*.b64")):
        try:
            os.remove(p); removed += 1
        except Exception:
            pass
    return jsonify({"ok": True, "removed_chunks": removed, "manifest_version": mf_ver})

@app.get("/admin/fota/upload")
def admin_fota_upload():
    out = [HTML_HEAD, """
    <h2>FOTA – Upload New Firmware</h2>
    <p class="info">Select a binary firmware file, provide a version number, and this tool will generate all FOTA chunks.</p>
    
    <div style="background:white; padding:20px; border-radius:5px; border:1px solid #ddd; margin-bottom:20px;">
      <form id="fota-upload-form" enctype="multipart/form-data">
        <div class="form-group">
          <label for="fota-file"><strong>📁 Binary File (.bin):</strong></label>
          <input type="file" id="fota-file" name="file" accept=".bin" required>
          <small style="color:#666;">Select your compiled ESP32 firmware binary</small>
        </div>
        
        <div class="form-group">
          <label for="fota-version"><strong>📌 Firmware Version:</strong></label>
          <input type="text" id="fota-version" name="version" placeholder="e.g., 1.0.8" required>
          <small style="color:#666;">Bump version to force a fresh OTA session (e.g., 1.0.7 → 1.0.8)</small>
        </div>
        
        <div class="form-group">
          <label for="fota-chunk"><strong>⚙️ Chunk Size (bytes):</strong></label>
          <input type="number" id="fota-chunk" name="chunk_size" value="8192" min="512" max="65536">
          <small style="color:#666;">Larger chunks = fewer uploads, but less resilient to interruptions</small>
        </div>
        
        <button type="button" onclick="uploadFotaBinary()">🚀 Upload & Generate Chunks</button>
        <span id="upload-status" style="margin-left:20px; font-weight:bold;"></span>
      </form>
      
      <div id="upload-result" style="margin-top:20px; display:none;">
        <div id="result-content"></div>
      </div>
    </div>
    
    <h3>Upload Progress</h3>
    <div id="progress-container" style="margin:20px 0; display:none;">
      <div style="background:#e3f2fd; padding:10px; border-radius:4px; margin-bottom:10px;">
        <strong>Processing:</strong> <span id="progress-text">0%</span>
      </div>
      <div style="background:#f0f0f0; height:20px; border-radius:4px; overflow:hidden;">
        <div id="progress-bar" style="background:#0066cc; height:100%; width:0%; transition:width 0.2s;"></div>
      </div>
    </div>
    
    <script>
    async function uploadFotaBinary() {
      const fileInput = document.getElementById('fota-file');
      const versionInput = document.getElementById('fota-version');
      const chunkInput = document.getElementById('fota-chunk');
      const statusSpan = document.getElementById('upload-status');
      const resultDiv = document.getElementById('upload-result');
      const resultContent = document.getElementById('result-content');
      const progressContainer = document.getElementById('progress-container');
      const progressBar = document.getElementById('progress-bar');
      const progressText = document.getElementById('progress-text');
      
      if (!fileInput.files.length) {
        alert('Please select a binary file');
        return;
      }
      
      if (!versionInput.value.trim()) {
        alert('Please enter a firmware version');
        return;
      }
      
      const file = fileInput.files[0];
      const version = versionInput.value.trim();
      const chunk_size = parseInt(chunkInput.value) || 8192;
      
      statusSpan.textContent = '📤 Uploading and processing...';
      resultDiv.style.display = 'none';
      progressContainer.style.display = 'block';
      
      const formData = new FormData();
      formData.append('file', file);
      formData.append('version', version);
      formData.append('chunk_size', chunk_size);
      
      try {
        const response = await fetch('/api/fota/upload', {
          method: 'POST',
          body: formData
        });
        
        const result = await response.json();
        progressContainer.style.display = 'none';
        
        if (response.ok && result.ok) {
          statusSpan.textContent = '✅ Success!';
          statusSpan.style.color = '#4caf50';
          const html = `
            <div class="success">
              <strong>✅ Firmware uploaded successfully!</strong><br/>
              Version: <code>${result.version}</code><br/>
              Size: <code>${result.size}</code> bytes<br/>
              Hash: <code style="word-break:break-all;">${result.hash}</code><br/>
              Chunks: <code>${result.chunks}</code><br/>
              <br/>
              <em>Ready for devices to download. New devices connecting will receive this firmware.</em>
            </div>
          `;
          document.getElementById('result-content').innerHTML = html;
        } else {
          statusSpan.textContent = '❌ Error';
          statusSpan.style.color = '#f44336';
          const html = `<div class="error"><strong>❌ Upload failed:</strong><br/>${result.error || 'Unknown error'}</div>`;
          document.getElementById('result-content').innerHTML = html;
        }
        resultDiv.style.display = 'block';
      } catch (error) {
        statusSpan.textContent = '❌ Error: ' + error.message;
        statusSpan.style.color = '#f44336';
        progressContainer.style.display = 'none';
        const html = `<div class="error"><strong>❌ Upload failed:</strong><br/>${error.message}</div>`;
        document.getElementById('result-content').innerHTML = html;
        resultDiv.style.display = 'block';
      }
    }
    </script>
    """]
    
    out.append(HTML_TAIL)
    return Response("".join(out), mimetype="text/html")

@app.route("/api/fota/upload", methods=["POST"])
def api_fota_upload():
    """
    Upload a binary firmware file and generate FOTA manifest + chunks.
    Expected form data:
      - file: binary file upload
      - version: firmware version (e.g., "1.0.8")
      - chunk_size: (optional) chunk size in bytes, default 8192
    """
    try:
        # Validate inputs
        if "file" not in request.files:
            return jsonify({"ok": False, "error": "missing-file"}), 400
        if "version" not in request.form:
            return jsonify({"ok": False, "error": "missing-version"}), 400
        
        file = request.files["file"]
        version = request.form.get("version", "").strip()
        chunk_size = int(request.form.get("chunk_size", 8192))
        
        if not file or file.filename == "":
            return jsonify({"ok": False, "error": "empty-file"}), 400
        if not version:
            return jsonify({"ok": False, "error": "empty-version"}), 400
        if chunk_size < 512 or chunk_size > 65536:
            return jsonify({"ok": False, "error": "invalid-chunk-size"}), 400
        
        # Read binary data
        binary_data = file.read()
        if not binary_data:
            return jsonify({"ok": False, "error": "empty-binary"}), 400
        
        size = len(binary_data)
        
        # Calculate SHA256 hash
        hash_hex = hashlib.sha256(binary_data).hexdigest().lower()
        
        # Create logs directory if needed
        os.makedirs(LOG_DIR, exist_ok=True)
        
        # Write manifest
        manifest = {
            "version": version,
            "size": size,
            "hash": hash_hex,
            "chunk_size": chunk_size
        }
        man_path = os.path.join(LOG_DIR, "fota_manifest.json")
        with open(man_path, "w") as f:
            json.dump(manifest, f)
        
        # Generate and write chunks
        num_chunks = 0
        for offset in range(0, size, chunk_size):
            length = min(chunk_size, size - offset)
            chunk_data = binary_data[offset:offset + length]
            chunk_b64 = base64.b64encode(chunk_data).decode("ascii")
            
            chunk_path = os.path.join(LOG_DIR, f"fota_chunk_{num_chunks:04d}.b64")
            with open(chunk_path, "w") as f:
                f.write(chunk_b64)
            
            num_chunks += 1
        
        # Log in database
        log_fota("server", "upload_binary", f"version={version} size={size} chunks={num_chunks} hash={hash_hex[:16]}...")
        
        return jsonify({
            "ok": True,
            "version": version,
            "size": size,
            "hash": hash_hex,
            "num_chunks": num_chunks,
            "chunk_size": chunk_size,
            "manifest_path": man_path
        })
    
    except Exception as e:
        return jsonify({"ok": False, "error": "exception", "detail": str(e)}), 500

@app.get("/api/fota/progress")
def api_fota_progress_all():
    db = get_db()
    rows = db.execute("""
      SELECT device, version, size, written, percent, updated
      FROM fota_progress
      ORDER BY updated DESC
    """).fetchall()
    return jsonify([
        {"device":r[0], "version":r[1], "size":r[2], "written":r[3], "percent":r[4], "updated":r[5]}
        for r in rows
    ])


@app.get("/api/fota/progress/<device>")
def api_fota_progress_device(device: str):
    db = get_db()
    r = db.execute("""
      SELECT device, version, size, written, percent, updated
      FROM fota_progress WHERE device=?
    """, (device,)).fetchone()
    if not r:
        return jsonify({"ok": False, "error": "not-found"}), 404
    return jsonify({"device":r[0], "version":r[1], "size":r[2], "written":r[3], "percent":r[4], "updated":r[5], "ok": True})


@app.get("/api/fota/events")
def api_fota_events_all():
    db = get_db()
    rows = db.execute("""
      SELECT ts, device, kind, detail
      FROM fota_events
      ORDER BY ts DESC, id DESC
      LIMIT 1000
    """).fetchall()
    return jsonify([
        {"ts":r[0], "device":r[1], "kind":r[2], "detail":r[3]}
        for r in rows
    ])


@app.get("/api/fota/events/<device>")
def api_fota_events_device(device: str):
    db = get_db()
    rows = db.execute("""
      SELECT ts, device, kind, detail
      FROM fota_events
      WHERE device=?
      ORDER BY ts DESC, id DESC
      LIMIT 1000
    """, (device,)).fetchall()
    return jsonify([
        {"ts":r[0], "device":r[1], "kind":r[2], "detail":r[3]}
        for r in rows
    ])

# ========== SIM FAULT PAGES ==========
@app.get("/admin/sim-fault")
def admin_sim_fault():
    """SIM Fault Injection Control Panel"""
    db = get_db()
    # Get all devices with recent activity
    devices = db.execute("""
      SELECT DISTINCT device_id as device FROM uploads 
      UNION SELECT DISTINCT device FROM device_events
      UNION SELECT DISTINCT device FROM sim_faults
      ORDER BY device
    """).fetchall()
    
    # Get recent faults across all devices
    faults = db.execute("""
      SELECT id, device, error_type, exception_code, delay_ms, status, created_at, triggered_at
      FROM sim_faults
      ORDER BY created_at DESC
      LIMIT 100
    """).fetchall()
    
    exception_codes = {
        "01": "Illegal Function",
        "02": "Illegal Data Address",
        "03": "Illegal Data Value",
        "04": "Slave Device Failure",
        "05": "Acknowledge",
        "06": "Slave Device Busy",
        "08": "Memory Parity Error",
        "0A": "Gateway Path Unavailable",
        "0B": "Gateway Target Device Failed"
    }
    
    html = f"""
    {HTML_HEAD}
    <div class="navbar">
        <a href="/"><strong>EcoWatt</strong></a>
        <a href="/admin/uploads">Uploads</a>
        <a href="/admin/fota">FOTA</a>
        <a href="/admin/power">Power</a>
        <a href="/admin/controls">Controls</a>
        <a href="/admin/sim-fault" class="active">SIM Fault</a>
    </div>
    <div class="container">
        <h1>SIM Fault Injection Testing</h1>
        
        <div style="background:#f9f9f9;padding:15px;border-radius:5px;margin-bottom:20px;">
            <h3>Inject Fault for Next Request</h3>
            <form id="faultForm" style="display:grid;gap:10px;max-width:400px;">
                <label>
                    Device:
                    <select name="device" id="device" required>
                        <option value="">-- Select Device --</option>
    """
    
    for (dev,) in devices:
        html += f'<option value="{dev}">{dev}</option>\n'
    
    html += f"""
                    </select>
                </label>
                <label>
                    Error Type:
                    <select name="error_type" id="error_type" onchange="updateExceptionField()" required>
                        <option value="">-- Select Error Type --</option>
                        <option value="EXCEPTION">Exception (Modbus)</option>
                        <option value="CRC_ERROR">CRC Error</option>
                        <option value="CORRUPT">Corrupt Response</option>
                        <option value="PACKET_DROP">Packet Drop</option>
                        <option value="DELAY">Delay (ms)</option>
                    </select>
                </label>
                <label>
                    Exception Code:
                    <select name="exception_code" id="exception_code" style="display:none;">
                        <option value="0">-- Select Code --</option>
    """
    
    for code, desc in exception_codes.items():
        html += f'<option value="{int(code, 16):02d}">{code} - {desc}</option>\n'
    
    html += f"""
                    </select>
                </label>
                <label>
                    Delay (ms):
                    <input type="number" name="delay_ms" id="delay_ms" min="0" max="60000" value="5000" style="display:none;">
                </label>
                <button type="button" onclick="submitFaultForm()" style="padding:10px;background:#d9534f;color:white;border:none;cursor:pointer;border-radius:3px;">
                    ⚠️ Queue Fault
                </button>
            </form>
            <div id="notification" style="display:none;margin-top:15px;padding:10px;border-radius:3px;font-weight:bold;"></div>
        </div>
        
        <h3>Recent Faults</h3>
        <table class="table" style="font-size:12px;">
            <tr>
                <th>ID</th><th>Device</th><th>Error Type</th><th>Exception</th><th>Delay</th>
                <th>Status</th><th>Created</th><th>Triggered</th>
            </tr>
    """
    
    for row in faults:
        id_, dev, err_type, exc_code, delay, status, created, triggered = row
        exc_str = f"{exc_code:02x}" if exc_code else "-"
        delay_str = f"{delay}ms" if delay else "-"
        triggered_str = triggered if triggered else "-"
        
        status_color = "green" if status == "triggered" else "orange" if status == "queued" else "gray"
        html += f"""
            <tr style="border-bottom:1px solid #ddd;">
                <td>{id_}</td>
                <td><strong>{dev}</strong></td>
                <td>{err_type}</td>
                <td>{exc_str}</td>
                <td>{delay_str}</td>
                <td style="color:{status_color};font-weight:bold;">{status}</td>
                <td style="font-size:11px;">{created}</td>
                <td style="font-size:11px;">{triggered_str}</td>
            </tr>
        """
    
    html += f"""
        </table>
        
        <h3>Device-Reported Faults (Last 50)</h3>
        <table class="table" style="font-size:12px;">
            <tr>
                <th>Device</th><th>Error Type</th><th>Exception Code</th><th>Description</th>
                <th>Status</th><th>Reported At</th>
            </tr>
    """
    
    # Get device-reported faults
    reported_faults = db.execute("""
      SELECT device, error_type, exception_code, description, status, created_at
      FROM sim_faults
      WHERE status='reported'
      ORDER BY created_at DESC
      LIMIT 50
    """).fetchall()
    
    for dev, err_type, exc_code, description, status, created in reported_faults:
        exc_str = f"0x{exc_code:02x}" if exc_code else "-"
        
        html += f"""
            <tr style="border-bottom:1px solid #ddd;">
                <td><strong>{dev}</strong></td>
                <td>{err_type}</td>
                <td>{exc_str}</td>
                <td class='mono' style='font-size:11px;'>{description[:100]}</td>
                <td style="color:red;font-weight:bold;">reported</td>
                <td style="font-size:11px;">{created}</td>
            </tr>
        """
    
    html += f"""
        </table>
        
        <h3>Fault Type Reference</h3>
        <table class="table" style="font-size:12px;">
            <tr><th>Type</th><th>Description</th><th>Use Case</th></tr>
            <tr>
                <td><strong>EXCEPTION</strong></td>
                <td>Modbus exception with code (01-0B)</td>
                <td>Test device handling of inverter errors</td>
            </tr>
            <tr>
                <td><strong>CRC_ERROR</strong></td>
                <td>Invalid CRC in response frame</td>
                <td>Test frame corruption detection</td>
            </tr>
            <tr>
                <td><strong>CORRUPT</strong></td>
                <td>Malformed or corrupted response</td>
                <td>Test parsing robustness</td>
            </tr>
            <tr>
                <td><strong>PACKET_DROP</strong></td>
                <td>Drop the response packet entirely</td>
                <td>Test timeout handling</td>
            </tr>
            <tr>
                <td><strong>DELAY</strong></td>
                <td>Delay response by N milliseconds</td>
                <td>Test slow network conditions</td>
            </tr>
        </table>
    </div>
    
    <script>
    function updateExceptionField() {{
        const type = document.getElementById('error_type').value;
        const excField = document.getElementById('exception_code');
        const delayField = document.getElementById('delay_ms').parentElement;
        
        if (type === 'EXCEPTION') {{
            excField.style.display = '';
            delayField.style.display = 'none';
        }} else if (type === 'DELAY') {{
            excField.style.display = 'none';
            delayField.style.display = '';
        }} else {{
            excField.style.display = 'none';
            delayField.style.display = 'none';
        }}
    }}
    
    function showNotification(message, isSuccess) {{
        const notif = document.getElementById('notification');
        notif.textContent = message;
        notif.style.display = 'block';
        notif.style.backgroundColor = isSuccess ? '#d4edda' : '#f8d7da';
        notif.style.color = isSuccess ? '#155724' : '#721c24';
        notif.style.borderLeft = (isSuccess ? '#28a745' : '#dc3545') + ' 4px solid';
        
        setTimeout(() => {{
            notif.style.display = 'none';
        }}, 5000);
    }}
    
    async function submitFaultForm() {{
        const form = document.getElementById('faultForm');
        const device = document.getElementById('device').value;
        const errorType = document.getElementById('error_type').value;
        const exceptionCode = document.getElementById('exception_code').value;
        const delayMs = document.getElementById('delay_ms').value;
        
        if (!device || !errorType) {{
            showNotification('Please select both Device and Error Type', false);
            return;
        }}
        
        const formData = new FormData();
        formData.append('device', device);
        formData.append('error_type', errorType);
        formData.append('exception_code', exceptionCode);
        formData.append('delay_ms', delayMs);
        
        try {{
            const response = await fetch('/api/sim-fault/inject', {{
                method: 'POST',
                body: formData
            }});
            
            const data = await response.json();
            
            if (data.ok) {{
                showNotification(`✓ Fault queued for ${{device}}. Inverter API: ${{data.inverter_api}}`, true);
                // Reload the page after 2 seconds to show the new fault in the tables
                setTimeout(() => location.reload(), 2000);
            }} else {{
                showNotification(`✗ Error: ${{data.error}}`, false);
            }}
        }} catch (error) {{
            showNotification(`✗ Network error: ${{error.message}}`, false);
        }}
    }}
    </script>
    {HTML_TAIL}
    """
    return Response(html, mimetype="text/html")

@app.post("/api/sim-fault/inject")
def api_sim_fault_inject():
    """Queue a fault injection for a device"""
    device = request.form.get("device", "").strip()
    error_type = request.form.get("error_type", "").strip()
    
    if not device or not error_type:
        return jsonify({"ok": False, "error": "missing-device-or-type"}), 400
    
    exception_code = 0
    delay_ms = 0
    
    if error_type == "EXCEPTION":
        try:
            exception_code = int(request.form.get("exception_code", 0))
        except:
            exception_code = 0
    elif error_type == "DELAY":
        try:
            delay_ms = int(request.form.get("delay_ms", 5000))
        except:
            delay_ms = 5000
    
    # Queue in database
    queue_sim_fault(device, error_type, exception_code, delay_ms)
    
    # Try to inject at inverter SIM immediately (for testing)
    success = trigger_sim_fault_at_inverter(error_type, exception_code, delay_ms)
    
    return jsonify({
        "ok": True, 
        "message": f"Fault queued for {device}",
        "inverter_api": "success" if success else "failed"
    })

@app.get("/admin/sim-fault/<device>")
def admin_sim_fault_device(device: str):
    """View SIM fault history for a specific device"""
    db = get_db()
    faults = get_sim_fault_history(device)
    
    html = f"""
    {HTML_HEAD}
    
    <div class="container">
        <h1>SIM Fault History – {device}</h1>
        <a href="/admin/sim-fault">← Back to SIM Fault Dashboard</a>
        <br><br>
        <table class="table" style="font-size:12px;">
            <tr>
                <th>ID</th><th>Error Type</th><th>Exception</th><th>Delay</th>
                <th>Status</th><th>Created</th><th>Triggered</th>
            </tr>
    """
    
    for row in faults:
        id_, err_type, exc_code, delay, status, created, triggered = row
        exc_str = f"{exc_code:02x}" if exc_code else "-"
        delay_str = f"{delay}ms" if delay else "-"
        triggered_str = triggered if triggered else "-"
        
        status_color = "green" if status == "triggered" else "orange" if status == "queued" else "gray"
        html += f"""
            <tr>
                <td>{id_}</td>
                <td>{err_type}</td>
                <td>{exc_str}</td>
                <td>{delay_str}</td>
                <td style="color:{status_color};font-weight:bold;">{status}</td>
                <td style="font-size:11px;">{created}</td>
                <td style="font-size:11px;">{triggered_str}</td>
            </tr>
        """
    
    html += f"""
        </table>
    </div>
    {HTML_TAIL}
    """
    return Response(html, mimetype="text/html")

@app.get("/admin/power")
def admin_power():
    db = get_db()
    # Summary by device (recent averages)
    summary = db.execute("""
       SELECT device,
            COUNT(*)                    AS n,
            ROUND(AVG(idle_budget_ms),1) AS avg_idle_ms,
            ROUND(AVG(t_sleep_ms), 1)     AS avg_sleep_ms,
            ROUND(AVG(t_uplink_ms), 1)    AS avg_uplink_ms,
            ROUND(AVG(uplink_bytes), 1)   AS avg_bytes,
            MAX(received_at)              AS last_recv
        FROM power_stats
        GROUP BY device
        ORDER BY last_recv DESC

    """).fetchall()

    # Recent raw rows (last 200)
    recent = db.execute("""
        SELECT device, received_at,idle_budget_ms, t_sleep_ms, t_uplink_ms, uplink_bytes
        FROM power_stats
        ORDER BY received_at DESC
        LIMIT 200
    """).fetchall()

    out = [HTML_HEAD, "<h2>Power – Summary by Device</h2>"]
    out.append(
        "<table class='table'><tr>"
        "<th>Device</th><th>Samples</th><th>Avg Idle (ms)</th>"
        "<th>Avg Sleep (ms)</th><th>Avg Uplink (ms)</th><th>Avg Bytes</th><th>Last Received</th>"
        "</tr>"
    )

    for dev, n, avg_idle,avg_sl, avg_ul, avg_b, last_ms in summary:
        last_h = datetime.datetime.fromtimestamp(last_ms/1000.0).strftime("%Y-%m-%d %H:%M:%S") if last_ms else "-"
        out.append(f"<tr><td><a href='/admin/power/{dev}'>{dev}</a></td>"
               f"<td>{n}</td><td>{avg_idle}</td><td>{avg_sl}</td><td>{avg_ul}</td><td>{avg_b}</td><td>{last_h}</td></tr>")
    out.append("</table>")

    out.append("<h2>Recent Entries</h2>")
    out.append("<table class='table'><tr><th>Time</th><th>Device</th><th>Idle (ms)</th><th>Sleep (ms)</th><th>Uplink (ms)</th><th>Bytes</th></tr>")
    for dev, rcv, idle, sl, ul, b in recent:
        rcv_h = datetime.datetime.fromtimestamp(rcv/1000.0).strftime("%Y-%m-%d %H:%M:%S")
        out.append(f"<tr><td>{rcv_h}</td><td>{dev}</td><td>{idle}</td><td>{sl}</td><td>{ul}</td><td>{b}</td></tr>")
    out.append("</table>" + HTML_TAIL)
    return Response("".join(out), mimetype="text/html")

@app.get("/admin/power/<device>")
def admin_power_device(device: str):
    db = get_db()
    rows = db.execute("""
        SELECT received_at,idle_budget_ms, t_sleep_ms, t_uplink_ms, uplink_bytes
        FROM power_stats
        WHERE device=?
        ORDER BY received_at DESC
        LIMIT 500
    """, (device,)).fetchall()

    out = [HTML_HEAD, f"<h2>Power – {device}</h2>"]
    out.append("<table class='table'><tr><th>Time</th><th>Idle (ms)</th><th>Sleep (ms)</th><th>Uplink (ms)</th><th>Bytes</th></tr>")
    # rows:
    for rcv, idle, sl, ul, b in rows:
        rcv_h = datetime.datetime.fromtimestamp(rcv/1000.0).strftime("%Y-%m-%d %H:%M:%S")
        out.append(f"<tr><td>{rcv_h}</td><td>{idle}</td><td>{sl}</td><td>{ul}</td><td>{b}</td></tr>")
    out.append("</table>" + HTML_TAIL)
    return Response("".join(out), mimetype="text/html")

@app.get("/api/power/summary")
def api_power_summary():
    db = get_db()
    rows = db.execute("""
        SELECT device,
            COUNT(*)               AS n,
            AVG(idle_budget_ms)    AS avg_idle_ms,
            AVG(t_sleep_ms)        AS avg_sleep_ms,
            AVG(t_manual_sleep_ms) AS avg_manual_sleep_ms,
            AVG(t_auto_sleep_ms)   AS avg_auto_sleep_ms,
            AVG(t_uplink_ms)       AS avg_uplink_ms,
            AVG(uplink_bytes)      AS avg_bytes,
            MAX(received_at)       AS last_recv
        FROM power_stats
        GROUP BY device
        ORDER BY last_recv DESC
    """).fetchall()

    return jsonify([
        {"device": r[0], "samples": r[1], "avg_idle_ms": r[2],
        "avg_sleep_ms": r[3], "avg_manual_sleep_ms": r[4], "avg_auto_sleep_ms": r[5],
        "avg_uplink_ms": r[6], "avg_bytes": r[7], "last_received_ms": r[8]}
        for r in rows
    ])

@app.get("/api/power/<device>")
def api_power_device(device: str):
    db = get_db()
    rows = db.execute("""
        SELECT received_at, idle_budget_ms, t_sleep_ms, t_manual_sleep_ms, t_auto_sleep_ms, t_uplink_ms, uplink_bytes
        FROM power_stats
        WHERE device=?
        ORDER BY received_at DESC
        LIMIT 1000
    """, (device,)).fetchall()

    return jsonify([
        {"received_at_ms": r[0], "idle_budget_ms": r[1],
        "t_sleep_ms": r[2], "t_manual_sleep_ms": r[3], "t_auto_sleep_ms": r[4],
        "t_uplink_ms": r[5], "uplink_bytes": r[6]}
        for r in rows
    ])


@app.post("/api/power/snapshot")
def api_power_snapshot():
    """Collect an N-minute snapshot for a device, save JSON/CSV/PNG in LOG_DIR and return paths.
    Params: device (required), minutes (optional, default=5), max_samples (optional)
    """
    req = request.get_json(force=True, silent=True) or {}
    dev = request.args.get("device") or req.get("device")
    if not dev: return jsonify({"ok": False, "error": "missing device"}), 400
    minutes = int(request.args.get("minutes") or req.get("minutes") or 5)
    max_samples = int(request.args.get("max_samples") or req.get("max_samples") or 10000)
    now_ms_val = int(time.time() * 1000)
    cutoff = now_ms_val - (minutes * 60 * 1000)
    db = get_db()
    rows = db.execute("""
        SELECT received_at, idle_budget_ms, t_sleep_ms, t_manual_sleep_ms, t_auto_sleep_ms, t_uplink_ms, uplink_bytes
        FROM power_stats
        WHERE device=? AND received_at>=?
        ORDER BY received_at DESC
        LIMIT ?
    """, (dev, cutoff, max_samples)).fetchall()
    data = [
        {"received_at_ms": r[0], "idle_budget_ms": r[1], "t_sleep_ms": r[2], 
         "t_manual_sleep_ms": r[3], "t_auto_sleep_ms": r[4], "t_uplink_ms": r[5], "uplink_bytes": r[6]}
        for r in rows
    ]
    pathlib.Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    base = os.path.join(LOG_DIR, f"power_snapshot_{dev}_{ts}")
    json_path = base + ".json"
    csv_path = base + ".csv"
    png_path = base + ".png"
    # write json
    open(json_path, "w").write(json.dumps(data, indent=2))
    # write csv
    try:
        with open(csv_path, "w", newline='') as cf:
            w = csv.writer(cf)
            w.writerow(["received_at_ms", "idle_budget_ms", "t_sleep_ms", "t_manual_sleep_ms", "t_auto_sleep_ms", "t_uplink_ms", "uplink_bytes"])
            for r in data:
                w.writerow([r['received_at_ms'], r['idle_budget_ms'], r['t_sleep_ms'], r['t_manual_sleep_ms'], r['t_auto_sleep_ms'], r['t_uplink_ms'], r['uplink_bytes']])
    except Exception as e:
        return jsonify({"ok": False, "error": "csv_write_failed", "detail": str(e)}), 500
    # create PNG plot
    try:
        if data:
            xs = [datetime.datetime.fromtimestamp(d['received_at_ms']/1000.0) for d in reversed(data)]
            sleep_y = [d['t_sleep_ms'] for d in reversed(data)]
            uplink_y = [d['t_uplink_ms'] for d in reversed(data)]
            idle_y = [d['idle_budget_ms'] for d in reversed(data)]
            fig, ax = plt.subplots(3, 1, figsize=(10,6), sharex=True)
            ax[0].plot(xs, sleep_y, '-o', label='t_sleep_ms')
            ax[0].legend(); ax[0].grid(True)
            ax[1].plot(xs, uplink_y, '-o', label='t_uplink_ms', color='C1')
            ax[1].legend(); ax[1].grid(True)
            ax[2].plot(xs, idle_y, '-o', label='idle_budget_ms', color='C2')
            ax[2].legend(); ax[2].grid(True)
            fig.autofmt_xdate()
            fig.suptitle(f"Power snapshot for {dev} (last {minutes} min)")
            fig.tight_layout(rect=[0,0,1,0.96])
            # compute energy estimate using env or defaults (mV, mA assumptions)
            try:
                V_mV = int(os.getenv('POWER_V_SUPPLY_MV') or os.getenv('ECOWATT_POWER_V_SUPPLY') or 5000)
                I_active = int(os.getenv('POWER_I_ACTIVE_MA') or os.getenv('ECOWATT_POWER_I_ACTIVE_MA') or 200)
                I_uplink = int(os.getenv('POWER_I_UPLINK_MA') or os.getenv('ECOWATT_POWER_I_UPLINK_MA') or 300)
                I_sleep = int(os.getenv('POWER_I_SLEEP_MA') or os.getenv('ECOWATT_POWER_I_SLEEP_MA') or 5)
                # compute totals (ms)
                total_sleep_ms = sum(sleep_y)
                total_uplink_ms = sum(uplink_y)
                total_idle_ms = sum(idle_y)
                # convert to seconds
                s_sleep = total_sleep_ms/1000.0
                s_uplink = total_uplink_ms/1000.0
                s_idle = total_idle_ms/1000.0
                # estimate energy (J) = V * I * t  (V in volts, I in A)
                V = V_mV / 1000.0
                E_sleep = V * (I_sleep/1000.0) * s_sleep
                E_uplink = V * (I_uplink/1000.0) * s_uplink
                E_idle = V * (I_active/1000.0) * s_idle
                E_total = E_sleep + E_uplink + E_idle
                est_text = f"Est energy: {E_total:.2f} J (sleep={E_sleep:.2f} J uplink={E_uplink:.2f} J idle={E_idle:.2f} J)"
            except Exception:
                est_text = "Est energy: n/a"
            fig.savefig(png_path)
            # write a small .meta.json alongside containing energy estimate
            try:
                open(base + ".meta.json", "w").write(json.dumps({"energy_estimate_j": E_total, "detail": est_text}, indent=2))
            except Exception:
                pass
            plt.close(fig)
    except Exception as e:
        return jsonify({"ok": False, "error": "plot_failed", "detail": str(e)}), 500

    return jsonify({"ok": True, "device": dev, "count": len(data), "json": json_path, "csv": csv_path, "png": png_path})


@app.get("/api/buffer/<device>")
def api_buffer_device(device: str):
    db = get_db()
    rows = db.execute("""
        SELECT received_at, dropped_samples, acq_failures, transport_failures
        FROM buffer_stats
        WHERE device=?
        ORDER BY received_at DESC
        LIMIT 1000
    """, (device,)).fetchall()
    return jsonify([
        {"received_at_ms": r[0], "dropped_samples": r[1], "acq_failures": r[2], "transport_failures": r[3]}
        for r in rows
    ])


@app.get("/admin/buffer")
def admin_buffer():
    db = get_db()
    summary = db.execute("""
        SELECT device, COUNT(*) AS n, MAX(received_at) AS last_recv, SUM(dropped_samples) AS drops
        FROM buffer_stats
        GROUP BY device
        ORDER BY last_recv DESC
    """).fetchall()
    out = [HTML_HEAD, "<h2>Buffer – Summary by Device</h2>"]
    out.append("<table class='table'><tr><th>Device</th><th>Samples</th><th>Total Drops</th><th>Last Received</th></tr>")
    for dev,n,drops,last in summary:
        last_h = datetime.datetime.fromtimestamp(last/1000.0).strftime('%Y-%m-%d %H:%M:%S') if last else '-'
        out.append(f"<tr><td>{dev}</td><td>{n}</td><td>{drops or 0}</td><td>{last_h}</td></tr>")
    out.append("</table>" + HTML_TAIL)
    return Response("".join(out), mimetype="text/html")

@app.get("/admin/events")
def admin_events():
    rows = get_db().execute("""
      SELECT strftime('%Y-%m-%d %H:%M:%S', received_at/1000,'unixepoch'),
             device, event
      FROM device_events
      ORDER BY received_at DESC
      LIMIT 300
    """).fetchall()
    out=[HTML_HEAD, "<h2>Recent Device Events</h2>",
         "<table class='table'><tr><th>Time</th><th>Device</th><th>Event</th></tr>"]
    for t, d, e in rows:
        out.append(f"<tr><td>{t}</td><td><a href='/admin/events/{d}'>{d}</a></td><td class='mono'>{e}</td></tr>")
    out.append("</table>"+HTML_TAIL)
    return Response("".join(out), mimetype="text/html")

@app.get("/admin/events/<device>")
def admin_events_device(device: str):
    rows = get_db().execute("""
      SELECT strftime('%Y-%m-%d %H:%M:%S', received_at/1000,'unixepoch'),
             event
      FROM device_events
      WHERE device=?
      ORDER BY received_at DESC
      LIMIT 1000
    """, (device,)).fetchall()
    out=[HTML_HEAD, f"<h2>Events – {device}</h2>",
         "<table class='table'><tr><th>Time</th><th>Event</th></tr>"]
    for t, e in rows:
        out.append(f"<tr><td>{t}</td><td class='mono'>{e}</td></tr>")
    out.append("</table>"+HTML_TAIL)
    return Response("".join(out), mimetype="text/html")

@app.route("/admin/controls", methods=["GET","POST"])
def admin_controls():
    pathlib.Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    msg = ""
    if request.method == "POST":
        kind = request.form.get("kind")
        if kind == "config":
            si  = int(request.form.get("sampling_interval") or 0)
            regs = [r.strip() for r in (request.form.get("registers") or "").split(",") if r.strip()]
            obj = {"config_update": {"sampling_interval": si, "registers": regs}}
            open(os.path.join(LOG_DIR, "config_update.json"), "w").write(json.dumps(obj))
            msg = "queued config_update"
        elif kind == "command":
            val = int(request.form.get("export_percent") or 0)
            obj = {"command":{"action":"write_register","target_register":"status_flag","value":val}}
            open(os.path.join(LOG_DIR, "command.json"), "w").write(json.dumps(obj))
            msg = "queued command"

        return redirect(url_for("admin_controls") + f"?ok={msg}")

    ok = request.args.get("ok","")
    html = f"""{HTML_HEAD}
    <h2>Controls</h2>
    <p class='small'>These are <b>one-shot</b>; the next device upload will pick them up.</p>
    {"<p><b>"+ok+"</b></p>" if ok else ""}

    <h3>Config update</h3>
        <form method="post" onsubmit="return submitRegisters();">
            <input type="hidden" name="kind" value="config">
            <label>Sampling interval (sec): <input name="sampling_interval" type="number" min="1" value="5"></label><br>
            <label>Registers:</label>
            <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px;">
                <!-- List of registers as checkboxes -->
                <label><input type="checkbox" name="reg" value="vac1" checked> vac1</label>
                <label><input type="checkbox" name="reg" value="iac1" checked> iac1</label>
                <label><input type="checkbox" name="reg" value="fac1" checked> fac1</label>
                <label><input type="checkbox" name="reg" value="vpv1" checked> vpv1</label>
                <label><input type="checkbox" name="reg" value="vpv2" checked> vpv2</label>
                <label><input type="checkbox" name="reg" value="ipv1" checked> ipv1</label>
                <label><input type="checkbox" name="reg" value="ipv2" checked> ipv2</label>
                <label><input type="checkbox" name="reg" value="temp" checked> temp</label>
                <label><input type="checkbox" name="reg" value="pac" checked> pac</label>
            </div>
            <input type="hidden" name="registers" id="registers_hidden">
            <button type="submit">Queue config_update</button>
        </form>
        <script>
        function submitRegisters() {{
            var regs = Array.from(document.querySelectorAll('input[name="reg"]:checked')).map(function(cb){{return cb.value;}});
            document.getElementById('registers_hidden').value = regs.join(',');
            return true;
        }}
        </script>

    <h3>Command (export power %)</h3>
    <form method="post">
      <input type="hidden" name="kind" value="command">
      <label>Export %: <input name="export_percent" type="number" min="0" max="100" value="25"></label><br>
      <button type="submit">Queue command</button>
    </form>
    {HTML_TAIL}"""
    return Response(html, mimetype="text/html")

if __name__ == "__main__":
    from waitress import serve
    port = int(os.getenv("PORT", "5000"))
    pathlib.Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    print(f"[API] starting on 0.0.0.0:{port}, DB={DB_PATH}, auth={'on' if REQUIRE_AUTH else 'off'}, envelope={'b64' if USE_B64 else 'plain'}")
    serve(app, host="0.0.0.0", port=port)
