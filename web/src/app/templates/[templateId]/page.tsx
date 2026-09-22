"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { getProfile, TemplateProfile } from "@/lib/api";

const ROLE_ORDER = ["brand", "accent", "surface", "on_surface", "muted", "border", "danger", "warning"];
const STEP_ORDER = ["display", "h1", "h2", "body", "caption", "micro"];

export default function TemplateProfilePage() {
  const params = useParams<{ templateId: string }>();
  const [profile, setProfile] = useState<TemplateProfile | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getProfile(params.templateId)
      .then((res) => {
        if (!cancelled) setProfile(res.profile);
      })
      .catch((err) => !cancelled && setError(err.message));
    return () => {
      cancelled = true;
    };
  }, [params.templateId]);

  if (error) return <div className="error-banner">{error}</div>;
  if (!profile) return <p className="muted">Загружаем дизайн-систему…</p>;

  const roles = Object.entries(profile.palette_roles).sort(
    (a, b) => ROLE_ORDER.indexOf(a[0]) - ROLE_ORDER.indexOf(b[0]),
  );
  const steps = Object.entries(profile.type_scale.steps).sort(
    (a, b) => STEP_ORDER.indexOf(a[0]) - STEP_ORDER.indexOf(b[0]),
  );
  const patternsByKind: Record<string, number> = {};
  for (const pattern of profile.patterns) {
    patternsByKind[pattern.kind] = (patternsByKind[pattern.kind] ?? 0) + 1;
  }

  return (
    <div>
      <h1>Дизайн-система шаблона «{profile.source_name}»</h1>

      <div className="grid-2">
        <div className="card">
          <h2>Палитра ролей {profile.palette_roles_source === "model" ? "(названо моделью)" : "(запасной вариант)"}</h2>
          <div className="swatch-row">
            {roles.map(([role, hex]) => (
              <div className="swatch" key={role}>
                <span className="dot" style={{ background: hex }} />
                {role} · {hex}
              </div>
            ))}
          </div>
        </div>

        <div className="card">
          <h2>Типографическая шкала</h2>
          {steps.map(([step, pt]) => (
            <div className="type-scale-row" key={step}>
              <span className="step-name">{step}</span>
              <span style={{ fontSize: Math.min(28, Math.max(11, pt)) }}>{pt.toFixed(1)}pt — Aa</span>
            </div>
          ))}
          <p className="muted" style={{ marginTop: 10 }}>
            Гарнитуры: {profile.type_scale.families.join(", ") || "не определены"}
          </p>
        </div>

        <div className="card">
          <h2>Сетка</h2>
          <p style={{ fontFamily: "var(--mono)", fontSize: 13 }}>
            поля: слева {profile.grid.margin_left.toFixed(3)}, справа{" "}
            {profile.grid.margin_right.toFixed(3)}, сверху {profile.grid.margin_top.toFixed(3)}, снизу{" "}
            {profile.grid.margin_bottom.toFixed(3)} (доли холста)
          </p>
          <p className="muted">{profile.grid.columns.length} направляющих колонок восстановлено кластеризацией</p>
        </div>

        <div className="card">
          <h2>Каталог паттернов вёрстки ({profile.patterns.length})</h2>
          <div className="swatch-row">
            {Object.entries(patternsByKind).map(([kind, count]) => (
              <span className="pill kind" key={kind}>
                {kind} × {count}
              </span>
            ))}
          </div>
          <p className="muted" style={{ marginTop: 10 }}>
            {profile.layouts.length} макетов в шаблоне
          </p>
        </div>
      </div>

      <div className="card">
        <h2>Откуда что взято</h2>
        <ul className="provenance-list">
          {profile.provenance.map((line, i) => (
            <li key={i}>{line}</li>
          ))}
        </ul>
      </div>

      {profile.warnings.length > 0 && (
        <div className="card">
          <h2>Предупреждения разбора</h2>
          <ul className="warning-list">
            {profile.warnings.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="row-actions">
        <Link className="button" href={`/templates/${params.templateId}/brief`}>
          Дальше: бриф и материалы →
        </Link>
      </div>
    </div>
  );
}
