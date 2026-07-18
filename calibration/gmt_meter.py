#!/usr/bin/env python3
"""
Drop-in replacement for rapl.py backed by Green Metrics Tool metric providers.

Use case: the benchmark harness runs INSIDE a container, but the energy meters
(an MCP39F511N wall-power meter on /dev/ttyACM0, and RAPL via /dev/cpu/*/msr)
are only readable by setuid binaries on the HOST. So the host runs each
provider once, continuously, appending to a log file in a bind-mounted
directory:

    metric-provider-binary -i 99 >> $RESULTS/power/mcp.log     # µW per line
    metric-provider-binary -i 99 >> $RESULTS/power/rapl.log    # µJ per line

and this module FOLLOWS those files, integrating to cumulative joules. It
exposes the exact API of rapl.py (open_meter, check, measure_idle, Meter,
Idle, RaplError), so collect_sweep.py runs unchanged: on a GMT machine, copy
this file over calibration/rapl.py in the deployed tree.

Log formats (see green-metrics-tool metric_providers/*/source.c):

    psu/energy/ac/mcp/machine       "<epoch_µs> <power_µW>"
    psu/energy/dc/rapl/msr/machine  "<epoch_µs> <energy_µJ_per_interval> <domain>"

The MCP line is instantaneous power averaged over the chip's accumulation
window; following GMT's provider.py, sample i's power is charged over the
interval (t_{i-1}, t_i]. The RAPL line is already energy per interval, so it
sums directly; multiple packages (Package_0, Package_1, ...) are summed,
PSYS_* lines are ignored (frozen/zero on machines that do not implement it).

Domain selection keeps rapl.py's CLI values working:

    auto | psys | mcp | wall   -> the wall meter log      (label mcp_ac_wall)
    package+dram | rapl        -> the RAPL MSR log        (label rapl_msr_pkg)

Timestamps returned by sample() are epoch SECONDS taken from the meter's own
lines, so windows recorded by one meter can be sliced out of the other
meter's log afterwards (collect_sweep.py stores t_mid per sample for this).

Environment:
    GMT_POWER_DIR   directory holding the logs (default /results/power)
    GMT_MCP_LOG     override wall log path
    GMT_RAPL_LOG    override RAPL log path
"""
import os
import statistics
import sys
import time

POWER_DIR = os.environ.get("GMT_POWER_DIR", "/results/power")

# How long sample() may wait for the logger to produce a line newer than the
# call instant. Generous vs the 99 ms provider interval; if it trips, the
# logger has died and continuing would silently truncate every window.
FRESH_TIMEOUT = 3.0


class RaplError(RuntimeError):
    pass


class Idle:
    """An idle-power estimate anchored at a point in time."""

    def __init__(self, t_mid, watts, std, n):
        self.t_mid, self.watts, self.std, self.n = t_mid, watts, std, n

    def __repr__(self):
        return f"Idle({self.watts:.3f} +- {self.std:.3f} W, n={self.n})"


class _LogMeter:
    """Follows one provider log, integrating to cumulative joules."""

    def __init__(self, path, kind, label):
        self.path, self.kind, self.label = path, kind, label
        try:
            self._f = open(path)
        except OSError as e:
            raise RaplError(
                f"cannot open meter log {path}: {e}\n"
                "Is the provider running on the host? e.g.\n"
                "  setsid nohup <provider>/metric-provider-binary -i 99 "
                f">> {path} &") from e
        self._last_ts = None          # µs, last line integrated
        self._joules = 0.0
        self._buf = ""

    def _ingest(self):
        """Consume whatever the logger has appended since the last call."""
        chunk = self._f.read()
        if not chunk:
            return
        self._buf += chunk
        lines = self._buf.split("\n")
        self._buf = lines.pop()               # tail may be a partial line
        for line in lines:
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                ts = int(parts[0])
                val = int(parts[1])
            except ValueError:
                continue
            if self.kind == "mcp":
                # power µW, charged over the preceding interval (GMT rule).
                # First line after attach has no interval; it only anchors t.
                if self._last_ts is not None and ts > self._last_ts:
                    self._joules += val * (ts - self._last_ts) / 1e12
                self._last_ts = ts
            else:                              # rapl: energy µJ per interval
                if len(parts) >= 3 and not parts[2].startswith("Package"):
                    continue                   # ignore PSYS_*/DRAM-less noise
                if self._last_ts is not None:
                    self._joules += val / 1e6
                self._last_ts = ts

    def sample(self):
        """(epoch_seconds, cumulative_joules), fresh to the call instant.

        Blocks (well under FRESH_TIMEOUT) until the log contains a line
        stamped at or after the moment sample() was called, so a window
        closed by this call fully covers the work that preceded it. Without
        this, the final <=99 ms of every query would land in the next window.
        """
        target_us = time.time() * 1e6
        deadline = time.monotonic() + FRESH_TIMEOUT
        while True:
            self._ingest()
            if self._last_ts is not None and self._last_ts >= target_us:
                return self._last_ts / 1e6, self._joules
            if time.monotonic() > deadline:
                raise RaplError(
                    f"meter log {self.path} stopped advancing "
                    f"(last stamp {self._last_ts}); logger dead?")
            time.sleep(0.02)

    def close(self):
        self._f.close()


def _mcp_path():
    return os.environ.get("GMT_MCP_LOG", os.path.join(POWER_DIR, "mcp.log"))


def _rapl_path():
    return os.environ.get("GMT_RAPL_LOG", os.path.join(POWER_DIR, "rapl.log"))


def open_meter(domain="auto"):
    """Map rapl.py's domain names onto the two GMT logs."""
    if domain in ("auto", "psys", "mcp", "wall"):
        return _LogMeter(_mcp_path(), "mcp", "mcp_ac_wall")
    if domain in ("package+dram", "rapl", "package"):
        return _LogMeter(_rapl_path(), "rapl", "rapl_msr_pkg")
    raise RaplError(f"unknown domain {domain!r}")


def check(meter, seconds=0.4):
    """Fail loudly if the log is present but not advancing / all zero."""
    _, e0 = meter.sample()
    time.sleep(max(seconds, 0.25))
    _, e1 = meter.sample()
    if e1 - e0 <= 0:
        raise RaplError(f"meter `{meter.label}` did not accumulate energy "
                        f"over {seconds}s -- provider running? machine on?")
    return e1 - e0


def measure_idle(meter, seconds, interval=0.5):
    """Mean power over a quiet window, with sub-window spread (cf. rapl.py)."""
    t0, e0 = meter.sample()
    watts, last = [], (t0, e0)
    deadline = t0 + seconds
    while True:
        time.sleep(interval)
        t, e = meter.sample()
        dt = t - last[0]
        if dt > 0:
            watts.append((e - last[1]) / dt)
        last = (t, e)
        if t >= deadline:
            break
    if not watts:
        raise RaplError("idle window too short to sample")
    std = statistics.pstdev(watts) if len(watts) > 1 else 0.0
    return Idle((t0 + last[0]) / 2, statistics.fmean(watts), std, len(watts))


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--domain", default="auto")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--idle", type=float, metavar="SEC")
    ap.add_argument("--watch", action="store_true")
    args = ap.parse_args()

    try:
        meter = open_meter(args.domain)
        if args.check:
            j = check(meter)
            print(f"ok: `{meter.label}` accumulated {j*1000:.1f} mJ in 0.4 s")
        if args.idle is not None:
            idle = measure_idle(meter, args.idle)
            print(f"idle `{meter.label}`: {idle.watts:.3f} W "
                  f"(sd {idle.std:.3f} W over {idle.n} sub-windows)")
        if args.watch:
            t0, e0 = meter.sample()
            while True:
                time.sleep(1.0)
                t, e = meter.sample()
                print(f"{(e - e0) / (t - t0):8.2f} W  [{meter.label}]",
                      flush=True)
                t0, e0 = t, e
    except RaplError as e:
        sys.exit(f"gmt_meter: {e}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
