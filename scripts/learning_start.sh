#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
learning_python=python
if [ -x .venv/bin/python ]; then learning_python=.venv/bin/python; fi
if command -v termux-wake-lock >/dev/null 2>&1; then termux-wake-lock; fi
nohup "$learning_python" -u -m fomo_agent.cli learning run >> logs/learning.log 2>&1 < /dev/null &
echo "Inicialização solicitada. Verifique: tail -n 20 logs/learning.log"
