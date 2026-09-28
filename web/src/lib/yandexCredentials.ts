// Свой ключ Yandex AI Studio пользователя. Лежит только в этом браузере,
// отдельно от прочих настроек (`slides:settings:v1`): те можно показать или
// сбросить целиком, а ключ нет. На сервер уходит заголовками с каждым
// запросом, который запускает модель, и там нигде не сохраняется.
export interface YandexCredentials {
  apiKey: string;
  folderId: string;
}

const STORAGE_KEY = "donor:yandex-credentials:v1";
const CHANGE_EVENT = "donor:yandex-credentials-change";

export function getYandexCredentials(): YandexCredentials | null {
  if (typeof window === "undefined") return null;
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null") as Partial<YandexCredentials> | null;
    const apiKey = typeof saved?.apiKey === "string" ? saved.apiKey.trim() : "";
    const folderId = typeof saved?.folderId === "string" ? saved.folderId.trim() : "";
    return apiKey && folderId ? { apiKey, folderId } : null;
  } catch {
    return null;
  }
}

function notify(): void {
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

export function saveYandexCredentials(credentials: YandexCredentials): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      apiKey: credentials.apiKey.trim(),
      folderId: credentials.folderId.trim(),
    }));
  } catch {
    // Хранилище недоступно (приватный режим): ключ просто не запомнится.
  }
  notify();
}

export function forgetYandexCredentials(): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    // нечего удалять
  }
  notify();
}

export function subscribeToYandexCredentials(listener: () => void): () => void {
  if (typeof window === "undefined") return () => undefined;
  window.addEventListener(CHANGE_EVENT, listener);
  return () => window.removeEventListener(CHANGE_EVENT, listener);
}

// Заголовки ключа для запроса; пустой объект, если своего ключа нет, и
// тогда сервер работает на своём ключе из `.env`.
export function yandexHeaders(credentials: YandexCredentials | null = getYandexCredentials()): Record<string, string> {
  if (!credentials) return {};
  return { "X-Yandex-Api-Key": credentials.apiKey, "X-Yandex-Folder-Id": credentials.folderId };
}
