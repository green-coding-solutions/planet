#!/usr/bin/env python3
"""
Apply calibrated PLANET coefficients to the containerised server.

This is the bridge between the two halves of the workflow: calibration runs on
the HOST (it needs RAPL, which Docker masks and which meters the physical
package regardless of where postgres runs), and the server runs in the
CONTAINER. The coefficients are the only thing that has to cross.

Accepts either shape of JSON, so both producers work unchanged:

    fit_model.py -o coefficients.json   ->  {"fit": {...}, "gucs": {"planet.x": 1.0}}
    config/coefficients.example.json    ->  {"fitted_offline": {"planet.x": 1.0}, ...}

Any `planet.*` key anywhere in the document is applied; `_comment`/`_note`
strings are ignored. Values land in postgresql.auto.conf via ALTER SYSTEM, which
is read after postgresql.conf and its includes, so the calibrated numbers win
over anything set in planet.conf.

Run at first init (docker/initdb.d/20-planet-coefficients.sh) or any time after:

    docker compose exec db planet-apply-coefficients
    docker compose exec db planet-apply-coefficients /etc/planet/other.json
"""
import json
import os
import re
import subprocess
import sys

DEFAULT_PATH = os.environ.get("PLANET_COEFFICIENTS", "/etc/planet/coefficients.json")

# Only the GUCs planet.c actually defines. A name outside this set is a typo or
# a stale key: applying it would be accepted as a placeholder and silently do
# nothing, which is the failure mode this check exists to prevent.
KNOWN = {
    "planet.cpu_active_watts",
    "planet.awake_watts",
    "planet.joules_per_read",
    "planet.joules_per_write",
    "planet.joules_per_wal_byte",
    "planet.grid_gco2_per_kwh",
    "planet.embodied_gco2_per_byte",
    "planet.idle_watts_per_byte",
}

NAME_RE = re.compile(r"^planet\.[a-z0-9_]+$")


def collect(node, out):
    """Walk the document and pick up every numeric `planet.*` leaf."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k.startswith("planet."):
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    sys.exit(f"planet: {k} is not a number: {v!r}")
                out[k] = float(v)
            else:
                collect(v, out)
    elif isinstance(node, list):
        for v in node:
            collect(v, out)
    return out


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH

    if not os.path.exists(path):
        print(f"planet: no {path}; using the extension's built-in defaults.\n"
              f"planet: carbon numbers will be UNCALIBRATED for this machine. "
              f"Fit them on the host with the scripts in calibration/, then "
              f"mount the result at {DEFAULT_PATH}.", file=sys.stderr)
        return 0

    with open(path) as f:
        doc = json.load(f)

    coeffs = collect(doc, {})
    if not coeffs:
        sys.exit(f"planet: {path} contains no planet.* keys")

    unknown = {k for k in coeffs if not NAME_RE.match(k)} | (set(coeffs) - KNOWN)
    if unknown:
        sys.exit(f"planet: unknown coefficient(s) in {path}: "
                 f"{', '.join(sorted(unknown))}")

    # Each ALTER SYSTEM is its own statement; psql autocommits them. The leading
    # LOAD makes the planet.* GUCs known to this session, without which ALTER
    # SYSTEM rejects them (the prefix is reserved by MarkGUCPrefixReserved).
    sql = ["LOAD 'planet';"]
    sql += [f"ALTER SYSTEM SET {k} = {v!r};" for k, v in sorted(coeffs.items())]
    sql.append("SELECT pg_reload_conf();")

    # stdout is discarded (it is just LOAD/ALTER SYSTEM/`t` chatter); psql
    # errors still reach stderr and ON_ERROR_STOP turns them into a non-zero
    # exit, which fails the init script rather than starting a server whose
    # coefficients silently did not apply.
    subprocess.run(
        ["psql", "-v", "ON_ERROR_STOP=1", "-q", "--no-password",
         "--username", os.environ.get("POSTGRES_USER", "postgres"),
         "--dbname", "postgres", "-f", "-"],
        input="\n".join(sql), text=True, check=True,
        stdout=subprocess.DEVNULL,
    )

    print(f"planet: applied {len(coeffs)} coefficient(s) from {path}",
          file=sys.stderr)
    for k, v in sorted(coeffs.items()):
        print(f"planet:   {k} = {v!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
