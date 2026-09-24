"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { createDeck } from "@/lib/api";
import { getAppSettings } from "@/lib/appSettings";
import { clearBriefDraft, loadBriefDraft, saveBriefDraft } from "@/lib/briefDraft";

const MIN_SLIDES = 10;
const MAX_SLIDES = 15;

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
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const draft = loadBriefDraft(params.templateId);
      if (draft) {
        setTitle(draft.title);
        setBrief(draft.brief);
        setSources(draft.sources);
        setTargetSlides(draft.targetSlides);
        setAutofix(draft.autofix);
      } else {
        const settings = getAppSettings();
        setTargetSlides(settings.defaultSlideCount === "auto" ? "" : settings.defaultSlideCount);
        setAutofix(settings.defaultAutofix);
      }
      setReady(true);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [params.templateId]);

  useEffect(() => {
    if (!ready) return;
    const timer = window.setTimeout(() => {
      saveBriefDraft(params.templateId, { title, brief, sources, targetSlides, autofix });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [ready, params.templateId, title, brief, sources, targetSlides, autofix]);

  const slideError = targetSlides !== "" && (targetSlides < MIN_SLIDES || targetSlides > MAX_SLIDES);
  const canSubmit = brief.trim().length > 0 && !slideError && !busy;

  function changeSlides(next: number | "") {
    if (next === "") setTargetSlides("");
    else setTargetSlides(Math.max(MIN_SLIDES, Math.min(MAX_SLIDES, next)));
  }

  function clearForm() {
    const settings = getAppSettings();
    setTitle(""); setBrief(""); setSources("");
    setTargetSlides(settings.defaultSlideCount === "auto" ? "" : settings.defaultSlideCount);
    setAutofix(settings.defaultAutofix); setError(null);
    clearBriefDraft(params.templateId);
  }

  async function submit() {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    saveBriefDraft(params.templateId, { title, brief, sources, targetSlides, autofix });
    try {
      const { job_id } = await createDeck({
        template_id: params.templateId,
        brief: brief.trim(),
        sources: sources.trim() ? [sources.trim()] : [],
        title: title.trim() || undefined,
        target_slides: targetSlides === "" ? undefined : targetSlides,
        autofix,
      });
      router.push(`/decks/${job_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось начать создание презентации.");
      setBusy(false);
    }
  }

  return (
    <div>
      <header className="page-header">
        <p className="eyebrow">Шаг 2 из 4</p>
        <h1>Опишите задачу</h1>
        <p className="lead">
          Расскажите, для кого нужна презентация и к какому решению она должна привести. Чем точнее исходные данные, тем полезнее будет результат.
        </p>
      </header>

      {error && <div className="error-banner" role="alert">{error}</div>}

      <section className="card">
        <div className="field">
          <label htmlFor="title">Название презентации <span className="counter">{title.length} знаков</span></label>
          <input id="title" type="text" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Например: Итоги проекта за третий квартал" />
          <p className="field-hint">Можно оставить пустым — название появится из описания задачи.</p>
        </div>

        <div className="field">
          <label htmlFor="brief">Задача презентации <span className="counter">{brief.length} знаков</span></label>
          <textarea
            id="brief"
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
            aria-describedby={brief.trim() || !ready ? "brief-hint" : "brief-hint brief-error"}
            aria-invalid={!brief.trim() && ready}
            placeholder="Кто увидит презентацию? Что аудитория уже знает? Какое решение нужно принять?"
          />
          <p className="field-hint" id="brief-hint">Обязательное поле. Укажите аудиторию, цель и желаемый тон.</p>
          {!brief.trim() && ready && <p className="field-error" id="brief-error">Добавьте описание задачи, чтобы продолжить.</p>}
        </div>

        <div className="field">
          <label htmlFor="sources">Исходные материалы <span className="counter">{sources.length} знаков</span></label>
          <textarea id="sources" value={sources} onChange={(e) => setSources(e.target.value)} placeholder="Вставьте факты, цифры, выдержки из документов и ссылки на источники." />
          <p className="field-hint">Необязательно. Числа из этого поля можно будет проверить на последнем шаге.</p>
        </div>

        <div className="grid-2">
          <div className="field">
            <span className="field-label" id="slides-label">Количество слайдов</span>
            <div className="number-control" role="group" aria-labelledby="slides-label">
              <button type="button" className="secondary" onClick={() => changeSlides(targetSlides === "" ? MIN_SLIDES : targetSlides - 1)} disabled={targetSlides === MIN_SLIDES} aria-label="Уменьшить количество слайдов">−</button>
              <input
                className="number-input"
                type="text"
                inputMode="numeric"
                value={targetSlides}
                aria-label="Количество слайдов"
                aria-describedby={slideError ? "slides-hint slides-error" : "slides-hint"}
                aria-invalid={slideError}
                placeholder="Авто"
                onChange={(e) => {
                  const raw = e.target.value.replace(/\D/g, "");
                  setTargetSlides(raw === "" ? "" : Number(raw));
                }}
                onBlur={() => { if (targetSlides !== "") changeSlides(targetSlides); }}
              />
              <button type="button" className="secondary" onClick={() => changeSlides(targetSlides === "" ? MIN_SLIDES : targetSlides + 1)} disabled={targetSlides === MAX_SLIDES} aria-label="Увеличить количество слайдов">+</button>
            </div>
            <p className="field-hint" id="slides-hint">Оставьте пустым для автоматического выбора. Допустимо от 10 до 15.</p>
            {slideError && <p className="field-error" id="slides-error">Введите число от 10 до 15.</p>}
          </div>
          <div className="field">
            <span className="field-label">Проверка оформления</span>
            <label className="check-row">
              <input type="checkbox" checked={autofix} onChange={(e) => setAutofix(e.target.checked)} />
              <span><strong>Исправлять найденные проблемы автоматически</strong><br /><span className="muted small">Применятся только безопасные исправления. Остальные замечания останутся для проверки.</span></span>
            </label>
          </div>
        </div>
      </section>

      <div className="row-actions">
        <button type="button" onClick={submit} disabled={!canSubmit}>{busy ? "Начинаем…" : "Создать три варианта"}</button>
        <button type="button" className="secondary" onClick={clearForm} disabled={busy}>Очистить поля</button>
      </div>
    </div>
  );
}
