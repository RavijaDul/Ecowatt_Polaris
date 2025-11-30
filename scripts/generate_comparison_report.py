#!/usr/bin/env python3
"""
Generate a comparison report from two power snapshot JSON files.

Usage:
  python scripts/generate_comparison_report.py --before before.json --after after.json --out report_dir

This will produce:
- report_dir/report.md   (markdown summary)
- report_dir/combined.png (time-series comparison)
- report_dir/before.csv, after.csv (copied CSVs if present)
- report_dir/metadata_before.json, metadata_after.json (if sidecar meta present)

The script expects the snapshot JSON format produced by `POST /api/power/snapshot` or
by `scripts/power_report.py snapshot` (which writes the server's JSON to file).

Energy estimates use either sidecar meta (.meta.json) or environment variables:
  POWER_V_SUPPLY_MV, POWER_I_ACTIVE_MA, POWER_I_UPLINK_MA, POWER_I_SLEEP_MA

Estimates are approximate; explain in report.
"""
import argparse, json, os, sys
import datetime
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def load_json(path):
    with open(path,'r') as f: return json.load(f)


def summarize_rows(rows):
    if not rows: return None
    n = len(rows)
    idle = [r['idle_budget_ms'] for r in rows]
    sleep = [r['t_sleep_ms'] for r in rows]
    uplink = [r['t_uplink_ms'] for r in rows]
    bytes_ = [r['uplink_bytes'] for r in rows]
    return {
        'n': n,
        'idle_avg': sum(idle)/n,
        'sleep_avg': sum(sleep)/n,
        'uplink_avg': sum(uplink)/n,
        'bytes_avg': sum(bytes_)/n,
        'idle_sum': sum(idle),
        'sleep_sum': sum(sleep),
        'uplink_sum': sum(uplink),
    }


def estimate_energy(rows, defaults):
    # Always return a dict so callers can index into returned values safely.
    if not rows:
        return {'total_J': 0.0, 'sleep_J': 0.0, 'uplink_J': 0.0, 'idle_J': 0.0}

    V = defaults['V_mV']/1000.0
    I_active = defaults['I_active_mA']/1000.0
    I_uplink = defaults['I_uplink_mA']/1000.0
    I_sleep = defaults['I_sleep_mA']/1000.0
    s_sleep = sum(r.get('t_sleep_ms', 0) for r in rows)/1000.0
    s_uplink = sum(r.get('t_uplink_ms', 0) for r in rows)/1000.0
    s_idle = sum(r.get('idle_budget_ms', 0) for r in rows)/1000.0
    E_sleep = V * I_sleep * s_sleep
    E_uplink = V * I_uplink * s_uplink
    E_idle = V * I_active * s_idle
    return {'total_J': E_sleep+E_uplink+E_idle, 'sleep_J': E_sleep, 'uplink_J': E_uplink, 'idle_J': E_idle}


def plot_compare(rows_before, rows_after, out_png, label_before='before', label_after='after'):
    # align times: use index positions since snapshots may be different timestamps
    x_b = list(range(len(rows_before)))
    x_a = list(range(len(rows_after)))
    fig, ax = plt.subplots(3,1,figsize=(12,8), sharex=False)
    if rows_before:
        ax[0].plot(x_b, [r['t_sleep_ms'] for r in rows_before], '-o', label=label_before)
    if rows_after:
        ax[0].plot(x_a, [r['t_sleep_ms'] for r in rows_after], '-o', label=label_after)
    ax[0].set_ylabel('t_sleep_ms'); ax[0].legend(); ax[0].grid(True)

    if rows_before:
        ax[1].plot(x_b, [r['t_uplink_ms'] for r in rows_before], '-o', label=label_before)
    if rows_after:
        ax[1].plot(x_a, [r['t_uplink_ms'] for r in rows_after], '-o', label=label_after)
    ax[1].set_ylabel('t_uplink_ms'); ax[1].legend(); ax[1].grid(True)

    if rows_before:
        ax[2].plot(x_b, [r['idle_budget_ms'] for r in rows_before], '-o', label=label_before)
    if rows_after:
        ax[2].plot(x_a, [r['idle_budget_ms'] for r in rows_after], '-o', label=label_after)
    ax[2].set_ylabel('idle_budget_ms'); ax[2].legend(); ax[2].grid(True)

    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)


def write_report(out_dir, before_path, after_path, before_rows, after_rows, before_meta, after_meta, defaults):
    s_before = summarize_rows(before_rows) or {}
    s_after = summarize_rows(after_rows) or {}
    e_before = estimate_energy(before_rows, defaults)
    e_after = estimate_energy(after_rows, defaults)
    os.makedirs(out_dir, exist_ok=True)
    png = os.path.join(out_dir, 'combined.png')
    plot_compare(before_rows, after_rows, png)

    md = []
    md.append(f"# Power Comparison Report\n")
    md.append(f"Generated: {datetime.datetime.utcnow().isoformat()} UTC\n")
    md.append("## Summary metrics\n")
    md.append("| Metric | Before | After | Delta |")
    md.append("|---:|---:|---:|---:|")
    def fmt(v):
        if isinstance(v, float): return f"{v:.2f}"
        return str(v)
    pairs = [
        ('samples', s_before.get('n',0), s_after.get('n',0)),
        ('idle_avg_ms', s_before.get('idle_avg',0), s_after.get('idle_avg',0)),
        ('sleep_avg_ms', s_before.get('sleep_avg',0), s_after.get('sleep_avg',0)),
        ('uplink_avg_ms', s_before.get('uplink_avg',0), s_after.get('uplink_avg',0)),
        ('uplink_bytes_avg', s_before.get('bytes_avg',0), s_after.get('bytes_avg',0)),
        ('est_energy_J', e_before['total_J'], e_after['total_J']),
    ]
    for k, bv, av in pairs:
        md.append(f"| {k} | {fmt(bv)} | {fmt(av)} | {fmt(av - bv)} |")

    md.append('\n## Plots\n')
    md.append('![combined](combined.png)')
    md.append('\n## Details\n')
    md.append('Before metadata:')
    md.append('```json')
    md.append(json.dumps(before_meta or {}, indent=2))
    md.append('```')
    md.append('After metadata:')
    md.append('```json')
    md.append(json.dumps(after_meta or {}, indent=2))
    md.append('```')

    md_path = os.path.join(out_dir, 'report.md')
    with open(md_path, 'w') as f:
        f.write('\n'.join(md))
    # copy source JSONs into report dir
    import shutil
    shutil.copy(before_path, os.path.join(out_dir,'before.json'))
    shutil.copy(after_path, os.path.join(out_dir,'after.json'))
    return md_path


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--before', required=True)
    p.add_argument('--after', required=True)
    p.add_argument('--out', required=True)
    args = p.parse_args()

    before = load_json(args.before)
    after = load_json(args.after)
    # handle top-level wrapper if present (snapshots previously written by server snapshot include list directly)
    rows_before = before if isinstance(before, list) else before.get('rows') if isinstance(before, dict) else None
    rows_after = after if isinstance(after, list) else after.get('rows') if isinstance(after, dict) else None
    if rows_before is None:
        # try to interpret as saved snapshot structure (list expected)
        if isinstance(before, dict) and 'data' in before:
            rows_before = before['data']
    if rows_after is None:
        if isinstance(after, dict) and 'data' in after:
            rows_after = after['data']
    if rows_before is None or rows_after is None:
        print('Unable to parse input JSONs into rows (expect list of power entries).', file=sys.stderr)
        sys.exit(2)

    # try read sidecar meta jsons
    before_meta = None
    after_meta = None
    bmeta = os.path.splitext(args.before)[0] + '.meta.json'
    ameta = os.path.splitext(args.after)[0] + '.meta.json'
    if os.path.exists(bmeta): before_meta = load_json(bmeta)
    if os.path.exists(ameta): after_meta = load_json(ameta)

    defaults = {
        'V_mV': int(os.getenv('POWER_V_SUPPLY_MV') or os.getenv('ECOWATT_POWER_V_SUPPLY') or 5000),
        'I_active_mA': int(os.getenv('POWER_I_ACTIVE_MA') or os.getenv('ECOWATT_POWER_I_ACTIVE_MA') or 200),
        'I_uplink_mA': int(os.getenv('POWER_I_UPLINK_MA') or os.getenv('ECOWATT_POWER_I_UPLINK_MA') or 300),
        'I_sleep_mA': int(os.getenv('POWER_I_SLEEP_MA') or os.getenv('ECOWATT_POWER_I_SLEEP_MA') or 5),
    }

    md = write_report(args.out, args.before, args.after, rows_before, rows_after, before_meta, after_meta, defaults)
    print('Report written to', md)

if __name__=='__main__':
    main()
