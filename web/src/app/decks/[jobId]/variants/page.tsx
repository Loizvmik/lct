"use client";

// Результат одного задания (одна презентация одного стиля). Если задание
// создано вместе с соседями одной кнопкой, отсюда ведёт ссылка на общий
// экран пакета со всеми стилями.
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import VariantCard from "@/components/VariantCard";
import { getJob, getVariants, JobResponse, VariantSummary, VARIANT_LABELS } from "@/lib/api";

export default function VariantsPage() {
  const params = useParams<{ jobId: string }>();
  const [variants, setVariants] = useState<VariantSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<JobResponse | null>(null);

  useEffect(() => {
    getVariants(params.jobId).then(setVariants).catch((err: Error) => setError(err.message));
    getJob(params.jobId).then(setJob).catch(() => undefined);
  }, [params.jobId]);

  if (error) return <div className="error-banner" role="alert">{error}</div>;
  if (!variants) return <p className="muted"><span className="spinner" /> Загружаем презентацию…</p>;

  return (
    <div>
      <header className="page-header">
        <p className="eyebrow">Шаг 3 из 4</p>
        <h1>{job ? `Презентация: ${VARIANT_LABELS[job.style].toLowerCase()} стиль` : "Готовая презентация"}</h1>
        <p className="lead">Одна презентация одного стиля, собранная своим заданием в бюджете пяти минут. Откройте её для подробной проверки или скачайте.</p>
      </header>

      <div className="row-actions">
        {job && <Link className="button secondary" href={`/templates/${job.template_id}/brief`}>← Изменить задание и создать заново</Link>}
        {job?.batch_id && <Link className="button secondary" href={`/batches/${job.batch_id}`}>Все стили этого запуска</Link>}
      </div>

      <div className={variants.length > 1 ? "grid-3" : "grid-single"}>
        {variants.map((variant) => (
          <VariantCard jobId={params.jobId} variant={variant} job={job} key={variant.variant} />
        ))}
      </div>
    </div>
  );
}
