#!/usr/bin/env bash
# Poll the local web health endpoint after a restart. Service startup time varies with
# migrations, imports, and host load, so one fixed-delay probe is not a reliable gate.
set -euo pipefail

HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8110/health}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-30}"
HEALTH_INTERVAL_SECONDS="${HEALTH_INTERVAL_SECONDS:-1}"
HEALTH_CURL_TIMEOUT_SECONDS="${HEALTH_CURL_TIMEOUT_SECONDS:-2}"

positive_integer() {
    case "$2" in
        ''|*[!0-9]*|0)
            echo "!! $1 must be a positive integer (got '$2')" >&2
            exit 2
            ;;
    esac
}

positive_integer HEALTH_TIMEOUT_SECONDS "$HEALTH_TIMEOUT_SECONDS"
positive_integer HEALTH_INTERVAL_SECONDS "$HEALTH_INTERVAL_SECONDS"
positive_integer HEALTH_CURL_TIMEOUT_SECONDS "$HEALTH_CURL_TIMEOUT_SECONDS"

deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
attempt=0
code="000"

while :; do
    attempt=$((attempt + 1))
    if code="$(curl -sS -m "$HEALTH_CURL_TIMEOUT_SECONDS" -o /dev/null \
        -w '%{http_code}' "$HEALTH_URL")"; then
        :
    else
        # curl already writes 000 for connection failures. Preserve one canonical code
        # instead of appending a second 000 and reporting the misleading value 000000.
        code="${code:-000}"
    fi
    echo "local /health: $code (attempt $attempt)"

    if [ "$code" = "200" ]; then
        echo "health gate passed."
        exit 0
    fi

    remaining=$((deadline - SECONDS))
    if [ "$remaining" -le 0 ]; then
        echo "!! /health did not return 200 within ${HEALTH_TIMEOUT_SECONDS}s" >&2
        exit 1
    fi

    sleep_for="$HEALTH_INTERVAL_SECONDS"
    if [ "$sleep_for" -gt "$remaining" ]; then
        sleep_for="$remaining"
    fi
    echo "-- waiting ${sleep_for}s for service startup"
    sleep "$sleep_for"
done
