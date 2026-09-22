"use client";

import { useRouter, useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { getJob, JobResponse, STAGE_LABELS, STAGE_ORDER, subscribeJobEvents } from "@/lib/api";

export default function JobProgressPage() {
  const router = useRouter();
  const params = useParams<{ jobId: string }>();
  const [job, setJob] = useState<JobResponse | null>(null);
  const redirected = useRef(false);

  useEffect(() => {
    // Снимок сразу (не ждать первого события потока) плюс подписка на
    // SSE для живого прогресса — то же самое, что и опрос `GET /api/jobs/
    // {id}`, но без поллинга каждую секунду.
    getJob(params.jobId).then(setJob).catch(() => undefined);
    const unsubscribe = subscribeJobEvents(params.jobId, (update) => {
      setJob(update);
      if (update.status === "done" && update.deck_id && !redirected.current) {
        redirected.current = true;
        router.push(`/decks/${update.deck_id}/variants`);
      }
    });
    return unsubscribe;
  }, [params.jobId, router]);

  useEffect(() => {
    if (job?.status === "done" && job.deck_id && !redirected.current) {
      redirected.current = true;
      router.push(`/decks/${job.deck_id}/variants`);
    }
  }, [job, router]);

  return (
    <div>
      <h1>Генерация колоды</h1>
      <p className="muted">
        Разбор шаблона → структура → текст слайдов → вёрстка трёх вариантов →
        детерминированный аудит → выгрузка. Обычно укладывается в пределах минут, не
        секунд — на VK Tech с двенадцатью слайдами живой замер даёт около 104с.
      </p>

      {job?.status === "error" && <div className="error-banner">Ошибка генерации: {job.error}</div>}

      <div className="card">
        <div className="stage-track">
          {STAGE_ORDER.map((stage) => {
            const doneIdx = job ? STAGE_ORDER.indexOf(job.stage as never) : -1;
            const thisIdx = STAGE_ORDER.indexOf(stage);
            const isDone = job?.stages.includes(stage) && thisIdx < doneIdx;
            const isActive = job?.stage === stage && job.status === "running";
            const isDoneFinal = job?.status === "done";
            const cls = isDoneFinal || isDone ? "done" : isActive ? "active" : "";
            return (
              <div className={`stage ${cls}`} key={stage}>
                {STAGE_LABELS[stage]}
              </div>
            );
          })}
        </div>
      </div>

      {job && job.status === "running" && (
        <p className="muted">
          <span className="spinner" style={{ marginRight: 8 }} />
          Сейчас: {STAGE_LABELS[job.stage ?? "parse"]}…
        </p>
      )}
    </div>
  );
}
