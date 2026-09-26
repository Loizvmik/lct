"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import {
  applyFix,
  assetUrl,
  Finding,
  getJob,
  getVariants,
  JobResponse,
  listJobs,
  SEVERITY_LABELS,
  VariantName,
  VariantSummary,
  VARIANT_LABELS,
  VARIANT_ORDER,
} from "@/lib/api";

function AuditScreen() {
  const params = useParams<{ jobId: string }>();
  const search = useSearchParams();
  const router = useRouter();
  const variantName = (search.get("variant") ?? "dense") as VariantName;

  const [all, setAll] = useState<VariantSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [slideIndex, setSlideIndex] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [applying, setApplying] = useState(false);
  const [lastResult, setLastResult] = useState<{ applied: number; skipped: number } | null>(null);
  const [templateId, setTemplateId] = useState<string | null>(null);
  // Задача Q: стили одного запуска — отдельные задания пакета; переключатель
  // ведёт на аудит соседнего задания, а не на вариант внутри этого.
  const [siblings, setSiblings] = useState<JobResponse[]>([]);

  useEffect(() => {
    getJob(params.jobId)
      .then((job) => {
        setTemplateId(job.template_id);
        if (job.batch_id) {
          listJobs(job.batch_id)
            .then((jobs) =>
              setSiblings(
                jobs
                  .filter((j) => j.status === "done")
                  .sort((a, b) => VARIANT_ORDER.indexOf(a.style) - VARIANT_ORDER.indexOf(b.style)),
              ),
            )
            .catch(() => undefined);
        }
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.jobId]);

  function load() {
    getVariants(params.jobId)
      .then((variants) => {
        setAll(variants);
        const current = variants.find((v) => v.variant === variantName) ?? variants[0];
        if (current) {
          setSelected(new Set(current.findings.filter((f) => f.fixable).map((f) => f.id)));
        }
      })
      .catch((err) => setError(err.message));
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.jobId, variantName]);

  // С задачи Q в задании одна презентация: без совпадения по имени берётся она.
  const variant = all?.find((v) => v.variant === variantName) ?? all?.[0] ?? null;

  const slideFindings = useMemo(
    () => (variant ? variant.findings.filter((f) => f.slide_index === slideIndex) : []),
    [variant, slideIndex],
  );

  const deckWideFindings = useMemo(
    () => (variant ? variant.findings.filter((f) => f.slide_index === null) : []),
    [variant],
  );

  if (error) return <div className="error-banner">{error}</div>;
  if (!all || !variant) return <p className="muted">Загружаем аудит…</p>;

  const previewSrc = variant.preview_pngs[slideIndex];

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function fixSelected() {
    if (selected.size === 0 || !variant) return;
    setApplying(true);
    setLastResult(null);
    try {
      const result = await applyFix(params.jobId, variant.variant, Array.from(selected));
      setLastResult({ applied: result.applied.length, skipped: result.skipped.length });
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось применить исправления");
    } finally {
      setApplying(false);
    }
  }

  function renderFindingRow(finding: Finding) {
    return (
      <div className={`finding-item${!finding.fixable ? " disabled" : ""}`} key={finding.id}>
        <input
          type="checkbox"
          disabled={!finding.fixable}
          checked={selected.has(finding.id)}
          onChange={() => toggle(finding.id)}
        />
        <div>
          <div className="finding-meta">
            <span className={`pill ${finding.severity}`}>{SEVERITY_LABELS[finding.severity]}</span>
            <span className="pill">{finding.check_id}</span>
            {!finding.fixable && <span className="muted" style={{ fontSize: 11 }}>нет автопочинки</span>}
          </div>
          <div>{finding.message}</div>
          {finding.fixable && <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>{finding.fix_hint}</div>}
        </div>
      </div>
    );
  }

  return (
    <div>
      <h1>Шаг 4 — аудит: {VARIANT_LABELS[variant.variant]} вариант</h1>
      <p className="muted">
        Рамки поверх превью — находки детерминированного аудита по координатам
        (доли холста). Отметьте, что чинить, и нажмите «Исправить выбранное» — презентация
        пересоберётся и аудит пройдёт заново.
      </p>

      <div className="row-actions" style={{ marginBottom: 16 }}>
        {siblings.length > 1 &&
          siblings.map((job) => (
            <button
              key={job.job_id}
              className={job.job_id === params.jobId ? "" : "secondary"}
              onClick={() => router.push(`/decks/${job.job_id}/audit?variant=${job.style}`)}
            >
              {VARIANT_LABELS[job.style]}
            </button>
          ))}
        {templateId && (
          <Link className="button secondary" href={`/templates/${templateId}/brief`}>
            ← Изменить бриф и сгенерировать заново
          </Link>
        )}
      </div>

      {lastResult && (
        <div className="card" style={{ borderColor: "var(--ok)" }}>
          Применено исправлений: {lastResult.applied}
          {lastResult.skipped > 0 && `, пропущено (нет автопочинки для этого типа): ${lastResult.skipped}`}
        </div>
      )}

      <div className="audit-stage">
        <div>
          <div className="slide-preview-wrap">
            {previewSrc && (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={assetUrl(previewSrc)} alt={`слайд ${slideIndex + 1}`} />
            )}
            {slideFindings
              .filter((f) => f.box)
              .map((f) => (
                <div
                  key={f.id}
                  className={`finding-box ${f.severity}${selected.has(f.id) ? " selected" : ""}`}
                  style={{
                    left: `${(f.box!.left * 100).toFixed(3)}%`,
                    top: `${(f.box!.top * 100).toFixed(3)}%`,
                    width: `${(f.box!.width * 100).toFixed(3)}%`,
                    height: `${(f.box!.height * 100).toFixed(3)}%`,
                  }}
                  title={f.message}
                />
              ))}
          </div>
          <div className="thumb-strip" style={{ marginTop: 10 }}>
            {variant.preview_pngs.map((png, idx) => {
              const count = variant.findings.filter((f) => f.slide_index === idx).length;
              return (
                <div
                  className={`thumb${idx === slideIndex ? " active" : ""}`}
                  key={png}
                  onClick={() => setSlideIndex(idx)}
                  style={{ position: "relative" }}
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={assetUrl(png)} alt={`слайд ${idx + 1}`} />
                  {count > 0 && (
                    <span
                      style={{
                        position: "absolute",
                        top: 2,
                        right: 2,
                        background: "var(--critical)",
                        color: "#fff",
                        borderRadius: 8,
                        fontSize: 10,
                        padding: "1px 5px",
                      }}
                    >
                      {count}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        <div>
          <div className="card">
            <h2>
              Находки на слайде {slideIndex + 1} ({slideFindings.length})
            </h2>
            {slideFindings.length === 0 && <p className="muted">На этом слайде находок нет.</p>}
            {slideFindings.map(renderFindingRow)}
          </div>

          {deckWideFindings.length > 0 && (
            <div className="card">
              <h2>Находки презентации целиком ({deckWideFindings.length})</h2>
              {deckWideFindings.map(renderFindingRow)}
            </div>
          )}

          <button onClick={fixSelected} disabled={applying || selected.size === 0} style={{ width: "100%" }}>
            {applying ? "Применяем…" : `Исправить выбранное (${selected.size})`}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function AuditPage() {
  return (
    <Suspense fallback={<p className="muted">Загружаем аудит…</p>}>
      <AuditScreen />
    </Suspense>
  );
}
