"use client";

import { useRouter, useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { createDeck } from "@/lib/api";
import { loadBriefDraft, saveBriefDraft } from "@/lib/briefDraft";
import { EXAMPLE_BRIEF, EXAMPLE_SOURCES, EXAMPLE_TARGET_SLIDES, EXAMPLE_TITLE } from "@/lib/exampleContent";

export default function BriefPage() {
  const router = useRouter();
  const params = useParams<{ templateId: string }>();
  const [title, setTitle] = useState("");
  const [brief, setBrief] = useState("");
  const [sources, setSources] = useState("");
  const [targetSlides, setTargetSlides] = useState<number | "">("");
  const [autofix, setAutofix] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Task 16, п.1: если сюда вернулись с экрана вариантов/аудита («изменить
  // бриф и перегенерировать»), форма не должна быть пустой — подставляем
  // последний черновик для этого шаблона. Шаблон при этом не перезагружаем:
  // `template_id` в URL тот же, профиль уже лежит в кеше API.
  useEffect(() => {
    const draft = loadBriefDraft(params.templateId);
    if (draft) {
      setTitle(draft.title);
      setBrief(draft.brief);
      setSources(draft.sources);
      setTargetSlides(draft.targetSlides);
      setAutofix(draft.autofix);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.templateId]);

  function fillExample() {
    setTitle(EXAMPLE_TITLE);
    setBrief(EXAMPLE_BRIEF);
    setSources(EXAMPLE_SOURCES);
    setTargetSlides(EXAMPLE_TARGET_SLIDES);
  }

  async function submit() {
    if (!brief.trim()) {
      setError("Бриф не может быть пустым");
      return;
    }
    setBusy(true);
    setError(null);
    saveBriefDraft(params.templateId, { title, brief, sources, targetSlides, autofix });
    try {
      const { job_id } = await createDeck({
        template_id: params.templateId,
        brief,
        sources: sources.trim() ? [sources] : [],
        title: title.trim() || undefined,
        target_slides: targetSlides === "" ? undefined : Number(targetSlides),
        autofix,
      });
      router.push(`/decks/${job_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось запустить генерацию");
      setBusy(false);
    }
  }

  return (
    <div>
      <h1>Шаг 2 — бриф и материалы</h1>
      <p className="muted">
        Опишите, о чём презентация и для какой аудитории, и приложите исходные цифры/факты —
        генератор пишет текст слайдов по этим материалам и ссылается на них при сверке
        цифр аудитом.
      </p>

      {error && <div className="error-banner">{error}</div>}

      <div className="row-actions example-fill">
        <button type="button" className="secondary" onClick={fillExample} disabled={busy}>
          Заполнить примером (маршрутизация заявок)
        </button>
      </div>

      <div className="card">
        <label>Название презентации</label>
        <input
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Например, «Сокращение времени согласования заявок»"
        />

        <label>Бриф — о чём презентация, для кого, какой тон</label>
        <textarea value={brief} onChange={(e) => setBrief(e.target.value)} placeholder="Просим комитет согласовать запуск автоматической маршрутизации заявок…" />
        <p className="field-hint">
          Пишите как задачу живому человеку: чего вы хотите от аудитории, кто она, какой тон.
          <br />
          <span className="bad">«Презентация про маршрутизацию заявок»</span> — из этого не следует структура слайдов.
          <br />
          <span className="good">
            «Просим комитет согласовать запуск, показать где уходит время и что дал пилот, аудитория знает
            предметную область»
          </span>{" "}
          — следует.
        </p>

        <label>Исходные материалы — цифры, факты, ссылки</label>
        <textarea value={sources} onChange={(e) => setSources(e.target.value)} placeholder="Выборка: 1240 заявок, медиана ожидания 18 часов…" />
        <p className="field-hint warn">
          Это единственный источник цифр для слайдов — модели прямо запрещено придумывать числа, она
          берёт их только отсюда. Пустое поле даёт текст из общих слов, без фактуры. Аудит («все цифры со
          слайда есть в исходных материалах») тоже сверяется с этим полем — без него ему не с чем сверять.
          <br />
          Кладите сюда сырые данные как есть, не причёсывая: таблицы, замеры, суммы, сроки.
        </p>

        <div className="grid-2">
          <div>
            <label>Целевое число слайдов (10–15, по умолчанию решает генератор)</label>
            <input
              type="number"
              min={10}
              max={15}
              value={targetSlides}
              onChange={(e) => setTargetSlides(e.target.value === "" ? "" : Number(e.target.value))}
            />
            <p className="field-hint">
              Можно оставить пустым — это нормально, генератор сам уложится в требование конкурса:
              от 10 до 15 слайдов.
            </p>
          </div>
          <div>
            <label>Починка находок</label>
            <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8 }}>
              <input type="checkbox" checked={autofix} onChange={(e) => setAutofix(e.target.checked)} style={{ width: "auto" }} />
              <span style={{ color: "var(--text)" }}>
                Автоматически починить всё, что чинится (рекомендуется для демонстрации)
              </span>
            </label>
          </div>
        </div>
      </div>

      <div className="row-actions">
        <button onClick={submit} disabled={busy}>
          {busy ? "Запускаем…" : "Сгенерировать три варианта →"}
        </button>
      </div>
    </div>
  );
}
