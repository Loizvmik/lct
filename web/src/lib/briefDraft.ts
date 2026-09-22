// Черновик брифа в localStorage, по одному на шаблон (Task 16, п.1 —
// «вернуться к брифу и перегенерировать с другим текстом, не загружая
// шаблон заново»). Шаблон уже разобран и лежит в кеше API по `template_id`
// — здесь хранится только то, что человек напечатал в форме, чтобы при
// возврате с экрана вариантов/аудита форма не была пустой.
export interface BriefDraft {
  title: string;
  brief: string;
  sources: string;
  targetSlides: number | "";
  autofix: boolean;
}

function key(templateId: string): string {
  return `deckforge:brief-draft:${templateId}`;
}

export function loadBriefDraft(templateId: string): BriefDraft | null {
  try {
    const raw = window.localStorage.getItem(key(templateId));
    if (!raw) return null;
    return JSON.parse(raw) as BriefDraft;
  } catch {
    return null;
  }
}

export function saveBriefDraft(templateId: string, draft: BriefDraft): void {
  try {
    window.localStorage.setItem(key(templateId), JSON.stringify(draft));
  } catch {
    // приватный режим/квота — черновик просто не переживёт переход, это не критично
  }
}
