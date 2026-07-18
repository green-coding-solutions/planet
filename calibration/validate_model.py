#!/usr/bin/env python3
"""
How accurate is PLANET's run-time energy model?  This is the fidelity number
worth quoting.

fit_model.py reports R^2 and MAPE *on the rows it fit*. That is an upper bound
on fidelity, not a measurement of it: with four free coefficients and a few
dozen queries it is optimistic, and a reviewer will say so. This script reports
what PLANET actually does on queries it has never seen:

  * k-fold cross-validation -- refit on k-1 folds, predict the held-out fold,
    so every row gets an out-of-sample prediction. This is the headline number.
  * leave-one-family-out -- train on every workload family except one, predict
    that one. This answers the harder question: does a model fit on scans and
    inserts extrapolate to a workload shape it never saw? Expect it to be worse
    than k-fold; report it anyway, it is the honest bound on portability.
  * a bootstrap CI on the held-out MAPE, so the number carries an error bar.
  * the design matrix's condition number and each term's share of predicted
    energy. If the sweep failed to decorrelate writes from WAL, the fit will
    still look great here and the coefficients will still be meaningless --
    a high condition number is the only warning you get.

Usage:
    python3 validate_model.py sweep.csv
    python3 validate_model.py sweep.csv -o fidelity.pdf     # + scatter plot
    python3 validate_model.py sweep.csv --tex fidelity.tex  # \\newcommand macros
"""
import argparse
import csv
import random
import sys

import numpy as np

from fit_model import FEATURES, GUC, solve

TERMS = {"cpu_seconds": "compute", "wall_seconds": "awake",
         "blks_read": "reads", "blks_written": "writes", "wal_bytes": "wal"}


def load(path, min_joules):
    X, y, fam, dropped = [], [], [], 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                xs = [float(row[k]) for k in FEATURES]
                e = float(row["energy_joules"])
            except (KeyError, ValueError):
                dropped += 1
                continue
            if e <= 0 or e < min_joules:
                # Non-positive energy means idle subtraction ate the signal:
                # the query cost less than the machine's own background.
                dropped += 1
                continue
            X.append(xs)
            y.append(e)
            fam.append(row.get("family", "all"))
    if len(X) < 8:
        sys.exit(f"only {len(X)} usable rows; need columns "
                 + ", ".join(FEATURES + ["energy_joules"]))
    return np.array(X), np.array(y), fam, dropped


def metrics(y, pred):
    ss_res = float(((y - pred) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum()) or 1.0
    ape = np.abs((y - pred) / y) * 100.0
    return {"r2": 1.0 - ss_res / ss_tot,
            "mape": float(ape.mean()), "medape": float(np.median(ape)),
            "p90ape": float(np.percentile(ape, 90)),
            "mae": float(np.abs(y - pred).mean()),
            "rmse": float(np.sqrt(((y - pred) ** 2).mean()))}


def cross_val(X, y, k, seed):
    """Out-of-sample prediction for every row, via k-fold CV."""
    idx = list(range(len(y)))
    random.Random(seed).shuffle(idx)
    folds = [idx[i::k] for i in range(k)]
    pred = np.zeros_like(y)
    for test in folds:
        held = set(test)
        train = [i for i in idx if i not in held]
        coef = solve(X[train], y[train])
        pred[test] = X[test] @ coef
    return pred


def leave_one_family_out(X, y, fam):
    out = {}
    for held in sorted(set(fam)):
        test = [i for i, f in enumerate(fam) if f == held]
        train = [i for i, f in enumerate(fam) if f != held]
        if not train or len(set(f for f in fam if f != held)) < 2:
            continue
        coef = solve(X[train], y[train])
        out[held] = (metrics(y[test], X[test] @ coef), len(test))
    return out


def bootstrap_mape(y, pred, n=2000, seed=1):
    rng = random.Random(seed)
    m = len(y)
    vals = []
    for _ in range(n):
        s = [rng.randrange(m) for _ in range(m)]
        vals.append(float((np.abs((y[s] - pred[s]) / y[s]) * 100.0).mean()))
    vals.sort()
    return vals[int(0.025 * n)], vals[int(0.975 * n)]


def conditioning(X):
    """Condition number of the column-normalized design matrix.

    Raw columns span 1e-1 (cpu seconds) to 1e9 (wal bytes), so the raw
    condition number measures unit choice, not collinearity. Normalizing each
    column to unit norm leaves only the geometry we care about.
    """
    norms = np.linalg.norm(X, axis=0)
    norms[norms == 0] = 1.0
    return float(np.linalg.cond(X / norms))


def plot(y, pred, fam, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("matplotlib is required for -o")
    fig, ax = plt.subplots(figsize=(4.2, 3.8))
    lo, hi = min(y.min(), pred.min()) * 0.7, max(y.max(), pred.max()) * 1.4
    ax.plot([lo, hi], [lo, hi], color="0.6", lw=0.8, zorder=0)
    for f in sorted(set(fam)):
        m = [i for i, x in enumerate(fam) if x == f]
        ax.scatter(y[m], pred[m], s=16, alpha=0.8, label=f)
    ax.set_xscale("log"), ax.set_yscale("log")
    ax.set_xlim(lo, hi), ax.set_ylim(lo, hi)
    ax.set_xlabel("measured energy (J, RAPL, above idle)")
    ax.set_ylabel("PLANET estimate (J, held out)")
    ax.legend(fontsize=6, frameon=False)
    fig.tight_layout()
    fig.savefig(path)
    print(f"\nwrote {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="sweep CSV from collect_sweep.py")
    ap.add_argument("-k", "--folds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20260804)
    ap.add_argument("--four-term", action="store_true",
                    help="drop the awake (wall_seconds) term")
    ap.add_argument("--min-joules", type=float, default=0.0,
                    help="drop samples below this measured energy")
    ap.add_argument("-o", "--out", help="scatter plot (needs matplotlib)")
    ap.add_argument("--tex", help="write \\newcommand macros for a LaTeX document")
    args = ap.parse_args()

    if args.four_term:
        import fit_model
        fit_model.set_four_term()
        global FEATURES
        FEATURES = fit_model.FEATURES

    X, y, fam, dropped = load(args.csv, args.min_joules)
    n, k = len(y), min(args.folds, len(y))

    coef = solve(X, y)
    ins = metrics(y, X @ coef)
    cv_pred = cross_val(X, y, k, args.seed)
    cv = metrics(y, cv_pred)
    lo, hi = bootstrap_mape(y, cv_pred)

    print("PLANET run-time energy model -- fidelity")
    print(f"  {n} queries, {y.min():.2f}-{y.max():.2f} J measured "
          f"({y.max()/y.min():.0f}x range)"
          + (f", {dropped} row(s) dropped" if dropped else ""))
    print()
    print(f"  {'':<18}{'in-sample':>12}{'held out (' + str(k) + '-fold)':>22}")
    for key, label, unit in [("r2", "R^2", ""), ("mape", "MAPE", " %"),
                             ("medape", "median APE", " %"),
                             ("p90ape", "90th pct APE", " %"),
                             ("mae", "MAE", " J"), ("rmse", "RMSE", " J")]:
        fmt = "{:>12.4f}" if key == "r2" else "{:>12.2f}"
        print(f"  {label:<18}" + fmt.format(ins[key]) +
              (fmt.format(cv[key]) + unit).rjust(22))
    print(f"\n  held-out MAPE 95% CI (bootstrap): {lo:.2f}% - {hi:.2f}%")
    print(f"  >>> held-out fidelity: {cv['mape']:.1f}% MAPE "
          f"(R^2 = {cv['r2']:.3f}), held out over {n} queries")

    print("\n  fitted coefficients (all rows):")
    contrib = (X * coef).sum(axis=0)
    total = contrib.sum() or 1.0
    for i, f in enumerate(FEATURES):
        print(f"    {GUC[f]:<30} = {coef[i]:<12.6g}"
              f"  {TERMS[f]:>8}: {100*contrib[i]/total:5.1f}% of energy")

    cond = conditioning(X)
    print(f"\n  design-matrix condition number: {cond:.1f}", end="  ")
    if cond > 30:
        print("\n  ! >30: counters are collinear. The fit may be accurate while\n"
              "    the individual coefficients are arbitrary -- widen the sweep\n"
              "    (e.g. unlogged inserts to separate writes from WAL).")
    else:
        print("(ok: each coefficient is identified)")

    lofo = leave_one_family_out(X, y, fam)
    if len(lofo) > 1:
        print(f"\n  leave-one-family-out (extrapolation to an unseen workload shape):")
        print(f"    {'family':<18}{'n':>4}{'MAPE %':>10}{'R^2':>9}")
        for f, (m, cnt) in sorted(lofo.items(), key=lambda kv: -kv[1][0]["mape"]):
            print(f"    {f:<18}{cnt:>4}{m['mape']:>10.1f}{m['r2']:>9.3f}")
        worst = max(lofo.values(), key=lambda v: v[0]["mape"])[0]["mape"]
        print(f"    worst family {worst:.1f}% -- quote this as the portability bound,"
              f"\n    not the {cv['mape']:.1f}% k-fold number, if you claim "
              "generalization to new workloads.")

    if args.out:
        plot(y, cv_pred, fam, args.out)

    if args.tex:
        with open(args.tex, "w") as f:
            f.write("% generated by validate_model.py -- do not edit\n")
            for name, val in [("PlanetQoneN", f"{n}"),
                              ("PlanetQoneMape", f"{cv['mape']:.1f}"),
                              ("PlanetQoneMapeLo", f"{lo:.1f}"),
                              ("PlanetQoneMapeHi", f"{hi:.1f}"),
                              ("PlanetQoneMedApe", f"{cv['medape']:.1f}"),
                              ("PlanetQoneRsq", f"{cv['r2']:.3f}"),
                              ("PlanetQoneRsqInSample", f"{ins['r2']:.3f}"),
                              ("PlanetQoneFolds", f"{k}"),
                              ("PlanetQoneCond", f"{cond:.0f}")]:
                f.write(f"\\newcommand{{\\{name}}}{{{val}}}\n")
        print(f"wrote {args.tex}")


if __name__ == "__main__":
    main()
