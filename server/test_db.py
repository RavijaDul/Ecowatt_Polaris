#!/usr/bin/env python3
import sqlite3
import os

DB_PATH = "ecowatt.db"

# Remove old DB
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

# Create connection
db = sqlite3.connect(DB_PATH)
db.execute("PRAGMA journal_mode=WAL;")
db.execute("PRAGMA synchronous=NORMAL;")

# Create tables
DDL = """
CREATE TABLE IF NOT EXISTS fota_progress(
  device TEXT PRIMARY KEY,
  version TEXT,
  size INTEGER,
  written INTEGER,
  percent INTEGER,
  status TEXT DEFAULT 'pending',
  updated TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fota_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT DEFAULT CURRENT_TIMESTAMP,
  device TEXT,
  kind TEXT,
  detail TEXT
);

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
);

CREATE INDEX IF NOT EXISTS idx_fota_versions_device ON fota_versions(device, updated_at DESC);
"""

db.executescript(DDL)
db.commit()

# Check tables
tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
print(f"✓ Database created with {len(tables)} tables:")
for (name,) in tables:
    cols = db.execute(f"PRAGMA table_info({name})").fetchall()
    print(f"  - {name}: {', '.join(col[1] for col in cols)}")

db.close()
print("\n✓ Database initialization successful!")
