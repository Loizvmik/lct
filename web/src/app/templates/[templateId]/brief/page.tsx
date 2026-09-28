"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import PhotoPicker, { PickedPhoto, toPicked } from "@/components/PhotoPicker";
import { createDeckBatch, healthcheck, loadExamplePhotos, VariantName, VARIANT_DESCRIPTIONS, VARIANT_LABELS, VARIANT_ORDER } from "@/lib/api";
import { getAppSettings } from "@/lib/appSettings";
import { clearBriefDraft, loadBriefDraft, saveBriefDraft } from "@/lib/briefDraft";
import { EXAMPLES } from "@/lib/exampleContent";
import { getYandexCredentials, subscribeToYandexCredentials } from "@/lib/yandexCredentials";

const MIN_SLIDES = 4;
const MAX_SLIDES = 15;

type StyleChoice = VariantName | "all";

export default function BriefPage() {
  const router = useRouter();
  const params = useParams<{ templateId: string }>();
  const [title, setTitle] = useState("");
  const [brief, setBrief] = useState("");
  const [sources, setSources] = useState("");
  const [targetSlides, setTargetSlides] = useState<number | "">("");
  const [autofix, setAutofix] = useState(true);
  // Фото в черновик не пишутся: `photo_id` живут в памяти сервиса, и после
  // его перезапуска черновик ссылался бы на пропавшие файлы.
  const [photos, setPhotos] = useState<PickedPhoto[]>([]);
  // Номер последней загрузки фото примера: ответ, пришедший после
  // «Очистить поля» или другого примера, не должен вернуть старые фото.
  const exampleRequest = useRef(0);
  // Предупреждение «модели не будет»: на сервере нет ключа, и свой ключ
  // человек не вставил. Кнопку не блокируем, запасной путь тоже соберёт файл.
  const [serverKey, setServerKey] = useState<boolean | null>(null);
  const [ownKey, setOwnKey] = useState(false);
  useEffect(() => {
    let alive = true;
    healthcheck()
      .then((health) => { if (alive) setServerKey(health.server_key ?? null); })
      .catch(() => undefined);
    const readOwnKey = () => setOwnKey(getYandexCredentials() !== null);
    const timer = window.setTimeout(readOwnKey, 0);
    const unsubscribe = subscribeToYandexCredentials(readOwnKey);
    return () => {
      alive = false;
      window.clearTimeout(timer);
      unsubscribe();
    };
  }, []);
  // Задача Q: «все» означает три стиля тремя заданиями одной кнопкой.
  const [style, setStyle] = useState<StyleChoice>("all");
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
    setAutofix(settings.defaultAutofix); setError(null); setPhotos([]);
    exampleRequest.current += 1;
    clearBriefDraft(params.templateId);
  }

  // Пример из защиты (маршрутизация заявок): с ним тестовый прогон не
  // требует придумывать бриф, а главное, у модели есть исходные цифры.
  // Черновиком по умолчанию он не становится: `briefDraft` намеренно не
  // подставляет встроенный пример вместо пустой формы.
  // Фото примера лежат в том же контент-пакете на сервере; если сервис
  // их не отдал, пример всё равно заполняется текстом.
  async function fillExample(name: keyof typeof EXAMPLES = "queue-latency") {
    const example = EXAMPLES[name];
    setTitle(example.title);
    setBrief(example.brief);
    setSources(example.sources);
    setTargetSlides(example.targetSlides);
    setError(null);
    setPhotos([]);
    const request = ++exampleRequest.current;
    try {
      const loaded = toPicked(await loadExamplePhotos(name));
      if (request === exampleRequest.current) setPhotos(loaded);
    } catch {
      // без фото пример остаётся рабочим
    }
  }

  async function submit() {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    saveBriefDraft(params.templateId, { title, brief, sources, targetSlides, autofix });
    try {
      // Один запрос создаёт по заданию на стиль: они идут параллельно, у
      // каждого свой бюджет, а структура презентации считается один раз.
      const { batch_id } = await createDeckBatch({
        template_id: params.templateId,
        brief: brief.trim(),
        sources: sources.trim() ? [sources.trim()] : [],
        title: title.trim() || undefined,
        target_slides: targetSlides === "" ? undefined : targetSlides,
        autofix,
        styles: style === "all" ? VARIANT_ORDER : [style],
        photos: photos.map((p) => ({ photo_id: p.photo_id, caption: p.caption.trim() || undefined })),
      });
      router.push(`/batches/${batch_id}`);
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

        <PhotoPicker photos={photos} onChange={setPhotos} disabled={busy} />

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
            <p className="field-hint" id="slides-hint">Оставьте пустым — количество подберётся по объёму материалов. Допустимо от 4 до 15.</p>
            {slideError && <p className="field-error" id="slides-error">Введите число от 4 до 15.</p>}
          </div>
          <div className="field">
            <span className="field-label">Проверка оформления</span>
            <label className="check-row">
              <input type="checkbox" checked={autofix} onChange={(e) => setAutofix(e.target.checked)} />
              <span><strong>Исправлять найденные проблемы автоматически</strong><br /><span className="muted small">Применятся только безопасные исправления. Остальные замечания останутся для проверки.</span></span>
            </label>
          </div>
        </div>

        <fieldset className="field choice-list">
          <legend className="field-label">Стиль</legend>
          <label className="check-row">
            <input type="radio" name="style" value="all" checked={style === "all"} onChange={() => setStyle("all")} />
            <span><strong>Все три стиля</strong><br /><span className="muted small">Три презентации собираются параллельно, у каждой свой бюджет пяти минут.</span></span>
          </label>
          {VARIANT_ORDER.map((name) => (
            <label className="check-row" key={name}>
              <input type="radio" name="style" value={name} checked={style === name} onChange={() => setStyle(name)} />
              <span><strong>Только {VARIANT_LABELS[name].toLowerCase()}</strong><br /><span className="muted small">{VARIANT_DESCRIPTIONS[name]}</span></span>
            </label>
          ))}
        </fieldset>
      </section>

      {serverKey === false && !ownKey && (
        <p className="notice" role="note">
          На сервере не настроен ключ модели. Добавьте свой ключ Yandex AI Studio в настройках, иначе презентация
          соберётся без модели и будет хуже.
        </p>
      )}
      <div className="row-actions">
        <button type="button" onClick={submit} disabled={!canSubmit}>
          {busy ? "Начинаем…" : style === "all" ? "Создать три варианта" : "Создать презентацию"}
        </button>
        <button type="button" className="secondary" onClick={() => fillExample("queue-latency")} disabled={busy}>
          Пример: заявки
        </button>
        <button type="button" className="secondary" onClick={() => fillExample("edu-platform")} disabled={busy}>
          Пример: учебная платформа
        </button>
        <button type="button" className="secondary" onClick={clearForm} disabled={busy}>Очистить поля</button>
      </div>
    </div>
  );
}
