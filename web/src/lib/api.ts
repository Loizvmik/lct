// Клиент DeckForge API (Task 16) — тонкая обёртка над `fetch`, без
// дополнительных библиотек: эндпоинтов немного, и типы здесь — прямое
// зеркало `deckforge.api.schemas` (см. комментарии у каждого типа).
export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

export type Stage = "parse" | "outline" | "write" | "compose" | "audit" | "export";
export type JobStatus = "running" | "done" | "error";
export type VariantName = "dense" | "airy" | "visual";

export interface TemplateUploadResponse {
  template_id: string;
  profile: TemplateProfile;
}

// Только поля профиля, которые реально показывает интерфейс — остальное
// (`layouts`/`patterns` целиком несут куда больше служебных полей) читаем
// как `unknown`-совместимый широкий тип и берём точечно.
export interface TemplateProfile {
  source_name: string;
  canvas_width_emu: number;
  canvas_height_emu: number;
  palette_roles: Record<string, string>;
  palette_roles_source: string;
  type_scale: {
    steps: Record<string, number>;
    families: string[];
  };
  grid: {
    margin_left: number;
    margin_right: number;
    margin_top: number;
    margin_bottom: number;
    columns: { center: number; count: number; confidence: number }[];
  };
  layouts: { layout_id: string; name: string; kind: string; kind_confidence: number }[];
  patterns: { pattern_id: string; kind: string; layout_id: string; score: number }[];
  provenance: string[];
  warnings: string[];
}

export interface JobResponse {
  job_id: string;
  template_id: string;
  status: JobStatus;
  stage: Stage | null;
  stages: Stage[];
  deck_id: string | null;
  error: string | null;
}

export interface FindingBox {
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface Finding {
  id: string;
  check_id: string;
  severity: "critical" | "major" | "minor";
  slide_index: number | null;
  shape_ref: string | null;
  message: string;
  box: FindingBox | null;
  fixable: boolean;
  fix_hint: string;
}

export interface VariantSummary {
  variant: VariantName;
  slide_count: number;
  preview_pngs: string[];
  findings: Finding[];
  by_severity: Record<string, number>;
  by_check: Record<string, number>;
  autofixed_count: number;
}

export interface FixResponse {
  variant: VariantName;
  applied: string[];
  skipped: string[];
  findings: Finding[];
  preview_pngs: string[];
}

async function asJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      // тело не JSON — оставляем statusText
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export function assetUrl(path: string): string {
  return path.startsWith("http") ? path : `${API_BASE}${path}`;
}

export async function uploadTemplate(file: File): Promise<TemplateUploadResponse> {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(`${API_BASE}/api/templates`, { method: "POST", body: form });
  return asJson(response);
}

export async function getProfile(templateId: string): Promise<TemplateUploadResponse> {
  const response = await fetch(`${API_BASE}/api/templates/${templateId}/profile`);
  return asJson(response);
}

export interface CreateDeckRequest {
  template_id: string;
  brief: string;
  sources: string[];
  title?: string;
  language?: string;
  target_slides?: number;
  autofix?: boolean;
}

export async function createDeck(payload: CreateDeckRequest): Promise<{ job_id: string }> {
  const response = await fetch(`${API_BASE}/api/decks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return asJson(response);
}

export async function getJob(jobId: string): Promise<JobResponse> {
  const response = await fetch(`${API_BASE}/api/jobs/${jobId}`);
  return asJson(response);
}

// Прогресс генерации потоком SSE (Step 2 брифа: "клиент читает их через
// SSE") — `onUpdate` вызывается на каждый снимок задания, поток сам
// закрывается сервером после `status` "done"/"error"; `onUpdate` также
// возвращает разорвать ли подписку (`EventSource` умеет автопереподключение,
// нам оно не нужно после терминального статуса).
export function subscribeJobEvents(
  jobId: string,
  onUpdate: (job: JobResponse) => void,
  onError?: () => void,
): () => void {
  const source = new EventSource(`${API_BASE}/api/jobs/${jobId}/events`);
  source.onmessage = (event) => {
    const job = JSON.parse(event.data) as JobResponse;
    onUpdate(job);
    if (job.status === "done" || job.status === "error") {
      source.close();
    }
  };
  source.onerror = () => {
    onError?.();
    source.close();
  };
  return () => source.close();
}

export async function getVariants(deckId: string): Promise<VariantSummary[]> {
  const response = await fetch(`${API_BASE}/api/decks/${deckId}/variants`);
  return asJson(response);
}

export async function applyFix(
  deckId: string,
  variant: VariantName,
  findingIds: string[],
): Promise<FixResponse> {
  const response = await fetch(`${API_BASE}/api/decks/${deckId}/fix`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ variant, finding_ids: findingIds }),
  });
  return asJson(response);
}

export function exportUrl(deckId: string, variant: VariantName, format: "pptx" | "pdf" | "html"): string {
  return `${API_BASE}/api/decks/${deckId}/export?format=${format}&variant=${variant}`;
}

export const STAGE_LABELS: Record<Stage, string> = {
  parse: "Разбор шаблона",
  outline: "Структура презентации",
  write: "Текст слайдов",
  compose: "Вёрстка трёх вариантов",
  audit: "Детерминированный аудит",
  export: "Выгрузка (PDF/HTML/превью)",
};

export const STAGE_ORDER: Stage[] = ["parse", "outline", "write", "compose", "audit", "export"];

export const VARIANT_LABELS: Record<VariantName, string> = {
  dense: "Плотный",
  airy: "Воздушный",
  visual: "Визуальный",
};

export const SEVERITY_LABELS: Record<string, string> = {
  critical: "Критично",
  major: "Существенно",
  minor: "Незначительно",
};
