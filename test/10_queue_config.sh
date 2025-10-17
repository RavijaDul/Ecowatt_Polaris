LOG_DIR="../server/logs"
mkdir -p "$LOG_DIR"
cat > "$LOG_DIR/config_update.json" <<'JSON'
{
  "sampling_interval": 5,
  "registers": ["voltage","current","frequency","temperature","export_percent","pac"]
}
JSON
echo "Queued config_update.json"
