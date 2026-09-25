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
# FONTCONFIG_PATH обязателен: без него LibreOffice на macOS подменяет
# шрифты шаблона засечными и картинка врёт про вёрстку.
set -euo pipefail

PPTX="${1:?укажите .pptx}"
OUT="${2:-$(dirname "$PPTX")/preview}"
SOFFICE="${SOFFICE:-/Applications/LibreOffice.app/Contents/MacOS/soffice}"

[ -f "$PPTX" ] || { echo "нет файла: $PPTX" >&2; exit 1; }
[ -x "$SOFFICE" ] || { echo "нет LibreOffice: $SOFFICE" >&2; exit 1; }

export FONTCONFIG_PATH="${FONTCONFIG_PATH:-/opt/homebrew/etc/fonts}"
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
