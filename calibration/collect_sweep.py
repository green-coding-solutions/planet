#!/usr/bin/env python3
"""
Build `sweep.csv`: the calibration sweep that fit_model.py regresses.

For each sample we run ONE query inside a PSYS energy window, then read the
executor counters PLANET recorded for exactly that query:

    CHECKPOINT                      # never let one fire inside the window
    <setup>                         # utility stmts only; do not touch planet state
    sleep(settle)                   # let power fall back to idle
    e0, t0 = psys.sample()
    <the measured query>
    e1, t1 = psys.sample()
    SELECT ... FROM planet_last()   # counters of the query above
    <cleanup>

Two things make the resulting CSV fit-able.

1. Idle subtraction.  Eq. 2 has no intercept: it models the *dynamic* increment
   above idle (paper Sec. 4.1, energy proportionality).  So the ground truth
   must be dynamic too:

       energy_joules = E_psys(window) - P_idle * wall_seconds

   P_idle is re-measured periodically and linearly interpolated in time, which
   also absorbs thermal drift over a long sweep.

2. Counter decorrelation.  NNLS on collinear columns returns coefficients that
   fit but mean nothing.  The workload therefore drives each counter through a
   regime where the others are quiet -- notably an UNLOGGED insert (writes, ~no
   WAL) against a cached UPDATE (WAL, ~no writes), which breaks the
   writes/WAL collinearity that a naive INSERT-only sweep has.

Run this ON THE DATABASE HOST, as a superuser, on an otherwise idle machine.

Usage:
    python3 rapl.py --check                    # first: is PSYS live and readable?
    python3 collect_sweep.py -d bench -o sweep.csv
    python3 collect_sweep.py -d bench --quick  # ~5 min smoke run
    python3 fit_model.py sweep.csv -o ../config/coefficients.json
    python3 validate_model.py sweep.csv        # held-out fidelity

Only psql and the stdlib are needed. Expect ~25 min at defaults; `--scale`
controls the big table (default 20M rows, ~1.5 GB -- it must exceed
shared_buffers or the read term never lights up).

CAVEAT (v0.1): PLANET's cpu_seconds is leader-only getrusage, while PSYS meters
all cores.  The sweep therefore pins `max_parallel_workers_per_gather = 0`; a
parallel sweep would inflate cpu_active_watts to hide the workers' CPU.  See
../extension/README.md.
"""
import argparse
import csv
import json
import os
import platform
import random
import re
import subprocess
import sys
import time

import rapl

SENTINEL = "__planet_sweep_done__"

# Columns fit_model.py requires, first; provenance and diagnostics after.
FIELDS = [
    "cpu_seconds", "blks_read", "blks_written", "wal_bytes", "energy_joules",
    "sample", "family", "label", "knob", "rep",
    "wall_seconds", "energy_joules_raw", "idle_watts", "idle_joules",
    "noise_ratio", "domain", "t_mid",
]


class Psql:
    """A single long-lived psql session.

    PLANET's state is per-backend and planet_last() reports the previous
    top-level query, so every sample must run down one connection.
    """

    def __init__(self, psql="psql", dbname=None):
        cmd = [psql, "-X", "-q", "-A", "-t", "-F", "|", "-v", "ON_ERROR_STOP=1"]
        if dbname:
            cmd += ["-d", dbname]
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)

    def run(self, sql):
        """Send SQL, block until it completes, return its output lines."""
        self.proc.stdin.write(f"{sql}\n\\echo {SENTINEL}\n")
        self.proc.stdin.flush()
        lines = []
        while True:
            line = self.proc.stdout.readline()
            if line == "":                      # ON_ERROR_STOP killed psql
                raise RuntimeError("psql exited:\n  " + "\n  ".join(lines[-12:]))
            line = line.rstrip("\n")
            if line == SENTINEL:
                return lines
            if line:
                lines.append(line)

    def scalar(self, sql):
        rows = self.run(sql)
        if not rows:
            raise RuntimeError(f"no rows from: {sql}")
        return rows[-1]

    def close(self):
        try:
            self.proc.stdin.write("\\q\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


class Sample:
    def __init__(self, family, knob, query, setup=None, cleanup=None):
        self.family, self.knob, self.query = family, knob, query
        self.setup, self.cleanup = setup, cleanup
        self.label = f"{family}_{knob}"


def workload(small_rows, quick=False):
    """Queries chosen to span counter space, not to look like a benchmark.

    Each family holds one counter high while the others idle, so the design
    matrix is well conditioned (validate_model.py prints its condition number).
    """
    s = 0.35 if quick else 1.0
    n = lambda x: max(100_000, int(x * s))       # noqa: E731
    W = []

    # cpu only. The SRF must live in the TARGET LIST (ProjectSet, value-per-
    # call): `FROM generate_series(...)` is a function scan that materialises
    # into a tuplestore, and past work_mem that SPILLS -- on a 60M-row series
    # ~90k temp blocks read AND written, more block I/O than the read family
    # itself. The energy model then sees a "cpu" family whose counters are
    # dominated by temp I/O, and with direct data I/O in play (device reads
    # dear, temp reads cheap) the fit collapses. Streaming does no block I/O.
    for rows in (8_000_000, 24_000_000, 60_000_000, 120_000_000):
        W.append(Sample("cpu", n(rows),
                        f"SELECT count(*) FILTER (WHERE sqrt(g::float8) > 0.5) "
                        f"FROM (SELECT generate_series(1,{n(rows)}) AS g) s;"))

    # block reads, little else: TABLESAMPLE SYSTEM reads a fraction of blocks.
    for pct in (5, 15, 40, 100):
        W.append(Sample("read", pct,
                        f"SELECT count(*) FROM planet_cal_big "
                        f"TABLESAMPLE SYSTEM ({pct});"))

    # reads with a different cpu-per-block ratio: separates P_cpu from beta_r.
    for pct in (10, 30, 70):
        W.append(Sample("read_cpu", pct,
                        f"SELECT sum(sqrt(v) + ln(v + 1)) FROM planet_cal_big "
                        f"TABLESAMPLE SYSTEM ({pct});"))

    # temp-file traffic: a spilling sort drives temp_blks_read AND _written.
    for pct in (10, 25, 50):
        W.append(Sample("sort_spill", pct,
                        f"SELECT count(*) FROM (SELECT v FROM planet_cal_big "
                        f"TABLESAMPLE SYSTEM ({pct}) ORDER BY v) t;",
                        setup="SET work_mem = '1MB';", cleanup="RESET work_mem;"))

    # writes + WAL (logged) ...
    for rows in (500_000, 2_000_000, 6_000_000):
        W.append(Sample("insert_logged", n(rows),
                        f"INSERT INTO planet_cal_sink "
                        f"SELECT g, g % 1000, repeat('x', 80) "
                        f"FROM generate_series(1,{n(rows)}) g;",
                        cleanup="TRUNCATE planet_cal_sink;"))

    # ... and writes with almost no WAL (unlogged). This pair is what lets NNLS
    # tell beta_w and beta_wal apart.
    for rows in (500_000, 2_000_000, 6_000_000):
        W.append(Sample("insert_unlogged", n(rows),
                        f"INSERT INTO planet_cal_sink_u "
                        f"SELECT g, g % 1000, repeat('x', 80) "
                        f"FROM generate_series(1,{n(rows)}) g;",
                        cleanup="TRUNCATE planet_cal_sink_u;"))

    # WAL with almost no backend writes: the small table stays in shared
    # buffers, so its dirty pages are the checkpointer's problem, not ours.
    for frac in (0.25, 0.5, 1.0):
        rows = int(small_rows * frac)
        W.append(Sample("update_wal", rows,
                        f"UPDATE planet_cal_small SET v = v + 1 "
                        f"WHERE id <= {rows};",
                        cleanup="VACUUM (ANALYZE) planet_cal_small;"))
    return W


SESSION_SQL = """
SET client_min_messages = warning;
SET max_parallel_workers_per_gather = 0;
SET jit = off;
SET planet.report = off;
SET planet.enabled = on;
"""


def setup_tables(db, scale, small_rows):
    log(f"building calibration tables (scale={scale:,})")
    db.run(f"""
DROP TABLE IF EXISTS planet_cal_big, planet_cal_small,
                     planet_cal_sink, planet_cal_sink_u;

CREATE TABLE planet_cal_big AS
  SELECT g AS id, (g % 1000) AS dim_id, (random() * 1000)::float8 AS v,
         md5(g::text) AS pad
  FROM generate_series(1,{scale}) g;

CREATE TABLE planet_cal_small AS
  SELECT g AS id, (random() * 1000)::float8 AS v
  FROM generate_series(1,{small_rows}) g;

CREATE TABLE planet_cal_sink   (id bigint, dim_id int, pad text);
CREATE UNLOGGED TABLE planet_cal_sink_u (id bigint, dim_id int, pad text);

ALTER TABLE planet_cal_big    SET (autovacuum_enabled = off);
ALTER TABLE planet_cal_small  SET (autovacuum_enabled = off, fillfactor = 70);
ALTER TABLE planet_cal_sink   SET (autovacuum_enabled = off);
ALTER TABLE planet_cal_sink_u SET (autovacuum_enabled = off);

VACUUM (ANALYZE) planet_cal_big;
VACUUM (ANALYZE) planet_cal_small;
""")
    size = db.scalar("SELECT pg_size_pretty(pg_total_relation_size('planet_cal_big'));")
    sb = db.scalar("SHOW shared_buffers;")
    log(f"planet_cal_big = {size} (shared_buffers = {sb})")


def interp(x, xs, ys):
    """Linear interpolation, clamped at the ends."""
    if len(xs) == 1 or x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            f = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
            return ys[i - 1] + f * (ys[i] - ys[i - 1])
    return ys[-1]


def measure(db, meter, s, settle):
    """One energy window around one query. Returns a raw measurement dict."""
    db.run("CHECKPOINT;")
    if s.setup:
        db.run(s.setup)
    time.sleep(settle)

    t0, e0 = meter.sample()
    db.run(s.query)
    t1, e1 = meter.sample()

    # Exactly one planet_last() call: it is itself a top-level query, so it
    # overwrites PLANET's state on the way out. A second call would report
    # the counters of the first.
    row = db.scalar("SELECT cpu_seconds, blks_read, blks_written, wal_bytes "
                    "FROM planet_last();")
    if s.cleanup:
        db.run(s.cleanup)

    parts = row.split("|")
    if len(parts) != 4 or any(p == "" for p in parts):
        raise RuntimeError(f"planet_last() returned NULLs ({row!r}) -- is the "
                           "extension loaded in this session?")
    cpu, reads, writes, wal = parts
    return {"cpu_seconds": float(cpu), "blks_read": int(reads),
            "blks_written": int(writes), "wal_bytes": int(wal),
            "wall_seconds": t1 - t0, "energy_joules_raw": e1 - e0,
            "t_mid": (t0 + t1) / 2}


def log(msg):
    print(f"[sweep] {msg}", file=sys.stderr, flush=True)


def host_meta(db, meter, args):
    cpu = ""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    gucs = ["server_version", "shared_buffers", "work_mem", "max_wal_size",
            "synchronous_commit", "full_page_writes", "wal_compression",
            "max_parallel_workers_per_gather", "checkpoint_timeout"]
    settings = {g: db.scalar(f"SHOW {g};") for g in gucs}
    return {"cpu_model": cpu, "cores": os.cpu_count(),
            "kernel": platform.release(), "rapl_domain": meter.label,
            "postgres": settings, "args": vars(args),
            "collected_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default="sweep.csv", help="output CSV")
    ap.add_argument("-d", "--dbname", help="database (else PGDATABASE)")
    ap.add_argument("--psql", default=os.environ.get("PSQL", "psql"))
    ap.add_argument("--domain", default="auto",
                    choices=["auto", "psys", "package+dram"],
                    help="RAPL ground-truth domain (default: psys if present)")
    ap.add_argument("--scale", type=int, default=20_000_000,
                    help="rows in planet_cal_big; must exceed shared_buffers")
    ap.add_argument("--small-rows", type=int, default=200_000)
    ap.add_argument("--reps", type=int, default=3, help="repetitions per config")
    ap.add_argument("--settle", type=float, default=1.0,
                    help="idle seconds before each energy window")
    ap.add_argument("--idle-seconds", type=float, default=10.0)
    ap.add_argument("--idle-every", type=int, default=8,
                    help="re-measure idle power every N samples (drift)")
    ap.add_argument("--pre-idle-settle", type=float, default=30.0,
                    help="quiesce this long after building tables, before the "
                         "first idle window (writeback + thermal settle)")
    ap.add_argument("--quick", action="store_true", help="smaller knobs, ~5 min")
    ap.add_argument("--skip-setup", action="store_true", help="reuse existing tables")
    ap.add_argument("--no-idle-subtract", action="store_true",
                    help="emit raw platform energy (diagnostic; breaks Eq. 2)")
    ap.add_argument("--seed", type=int, default=20260804)
    args = ap.parse_args()

    if args.quick:
        args.scale = min(args.scale, 5_000_000)
        args.reps = min(args.reps, 2)
        args.idle_seconds = min(args.idle_seconds, 4.0)

    try:
        meter = rapl.open_meter(args.domain)
        rapl.check(meter)
    except rapl.RaplError as e:
        sys.exit(f"collect_sweep: {e}")
    log(f"ground truth: RAPL `{meter.label}`")

    db = Psql(args.psql, args.dbname)
    try:
        db.run("CREATE EXTENSION IF NOT EXISTS planet;")
        db.run("LOAD 'planet';")          # installs the executor hooks now
        db.run(SESSION_SQL)
        if not args.skip_setup:
            setup_tables(db, args.scale, args.small_rows)

        samples = [(s, r) for s in workload(args.small_rows, args.quick)
                   for r in range(1, args.reps + 1)]
        # Shuffle so thermal drift cannot masquerade as a family effect.
        random.Random(args.seed).shuffle(samples)

        # The first idle window anchors the idle interpolation for every early
        # sample, so it must measure a *settled* machine. Taken immediately after
        # setup_tables() it does not: the checkpointer and the kernel are still
        # flushing several hundred MB of dirty pages, and the CPU is shedding the
        # heat of building the table. On the shakedown laptop that read 3.29 W
        # against a settled 2.42 W -- a 26% overestimate, subtracted from the
        # dynamic energy of every query early in the sweep. Quiesce first.
        db.run("CHECKPOINT;")
        if args.pre_idle_settle > 0:
            log(f"quiescing {args.pre_idle_settle:.0f}s before the first idle window")
            time.sleep(args.pre_idle_settle)

        idles = [rapl.measure_idle(meter, args.idle_seconds)]
        log(f"idle: {idles[0].watts:.2f} W (sd {idles[0].std:.2f} W)")

        rows, warnings = [], 0
        for i, (s, rep) in enumerate(samples, 1):
            if i > 1 and (i - 1) % args.idle_every == 0:
                idles.append(rapl.measure_idle(meter, args.idle_seconds))

            m = measure(db, meter, s, args.settle)
            idle_w = interp(m["t_mid"], [k.t_mid for k in idles],
                            [k.watts for k in idles])
            idle_sd = interp(m["t_mid"], [k.t_mid for k in idles],
                             [k.std for k in idles])
            idle_j = idle_w * m["wall_seconds"]
            dyn = m["energy_joules_raw"] - idle_j

            # How far above the machine's own background is this query?
            floor = max(idle_sd * m["wall_seconds"], 1e-9)
            noise_ratio = dyn / floor
            if noise_ratio < 3.0:
                warnings += 1
                log(f"  ! {s.label} rep{rep}: dynamic energy only "
                    f"{noise_ratio:.1f}x the idle noise floor")

            rows.append({
                **{k: m[k] for k in ("cpu_seconds", "blks_read", "blks_written",
                                     "wal_bytes", "wall_seconds",
                                     "energy_joules_raw")},
                "energy_joules": m["energy_joules_raw"] if args.no_idle_subtract else dyn,
                "sample": i, "family": s.family, "label": s.label,
                "knob": s.knob, "rep": rep,
                "idle_watts": round(idle_w, 4), "idle_joules": round(idle_j, 4),
                "noise_ratio": round(noise_ratio, 2), "domain": meter.label,
                # Wall-clock centre of the measurement window. Lets a second,
                # concurrently logged meter (e.g. RAPL next to the wall meter)
                # be sliced per sample after the fact: [t_mid - wall/2, + wall/2].
                "t_mid": round(m["t_mid"], 3),
            })
            log(f"{i:3d}/{len(samples)} {s.label:<24} "
                f"{m['wall_seconds']:6.2f}s  raw={m['energy_joules_raw']:8.2f}J  "
                f"dyn={dyn:8.2f}J  cpu={m['cpu_seconds']:5.2f}s "
                f"r={m['blks_read']:>9,} w={m['blks_written']:>8,} "
                f"wal={m['wal_bytes']:>11,}B")

        idles.append(rapl.measure_idle(meter, args.idle_seconds))
        drift = abs(idles[-1].watts - idles[0].watts) / max(idles[0].watts, 1e-9)
        log(f"idle drift over the sweep: {drift*100:.1f}% "
            f"({idles[0].watts:.2f} -> {idles[-1].watts:.2f} W)")
        if drift > 0.10:
            falling = idles[-1].watts < idles[0].watts
            log("  ! >10% drift: the machine was not thermally settled or was "
                "not idle.")
            log("    idle FELL over the sweep, so the FIRST window was the bad "
                "one: it caught post-setup writeback or heat, and early samples "
                f"had too much idle subtracted. Raise --pre-idle-settle (now "
                f"{args.pre_idle_settle:.0f}s) and re-run."
                if falling else
                "    idle ROSE over the sweep: the machine heated up or "
                "something else started running. Cool it, close everything, "
                "raise --settle, and re-run.")
    finally:
        db.close()
        meter.close()

    rows.sort(key=lambda r: r["sample"])
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    meta_path = re.sub(r"\.csv$", "", args.out) + ".meta.json"
    db2 = Psql(args.psql, args.dbname)
    try:
        meta = host_meta(db2, meter, args)
    finally:
        db2.close()
    meta["idle_watts"] = [{"t": k.t_mid, "watts": k.watts, "std": k.std} for k in idles]
    meta["n_rows"] = len(rows)
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    log(f"wrote {args.out} ({len(rows)} rows) and {meta_path}")
    if warnings:
        log(f"{warnings} sample(s) close to the noise floor -- "
            "validate_model.py will report their leverage")
    log("next: python3 fit_model.py "
        f"{args.out} -o ../config/coefficients.json && "
        f"python3 validate_model.py {args.out}")


if __name__ == "__main__":
    main()
