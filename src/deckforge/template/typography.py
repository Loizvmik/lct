"""Типографическая шкала шаблона: display/h1/h2/body/caption/micro, интерлиньяж,
идиоматичность bold/italic, гарнитуры — из lstStyle плейсхолдеров лейаутов и
фактической гистограммы кеглей на слайдах, не из txStyles мастера (заглушка
Google-экспорта, см. theme.py).

Источники по убыванию доверия (брифом, Step 2):
1. `a:lstStyle/a:lvl1pPr/a:defRPr/@sz` конкретного плейсхолдера лейаута,
   нормированный к эталонному холсту (`Canvas.norm`) и округлённый тем же
   шагом 0.5pt, что и `usage.sizes_pt` (`round(pt * 2) / 2`) — иначе дробные
   кегли Google-пересчёта (8.12, 6.75, 14.06 у VK Tech, п.5 разведки)
   рассыпают гистограмму в шум вместо чистых ступеней.
2. Гистограмма `sz` по run'ам слайдов — уже собрана в `Usage.sizes_pt`, но
   БЕЗ разбивки по типу плейсхолдера; для "мода среди run'ов title/body-
   плейсхолдеров" этого недостаточно (`Usage` не хранит, какому шейпу
   принадлежит run), поэтому этот модуль сам обходит слайды за size+ph_type.
3. Медиана по ph_type — когда ни лейаут, ни явные run'ы ничего не дают.

Оба тира (1 и 2) для title/body суммируются как голоса "эта ступень
встретилась ещё раз" — один и тот же кегль, устойчиво повторённый и в
дизайне лейаутов, и в реальном тексте слайдов, надёжнее любого из источников
по отдельности (проверено разведкой: для WorkSpace/Education это меняет
победителя моды в сторону кегля, который РЕАЛЬНО используется на слайдах, а
не просто задан в шаблоне лейаута и, возможно, никогда не тронут).

body для WorkSpace — ключевой случай задачи: в лейаутах WorkSpace задан
только title (36pt/54pt), body-шкалы там нет вовсе (п.3 разведки). Когда ни
лейаут, ни явные run'ы body-плейсхолдеров ничего не дают, body достраивается
из МОДЫ ПО СИМВОЛАМ среди вообще всех run'ов шаблона (Step 2 брифа: "при их
отсутствии — мода по символам среди всех run'ов") — не по числу run'ов, как
для остальных источников: короткая подпись из трёх run'ов не должна перевесить
абзац из одного length-run'а на пятьдесят символов.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass, field

from deckforge.ooxml.geometry import Canvas
from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.walk import walk_shapes
from deckforge.template.grid import cluster
from deckforge.template.usage import FontUsage, Usage

# Шаг округления нормированного кегля — тот же, что в usage.py
# (`round(pt * 2) / 2`), для прямой сопоставимости источников 1 и 2.
_SIZE_ROUNDING_STEP = 0.5

# Прореживание шкалы: "два значения ближе 1.5pt схлопываются в одно"
# (брифом, Step 2) — переиспользует общую cluster() из grid.py.
_SCALE_THINNING_TOLERANCE_PT = 1.5

_TITLE_PH_TYPES = frozenset({"title", "ctrTitle"})
_BODY_PH_TYPES = frozenset({"body", "subTitle"})

# Признаки моноширинных гарнитур по имени (брифом, п.8: "Consolas-подобных").
# Список шире одного Consolas — на неизвестном шаблоне защиты моноширинный
# шрифт может называться иначе; частичное совпадение подстроки без учёта
# регистра ловит основные семейства, реально встречающиеся в .pptx (Courier,
# generic "Mono"/"Code" в названии, системные Menlo/Monaco/Cascadia).
_MONO_FONT_MARKERS = ("consolas", "courier", "mono", "code", "menlo", "monaco", "cascadia")

# Синтетические ступени ниже body, когда среди кандидатов нет ни одной
# реально наблюдаемой ступени: типографский шаг ~0.8 — общепринятая
# практика (каждая следующая ступень заметно, но не драматично мельче).
_CAPTION_TO_BODY_RATIO = 0.8
_MICRO_TO_CAPTION_RATIO = 0.8

# Доля run'ов с явным b="1"/i="1" от total_runs, начиная с которой стиль
# считается идиоматичным (управляющим приёмом шаблона), а не редким
# исключением. На трёх учебных шаблонах максимум — 6.3% (WorkSpace, bold);
# порог 15% берётся с заметным запасом, чтобы шаблон, где bold — реальный
# приём (не эти три, но возможный на защите), не был ошибочно отсеян.
_IDIOMATIC_STYLE_SHARE = 0.15

_DEFAULT_ALIGN = "l"  # дефолт OOXML для a:pPr/@algn, когда нет ни одного явного значения
_DEFAULT_HEADING_LINE_SPACING = 0.9
_DEFAULT_BODY_LINE_SPACING = 1.0


@dataclass(frozen=True)
class TypeScale:
    steps: dict[str, float]
    heading_line_spacing: float
    body_line_spacing: float
    default_align: str
    bold_is_idiomatic: bool
    italic_is_idiomatic: bool
    families: list[str]
    # Моноширинные гарнитуры (код) — отдельно от families, не в счёт "не
    # больше двух" (брифом, п.8; families не должно ложно раздуваться).
    mono: list[str] = field(default_factory=list)
    # Уверенность для каждой ступени steps — общее требование задачи ("каждое
    # число... должно нести меру уверенности"), не отдельное поле интерфейса
    # брифа. Смысл см. _step_confidence.
    step_confidence: dict[str, float] = field(default_factory=dict)


def build_type_scale(pkg: PptxPackage, canvas: Canvas, usage: Usage) -> TypeScale:
    layout_title, layout_body = _layout_defrpr_sizes(pkg, canvas)
    slide_title, slide_body, all_run_chars = _slide_run_sizes(pkg, canvas)

    title_votes = layout_title + slide_title
    body_votes = layout_body + slide_body

    # Общий пул кандидатов ступеней: лейаут-декларации (оба типа
    # плейсхолдеров) плюс полная гистограмма run'ов слайдов (Usage.sizes_pt,
    # источник 2) — из него после прореживания строится лестница
    # display/h2/caption/micro.
    candidate_pool: Counter[float] = Counter()
    candidate_pool.update(layout_title)
    candidate_pool.update(layout_body)
    candidate_pool.update(usage.sizes_pt)
    thinned = sorted(
        c.center for c in cluster(list(candidate_pool.elements()), _SCALE_THINNING_TOLERANCE_PT)
    )

    display = thinned[-1] if thinned else 0.0

    if title_votes:
        h1 = _mode(title_votes)
    elif len(thinned) >= 2:
        # Нет данных о title вовсе — вторая по величине прореженная ступень
        # разумнее, чем совпадение h1 с display (заголовок обычно не самый
        # крупный текст на слайде, это часто декоративный/акцентный элемент).
        h1 = thinned[-2]
    else:
        h1 = display
    h1 = min(h1, display)  # h1 не может быть крупнее display по построению шкалы

    if body_votes:
        body = _mode(body_votes)
    elif all_run_chars:
        body = _mode(all_run_chars)
    elif thinned:
        body = thinned[0]
    else:
        body = 0.0
    body = min(body, h1)  # защита монотонности на непредвиденных данных (см. докстроку модуля)

    h2 = _step_between(thinned, low=body, high=h1)
    caption = _step_below(thinned, above=body, ratio=_CAPTION_TO_BODY_RATIO)
    micro = _step_below(thinned, above=caption, ratio=_MICRO_TO_CAPTION_RATIO)

    steps = {"micro": micro, "caption": caption, "body": body, "h2": h2, "h1": h1, "display": display}
    step_confidence = _step_confidence(
        steps, thinned=thinned, title_votes=title_votes, body_votes=body_votes, all_run_chars=all_run_chars,
    )

    heading_line_spacing, body_line_spacing = _line_spacing(pkg, canvas)
    families, mono = _families_and_mono(usage.fonts)

    total_runs = usage.total_runs or 1
    bold_is_idiomatic = usage.bold_runs / total_runs >= _IDIOMATIC_STYLE_SHARE
    italic_is_idiomatic = usage.italic_runs / total_runs >= _IDIOMATIC_STYLE_SHARE
    default_align = _mode(usage.align) if usage.align else _DEFAULT_ALIGN

    return TypeScale(
        steps=steps,
        heading_line_spacing=heading_line_spacing,
        body_line_spacing=body_line_spacing,
        default_align=default_align,
        bold_is_idiomatic=bold_is_idiomatic,
        italic_is_idiomatic=italic_is_idiomatic,
        families=families,
        mono=mono,
        step_confidence=step_confidence,
    )


def _slide_parts(pkg: PptxPackage) -> list[str]:
    return sorted(n for n in pkg.names() if n.startswith("ppt/slides/slide") and n.endswith(".xml"))


def _layout_parts(pkg: PptxPackage) -> list[str]:
    return sorted(n for n in pkg.names() if n.startswith("ppt/slideLayouts/slideLayout") and n.endswith(".xml"))


def _normalize_size(sz_raw: str, canvas: Canvas) -> float:
    pt = int(sz_raw) / 100 * canvas.norm
    return round(pt / _SIZE_ROUNDING_STEP) * _SIZE_ROUNDING_STEP


def _layout_defrpr_sizes(pkg: PptxPackage, canvas: Canvas) -> tuple[Counter[float], Counter[float]]:
    """Источник 1: `a:lstStyle/a:lvl1pPr/a:defRPr/@sz` плейсхолдеров лейаутов,
    по одному голосу на плейсхолдер (не по символам — это декларация
    оформления, не текст)."""
    title_votes: Counter[float] = Counter()
    body_votes: Counter[float] = Counter()
    for part in _layout_parts(pkg):
        root = pkg.xml(part)
        for ref in walk_shapes(root, canvas, include_groups=False):
            if not ref.is_placeholder:
                continue
            sz = _placeholder_defrpr_sz(ref.element, canvas)
            if sz is None:
                continue
            if ref.ph_type in _TITLE_PH_TYPES:
                title_votes[sz] += 1
            elif ref.ph_type in _BODY_PH_TYPES:
                body_votes[sz] += 1
    return title_votes, body_votes


def _placeholder_defrpr_sz(element, canvas: Canvas) -> float | None:
    tx_body = element.find(qn("p:txBody"))
    lst_style = tx_body.find(qn("a:lstStyle")) if tx_body is not None else None
    lvl1 = lst_style.find(qn("a:lvl1pPr")) if lst_style is not None else None
    def_rpr = lvl1.find(qn("a:defRPr")) if lvl1 is not None else None
    sz_raw = def_rpr.get("sz") if def_rpr is not None else None
    return _normalize_size(sz_raw, canvas) if sz_raw is not None else None


def _slide_run_sizes(pkg: PptxPackage, canvas: Canvas) -> tuple[Counter[float], Counter[float], Counter[float]]:
    """Источник 2: явные `a:rPr/@sz` run'ов на слайдах — по типу плейсхолдера
    их содержащего шейпа (title_votes/body_votes, по одному голосу на run) и
    отдельно по символам среди ВСЕХ run'ов (all_run_chars — фолбэк для body,
    см. докстроку модуля)."""
    title_votes: Counter[float] = Counter()
    body_votes: Counter[float] = Counter()
    all_run_chars: Counter[float] = Counter()

    for part in _slide_parts(pkg):
        root = pkg.xml(part)
        for ref in walk_shapes(root, canvas, include_groups=False):
            if ref.kind != "shape":
                continue
            category = None
            if ref.is_placeholder and ref.ph_type in _TITLE_PH_TYPES:
                category = "title"
            elif ref.is_placeholder and ref.ph_type in _BODY_PH_TYPES:
                category = "body"
            for sz, chars in _run_sizes(ref.element, canvas):
                all_run_chars[sz] += chars
                if category == "title":
                    title_votes[sz] += 1
                elif category == "body":
                    body_votes[sz] += 1

    return title_votes, body_votes, all_run_chars


def _run_sizes(sp_element, canvas: Canvas):
    tx_body = sp_element.find(qn("p:txBody"))
    if tx_body is None:
        return
    for p in tx_body.findall(qn("a:p")):
        for r in p.findall(qn("a:r")):
            r_pr = r.find(qn("a:rPr"))
            sz_raw = r_pr.get("sz") if r_pr is not None else None
            if sz_raw is None:
                continue
            t_el = r.find(qn("a:t"))
            chars = len(t_el.text or "") if t_el is not None else 0
            yield _normalize_size(sz_raw, canvas), chars


def _mode(counter: Counter) -> float:
    """Ключ с наибольшим весом; при равенстве — больший ключ (детерминированный
    тай-брейк, не влияет ни на один из трёх учебных шаблонов — там ничьих не
    возникло, но на незнакомом шаблоне защиты гарантирует стабильный выбор)."""
    return max(counter.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _step_between(thinned: list[float], *, low: float, high: float) -> float:
    """h2 — ближайшая наблюдаемая ступень СТРОГО между body и h1. Если такой
    нет (в прореженной шкале body и h1 — соседи), синтезируем геометрическую
    середину, округлённую до 0.1pt."""
    between = [c for c in thinned if low < c < high]
    if between:
        return max(between)
    return round((low + high) / 2, 1)


def _step_below(thinned: list[float], *, above: float, ratio: float) -> float:
    """caption/micro — ближайшая наблюдаемая ступень ниже `above`; если такой
    нет, синтезируем через типографский коэффициент (см. _CAPTION_TO_BODY_
    RATIO/_MICRO_TO_CAPTION_RATIO)."""
    below = [c for c in thinned if c < above]
    if below:
        return max(below)
    return round(above * ratio, 1)


def _step_confidence(
    steps: dict[str, float], *, thinned: list[float],
    title_votes: Counter[float], body_votes: Counter[float], all_run_chars: Counter[float],
) -> dict[str, float]:
    """Уверенность по каждой ступени — общее требование задачи. h1/body:
    доля голосов выигравшего значения от суммы голосов его категории (явный
    сигнал того же рода, что confidence в Grid). h2/caption/micro: 0.5, если
    ступень реально наблюдалась в прореженном пуле кандидатов, иначе 0.3
    (синтезирована коэффициентом — не выдумана из воздуха, но и не измерена).
    display: 1.0, если пул кандидатов вообще был непуст."""
    def _vote_share(counter: Counter[float], value: float) -> float:
        total = sum(counter.values())
        return counter[value] / total if total else 0.0

    if title_votes:
        h1_conf = _vote_share(title_votes, steps["h1"])
    else:
        h1_conf = 0.2  # чистый фолбэк по позиции в шкале, не по наблюдению

    if body_votes:
        body_conf = _vote_share(body_votes, steps["body"])
    elif all_run_chars:
        # Фолбэк на моду по символам — на ступень ниже по надёжности
        # источника, отражаем это множителем 0.7.
        body_conf = 0.7 * _vote_share(all_run_chars, steps["body"])
    else:
        body_conf = 0.1

    observed = set(thinned)
    return {
        "display": 1.0 if thinned else 0.0,
        "h1": h1_conf,
        "h2": 0.5 if steps["h2"] in observed else 0.3,
        "body": body_conf,
        "caption": 0.5 if steps["caption"] in observed else 0.3,
        "micro": 0.5 if steps["micro"] in observed else 0.3,
    }


def _families_and_mono(fonts: dict[str, FontUsage]) -> tuple[list[str], list[str]]:
    """Два самых весомых по символам не-моноширинных шрифта → families;
    моноширинные (по имени, см. _MONO_FONT_MARKERS) — отдельно в mono, чтобы
    не раздувать families и не ломать аудит T01 "гарнитур больше двух"
    (брифом, п.8)."""
    mono = sorted(name for name in fonts if _is_mono(name))
    mono_set = set(mono)
    families = sorted((name for name in fonts if name not in mono_set), key=lambda name: -fonts[name].chars)
    return families[:2], mono


def _is_mono(family: str) -> bool:
    lowered = family.lower()
    return any(marker in lowered for marker in _MONO_FONT_MARKERS)


def _line_spacing(pkg: PptxPackage, canvas: Canvas) -> tuple[float, float]:
    """heading — мода интерлиньяжа title-плейсхолдеров; body — мода среди
    ВСЕГО ОСТАЛЬНОГО (не только body-плейсхолдеров: у VK Tech основной текст
    в значительной части сидит в нередактируемых свободных фигурах-карточках,
    не в формальных плейсхолдерах — ограничение title-плейсхолдеров дало бы
    те же 90%, что и title, и завалило бы правило "заголовок теснее текста",
    см. разведку в scratchpad)."""
    title_sp, nontitle_sp = _paragraph_spacing(pkg, canvas)
    heading = _mode(title_sp) / 100 if title_sp else _DEFAULT_HEADING_LINE_SPACING
    body = _mode(nontitle_sp) / 100 if nontitle_sp else _DEFAULT_BODY_LINE_SPACING
    return heading, body


def _paragraph_spacing(pkg: PptxPackage, canvas: Canvas) -> tuple[Counter[float], Counter[float]]:
    title_sp: Counter[float] = Counter()
    nontitle_sp: Counter[float] = Counter()
    for part in _slide_parts(pkg) + _layout_parts(pkg):
        root = pkg.xml(part)
        for ref in walk_shapes(root, canvas, include_groups=False):
            if ref.kind != "shape":
                continue
            is_title = ref.is_placeholder and ref.ph_type in _TITLE_PH_TYPES
            bucket = title_sp if is_title else nontitle_sp
            for value in _shape_paragraph_spacings(ref.element):
                bucket[value] += 1
    return title_sp, nontitle_sp


def _shape_paragraph_spacings(sp_element):
    tx_body = sp_element.find(qn("p:txBody"))
    if tx_body is None:
        return
    for p in tx_body.findall(qn("a:p")):
        p_pr = p.find(qn("a:pPr"))
        if p_pr is None:
            continue
        ln_spc = p_pr.find(qn("a:lnSpc"))
        spc_pct = ln_spc.find(qn("a:spcPct")) if ln_spc is not None else None
        if spc_pct is not None and spc_pct.get("val") is not None:
            yield int(spc_pct.get("val")) / 1000
