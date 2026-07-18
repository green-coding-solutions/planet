# PLANET in Docker

PostgreSQL 18.4 with the `planet` extension compiled in, installed, and (if you
have calibrated) your coefficients applied. This is the fastest way to try
PLANET without a build toolchain.

## Quick start

```sh
make up        # build the image, start the server, print active coefficients
make psql      # a psql shell on it
make down      # stop   (make clean also drops the data volume)
```

The server listens on `127.0.0.1:5432` with the development password `planet`.
Change it before doing anything real:

```sh
echo 'POSTGRES_PASSWORD=something-better' > .env
make clean up          # clean, because the password is baked in at initdb time
```

Other `.env` knobs: `POSTGRES_DB` (default `planet`), `POSTGRES_USER`
(`postgres`), `PGPORT` (`5432`), `PLANET_VOLUME`
(`planet-pg_planet-pgdata`).

### Where your data lives

The compose project is `planet-pg` and the data volume is
`planet-pg_planet-pgdata`. Both names are deliberately specific rather than a
generic `planet`/`pgdata`, because compose volumes are global to the Docker
daemon: two projects that pick the same name silently share one data directory.
When that happens the server finds an initialized `PGDATA`, skips `initdb.d`
altogether, and comes up with the extension not installed and unrelated
databases mounted, and a later `make clean` deletes them.

To run more than one instance from this checkout, give each its own volume and
port:

```sh
printf 'PLANET_VOLUME=planet-pg_scratch\nPGPORT=55432\n' > .env
```

`make clean` destroys the volume named in the current `.env`. `make down` never
touches it.

## Layout

| file | purpose |
| --- | --- |
| `Dockerfile` | builds `planet.so` against PG18 headers, then a runtime image |
| `compose.yaml` | the `db` service, published on loopback |
| `planet.conf` | server settings, bind-mounted and `include_if_exists`'d |
| `initdb.d/` | first boot: wire up the include, `CREATE EXTENSION`, apply coefficients |
| `apply-coefficients.py` | `coefficients.json` → `ALTER SYSTEM`; also on `$PATH` as `planet-apply-coefficients` |

Changing `extension/` or the `Dockerfile` needs `make build`. Changing
`planet.conf` needs only `docker compose restart db`.

## The calibration handoff

The split is deliberate:

| | where | why |
| --- | --- | --- |
| **calibration** | the **host** | needs an energy counter (RAPL), which Docker masks by default |
| **the server** | the **container** | pinned version, config, and extension build |

The only thing that crosses the boundary is `config/coefficients.json`, a
handful of fitted numbers. Calibrate against the machine you will run on:

```sh
sudo chmod a+r /sys/class/powercap/intel-rapl:*/energy_uj
cd ../calibration
python3 collect_sweep.py -d planet -o sweep.csv
python3 fit_model.py sweep.csv -o ../config/coefficients.json
```

`../config` is bind-mounted at `/etc/planet`, so the file is visible to the
container immediately. Apply it without a restart:

```sh
make coefficients      # ALTER SYSTEM + pg_reload_conf()
```

It is also applied automatically on first boot. `make up` prints the
coefficients in force; if none were found, the server logs that carbon numbers
are **uncalibrated** and only meaningful as rankings.

`apply-coefficients.py` accepts either `fit_model.py`'s `{"gucs": {...}}` or the
sectioned shape of `config/coefficients.example.json`, and rejects unknown or
non-numeric `planet.*` keys rather than letting them become silent no-ops.

### Calibrating inside the container instead

If the host has no `psql`/`scipy`, the scripts are in the image at
`/opt/planet/calibration`. They still need the energy counter, so on the host:

```sh
sudo chmod a+r /sys/class/powercap/intel-rapl:*/energy_uj
```

and uncomment in `compose.yaml`:

```yaml
    security_opt:
      - systempaths=unconfined
```

Docker masks `/sys/devices/virtual/powercap` by default: it is runc's mitigation
for CVE-2020-8694 ("PLATYPUS"), because a high-resolution energy counter is a
side channel against constant-time crypto. Do not unmask it on a shared host.
RAPL meters the physical package regardless of who is containerized, so the
readings are correct either way.

## Why nothing preloads `planet`

Neither `shared_preload_libraries` nor `session_preload_libraries` names
`planet` here. Sessions that want it run `LOAD 'planet';` themselves. Preloading
is convenient and costs you the ability to compare against a hooks-absent
baseline in the same cluster, which is the only way to measure PLANET's own
overhead. If you want it anyway, `planet.conf` has a commented
`session_preload_libraries` line: prefer that over `shared_preload_libraries`,
which no reload can undo.

(If you set it via `ALTER SYSTEM`, use `RESET` rather than `SET = ''` to undo:
`session_preload_libraries` is list-valued, so `ALTER SYSTEM` stores the empty
string as `'""'`, a one-element list holding an empty filename. Every backend
started afterwards then dies with `FATAL: could not access file ""`, including
the one you would use to fix it.)

## Notes on the container as a measurement instrument

- **`shm_size: 2gb`.** Parallel workers put their tuple queues in `/dev/shm`.
  Docker's 64 MB default makes multi-worker plans fail with `could not resize
  shared memory segment`, which reads like a planner outcome and is not.
- **`planet.conf` is not the command line.** Postmaster command-line options
  outrank `ALTER SYSTEM`. Settings live in an included file so that
  `postgresql.auto.conf`, and therefore your calibrated coefficients, still win.
- The container shares the host's CPU, thermals, and page cache. If you are
  measuring, run nothing else on the box and record `docker inspect` limits
  alongside your numbers.
