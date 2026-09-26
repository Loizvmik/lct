#!/usr/bin/env bash
# Идемпотентная установка DeckForge на чистой Ubuntu (22.04/24.04) — задача Z
# (защита ЛЦТ на арендованном сервере, не macOS). Повторный запуск безопасен:
# каждый шаг сам проверяет, нужен ли он ещё.
#
# Использование:
#   ./scripts/setup_ubuntu.sh
#
# Что делает: системные пакеты (LibreOffice headless, poppler для pdftoppm,
# fontconfig, git, curl), Python 3.12 (apt, а если в репозиториях его нет —
# как на 22.04 без deadsnakes — управляемый интерпретатор через uv, см. ниже),
# uv + `uv sync` по pyproject.toml/uv.lock, Node LTS + pnpm (packageManager
# в web/package.json) + сборка web/, шрифт Play (Google Fonts) и Liberation,
# каталоги cache/artifacts/workspace.
#
# Не делает: не трогает .env (секреты — руками, см. .env.example), не
# запускает сервисы (см. deploy/*.service и docs/DEPLOY.md).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

log() { echo "==> $*"; }

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "Этот скрипт рассчитан на Ubuntu/Debian (apt-get не найден)." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

# --- 1. Системные пакеты -----------------------------------------------
# Пакет python3.12 в apt проверяем отдельно ниже (на Ubuntu 22.04 его нет
# без стороннего PPA) — остальное ставим одним проходом, только то, чего
# ещё нет (apt install идемпотентен сам по себе, но избегаем лишнего
# `apt-get update`, если ставить вовсе нечего).
BASE_PACKAGES=(
  libreoffice-impress
  poppler-utils
  fontconfig
  fonts-liberation
  git
  curl
  ca-certificates
)
MISSING=()
for pkg in "${BASE_PACKAGES[@]}"; do
  dpkg -s "$pkg" >/dev/null 2>&1 || MISSING+=("$pkg")
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  log "Системные пакеты: ${MISSING[*]}"
  $SUDO apt-get update -qq
  $SUDO apt-get install -y "${MISSING[@]}"
else
  log "Системные пакеты уже стоят, пропускаем apt-get"
fi

# --- 2. Python 3.12 ------------------------------------------------------
# Ubuntu 24.04 даёт python3.12 из коробки; на 22.04 его нет без deadsnakes
# PPA, а PPA на арендованном сервере — лишняя внешняя зависимость и точка
# отказа. Пробуем apt, и если пакета нет в репозиториях — не добавляем PPA,
# а доверяем `uv python install`: uv сам скачивает автономную сборку
# CPython 3.12 (pyproject.toml пинит ">=3.12,<3.13") и `uv sync` ниже
# использует именно её, без системного python3.12 вовсе.
if dpkg -s python3.12 >/dev/null 2>&1; then
  log "python3.12 уже установлен (apt)"
elif $SUDO apt-get install -y python3.12 python3.12-venv 2>/dev/null; then
  log "python3.12 установлен из apt"
else
  log "python3.12 недоступен в apt-репозиториях этой Ubuntu — доверяем uv (см. шаг ниже)"
fi

# --- 3. uv ----------------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
  log "uv уже установлен: $(uv --version)"
else
  log "Устанавливаем uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
export PATH="$HOME/.local/bin:$PATH"

# Гарантия python 3.12 независимо от того, нашёлся ли системный пакет выше.
uv python install 3.12

log "Python-зависимости (uv sync --extra dev)"
uv sync --extra dev

# --- 4. Node LTS + pnpm ---------------------------------------------------
NODE_MIN_MAJOR=20
node_ok() {
  command -v node >/dev/null 2>&1 || return 1
  local major
  major="$(node -p 'process.versions.node.split(".")[0]')"
  [ "$major" -ge "$NODE_MIN_MAJOR" ]
}
if node_ok; then
  log "Node уже подходящей версии: $(node --version)"
else
  log "Устанавливаем Node.js LTS (NodeSource)"
  # Раньше здесь было `$SUDO -E bash -`: при пустом $SUDO (уже root, как в
  # тестовом контейнере без sudo) это разворачивается в `-E bash -` —
  # первым словом становится "-E", и shell пытается выполнить несуществующую
  # команду "-E" вместо bash. Явная ветка вместо расчёта на пустое
  # раскрытие переменной.
  curl -fsSL https://deb.nodesource.com/setup_lts.x -o /tmp/deckforge-nodesource-setup.sh
  if [ -n "$SUDO" ]; then
    $SUDO -E bash /tmp/deckforge-nodesource-setup.sh
  else
    bash /tmp/deckforge-nodesource-setup.sh
  fi
  rm -f /tmp/deckforge-nodesource-setup.sh
  $SUDO apt-get install -y nodejs
fi

if ! command -v corepack >/dev/null 2>&1; then
  $SUDO npm install -g corepack
fi
corepack enable
# Версия pnpm закреплена в web/package.json (`packageManager`) — corepack
# сам подтягивает нужную при первом вызове в этом каталоге.
log "Node-зависимости и сборка web/ (pnpm, production build)"
(cd web && corepack pnpm install --frozen-lockfile && corepack pnpm run build)

# --- 5. Каталоги данных ----------------------------------------------------
mkdir -p cache/profiles artifacts workspace
log "Каталоги cache/ artifacts/ workspace/ готовы"

# --- 6. Шрифт Play (Google Fonts) + Liberation -----------------------------
# Liberation уже пришёл пакетом fonts-liberation выше и виден fontconfig
# сразу. Play в Ubuntu не пакетирован — тянем TTF из google/fonts (тот же
# репозиторий, что отдаёт fonts.google.com, стабильные raw-пути по семейству
# шрифта, без API-ключа и без разбора zip с fonts.google.com/download).
FONT_DIR="$HOME/.local/share/fonts/deckforge-play"
mkdir -p "$FONT_DIR"
PLAY_BASE_URL="https://github.com/google/fonts/raw/main/ofl/play"
for weight in Regular Bold; do
  dest="$FONT_DIR/Play-${weight}.ttf"
  if [ -s "$dest" ]; then
    continue
  fi
  log "Скачиваем шрифт Play-${weight}"
  curl -fsSL "${PLAY_BASE_URL}/Play-${weight}.ttf" -o "$dest.tmp"
  mv "$dest.tmp" "$dest"
done
fc-cache -f "$FONT_DIR" >/dev/null

MATCH="$(fc-match Play 2>/dev/null || true)"
log "fc-match Play -> ${MATCH:-<пусто>}"
case "$MATCH" in
  Play*) log "Шрифт шаблона Play подключён" ;;
  *)
    echo "ВНИМАНИЕ: fc-match Play не вернул сам Play (получили: ${MATCH:-пусто})." >&2
    echo "Рендер PNG/PDF не упадёт, но шрифт слайда может расходиться с шаблоном — см. docs/DEPLOY.md." >&2
    ;;
esac

log "Готово. Дальше: cp .env.example .env (секреты), затем docs/DEPLOY.md — прогрев кэша шаблонов и запуск сервисов."
