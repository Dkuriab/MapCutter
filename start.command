#!/bin/bash
# City Map 3D — запуск на macOS. Двойной клик в Finder или ./start.command в Терминале.
set -e
cd "$(dirname "$0")"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Нужен Python 3.9+. Поставь его: brew install python  (или с python.org)"
  read -r -p "Enter — закрыть"; exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Первый запуск: ставлю зависимости в .venv (пару минут)…"
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
fi
# reinstall only when requirements.txt changed
if [ ! -f .venv/.req_ok ] || ! cmp -s requirements.txt .venv/.req_ok; then
  .venv/bin/python -m pip install -r requirements.txt
  cp requirements.txt .venv/.req_ok
fi

PORT=${PORT:-8000}
( sleep 2; open "http://localhost:$PORT" ) &
echo "Сервер: http://localhost:$PORT  (Ctrl+C — остановить)"
exec .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port "$PORT"
