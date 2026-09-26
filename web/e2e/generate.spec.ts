import { test, expect } from "@playwright/test";
import path from "node:path";

const TEMPLATE_PATH = path.resolve(__dirname, "../../dataset/templates/ЛЦТ2026 Шаблон презентации.pptx");

test("полный путь: шаблон, задание, варианты, проверка и скачивание", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Загрузите шаблон презентации" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Выбрать файл" })).toBeVisible();
  await page.locator('input[type="file"]').setInputFiles(TEMPLATE_PATH);

  await page.waitForURL(/\/templates\/[a-f0-9]+$/, { timeout: 45_000 });
  await expect(page.getByRole("heading", { level: 1, name: "ЛЦТ2026 Шаблон презентации.pptx" })).toBeVisible();
  await expect(page.getByText("Основные цвета")).toBeVisible();
  await page.getByRole("link", { name: "Перейти к заданию" }).click();

  await expect(page.getByRole("heading", { level: 1, name: "Опишите задачу" })).toBeVisible();
  const task = page.getByLabel(/Задача презентации/);
  await expect(task).toHaveValue("");
  await page.getByRole("button", { name: "Пример: заявки" }).click();
  await expect(task).not.toHaveValue("");
  await page.getByRole("button", { name: "Очистить поля" }).click();
  await expect(task).toHaveValue("");
  await expect(page.getByRole("radio", { name: /Все три стиля/ })).toBeChecked();
  await expect(page.getByRole("button", { name: "Создать три варианта" })).toBeDisabled();
  await page.getByLabel(/Название презентации/).fill("Итоги проекта");
  await task.fill("Показать руководителям результаты проекта и получить решение о запуске следующего этапа.");
  await page.getByLabel(/Исходные материалы/).fill("В пилоте участвовали 4 отдела. Срок обработки сократился на 27%.");
  await expect(page.getByRole("button", { name: "Создать три варианта" })).toBeEnabled();
  await page.getByRole("button", { name: "Создать три варианта" }).click();

  // Задача Q: одна кнопка создаёт три задания (по стилю), экран пакета
  // показывает прогресс каждого отдельно, задания идут параллельно.
  await page.waitForURL(/\/batches\/[a-f0-9]+$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { level: 1, name: "Три стиля, три презентации" })).toBeVisible();
  await expect(page.locator(".variant-card")).toHaveCount(3);
  await expect(page.locator(".stage-track .stage").first()).toBeVisible();

  // Каждое задание укладывается в пять минут по ТЗ; ждём превью всех трёх.
  await expect(page.locator(".variant-card .preview img")).toHaveCount(3, { timeout: 5 * 60 * 1000 });
  for (const label of ["Плотный", "Воздушный", "Визуальный"]) {
    await expect(page.getByRole("heading", { level: 2, name: label })).toBeVisible();
  }

  await page.locator(".variant-card").first().getByRole("link", { name: "Проверить вариант" }).click();
  await expect(page.getByRole("heading", { name: "Проверьте презентацию" })).toBeVisible();
  await expect(page.locator(".slide-preview-wrap img")).toBeVisible();

  const thumbs = page.locator(".audit-stage .thumb");
  let foundBox = false;
  for (let index = 0; index < await thumbs.count() && !foundBox; index += 1) {
    await thumbs.nth(index).click();
    if (await page.locator(".finding-box").count()) foundBox = true;
  }
  expect(foundBox).toBe(true);
  await expect(page.getByRole("link", { name: "Скачать PowerPoint" })).toHaveAttribute("href", /format=pptx/);
});

test("настройки и управление с клавиатуры", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveTitle("Донор — презентации по вашему образцу");
  await expect(page.getByRole("link", { name: "Донор — на главную" })).toBeVisible();
  await expect(page.locator('link[rel="icon"]')).toHaveCount(2);
  await page.getByRole("button", { name: /настройки/i }).click();
  await expect(page.getByRole("dialog", { name: "Настройки" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Создание презентации" })).toBeVisible();
  await page.getByLabel("Количество слайдов по умолчанию").selectOption("12");
  await page.getByLabel("Предпочтительный формат").selectOption("pdf");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Настройки" })).toBeHidden();
  await page.goto("/templates/settings-test/brief");
  await expect(page.getByRole("textbox", { name: "Количество слайдов", exact: true })).toHaveValue("12");
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus-visible")).toBeVisible();
});

test("новая презентация требует подтверждения", async ({ page }) => {
  await page.goto("/templates/ui-test-template/brief");
  await page.getByRole("button", { name: "Новая презентация" }).click();
  const dialog = page.getByRole("dialog", { name: "Начать новую презентацию?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Остаться" }).click();
  await expect(page).toHaveURL(/\/templates\/ui-test-template\/brief$/);
  await page.getByRole("button", { name: "Новая презентация" }).click();
  await dialog.getByRole("button", { name: "Начать новую" }).click();
  await expect(page).toHaveURL(/\/$/);
});

test("пустая форма, черновик и проверка числа слайдов", async ({ page }) => {
  const templateId = "ui-test-template";
  await page.goto(`/templates/${templateId}/brief`);

  const title = page.getByLabel(/Название презентации/);
  const task = page.getByLabel(/Задача презентации/);
  const sources = page.getByLabel(/Исходные материалы/);
  const slides = page.getByRole("textbox", { name: "Количество слайдов", exact: true });
  const submit = page.getByRole("button", { name: "Создать три варианта" });

  await expect(title).toHaveValue("");
  await expect(task).toHaveValue("");
  await page.getByRole("button", { name: "Пример: заявки" }).click();
  await expect(task).not.toHaveValue("");
  await page.getByRole("button", { name: "Очистить поля" }).click();
  await expect(task).toHaveValue("");
  await expect(page.getByRole("radio", { name: /Все три стиля/ })).toBeChecked();
  await expect(sources).toHaveValue("");
  await expect(submit).toBeDisabled();

  await title.fill("Сохранённое название");
  await task.fill("Показать команде итоги и согласовать следующий этап.");
  await sources.fill("Обработано 410 заявок.");
  await slides.fill("99");
  await expect(page.getByText("Введите число от 4 до 15.")).toBeVisible();
  await expect(submit).toBeDisabled();
  await slides.fill("12");
  await expect(submit).toBeEnabled();
  await page.waitForTimeout(300);
  await page.reload();
  await expect(title).toHaveValue("Сохранённое название");
  await expect(task).toHaveValue(/Показать команде итоги/);
  await expect(slides).toHaveValue("12");

  // Задача Q: одна кнопка создаёт три задания (по стилю), экран пакета
  // показывает прогресс каждого отдельно, задания идут параллельно.
  await page.waitForURL(/\/batches\/[a-f0-9]+$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { level: 1, name: "Три стиля, три презентации" })).toBeVisible();
  await expect(page.locator(".variant-card")).toHaveCount(3);
  await expect(page.locator(".stage-track .stage").first()).toBeVisible();

  // Каждое задание укладывается в пять минут по ТЗ; ждём превью всех трёх.
  await expect(page.locator(".variant-card .preview img")).toHaveCount(3, { timeout: 5 * 60 * 1000 });
  for (const label of ["Плотный", "Воздушный", "Визуальный"]) {
    await expect(page.getByRole("heading", { level: 2, name: label })).toBeVisible();
  }
});
