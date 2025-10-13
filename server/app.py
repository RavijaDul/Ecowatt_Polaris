# server.py
import os, time, base64, json, sqlite3, pathlib, datetime, glob, hmac
from typing import List, Tuple, Optional
from flask import Flask, request, jsonify, g, Response


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
                if os.path.exists(man_path):
                    os.remove(man_path)
                for p in glob.glob(os.path.join(LOG_DIR, "fota_chunk_*.b64")):
                    os.remove(p)
                if dev in LAST_FOTA:
                    del LAST_FOTA[dev]
                print(f"[FOTA] Cleanup done after boot_ok for {dev}", flush=True)
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

if __name__ == "__main__":
    from waitress import serve
    port = int(os.getenv("PORT", "5000"))
    pathlib.Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    print(f"[API] starting on 0.0.0.0:{port}, DB={DB_PATH}, auth={'on' if REQUIRE_AUTH else 'off'}, envelope={'b64' if USE_B64 else 'plain'}")
    serve(app, host="0.0.0.0", port=port)
