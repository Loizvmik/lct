"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { PreviewImage } from "@/components/PreviewImage";
import { applyFix, assetUrl, exportUrl, Finding, getJob, getVariants, SEVERITY_LABELS, VariantName, VariantSummary, VARIANT_LABELS } from "@/lib/api";

function AuditScreen() {
  const params = useParams<{ jobId: string }>();
  const search = useSearchParams();
  const router = useRouter();
  const requestedVariant = search.get("variant");
  const variantName: VariantName = requestedVariant === "airy" || requestedVariant === "visual" ? requestedVariant : "dense";
  const [all, setAll] = useState<VariantSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [slideIndex, setSlideIndex] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [applying, setApplying] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [templateId, setTemplateId] = useState<string | null>(null);

  const load = useCallback(() => {
    getVariants(params.jobId)
      .then((variants) => {
        setAll(variants);
        const current = variants.find((item) => item.variant === variantName);
        setSelected(new Set(current?.findings.filter((item) => item.fixable).map((item) => item.id) ?? []));
      })
      .catch((err: Error) => setError(err.message));
  }, [params.jobId, variantName]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { getJob(params.jobId).then((job) => setTemplateId(job.template_id)).catch(() => undefined); }, [params.jobId]);
  const variant = all?.find((item) => item.variant === variantName) ?? null;
  const slideFindings = useMemo(() => variant?.findings.filter((item) => item.slide_index === slideIndex) ?? [], [variant, slideIndex]);
  const commonFindings = useMemo(() => variant?.findings.filter((item) => item.slide_index === null) ?? [], [variant]);

  if (error) return <div className="error-banner" role="alert">{error}</div>;
  if (!variant) return <p className="muted"><span className="spinner" /> Загружаем проверку…</p>;

  function toggle(id: string) {
    setSelected((before) => {
      const next = new Set(before);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  async function fixSelected() {
    if (!selected.size) return;
    setApplying(true); setResult(null); setError(null);
    try {
      const response = await applyFix(params.jobId, variantName, Array.from(selected));
      setResult(`Исправлено: ${response.applied.length}. Не удалось исправить автоматически: ${response.skipped.length}.`);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось применить исправления.");
    } finally {
      setApplying(false);
    }
  }

  function FindingRow({ finding }: { finding: Finding }) {
    const checkboxId = `finding-${finding.id}`;
    return (
      <div className={`finding-item${finding.fixable ? "" : " disabled"}`}>
        <input id={checkboxId} type="checkbox" disabled={!finding.fixable} checked={selected.has(finding.id)} onChange={() => toggle(finding.id)} />
        <label htmlFor={checkboxId}>
          <span className="finding-meta">
            <span className={`count ${finding.severity}`}>{SEVERITY_LABELS[finding.severity]}</span>
            {!finding.fixable && <span className="pill">Требуется ручная правка</span>}
          </span>
          <span>{finding.message}</span>
          {finding.fixable && finding.fix_hint && <span className="field-hint">{finding.fix_hint}</span>}
        </label>
      </div>
    );
  }

  return (
    <div>
      <header className="page-header">
        <p className="eyebrow">Шаг 4 из 4</p>
        <h1>Проверьте презентацию</h1>
        <p className="lead">Выберите слайд, изучите замечания и примените доступные исправления перед скачиванием.</p>
      </header>

      <div className="row-actions" aria-label="Варианты презентации">
        {(["dense", "airy", "visual"] as VariantName[]).map((name) => (
          <button type="button" key={name} className={name === variantName ? "" : "secondary"} aria-pressed={name === variantName} onClick={() => { setSlideIndex(0); setResult(null); router.push(`/decks/${params.jobId}/audit?variant=${name}`); }}>
            {VARIANT_LABELS[name]}
          </button>
        ))}
      </div>

      {error && <div className="error-banner" role="alert">{error}</div>}
      {result && <div className="success-banner" role="status">{result}</div>}

      <div className="audit-stage">
        <div>
          <div className="slide-preview-wrap">
            {variant.preview_pngs[slideIndex]
              ? <PreviewImage src={assetUrl(variant.preview_pngs[slideIndex])} alt={`Слайд ${slideIndex + 1}`} />
              : <div className="preview-fallback">Предпросмотр пока недоступен</div>}
            {slideFindings.filter((item) => item.box).map((item) => (
              <span
                key={item.id}
                className={`finding-box ${item.severity}${selected.has(item.id) ? " selected" : ""}`}
                style={{ left: `${item.box!.left * 100}%`, top: `${item.box!.top * 100}%`, width: `${item.box!.width * 100}%`, height: `${item.box!.height * 100}%` }}
                aria-hidden="true"
              />
            ))}
          </div>
          <div className="thumb-strip" aria-label="Слайды">
            {variant.preview_pngs.map((png, index) => {
              const count = variant.findings.filter((item) => item.slide_index === index).length;
              return (
                <button type="button" className={`thumb${index === slideIndex ? " active" : ""}`} key={png} onClick={() => setSlideIndex(index)} aria-label={`Слайд ${index + 1}, замечаний: ${count}`} aria-pressed={index === slideIndex}>
                  <PreviewImage src={assetUrl(png)} alt="" />
                  <span className="slide-number">{index + 1}{count ? ` · ${count}` : ""}</span>
                </button>
              );
            })}
          </div>
        </div>

        <aside>
          <section className="card">
            <h2>Замечания на слайде {slideIndex + 1}</h2>
            {slideFindings.length ? slideFindings.map((finding) => <FindingRow finding={finding} key={finding.id} />) : <p className="muted">На этом слайде замечаний нет.</p>}
          </section>
          {commonFindings.length > 0 && <section className="card"><h2>Для всей презентации</h2>{commonFindings.map((finding) => <FindingRow finding={finding} key={finding.id} />)}</section>}
          <button type="button" onClick={fixSelected} disabled={applying || selected.size === 0} style={{ width: "100%" }}>
            {applying ? "Исправляем…" : `Исправить выбранное (${selected.size})`}
          </button>
        </aside>
      </div>

      <div className="row-actions">
        <a className="button" href={exportUrl(params.jobId, variantName, "pptx")}>Скачать PowerPoint</a>
        <a className="button secondary" href={exportUrl(params.jobId, variantName, "pdf")}>Скачать PDF</a>
        <a className="button secondary" href={exportUrl(params.jobId, variantName, "html")}>Открыть веб-версию</a>
        <Link className="button ghost" href={`/decks/${params.jobId}/variants`}>Вернуться к вариантам</Link>
        {templateId && <Link className="button ghost" href={`/templates/${templateId}/brief`}>Изменить задание</Link>}
      </div>
    </div>
  );
}

export default function AuditPage() {
  return <Suspense fallback={<p className="muted">Загружаем проверку…</p>}><AuditScreen /></Suspense>;
}
