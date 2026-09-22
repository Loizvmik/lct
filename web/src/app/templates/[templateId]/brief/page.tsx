"use client";

import { useRouter, useParams } from "next/navigation";
import { useState } from "react";
import { createDeck } from "@/lib/api";

export default function BriefPage() {
  const router = useRouter();
  const params = useParams<{ templateId: string }>();
  const [title, setTitle] = useState("");
  const [brief, setBrief] = useState("");
  const [sources, setSources] = useState("");
  const [targetSlides, setTargetSlides] = useState<number | "">("");
  const [autofix, setAutofix] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    if (!brief.trim()) {
      setError("Бриф не может быть пустым");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const { job_id } = await createDeck({
        template_id: params.templateId,
        brief,
        sources: sources.trim() ? [sources] : [],
        title: title.trim() || undefined,
        target_slides: targetSlides === "" ? undefined : Number(targetSlides),
        autofix,
      });
      router.push(`/decks/${job_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось запустить генерацию");
      setBusy(false);
    }
  }

  return (
    <div>
      <h1>Шаг 2 — бриф и материалы</h1>
      <p className="muted">
        Опишите, о чём колода и для какой аудитории, и приложите исходные цифры/факты —
        генератор пишет текст слайдов по этим материалам и ссылается на них при сверке
        цифр аудитом.
      </p>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        <label>Название колоды</label>
        <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Например, «Сокращение времени согласования заявок»" />

        <label>Бриф — о чём колода, для кого, какой тон</label>
        <textarea value={brief} onChange={(e) => setBrief(e.target.value)} placeholder="Просим комитет согласовать запуск автоматической маршрутизации заявок…" />

        <label>Исходные материалы — цифры, факты, ссылки (необязательно)</label>
        <textarea value={sources} onChange={(e) => setSources(e.target.value)} placeholder="Выборка: 1240 заявок, медиана ожидания 18 часов…" />

        <div className="grid-2">
          <div>
            <label>Целевое число слайдов (10–15, по умолчанию решает генератор)</label>
            <input
              type="number"
              min={10}
              max={15}
              value={targetSlides}
              onChange={(e) => setTargetSlides(e.target.value === "" ? "" : Number(e.target.value))}
            />
          </div>
          <div>
            <label>Починка находок</label>
            <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8 }}>
              <input type="checkbox" checked={autofix} onChange={(e) => setAutofix(e.target.checked)} style={{ width: "auto" }} />
              <span style={{ color: "var(--text)" }}>
                Автоматически починить всё, что чинится (рекомендуется для демонстрации)
              </span>
            </label>
          </div>
        </div>
      </div>

      <div className="row-actions">
        <button onClick={submit} disabled={busy}>
          {busy ? "Запускаем…" : "Сгенерировать три варианта →"}
        </button>
      </div>
    </div>
  );
}
