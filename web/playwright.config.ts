import { defineConfig, devices } from "@playwright/test";

// Task 16, обязательная проверка: полный путь пользователя в браузере
// (загрузка шаблона -> дизайн-система -> генерация -> три варианта ->
// аудит с подсветкой находок -> выгрузка). Генерация реальная (не мок) —
// минуты, не секунды (ТЗ: "время генерации одной колоды — не более пяти
// минут"), отсюда увеличенный `timeout` теста и `webServer.timeout`.
//
// Браузер берётся уже установленный в системе, а не скачанный Playwright
// Chromium: в среде без доступа к cdn.playwright.dev иначе e2e не прогнать.
// По умолчанию Google Chrome (macOS), на Windows `E2E_CHANNEL=msedge`.
// Порты переопределяются через `E2E_WEB_PORT`/`E2E_API_PORT`, чтобы прогон
// не мешал уже запущенному сервису на 3000/8000; интерфейс при этом нужно
// собрать с `NEXT_PUBLIC_API_BASE` на тот же порт API.
const CHANNEL = process.env.E2E_CHANNEL ?? "chrome";
const WEB_PORT = process.env.E2E_WEB_PORT ?? "3000";
const API_PORT = process.env.E2E_API_PORT ?? "8000";

export default defineConfig({
  testDir: "./e2e",
  timeout: 6 * 60 * 1000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: "retain-on-failure",
    channel: CHANNEL,
  },
  projects: [
    { name: CHANNEL, use: { ...devices["Desktop Chrome"], channel: CHANNEL } },
  ],
  webServer: [
    {
      command: `npx next start -p ${WEB_PORT}`,
      cwd: __dirname,
      url: `http://127.0.0.1:${WEB_PORT}`,
      timeout: 60_000,
      reuseExistingServer: !process.env.CI,
    },
    {
      command: `uv run uvicorn deckforge.api.app:app --host 127.0.0.1 --port ${API_PORT}`,
      cwd: "..",
      url: `http://127.0.0.1:${API_PORT}/api/health`,
      timeout: 60_000,
      reuseExistingServer: !process.env.CI,
    },
  ],
});
