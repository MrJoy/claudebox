#!/usr/bin/env bash
# Unit tests for the Python review loop. No Docker, no network, no credentials.
# The bash acceptance suites (test-providers.sh, test-personas.sh) prove the
# wiring; these prove the decisions the loop makes.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m unittest discover -s tests -t tests "$@"
