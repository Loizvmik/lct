"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { PreviewImage } from "@/components/PreviewImage";
import {
  applyFix,
  assetUrl,
  exportUrl,
  Finding,
  getJob,
  getVariants,
  isJobDone,
  JobResponse,
  listJobs,
  SEVERITY_LABELS,
  VariantName,
  VariantSummary,
  VARIANT_LABELS,
  VARIANT_ORDER,
} from "@/lib/api";
import { EXPORT_FORMATS, ExportFormat, getAppSettings, subscribeToAppSettings } from "@/lib/appSettings";

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
  const [preferredFormat, setPreferredFormat] = useState<ExportFormat>("pptx");
  // Задача Q: стили одного запуска это отдельные задания пакета; переключатель
  // ведёт на проверку соседнего задания, а не на вариант внутри этого.
  const [siblings, setSiblings] = useState<JobResponse[]>([]);

  const load = useCallback(() => {
    getVariants(params.jobId)
      .then((variants) => {
        setAll(variants);
        const current = variants.find((item) => item.variant === variantName) ?? variants[0];
        setSelected(new Set(current?.findings.filter((item) => item.fixable).map((item) => item.id) ?? []));
      })
      .catch((err: Error) => setError(err.message));
  }, [params.jobId, variantName]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    getJob(params.jobId)
      .then((job) => {
        setTemplateId(job.template_id);
        if (!job.batch_id) return;
        listJobs(job.batch_id)
          .then((jobs) => setSiblings(
            jobs
              .filter((item) => isJobDone(item.status))
              .sort((a, b) => VARIANT_ORDER.indexOf(a.style) - VARIANT_ORDER.indexOf(b.style)),
          ))
          .catch(() => undefined);
      })
      .catch(() => undefined);
  }, [params.jobId]);
  useEffect(() => {
    const timer = window.setTimeout(() => setPreferredFormat(getAppSettings().preferredExportFormat), 0);
    const unsubscribe = subscribeToAppSettings((settings) => setPreferredFormat(settings.preferredExportFormat));
    return () => { window.clearTimeout(timer); unsubscribe(); };
  }, []);
  // С задачи Q в задании одна презентация: без совпадения по имени берётся она.
  const variant = all?.find((item) => item.variant === variantName) ?? all?.[0] ?? null;
  const slideFindings = useMemo(() => variant?.findings.filter((item) => item.slide_index === slideIndex) ?? [], [variant, slideIndex]);
  const commonFindings = useMemo(() => variant?.findings.filter((item) => item.slide_index === null) ?? [], [variant]);
  const downloadFormats = [...EXPORT_FORMATS].sort((a, b) =>
    Number(b.value === preferredFormat) - Number(a.value === preferredFormat)
  );

  if (error) return <div className="error-banner" role="alert">{error}</div>;
  if (!variant) return <p className="muted"><span className="spinner" /> Загружаем проверку…</p>;
  // Имя варианта берётся из загруженного результата, а не из адреса: в
  // задании одного стиля адрес может называть другой вариант.
  const shownVariant = variant.variant;

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
      const response = await applyFix(params.jobId, shownVariant, Array.from(selected));
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
            {finding.repair === "structural"
              ? <span className="pill">Нужен другой текст или раскладка</span>
              : !finding.fixable && <span className="pill">Требуется ручная правка</span>}
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

      {siblings.length > 1 && (
        <div className="row-actions" aria-label="Стили этого запуска">
          {siblings.map((job) => (
            <button
              type="button"
              key={job.job_id}
              className={job.job_id === params.jobId ? "" : "secondary"}
              aria-pressed={job.job_id === params.jobId}
              onClick={() => { setSlideIndex(0); setResult(null); router.push(`/decks/${job.job_id}/audit?variant=${job.style}`); }}
            >
              {VARIANT_LABELS[job.style]}
            </button>
          ))}
        </div>
      )}

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
        {downloadFormats.map((format) => (
          <a className={`button${format.value === preferredFormat ? "" : " secondary"}`} href={exportUrl(params.jobId, variant.variant, format.value)} key={format.value}>
            {format.value === "html" ? "Открыть веб-версию" : `Скачать ${format.label}`}
          </a>
        ))}
        <Link className="button ghost" href={`/decks/${params.jobId}/variants`}>Вернуться к презентации</Link>
        {templateId && <Link className="button ghost" href={`/templates/${templateId}/brief`}>Изменить задание</Link>}
      </div>
    </div>
  );
}

export default function AuditPage() {
  return <Suspense fallback={<p className="muted">Загружаем проверку…</p>}><AuditScreen /></Suspense>;
}
