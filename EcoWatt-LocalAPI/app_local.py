# app_local.py — Minimal EcoWatt Cloud ingest service (Flask + SQLite)

import os, time, base64, json, sqlite3, pathlib, datetime    # L1: stdlib imports for env, time, DB, files, dates.
from typing import List, Tuple, Optional                     # L2: typing hints.
from flask import Flask, request, jsonify, g, Response       # L3: Flask framework primitives.

# ---- Config via env ----
AUTH_KEYS_B64 = [k.strip() for k in os.getenv("AUTH_KEYS_B64", "").split(",") if k.strip()]  # L6: optional auth keys (B64).
REQUIRE_AUTH  = bool(AUTH_KEYS_B64)               # L7: enable auth if keys provided.
DB_PATH       = os.getenv("SQLITE_PATH", "ecowatt.db")       # L8: SQLite file path.
LOG_DIR       = os.getenv("LOG_DIR", "logs")                  # L9: directory for any logs.

# SQLite schema: upload batches table
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
"""                                                     # L11–L29: two DDL statements (table + covering index).

app = Flask(__name__)                                  # L31: create WSGI app.

# ------------ auth ------------
def _auth_ok(header: str) -> bool:                     # L34: quick header check helper.
    """Accept Basic <B64> or raw B64 token; compare against AUTH_KEYS_B64."""  # L35
    if not REQUIRE_AUTH: return True                   # L36: auth disabled → always allow.
    if not header: return False                        # L37: no header → deny.
    token = header.strip()                             # L38: normalize.
    if token.lower().startswith("basic "):             # L39: allow "Basic <token>" form.
        token = token[6:].strip()                      # L40: strip scheme name.
    return token in AUTH_KEYS_B64                      # L41: check against allowed list.

# ------------ DB open + migration ------------
def _migrate(db: sqlite3.Connection) -> None:          # L44: add columns if older DB exists.
    """Add new columns if the DB already existed from an older schema."""      # L45
    cols = {row[1] for row in db.execute("PRAGMA table_info(uploads)").fetchall()}  # L46: existing columns set.
    changed = False                                     # L47: flag to commit once.
    if "ts_list_json" not in cols:                      # L48
        db.execute("ALTER TABLE uploads ADD COLUMN ts_list_json TEXT"); changed = True  # L49
    if "orig_samples" not in cols:                      # L50
        db.execute("ALTER TABLE uploads ADD COLUMN orig_samples INTEGER"); changed = True  # L51
    if "orig_bytes" not in cols:                        # L52
        db.execute("ALTER TABLE uploads ADD COLUMN orig_bytes INTEGER"); changed = True    # L53
    if changed: db.commit()                             # L54: commit if schema changed.

def get_db():
    """Open a per-request connection; create tables; set pragmas; run migrations."""  # L57
    if "db" not in g:                                  # L58: first access in this request?
        g.db = sqlite3.connect(DB_PATH, check_same_thread=False)  # L59: connect (allow threads).
        g.db.execute("PRAGMA journal_mode=WAL;")       # L60: better concurrency.
        g.db.execute("PRAGMA synchronous=NORMAL;")     # L61: perf tradeoff.
        g.db.executescript(DDL)                        # L62: ensure base schema exists.
        _migrate(g.db)                                 # L63: bring existing DB up to date.
    return g.db                                        # L64: return connection.

@app.teardown_appcontext
def close_db(_=None):                                  # L68: Flask teardown hook.
    db = g.pop("db", None)                             # L69: drop connection from context if present.
    if db is not None: db.close()                      # L70: close it.

# ---- human formatting helpers ----
GAIN = {                                               # L73: register gains to scale raw units.
    "vac1": 10.0, "iac1": 10.0, "fac1": 100.0,
    "vpv1": 10.0, "vpv2": 10.0, "ipv1": 10.0, "ipv2": 10.0,
    "temp": 10.0, "export_percent": 1.0, "pac": 1.0
}

def decode_delta_rle_v1(block: bytes, order: List[str]) -> Tuple[List[List[int]], str]:  # L79
    """Minimal mirror of device-side decoder for display/debug (no CRC verify)."""       # L80
    def u16(b, o): return b[o] | (b[o+1]<<8)            # L81: read little-endian uint16.
    def s16(b, o):                                      # L82: read little-endian int16.
        v = u16(b,o); return v-0x10000 if v & 0x8000 else v

    if len(block) < 12: return [], "short"              # L85: quick sanity.
    pos=0                                               # L86: cursor.
    ver = block[pos]; pos+=1                            # L87: version.
    nf  = block[pos]; pos+=1                            # L88: n_fields.
    n   = u16(block,pos); pos+=2                        # L89: n_samples.
    pos+=4                                              # L90: reserved skip.
    if ver != 1 or nf != len(order) or n == 0:          # L91: header consistency.
        return [], "header mismatch or empty"           # L92

    if len(block) < pos + nf*2 + 4: return [], "truncated"  # L94: ensure room for inits and CRC.
    last = [u16(block, pos + 2*i) for i in range(nf)]   # L95: read initial absolute values.
    pos += nf*2                                         # L96

    fields = [[0]*n for _ in range(nf)]                 # L98: output matrix.
    for f in range(nf):                                 # L99: per-field stream.
        fields[f][0] = last[f]                          # L100: first sample is absolute.
        produced = 0                                    # L101: number after the first sample.
        while produced < n-1:                           # L102: decode until n samples done.
            if pos >= len(block)-4: return [], "early EOF"   # L103: keep 4 bytes for CRC tail.
            op = block[pos]; pos += 1                  # L104: read opcode.
            if op == 0x00:                             # L105: repeat run.
                if pos >= len(block)-4: return [], "EOF len" # L106
                rep = block[pos]; pos += 1             # L107: repeat length.
                for _ in range(rep):                   # L108: emit repeated values.
                    fields[f][1+produced] = last[f];   # L109
                    produced += 1                      # L110
            elif op == 0x01:                           # L111: delta run.
                if pos+2 > len(block)-4: return [], "EOF delta" # L112
                d = s16(block, pos); pos += 2          # L113: read signed delta.
                cur = (last[f] + d) & 0xFFFF           # L114: update current value.
                fields[f][1+produced] = cur            # L115: store it.
                last[f] = cur                          # L116: update last.
                produced += 1                          # L117
            else:                                      # L118
                return [], "bad op"                    # L119: unknown opcode.
    rows = [[fields[f][i] for f in range(nf)] for i in range(n)]  # L121: transpose back to row-major.
    return rows, "ok"                                   # L122

def _fmt_row(order: List[str], raw: List[int]) -> str:  # L125: human-friendly one-line formatter.
    parts=[]                                            # L126
    for name, val in zip(order, raw):                   # L127
        g = GAIN.get(name, 1.0)                         # L128
        if name == "fac1": parts.append(f"{name}={val/g:.2f}Hz")     # L129
        elif name in ("vac1","vpv1","vpv2"): parts.append(f"{name}={val/g:.1f}V")  # L130
        elif name in ("iac1","ipv1","ipv2"):  parts.append(f"{name}={val/g:.1f}A") # L131
        elif name == "temp": parts.append(f"{name}={val/g:.1f}C")    # L132
        elif name == "export_percent": parts.append(f"{name}={int(val)}%")  # L133
        elif name == "pac": parts.append(f"{name}={int(val)}W")      # L134
        else: parts.append(f"{name}={val}")                          # L135
    return " ".join(parts)                                           # L136

def _looks_epoch_ms(v: int) -> bool:                 # L139: heuristic to detect epoch-ms timestamps.
    return v >= 1_000_000_000_000                    # L140: > ~2001-09-09.

def _device_ms_list(n: int, ts0: int, ts1: int) -> List[int]:   # L143: reconstruct device-ms list when absent.
    if n <= 1 or ts0 == ts1: return [ts0]*max(n,1)   # L144: single or degenerate → same timestamp(s).
    return [int(round(ts0 + i * (ts1 - ts0) / (n - 1))) for i in range(n)]  # L145: linear interpolation.

def _epoch_ms_list(n: int, ts0: int, ts1: int, recv: int, ts_list_opt: Optional[List[int]]) -> List[int]: # L148
    if ts_list_opt and len(ts_list_opt) >= n:         # L149: prefer provided ts_list if length ok.
        xs = [int(v) for v in ts_list_opt[:n]]        # L150: coerce to ints.
        if all(_looks_epoch_ms(v) for v in xs):       # L151: already epoch-ms?
            return xs                                 # L152: return directly.
        out=[]                                        # L153: else map device-ms to epoch estimates.
        for x in xs:                                  # L154
            if ts1 == ts0: out.append(recv)           # L155
            else:                                     
                frac = (x - ts0) / (ts1 - ts0)        # L157: relative position in window.
                out.append(int(recv - (1.0 - frac) * (ts1 - ts0)))  # L158: map into epoch using receive time.
        return out                                     # L159
    devs = _device_ms_list(n, ts0, ts1)               # L161: fallback: construct device-ms vector.
    out=[]                                            # L162
    for d in devs:                                    # L163
        if ts1 == ts0: out.append(recv)               # L164
        else:
            frac = (d - ts0) / (ts1 - ts0)            # L166
            out.append(int(recv - (1.0 - frac) * (ts1 - ts0)))  # L167
    return out                                        # L168

HTML_HEAD = """<!doctype html><html><head><meta charset="utf-8">
<title>EcoWatt Upload</title>
<style>
body{font-family:system-ui,Segoe UI,Arial,sans-serif;padding:20px;line-height:1.35}
code,pre{font-family:ui-monospace,Consolas,monospace}
.table{border-collapse:collapse;margin-top:12px}
.table th,.table td{border:1px solid #ccc;padding:6px 8px}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px;white-space:pre}
.small{color:#666}
</style></head><body>"""                                  # L171–L180: basic HTML styling for /admin.
HTML_TAIL = "</body></html>"                             # L181: HTML tail.

@app.get("/api/health")
def health():                                            # L184: simple health probe.
    return jsonify({"ok": True})                         # L185: always 200 OK.

@app.post("/api/device/upload")
def device_upload():                                     # L188: ingestion endpoint for device payloads.
    if not _auth_ok(request.headers.get("Authorization")):   # L189: optional auth check.
        return jsonify({"ok": False, "error": "unauthorized"}), 401  # L190: reject.

    body = request.get_json(force=True, silent=True)     # L192: parse JSON (force to read body as JSON).
    if not body:                                         # L193
        return jsonify({"ok": False, "error": "invalid-json"}), 400  # L194

    for f in ("device_id","ts_start","ts_end","codec","order","block_b64"):  # L196: required fields.
        if f not in body: return jsonify({"ok": False, "error": "missing-fields"}), 400  # L197

    try:
        blob = base64.b64decode(body["block_b64"], validate=True)  # L200: decode compressed block.
    except Exception:
        return jsonify({"ok": False, "error": "bad-base64"}), 400  # L202: invalid base64.

    dev   = str(body["device_id"])                    # L204: device id.
    ts0   = int(body["ts_start"])                     # L205: device-ms window start.
    ts1   = int(body["ts_end"])                       # L206: device-ms window end.
    seq   = int(body.get("seq", 0))                   # L207: optional sequence.
    codec = str(body["codec"])                        # L208: codec label.
    order = list(body["order"])                       # L209: field order array.
    ts_list = body.get("ts_list")                     # L210: optional per-sample timestamps.
    now_ms = int(time.time() * 1000)                  # L211: server receive time (epoch-ms).

    db = get_db()                                     # L213: open connection.
    cur = db.cursor()                                 # L214: cursor.
    cur.execute("""INSERT INTO uploads
                   (device_id, ts_start, ts_end, seq, codec, order_json, ts_list_json,
                    orig_samples, orig_bytes, received_at, block)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (dev, ts0, ts1, seq, codec, json.dumps(order),
                 json.dumps(ts_list) if ts_list is not None else None,
                 body.get("orig_samples"), body.get("orig_bytes"),
                 now_ms, blob))                       # L215–L222: parameterized insert.
    db.commit()                                       # L223: persist row.
    rowid = cur.lastrowid                             # L224: get autoincrement ID.

    if codec == "delta_rle_v1":                       # L226: try to decode for console print.
        rows, _ = decode_delta_rle_v1(blob, order)    # L227: decode compressed payload (no CRC here).
        n = len(rows)                                 # L228
        dev_ms_list = _device_ms_list(n, ts0, ts1)    # L229: device-ms per sample (approximate if missing).
        epoch_ms_list = _epoch_ms_list(n, ts0, ts1, now_ms, ts_list if isinstance(ts_list, list) else None)  # L230
        for i in range(n):                            # L231: print each row on its own line (PowerShell-friendly).
            t_local = datetime.datetime.fromtimestamp(epoch_ms_list[i]/1000.0).strftime("%Y-%m-%d %H:%M:%S") # L232
            print(f"{t_local} dev={dev} dev_ms={dev_ms_list[i]} {_fmt_row(order, rows[i])}", flush=True)     # L233
    else:
        print(f"[API] stored upload id={rowid} dev={dev} bytes={len(blob)} (codec={codec})", flush=True)     # L235

    return jsonify({"ok": True, "id": rowid}), 200     # L237: success response.

@app.get("/admin")
def admin_home():                                      # L240: recent uploads dashboard.
    cur = get_db().cursor()                            # L241
    cur.execute("SELECT id, device_id, ts_start, ts_end, codec, received_at FROM uploads ORDER BY id DESC LIMIT 50") # L242
    rows = cur.fetchall()                              # L243
    out = [HTML_HEAD, "<h2>Recent uploads</h2><table class='table'><tr><th>ID</th><th>Device</th><th>Dev ms</th><th>Codec</th><th>Received (server)</th></tr>"]  # L244
    for (id_, dev, ts0, ts1, codec, recv) in rows:     # L245
        recvt = datetime.datetime.fromtimestamp(recv/1000.0).strftime("%Y-%m-%d %H:%M:%S") # L246
        out.append(f"<tr><td><a href='/admin/upload/{id_}'>{id_}</a></td>"
                   f"<td>{dev}</td><td>{ts0} → {ts1}</td><td>{codec}</td><td>{recvt}</td></tr>")  # L247–L248
    out.append("</table>" + HTML_TAIL)                 # L249
    return Response("".join(out), mimetype="text/html")# L250

@app.get("/admin/upload/<int:rowid>")
def admin_view(rowid: int):                            # L253: details for one upload row.
    cur = get_db().cursor()                            # L254
    cur.execute("""SELECT device_id, ts_start, ts_end, seq, codec, order_json, ts_list_json,
                          orig_samples, orig_bytes, received_at, block
                   FROM uploads WHERE id=?""", (rowid,))  # L255–L257
    row = cur.fetchone()                               # L258
    if not row:
        return Response(HTML_HEAD + "<h3>Not found</h3>" + HTML_TAIL, mimetype="text/html")  # L260
    dev, ts0, ts1, seq, codec, order_json, ts_list_json, orig_samples, orig_bytes, recv, blob = row  # L261
    order = json.loads(order_json)                     # L262
    ts_list = json.loads(ts_list_json) if ts_list_json else None  # L263
    recv_h = datetime.datetime.fromtimestamp(recv/1000.0).strftime("%Y-%m-%d %H:%M:%S")  # L264

    rows = []; note = ""                               # L266
    if codec == "delta_rle_v1":
        rows, note = decode_delta_rle_v1(blob, order)  # L268

    out = [HTML_HEAD, "<h2>Upload detail</h2><pre class='mono'>"]  # L270
    out.append(f"Upload ID      : {rowid}\n")           # L271
    out.append(f"Device ID      : {dev}\n")             # L272
    out.append(f"Device ms range: {ts0} -> {ts1}  (ms since boot)\n")  # L273
    out.append(f"Server received: {recv_h} (local time)\n")            # L274
    out.append(f"Codec          : {codec}\n")           # L275
    out.append(f"Compressed size: {len(blob)} bytes\n") # L276
    out.append(f"Order          : {', '.join(order)}\n")# L277
    if ts_list:
        out.append(f"ts_list        : {len(ts_list)} items (exact per-sample timestamps)\n") # L279
    out.append(f"\n[Decoded {len(rows)} samples — ok{' | using ts_list' if ts_list else ''}]\n")  # L280
    out.append("</pre>")                                # L281

    if rows:
        out.append("<table class='table'><tr><th>#</th><th>dev_ms</th><th>time (local)</th><th>raw</th><th>scaled</th></tr>")  # L284
        n = len(rows)                                   # L285
        dev_ms_list = _device_ms_list(n, ts0, ts1)      # L286
        epoch_ms_list = _epoch_ms_list(n, ts0, ts1, recv, ts_list)  # L287
        for i, raw in enumerate(rows):                  # L288
            t_local = datetime.datetime.fromtimestamp(epoch_ms_list[i]/1000.0).strftime("%Y-%m-%d %H:%M:%S")  # L289
            out.append(f"<tr><td>{i}</td><td>{dev_ms_list[i]}</td><td>{t_local}</td>"
                       f"<td class='mono'>{' '.join(str(x) for x in raw)}</td>"
                       f"<td>{_fmt_row(order, raw)}</td></tr>")      # L290–L292
        out.append("</table>")                          # L293

    hexrows = []                                        # L295: hex dump view of the BLOB.
    for i in range(0, len(blob), 16):                   # L296
        chunk = blob[i:i+16]                            # L297
        hexrows.append(f"{i:04X}: " + " ".join(f"{x:02X}" for x in chunk))  # L298
    out.append("<h3>Compressed block (hex dump)</h3><pre class='mono'>")    # L299
    out.append("\n".join(hexrows))                      # L300
    out.append("</pre>" + HTML_TAIL)                    # L301
    return Response("".join(out), mimetype="text/html") # L302

@app.get("/api/upload/<int:rowid>.json")
def api_decoded(rowid: int):                            # L305: machine-readable decoded endpoint.
    cur = get_db().cursor()                             # L306
    cur.execute("SELECT device_id, ts_start, ts_end, codec, order_json, ts_list_json, received_at, block FROM uploads WHERE id=?", (rowid,))  # L307
    row = cur.fetchone()                                # L308
    if not row: return jsonify({"ok": False, "error": "not-found"}), 404  # L309
    dev, ts0, ts1, codec, order_json, ts_list_json, recv, blob = row      # L310
    order = json.loads(order_json)                      # L311
    ts_list = json.loads(ts_list_json) if ts_list_json else None  # L312
    rows, note = decode_delta_rle_v1(blob, order) if codec=="delta_rle_v1" else ([], "unsupported")  # L313

    n = len(rows)                                       # L315
    device_ms = _device_ms_list(n, ts0, ts1)            # L316
    times_ms  = _epoch_ms_list(n, ts0, ts1, recv, ts_list)  # L317

    return jsonify({                                    # L319
        "ok": True,
        "device_id": dev,
        "codec": codec,
        "order": order,
        "rows_raw": rows,
        "device_ms": device_ms,
        "times_ms": times_ms,
        "decode_note": note,
        "received_at_ms": recv
    })                                                  # L330

if __name__ == "__main__":                              # L333: dev server entry point (not used in production WSGI).
    from waitress import serve                          # L334: waitress is a simple WSGI server.
    port = int(os.getenv("PORT", "5000"))               # L335: port from env or default 5000.
    pathlib.Path(LOG_DIR).mkdir(parents=True, exist_ok=True)  # L336: ensure logs dir exists.
    print(f"[API] starting on 0.0.0.0:{port}, DB={DB_PATH}, auth={'on' if REQUIRE_AUTH else 'off'}")  # L337: console note.
    serve(app, host="0.0.0.0", port=port)               # L338: run waitress server.
