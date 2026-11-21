#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/probe-agent"
ENV_FILE="${APP_DIR}/.env"
SERVICE_FILE="/etc/systemd/system/probe-agent.service"

# Замени на свой репозиторий и ветку (или передай REPO_RAW_BASE при запуске)
REPO_RAW_BASE="${REPO_RAW_BASE:-https://raw.githubusercontent.com/<YOUR_GITHUB_USER>/msk-probe-agent/main}"

echo "==> [1/5] Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -q
apt-get install -y --no-install-recommends python3-venv python3-pip curl ca-certificates -q

mkdir -p "${APP_DIR}"

echo "==> [2/5] Fetching app.py and requirements.txt from GitHub..."
curl -fsSL "${REPO_RAW_BASE}/app.py" -o "${APP_DIR}/app.py"
curl -fsSL "${REPO_RAW_BASE}/requirements.txt" -o "${APP_DIR}/requirements.txt"
sed -i 's/\r$//' "${APP_DIR}/app.py" "${APP_DIR}/requirements.txt"

if [ ! -d "${APP_DIR}/.venv" ]; then
  echo "==> [3/5] Creating virtual environment..."
  python3 -m venv "${APP_DIR}/.venv"
else
  echo "==> [3/5] Using existing virtual environment..."
fi

echo "==> Installing Python dependencies..."
"${APP_DIR}/.venv/bin/pip" install --upgrade pip -q
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt" -q

if [ ! -f "${ENV_FILE}" ]; then
  echo "==> [4/5] Generating new /opt/probe-agent/.env..."
  TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  cat > "${ENV_FILE}" <<EOF
PROBE_TOKEN=${TOKEN}
DEFAULT_TIMEOUT_MS=3000
MAX_CONCURRENCY=50
EOF
  chmod 600 "${ENV_FILE}"
  echo "============================================================"
  echo "NEW PROBE_TOKEN (save this to your backend .env):"
  echo "${TOKEN}"
  echo "============================================================"
else
  echo "==> [4/5] Existing ${ENV_FILE} kept intact."
fi

echo "==> [5/5] Configuring systemd service and restarting..."
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=MSK Node Probe Agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8090 --workers 1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable probe-agent
systemctl restart probe-agent

sleep 1
echo "==> Verifying /healthz..."
curl -fsS http://127.0.0.1:8090/healthz
echo ""
echo "==> Done."