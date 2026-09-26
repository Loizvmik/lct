"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { uploadTemplate } from "@/lib/api";

const MAX_FILE_SIZE = 100 * 1024 * 1024;

export default function UploadPage() {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleFile(file: File) {
    if (!file.name.toLowerCase().endsWith(".pptx")) {
      setError("Выберите презентацию в формате .pptx.");
      return;
    }
    if (file.size > MAX_FILE_SIZE) {
      setError("Файл больше 100 МБ. Уменьшите его и попробуйте снова.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await uploadTemplate(file);
      router.push(`/templates/${result.template_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось загрузить шаблон.");
      setBusy(false);
    }
  }

  return (
    <div>
      <header className="page-header">
        <p className="eyebrow">Шаг 1 из 4</p>
        <h1>Загрузите шаблон презентации</h1>
        <p className="lead">
          Мы определим цвета, шрифты и макеты, чтобы новые слайды выглядели как часть вашего шаблона.
        </p>
      </header>

      {error && <div className="error-banner" role="alert">{error}</div>}

      <div
        className={`dropzone${dragging ? " drag" : ""}`}
        onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          const file = event.dataTransfer.files?.[0];
          if (file) void handleFile(file);
        }}
      >
        <div>
          <span className="dropzone-icon" aria-hidden="true">↑</span>
          <h2>{busy ? "Изучаем шаблон…" : "Перетащите файл сюда"}</h2>
          <p className="muted">{busy ? "Не закрывайте эту страницу." : "Подойдёт файл PowerPoint в формате .pptx размером до 100 МБ."}</p>
          {!busy && (
            <button type="button" onClick={() => inputRef.current?.click()}>
              Выбрать файл
            </button>
          )}
          {busy && <span className="spinner" aria-label="Загрузка" />}
        </div>
      </div>
      <input
        ref={inputRef}
        className="file-input"
        type="file"
        accept=".pptx,application/vnd.openxmlformats-officedocument.presentationml.presentation"
        aria-label="Выбрать шаблон PowerPoint"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void handleFile(file);
          event.currentTarget.value = "";
        }}
      />
    </div>
  );
}
