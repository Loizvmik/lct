"""Фрагменты кода источников и слайд под них, до письма (задача T2).

Бриф прямо просит «код показывать как код», а писатель пересказывал
правило маршрутизации словами: у плана не было блока «код». Теперь решает
этот слой, без модели, тем же приёмом, что `plan.data_types` для графиков:
каждый fenced-блок источника (```python ... ```) становится намерением
`CodeIntent`, структура отдаёт ему пункт формы `code`, контракт везёт
фрагмент готовым, и писатель пишет только заголовок-вывод и подпись. Код
на слайд идёт дословно из источника: сгенерированный моделью код выглядит
настоящим и потому хуже любого пересказа.

Единственная правка фрагмента: по строкам под раскладку (`fit_code`).
Длинный фрагмент режется с «…», и это видно в заметках докладчика."""
from __future__ import annotations
import re
import textwrap
from dataclasses import dataclass, replace

from deckforge.plan.series import stems

# Ограда Markdown: ``` или ~~~, необязательная метка языка, тело до такой
# же ограды. Ограда внутри строки текста блоком кода не считается.
_FENCE_RE = re.compile(
    r"^(?P<fence>```|~~~)[ \t]*(?P<lang>[\w+#.-]*)[^\n]*\n(?P<body>.*?)^(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)

# Строк кода на слайде: меньше 14 фрагмент теряет смысл, больше 18 кегль
# моно уходит ниже 10 pt на обычной рамке (задача T2).
MIN_CODE_LINES = 14
MAX_CODE_LINES = 18
DEFAULT_CODE_LINES = 16
# Знаков в строке: шире строка не читается с экрана и на рамке в две
# трети слайда требует кегля мельче 10 pt.
MAX_CODE_COLS = 80
# Слайдов с кодом в колоде: дальше колода становится листингом.
MAX_CODE_SLIDES = 2
# Меньше строк фрагмент не показ кода, а строка в тексте.
MIN_SNIPPET_LINES = 2
ELLIPSIS = "…"
_TAB = "    "

# Слова пункта структуры, по которым видно, что он пересказывает код.
_CODE_WORDS = ("код", "правил", "пример", "скрипт", "функци", "api", "sql", "конфиг")


@dataclass(frozen=True)
class CodeIntent:
    """Один фрагмент кода источника. Интерфейс тот же, что у
    `data_types.VisualIntent` (`type`, `required`, `reason`, `data_ref`,
    `payload`, `to_dict`): структура, намерение слайда и контракт везут
    его в том же поле `visual_intent`."""
    data_ref: str
    language: str
    code: str
    heading: str = ""
    context: str = ""
    required: bool = True
    type: str = "code"
    chart_kind: str | None = None
    data: object | None = None

    @property
    def reason(self) -> str:
        lang = f" на {self.language}" if self.language else ""
        return f"в источнике фрагмент кода{lang}: показать как код, не пересказом"

    @property
    def words(self) -> set[str]:
        return stems(" ".join([self.heading, self.context, self.language]))

    def payload(self) -> dict:
        return {"language": self.language, "code": self.code}

    def to_dict(self) -> dict:
        return {
            "type": self.type, "required": self.required, "reason": self.reason, "data_ref": self.data_ref,
            "language": self.language, "lines": len(self.code.splitlines()),
        }


def normalize_code(code: str) -> str:
    """Табуляция в пробелы, общий отступ и хвостовые пробелы убраны,
    пустые строки по краям сняты. Смысл кода не меняется: так он ляжет на
    слайд без переносов и без пустой первой строки."""
    lines = [line.replace("\t", _TAB).rstrip() for line in code.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return textwrap.dedent("\n".join(lines))


def find_code_blocks(sources) -> list[CodeIntent]:
    """Fenced-блоки всех источников (`outline.SourceDoc`) подряд; `data_ref`
    по порядку: c1, c2, ... Контекст фрагмента: ближайший заголовок выше и
    абзац прямо перед оградой, по ним фрагмент ищет свой пункт структуры."""
    found: list[CodeIntent] = []
    for doc in sources or []:
        text = getattr(doc, "text", "") or ""
        for m in _FENCE_RE.finditer(text):
            code = normalize_code(m.group("body"))
            if len([line for line in code.splitlines() if line.strip()]) < MIN_SNIPPET_LINES:
                continue
            before = text[:m.start()]
            headings = _HEADING_RE.findall(before)
            paragraph = before.rstrip().rsplit("\n\n", 1)[-1].strip()
            if paragraph.startswith("#"):
                paragraph = ""
            found.append(CodeIntent(
                data_ref=f"c{len(found) + 1}", language=m.group("lang").lower(), code=code,
                heading=headings[-1] if headings else "", context=paragraph,
                required=len(found) < MAX_CODE_SLIDES,
            ))
    return found


def fit_code(code: str, max_lines: int = DEFAULT_CODE_LINES, max_cols: int = MAX_CODE_COLS) -> tuple[str, bool]:
    """Фрагмент под место: не больше `max_lines` строк и `max_cols` знаков в
    строке. Лишнее отрезается целыми строками, последняя строка «…»;
    длинная строка обрезается с «…» на конце. Второе значение: резали ли.
    Слова не переносятся: перенос внутри строки кода меняет её смысл на
    глаз, обрезка с «…» честно показывает, что строка длиннее."""
    lines = code.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = [*lines[:max(1, max_lines - 1)], ELLIPSIS]
        truncated = True
    out = []
    for line in lines:
        if len(line) > max_cols:
            line = line[:max(1, max_cols - 1)].rstrip() + ELLIPSIS
            truncated = True
        out.append(line)
    return "\n".join(out), truncated


def code_payload(vi: CodeIntent, max_lines: int, max_cols: int = MAX_CODE_COLS) -> dict:
    """Данные контракта писателя: фрагмент, уже укороченный под место."""
    code, truncated = fit_code(vi.code, max_lines, max_cols)
    out = {"language": vi.language, "code": code, "truncated": truncated}
    if truncated:
        out["lines_total"] = len(vi.code.splitlines())
    return out


# ---------------------------------------------------------------------------
# Пункт структуры под фрагмент
# ---------------------------------------------------------------------------

_HERO_KINDS = ("title", "closing")
_NEAR_WORDS = 2


def _slide_text(slide) -> str:
    return " ".join([slide.intent, *slide.needs])


def _overlap(slide, vi: CodeIntent) -> int:
    return len(stems(_slide_text(slide)) & vi.words)


def _mentions_code(slide) -> bool:
    text = _slide_text(slide).lower()
    return any(w in text for w in _CODE_WORDS)


def apply_code_intents(slides: list, sources, *, max_slides: int) -> list:
    """Пункты формы `code` по фрагментам источников.

    1. Пункт, которому модель структуры назначила `code`, получает самый
       близкий по словам свободный фрагмент; фрагментов нет вовсе: форма
       снимается (показать нечего, выдумывать код нельзя).
    2. Обязательный фрагмент (`CodeIntent.required`), который никто не
       взял, получает пункт сам: пункт без формы, который по словам и так
       о нём (пересказывает правило или пример), переделывается под код;
       иначе новый пункт встаёт после самого близкого; при полной колоде
       (`max_slides`) под код переделывается самый близкий пункт без формы.
    Каждый фрагмент достаётся одному пункту."""
    snippets = find_code_blocks(sources)
    slides = list(slides)
    if not slides:
        return slides
    used: set[str] = set()
    for i, slide in enumerate(slides):
        if slide.form != "code" or slide.visual_intent is not None:
            continue
        pool = [vi for vi in snippets if vi.data_ref not in used]
        if not pool:
            # Фрагмента в источниках нет: формы «код» у пункта нет,
            # писать нечего, выдумывать код нельзя.
            slides[i] = replace(slide, form=None)
            continue
        best = max(enumerate(pool), key=lambda p: (_overlap(slide, p[1]), -p[0]))[1]
        slides[i] = replace(slide, items=1, visual_intent=best)
        used.add(best.data_ref)

    for vi in snippets:
        if not vi.required or vi.data_ref in used:
            continue
        content = [i for i, s in enumerate(slides) if s.kind not in _HERO_KINDS and s.visual_intent is None]
        free = [i for i in content if slides[i].form is None and slides[i].kind != "agenda"]
        retold = [i for i in free if _mentions_code(slides[i]) and _overlap(slides[i], vi) >= _NEAR_WORDS]
        if retold:
            target = max(retold, key=lambda i: (_overlap(slides[i], vi), -i))
            slides[target] = replace(slides[target], form="code", items=1, visual_intent=vi)
        elif len(slides) < max_slides:
            near = max(content, key=lambda i: (_overlap(slides[i], vi), -i), default=None)
            at = near + 1 if near is not None and _overlap(slides[near], vi) >= _NEAR_WORDS else max(1, len(slides) - 1)
            slides.insert(at, _new_slide(slides[0].__class__, vi))
        elif free:
            target = max(free, key=lambda i: (_overlap(slides[i], vi), -i))
            slides[target] = replace(slides[target], form="code", items=1, visual_intent=vi)
        else:
            continue
        used.add(vi.data_ref)
    return slides


def _new_slide(cls, vi: CodeIntent):
    what = vi.heading or "пример из источника"
    lang = f" ({vi.language})" if vi.language else ""
    return cls(
        kind="how_it_works",
        intent=f"Как это устроено в коде: «{what}»{lang}",
        needs=[f"{vi.reason}; вывод: что делает этот фрагмент"],
        items=1, form="code", visual_intent=vi,
    )
