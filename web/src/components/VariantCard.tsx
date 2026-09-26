"use client";

// Карточка одной готовой презентации: превью, замечания, оценки, верность
// шаблону, как собраны слайды, время и режим, ссылки на проверку и
// скачивание. С задачи Q у каждой презентации своё задание (`jobId`),
// поэтому ссылки строятся от него, а не от общей колоды на три варианта.
import Link from "next/link";
import { useEffect, useState } from "react";
import { PreviewImage } from "@/components/PreviewImage";
import {
  assetUrl,
  exportUrl,
  JobResponse,
  JOB_STATUS_LABELS,
  LADDER_TITLES,
  MODE_LABELS,
  SEVERITY_LABELS,
  VariantSummary,
  VARIANT_DESCRIPTIONS,
  VARIANT_LABELS,
} from "@/lib/api";
import { EXPORT_FORMATS, ExportFormat, getAppSettings, subscribeToAppSettings } from "@/lib/appSettings";

function formatScore(value: number): string {
  return value.toFixed(1).replace(".", ",");
}

// Предпочитаемый формат скачивания из настроек; до первого чтения
// localStorage стоит pptx, чтобы разметка сервера и клиента совпала.
export function usePreferredFormat(): ExportFormat {
  const [preferred, setPreferred] = useState<ExportFormat>("pptx");
  useEffect(() => {
    const timer = window.setTimeout(() => setPreferred(getAppSettings().preferredExportFormat), 0);
    const unsubscribe = subscribeToAppSettings((settings) => setPreferred(settings.preferredExportFormat));
    return () => { window.clearTimeout(timer); unsubscribe(); };
  }, []);
  return preferred;
}

// Подписи стадий в шкале прогресса глагольные («Читаем шаблон»), а в
// сводке нужен предмет: что именно задание получило готовым.
const SHARED_STAGE_LABELS: Record<string, string> = {
  parse: "разбор шаблона",
  outline: "план презентации",
  write: "тексты слайдов",
};

// Строки сводки задания: время из бюджета, режим, переиспользованные
// стадии. Пустые поля пропускаются, а не показываются прочерком.
export function jobFacts(job: JobResponse): { label: string; value: string }[] {
  const facts: { label: string; value: string }[] = [];
  if (job.seconds != null) {
    const limit = job.budget?.budget_seconds ?? 300;
    facts.push({ label: "Время", value: `${Math.round(job.seconds)} с из ${Math.round(limit)} с` });
  }
  if (job.mode) facts.push({ label: "Режим", value: MODE_LABELS[job.mode] ?? job.mode });
  const shared = Object.keys(job.budget?.shared_stages ?? {});
  if (shared.length) {
    facts.push({
      label: "Взято готовым",
      value: shared.map((s) => SHARED_STAGE_LABELS[s] ?? s).join(", "),
    });
  }
  return facts;
}

export default function VariantCard({
  jobId,
  variant,
  job,
}: {
  jobId: string;
  variant: VariantSummary;
  job?: JobResponse | null;
}) {
  const [activeSlide, setActiveSlide] = useState(0);
  const preferredFormat = usePreferredFormat();
  const slideIndex = Math.min(activeSlide, Math.max(variant.preview_pngs.length - 1, 0));
  const label = VARIANT_LABELS[variant.variant];
  const downloadFormats = [...EXPORT_FORMATS].sort(
    (a, b) => Number(b.value === preferredFormat) - Number(a.value === preferredFormat),
  );

  const facts = job ? jobFacts(job) : [];
  if (variant.content_avg != null || variant.design_avg != null) {
    facts.push({
      label: "Оценка модели",
      value: [
        variant.content_avg != null && `содержание ${formatScore(variant.content_avg)}`,
        variant.design_avg != null && `дизайн ${formatScore(variant.design_avg)}`,
      ].filter(Boolean).join(", "),
    });
  }
  if (variant.fidelity?.summary) facts.push({ label: "Верность шаблону", value: variant.fidelity.summary });
  const ladder = job?.ladder?.[variant.variant];
  const ladderParts = Object.entries(ladder ?? {})
    .filter(([, count]) => count > 0)
    .map(([rung, count]) => `${count} ${LADDER_TITLES[rung] ?? rung}`);
  if (ladderParts.length) facts.push({ label: "Как собраны слайды", value: ladderParts.join(", ") });
  const structural = job?.structural?.[variant.variant] ?? [];

  return (
    <article className="variant-card">
      <header>
        <div>
          <h2>{label}</h2>
          <p className="muted small">{VARIANT_DESCRIPTIONS[variant.variant]}</p>
          {job?.status === "done_with_warnings" && (
            <span className="pill status-pill warn">{JOB_STATUS_LABELS.done_with_warnings}</span>
          )}
        </div>
        <span className="slide-count" aria-label={`Количество слайдов: ${variant.slide_count}`}>
          <strong>{variant.slide_count}</strong>
          <span>слайдов</span>
        </span>
      </header>
      <div>
        <div className="preview">
          {variant.preview_pngs[slideIndex]
            ? <PreviewImage src={assetUrl(variant.preview_pngs[slideIndex])} alt={`${label}, слайд ${slideIndex + 1}`} />
            : <div className="preview-fallback">Предпросмотр пока недоступен</div>}
        </div>
        <div className="thumb-strip" aria-label={`Слайды варианта «${label}»`}>
          {variant.preview_pngs.map((png, index) => (
            <button
              type="button"
              className={`thumb${index === slideIndex ? " active" : ""}`}
              key={png}
              onClick={() => setActiveSlide(index)}
              aria-label={`Показать слайд ${index + 1}`}
              aria-pressed={index === slideIndex}
            >
              <PreviewImage src={assetUrl(png)} alt="" />
              <span className="slide-number">{index + 1}</span>
            </button>
          ))}
        </div>
      </div>
      <div className="body">
        <div className="severity-counts" aria-label="Результаты проверки">
          {Object.entries(variant.by_severity).filter(([, count]) => count > 0).map(([level, count]) => (
            <span className={`count ${level}`} key={level}>{SEVERITY_LABELS[level] ?? "Замечания"}: {count}</span>
          ))}
          {variant.findings.length === 0 && <span className="count minor">Замечаний нет</span>}
        </div>
        {variant.autofixed_count > 0 && <p className="field-hint">Уже исправлено автоматически: {variant.autofixed_count}.</p>}
        {facts.length > 0 && (
          <dl className="variant-facts">
            {facts.map((fact) => (
              <div key={fact.label}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>
            ))}
          </dl>
        )}
        {structural.length > 0 && (
          <>
            <p className="field-label">Нужен другой текст или раскладка</p>
            <ul className="structural-list">
              {structural.map((item, index) => (
                <li key={`${item.check_id}-${item.slide_index}-${index}`}>
                  {item.slide_index != null && `Слайд ${item.slide_index + 1}: `}{item.message}
                </li>
              ))}
            </ul>
          </>
        )}
        <div className="row-actions">
          <Link className="button" href={`/decks/${jobId}/audit?variant=${variant.variant}`}>Проверить вариант</Link>
        </div>
        <div className="row-actions" aria-label="Скачать вариант">
          {downloadFormats.map((format) => (
            <a
              className={`button secondary${format.value === preferredFormat ? " preferred-download" : ""}`}
              href={exportUrl(jobId, variant.variant, format.value)}
              key={format.value}
            >
              {format.label}
            </a>
          ))}
        </div>
      </div>
    </article>
  );
}
