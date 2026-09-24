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
import { getJob } from "@/lib/api";

type StepKey = "template" | "brief" | "variants" | "audit";

const STEPS: { key: StepKey; label: string }[] = [
  { key: "template", label: "1. Шаблон" },
  { key: "brief", label: "2. Задание" },
  { key: "variants", label: "3. Варианты" },
  { key: "audit", label: "4. Проверка" },
];

export default function StepNav() {
  const pathname = usePathname();
  const templateMatch = pathname.match(/^\/templates\/([^/]+)/);
  const deckMatch = pathname.match(/^\/decks\/([^/]+)/);
  const templateIdFromUrl = templateMatch ? decodeURIComponent(templateMatch[1]) : null;
  const jobId = deckMatch ? decodeURIComponent(deckMatch[1]) : null;

  const [jobTemplateId, setJobTemplateId] = useState<string | null>(null);
  const [deckReady, setDeckReady] = useState(false);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    getJob(jobId)
      .then((job) => {
        if (cancelled) return;
        setJobTemplateId(job.template_id);
        setDeckReady(job.status === "done");
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  const templateId = templateIdFromUrl ?? (jobId ? jobTemplateId : null);
  const currentDeckReady = jobId ? deckReady : false;

  let current: StepKey = "template";
  if (templateMatch && pathname.endsWith("/brief")) current = "brief";
  else if (templateMatch) current = "template";
  else if (pathname.includes("/audit")) current = "audit";
  else if (pathname.includes("/variants")) current = "variants";
  else if (deckMatch) current = "variants"; // экран прогресса генерации — на пути к вариантам

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
        return jobId && currentDeckReady ? `/decks/${jobId}/variants` : null;
      case "audit":
        return jobId && currentDeckReady ? `/decks/${jobId}/audit?variant=dense` : null;
    }
  }

  return (
    <nav className="steps" aria-label="Этапы создания презентации">
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
    </nav>
  );
}
