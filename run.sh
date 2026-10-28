#!/usr/bin/env bash
# Run the payment gateway sandbox exporter, Prometheus and Grafana on this host with podman or docker.
#
#   ./run.sh up        build the exporter image and start all three containers
#   ./run.sh down      stop and remove the containers (data volumes are kept)
#   ./run.sh purge     same as down, and also remove the data volumes
#   ./run.sh status    show containers and the exporter's health metrics
#   ./run.sh logs exporter|validator|prometheus|grafana
#
# $GW_BASE_URL must point to the gateway sandbox. The API key is taken from $GW_KEY. If it is not set, the script asks for it (input hidden),
# or, when a `with-secrets` helper and $GW_KEEPASS_ENTRY are present, fetches it from KeePass.
# It is passed to the exporter container as an environment variable and never written to a file.
set -euo pipefail
cd "$(dirname "$0")"

RUNTIME="${RUNTIME:-$(command -v podman || command -v docker || true)}"
NET=gwmon
BIND="${BIND:-127.0.0.1}"
PROM_PORT="${PROM_PORT:-9090}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
EXPORTER_PORT="${EXPORTER_PORT:-9105}"
VALIDATOR_PORT="${VALIDATOR_PORT:-9106}"
VALIDATION_INTERVAL_SECONDS="${VALIDATION_INTERVAL_SECONDS:-300}"
GW_BASE_URL="${GW_BASE_URL:-}"
GW_REFRESH_SECONDS="${GW_REFRESH_SECONDS:-60}"
GW_WINDOW_HOURS="${GW_WINDOW_HOURS:-24}"
GW_STUCK_MINUTES="${GW_STUCK_MINUTES:-30}"
PROM_IMAGE="${PROM_IMAGE:-docker.io/prom/prometheus:v2.55.1}"
GRAFANA_IMAGE="${GRAFANA_IMAGE:-docker.io/grafana/grafana-oss:11.3.0}"
EXPORTER_IMAGE="${EXPORTER_IMAGE:-localhost/gateway-exporter:latest}"
NEWMAN_IMAGE="${NEWMAN_IMAGE:-docker.io/postman/newman:6.1.3-alpine}"

die() { echo "error: $*" >&2; exit 1; }

check_runtime() {
  [ -n "$RUNTIME" ] || die "podman or docker is required"
  if ! "$RUNTIME" info >/dev/null 2>&1; then
    case "$RUNTIME" in
      *podman) die "podman is not reachable; on macOS/Windows run: podman machine start" ;;
      *) die "docker daemon is not reachable; start Docker first" ;;
    esac
  fi
}

# SELinux hosts need relabelled bind mounts
mount_opts() {
  if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = "Enforcing" ]; then
    echo "ro,Z"
  else
    echo "ro"
  fi
}

ensure_key() {
  [ -n "${GW_KEY:-}" ] && return
  if [ -n "${GW_KEEPASS_ENTRY:-}" ] && command -v with-secrets >/dev/null 2>&1; then
    exec with-secrets GW_KEY="$GW_KEEPASS_ENTRY" -- "$0" "$@"
  fi
  [ -t 0 ] || die "set GW_KEY (sandbox API key) in the environment"
  read -rsp "API key: " GW_KEY
  echo
  [ -n "$GW_KEY" ] || die "empty API key"
}

wait_http() {
  local url=$1 name=$2 i
  for i in $(seq 1 60); do
    if curl -sf -o /dev/null "$url"; then echo "  $name ready"; return 0; fi
    sleep 1
  done
  echo "  $name did not answer at $url within 60s (check: ./run.sh logs ${name})" >&2
}

up() {
  check_runtime
  [ -n "$GW_BASE_URL" ] || die "set GW_BASE_URL to the sandbox base URL, e.g. https://sandbox.example.com"
  ensure_key "$@"
  export GW_KEY GW_BASE_URL GW_REFRESH_SECONDS GW_WINDOW_HOURS GW_STUCK_MINUTES VALIDATION_INTERVAL_SECONDS
  local mo; mo="$(mount_opts)"

  "$RUNTIME" network inspect "$NET" >/dev/null 2>&1 || "$RUNTIME" network create "$NET" >/dev/null
  for v in gwmon-prom-data gwmon-grafana-data; do
    "$RUNTIME" volume inspect "$v" >/dev/null 2>&1 || "$RUNTIME" volume create "$v" >/dev/null
  done

  echo "building exporter image"
  "$RUNTIME" build -q -t "$EXPORTER_IMAGE" exporter >/dev/null

  "$RUNTIME" rm -f gwmon-exporter gwmon-validator gwmon-prometheus gwmon-grafana >/dev/null 2>&1 || true

  echo "starting containers"
  # -e NAME without a value copies it from this process, so the key never appears on a command line
  "$RUNTIME" run -d --name gwmon-exporter --network "$NET" --restart unless-stopped \
    -p "$BIND:$EXPORTER_PORT:9105" -e GW_KEY -e GW_BASE_URL \
    -e GW_REFRESH_SECONDS -e GW_WINDOW_HOURS -e GW_STUCK_MINUTES \
    "$EXPORTER_IMAGE" >/dev/null

  # read-only Postman collection run by Newman on a schedule; results served as metrics
  "$RUNTIME" run -d --name gwmon-validator --network "$NET" --restart unless-stopped \
    -p "$BIND:$VALIDATOR_PORT:9106" -e GW_KEY -e GW_BASE_URL -e VALIDATION_INTERVAL_SECONDS \
    -e NODE_PATH=/usr/local/lib/node_modules \
    -v "$PWD/validation:/validation:$mo" \
    --entrypoint node "$NEWMAN_IMAGE" /validation/runner.js >/dev/null

  "$RUNTIME" run -d --name gwmon-prometheus --network "$NET" --restart unless-stopped \
    -p "$BIND:$PROM_PORT:9090" \
    -v "$PWD/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:$mo" \
    -v "$PWD/prometheus/rules.yml:/etc/prometheus/rules.yml:$mo" \
    -v gwmon-prom-data:/prometheus \
    "$PROM_IMAGE" --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.retention.time=15d >/dev/null

  "$RUNTIME" run -d --name gwmon-grafana --network "$NET" --restart unless-stopped \
    -p "$BIND:$GRAFANA_PORT:3000" \
    -e GF_AUTH_ANONYMOUS_ENABLED=true -e GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer \
    -e GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/var/lib/grafana/dashboards/gateway-sandbox.json \
    -v "$PWD/grafana/provisioning:/etc/grafana/provisioning:$mo" \
    -v "$PWD/grafana/dashboards:/var/lib/grafana/dashboards:$mo" \
    -v gwmon-grafana-data:/var/lib/grafana \
    "$GRAFANA_IMAGE" >/dev/null

  wait_http "http://localhost:$EXPORTER_PORT/metrics" exporter
  wait_http "http://localhost:$VALIDATOR_PORT/metrics" validator
  wait_http "http://localhost:$PROM_PORT/-/ready" prometheus
  wait_http "http://localhost:$GRAFANA_PORT/api/health" grafana
  echo
  echo "Grafana:    http://localhost:$GRAFANA_PORT  (anonymous view; admin/admin to edit)"
  echo "Prometheus: http://localhost:$PROM_PORT"
  echo "Exporter:   http://localhost:$EXPORTER_PORT/metrics  (first data after ~1 min)"
  echo "Validator:  http://localhost:$VALIDATOR_PORT/metrics  (Newman run every ${VALIDATION_INTERVAL_SECONDS}s)"
}

down() {
  check_runtime
  "$RUNTIME" rm -f gwmon-exporter gwmon-validator gwmon-prometheus gwmon-grafana >/dev/null 2>&1 || true
  echo "containers removed; volumes kept (./run.sh purge removes them)"
}

purge() {
  down
  "$RUNTIME" volume rm gwmon-prom-data gwmon-grafana-data >/dev/null 2>&1 || true
  "$RUNTIME" network rm "$NET" >/dev/null 2>&1 || true
  echo "volumes and network removed"
}

status() {
  check_runtime
  "$RUNTIME" ps -a --filter name=gwmon- --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
  echo
  curl -sf "http://localhost:$EXPORTER_PORT/metrics" \
    | grep -E '^gateway_(api_up|api_auth_ok|payments_window_ok|exporter_refresh_ok) ' \
    || echo "exporter metrics not available yet"
  curl -sf "http://localhost:$VALIDATOR_PORT/metrics" \
    | grep -E '^gateway_validation_(up|assertions|check_ok)' \
    || echo "validator metrics not available yet"
}

logs() {
  check_runtime
  local svc=${1:-}
  case "$svc" in
    exporter|validator|prometheus|grafana) "$RUNTIME" logs -f "gwmon-$svc" ;;
    *) die "usage: ./run.sh logs exporter|validator|prometheus|grafana" ;;
  esac
}

cmd=${1:-}
shift || true
case "$cmd" in
  up) up "$cmd" "$@" ;;
  down) down ;;
  purge) purge ;;
  status) status ;;
  logs) logs "$@" ;;
  *) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 64 ;;
esac
