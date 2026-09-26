export type DefaultSlideCount = "auto" | 6 | 8 | 10 | 12 | 15;
export type ExportFormat = "pptx" | "pdf" | "html";

export interface AppSettings {
  defaultSlideCount: DefaultSlideCount;
  defaultAutofix: boolean;
  preferredExportFormat: ExportFormat;
  rememberDrafts: boolean;
}

export const DEFAULT_APP_SETTINGS: AppSettings = {
  defaultSlideCount: "auto",
  defaultAutofix: true,
  preferredExportFormat: "pptx",
  rememberDrafts: true,
};

export const EXPORT_FORMATS: ReadonlyArray<{ value: ExportFormat; label: string }> = [
  { value: "pptx", label: "PowerPoint" },
  { value: "pdf", label: "PDF" },
  { value: "html", label: "Веб-версия" },
];

const STORAGE_KEY = "slides:settings:v1";
const CHANGE_EVENT = "slides:settings-change";
const DRAFT_PREFIXES = ["slides:task-draft:v2:", "deckforge:brief-draft:"];

function isDefaultSlideCount(value: unknown): value is DefaultSlideCount {
  return value === "auto" || value === 6 || value === 8 || value === 10 || value === 12 || value === 15;
}

function isExportFormat(value: unknown): value is ExportFormat {
  return value === "pptx" || value === "pdf" || value === "html";
}

export function getAppSettings(): AppSettings {
  if (typeof window === "undefined") return DEFAULT_APP_SETTINGS;
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}") as Partial<AppSettings>;
    return {
      defaultSlideCount: isDefaultSlideCount(saved.defaultSlideCount)
        ? saved.defaultSlideCount
        : DEFAULT_APP_SETTINGS.defaultSlideCount,
      defaultAutofix: typeof saved.defaultAutofix === "boolean"
        ? saved.defaultAutofix
        : DEFAULT_APP_SETTINGS.defaultAutofix,
      preferredExportFormat: isExportFormat(saved.preferredExportFormat)
        ? saved.preferredExportFormat
        : DEFAULT_APP_SETTINGS.preferredExportFormat,
      rememberDrafts: typeof saved.rememberDrafts === "boolean"
        ? saved.rememberDrafts
        : DEFAULT_APP_SETTINGS.rememberDrafts,
    };
  } catch {
    localStorage.removeItem(STORAGE_KEY);
    return DEFAULT_APP_SETTINGS;
  }
}

export function saveAppSettings(settings: AppSettings): void {
  if (typeof window === "undefined") return;
  localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  window.dispatchEvent(new CustomEvent<AppSettings>(CHANGE_EVENT, { detail: settings }));
}

export function subscribeToAppSettings(listener: (settings: AppSettings) => void): () => void {
  if (typeof window === "undefined") return () => undefined;
  const handleChange = (event: Event) => {
    listener((event as CustomEvent<AppSettings>).detail ?? getAppSettings());
  };
  window.addEventListener(CHANGE_EVENT, handleChange);
  return () => window.removeEventListener(CHANGE_EVENT, handleChange);
}

export function clearSavedTaskDrafts(): number {
  if (typeof window === "undefined") return 0;
  const keys = Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index))
    .filter((key): key is string => Boolean(key) && DRAFT_PREFIXES.some((prefix) => key!.startsWith(prefix)));
  keys.forEach((key) => localStorage.removeItem(key));
  return keys.length;
}
