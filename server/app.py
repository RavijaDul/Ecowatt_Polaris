# app.py
import os, time, base64, json, sqlite3, pathlib, datetime, glob, hmac, io, csv
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from typing import List, Tuple, Optional
from flask import Flask, request, jsonify, g, Response, redirect, url_for
import requests


# ---- Config via env ----
AUTH_KEYS_B64 = [k.strip() for k in os.getenv("AUTH_KEYS_B64", "").split(",") if k.strip()]
REQUIRE_AUTH  = bool(AUTH_KEYS_B64)
DB_PATH       = os.getenv("SQLITE_PATH", "ecowatt.db")
LOG_DIR       = os.getenv("LOG_DIR", "logs")
PSK           = os.getenv("PSK", "ecowatt-demo-psk")
USE_B64       = bool(int(os.getenv("USE_B64", "1")))  # 1=use base64 envelope

# Track what we last served, so we can estimate "written"
LAST_FOTA = {}  # device_id -> {"version":str, "size":int, "chunk_size":int, "next":int, "written":int}

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
  kind TEXT,          -- 'manifest','chunk','verify_ok','verify_fail','boot_ok','boot_rollback'
  detail TEXT
);

CREATE TABLE IF NOT EXISTS fota_progress(
  device TEXT PRIMARY KEY,
  version TEXT,
  size INTEGER,
  written INTEGER,
  percent INTEGER,
  updated TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS power_stats (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  device        TEXT    NOT NULL,
  received_at   INTEGER NOT NULL,
  t_sleep_ms    INTEGER NOT NULL,
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

def upsert_progress(device, version, size, written):
    pct = int((written*100)//size) if size else 0
    db = get_db()
    db.execute("""
      INSERT INTO fota_progress(device,version,size,written,percent)
      VALUES(?,?,?,?,?)
      ON CONFLICT(device) DO UPDATE SET
        version=excluded.version, size=excluded.size,
        written=excluded.written, percent=excluded.percent, updated=CURRENT_TIMESTAMP
    """, (device, version, size, written, pct))
    db.commit()

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
<title>EcoWatt Upload</title>
<style>
body{font-family:system-ui,Segoe UI,Arial,sans-serif;padding:20px;line-height:1.35}
code,pre{font-family:ui-monospace,Consolas,monospace}
.table{border-collapse:collapse;margin-top:12px}
.table th,.table td{border:1px solid #ccc;padding:6px 8px}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px;white-space:pre}
.small{color:#666}
</style></head><body>"""
HTML_TAIL = "</body></html>"

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
    html = """
    <html>
    <head>
        <title>EcoWatt Admin Dashboard</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; }
            h1 { color: #0078D4; }
            ul { line-height: 1.8; }
            a { color: #0078D4; text-decoration: none; }
            a:hover { text-decoration: underline; }
            .note { color: gray; font-size: 0.9em; }
        </style>
    </head>
    <body>
        <h1>EcoWatt Admin</h1>
        <ul>
            <li><a href="/admin">Uploads</a> — browse recent uploads and drill into details</li>
            <li><a href="/admin/fota">FOTA</a> — device progress & event history</li>
            <li><a href="/admin/power">Power</a> — sleep/uplink timing stats per device</li>
            <li><a href="/admin/controls">Controls</a> — configurations and command execution</li>
            <li><a href="/admin/sim-fault">SIM Fault</a> — hits the Inverter SIM API</li>

        </ul>
        
    </body>
    </html>
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
            upsert_progress(dev, version, size, written)   # <-- writes fota_progress table
            log_fota(dev, "progress", f"next={next_chunk} written={written}")
        # verify/apply outcomes
        if "verify" in fota_in:
            log_fota(dev, "verify_ok" if fota_in["verify"] == "ok" else "verify_fail", "")
        if "apply" in fota_in:
            log_fota(dev, "apply_ok" if fota_in["apply"] == "ok" else "apply_fail", "")
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
                        upsert_progress(dev, mf_ver, total, total)
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
            t_sleep  = int(ps.get("t_sleep_ms") or 0)          # will be 0 with our approach
            t_uplink = int(ps.get("t_uplink_ms") or 0)
            ubytes   = int(ps.get("uplink_bytes") or 0)
            idle_b   = int(ps.get("idle_budget_ms") or 0)
            db = get_db()
            db.execute(
                "INSERT INTO power_stats(device, received_at, t_sleep_ms, t_uplink_ms, uplink_bytes, idle_budget_ms) VALUES(?,?,?,?,?,?)",
                (dev, now_ms, t_sleep, t_uplink, ubytes, idle_b)
            )
            db.commit()
            print(f"[PWR] dev={dev} idle={idle_b}ms uplink={t_uplink}ms bytes={ubytes}", flush=True)
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

    # fota manifest (sticky until chunks done)
    man_path = os.path.join(LOG_DIR, "fota_manifest.json")
    if os.path.exists(man_path):
        try:
            mf = json.loads(open(man_path, "r").read())
            reply.setdefault("fota", {})["manifest"] = mf
            print(f"[QUEUE] FOTA manifest available -> version={mf.get('version')} size={mf.get('size')} chunk={mf.get('chunk_size')}", flush=True)
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

    # Remember manifest we just served to this device
    if "fota" in reply and "manifest" in reply["fota"]:
        mf = reply["fota"]["manifest"]
        LAST_FOTA[dev] = {
            "version": mf.get("version"),
            "size": int(mf.get("size") or 0),
            "chunk_size": int(mf.get("chunk_size") or 0),
            "next": int(want_next or 0),
            "written": int((want_next or 0) * int(mf.get("chunk_size") or 0))
        }
        log_fota(dev, "manifest",
                f"v={mf.get('version')} size={mf.get('size')} cs={mf.get('chunk_size')}")

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
      SELECT device, version, size, written, percent, updated
      FROM fota_progress
      ORDER BY updated DESC
    """).fetchall()
    events = db.execute("""
      SELECT ts, device, kind, detail
      FROM fota_events
      ORDER BY ts DESC, id DESC
      LIMIT 200
    """).fetchall()

    out = [HTML_HEAD, "<h2>FOTA – Progress (all devices)</h2>"]
    out.append("<table class='table'><tr><th>Device</th><th>Version</th><th>Written</th><th>Size</th><th>Percent</th><th>Updated</th></tr>")
    for d,v,sz,wr,pct,upd in prog:
        out.append(
            f"<tr>"
            f"<td><a href='/admin/fota/{d}'>{d}</a></td>"
            f"<td>{v}</td>"
            f"<td>{wr}</td>"
            f"<td>{sz}</td>"
            f"<td>{pct}%</td>"
            f"<td>{upd}</td>"
            f"</tr>"
        )
    out.append("</table>")

    out.append("<h2>Recent FOTA Events</h2>")
    out.append("<table class='table'><tr><th>Time</th><th>Device</th><th>Kind</th><th>Detail</th></tr>")
    for ts, dev, kind, detail in events:
        out.append(f"<tr><td>{ts}</td><td>{dev}</td><td>{kind}</td><td class='mono'>{detail}</td></tr>")
    out.append("</table>" + HTML_TAIL)
    return Response("".join(out), mimetype="text/html")

@app.get("/admin/fota/<device>")
def admin_fota_device(device: str):
    db = get_db()
    prog = db.execute("""
      SELECT device, version, size, written, percent, updated
      FROM fota_progress WHERE device=?
    """, (device,)).fetchone()
    events = db.execute("""
      SELECT ts, device, kind, detail
      FROM fota_events
      WHERE device=?
      ORDER BY ts DESC, id DESC
      LIMIT 500
    """, (device,)).fetchall()

    out = [HTML_HEAD, f"<h2>FOTA – {device}</h2>"]
    out.append("<h3>Current progress</h3>")
    if prog:
        d,v,sz,wr,pct,upd = prog
        out.append("<table class='table'><tr><th>Version</th><th>Written</th><th>Size</th><th>Percent</th><th>Updated</th></tr>")
        out.append(f"<tr><td>{v}</td><td>{wr}</td><td>{sz}</td><td>{pct}%</td><td>{upd}</td></tr></table>")
    else:
        out.append("<p>No progress recorded.</p>")

    out.append("<h3>Event timeline</h3>")
    out.append("<table class='table'><tr><th>Time</th><th>Kind</th><th>Detail</th></tr>")
    for ts, _dev, kind, detail in events:
        out.append(f"<tr><td>{ts}</td><td>{kind}</td><td class='mono'>{detail}</td></tr>")
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
        SELECT received_at,idle_budget_ms,  t_sleep_ms, t_uplink_ms, uplink_bytes
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
            AVG(t_uplink_ms)       AS avg_uplink_ms,
            AVG(uplink_bytes)      AS avg_bytes,
            MAX(received_at)       AS last_recv
        FROM power_stats
        GROUP BY device
        ORDER BY last_recv DESC
    """).fetchall()

    return jsonify([
        {"device": r[0], "samples": r[1], "avg_idle_ms": r[2],
        "avg_sleep_ms": r[3], "avg_uplink_ms": r[4],
        "avg_bytes": r[5], "last_received_ms": r[6]}
        for r in rows
    ])

@app.get("/api/power/<device>")
def api_power_device(device: str):
    db = get_db()
    rows = db.execute("""
        SELECT received_at, idle_budget_ms, t_sleep_ms, t_uplink_ms, uplink_bytes
        FROM power_stats
        WHERE device=?
        ORDER BY received_at DESC
        LIMIT 1000
    """, (device,)).fetchall()

    return jsonify([
        {"received_at_ms": r[0], "idle_budget_ms": r[1],
        "t_sleep_ms": r[2], "t_uplink_ms": r[3], "uplink_bytes": r[4]}
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
        SELECT received_at, idle_budget_ms, t_sleep_ms, t_uplink_ms, uplink_bytes
        FROM power_stats
        WHERE device=? AND received_at>=?
        ORDER BY received_at DESC
        LIMIT ?
    """, (dev, cutoff, max_samples)).fetchall()
    data = [
        {"received_at_ms": r[0], "idle_budget_ms": r[1], "t_sleep_ms": r[2], "t_uplink_ms": r[3], "uplink_bytes": r[4]}
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
            w.writerow(["received_at_ms", "idle_budget_ms", "t_sleep_ms", "t_uplink_ms", "uplink_bytes"])
            for r in data:
                w.writerow([r['received_at_ms'], r['idle_budget_ms'], r['t_sleep_ms'], r['t_uplink_ms'], r['uplink_bytes']])
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
            fig.savefig(png_path)
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
    <form method="post">
      <input type="hidden" name="kind" value="config">
      <label>Sampling interval (sec): <input name="sampling_interval" type="number" min="1" value="5"></label><br>
      <label>Registers (comma): <input name="registers" type="text" placeholder="vac1,iac1,fac1"></label><br>
      <button type="submit">Queue config_update</button>
    </form>

    <h3>Command (export power %)</h3>
    <form method="post">
      <input type="hidden" name="kind" value="command">
      <label>Export %: <input name="export_percent" type="number" min="0" max="100" value="25"></label><br>
      <button type="submit">Queue command</button>
    </form>
    {HTML_TAIL}"""
    return Response(html, mimetype="text/html")

@app.route("/admin/sim-fault", methods=["GET","POST"])
def admin_sim_fault():
    base = os.getenv("SIM_BASE", "http://20.15.114.131:8080").rstrip("/")
    key  = os.getenv("SIM_KEY_B64", "")  # must match what the device uses
    note = ""
    detail = ""

    def _post_json(path, payload):
        url = f"{base}{path}"
        try:
            r = requests.post(
                url,
                headers={"Authorization": key, "Content-Type": "application/json"},
                json=payload,
                timeout=6,
            )
            return r.status_code, (r.text or "").strip()
        except Exception as e:
            return None, f"Exception: {e}"

    def _get(path):
        url = f"{base}{path}"
        try:
            r = requests.get(url, headers={"Authorization": key}, timeout=4)
            return r.status_code, (r.text or "").strip()
        except Exception as e:
            return None, f"Exception: {e}"

    if request.method == "POST":
        action = request.form.get("action", "trigger")
        if action == "ping":
            code, body = _get("/api/health")
            note = f"Ping → {code}"
            detail = body
        else:
            payload = {
                "errorType": request.form.get("type", "EXCEPTION").strip(),
                "exceptionCode": int(request.form.get("code") or 0),
                "delayMs": int(request.form.get("delay") or 0),
            }
            code, body = _post_json("/api/user/error-flag/add", payload)
            note = f"Sent → {code}"
            detail = body

    warn = []
    if not key:
        warn.append("SIM_KEY_B64 is empty (calls will likely be unauthorized).")
    if not base.startswith("http"):
        warn.append("SIM_BASE looks invalid; set a full http(s) URL.")

    html = f"""{HTML_HEAD}
    <h2>SIM Fault Injection</h2>
    <p class='small'>Sets a one-shot fault on the supervisor's SIM API; the next device SIM read should see it.</p>
    {"".join(f"<p style='color:#b00'><b>⚠ {w}</b></p>" for w in warn)}
    {"<p><b>"+note+"</b></p><pre class='mono'>"+detail+"</pre>" if note else ""}

    <form method="post" style="margin-bottom:18px">
      <input type="hidden" name="action" value="trigger">
      <label>Type:
        <select name="type">
          <option>EXCEPTION</option>
          <option>CRC_ERROR</option>
          <option>CORRUPT</option>
          <option>PACKET_DROP</option>
          <option>DELAY</option>
        </select>
      </label>
      <label>Exception code: <input name="code" type="number" min="0" value="2"></label>
      <label>Delay (ms): <input name="delay" type="number" min="0" value="8000"></label>
      <button type="submit">Trigger</button>
    </form>

    <form method="post">
      <input type="hidden" name="action" value="ping">
      <button type="submit">Ping SIM /api/health</button>
    </form>

    <p class='small'>Using SIM_BASE=<code>{base}</code></p>
    {HTML_TAIL}"""
    return Response(html, mimetype="text/html")

if __name__ == "__main__":
    from waitress import serve
    port = int(os.getenv("PORT", "5000"))
    pathlib.Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    print(f"[API] starting on 0.0.0.0:{port}, DB={DB_PATH}, auth={'on' if REQUIRE_AUTH else 'off'}, envelope={'b64' if USE_B64 else 'plain'}")
    serve(app, host="0.0.0.0", port=port)
