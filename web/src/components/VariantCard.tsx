"use client";

// Карточка одной готовой презентации: превью, находки, оценки, верность
// шаблону, ссылки на аудит и скачивание. С задачи Q у каждой презентации
// своё задание (`jobId`), поэтому ссылки строятся от него, а не от общей
// колоды на три варианта.
import Link from "next/link";
import { useState } from "react";
import { assetUrl, exportUrl, SEVERITY_LABELS, VariantSummary, VARIANT_LABELS } from "@/lib/api";

export default function VariantCard({
  jobId,
  variant,
  footer,
}: {
  jobId: string;
  variant: VariantSummary;
  footer?: React.ReactNode;
}) {
  const [slideIdx, setSlideIdx] = useState(0);
  const totalFindings = variant.findings.length;
  return (
    <div className="variant-card">
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
          <div className={`thumb${idx === slideIdx ? " active" : ""}`} key={png} onClick={() => setSlideIdx(idx)}>
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
        {variant.fidelity?.summary && (
          <p className="muted" style={{ fontSize: 12 }}>
            {variant.fidelity.summary}
          </p>
        )}
        {footer}
        <div className="row-actions" style={{ marginTop: "auto" }}>
          <Link className="button" href={`/decks/${jobId}/audit?variant=${variant.variant}`}>
            Аудит и починка ({totalFindings})
          </Link>
        </div>
        <div className="row-actions">
          <a className="button secondary" href={exportUrl(jobId, variant.variant, "pptx")}>
            .pptx
          </a>
          <a className="button secondary" href={exportUrl(jobId, variant.variant, "pdf")}>
            .pdf
          </a>
          <a className="button secondary" href={exportUrl(jobId, variant.variant, "html")}>
            .html
          </a>
        </div>
      </div>
    </div>
  );
}
