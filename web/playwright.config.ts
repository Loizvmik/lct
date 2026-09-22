import { defineConfig, devices } from "@playwright/test";

// Task 16, обязательная проверка: полный путь пользователя в браузере
// (загрузка шаблона -> дизайн-система -> генерация -> три варианта ->
// аудит с подсветкой находок -> выгрузка). Генерация реальная (не мок) —
// минуты, не секунды (ТЗ: "время генерации одной колоды — не более пяти
// минут"), отсюда увеличенный `timeout` теста и `webServer.timeout`.
//
// `channel: "chrome"` — тестовый профиль использует УЖЕ УСТАНОВЛЕННЫЙ в
// системе Google Chrome, а не отдельно загружаемый Playwright Chromium:
// «актуальная версия Chrome» — один из четырёх браузеров, которые ТЗ прямо
// требует поддерживать («Chrome, Firefox, Safari, Яндекс Браузер, MacOS &
// Windows, актуальная и предыдущая версии»), и в среде без доступа в сеть к
// cdn.playwright.dev это единственный способ прогнать e2e вовсе.
export default defineConfig({
  testDir: "./e2e",
  timeout: 6 * 60 * 1000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:3000",
    trace: "retain-on-failure",
    channel: "chrome",
  },
  projects: [
    { name: "chrome", use: { ...devices["Desktop Chrome"], channel: "chrome" } },
  ],
  webServer: [
    {
      command: "pnpm start",
      cwd: __dirname,
      url: "http://127.0.0.1:3000",
      timeout: 60_000,
      reuseExistingServer: !process.env.CI,
    },
    {
      command: "uv run uvicorn deckforge.api.app:app --host 127.0.0.1 --port 8000",
      cwd: "..",
      url: "http://127.0.0.1:8000/api/health",
      timeout: 60_000,
      reuseExistingServer: !process.env.CI,
    },
  ],
});
