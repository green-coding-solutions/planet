#!/usr/bin/env python3
"""
Validity check for one calibration sweep. Run it after every sweep.

A healthy sweep fits its own data in-sample at roughly 7-12% MAPE. A failing
wall meter does NOT announce itself in the row count: the CSV looks complete
while the energy column is quietly wrong, and the in-sample fit blows up
(we have observed 58-304%). That is the signal this script tests, together
with cheap structural checks.

    python3 check_sweep.py sweep.csv [limit_mape]

Exits 0 on PASS, 1 on FAIL, and prints one line either way.
"""
import csv
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fit_model  # noqa: E402  (uses scipy NNLS when present, numpy otherwise)

FEATS = ["cpu_seconds", "wall_seconds", "blks_read", "blks_written", "wal_bytes"]
MIN_ROWS = 60
DEFAULT_LIMIT = 15.0


def main():
    path = sys.argv[1]
    limit = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_LIMIT
    try:
        rows = list(csv.DictReader(open(path)))
    except FileNotFoundError:
        print(f"FAIL {path}: missing (sweep did not finish)")
        return 1

    if len(rows) < MIN_ROWS:
        print(f"FAIL rows={len(rows)} < {MIN_ROWS} (sweep truncated)")
        return 1

    X = np.array([[float(r[f]) for f in FEATS] for r in rows])
    y = np.array([float(r["energy_joules"]) for r in rows])

    if (y <= 0).any():
        print(f"FAIL {int((y <= 0).sum())} non-positive energy samples "
              "(meter gap or bad idle subtraction)")
        return 1

    coef = fit_model.solve(X, y)
    pred = X @ coef
    mape = float(np.mean(np.abs((y - pred) / y)) * 100)

    per_fam = {}
    for r, p, a in zip(rows, pred, y):
        per_fam.setdefault(r["family"], []).append(abs((a - p) / a) * 100)
    worst_err, worst_fam = max((float(np.mean(v)), k) for k, v in per_fam.items())

    ok = mape <= limit
    print(f"{'PASS' if ok else 'FAIL'} rows={len(rows)} "
          f"in-sample MAPE={mape:.1f}% (limit {limit:.0f}%) "
          f"worst family={worst_fam} {worst_err:.0f}% "
          f"P_cpu={coef[0]:.2f}W P_awake={coef[1]:.2f}W")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
