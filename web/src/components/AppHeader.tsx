"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { getJob, getProfile, healthcheck, TemplateProfile } from "@/lib/api";
import StepNav from "@/components/StepNav";

function SettingsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const pathname = usePathname();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [apiAvailable, setApiAvailable] = useState<boolean | null>(null);
  const [profile, setProfile] = useState<TemplateProfile | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    healthcheck().then(() => setApiAvailable(true)).catch(() => setApiAvailable(false));

    const templateMatch = pathname.match(/^\/templates\/([^/]+)/);
    const deckMatch = pathname.match(/^\/decks\/([^/]+)/);
    if (templateMatch) {
      getProfile(decodeURIComponent(templateMatch[1]))
        .then((result) => setProfile(result.profile))
        .catch(() => setProfile(null));
      return;
    }
    if (deckMatch) {
      getJob(decodeURIComponent(deckMatch[1]))
        .then((job) => getProfile(job.template_id))
        .then((result) => setProfile(result.profile))
        .catch(() => setProfile(null));
      return;
    }
    const timer = window.setTimeout(() => setProfile(null), 0);
    return () => window.clearTimeout(timer);
  }, [open, pathname]);

  return (
    <dialog
      className="settings-dialog"
      ref={dialogRef}
      onClose={onClose}
      onCancel={onClose}
      aria-labelledby="settings-title"
    >
      <div className="dialog-header">
        <div>
          <p className="eyebrow">Приложение</p>
          <h2 id="settings-title">Настройки</h2>
        </div>
        <button className="icon-button" type="button" onClick={onClose} aria-label="Закрыть настройки">
          ×
        </button>
      </div>

      <section className="settings-section" aria-labelledby="interface-settings">
        <h3 id="interface-settings">Интерфейс</h3>
        <div className="settings-row">
          <span>Язык</span>
          <strong>Русский</strong>
        </div>
        <p className="helper-text">Основные действия и пояснения написаны без технических терминов.</p>
      </section>

      <details className="diagnostics">
        <summary>Диагностика</summary>
        <p className="helper-text">
          Эти сведения помогают понять состояние приложения. Секретные ключи и локальные пути здесь не показываются.
        </p>
        <dl className="diagnostics-list">
          <div>
            <dt>Сервер обработки</dt>
            <dd>{apiAvailable === null ? "Проверяем…" : apiAvailable ? "Доступен" : "Недоступен"}</dd>
          </div>
          <div>
            <dt>Текущий шаблон</dt>
            <dd>{profile?.source_name ?? "Не выбран"}</dd>
          </div>
          {profile && (
            <>
              <div>
                <dt>Найдено макетов</dt>
                <dd>{profile.layouts.length}</dd>
              </div>
              <div>
                <dt>Вариантов размещения</dt>
                <dd>{profile.patterns.length}</dd>
              </div>
              <div>
                <dt>Технических предупреждений</dt>
                <dd>{profile.warnings.length}</dd>
              </div>
            </>
          )}
        </dl>
        {profile && profile.warnings.length > 0 && (
          <details className="raw-diagnostics">
            <summary>Показать технические сообщения</summary>
            <ul>
              {profile.warnings.map((warning, index) => (
                <li key={`${warning}-${index}`}>{warning}</li>
              ))}
            </ul>
          </details>
        )}
      </details>
    </dialog>
  );
}

export default function AppHeader() {
  const [settingsOpen, setSettingsOpen] = useState(false);

  return (
    <header className="topbar">
      <Link className="brand" href="/" aria-label="Слайды — на главную">
        <span className="brand-mark" aria-hidden="true">С</span>
        <span className="brand-text">Слайды</span>
      </Link>
      <StepNav />
      <div className="topbar-actions">
        <button className="secondary" type="button" aria-label="Открыть настройки" onClick={() => setSettingsOpen(true)}>
          <span className="settings-label">Настройки</span><span aria-hidden="true">⚙</span>
        </button>
        <Link className="button compact" href="/">
          Новая презентация
        </Link>
      </div>
      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </header>
  );
}
