"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { getProfile, TemplateProfile } from "@/lib/api";

const COLOR_NAMES: Record<string, string> = {
  brand: "Основной", accent: "Акцентный", surface: "Фон", on_surface: "Текст",
  muted: "Второстепенный", border: "Границы", danger: "Ошибка", warning: "Предупреждение",
};

function friendlyWarning(text: string): string {
  const lower = text.toLowerCase();
  if (lower.includes("logo") || lower.includes("логотип")) return "Логотип в шаблоне не найден. Его можно будет добавить в готовый файл вручную.";
  if (lower.includes("media") || lower.includes("медиа")) return "Некоторые изображения не удалось распознать. Они останутся частью исходного шаблона.";
  if (lower.includes("confidence") || lower.includes("уверен")) return "Некоторые параметры оформления определены приблизительно. Проверьте готовые слайды перед отправкой.";
  return "Часть оформления определена приблизительно. Проверьте готовые слайды перед отправкой.";
}

export default function TemplateProfilePage() {
  const params = useParams<{ templateId: string }>();
  const [profile, setProfile] = useState<TemplateProfile | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getProfile(params.templateId)
      .then((res) => { if (!cancelled) setProfile(res.profile); })
      .catch((err: Error) => { if (!cancelled) setError(err.message); });
    return () => { cancelled = true; };
  }, [params.templateId]);

  if (error) return <div className="error-banner" role="alert">{error}</div>;
  if (!profile) return <p className="muted" aria-live="polite"><span className="spinner" /> Изучаем шаблон…</p>;

  const warnings = Array.from(new Set(profile.warnings.map(friendlyWarning)));
  return (
    <div>
      <header className="page-header">
        <p className="eyebrow">Шаблон готов</p>
        <h1>{profile.source_name}</h1>
        <p className="lead">Мы нашли основные правила оформления. Теперь можно описать будущую презентацию.</p>
      </header>

      <div className="summary-grid" aria-label="Сводка по шаблону">
        <div className="summary-tile"><span className="muted small">Макеты</span><span className="summary-value">{profile.layouts.length}</span></div>
        <div className="summary-tile"><span className="muted small">Цвета</span><span className="summary-value">{Object.keys(profile.palette_roles).length}</span></div>
        <div className="summary-tile"><span className="muted small">Шрифты</span><span className="summary-value">{profile.type_scale.families.length || "—"}</span></div>
      </div>

      <div className="grid-2">
        <section className="card">
          <h2>Основные цвета</h2>
          <div className="color-list">
            {Object.entries(profile.palette_roles).slice(0, 8).map(([role, hex]) => (
              <div className="color-chip" key={role}>
                <span className="color-dot" style={{ background: hex }} aria-hidden="true" />
                <span><strong>{COLOR_NAMES[role] ?? "Цвет"}</strong><br /><span className="muted small">{hex}</span></span>
              </div>
            ))}
          </div>
        </section>
        <section className="card">
          <h2>Шрифты</h2>
          {profile.type_scale.families.length ? (
            <p className="lead">{profile.type_scale.families.join(", ")}</p>
          ) : (
            <p className="muted">Не удалось определить. Будут использованы шрифты из макетов.</p>
          )}
          <p className="field-hint">Размеры и начертания будут подобраны по образцам в шаблоне.</p>
        </section>
      </div>

      {warnings.length > 0 && (
        <section className="notice">
          <h2>Что стоит проверить</h2>
          <ul className="warning-list">{warnings.map((line) => <li key={line}>{line}</li>)}</ul>
        </section>
      )}

      <div className="row-actions">
        <Link className="button" href={`/templates/${params.templateId}/brief`}>Перейти к заданию</Link>
      </div>
    </div>
  );
}
