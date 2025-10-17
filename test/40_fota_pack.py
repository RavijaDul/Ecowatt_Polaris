# 40_fota_pack.py
import sys, json, base64, math, pathlib, hashlib, os
BIN   = sys.argv[1] if len(sys.argv)>1 else "build/ecowatt.bin"
CHUNK = int(sys.argv[2]) if len(sys.argv)>2 else 8192
VER   = sys.argv[3] if len(sys.argv)>3 else "1.0.4"
LOG   = os.getenv("LOG_DIR","logs")
pathlib.Path(LOG).mkdir(parents=True, exist_ok=True)

b = open(BIN,"rb").read()
h = hashlib.sha256(b).hexdigest()
mf = {"version":VER,"size":len(b),"hash":h,"chunk_size":CHUNK}
open(f"{LOG}/fota_manifest.json","w").write(json.dumps(mf, separators=(",",":")))

for i in range(0, len(b), CHUNK):
    chunk = b[i:i+CHUNK]
    enc   = base64.b64encode(chunk).decode()
    open(f"{LOG}/fota_chunk_{i//CHUNK:04d}.b64","w").write(enc+"\n")

print("manifest + chunks ready in", LOG)
