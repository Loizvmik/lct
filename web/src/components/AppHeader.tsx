"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { getJob, getProfile, healthcheck, TemplateProfile } from "@/lib/api";
import {
  AppSettings,
  clearSavedTaskDrafts,
  DEFAULT_APP_SETTINGS,
  EXPORT_FORMATS,
  getAppSettings,
  saveAppSettings,
} from "@/lib/appSettings";
import StepNav from "@/components/StepNav";

function CloseButton({ onClick, label }: { onClick: () => void; label: string }) {
  return (
    <button className="icon-button" type="button" onClick={onClick} aria-label={label}>
      <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
        <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
      </svg>
    </button>
  );
}

function SettingsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const pathname = usePathname();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [apiAvailable, setApiAvailable] = useState<boolean | null>(null);
  const [profile, setProfile] = useState<TemplateProfile | null>(null);
  const [preferences, setPreferences] = useState<AppSettings>(DEFAULT_APP_SETTINGS);
  const [draftMessage, setDraftMessage] = useState<string | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const settingsTimer = window.setTimeout(() => {
      setPreferences(getAppSettings());
      setDraftMessage(null);
    }, 0);
    healthcheck().then(() => setApiAvailable(true)).catch(() => setApiAvailable(false));

    const templateMatch = pathname.match(/^\/templates\/([^/]+)/);
    const deckMatch = pathname.match(/^\/decks\/([^/]+)/);
    if (templateMatch) {
      getProfile(decodeURIComponent(templateMatch[1]))
        .then((result) => setProfile(result.profile))
        .catch(() => setProfile(null));
      return () => window.clearTimeout(settingsTimer);
    }
    if (deckMatch) {
      getJob(decodeURIComponent(deckMatch[1]))
        .then((job) => getProfile(job.template_id))
        .then((result) => setProfile(result.profile))
        .catch(() => setProfile(null));
      return () => window.clearTimeout(settingsTimer);
    }
    const timer = window.setTimeout(() => setProfile(null), 0);
    return () => {
      window.clearTimeout(timer);
      window.clearTimeout(settingsTimer);
    };
  }, [open, pathname]);

  function updatePreferences(next: Partial<AppSettings>) {
    const updated = { ...preferences, ...next };
    setPreferences(updated);
    saveAppSettings(updated);
  }

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
        <CloseButton onClick={onClose} label="Закрыть настройки" />
      </div>

      <section className="settings-section" aria-labelledby="creation-settings">
        <h3 id="creation-settings">Создание презентации</h3>
        <div className="settings-field">
          <label htmlFor="default-slides">Количество слайдов по умолчанию</label>
          <select
            id="default-slides"
            value={preferences.defaultSlideCount}
            onChange={(event) => updatePreferences({
              defaultSlideCount: event.target.value === "auto" ? "auto" : Number(event.target.value) as 6 | 8 | 10 | 12 | 15,
            })}
          >
            <option value="auto">Выбирать автоматически</option>
            <option value="6">6 слайдов</option>
            <option value="8">8 слайдов</option>
            <option value="10">10 слайдов</option>
            <option value="12">12 слайдов</option>
            <option value="15">15 слайдов</option>
          </select>
          <p className="helper-text">Применится к новому заданию. Количество можно изменить перед созданием.</p>
        </div>
        <label className="settings-check">
          <input
            type="checkbox"
            checked={preferences.defaultAutofix}
            onChange={(event) => updatePreferences({ defaultAutofix: event.target.checked })}
          />
          <span><strong>Автоматически исправлять безопасные проблемы</strong><small>Остальные замечания останутся на последнем шаге.</small></span>
        </label>
      </section>

      <section className="settings-section" aria-labelledby="download-settings">
        <h3 id="download-settings">Скачивание</h3>
        <div className="settings-field">
          <label htmlFor="preferred-format">Предпочтительный формат</label>
          <select
            id="preferred-format"
            value={preferences.preferredExportFormat}
            onChange={(event) => updatePreferences({ preferredExportFormat: event.target.value as AppSettings["preferredExportFormat"] })}
          >
            {EXPORT_FORMATS.map((format) => <option value={format.value} key={format.value}>{format.label}</option>)}
          </select>
          <p className="helper-text">Этот формат будет первым среди вариантов скачивания.</p>
        </div>
      </section>

      <section className="settings-section" aria-labelledby="draft-settings">
        <h3 id="draft-settings">Черновики</h3>
        <label className="settings-check">
          <input
            type="checkbox"
            checked={preferences.rememberDrafts}
            onChange={(event) => updatePreferences({ rememberDrafts: event.target.checked })}
          />
          <span><strong>Запоминать введённый текст в этом браузере</strong><small>Помогает вернуться к незавершённому заданию после обновления страницы.</small></span>
        </label>
        <button
          className="secondary settings-clear"
          type="button"
          onClick={() => {
            const removed = clearSavedTaskDrafts();
            setDraftMessage(removed ? "Сохранённые черновики удалены." : "Сохранённых черновиков нет.");
          }}
        >
          Удалить сохранённые черновики
        </button>
        {draftMessage && <p className="helper-text" role="status">{draftMessage}</p>}
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

function NewPresentationDialog({ open, onClose, onConfirm }: { open: boolean; onClose: () => void; onConfirm: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      className="confirm-dialog"
      ref={dialogRef}
      onClose={onClose}
      onCancel={onClose}
      aria-labelledby="new-presentation-title"
    >
      <div className="dialog-header">
        <div>
          <p className="eyebrow">Новая работа</p>
          <h2 id="new-presentation-title">Начать новую презентацию?</h2>
        </div>
        <CloseButton onClick={onClose} label="Закрыть окно" />
      </div>
      <div className="dialog-body">
        <p>Вы перейдёте к загрузке новой презентации-образца. Несохранённые изменения на текущей странице могут потеряться.</p>
        <div className="dialog-actions">
          <button className="secondary" type="button" onClick={onClose}>Остаться</button>
          <button type="button" onClick={onConfirm}>Начать новую</button>
        </div>
      </div>
    </dialog>
  );
}

export default function AppHeader() {
  const router = useRouter();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [newPresentationOpen, setNewPresentationOpen] = useState(false);

  return (
    <header className="topbar">
      <Link className="brand" href="/" aria-label="Донор — на главную">
        <Image className="brand-mark" src="/brand/donor-mark.svg" width={36} height={36} alt="" aria-hidden="true" />
        <Image className="brand-wordmark" src="/brand/donor-wordmark.svg" width={94} height={29} alt="" aria-hidden="true" />
      </Link>
      <StepNav />
      <div className="topbar-actions">
        <button className="secondary" type="button" aria-label="Открыть настройки" onClick={() => setSettingsOpen(true)}>
          <span className="settings-label">Настройки</span><span aria-hidden="true">⚙</span>
        </button>
        <button className="compact" type="button" onClick={() => setNewPresentationOpen(true)}>
          Новая презентация
        </button>
      </div>
      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      <NewPresentationDialog
        open={newPresentationOpen}
        onClose={() => setNewPresentationOpen(false)}
        onConfirm={() => {
          setNewPresentationOpen(false);
          router.push("/");
        }}
      />
    </header>
  );
}
