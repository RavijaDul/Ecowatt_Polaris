#!/usr/bin/env python3
"""
Power report helper

Usage:
  # Snapshot server power summary for a device and write to JSON
  python scripts/power_report.py snapshot --url http://localhost:5000 --device EcoWatt-Dev-01 --out before.json

  # Compare two snapshots
  python scripts/power_report.py compare --before before.json --after after.json

This script queries `GET /api/power/<device>` for recent entries and summarizes average idle/sleep/uplink times.
"""
import argparse, json, sys, statistics, requests


def snapshot(url, device, out):
    r = requests.get(f"{url.rstrip('/')}/api/power/{device}")
    r.raise_for_status()
    with open(out, 'w') as f:
        json.dump(r.json(), f, indent=2)
    print(f"Wrote snapshot to {out}")


def summarize_list(rows):
    if not rows:
        return {}
    idle = [r['idle_budget_ms'] for r in rows]
    sleep = [r['t_sleep_ms'] for r in rows]
    uplink = [r['t_uplink_ms'] for r in rows]
    bytes_ = [r['uplink_bytes'] for r in rows]
    # energy estimate using env or defaults (mV,mA)
    import os
    V_mV = int(os.getenv('POWER_V_SUPPLY_MV') or 5000)
    I_active = int(os.getenv('POWER_I_ACTIVE_MA') or 200)
    I_uplink = int(os.getenv('POWER_I_UPLINK_MA') or 300)
    I_sleep = int(os.getenv('POWER_I_SLEEP_MA') or 5)
    total_sleep_s = sum(sleep)/1000.0
    total_uplink_s = sum(uplink)/1000.0
    total_idle_s = sum(idle)/1000.0
    V = V_mV/1000.0
    E_sleep = V*(I_sleep/1000.0)*total_sleep_s
    E_uplink = V*(I_uplink/1000.0)*total_uplink_s
    E_idle = V*(I_active/1000.0)*total_idle_s
    energy_J = E_sleep + E_uplink + E_idle
    return {
        'n': len(rows),
        'idle_avg': statistics.mean(idle),
        'sleep_avg': statistics.mean(sleep),
        'uplink_avg': statistics.mean(uplink),
        'bytes_avg': statistics.mean(bytes_)
        , 'est_energy_J': energy_J
    }


def compare(before_file, after_file):
    b = json.load(open(before_file))
    a = json.load(open(after_file))
    sb = summarize_list(b)
    sa = summarize_list(a)
    print("Metric, before, after, delta")
    keys = [('idle_avg','Idle ms'),('sleep_avg','Sleep ms'),('uplink_avg','Uplink ms'),('bytes_avg','Bytes')]
    for k,label in keys:
        bv = sb.get(k, 0)
        av = sa.get(k, 0)
        print(f"{label}, {bv:.2f}, {av:.2f}, {av-bv:.2f}")


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest='cmd')
    s1 = sp.add_parser('snapshot')
    s1.add_argument('--url', required=True)
    s1.add_argument('--device', required=True)
    s1.add_argument('--out', required=True)
    s2 = sp.add_parser('compare')
    s2.add_argument('--before', required=True)
    s2.add_argument('--after', required=True)
    args = p.parse_args()
    if args.cmd == 'snapshot':
        snapshot(args.url, args.device, args.out)
    elif args.cmd == 'compare':
        compare(args.before, args.after)
    else:
        p.print_help()
        sys.exit(1)
