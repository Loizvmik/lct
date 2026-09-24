"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { getJob, JobResponse, STAGE_LABELS, STAGE_ORDER, subscribeJobEvents } from "@/lib/api";

export default function JobProgressPage() {
  const router = useRouter();
  const params = useParams<{ jobId: string }>();
  const [job, setJob] = useState<JobResponse | null>(null);
  const [connectionLost, setConnectionLost] = useState(false);
  const redirected = useRef(false);

  useEffect(() => {
    getJob(params.jobId).then(setJob).catch(() => setConnectionLost(true));
    return subscribeJobEvents(
      params.jobId,
      (update) => {
        setConnectionLost(false);
        setJob(update);
        if (update.status === "done" && update.deck_id && !redirected.current) {
          redirected.current = true;
          router.push(`/decks/${update.deck_id}/variants`);
        }
      },
      () => setConnectionLost(true),
    );
  }, [params.jobId, router]);

  useEffect(() => {
    if (job?.status === "done" && job.deck_id && !redirected.current) {
      redirected.current = true;
      router.push(`/decks/${job.deck_id}/variants`);
    }
  }, [job, router]);

  return (
    <div>
      <header className="page-header">
        <p className="eyebrow">Создаём презентацию</p>
        <h1>Готовим три варианта</h1>
        <p className="lead">Можно оставить страницу открытой. Она перейдёт к результатам, когда всё будет готово.</p>
      </header>

      {job?.status === "error" && <div className="error-banner" role="alert">Не удалось создать презентацию: {job.error}</div>}
      {connectionLost && <div className="notice" role="status">Связь с сервером прервалась. Обновите страницу, чтобы проверить состояние.</div>}

      <section className="card" aria-live="polite" aria-busy={job?.status !== "done"}>
        <div className="stage-track">
          {STAGE_ORDER.map((stage) => {
            const currentIndex = job?.stage ? STAGE_ORDER.indexOf(job.stage) : 0;
            const index = STAGE_ORDER.indexOf(stage);
            const done = job?.status === "done" || job?.stages.includes(stage) && index < currentIndex;
            const active = job?.status !== "done" && (job?.stage ?? "parse") === stage;
            return <div className={`stage ${done ? "done" : active ? "active" : ""}`} key={stage}>{STAGE_LABELS[stage]}</div>;
          })}
        </div>
      </section>

      {job?.status === "running" && <p className="muted" role="status"><span className="spinner" /> Сейчас: {STAGE_LABELS[job.stage ?? "parse"].toLowerCase()}.</p>}
    </div>
  );
}
