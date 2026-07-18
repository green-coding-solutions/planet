#!/usr/bin/env bash
# Apply host-calibrated coefficients, if any were mounted at /etc/planet.
#
# Absence is not an error: the extension has built-in defaults and the server
# must still come up. It IS worth a loud line in the log, because uncalibrated
# carbon numbers are only meaningful as rankings, never as joules.
set -Eeuo pipefail

planet-apply-coefficients
