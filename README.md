# PLANET

**Per-query carbon accounting inside PostgreSQL.**

PLANET is a PostgreSQL 18 extension that reports the operational energy, and
through your grid's carbon intensity the operational carbon, of every SQL
statement. It does this from counters the engine already maintains, so there is
no hardware energy counter read at run time and nothing to install outside the
database:

```
planet=# LOAD 'planet';
planet=# SET planet.report = on;
planet=# SELECT count(*) FROM orders WHERE placed_at > now() - interval '7 days';
INFO:  PLANET carbon=7.13922e-05 gCO2e (compute=4.917e-05 awake=0 io=2.22222e-05 wal=0)
       cpu=0.029502s wall=0.0181736s blks_read=2500 blks_written=0 wal=0B
 count
-------
 70054
```

The estimate comes from a five-coefficient linear model over `BufferUsage`,
`WalUsage`, and per-backend CPU and wall time. The coefficients are fit **once
per machine**, offline, against a real energy meter (see
[calibration/](calibration/)). At run time PLANET only diffs counters it already
has, which is why it works unchanged inside a cloud VM where RAPL is
unavailable.

> **Status: research prototype.** It is useful today for *ranking*: comparing two
> plans, two settings, or two schema choices on the same machine. Treat the
> absolute gCO2e as an estimate with a calibration behind it, not as a
> measurement. See [Limitations](#limitations).

## Quick start (Docker)

The fastest path, no build toolchain required:

```sh
git clone https://github.com/green-coding-solutions/planet
cd planet/docker
make up          # PostgreSQL 18.4 on 127.0.0.1:5432, extension installed
make psql
```

See [docker/README.md](docker/README.md) for passwords, ports, and how to feed
in your calibrated coefficients.

## Quick start (existing PostgreSQL)

Requires **PostgreSQL 18** and its server headers. The build fails loudly on
anything older: `ExecutorRun_hook` lost its `execute_once` argument in 18, and
`planet.c` `#error`s on older headers.

```sh
cd extension
make PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config
sudo make install PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config
```

Then, in a database:

```sql
CREATE EXTENSION planet;
```

## Using it

**Start every session with `LOAD 'planet';`.**

```sql
LOAD 'planet';                 -- installs the hooks, defines the planet.* GUCs
SET planet.report = on;        -- print an INFO line after every top-level query
```

The `LOAD` is not optional in practice. The extension's functions do dlopen the
module on first call, but the executor hooks only exist from that moment on, so
without a `LOAD` the *first* `planet_last()` in a session returns NULLs and the
query you actually wanted to measure has already gone unrecorded. `SET
planet.report` needs it too: the `planet.*` GUCs do not exist until the module is
in your session.

(`planet_table_carbon()` is the exception. It reads the coefficients as plain
settings and is correct in any session, `LOAD` or not.)

To have every session start that way, uncomment `session_preload_libraries` in
[docker/planet.conf](docker/planet.conf) or set it in your own
`postgresql.conf`. Prefer it over `shared_preload_libraries`, which no reload can
undo.

### The footprint of the last query, as a row

```sql
SELECT count(*) FROM orders;
SELECT * FROM planet_last();
```

```
  carbon_g  | compute_g |   io_g   | wal_g | awake_g | energy_j | cpu_seconds | wall_seconds | blks_read | blks_written | wal_bytes
------------+-----------+----------+-------+---------+----------+-------------+--------------+-----------+--------------+-----------
 7.1392e-05 | 4.917e-05 | 2.22e-05 |     0 |       0 |  0.64253 |    0.029502 |   0.01817361 |      2500 |            0 |         0
```

Scalar getters exist too (`planet_last_carbon_g()`, `planet_last_energy_joules()`,
`planet_last_cpu_seconds()`, and so on), and `planet_reset()` clears the state.

### Comparing two plans

This is what PLANET is best at. Run the same query two ways and compare:

```sql
LOAD 'planet';

SET enable_seqscan = off;
SELECT count(*) FROM orders WHERE customer_id = 42;
SELECT 'index' AS plan, carbon_g, energy_j, blks_read FROM planet_last();

SET enable_seqscan = on;
SET enable_indexscan = off;
SELECT count(*) FROM orders WHERE customer_id = 42;
SELECT 'seqscan' AS plan, carbon_g, energy_j, blks_read FROM planet_last();

RESET enable_indexscan;
```

Run each side a few times and alternate them: a cold cache on the first run will
dominate anything the model has to say. The comparison is only meaningful with
the same coefficients on the same machine.

### Logging every statement's footprint

`planet_last()` reads whatever the previous top-level statement left behind, so
the pattern is: run the statement, then capture.

```sql
CREATE TABLE query_carbon (
    at        timestamptz DEFAULT now(),
    label     text,
    carbon_g  double precision,
    energy_j  double precision,
    cpu_s     double precision,
    blks_read bigint
);

-- the statement you care about
SELECT count(*) FROM orders o JOIN customers c USING (customer_id);

INSERT INTO query_carbon (label, carbon_g, energy_j, cpu_s, blks_read)
SELECT 'orders join customers', carbon_g, energy_j, cpu_seconds, blks_read
FROM planet_last();
```

Then the obvious question becomes a query:

```sql
SELECT label,
       count(*)                         AS runs,
       round(sum(carbon_g)::numeric, 6) AS total_gco2e,
       round(avg(carbon_g)::numeric, 6) AS avg_gco2e
FROM query_carbon
GROUP BY label
ORDER BY total_gco2e DESC;
```

### The carbon a table *is*, not the carbon a query *costs*

`planet_last()` reports a **flow**: the marginal energy a query caused.
`planet_table_carbon()` reports a **stock**: the embodied carbon of the storage
the table occupies, plus the retention carbon it has accrued over its lifetime.

```sql
-- embodied only
SELECT * FROM planet_table_carbon('orders');

-- embodied + 90 days of retention
SELECT * FROM planet_table_carbon('orders', 90 * 86400);
```

```
  bytes   | embodied_g | retention_g |  stock_g
----------+------------+-------------+------------
 29384704 | 0.44077056 |  0.06347096 | 0.50424152
```

Rank your schema by it:

```sql
SELECT c.relname, (planet_table_carbon(c.oid, 365 * 86400)).*
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r' AND n.nspname = 'public'
ORDER BY stock_g DESC
LIMIT 10;
```

The two numbers are deliberately never summed. Reading data never changes the
stock; only writes, age, and derivation do ("reads meter, writes mint").

### Point it at your actual grid

`grid_gco2_per_kwh` is a plain GUC, so a marginal-intensity feed (Electricity
Maps, WattTime, your national grid operator) can be pushed in live:

```sql
SET planet.grid_gco2_per_kwh = 312;                 -- this session
ALTER SYSTEM SET planet.grid_gco2_per_kwh = 312;    -- cluster-wide
SELECT pg_reload_conf();
```

## Calibrating for your machine

Out of the box PLANET uses coefficients fit on one particular machine. They are
enough to rank plans; they are not your hardware. To fit your own, on an
otherwise idle host:

```sh
sudo chmod a+r /sys/class/powercap/intel-rapl:*/energy_uj
cd calibration
python3 collect_sweep.py -d yourdb -o sweep.csv    # ~25 min
python3 check_sweep.py sweep.csv                   # PASS / FAIL
python3 fit_model.py sweep.csv -o ../config/coefficients.json
python3 validate_model.py sweep.csv                # held-out accuracy
```

`fit_model.py` prints the `ALTER SYSTEM SET` statements to apply. On the
containerised server, `cd docker && make coefficients` does it for you.

Full detail, including what to do when RAPL is unavailable and why the sweep is
shaped the way it is: [calibration/README.md](calibration/README.md).

## Configuration

All settings are `planet.*` GUCs. Five of them are calibration outputs; the rest
are inputs you supply.

| GUC | default | meaning |
| --- | --- | --- |
| `planet.enabled` | `on` | master switch (superuser) |
| `planet.report` | `off` | emit an `INFO` line per top-level query |
| `planet.cpu_active_watts` | `15.0` | dynamic power per busy core, W *(fit)* |
| `planet.awake_watts` | `0.0` | load-independent power while a query runs, W *(fit)* |
| `planet.joules_per_read` | `8e-5` | energy per block read, J *(fit)* |
| `planet.joules_per_write` | `1.6e-4` | energy per block written, J *(fit)* |
| `planet.joules_per_wal_byte` | `2e-8` | energy per WAL byte, J *(fit)* |
| `planet.grid_gco2_per_kwh` | `400.0` | grid carbon intensity |
| `planet.embodied_gco2_per_byte` | `1.5e-8` | embodied storage carbon, from the device datasheet |
| `planet.idle_watts_per_byte` | `2.5e-12` | idle storage power, from the device datasheet |

See [config/coefficients.example.json](config/coefficients.example.json) for
where each number comes from.

## Limitations

- **Top-level statements only.** Nested queries fold into their caller.
- **Per-query, not per-node.** Per-operator carbon and a native
  `EXPLAIN (PLANET)` option are TODO; PG18 ships the hook they need
  (`RegisterExtensionExplainOption()`).
- **Compute energy is a calibrated estimate, not a measurement.** The storage
  terms are near-exact in bytes. Claims are about rankings, not absolute joules.
- **CPU time is per backend** (`getrusage(RUSAGE_SELF)`): accurate for one query
  per session, approximate under heavy intra-backend concurrency.
- **Parallel workers are undercounted.** Worker CPU is invisible to the leader's
  `getrusage`, so parallel plans under-report compute. Block I/O and WAL are
  fine, since PostgreSQL aggregates those into the leader. With
  `planet.report = on` an N-worker query also prints N+1 `INFO` lines; only the
  leader's is a whole-query figure.
- **Coefficients do not transfer between machines.** Recalibrate per platform.

## Layout

| directory | what |
| --- | --- |
| [extension/](extension/) | the C extension and its SQL interface |
| [calibration/](calibration/) | offline coefficient fitting, and how to check it |
| [config/](config/) | annotated example coefficients |
| [docker/](docker/) | PostgreSQL 18.4 with the extension built in |

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).

Built by [Green Coding Solutions](https://www.green-coding.io).
