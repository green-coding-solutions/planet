#!/usr/bin/env python3
"""
Read Intel RAPL energy counters through the Linux powercap sysfs interface.

This is PLANET's *offline* ground-truth probe. It is used by
`collect_sweep.py` to build the calibration sweep. The extension itself never
reads a hardware counter.

Domains
-------
`psys` (a.k.a. PSYS / "platform") is preferred: it meters the whole SoC power
plane -- cores, uncore, DRAM and the on-package rails -- in a single counter,
so it needs no summing and cannot double-count. Where the firmware does not
expose it, fall back to `package-N` + `dram` (`--domain package+dram`).

Caveat: PSYS bounds the *platform* rails. A discrete NVMe drive
or fans on a separate rail are outside it, so the fitted beta_r / beta_w absorb
only the I/O energy PSYS sees. For a full-node number, cross-check against a
wall-power meter -- the fitted coefficients are only as wide as the meter.

Permissions
-----------
`energy_uj` is root-only on current kernels (CVE-2020-8694, "PLATYPUS": a
high-resolution energy counter is a side channel against constant-time crypto).
Granting read access is a real, if modest, security trade-off -- do it on a
calibration box, not in production:

    sudo chmod a+r /sys/class/powercap/intel-rapl:*/energy_uj   # until reboot

Usage
-----
    python3 rapl.py --list             # what this machine exposes
    python3 rapl.py --check            # verify the counter actually advances
    python3 rapl.py --idle 10          # measure idle platform power (W)
    python3 rapl.py --watch            # live watts, one line per second
"""
import argparse
import glob
import os
import statistics
import sys
import time

SYSFS = "/sys/class/powercap"

PERM_HELP = """\
cannot read RAPL energy counters (root-only since CVE-2020-8694).

Grant read access on this calibration machine:

    sudo chmod a+r /sys/class/powercap/intel-rapl:*/energy_uj

To persist across reboots, add a udev rule:

    echo 'SUBSYSTEM=="powercap", ACTION=="add", RUN+="/bin/chmod a+r /sys%p/energy_uj"' \\
      | sudo tee /etc/udev/rules.d/99-rapl-readable.rules

Running the collector as root instead is possible but then psql connects as
root too -- pass PGUSER/PGDATABASE explicitly if you go that way."""


class RaplError(RuntimeError):
    pass


def _read(path):
    with open(path) as f:
        return f.read().strip()


class Domain:
    """One powercap domain, with wraparound-corrected cumulative energy."""

    def __init__(self, path):
        self.path = path
        self.name = _read(os.path.join(path, "name"))
        self.max_uj = int(_read(os.path.join(path, "max_energy_range_uj")))
        try:
            self._f = open(os.path.join(path, "energy_uj"))
        except PermissionError:
            raise RaplError(PERM_HELP)
        self._last = None
        self._total_uj = 0

    @property
    def enabled(self):
        p = os.path.join(self.path, "enabled")
        return _read(p) == "1" if os.path.exists(p) else True

    def _raw_uj(self):
        self._f.seek(0)
        return int(self._f.read())

    def poll_uj(self):
        """Cumulative microjoules since the first poll (wrap-corrected).

        The counter is a free-running register that wraps at
        max_energy_range_uj; consecutive polls must be closer together than the
        wrap period (hours at platform power levels), which they always are.
        """
        raw = self._raw_uj()
        if self._last is not None:
            delta = raw - self._last
            if delta < 0:
                delta += self.max_uj + 1
            self._total_uj += delta
        self._last = raw
        return self._total_uj

    def close(self):
        self._f.close()


class Meter:
    """A set of domains sampled together and summed."""

    def __init__(self, domains):
        if not domains:
            raise RaplError("no usable RAPL domains")
        self.domains = domains
        self.label = "+".join(d.name for d in domains)

    def sample(self):
        """(monotonic_seconds, cumulative_joules_since_first_sample)."""
        uj = sum(d.poll_uj() for d in self.domains)
        return time.monotonic(), uj / 1e6

    def close(self):
        for d in self.domains:
            d.close()


def _domain_paths():
    """Top-level (intel-rapl:N) and sub- (intel-rapl:N:M) powercap domains.

    The glob deliberately excludes `intel-rapl-mmio:*`, which mirrors the same
    package rail through a different register and would double-count.
    """
    out = []
    for p in sorted(glob.glob(os.path.join(SYSFS, "intel-rapl:*"))):
        if os.path.exists(os.path.join(p, "energy_uj")):
            depth = len(os.path.basename(p).split(":")) - 1
            out.append((depth, p))
    return out


def list_domains():
    rows = []
    for depth, p in _domain_paths():
        try:
            name = _read(os.path.join(p, "name"))
        except OSError:
            continue
        readable = os.access(os.path.join(p, "energy_uj"), os.R_OK)
        rows.append((depth, p, name, readable))
    return rows


def _in_container():
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def open_meter(domain="auto"):
    """Build a Meter for `psys`, `package+dram`, or auto (psys if present)."""
    found = list_domains()
    if not found:
        # In a container the domain symlinks under /sys/class/powercap are still
        # visible while their targets are not, so this looks exactly like absent
        # hardware. It usually is not.
        hint = ("inside a container: Docker masks /sys/devices/virtual/powercap "
                "(runc's mitigation for CVE-2020-8694). Run with "
                "`--security-opt systempaths=unconfined`, or meter from the host "
                "-- RAPL counts the physical package either way"
                if _in_container() else
                "not an Intel/AMD RAPL machine, or the msr/intel_rapl modules "
                "are not loaded")
        raise RaplError(f"no RAPL domains under {SYSFS} ({hint})")

    def by_name(pred):
        return [p for depth, p, name, _ in found if pred(depth, name)]

    if domain in ("auto", "psys"):
        paths = by_name(lambda d, n: n == "psys")
        if paths:
            return Meter([Domain(p) for p in paths])
        if domain == "psys":
            raise RaplError("no `psys` domain on this machine; "
                            "use --domain package+dram")

    # package-N (top level) + their dram sub-domains. `core` and `uncore` are
    # subsets of the package and must NOT be added.
    paths = by_name(lambda d, n: d == 1 and n.startswith("package")) + \
        by_name(lambda d, n: d == 2 and n == "dram")
    if not paths:
        raise RaplError("found no package/dram domains")
    return Meter([Domain(p) for p in paths])


class Idle:
    """An idle-power estimate anchored at a point in time."""

    def __init__(self, t_mid, watts, std, n):
        self.t_mid, self.watts, self.std, self.n = t_mid, watts, std, n

    def __repr__(self):
        return f"Idle({self.watts:.3f} +- {self.std:.3f} W, n={self.n})"


def measure_idle(meter, seconds, interval=0.5):
    """Mean platform power over a quiet window, plus its sub-window spread.

    The spread is the noise floor a single query has to clear: a sample whose
    dynamic energy is under a couple of `std * duration` is measuring the
    machine's background, not the query.
    """
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


def check(meter, seconds=0.4):
    """Fail loudly if the counter is present but frozen (common for psys)."""
    _, e0 = meter.sample()
    time.sleep(seconds)
    _, e1 = meter.sample()
    if e1 - e0 <= 0:
        raise RaplError(f"domain `{meter.label}` did not advance over "
                        f"{seconds}s -- the firmware exposes it but does not "
                        "update it. Use --domain package+dram.")
    return e1 - e0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", default="auto",
                    choices=["auto", "psys", "package+dram"])
    ap.add_argument("--list", action="store_true", help="show available domains")
    ap.add_argument("--check", action="store_true", help="verify the counter advances")
    ap.add_argument("--idle", type=float, metavar="SEC", help="measure idle power")
    ap.add_argument("--watch", action="store_true", help="print watts every second")
    args = ap.parse_args()

    if args.list:
        for depth, path, name, readable in list_domains():
            kind = "domain " if depth == 1 else "  sub   "
            print(f"{kind} {name:<12} {path}"
                  f"{'' if readable else '   [NOT READABLE -- see --help]'}")
        return

    try:
        meter = open_meter(args.domain)
    except RaplError as e:
        sys.exit(f"rapl: {e}")

    try:
        if args.check:
            j = check(meter)
            print(f"ok: `{meter.label}` advanced {j*1000:.1f} mJ in 0.4 s")
        if args.idle is not None:
            idle = measure_idle(meter, args.idle)
            print(f"idle `{meter.label}`: {idle.watts:.3f} W "
                  f"(sd {idle.std:.3f} W over {idle.n} sub-windows)")
        if args.watch:
            _, prev_e = meter.sample()
            prev_t = time.monotonic()
            while True:
                time.sleep(1.0)
                t, e = meter.sample()
                print(f"{(e - prev_e) / (t - prev_t):8.2f} W  [{meter.label}]",
                      flush=True)
                prev_t, prev_e = t, e
        if not (args.check or args.idle is not None):
            print(f"domain `{meter.label}` ready; try --idle 10 or --watch")
    except KeyboardInterrupt:
        pass
    except RaplError as e:
        sys.exit(f"rapl: {e}")
    finally:
        meter.close()


if __name__ == "__main__":
    main()
