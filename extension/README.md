# PLANET PostgreSQL extension

Per-query carbon accounting inside PostgreSQL. `planet` installs the executor
hooks, turns the counters the engine already maintains (`BufferUsage`,
`WalUsage`, per-backend CPU time) into an energy/carbon estimate with a
calibrated linear model, and exposes the result per query.

**No RAPL at run time.** RAPL (and a wall-power meter) are used only *offline*,
to fit the coefficients that live in the `planet.*` GUCs — see
[`../calibration`](../calibration). At run time PLANET only diffs counters it
already has.

## Build & install

Requires **PostgreSQL 18**; the build fails loudly on anything older.

```sh
make PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config
make install PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config
```

Or skip the toolchain and use the container, which pins 18.4:
[`../docker`](../docker).

Then, in a database:

```sql
CREATE EXTENSION planet;
LOAD 'planet';                   -- defines the planet.* GUCs in this session
SET planet.report = on;          -- print carbon after every query
SELECT count(*) FROM pg_class;   -- INFO: PLANET carbon=... gCO2e (...)
SELECT * FROM planet_last();     -- same numbers, as a row (for scripting)
```

The `LOAD` is not optional in practice. `planet_last()` does dlopen the module on
first call, but the executor hooks only exist from that call onward: without a
`LOAD` the first `planet_last()` in a session returns NULLs, and the query it was
meant to describe was never observed. `SET planet.report` needs the `LOAD` too,
since the GUC does not exist until the module is in the session.

`planet_table_carbon()` is the exception: it reads the coefficients through
`current_setting(..., missing_ok)`, which resolves `ALTER SYSTEM`-set values even
in a session that never loaded the module.

**Think before putting `planet` in `shared_preload_libraries`.** It is
convenient, and no `pg_reload_conf()` can unload a library the postmaster
preloaded, so it also removes your ability to compare against a hooks-absent
baseline in the same cluster. If you want it everywhere, prefer
`session_preload_libraries`, which a reload can undo.

## Interface

| Object | Meaning |
| --- | --- |
| `planet.report` (GUC) | emit an `INFO` carbon line after each top-level query |
| `planet_last()` | footprint (**flow**) of the last top-level query, as one row |
| `planet_table_carbon(rel, age_seconds)` | stored **stock** (embodied + retention) of a table (Eq. 1) |
| `planet_reset()` | clear the last-query state |

`planet.*` coefficient GUCs (all tunable, all from calibration): `cpu_active_watts`,
`joules_per_read`, `joules_per_write`, `joules_per_wal_byte`, `grid_gco2_per_kwh`,
`embodied_gco2_per_byte`, `idle_watts_per_byte`.

### Flow vs. stock — "reads meter, writes mint"

`planet_last()` reports the **marginal flow** a query caused (compute + I/O +
WAL). `planet_table_carbon()` reports the **accrued stock** of stored data.
Reading data never changes the stock; only writes, age, and derivation do. The
two are never summed to form a physical total.

## Scope of this prototype

- Accounts **top-level** statements only; nested queries fold into their caller.
- Per-**query** totals. Per-**node** (per-operator) carbon and the native
  `EXPLAIN (PLANET)` option are **TODO**; PG18 ships the hook they need,
  `RegisterExtensionExplainOption()` (`commands/explain_state.h`).
- Compute energy is a **calibrated estimate**, not a measurement; storage terms
  are near-exact in bytes. Claims are about rankings, not absolute joules.
- CPU time via `getrusage(RUSAGE_SELF)` is per backend: accurate for one query
  per session, approximate under heavy intra-backend concurrency.
- **Parallel workers each run `ExecutorEnd`.** With `planet.report = on`, an
  N-worker query prints N+1 `INFO` lines: one per worker (its own leader-blind
  counters) and one for the leader. Only the leader's is a whole-query figure.
  `planet_last()` in a worker is meaningless — different process. The workers'
  `ExecutorEnd` is, however, exactly where the worker-CPU aggregation TODO
  above would hook in; suppressing the lines with `IsParallelWorker()` would be
  cosmetic, not a fix.

Tested against PostgreSQL 18.4 (Debian trixie, via [`../docker`](../docker)).
Requires 18: `ExecutorRun_hook` lost its `execute_once` argument in that
release, and `planet.c` `#error`s on older headers.
