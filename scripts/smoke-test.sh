#!/usr/bin/bash
# Installs the package into the active environment and exercises all five
# console scripts against a fixture database, to catch packaging/import
# regressions that unit-level checks might miss. Assumes `pip` on PATH
# points at the environment to test.
set -e

WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Installing package from ${REPO_DIR}"
pip install --quiet "${REPO_DIR}"

echo "==> Creating fixture database and config"
python3 "${REPO_DIR}/scripts/make-fixture-db.py" "${WORKDIR}/readings.db"

cat > "${WORKDIR}/config.ini" <<EOF
[daemon]
address = AA:BB:CC:DD:EE:FF
log_level = INFO

[storage]
db_path = ${WORKDIR}/readings.db
EOF

echo "==> health-thermometer-daemon"
health-thermometer-daemon --version
health-thermometer-daemon --help > /dev/null
health-thermometer-daemon --config "${WORKDIR}/config.ini" --check-config

echo "==> health-thermometer-report"
health-thermometer-report --version
health-thermometer-report --help > /dev/null
health-thermometer-report --config "${WORKDIR}/config.ini" --output "${WORKDIR}/out.pdf"
test -s "${WORKDIR}/out.pdf"
health-thermometer-report --config "${WORKDIR}/config.ini" --format csv --output "${WORKDIR}/out.csv"
grep -q "Date/Time" "${WORKDIR}/out.csv"

echo "==> health-thermometer-prune"
health-thermometer-prune --version
health-thermometer-prune --help > /dev/null
health-thermometer-prune --config "${WORKDIR}/config.ini" --older-than 9999 | grep -q "Would delete 0"

echo "==> health-thermometer-alert-check"
health-thermometer-alert-check --version
health-thermometer-alert-check --help > /dev/null
health-thermometer-alert-check --config "${WORKDIR}/config.ini" | grep -q "disabled"

echo "==> health-thermometer-api"
health-thermometer-api --version
health-thermometer-api --help > /dev/null
health-thermometer-api --config "${WORKDIR}/config.ini" | grep -q "disabled"

echo "==> Smoke test passed"
