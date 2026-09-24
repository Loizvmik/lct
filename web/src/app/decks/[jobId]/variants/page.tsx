"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { PreviewImage } from "@/components/PreviewImage";
import { assetUrl, exportUrl, getJob, getVariants, SEVERITY_LABELS, VariantSummary, VARIANT_LABELS } from "@/lib/api";

const DESCRIPTIONS = {
  dense: "Больше фактов и деталей на каждом слайде.",
  airy: "Крупнее текст, больше свободного пространства.",
  visual: "Больше визуальных акцентов и короче формулировки.",
} as const;

export default function VariantsPage() {
  const params = useParams<{ jobId: string }>();
  const [variants, setVariants] = useState<VariantSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeSlide, setActiveSlide] = useState<Record<string, number>>({});
  const [templateId, setTemplateId] = useState<string | null>(null);

  useEffect(() => {
    getVariants(params.jobId).then(setVariants).catch((err: Error) => setError(err.message));
    getJob(params.jobId).then((job) => setTemplateId(job.template_id)).catch(() => undefined);
  }, [params.jobId]);

  if (error) return <div className="error-banner" role="alert">{error}</div>;
  if (!variants) return <p className="muted"><span className="spinner" /> Загружаем варианты…</p>;

  return (
    <div>
      <header className="page-header">
        <p className="eyebrow">Шаг 3 из 4</p>
        <h1>Выберите подачу</h1>
        <p className="lead">Содержание одинаковое, меняются плотность и визуальный ритм. Откройте любой вариант для подробной проверки.</p>
      </header>

      {templateId && <p><Link href={`/templates/${templateId}/brief`}>← Изменить задание и создать заново</Link></p>}

      <div className="grid-3">
        {variants.map((variant) => {
          const slideIndex = Math.min(activeSlide[variant.variant] ?? 0, Math.max(variant.preview_pngs.length - 1, 0));
          return (
            <article className="variant-card" key={variant.variant}>
              <header>
                <div><h2>{VARIANT_LABELS[variant.variant]}</h2><p className="muted small">{DESCRIPTIONS[variant.variant]}</p></div>
                <span className="pill">{variant.slide_count} слайдов</span>
              </header>
              <div>
                <div className="preview">
                  {variant.preview_pngs[slideIndex]
                    ? <PreviewImage src={assetUrl(variant.preview_pngs[slideIndex])} alt={`${VARIANT_LABELS[variant.variant]}, слайд ${slideIndex + 1}`} />
                    : <div className="preview-fallback">Предпросмотр пока недоступен</div>}
                </div>
                <div className="thumb-strip" aria-label={`Слайды варианта «${VARIANT_LABELS[variant.variant]}»`}>
                  {variant.preview_pngs.map((png, index) => (
                    <button
                      type="button"
                      className={`thumb${index === slideIndex ? " active" : ""}`}
                      key={png}
                      onClick={() => setActiveSlide((prev) => ({ ...prev, [variant.variant]: index }))}
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
                <div className="row-actions">
                  <Link className="button" href={`/decks/${params.jobId}/audit?variant=${variant.variant}`}>Проверить вариант</Link>
                </div>
                <div className="row-actions" aria-label="Скачать вариант">
                  <a className="button secondary" href={exportUrl(params.jobId, variant.variant, "pptx")}>PowerPoint</a>
                  <a className="button secondary" href={exportUrl(params.jobId, variant.variant, "pdf")}>PDF</a>
                  <a className="button secondary" href={exportUrl(params.jobId, variant.variant, "html")}>Веб-версия</a>
                </div>
              </div>
            </article>
          );
        })}
      </div>
    </div>
  );
}
