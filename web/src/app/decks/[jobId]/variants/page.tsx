"use client";

// Результат одного задания (одна презентация одного стиля). Если задание
// создано вместе с соседями одной кнопкой, отсюда ведёт ссылка на общий
// экран пакета со всеми стилями.
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { getJob, getVariants, JobResponse, VariantSummary, VARIANT_LABELS } from "@/lib/api";
import VariantCard from "@/components/VariantCard";

export default function VariantsPage() {
  const params = useParams<{ jobId: string }>();
  const [variants, setVariants] = useState<VariantSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<JobResponse | null>(null);

  useEffect(() => {
    getVariants(params.jobId).then(setVariants).catch((err) => setError(err.message));
    getJob(params.jobId).then(setJob).catch(() => undefined);
  }, [params.jobId]);

  if (error) return <div className="error-banner">{error}</div>;
  if (!variants) return <p className="muted">Загружаем презентацию…</p>;

  return (
    <div>
      <h1>Шаг 3 — готовая презентация{job ? `: ${VARIANT_LABELS[job.style]}` : ""}</h1>
      <p className="muted">
        Одна презентация одного стиля, собранная своим заданием в своём бюджете пяти минут.
      </p>

      <div className="row-actions" style={{ marginBottom: 16 }}>
        {job && (
          <Link className="button secondary" href={`/templates/${job.template_id}/brief`}>
            ← Изменить бриф и сгенерировать заново
          </Link>
        )}
        {job?.batch_id && (
          <Link className="button secondary" href={`/batches/${job.batch_id}`}>
            Все стили этого запуска
          </Link>
        )}
      </div>

      <div className="grid-3">
        {variants.map((variant) => (
          <VariantCard jobId={params.jobId} variant={variant} key={variant.variant} />
        ))}
      </div>
    </div>
  );
}
