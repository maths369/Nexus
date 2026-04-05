#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICES=(
  "nexus-api.service"
  "nexus-web.service"
  "nexus-mqtt.service"
  "nexus-weixin-host.service"
)

ACTIVE_SERVICES=()

if [ -n "${PYTHON_BIN:-}" ]; then
  PYTHON_CMD=("${PYTHON_BIN}")
elif [ -x "${HOME}/miniconda3/bin/conda" ]; then
  PYTHON_CMD=("${HOME}/miniconda3/bin/conda" "run" "--no-capture-output" "-n" "ai_assist" "python")
elif command -v conda >/dev/null 2>&1; then
  PYTHON_CMD=("conda" "run" "--no-capture-output" "-n" "ai_assist" "python")
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD=("python3")
else
  PYTHON_CMD=("python")
fi

restart_active_services() {
  if [ "${#ACTIVE_SERVICES[@]}" -eq 0 ]; then
    return
  fi
  for service in "${ACTIVE_SERVICES[@]}"; do
    systemctl --user start "${service}"
  done
}

trap restart_active_services EXIT

cd "${ROOT_DIR}"

for service in "${SERVICES[@]}"; do
  if systemctl --user is-active --quiet "${service}"; then
    ACTIVE_SERVICES+=("${service}")
  fi
done

if [ "${#ACTIVE_SERVICES[@]}" -gt 0 ]; then
  for service in "${ACTIVE_SERVICES[@]}"; do
    systemctl --user stop "${service}"
  done
fi

"${PYTHON_CMD[@]}" -m nexus vault-bootstrap-medical-kb
"${PYTHON_CMD[@]}" -m nexus reindex

if [ "${#ACTIVE_SERVICES[@]}" -gt 0 ]; then
  for service in "${ACTIVE_SERVICES[@]}"; do
    systemctl --user start "${service}"
  done
  for service in "${ACTIVE_SERVICES[@]}"; do
    systemctl --user --no-pager --full status "${service}" >/dev/null
  done
fi

trap - EXIT
