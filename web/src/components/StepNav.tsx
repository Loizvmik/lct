"use client";

// Шаги в шапке (Task 16, п.1 — «некуда вернуться»). Раньше это были
// нежинтерактивные <span>, теперь — настоящая навигация: пройденные шаги
// кликабельны и возвращают на свой экран с сохранённым состоянием (шаблон
// уже разобран и лежит в кеше API по `template_id`, колода — по
// `job_id`/`deck_id`, оба адресуются напрямую через URL), будущие шаги —
// не кликабельны.
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { getJob, listJobs, VariantName } from "@/lib/api";

type StepKey = "template" | "brief" | "variants" | "audit";

const STEPS: { key: StepKey; label: string }[] = [
  { key: "template", label: "1. Шаблон" },
  { key: "brief", label: "2. Бриф" },
  { key: "variants", label: "3. Варианты" },
  { key: "audit", label: "4. Аудит" },
];

export default function StepNav() {
  const pathname = usePathname();
  const templateMatch = pathname.match(/^\/templates\/([^/]+)/);
  const deckMatch = pathname.match(/^\/decks\/([^/]+)/);
  // Задача Q: экран пакета стилей (`/batches/{id}`) — тот же шаг 3.
  const batchMatch = pathname.match(/^\/batches\/([^/]+)/);
  const batchIdFromUrl = batchMatch ? decodeURIComponent(batchMatch[1]) : null;
  const templateIdFromUrl = templateMatch ? decodeURIComponent(templateMatch[1]) : null;
  const jobId = deckMatch ? decodeURIComponent(deckMatch[1]) : null;

  const [jobTemplateId, setJobTemplateId] = useState<string | null>(null);
  const [deckReady, setDeckReady] = useState(false);
  const [jobStyle, setJobStyle] = useState<VariantName>("dense");
  const [jobBatchId, setJobBatchId] = useState<string | null>(null);
  const [batchTemplateId, setBatchTemplateId] = useState<string | null>(null);

  useEffect(() => {
    if (!batchIdFromUrl) return;
    let cancelled = false;
    listJobs(batchIdFromUrl)
      .then((jobs) => {
        if (!cancelled && jobs.length) setBatchTemplateId(jobs[0].template_id);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [batchIdFromUrl]);

  useEffect(() => {
    if (!jobId) {
      setJobTemplateId(null);
      setDeckReady(false);
      return;
    }
    let cancelled = false;
    getJob(jobId)
      .then((job) => {
        if (cancelled) return;
        setJobTemplateId(job.template_id);
        setDeckReady(job.status === "done");
        setJobStyle(job.style);
        setJobBatchId(job.batch_id);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  // Шаблон пакета берётся, только пока мы на экране пакета: старое значение
  // не сбрасывается в эффекте, а просто не читается на других экранах.
  const templateId = templateIdFromUrl ?? jobTemplateId ?? (batchIdFromUrl ? batchTemplateId : null);

  let current: StepKey = "template";
  if (templateMatch && pathname.endsWith("/brief")) current = "brief";
  else if (templateMatch) current = "template";
  else if (pathname.includes("/audit")) current = "audit";
  else if (pathname.includes("/variants")) current = "variants";
  else if (deckMatch || batchMatch) current = "variants"; // экран прогресса генерации — на пути к вариантам

  // «Пройденные шаги кликабельны, будущие — нет»: шаг «пройден», когда
  // данные, нужные для его экрана, уже есть — шаблон разобран (`template_id`
  // из URL или из задания) для шагов 1–2, готовая колода (`job.status ==
  // "done"`) для шагов 3–4. Явного номера текущего шага не считаем: с
  // экрана вариантов «4. Аудит» тоже должен быть кликабелен — колода уже
  // готова, аудит по ней доступен без промежуточного «посещения» шага 3.
  function hrefFor(key: StepKey): string | null {
    switch (key) {
      case "template":
        return templateId ? `/templates/${templateId}` : null;
      case "brief":
        return templateId ? `/templates/${templateId}/brief` : null;
      case "variants":
        if (jobId && jobBatchId) return `/batches/${jobBatchId}`;
        return jobId && deckReady ? `/decks/${jobId}/variants` : null;
      case "audit":
        return jobId && deckReady ? `/decks/${jobId}/audit?variant=${jobStyle}` : null;
    }
  }

  return (
    <div className="steps">
      {STEPS.map((step) => {
        const isActive = step.key === current;
        const href = hrefFor(step.key);
        const cls = `step${isActive ? " active" : ""}${href ? " clickable" : ""}`;
        if (href && !isActive) {
          return (
            <Link className={cls} href={href} key={step.key}>
              {step.label}
            </Link>
          );
        }
        return (
          <span className={cls} key={step.key}>
            {step.label}
          </span>
        );
      })}
    </div>
  );
}
