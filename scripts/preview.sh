#!/usr/bin/env bash
# Рендерит собранную презентацию в картинки — по одной на слайд.
#
# Нужен для разбора глазами: аудит ловит то, что умеет посчитать, а
# «слайд выглядит пустым» или «иконки вразброс» видно только на картинке.
#
#   scripts/preview.sh <файл.pptx> [каталог-вывода]
#
# Без каталога вывода кладёт рядом с файлом, в подкаталог `preview`.
#
# FONTCONFIG_PATH обязателен: без него LibreOffice (что на macOS, что на
# Ubuntu без системного каталога шрифтов в PATH поиска) подменяет шрифты
# шаблона засечными и картинка врёт про вёрстку.
set -euo pipefail

PPTX="${1:?укажите .pptx}"
OUT="${2:-$(dirname "$PPTX")/preview}"
# Задача Z (деплой на Ubuntu): раньше SOFFICE был жёстко зашит на путь
# macOS-приложения — на Linux бинарник лежит в PATH (`/usr/bin/soffice`
# после `apt install libreoffice-impress`), находим тем же порядком, что и
# `RenderConfig.resolve_soffice` в settings.py (PATH, затем типичные пути).
SOFFICE="${SOFFICE:-$(command -v soffice || true)}"
for candidate in /opt/homebrew/bin/soffice /Applications/LibreOffice.app/Contents/MacOS/soffice /usr/local/bin/soffice /usr/bin/soffice; do
  [ -n "$SOFFICE" ] && break
  [ -x "$candidate" ] && SOFFICE="$candidate"
done

[ -f "$PPTX" ] || { echo "нет файла: $PPTX" >&2; exit 1; }
[ -n "$SOFFICE" ] && [ -x "$SOFFICE" ] || { echo "нет LibreOffice (soffice не найден в PATH и типичных путях)" >&2; exit 1; }

for fc_candidate in "${FONTCONFIG_PATH:-}" /opt/homebrew/etc/fonts /usr/local/etc/fonts /etc/fonts; do
  [ -n "$fc_candidate" ] && [ -d "$fc_candidate" ] && { FONTCONFIG_PATH="$fc_candidate"; break; }
done
export FONTCONFIG_PATH="${FONTCONFIG_PATH:-}"
mkdir -p "$OUT"

# Свой каталог профиля на вызов — параллельные вызовы LibreOffice иначе
# дерутся за один профиль пользователя и молча падают.
PROFILE_DIR="$(mktemp -d)"
"$SOFFICE" --headless -env:UserInstallation="file://$PROFILE_DIR" \
  --convert-to pdf --outdir "$OUT" "$PPTX" >/dev/null 2>&1
rm -rf "$PROFILE_DIR"

PDF="$OUT/$(basename "${PPTX%.pptx}").pdf"
[ -f "$PDF" ] || { echo "LibreOffice не отдал pdf" >&2; exit 1; }

pdftoppm -r 70 -png "$PDF" "$OUT/s"
ls "$OUT"/s-*.png | wc -l | xargs echo "слайдов отрисовано:"
echo "$OUT"
