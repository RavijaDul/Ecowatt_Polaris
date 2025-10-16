# 20_queue_command.sh
LOG_DIR="../server/logs"

cat > "$LOG_DIR/command.json" <<'JSON'
{
  "action": "write_register",
  "target_register": "status_flag",
  "value": 30
}
JSON
echo "Queued command.json"
