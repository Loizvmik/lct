export interface BriefDraft {
  title: string;
  brief: string;
  sources: string;
  targetSlides: number | "";
  autofix: boolean;
}

const key = (templateId: string) => `slides:task-draft:v2:${templateId}`;
const legacyKey = (templateId: string) => `deckforge:brief-draft:${templateId}`;

function looksLikeBundledExample(draft: BriefDraft): boolean {
  return draft.title.includes("Сокращение времени согласования заявок")
    || draft.brief.includes("автоматической маршрутизации заявок");
}

export function loadBriefDraft(templateId: string): BriefDraft | null {
  if (typeof window === "undefined") return null;
  const current = localStorage.getItem(key(templateId));
  if (current) {
    try { return JSON.parse(current) as BriefDraft; } catch { localStorage.removeItem(key(templateId)); }
  }
  const legacy = localStorage.getItem(legacyKey(templateId));
  if (!legacy) return null;
  try {
    const draft = JSON.parse(legacy) as BriefDraft;
    localStorage.removeItem(legacyKey(templateId));
    if (looksLikeBundledExample(draft)) return null;
    saveBriefDraft(templateId, draft);
    return draft;
  } catch {
    localStorage.removeItem(legacyKey(templateId));
    return null;
  }
}

export function saveBriefDraft(templateId: string, draft: BriefDraft): void {
  if (typeof window !== "undefined") localStorage.setItem(key(templateId), JSON.stringify(draft));
}

export function clearBriefDraft(templateId: string): void {
  if (typeof window !== "undefined") {
    localStorage.removeItem(key(templateId));
    localStorage.removeItem(legacyKey(templateId));
  }
}
