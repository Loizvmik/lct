"use client";

// Блок «Фотографии» на экране задания (задача D2). Файлы уходят на сервер
// сразу при выборе: так превью показывает то, что реально сохранилось, а
// отказ по размеру или типу виден до нажатия «Создать». Подпись правится
// здесь и едет в задание вместе с `photo_id`.
import { useRef, useState } from "react";
import { PreviewImage } from "@/components/PreviewImage";
import { assetUrl, MAX_PHOTO_BYTES, MAX_PHOTOS, uploadPhotos, UploadedPhoto } from "@/lib/api";

export interface PickedPhoto {
  photo_id: string;
  name: string;
  url: string;
  caption: string;
}

export function toPicked(photos: UploadedPhoto[]): PickedPhoto[] {
  return photos.map((p) => ({ photo_id: p.photo_id, name: p.name, url: p.url, caption: p.caption ?? "" }));
}

const ACCEPTED = /\.(jpe?g|png)$/i;

export default function PhotoPicker({
  photos,
  onChange,
  disabled,
}: {
  photos: PickedPhoto[];
  onChange: (next: PickedPhoto[]) => void;
  disabled?: boolean;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const room = MAX_PHOTOS - photos.length;

  async function pick(list: FileList | null) {
    const files = Array.from(list ?? []);
    if (input.current) input.current.value = "";
    if (!files.length) return;
    // Проверка здесь только ради понятного сообщения без круга к серверу;
    // окончательно файл проверяет сервер, по содержимому.
    const wrongType = files.find((f) => !ACCEPTED.test(f.name));
    if (wrongType) { setError(`«${wrongType.name}»: подходят только файлы .jpg и .png.`); return; }
    const tooBig = files.find((f) => f.size > MAX_PHOTO_BYTES);
    if (tooBig) { setError(`«${tooBig.name}» больше 10 МБ.`); return; }
    if (files.length > room) { setError(`Можно добавить не больше ${MAX_PHOTOS} фотографий, осталось мест: ${room}.`); return; }
    setError(null);
    setUploading(true);
    try {
      const uploaded = await uploadPhotos(files);
      onChange([...photos, ...toPicked(uploaded)]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось загрузить фотографии.");
    } finally {
      setUploading(false);
    }
  }

  function setCaption(id: string, caption: string) {
    onChange(photos.map((p) => (p.photo_id === id ? { ...p, caption } : p)));
  }

  return (
    <div className="field">
      <span className="field-label" id="photos-label">Фотографии <span className="counter">{photos.length} из {MAX_PHOTOS}</span></span>
      <input
        ref={input}
        className="file-input"
        type="file"
        accept=".jpg,.jpeg,.png,image/jpeg,image/png"
        multiple
        aria-label="Выбрать фотографии"
        onChange={(e) => pick(e.target.files)}
        disabled={disabled || uploading || room <= 0}
      />
      {photos.length > 0 && (
        <ul className="photo-grid" aria-labelledby="photos-label">
          {photos.map((photo, index) => (
            <li className="photo-item" key={photo.photo_id}>
              <div className="photo-thumb"><PreviewImage src={assetUrl(photo.url)} alt={photo.name} /></div>
              <div className="photo-meta">
                <span className="photo-name" title={photo.name}>{photo.name}</span>
                <input
                  type="text"
                  value={photo.caption}
                  maxLength={300}
                  onChange={(e) => setCaption(photo.photo_id, e.target.value)}
                  aria-label={`Подпись к фото ${index + 1}`}
                  placeholder="Что на снимке, например: команда на стендапе"
                  disabled={disabled}
                />
                <button
                  type="button"
                  className="ghost small-button"
                  onClick={() => onChange(photos.filter((p) => p.photo_id !== photo.photo_id))}
                  disabled={disabled}
                  aria-label={`Убрать фото ${photo.name}`}
                >
                  Убрать
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      <div className="row-actions photo-actions">
        <button
          type="button"
          className="secondary"
          onClick={() => input.current?.click()}
          disabled={disabled || uploading || room <= 0}
        >
          {uploading ? "Загружаем…" : photos.length ? "Добавить ещё" : "Выбрать фотографии"}
        </button>
      </div>
      <p className="field-hint">Необязательно. До {MAX_PHOTOS} снимков .jpg или .png, каждый до 10 МБ. Фото лягут на подходящие слайды, подпись помогает выбрать слайд.</p>
      {error && <p className="field-error" role="alert">{error}</p>}
    </div>
  );
}
