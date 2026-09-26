"use client";

// Пакет стилей, запущенный одной кнопкой (задача Q): по заданию на стиль,
// они идут параллельно, у каждого свой прогресс и свой бюджет пяти минут.
// Пока задание идёт, в его колонке прогресс по стадиям; готовое задание
// показывает свою карточку с отчётом, оценками и скачиванием.
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  getVariants,
  JobResponse,
  listJobs,
  MODE_LABELS,
  STAGE_LABELS,
  STAGE_ORDER,
  VariantSummary,
  VARIANT_LABELS,
  VARIANT_ORDER,
} from "@/lib/api";
import VariantCard from "@/components/VariantCard";

const POLL_MS = 1500;

function budgetLine(job: JobResponse): string {
  const parts: string[] = [];
  if (job.seconds != null) {
    const limit = job.budget?.budget_seconds ?? 300;
    parts.push(`${job.seconds.toFixed(0)} с из ${limit.toFixed(0)} с`);
  }
  if (job.mode) parts.push(MODE_LABELS[job.mode] ?? job.mode);
  const shared = Object.keys(job.budget?.shared_stages ?? {});
  if (shared.length) {
    parts.push(`переиспользовано: ${shared.map((s) => STAGE_LABELS[s as keyof typeof STAGE_LABELS] ?? s).join(", ")}`);
  }
  return parts.join(" · ");
}

function StageTrack({ job }: { job: JobResponse }) {
  const doneIdx = STAGE_ORDER.indexOf(job.stage as never);
  return (
    <div className="stage-track">
      {STAGE_ORDER.map((stage) => {
        const thisIdx = STAGE_ORDER.indexOf(stage);
        const isDone = job.status === "done" || (job.stages.includes(stage) && thisIdx < doneIdx);
        const isActive = job.stage === stage && job.status === "running";
        return (
          <div className={`stage ${isDone ? "done" : isActive ? "active" : ""}`} key={stage}>
            {STAGE_LABELS[stage]}
          </div>
        );
      })}
    </div>
  );
}

export default function BatchPage() {
  const params = useParams<{ batchId: string }>();
  const [jobs, setJobs] = useState<JobResponse[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, VariantSummary | null>>({});
  const requested = useRef<Set<string>>(new Set());

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const list = await listJobs(params.batchId);
        if (stopped) return;
        setJobs(list);
        if (list.length === 0) {
          setError("Запуск не найден: сервис мог перезапуститься, реестр заданий живёт в памяти.");
          return;
        }
        if (list.some((j) => j.status === "running")) timer = setTimeout(poll, POLL_MS);
      } catch (err) {
        if (!stopped) setError(err instanceof Error ? err.message : "Не удалось получить задания");
      }
    }
    poll();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [params.batchId]);

  // Результат каждого готового задания запрашивается один раз.
  useEffect(() => {
    for (const job of jobs ?? []) {
      if (job.status !== "done" || requested.current.has(job.job_id)) continue;
      requested.current.add(job.job_id);
      getVariants(job.job_id)
        .then((variants) => setResults((prev) => ({ ...prev, [job.job_id]: variants[0] ?? null })))
        .catch(() => setResults((prev) => ({ ...prev, [job.job_id]: null })));
    }
  }, [jobs]);

  if (error) return <div className="error-banner">{error}</div>;
  if (!jobs) return <p className="muted">Загружаем задания…</p>;

  const ordered = [...jobs].sort((a, b) => VARIANT_ORDER.indexOf(a.style) - VARIANT_ORDER.indexOf(b.style));
  const running = ordered.filter((j) => j.status === "running").length;
  const title = ordered.length > 1 ? `Шаг 3 — ${ordered.length} стиля, ${ordered.length} презентации` : "Шаг 3 — презентация";

  return (
    <div>
      <h1>{title}</h1>
      <p className="muted">
        Каждый стиль собирается своим заданием в своём бюджете пяти минут, задания идут параллельно.
        Структура презентации общая, текст каждый стиль пишет под свою композицию.
        {running > 0 && ` Сейчас идут: ${running}.`}
      </p>

      <div className="row-actions" style={{ marginBottom: 16 }}>
        <Link className="button secondary" href={`/templates/${ordered[0].template_id}/brief`}>
          ← Изменить бриф и сгенерировать заново
        </Link>
      </div>

      <div className="grid-3">
        {ordered.map((job) => {
          const summary = results[job.job_id];
          const line = budgetLine(job);
          if (job.status === "done" && summary) {
            return (
              <VariantCard
                jobId={job.job_id}
                variant={summary}
                key={job.job_id}
                footer={line ? <p className="muted job-budget" style={{ fontSize: 12 }}>{line}</p> : null}
              />
            );
          }
          return (
            <div className="variant-card job-progress" key={job.job_id}>
              <header>
                <strong>{VARIANT_LABELS[job.style]}</strong>
                <span className="pill">
                  {job.status === "error" ? "ошибка" : job.status === "done" ? "готово" : "идёт"}
                </span>
              </header>
              <div className="body">
                {job.status === "error" ? (
                  <div className="error-banner">Ошибка генерации: {job.error}</div>
                ) : (
                  <StageTrack job={job} />
                )}
                {job.status === "running" && (
                  <p className="muted">
                    <span className="spinner" style={{ marginRight: 8 }} />
                    Сейчас: {STAGE_LABELS[job.stage ?? "parse"]}…
                  </p>
                )}
                {job.status === "done" && summary === undefined && <p className="muted">Загружаем результат…</p>}
                {line && <p className="muted job-budget" style={{ fontSize: 12 }}>{line}</p>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
