# Развёртывание на арендованном сервере (Ubuntu)

Задача Z: защита ЛЦТ пройдёт на арендованном сервере с Ubuntu 22.04/24.04,
не на macOS, где до сих пор велась разработка (`scripts/run.sh`,
LibreOffice и шрифт `Play` из шаблона ставились вручную). Этот документ —
шаги, специфичные для такого сервера; общая логика пайплайна — в
[ARCHITECTURE.md](ARCHITECTURE.md), сами команды `deckforge` — в
[README.md](../README.md).

## Требования к серверу

- Ubuntu 22.04 LTS или 24.04 LTS, доступ по SSH с `sudo`.
- **Минимум 4 ГБ ОЗУ.** LibreOffice headless (рендер PDF/PNG) и сборка
  веб-интерфейса (`next build`) — самые прожорливые шаги; на 2 ГБ сборка
  веба реально падает по OOM без свопа.
- Порт 80 свободен под nginx (или 8000/3000 напрямую, если без nginx),
  исходящий доступ в интернет (apt, Google Fonts, PyPI/npm, Yandex Cloud).
- LibreOffice **headless** — на сервере нет и не нужен X11/десктоп;
  `soffice --headless` (см. `render/soffice.py`) работает без дисплея.

## 1. Клонирование

```bash
sudo mkdir -p /opt/deckforge && sudo chown "$USER" /opt/deckforge
git clone <адрес репозитория> /opt/deckforge
cd /opt/deckforge
```

Путь `/opt/deckforge` — тот, что уже подставлен в `deploy/*.service`; при
другом пути поправьте `WorkingDirectory`/`ExecStart` в обоих unit-файлах.

## 2. Установка (`setup_ubuntu.sh`)

```bash
./scripts/setup_ubuntu.sh
```

Идемпотентен — можно перезапускать (например, после `git pull`). Ставит:
системные пакеты (`libreoffice-impress`, `poppler-utils` для `pdftoppm`,
`fontconfig`, `fonts-liberation`, `git`, `curl`), Python 3.12 (apt, а если
в репозиториях нет — управляемый интерпретатор через `uv python install`),
`uv` и `uv sync --extra dev` по `pyproject.toml`/`uv.lock`, Node LTS + pnpm
(версия закреплена в `web/package.json`) и **production**-сборку `web/`
(`pnpm run build`), каталоги `cache/`, `artifacts/`, `workspace/`, шрифт
`Play` (Google Fonts, `~/.local/share/fonts`) и обновляет кэш `fontconfig`.

**Собираете под nginx с одним доменом (см. шаг 5)?** Соберите веб с пустым
`NEXT_PUBLIC_API_BASE`, чтобы браузер ходил по относительному `/api/...`
(тот же origin, без CORS) — иначе сборка зашивает дефолт
`http://127.0.0.1:8000`, недоступный снаружи:

```bash
NEXT_PUBLIC_API_BASE= ./scripts/setup_ubuntu.sh
```

Проверка шрифта отдельно (скрипт печатает это сам последним шагом):

```bash
fc-match Play   # должно вернуть Play.ttf, не заменитель с засечками
```

## 3. Секреты (`.env`)

```bash
cp .env.example .env
$EDITOR .env   # YANDEX_FOLDER_ID, YANDEX_API_KEY
chmod 600 .env
```

`.env` — единственное место для секретов (`config/app.yaml` их не
принимает, `Settings.load` упадёт на старте, если найдёт `yandex_api_key`/
`yandex_folder_id` прямо в yaml). Без ключей пайплайн не падает: каждая
роль модели откатывается на детерминированный запасной вариант — но так
получится хуже по содержанию, проверяйте вживую заранее (см. шаг 4).

Секреты читает `deckforge.settings.Settings.load` напрямую из переменных
окружения процесса (`os.environ`), без `python-dotenv` (он есть в
зависимостях транзитивно — тянет `uvicorn[standard]` для своего
`--env-file`, но код `Settings` им не пользуется). Поэтому **сам файл
`.env` ничего не подгружает**: `scripts/run.sh` перед стартом делает
`source .env`, а systemd-юниты ниже читают его через `EnvironmentFile=` —
оба пути рабочие, ничего сверх файла добавлять не нужно.

## 4. Прогрев кэша шаблонов

Разбор незнакомого `.pptx`-шаблона в `TemplateProfile` — самый долгий
офлайн-шаг (уточнение вида раскладки моделью по картинке, минуты); кэш по
отпечатку файла (`cache/profiles`, `paths.profile_cache` в
`config/app.yaml`) экономит это при каждой следующей генерации на том же
шаблоне. Прогрейте кэш **до** защиты, на все шаблоны, которые собираетесь
показывать:

```bash
set -a && source .env && set +a   # без этого разбор идёт без модели
for f in dataset/templates/*.pptx; do
  uv run deckforge parse "$f" -o /tmp/"$(basename "$f").profile.json"
done
```

Каждый разбор — примерно 2–5 минут (живой замер на контрольной машине:
191.6с на первый разбор, 0.04с на повторный из кэша — см. README.md,
раздел «Замеры»). На четыре шаблона закладывайте до 20 минут с запасом на
429 от модели (см. ниже) и повторные попытки.

## 5. Запуск сервисов

### Разовая проверка (dev-режим)

```bash
./scripts/run.sh
```

Поднимает оба сервиса в dev-режиме (`uvicorn` без `--reload` по умолчанию
— см. комментарий в самом скрипте; `next dev` для веба) на 127.0.0.1:8000
и 127.0.0.1:3000, останавливается по Ctrl+C. Годится проверить, что всё
собралось, не для постоянной работы на защите.

### Постоянный запуск (systemd)

```bash
sudo cp deploy/deckforge-api.service deploy/deckforge-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deckforge-api deckforge-web
sudo systemctl status deckforge-api deckforge-web
```

API — без `--reload` (`deploy/deckforge-api.service`), веб — production
`next start`, не `next dev` (`deploy/deckforge-web.service`). Оба слушают
только `127.0.0.1` — наружу их отдаёт nginx.

Логи:

```bash
journalctl -u deckforge-api -f
journalctl -u deckforge-web -f
```

### nginx (один внешний адрес)

```bash
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/deckforge
sudo ln -s /etc/nginx/sites-available/deckforge /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

`/api/*` уходит на 8000, всё остальное — на 3000 (см. комментарии в самом
файле, включая `proxy_buffering off` для потока прогресса задачи,
`/api/jobs/{id}/events`, SSE).

## 6. Проверка

```bash
curl -s http://127.0.0.1:8000/api/health   # напрямую к API
curl -s http://<адрес сервера>/api/health  # через nginx
```

`/docs` (Swagger UI) FastAPI регистрирует сам, без префикса `/api` — в
примере `nginx.conf.example` под него нет отдельного `location`, доступен
только по прямому порту:

```bash
curl -s http://127.0.0.1:8000/docs   # напрямую к API, минуя nginx
```

## Если модель отвечает 429

Каждая роль модели (структура, текст, палитра, аудит по картинке) в
пайплайне честно откатывается на детерминированный запасной вариант при
ошибке или таймауте (`llm.deadline_seconds` в `config/app.yaml`, по
умолчанию 60с на вызов) — генерация не падает, просто хуже по содержанию.
Признаки: в ответе `deckforge parse`/логе job'а `skipped_reason` или
предупреждение про запасной вариант вместо тишины. Если 429 систематический
(не единичный всплеск) — это лимит запросов сервисного аккаунта Yandex
Cloud, снижайте параллелизм в `config/app.yaml` (`llm.slide_writer_max_workers`,
`llm.pattern_kind_max_workers` и соседние `*_max_workers`).

## Где искать логи

- `journalctl -u deckforge-api` / `journalctl -u deckforge-web` — сервисы.
- `nginx` — `/var/log/nginx/error.log`, `/var/log/nginx/access.log`.
- Прогресс конкретной генерации — SSE `/api/jobs/{id}/events` (в
  интерфейсе показывается сам), в терминале: `curl -N .../api/jobs/<id>/events`.
