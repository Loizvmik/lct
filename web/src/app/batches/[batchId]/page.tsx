"use client";

// Пакет стилей, запущенный одной кнопкой (задача Q): по заданию на стиль,
// они идут параллельно, у каждого свой прогресс и свой бюджет пяти минут.
// Пока задание идёт, в его колонке прогресс по стадиям; готовое задание
// показывает свою карточку с замечаниями, оценками и скачиванием.
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import VariantCard, { jobFacts } from "@/components/VariantCard";
import {
  getVariants,
  isJobDone,
  JobResponse,
  JOB_STATUS_LABELS,
  listJobs,
  STAGE_LABELS,
  STAGE_ORDER,
  VariantSummary,
  VARIANT_DESCRIPTIONS,
  VARIANT_LABELS,
  VARIANT_ORDER,
} from "@/lib/api";

const POLL_MS = 1500;

function StageTrack({ job }: { job: JobResponse }) {
  const currentIndex = job.stage ? STAGE_ORDER.indexOf(job.stage) : 0;
  return (
    <div className="stage-track">
      {STAGE_ORDER.map((stage) => {
        const index = STAGE_ORDER.indexOf(stage);
        const done = isJobDone(job.status) || (job.stages.includes(stage) && index < currentIndex);
        const active = job.status === "running" && (job.stage ?? "parse") === stage;
        return <div className={`stage ${done ? "done" : active ? "active" : ""}`} key={stage}>{STAGE_LABELS[stage]}</div>;
      })}
    </div>
  );
}

// Колонка задания, у которого ещё нет карточки результата: идёт, упало или
// готово, но результат ещё не пришёл.
function JobColumn({ job, loadingResult }: { job: JobResponse; loadingResult: boolean }) {
  const facts = jobFacts(job);
  const statusClass = { running: "", done: "done", done_with_warnings: "warn", error: "error" }[job.status];
  return (
    <article className="variant-card" aria-busy={job.status === "running"}>
      <header>
        <div>
          <h2>{VARIANT_LABELS[job.style]}</h2>
          <p className="muted small">{VARIANT_DESCRIPTIONS[job.style]}</p>
        </div>
        <span className={`pill status-pill ${statusClass}`}>
          {JOB_STATUS_LABELS[job.status]}
        </span>
      </header>
      <div className="body">
        {job.status === "error"
          ? <div className="error-banner" role="alert">Не удалось создать презентацию: {job.error}</div>
          : <StageTrack job={job} />}
        {job.status === "running" && (
          <p className="muted" role="status"><span className="spinner" /> Сейчас: {STAGE_LABELS[job.stage ?? "parse"].toLowerCase()}.</p>
        )}
        {loadingResult && <p className="muted"><span className="spinner" /> Загружаем результат…</p>}
        {facts.length > 0 && (
          <dl className="variant-facts">
            {facts.map((fact) => <div key={fact.label}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>)}
          </dl>
        )}
      </div>
    </article>
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
          setError("Запуск не найден: сервис мог перезапуститься, а список заданий хранится только в памяти.");
          return;
        }
        if (list.some((j) => j.status === "running")) timer = setTimeout(poll, POLL_MS);
      } catch (err) {
        if (!stopped) setError(err instanceof Error ? err.message : "Не удалось получить задания.");
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
      if (!isJobDone(job.status) || requested.current.has(job.job_id)) continue;
      requested.current.add(job.job_id);
      getVariants(job.job_id)
        .then((variants) => setResults((prev) => ({ ...prev, [job.job_id]: variants[0] ?? null })))
        .catch(() => setResults((prev) => ({ ...prev, [job.job_id]: null })));
    }
  }, [jobs]);

  if (error) return <div className="error-banner" role="alert">{error}</div>;
  if (!jobs) return <p className="muted"><span className="spinner" /> Загружаем задания…</p>;

  const ordered = [...jobs].sort((a, b) => VARIANT_ORDER.indexOf(a.style) - VARIANT_ORDER.indexOf(b.style));
  const running = ordered.filter((j) => j.status === "running").length;
  const several = ordered.length > 1;

  return (
    <div>
      <header className="page-header">
        <p className="eyebrow">Шаг 3 из 4</p>
        <h1>{ordered.length === 3 ? "Три стиля, три презентации" : several ? "Стили этого запуска" : "Презентация"}</h1>
        <p className="lead">
          Каждый стиль собирается своим заданием в бюджете пяти минут, задания идут параллельно.
          Структура общая, текст каждый стиль пишет под свою композицию.
        </p>
      </header>

      <p className="muted" role="status" aria-live="polite">
        {running > 0 ? <><span className="spinner" /> Ещё собираются: {running} из {ordered.length}.</> : "Все задания завершены."}
      </p>

      <div className="row-actions">
        <Link className="button secondary" href={`/templates/${ordered[0].template_id}/brief`}>← Изменить задание и создать заново</Link>
      </div>

      <div className={several ? "grid-3" : "grid-single"}>
        {ordered.map((job) => {
          const summary = results[job.job_id];
          if (isJobDone(job.status) && summary) {
            return <VariantCard jobId={job.job_id} variant={summary} job={job} key={job.job_id} />;
          }
          return <JobColumn job={job} loadingResult={isJobDone(job.status) && summary === undefined} key={job.job_id} />;
        })}
      </div>
    </div>
  );
}
