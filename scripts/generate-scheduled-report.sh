#!/usr/bin/bash
# Generates a timestamped PDF report. Intended to be run on a schedule (the
# health-thermometer-report-generate.timer systemd unit, or a cron job)
# rather than invoked directly. Configure via environment variables, not
# flags, since a scheduler invokes this with a fixed command line.
set -e

CONFIG="${HEALTH_THERMOMETER_CONFIG:-/etc/health-thermometer-daemon/config.ini}"
REPORT_DIR="${HEALTH_THERMOMETER_REPORT_DIR:-/var/lib/health-thermometer-daemon/reports}"

mkdir -p "${REPORT_DIR}"
timestamp="$(date +%Y%m%d-%H%M%S)"
health-thermometer-report --config "${CONFIG}" --output "${REPORT_DIR}/report-${timestamp}.pdf"
