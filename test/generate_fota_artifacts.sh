#!/usr/bin/env bash
set -euo pipefail

# === generate_fota_artifacts.sh ===
# Usage:
#   ./generate_fota_artifacts.sh /path/to/ecowatt.bin 32768 1.2.0 ./fota_out
# Produces:
#   <OUT>/logs/fota_manifest_good.json
#   <OUT>/logs/fota_manifest_bad.json
#   <OUT>/logs/fota_chunk_0000.b64 ...

BIN="${1:-./ecowatt.bin}"
CHUNK="${2:-32768}"
VERSION="${3:-1.0.0}"
OUTDIR="${4:-./fota_out}"

mkdir -p "${OUTDIR}/logs"

# ---- File size (portable) ----
if command -v python3 >/dev/null 2>&1; then
  SIZE=$(python3 - "$BIN" <<'PY'
import os,sys
p=sys.argv[1]
print(os.path.getsize(p))
PY
)
elif command -v python >/dev/null 2>&1; then
  SIZE=$(python - "$BIN" <<'PY'
import os,sys
p=sys.argv[1]
print(os.path.getsize(p))
PY
)
else
  # fallback: wc -c (works on Git Bash too)
  SIZE=$(wc -c <"$BIN" | tr -d '[:space:]')
fi

# ---- SHA-256 (portable) ----
if command -v sha256sum >/dev/null 2>&1; then
  HASH=$(sha256sum "$BIN" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
  HASH=$(shasum -a 256 "$BIN" | awk '{print $1}')
elif command -v openssl >/dev/null 2>&1; then
  # openssl prints "(stdin)= ABCD..."; we need the last field
  HASH=$(openssl dgst -sha256 "$BIN" | awk '{print $NF}')
else
  # python fallback
  if command -v python3 >/dev/null 2>&1; then
    HASH=$(python3 - "$BIN" <<'PY'
import sys,hashlib
b=open(sys.argv[1],'rb').read()
print(hashlib.sha256(b).hexdigest())
PY
)
  else
    echo "No SHA256 tool available (sha256sum/shasum/openssl/python)." >&2
    exit 1
  fi
fi

echo "[FOTA] BIN=${BIN} SIZE=${SIZE} HASH=${HASH} CHUNK=${CHUNK} VERSION=${VERSION}"

# ---- Split + base64 ----
tmpdir="${OUTDIR}/chunks_raw"
rm -rf "${tmpdir}"
mkdir -p "${tmpdir}"

# Prefer GNU/BSD split if present; otherwise use a Python chunker
if command -v split >/dev/null 2>&1; then
  # -d for numeric suffix if supported; ignore if not.
  if split --help 2>&1 | grep -q -- '-d'; then
    split -b "${CHUNK}" -d -a 4 -- "${BIN}" "${tmpdir}/part_"
  else
    split -b "${CHUNK}"    -a 4    -- "${BIN}" "${tmpdir}/part_"
  fi
else
  # Python fallback to create part_0000, part_0001, ...
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$BIN" "$CHUNK" "$tmpdir" <<'PY'
import os,sys,math
path,chunk,tmp=sys.argv[1],int(sys.argv[2]),sys.argv[3]
b=open(path,'rb').read()
n=math.ceil(len(b)/chunk)
for i in range(n):
    with open(os.path.join(tmp,f"part_{i:04d}"),'wb') as f:
        f.write(b[i*chunk:(i+1)*chunk])
PY
  else
    echo "No split or python3 available to chunk firmware." >&2
    exit 1
  fi
fi

# base64 each chunk to OUTDIR/logs/fota_chunk_XXXX.b64
i=0
# robust glob (handles spaces)
while IFS= read -r -d '' f; do
  printf -v idx "%04d" "$i"
  if command -v base64 >/dev/null 2>&1; then
    base64 "$f" > "${OUTDIR}/logs/fota_chunk_${idx}.b64"
  else
    # openssl fallback
    openssl base64 -A -in "$f" -out "${OUTDIR}/logs/fota_chunk_${idx}.b64"
  fi
  i=$((i+1))
done < <(find "$tmpdir" -type f -name 'part_*' -print0 | sort -z)

rm -rf "${tmpdir}"

# ---- Manifests ----
cat > "${OUTDIR}/logs/fota_manifest_good.json" <<EOF
{
  "manifest": {
    "version": "${VERSION}",
    "size": ${SIZE},
    "chunk_size": ${CHUNK},
    "hash": "${HASH}"
  }
}
EOF

# flip last hex nibble for BAD manifest
BADHASH="${HASH}"
if [ -n "${BADHASH}" ]; then
  last="${BADHASH: -1}"
  case "$last" in



# #!/usr/bin/env bash
# set -euo pipefail
# # === generate_fota_artifacts.sh ===
# # Usage:
# #   ./generate_fota_artifacts.sh /path/to/ecowatt.bin 8192 1.1.0 ./fota_out
# # ./generate_fota_artifacts.sh /path/to/ecowatt.bin 8192 1.1.0 ./fota_out
# # It will produce:
# #   fota_out/logs/fota_manifest_good.json
# #   fota_out/logs/fota_manifest_bad.json   (intentionally wrong hash)
# #   fota_out/logs/fota_chunk_0000.b64 ... (base64 chunks)
# #
# # If your server watches <server_root>/logs/* just copy the produced "logs" dir there.
# #
# BIN="${1:-./ecowatt.bin}"
# CHUNK="${2:-8192}"
# VERSION="${3:-1.1.0}"
# OUTDIR="${4:-./fota_out}"

# mkdir -p "${OUTDIR}/logs"
# SIZE=$(stat -c%s "${BIN}" 2>/dev/null || stat -f%z "${BIN}")
# HASH=$(sha256sum "${BIN}" 2>/dev/null | awk '{print $1}')
# if [ -z "${HASH:-}" ]; then
#   # macOS fallback
#   HASH=$(shasum -a 256 "${BIN}" | awk '{print $1}')
# fi

# echo "[FOTA] BIN=${BIN} SIZE=${SIZE} HASH=${HASH} CHUNK=${CHUNK} VERSION=${VERSION}"

# # Split and base64-encode chunks (0000,0001,...)
# tmpdir="${OUTDIR}/chunks_raw"
# rm -rf "${tmpdir}"
# mkdir -p "${tmpdir}"
# # split with numeric suffix width 4 (0000, 0001, ...)
# split -b "${CHUNK}" -d -a 4 "${BIN}" "${tmpdir}/part_"
# i=0
# for f in $(ls "${tmpdir}" | sort); do
#   printf -v idx "%04d" "${i}"
#   base64 "${tmpdir}/${f}" > "${OUTDIR}/logs/fota_chunk_${idx}.b64"
#   i=$((i+1))
# done
# rm -rf "${tmpdir}"

# # Manifest (good)
# cat > "${OUTDIR}/logs/fota_manifest_good.json" <<EOF
# {
#   "manifest": {
#     "version": "${VERSION}",
#     "size": ${SIZE},
#     "chunk_size": ${CHUNK},
#     "hash": "${HASH}"
#   }
# }
# EOF

# # Manifest (bad hash -> last hex nibble flipped)
# BADHASH="${HASH}"
# if [ -n "${BADHASH}" ]; then
#   last="${BADHASH: -1}"
#   case "$last" in
#     0) repl=1;; 1) repl=2;; 2) repl=3;; 3) repl=4;; 4) repl=5;;
#     5) repl=6;; 6) repl=7;; 7) repl=8;; 8) repl=9;; 9) repl=a;;
#     a) repl=b;; b) repl=c;; c) repl=d;; d) repl=e;; e) repl=f;; f) repl=0;;
#     *) repl=0;;
#   esac
#   BADHASH="${BADHASH::-1}${repl}"
# fi

# cat > "${OUTDIR}/logs/fota_manifest_bad.json" <<EOF
# {
#   "manifest": {
#     "version": "${VERSION}",
#     "size": ${SIZE},
#     "chunk_size": ${CHUNK},
#     "hash": "${BADHASH}"
#   }
# }
# EOF

# echo "[FOTA] Wrote manifests + chunks into ${OUTDIR}/logs"
# echo "      Copy ${OUTDIR}/logs/* to your server's logs/ directory (or point server to OUTDIR)."
