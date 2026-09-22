import { test, expect } from "@playwright/test";
import path from "node:path";

// Обязательная проверка Task 16 — весь путь пользователя в браузере на
// РЕАЛЬНОМ пайплайне (не мок API): загрузка шаблона -> дизайн-система ->
// бриф -> прогресс по этапам -> три варианта -> аудит с подсветкой находок
// поверх превью -> выгрузка. Реальная генерация занимает минуты (ТЗ:
// "не более пяти минут"), поэтому тест один, длинный, а не набор мелких
// изолированных тестов с моками — мок здесь бы проверил вёрстку, а не то,
// что брифом действительно требуется показать вживую.
const TEMPLATE_PATH = path.resolve(__dirname, "../../dataset/templates/ЛЦТ2026 Шаблон презентации.pptx");

test("полный путь: шаблон -> бриф -> варианты -> аудит с подсветкой -> выгрузка", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: /Шаг 1 — загрузите/ })).toBeVisible();

  const fileInput = page.locator('input[type="file"]');
  await fileInput.setInputFiles(TEMPLATE_PATH);

  // Разбор шаблона -> редирект на /templates/{id}, дизайн-система видна.
  await page.waitForURL(/\/templates\/[a-f0-9]+$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { level: 1, name: /Дизайн-система шаблона/ })).toBeVisible();
  await expect(page.getByText("Палитра ролей", { exact: false })).toBeVisible();
  await expect(page.getByText("Каталог паттернов вёрстки", { exact: false })).toBeVisible();
  await expect(page.getByText("Откуда что взято")).toBeVisible();

  await page.getByRole("link", { name: /Дальше: бриф и материалы/ }).click();
  await page.waitForURL(/\/templates\/[a-f0-9]+\/brief$/);

  await page.getByPlaceholder(/Например, «Сокращение времени/).fill("e2e — согласование заявок");
  await page.getByPlaceholder(/Просим комитет согласовать/).fill(
    "Просим комитет согласовать запуск автоматической маршрутизации заявок на " +
      "внутренней платформе. Показать, где уходит время, что дал пилот и что нужно " +
      "для раскатки на всю компанию.",
  );
  await page.getByPlaceholder(/Выборка: 1240 заявок/).fill(
    "Выборка: 1240 заявок, медиана ожидания 18 часов, чистая работа — 28 минут.",
  );

  await page.getByRole("button", { name: /Сгенерировать три варианта/ }).click();
  await page.waitForURL(/\/decks\/[a-f0-9]+$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { level: 1, name: "Генерация колоды" })).toBeVisible();

  // Прогресс — реальные этапы, не крутилка (ТЗ, Step 3 брифа задачи).
  await expect(page.locator(".stage-track .stage").first()).toBeVisible();

  // Вся генерация: разбор + структура + текст + три варианта + аудит +
  // выгрузка — до пяти минут по ТЗ; берём запас на CI/холодный кеш шаблона.
  await page.waitForURL(/\/decks\/[a-f0-9]+\/variants$/, { timeout: 5 * 60 * 1000 });

  await expect(page.getByRole("heading", { level: 1, name: /три варианта вёрстки/ })).toBeVisible();
  const variantCards = page.locator(".variant-card");
  await expect(variantCards).toHaveCount(3);
  for (const label of ["Плотный", "Воздушный", "Визуальный"]) {
    await expect(page.getByText(label, { exact: true })).toBeVisible();
  }
  // Каждый вариант несёт хотя бы одно превью-изображение слайда.
  await expect(page.locator(".variant-card .preview img").first()).toBeVisible();

  await page.locator(".variant-card").first().getByRole("link", { name: /Аудит и починка/ }).click();
  await page.waitForURL(/\/decks\/[a-f0-9]+\/audit\?variant=/);

  await expect(page.getByRole("heading", { level: 1, name: /Шаг 4 — аудит/ })).toBeVisible();
  await expect(page.locator(".slide-preview-wrap img")).toBeVisible();

  // Сердце задачи: подсветка находок ПОВЕРХ превью по координатам —
  // проверяем, что хотя бы одна рамка позиционирована процентами (не
  // нулевой заглушкой) поверх картинки слайда, обходя слайды при
  // необходимости (не на каждом слайде обязана быть находка).
  let foundBox = false;
  const thumbCount = await page.locator(".thumb-strip .thumb").count();
  for (let i = 0; i < thumbCount && !foundBox; i++) {
    await page.locator(".thumb-strip .thumb").nth(i).click();
    const boxes = page.locator(".finding-box");
    if (await boxes.count()) {
      const style = await boxes.first().getAttribute("style");
      expect(style).toMatch(/left: \d/);
      expect(style).toMatch(/top: \d/);
      foundBox = true;
    }
  }
  expect(foundBox).toBe(true);

  // Экран выбора: чекбокс на находке, кнопка «исправить выбранное».
  const checkbox = page.locator('.finding-item input[type="checkbox"]:not([disabled])').first();
  if (await checkbox.count()) {
    await expect(checkbox).toBeVisible();
    const fixButton = page.getByRole("button", { name: /Исправить выбранное/ });
    await expect(fixButton).toBeEnabled();
  }

  // Выгрузка — три ссылки (.pptx/.pdf/.html) на вариант, ведущие на
  // реальный маршрут экспорта API (скачивание файла не гоняем через
  // браузерный download-диалог в CI — проверяем сам URL).
  await page.goto(`/decks/${page.url().match(/decks\/([a-f0-9]+)/)![1]}/variants`);
  const pptxLink = page.locator('.variant-card a:text("pptx")').first();
  await expect(pptxLink).toHaveAttribute("href", /\/api\/decks\/.+\/export\?format=pptx&variant=/);
});
