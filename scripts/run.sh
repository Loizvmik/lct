#!/usr/bin/env bash
# Воспроизводимый запуск DeckForge одной командой (ТЗ: "воспроизводимый
# сетап и запуск конфиг файлом") — поднимает API (FastAPI/uvicorn,
# config/app.yaml — единственная точка настройки пайплайна) и веб-интерфейс
# (Next.js, web/) вместе, останавливает оба по Ctrl+C.
#
# Использование:
#   ./scripts/run.sh
#
# Переменные окружения (необязательные):
#   API_PORT   — порт FastAPI (по умолчанию 8000)
#   WEB_PORT   — порт Next.js (по умолчанию 3000)
#   YANDEX_API_KEY / YANDEX_FOLDER_ID — секреты модели (.env, см. .env.example);
#     без них пайплайн работает запасными вариантами на каждом шаге, не падает.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3000}"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

echo "==> Python-зависимости (uv sync)"
uv sync --extra dev

echo "==> Node-зависимости (pnpm install, web/)"
(cd web && pnpm install --frozen-lockfile 2>/dev/null || pnpm install)

cleanup() {
  echo
  echo "==> Останавливаем API и веб-интерфейс"
  kill "${API_PID:-}" "${WEB_PID:-}" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "==> API: http://127.0.0.1:${API_PORT} (config/app.yaml)"
uv run uvicorn deckforge.api.app:app --host 127.0.0.1 --port "$API_PORT" &
API_PID=$!

echo "==> Веб-интерфейс: http://127.0.0.1:${WEB_PORT}"
(cd web && NEXT_PUBLIC_API_BASE="http://127.0.0.1:${API_PORT}" pnpm dev --port "$WEB_PORT") &
WEB_PID=$!

wait "$API_PID" "$WEB_PID"
