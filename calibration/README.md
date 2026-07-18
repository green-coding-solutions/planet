# Calibration (offline, once per platform)

PLANET's run-time model is a linear map from executor counters to joules:

```
E_joules = cpu_active_watts    * cpu_seconds
         + joules_per_read     * blks_read
         + joules_per_write    * blks_written
         + joules_per_wal_byte * wal_bytes
```

Its four coefficients are fit **once**, offline, against a ground-truth energy
signal. This is the only place RAPL or a power meter is used. At run time the
extension reads no hardware counter, which is why it runs unchanged in cloud VMs
where those counters are unavailable.

Until you do this, PLANET uses built-in defaults from one particular machine.
Those are fine for **ranking** two plans on the same host and meaningless as
absolute joules.

| script | what |
| --- | --- |
| [`rapl.py`](rapl.py) | read PSYS / package+DRAM energy from `powercap` sysfs |
| [`collect_sweep.py`](collect_sweep.py) | run the workload sweep → `sweep.csv` |
| [`check_sweep.py`](check_sweep.py) | is this sweep healthy? run it after every sweep |
| [`fit_model.py`](fit_model.py) | NNLS fit → the four `planet.*` GUCs |
| [`validate_model.py`](validate_model.py) | **held-out** accuracy: the number worth quoting |
| [`gmt_meter.py`](gmt_meter.py) | alternative meter, backed by Green Metrics Tool providers |

Needs `psql` on `PATH` and, for the fit, numpy (scipy if you want NNLS).

## 0. Make RAPL readable

`energy_uj` is root-only on current kernels (CVE-2020-8694: a high-resolution
energy counter is a side channel against constant-time crypto). On a calibration
box that trade-off is fine; don't do it in production.

```sh
sudo chmod a+r /sys/class/powercap/intel-rapl:*/energy_uj
python3 rapl.py --list      # which domains exist
python3 rapl.py --check     # is the counter actually advancing?
python3 rapl.py --idle 10   # idle platform power, W
```

`--check` matters: many machines expose a `psys` domain that the firmware never
updates. If it is frozen, fall back to `--domain package+dram`.

**Why PSYS.** It meters the whole SoC power plane, cores, uncore, DRAM, and
on-package rails, as one counter, so nothing is summed and nothing is
double-counted. `package+dram` is the fallback. Neither sees a discrete NVMe
drive on its own rail, so the fitted `joules_per_read`/`joules_per_write` are
only as wide as the meter; cross-check against a wall meter before claiming a
whole-node number.

No RAPL at all (AMD without the powercap driver, a VM, an ARM board)? Use a wall
meter through `gmt_meter.py`, or accept the defaults and treat every number as a
ranking.

## 1. Collect a sweep

```sh
# on the DATABASE HOST, as a superuser, on an otherwise idle machine
createdb calib
python3 collect_sweep.py -d calib -o sweep.csv     # ~25 min
python3 collect_sweep.py -d calib --quick          # ~5 min smoke run
python3 check_sweep.py sweep.csv                   # PASS / FAIL, one line
```

One row per measured query: the executor counters **and** the measured energy.

| column | source |
| --- | --- |
| `cpu_seconds`, `blks_read`, `blks_written`, `wal_bytes` | `SELECT * FROM planet_last()` (same session) |
| `energy_joules` | **ground truth**: PSYS integrated over the query, minus idle |

Two things make the CSV fit-able, and both are easy to get wrong by hand:

**Idle subtraction.** The model has no intercept: it describes the *dynamic*
increment above idle. So the ground truth must be dynamic too:
`energy_joules = E_psys(window) − P_idle · wall_seconds`. `collect_sweep.py`
re-measures `P_idle` every few samples and interpolates it in time, which also
absorbs thermal drift; it warns if drift exceeds 10%, or if a query's dynamic
energy fails to clear the idle noise floor.

**Counter decorrelation.** NNLS on collinear columns returns coefficients that
fit well and mean nothing. The sweep therefore drives each counter through a
regime where the others are quiet, in particular an **unlogged** insert (writes,
almost no WAL) against a **cached** update (WAL, almost no backend writes). That
pair is what lets the fit tell `joules_per_write` and `joules_per_wal_byte`
apart; an insert-only sweep cannot, and nothing in the fit output would tell you.

The script also writes `sweep.meta.json` (CPU model, kernel, `shared_buffers`,
`synchronous_commit`, idle-power trace). Keep it: it is the provenance of the
coefficients you are about to trust.

Ground-truth energy **captures all cores**, including parallel workers, unlike
PLANET's leader-only `getrusage` (see the extension README). The sweep pins
`max_parallel_workers_per_gather = 0` for exactly this reason: a parallel sweep
would inflate `cpu_active_watts` to absorb the workers' invisible CPU.

## 2. Fit

```sh
python3 fit_model.py sweep.csv -o ../config/coefficients.json
```

It prints the `SET planet.*` statements and writes the JSON that
`docker/apply-coefficients.py` consumes. Applying them:

```sh
cd ../docker && make coefficients        # containerised server
# or, on a native install, paste the ALTER SYSTEM lines and pg_reload_conf()
```

## 3. Check the accuracy honestly

`fit_model.py` reports `R^2`/MAPE **on the rows it fit**. That is an upper bound
on fidelity, not a measurement of it. Use:

```sh
python3 validate_model.py sweep.csv -o fidelity.pdf
```

which reports, on queries the model never saw:

- **k-fold cross-validated MAPE and `R^2`**, with a bootstrap 95% CI;
- **leave-one-family-out**: train on every workload shape but one, predict that
  one. Always worse than k-fold, and the number to look at if you care whether
  the model generalizes to workloads outside the sweep;
- the **condition number** of the design matrix, and each term's share of
  predicted energy. A high condition number is the *only* warning that the
  coefficients are arbitrary even though the fit looks excellent.

`--tex` writes the same numbers as LaTeX `\newcommand` macros, if you are
putting them in a document.

## What is *not* fit here

`grid_gco2_per_kwh` (from your grid, or a marginal-intensity feed such as
Electricity Maps or WattTime), `embodied_gco2_per_byte` and `idle_watts_per_byte`
(from the storage device datasheet) are inputs, not regression outputs. See
[`../config/coefficients.example.json`](../config/coefficients.example.json).
