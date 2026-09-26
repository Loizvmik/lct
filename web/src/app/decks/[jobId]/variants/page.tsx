"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import {
  assetUrl,
  exportUrl,
  getJob,
  getVariants,
  SEVERITY_LABELS,
  VariantSummary,
  VARIANT_LABELS,
} from "@/lib/api";

export default function VariantsPage() {
  const params = useParams<{ jobId: string }>();
  const [variants, setVariants] = useState<VariantSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeSlide, setActiveSlide] = useState<Record<string, number>>({});
  const [templateId, setTemplateId] = useState<string | null>(null);

  useEffect(() => {
    getVariants(params.jobId).then(setVariants).catch((err) => setError(err.message));
    getJob(params.jobId).then((job) => setTemplateId(job.template_id)).catch(() => undefined);
  }, [params.jobId]);

  if (error) return <div className="error-banner">{error}</div>;
  if (!variants) return <p className="muted">Загружаем варианты…</p>;

  return (
    <div>
      <h1>Шаг 3 — три варианта вёрстки, один и тот же контент</h1>
      <p className="muted">
        Плотный (максимум фактов на слайд), воздушный (акцент на читаемость и паузы),
        визуальный (упор на изображения/иконографику) — сравните и выберите, с каким
        работать дальше на экране аудита.
      </p>

      <div className="row-actions" style={{ marginBottom: 16 }}>
        {templateId && (
          <Link className="button secondary" href={`/templates/${templateId}/brief`}>
            ← Изменить бриф и сгенерировать заново
          </Link>
        )}
      </div>

      <div className="grid-3">
        {variants.map((variant) => {
          const slideIdx = activeSlide[variant.variant] ?? 0;
          const totalFindings = variant.findings.length;
          return (
            <div className="variant-card" key={variant.variant}>
              <header>
                <strong>{VARIANT_LABELS[variant.variant]}</strong>
                <span className="pill">{variant.slide_count} слайдов</span>
              </header>
              <div className="preview">
                {variant.preview_pngs[slideIdx] && (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={assetUrl(variant.preview_pngs[slideIdx])} alt={`${variant.variant} слайд ${slideIdx + 1}`} />
                )}
              </div>
              <div className="thumb-strip" style={{ padding: "8px 12px" }}>
                {variant.preview_pngs.map((png, idx) => (
                  <div
                    className={`thumb${idx === slideIdx ? " active" : ""}`}
                    key={png}
                    onClick={() => setActiveSlide((prev) => ({ ...prev, [variant.variant]: idx }))}
                  >
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={assetUrl(png)} alt={`слайд ${idx + 1}`} />
                  </div>
                ))}
              </div>
              <div className="body">
                <div className="severity-counts">
                  {Object.entries(variant.by_severity).map(([sev, count]) => (
                    <span className={`count ${sev}`} key={sev}>
                      {SEVERITY_LABELS[sev] ?? sev}: {count}
                    </span>
                  ))}
                  {totalFindings === 0 && <span className="count minor">Находок нет</span>}
                </div>
                {variant.autofixed_count > 0 && (
                  <p className="muted" style={{ fontSize: 12 }}>
                    Автопочинка уже применила {variant.autofixed_count} исправлений.
                  </p>
                )}
                {(variant.content_avg != null || variant.design_avg != null) && (
                  <p className="muted" style={{ fontSize: 12 }}>
                    Оценка модели:{" "}
                    {[
                      variant.content_avg != null && `содержание ${variant.content_avg.toFixed(1).replace(".", ",")}`,
                      variant.design_avg != null && `дизайн ${variant.design_avg.toFixed(1).replace(".", ",")}`,
                    ]
                      .filter(Boolean)
                      .join(" / ")}
                  </p>
                )}
                <div className="row-actions" style={{ marginTop: "auto" }}>
                  <Link className="button" href={`/decks/${params.jobId}/audit?variant=${variant.variant}`}>
                    Аудит и починка ({totalFindings})
                  </Link>
                </div>
                <div className="row-actions">
                  <a className="button secondary" href={exportUrl(params.jobId, variant.variant, "pptx")}>
                    .pptx
                  </a>
                  <a className="button secondary" href={exportUrl(params.jobId, variant.variant, "pdf")}>
                    .pdf
                  </a>
                  <a className="button secondary" href={exportUrl(params.jobId, variant.variant, "html")}>
                    .html
                  </a>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
