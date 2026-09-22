"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { uploadTemplate } from "@/lib/api";

export default function UploadPage() {
  const router = useRouter();
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  async function handleFile(file: File) {
    setBusy(true);
    setError(null);
    try {
      const result = await uploadTemplate(file);
      router.push(`/templates/${result.template_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось загрузить шаблон");
      setBusy(false);
    }
  }

  return (
    <div>
      <h1>Шаг 1 — загрузите .pptx-шаблон</h1>
      <p className="muted">
        DeckForge разберёт файл на дизайн-систему: палитру с ролями, шкалу кеглей,
        сетку, каталог макетов и композиционных паттернов — это займёт меньше секунды
        на знакомом шаблоне и около секунды на новом.
      </p>

      {error && <div className="error-banner">{error}</div>}

      <div
        className={`dropzone${dragging ? " drag" : ""}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          const file = event.dataTransfer.files?.[0];
          if (file) void handleFile(file);
        }}
      >
        {busy ? (
          <>
            <div className="spinner" /> Разбираем шаблон…
          </>
        ) : (
          <>
            Перетащите .pptx сюда или нажмите, чтобы выбрать файл
            <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>
              Только .pptx — OOXML-контейнер PowerPoint
            </div>
          </>
        )}
      </div>
      <input
        ref={inputRef}
        type="file"
        accept=".pptx"
        style={{ display: "none" }}
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void handleFile(file);
        }}
      />
    </div>
  );
}
