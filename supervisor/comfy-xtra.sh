#!/bin/bash
# Supervisor launcher for the Comfortable dashboard (see comfy-xtra.conf).
XTRA_DIR="$(cd "$(dirname "$0")/.." && pwd)"
utils=/opt/supervisor-scripts/utils
[[ -f "${utils}/environment.sh" ]] && . "${utils}/environment.sh"
ENV_FILE=/workspace/.comfy_xtra.env
[[ -f "$ENV_FILE" ]] && . "$ENV_FILE"
if [[ -x /venv/main/bin/python ]]; then PY=/venv/main/bin/python
elif [[ -x /opt/conda/bin/python ]]; then PY=/opt/conda/bin/python
else PY="$(command -v python3)"
fi
# Free the port if a copy launched outside supervisord still holds it.
fuser -k 17890/tcp >/dev/null 2>&1 || true
exec > >(tee -a /var/log/comfy-xtra.log) 2>&1
exec "$PY" -u "$XTRA_DIR/dashboard/server.py"
