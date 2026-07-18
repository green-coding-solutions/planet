#!/usr/bin/env python3
"""
Fit PLANET's run-time energy coefficients (offline, once per platform).

PLANET's run-time model (paper Eq. 2) is linear in counters the engine already
maintains:

    E_joules = cpu_active_watts   * cpu_seconds
             + joules_per_read    * blks_read
             + joules_per_write   * blks_written
             + joules_per_wal_byte* wal_bytes

We fit the four coefficients by regressing this counter vector against a
GROUND-TRUTH energy signal (RAPL package+DRAM on Linux, and/or a wall-power
meter) collected over a workload sweep.  This is the ONLY place RAPL is used;
at run time PLANET reads no hardware counter.

Input CSV (one row per measured query) with a header including at least:
    cpu_seconds, blks_read, blks_written, wal_bytes, energy_joules
where energy_joules is the measured ground truth for that query.

Usage:
    python3 fit_model.py sweep.csv                 # print coefficients + SET stmts
    python3 fit_model.py sweep.csv -o coeffs.json  # also write JSON

Non-negative least squares is used (all coefficients are physically >= 0).
Reports R^2 and mean absolute percentage error so you can judge fidelity
(paper Q1).
"""
import argparse
import csv
import json
import sys

# wall_seconds carries the awake term: power the package draws for the whole
# duration of a query merely because it is out of deep C-states -- above idle,
# independent of load. Without it, the wall-meter fit on a Xeon E-2176G lands
# at 22.8% held-out MAPE; with it, 2.0%. NNLS keeps the coefficient at 0 on
# machines where the term does not exist, recovering the 4-term model.
FEATURES = ["cpu_seconds", "wall_seconds", "blks_read", "blks_written",
            "wal_bytes"]
GUC = {
    "cpu_seconds": "planet.cpu_active_watts",
    "wall_seconds": "planet.awake_watts",
    "blks_read": "planet.joules_per_read",
    "blks_written": "planet.joules_per_write",
    "wal_bytes": "planet.joules_per_wal_byte",
}


def set_four_term():
    """Drop the awake term (wall_seconds is collinear with cpu_seconds on
    fully cached sweeps, e.g. the laptop's, and NNLS then degenerates)."""
    global FEATURES
    FEATURES = [f for f in FEATURES if f != "wall_seconds"]


def load(path):
    X, y = [], []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                X.append([float(row[k]) for k in FEATURES])
                y.append(float(row["energy_joules"]))
            except (KeyError, ValueError) as e:
                print(f"skipping row ({e}): {row}", file=sys.stderr)
    if not X:
        sys.exit("no usable rows; need columns: "
                 + ", ".join(FEATURES + ["energy_joules"]))
    return X, y


def solve(A, b):
    """Non-negative least squares; the single definition of "fitting PLANET".

    validate_model.py imports this so its cross-validated numbers come from the
    same estimator the deployed coefficients do.
    """
    try:
        from scipy.optimize import nnls
        coef, _ = nnls(A, b)
    except ImportError:
        import numpy as np
        coef, *_ = np.linalg.lstsq(A, b, rcond=None)
        coef = np.clip(coef, 0.0, None)  # enforce non-negativity
    return coef


def fit(X, y):
    try:
        import numpy as np
    except ImportError:
        sys.exit("numpy is required: pip install numpy (scipy optional for NNLS)")
    A = np.asarray(X, dtype=float)
    b = np.asarray(y, dtype=float)
    coef = solve(A, b)
    pred = A @ coef
    ss_res = float(((b - pred) ** 2).sum())
    ss_tot = float(((b - b.mean()) ** 2).sum()) or 1.0
    r2 = 1.0 - ss_res / ss_tot
    nz = b != 0
    mape = float((abs((b[nz] - pred[nz]) / b[nz])).mean() * 100.0) if nz.any() else float("nan")
    return {k: float(c) for k, c in zip(FEATURES, coef)}, r2, mape


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="sweep CSV with counters + energy_joules")
    ap.add_argument("-o", "--out", help="write coefficients JSON here")
    ap.add_argument("--four-term", action="store_true",
                    help="drop the awake (wall_seconds) term")
    args = ap.parse_args()

    if args.four_term:
        set_four_term()

    X, y = load(args.csv)
    coef, r2, mape = fit(X, y)

    print(f"# fit on {len(y)} queries   R^2={r2:.5f}   MAPE={mape:.2f}%\n")
    print("-- paste into psql (or ALTER SYSTEM / postgresql.conf):")
    for feat in FEATURES:
        print(f"SET {GUC[feat]} = {coef[feat]:.6g};")
    print("\n# note: grid_gco2_per_kwh, embodied_gco2_per_byte and "
          "idle_watts_per_byte come from the grid + device datasheets, not this fit.")

    if args.out:
        with open(args.out, "w") as f:
            json.dump({"fit": {"r2": r2, "mape_pct": mape, "n": len(y)},
                       "gucs": {GUC[k]: coef[k] for k in FEATURES}}, f, indent=2)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
