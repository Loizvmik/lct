#!/usr/bin/env bash
# Воспроизводимый запуск DeckForge одной командой (ТЗ: "воспроизводимый
# сетап и запуск конфиг файлом") — поднимает API (FastAPI/uvicorn,
# config/app.yaml — единственная точка настройки пайплайна) и веб-интерфейс
# (Next.js, web/) вместе, останавливает оба по Ctrl+C.
#
# Использование:
#   ./scripts/run.sh
#
# Для постоянного запуска на арендованном сервере (а не разовой проверки)
# используйте deploy/deckforge-api.service и deploy/deckforge-web.service
# (systemd, production `next start`) — см. docs/DEPLOY.md. Этот скрипт как
# был, так и остаётся удобством для разработки (`pnpm dev` — с hot reload).
#
# Переменные окружения (необязательные):
#   API_PORT   — порт FastAPI (по умолчанию 8000)
#   WEB_PORT   — порт Next.js (по умолчанию 3000)
#   DECKFORGE_RELOAD — 1 включает `uvicorn --reload` (по умолчанию выключен:
#     задача Z, найдено ненадёжным при долгом прогоне на сервере — не
#     подхватывал часть правок и не восстанавливался сам после падения
#     воркера; для разработки на своей машине включайте явно)
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

# `--reload` — чтобы правка в src/ применялась без ручного перезапуска, как
# это давно делает веб-часть (`pnpm dev`). 23 сентября 2026 несимметрия
# стоила полудня: фиксы в Python лежали закоммиченными, сервер крутился со
# старым кодом, прогоны через интерфейс их не видели, и выглядело это как
# «починили, а ничего не изменилось». Но на арендованном сервере (задача Z)
# он не нужен (код там не правят между прогонами) и добавляет риск — по
# умолчанию выключен, включайте через DECKFORGE_RELOAD=1.
# Пустой массив под `set -u` в bash 3.2 (macOS) считается несвязанной
# переменной, поэтому флаг собирается строкой, а не массивом.
RELOAD_ARGS=""
if [ "${DECKFORGE_RELOAD:-0}" = "1" ]; then
  RELOAD_ARGS="--reload --reload-dir src"
fi
echo "==> API: http://127.0.0.1:${API_PORT} (config/app.yaml)"
# shellcheck disable=SC2086
uv run uvicorn deckforge.api.app:app --host 127.0.0.1 --port "$API_PORT" $RELOAD_ARGS &
API_PID=$!

echo "==> Веб-интерфейс: http://127.0.0.1:${WEB_PORT}"
(cd web && NEXT_PUBLIC_API_BASE="http://127.0.0.1:${API_PORT}" pnpm dev --port "$WEB_PORT") &
WEB_PID=$!

wait "$API_PID" "$WEB_PID"
