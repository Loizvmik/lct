// Клиент API — тонкая обёртка над `fetch`, без
// дополнительных библиотек: эндпоинтов немного, и типы здесь — прямое
// зеркало `deckforge.api.schemas` (см. комментарии у каждого типа).
import { YandexCredentials, yandexHeaders } from "@/lib/yandexCredentials";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

export type Stage = "parse" | "outline" | "write" | "compose" | "audit" | "export";
// Сервер отдаёт `status: "done"` и отдельно `outcome`; `done_with_warnings`
// здесь состояние для интерфейса, его собирает `jobState` из этих двух полей.
export type JobStatus = "running" | "done" | "done_with_warnings" | "error";
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
  // Задача Q: одно задание = одна презентация одного стиля; задания,
  // созданные одной кнопкой, несут общий `batch_id`.
  style: VariantName;
  batch_id: string | null;
  mode: string | null;
  seconds: number | null;
  budget?: {
    budget_seconds: number;
    elapsed_seconds: number;
    stage_seconds: Record<string, number>;
    skipped: Record<string, string>;
    mode: string | null;
    // Стадии, полученные готовыми (разбор шаблона, общая структура
    // пакета): значение «переиспользовано».
    shared_stages?: Record<string, string>;
  } | null;
  // Задача M: сводка аудита по картинке — по КАЖДОМУ варианту (ключ —
  // имя варианта), не одна на всю колоду, как было, пока аудитом
  // накрывали только dense.
  visual_audit?: Record<
    string,
    {
      ran: boolean;
      skipped_reason: string | null;
      slides?: number[];
      findings?: number;
      seconds?: number;
    }
  > | null;
  // Задача R: по вариантам находки, которые автопочинка не трогает, потому
  // что нужен другой текст или другая раскладка.
  structural?: Record<string, { slide_index: number | null; check_id: string; message: string }[]> | null;
  // Задача U: по вариантам сколько слайдов какой ступенью лестницы сборки
  // собрано (ключи: `LADDER_TITLES`).
  ladder?: Record<string, Record<string, number>> | null;
  // Задача W: итог готового задания. `done_with_warnings`, если что-то
  // сделано запасным путём (жёсткий потолок бюджета, фото не легло);
  // `warnings` перечисляет, что именно.
  outcome?: "done" | "done_with_warnings" | null;
  warnings?: string[];
  // Задача D2: сколько фото пришло, сколько распределено, сколько в файле.
  photos?: { sent: number; planned: number; embedded: number | null; notes: string[] } | null;
}

// Состояние задания для интерфейса: готовое с предупреждениями отличается
// от просто готового, хотя сервер у обоих держит `status: "done"`.
export function jobState(job: Pick<JobResponse, "status" | "outcome">): JobStatus {
  if (job.status === "done" && job.outcome === "done_with_warnings") return "done_with_warnings";
  return job.status;
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
  repair?: "local" | "structural" | "none";
}

export interface VariantSummary {
  variant: VariantName;
  slide_count: number;
  preview_pngs: string[];
  findings: Finding[];
  by_severity: Record<string, number>;
  by_check: Record<string, number>;
  autofixed_count: number;
  // Задача M: средние оценки PPTEval (content/design) этого варианта —
  // отсутствуют, пока аудит по картинке не прошёл или ничего не оценил.
  content_avg?: number | null;
  design_avg?: number | null;
  // Задача T: метрики верности шаблону, снимок `FidelityReport`.
  fidelity?: { summary?: string } | null;
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
  // Разбор шаблона зовёт модель (имена цветов, виды раскладок), поэтому
  // тоже несёт ключ пользователя.
  const response = await fetch(`${API_BASE}/api/templates`, { method: "POST", body: form, headers: yandexHeaders() });
  return asJson(response);
}

// `server_key`: есть ли ключ модели в `.env` сервера. Без него и без своего
// ключа презентация соберётся запасными путями, без модели.
// Модель генерации по умолчанию из реестра сервера (`config/models.yaml`).
export interface ModelInfo {
  id: string;
  hf: string;
  license: string;
  params_b: number;
  active_params_b: number | null;
  vision: boolean;
  provider: string;
}

export function describeModel(model: ModelInfo): string {
  const size = model.active_params_b
    ? `${model.params_b}B параметров, ${model.active_params_b}B активных`
    : `${model.params_b}B параметров`;
  return `${model.hf.split("/").pop()} · ${size} · ${model.license} · ${model.provider}`;
}

export async function healthcheck(): Promise<{ status: string; stages: Stage[]; server_key?: boolean; model?: ModelInfo | null }> {
  const response = await fetch(`${API_BASE}/api/health`);
  return asJson(response);
}

export async function checkYandexCredentials(credentials: YandexCredentials): Promise<{ ok: boolean; message: string }> {
  const response = await fetch(`${API_BASE}/api/credentials/check`, {
    method: "POST",
    headers: yandexHeaders(credentials),
  });
  return asJson(response);
}

export async function getProfile(templateId: string): Promise<TemplateUploadResponse> {
  const response = await fetch(`${API_BASE}/api/templates/${templateId}/profile`);
  return asJson(response);
}

// Задача D2: фотографии пользователя. Загружаются сразу при выборе, задание
// потом ссылается на них по `photo_id`.
export interface UploadedPhoto {
  photo_id: string;
  name: string;
  caption: string | null;
  url: string;
}

export const MAX_PHOTOS = 10;
export const MAX_PHOTO_BYTES = 10 * 1024 * 1024;

export async function uploadPhotos(files: File[], captions: string[] = []): Promise<UploadedPhoto[]> {
  const form = new FormData();
  files.forEach((file, index) => {
    form.append("files", file);
    form.append("captions", captions[index] ?? "");
  });
  const response = await fetch(`${API_BASE}/api/photos`, { method: "POST", body: form });
  return (await asJson<{ photos: UploadedPhoto[] }>(response)).photos;
}

export async function loadExamplePhotos(example: string): Promise<UploadedPhoto[]> {
  const response = await fetch(`${API_BASE}/api/examples/${encodeURIComponent(example)}/photos`, { method: "POST" });
  return (await asJson<{ photos: UploadedPhoto[] }>(response)).photos;
}

export interface CreateDeckRequest {
  template_id: string;
  brief: string;
  sources: string[];
  title?: string;
  language?: string;
  target_slides?: number;
  autofix?: boolean;
  photos?: { photo_id: string; caption?: string }[];
}

export async function createDeck(
  payload: CreateDeckRequest & { style?: VariantName },
): Promise<{ job_id: string }> {
  const response = await fetch(`${API_BASE}/api/decks`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...yandexHeaders() },
    body: JSON.stringify(payload),
  });
  return asJson(response);
}

// Задача Q: несколько стилей одним запросом — по заданию на стиль, они идут
// параллельно, каждое со своим бюджетом; структура считается один раз.
export async function createDeckBatch(
  payload: CreateDeckRequest & { styles: VariantName[] },
): Promise<{ batch_id: string; job_ids: string[] }> {
  const response = await fetch(`${API_BASE}/api/decks/batch`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...yandexHeaders() },
    body: JSON.stringify(payload),
  });
  return asJson(response);
}

export async function listJobs(batchId?: string): Promise<JobResponse[]> {
  const query = batchId ? `?batch_id=${encodeURIComponent(batchId)}` : "";
  const response = await fetch(`${API_BASE}/api/jobs${query}`);
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
    if (isJobFinished(job.status)) {
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
  parse: "Читаем шаблон",
  outline: "Составляем план",
  write: "Готовим тексты",
  compose: "Оформляем слайды",
  audit: "Проверяем результат",
  export: "Подготавливаем файлы",
};

// Задание закончилось результатом, с замечаниями или без: и переход к
// вариантам, и шаги навигации ведут себя одинаково.
export function isJobDone(status: JobStatus | undefined | null): boolean {
  return status === "done" || status === "done_with_warnings";
}

export function isJobFinished(status: JobStatus | undefined | null): boolean {
  return isJobDone(status) || status === "error";
}

export const JOB_STATUS_LABELS: Record<JobStatus, string> = {
  running: "Идёт",
  done: "Готово",
  done_with_warnings: "Готово с предупреждениями",
  error: "Ошибка",
};

// Ступени лестницы сборки (`compose.builder.LADDER_RUNGS`) в порядке от
// самой бережной к самой дальней от шаблона.
export const LADDER_TITLES: Record<string, string> = {
  clone: "по раскладке шаблона",
  adapt: "по запасной раскладке",
  shorten: "с сокращённым текстом",
  split: "разделено на два",
  scratch: "собрано с нуля",
};

export const STAGE_ORDER: Stage[] = ["parse", "outline", "write", "compose", "audit", "export"];

export const VARIANT_ORDER: VariantName[] = ["dense", "airy", "visual"];

export const MODE_LABELS: Record<string, string> = {
  full: "Полный",
  fast: "Быстрый",
  emergency: "Аварийный",
};

export const VARIANT_LABELS: Record<VariantName, string> = {
  dense: "Плотный",
  airy: "Воздушный",
  visual: "Визуальный",
};

export const VARIANT_DESCRIPTIONS: Record<VariantName, string> = {
  dense: "Больше фактов и деталей на каждом слайде.",
  airy: "Крупнее текст, больше свободного пространства.",
  visual: "Больше визуальных акцентов и короче формулировки.",
};

export const SEVERITY_LABELS: Record<string, string> = {
  critical: "Критично",
  major: "Существенно",
  minor: "Незначительно",
};
