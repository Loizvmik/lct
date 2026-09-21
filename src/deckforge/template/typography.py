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
2. Гистограмма `sz` по run'ам — уже собрана в `Usage.sizes_pt` (по слайдам,
   лейаутам И мастерам — см. `usage._all_shape_bearing_parts`), но БЕЗ
   разбивки по типу плейсхолдера; для "мода среди run'ов title/body-
   плейсхолдеров" этого недостаточно (`Usage` не хранит, какому шейпу
   принадлежит run), поэтому этот модуль сам обходит СЛАЙДЫ (не лейауты/
   мастера — там txStyles/lstStyle это декларация, не факт употребления) за
   size+ph_type.
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

Известное ограничение: текст ячеек таблиц (`a:tbl/a:tc`) не разбирается ни
одним из обходчиков этого модуля (в отличие от `usage.py`, который явно это
делает — см. `_collect_table` там) — попадает в `candidate_pool` только
транзитивно через `Usage.sizes_pt`, но не участвует в голосовании за
title/body и не входит в `all_run_chars`. На table-heavy шаблоне (Education,
3 таблицы) это может сдвинуть моду body, если основной текст сидит именно в
таблицах — не подтверждено разведкой ни на одном из трёх шаблонов, но стоит
иметь в виду при разборе шаблона с защиты.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass, field

from deckforge.ooxml.geometry import Canvas
from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.walk import walk_shapes
from deckforge.template.grid import BODY_PH_TYPES, TITLE_PH_TYPES, cluster
from deckforge.template.usage import FontUsage, Usage

# Шаг округления нормированного кегля — тот же, что в usage.py
# (`round(pt * 2) / 2`), для прямой сопоставимости источников 1 и 2, И для
# итогового вывода ступеней шкалы (см. _rounded_steps) — без этого центр
# кластера (среднее сырых значений) просачивался наружу нескруглённым числом
# вида 14.191503267973856 вперемешку с округлёнными до 0.1pt синтетическими
# ступенями (найдено general-purpose ревьюером, Task 4, повторное ревью).
_SIZE_ROUNDING_STEP = 0.5

# Прореживание шкалы: "два значения ближе 1.5pt схлопываются в одно"
# (брифом, Step 2) — переиспользует общую cluster() из grid.py.
_SCALE_THINNING_TOLERANCE_PT = 1.5

# Минимальная поддержка (число run'ов) кегля из гистограммы Usage.sizes_pt,
# чтобы он вообще попал в пул кандидатов ступеней шкалы. Без этого порога
# единичный декоративный run (иконка-цифра, порядковый номер шага крупным
# шрифтом — на VK Tech такой run с текстом "7" при sz=221.5pt нормированных
# встретился дважды на весь шаблон) становится "display" с confidence=1.0,
# хотя это не типографическая ступень, а случайная деталь оформления —
# нашёл adversarial-reviewer (Task 4, повторное ревью). Порог 3 отсекает
# ровно такие единичные/парные выбросы (проверено на всех трёх шаблонах:
# следующая по редкости легитимная ступень везде имеет поддержку от 3-4
# run'ов), не задевая реальные ступени шкалы — те по определению кегль,
# которым набрано много текста, а не один decorative run.
_MIN_RUN_HISTOGRAM_SUPPORT = 3

# TITLE_PH_TYPES/BODY_PH_TYPES — импортированы из grid.py (см. import выше),
# не продублированы: единый список категорий плейсхолдеров для обоих модулей.

# Признаки моноширинных гарнитур по имени (брифом, п.8: "Consolas-подобных").
# Список шире одного Consolas — на неизвестном шаблоне защиты моноширинный
# шрифт может называться иначе; частичное совпадение подстроки без учёта
# регистра ловит основные семейства, реально встречающиеся в .pptx (Courier,
# generic "Mono"/"Code" в названии, системные Menlo/Monaco/Cascadia).
_MONO_FONT_MARKERS = ("consolas", "courier", "mono", "code", "menlo", "monaco", "cascadia")

# Минимальная доля символов (от всех не-моноширинных), чтобы шрифт вообще
# считался гарнитурой шаблона, а не случайным вкраплением (импортированный
# слайд, буква-другая latin в основном кириллическом тексте). На VK Tech без
# порога Arial (5 символов из 7586, 0.07%) становится "второй гарнитурой
# шаблона" наравне с Play (7581 символ) — абсурд, который проходил мимо теста
# "не больше двух гарнитур" именно потому, что гарнитур и так было ровно две
# (нашёл general-purpose ревьюер). Порог 2% с большим запасом отсекает такие
# случайные вкрапления, не отсеивая реальную вторую гарнитуру (у Education
# Arial — 2814 из 7569 символов, 37% — далеко за порогом).
_MIN_FAMILY_CHAR_SHARE = 0.02

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
# Знаменатель (total_runs) шире числителя по построению (usage.py считает
# bold_runs только для run'ов с явным a:rPr — см. Usage.bold_runs), то есть
# фактическая доля СРЕДИ ЯВНО СТИЛИЗОВАННЫХ run'ов будет выше этого числа —
# на шаблоне с большой долей унаследованных (без явного rPr) run'ов порог
# может сработать позже, чем кажется на первый взгляд; на трёх учебных
# шаблонах (explicit_style_chars — большинство текста) это не меняет вывод.
_IDIOMATIC_STYLE_SHARE = 0.15

_DEFAULT_ALIGN = "l"  # дефолт OOXML для a:pPr/@algn, когда нет ни одного явного значения
_DEFAULT_HEADING_LINE_SPACING = 0.9
_DEFAULT_BODY_LINE_SPACING = 1.0

# Уверенность интерлиньяжа, когда a:lnSpc в шаблоне не найдено ни разу и
# число — чистая константа по умолчанию (_DEFAULT_HEADING_LINE_SPACING/
# _DEFAULT_BODY_LINE_SPACING), не измерение (находка повторного код-ревью,
# п.4: раньше 0.9/1.0 не отличались от честно измеренных значений). 0.0, не
# какое-то промежуточное число вроде 0.2/0.3, использованных для других
# "чистых фолбэков" шкалы кеглей (см. _step_confidence): те фолбэки всё же
# опираются на какую-то структуру данных (позицию в шкале, наличие соседних
# ступеней), а здесь измерения нет ВООБЩЕ — честный ответ "уверенность
# отсутствует", а не "низкая, но не нулевая".
_UNMEASURED_LINE_SPACING_CONFIDENCE = 0.0


@dataclass(frozen=True)
class TypeScale:
    steps: dict[str, float]
    heading_line_spacing: float
    body_line_spacing: float
    default_align: str
    bold_is_idiomatic: bool
    italic_is_idiomatic: bool
    # `families` — НОРМАЛИЗОВАННЫЕ имена (хвост начертания отброшен, см.
    # _normalize_family): "Montserrat" и "Montserrat Medium" — одна
    # гарнитура, не две (находка повторного код-ревью, п.5 — на контрольном
    # ЛЦТ2026 без нормализации families_total было 3 вместо 2).
    families: list[str]
    # Моноширинные гарнитуры (код) — отдельно от families, не в счёт "не
    # больше двух" (брифом, п.8; families не должно ложно раздуваться).
    # Тоже нормализованы тем же способом, для единообразия.
    mono: list[str] = field(default_factory=list)
    # Сколько всего НОРМАЛИЗОВАННЫХ не-моноширинных гарнитур прошло порог
    # _MIN_FAMILY_CHAR_SHARE — не только контрактных двух в `families`.
    # families всегда ограничено двумя (интерфейс брифа), но если
    # families_total > 2, аудит T01 "гарнитур больше двух" обязан сработать
    # по ЭТОМУ полю, а не по длине families (она искусственно обрезана и
    # всегда <= 2 — нашёл general-purpose ревьюер: без этого поля аудит
    # физически не может обнаружить третью гарнитуру).
    families_total: int = 0
    # Нормализованное семейство -> отсортированный список ФАКТИЧЕСКИХ имён
    # typeface, которыми оно набрано в файле (находка повторного код-ревью,
    # п.5: без стилевой связки .pptx ссылается на начертание буквально по
    # имени — вёрстке нужно точное raw-имя ("Montserrat Medium"), а не
    # только нормализованное ("Montserrat"), чтобы набрать текст тем
    # начертанием, которое реально есть в шаблоне). Ключи — подмножество
    # тех нормализованных имён, что прошли порог поддержки (не только
    # `families`, весь набор, см. families_total).
    family_variants: dict[str, list[str]] = field(default_factory=dict)
    # Уверенность для каждой ступени steps — общее требование задачи ("каждое
    # число... должно нести меру уверенности"), не отдельное поле интерфейса
    # брифа. Смысл см. _step_confidence.
    step_confidence: dict[str, float] = field(default_factory=dict)
    # Уверенность интерлиньяжа — тот же принцип, что и step_confidence
    # (находка повторного код-ревью, п.4): доля голосов моды среди
    # измеренных a:lnSpc, или _UNMEASURED_LINE_SPACING_CONFIDENCE (0.0),
    # если измерений не было вовсе и число — константа по умолчанию.
    heading_line_spacing_confidence: float = 0.0
    body_line_spacing_confidence: float = 0.0


def build_type_scale(pkg: PptxPackage, canvas: Canvas, usage: Usage) -> TypeScale:
    layout_title, layout_body = _layout_defrpr_sizes(pkg, canvas)
    slide_title, slide_body, all_run_chars = _slide_run_sizes(pkg, canvas)

    title_votes = layout_title + slide_title
    body_votes = layout_body + slide_body

    # Пул кандидатов ступеней: лейаут-декларации (без порога поддержки — это
    # явное дизайнерское решение, а не статистика по тексту, редкость сама
    # по себе не делает его недостоверным) плюс гистограмма run'ов слайдов,
    # ОТФИЛЬТРОВАННАЯ по _MIN_RUN_HISTOGRAM_SUPPORT (см. её докстроку).
    candidate_pool: Counter[float] = Counter()
    candidate_pool.update(layout_title)
    candidate_pool.update(layout_body)
    candidate_pool.update({sz: n for sz, n in usage.sizes_pt.items() if n >= _MIN_RUN_HISTOGRAM_SUPPORT})
    size_clusters = cluster(list(candidate_pool.elements()), _SCALE_THINNING_TOLERANCE_PT)
    thinned = sorted(c.center for c in size_clusters)
    support_by_center = {c.center: c.count for c in size_clusters}
    total_candidate_weight = sum(c.count for c in size_clusters)

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

    raw_steps = {"micro": micro, "caption": caption, "body": body, "h2": h2, "h1": h1, "display": display}
    step_confidence = _step_confidence(
        raw_steps, support_by_center=support_by_center, total_candidate_weight=total_candidate_weight,
        title_votes=title_votes, body_votes=body_votes, all_run_chars=all_run_chars,
    )
    # Округление — ПОСЛЕ вычисления confidence (которое ищет точное совпадение
    # с сырыми центрами кластеров/декларациями), но ДО возврата наружу: без
    # этого центр кластера (среднее сырых значений) просачивался как
    # 14.191503267973856pt рядом с округлёнными до 0.1pt синтетическими
    # ступенями — два разных уровня точности в одном словаре.
    steps = {k: round(v / _SIZE_ROUNDING_STEP) * _SIZE_ROUNDING_STEP for k, v in raw_steps.items()}

    heading_line_spacing, heading_line_spacing_confidence, body_line_spacing, body_line_spacing_confidence = (
        _line_spacing(pkg, canvas)
    )
    families, mono, families_total, family_variants = _families_and_mono(usage.fonts)

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
        families_total=families_total,
        family_variants=family_variants,
        step_confidence=step_confidence,
        heading_line_spacing_confidence=heading_line_spacing_confidence,
        body_line_spacing_confidence=body_line_spacing_confidence,
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
            if ref.ph_type in TITLE_PH_TYPES:
                title_votes[sz] += 1
            elif ref.ph_type in BODY_PH_TYPES:
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
            if ref.is_placeholder and ref.ph_type in TITLE_PH_TYPES:
                category = "title"
            elif ref.is_placeholder and ref.ph_type in BODY_PH_TYPES:
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
    нет (в прореженной шкале body и h1 — соседи), синтезируем среднее
    арифметическое, округлённое до 0.1pt (округление до общего шага 0.5pt —
    финальным проходом в build_type_scale, см. _SIZE_ROUNDING_STEP)."""
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
    steps: dict[str, float], *, support_by_center: dict[float, int], total_candidate_weight: int,
    title_votes: Counter[float], body_votes: Counter[float], all_run_chars: Counter[float],
) -> dict[str, float]:
    """Уверенность по каждой ступени — общее требование задачи.

    display/h2/caption/micro (когда наблюдались): доля веса выигравшего
    кластера от общего веса пула кандидатов — та же величина, что и weight-
    based confidence в Grid, а не блок "1.0, потому что пул был непуст"
    (это давало confidence=1.0 декоративному выбросу — см. докстроку
    _MIN_RUN_HISTOGRAM_SUPPORT). h1/body: доля голосов выигравшего значения
    от суммы голосов его категории. Синтезированные (не наблюдавшиеся)
    h2/caption/micro — фиксированные 0.3 (не измерены, но и не выдуманы из
    воздуха — коэффициент типографского шага)."""
    def _vote_share(counter: Counter[float], value: float) -> float:
        total = sum(counter.values())
        return counter[value] / total if total else 0.0

    def _support_share(value: float) -> float:
        return support_by_center.get(value, 0) / total_candidate_weight if total_candidate_weight else 0.0

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

    return {
        "display": _support_share(steps["display"]),
        "h1": h1_conf,
        "h2": _support_share(steps["h2"]) if steps["h2"] in support_by_center else 0.3,
        "body": body_conf,
        "caption": _support_share(steps["caption"]) if steps["caption"] in support_by_center else 0.3,
        "micro": _support_share(steps["micro"]) if steps["micro"] in support_by_center else 0.3,
    }


def _families_and_mono(
    fonts: dict[str, FontUsage],
) -> tuple[list[str], list[str], int, dict[str, list[str]]]:
    """Два самых весомых по символам не-моноширинных НОРМАЛИЗОВАННЫХ
    семейства, набравших не меньше _MIN_FAMILY_CHAR_SHARE суммарной доли
    символов → families; моноширинные (по имени, см. _MONO_FONT_MARKERS) —
    отдельно в mono, чтобы не раздувать families и не ломать аудит T01
    "гарнитур больше двух" (брифом, п.8).

    Нормализация (находка повторного код-ревью, п.5): raw-имена typeface из
    .pptx нередко несут начертание прямо в имени семейства ("Montserrat
    Medium", "Open Sans SemiBold") — без неё это раздувает счётчик гарнитур
    (на контрольном ЛЦТ2026 "Montserrat" и "Montserrat Medium" считались
    двумя разными гарнитурами, families_total=3 вместо честных 2). Символы
    нескольких raw-имён с одним нормализованным семейством суммируются
    ДО применения порога поддержки — иначе "Montserrat"+"Montserrat Medium"
    по отдельности могли бы не пройти порог, хотя суммарно являются
    доминирующей гарнитурой.

    Возвращает и общее число гарнитур, прошедших порог (families_total) —
    см. докстроку TypeScale.families_total — и family_variants: раскладку
    каждого прошедшего порог нормализованного семейства на фактические
    raw-имена, которыми оно было набрано (нужно вёрстке — см. докстроку
    TypeScale.family_variants)."""
    mono_raw = {name for name in fonts if _is_mono(name)}
    mono = sorted({_normalize_family(name) for name in mono_raw})

    non_mono = {name: fu for name, fu in fonts.items() if name not in mono_raw}
    grouped_chars: dict[str, int] = {}
    grouped_variants: dict[str, set[str]] = {}
    for raw_name, fu in non_mono.items():
        normalized = _normalize_family(raw_name)
        grouped_chars[normalized] = grouped_chars.get(normalized, 0) + fu.chars
        grouped_variants.setdefault(normalized, set()).add(raw_name)

    total_chars = sum(grouped_chars.values())
    qualifying = sorted(
        (name for name, chars in grouped_chars.items() if total_chars and chars / total_chars >= _MIN_FAMILY_CHAR_SHARE),
        key=lambda name: -grouped_chars[name],
    )
    family_variants = {name: sorted(grouped_variants[name]) for name in qualifying}
    return qualifying[:2], mono, len(qualifying), family_variants


def _is_mono(family: str) -> bool:
    lowered = family.lower()
    return any(marker in lowered for marker in _MONO_FONT_MARKERS)


# Хвосты начертания, отбрасываемые при нормализации имени гарнитуры (брифом,
# п.5 повторного код-ревью, список токенов — дословно из его текста):
# "Thin, ExtraLight, Light, Regular, Medium, SemiBold, Demi, Bold,
# ExtraBold, Black, Heavy, Italic, Oblique и их сочетания". Проверка — по
# ЦЕЛОМУ слову (токену), не по подстроке: иначе "Blackletter" или "Lighthouse"
# ложно потеряли бы часть имени.
_STYLE_SUFFIX_TOKENS = frozenset({
    "thin", "extralight", "light", "regular", "medium", "semibold", "demi",
    "bold", "extrabold", "black", "heavy", "italic", "oblique",
})


def _normalize_family(name: str) -> str:
    """Отбрасывает хвост начертания из имени гарнитуры: "Montserrat Medium"
    -> "Montserrat", "Open Sans Extra Bold Italic" -> "Open Sans" (сочетания
    начертаний — несколько токенов подряд, отбрасываются по одному с конца).
    Полное raw-имя не теряется — оно остаётся в TypeScale.family_variants
    (см. его докстроку), нормализуется только имя, используемое для счёта
    "сколько гарнитур в шаблоне".

    Никогда не отбрасывает ПОСЛЕДНИЙ оставшийся токен — гарнитура без
    единого "содержательного" слова в имени (маловероятный, но не нулевой
    случай) не должна схлопнуться в пустую строку."""
    tokens = name.split()
    while len(tokens) > 1 and tokens[-1].lower() in _STYLE_SUFFIX_TOKENS:
        tokens.pop()
    return " ".join(tokens)


def _line_spacing(pkg: PptxPackage, canvas: Canvas) -> tuple[float, float, float, float]:
    """heading — мода интерлиньяжа title-плейсхолдеров; body — мода среди
    ВСЕГО ОСТАЛЬНОГО (не только body-плейсхолдеров: у VK Tech основной текст
    в значительной части сидит в нередактируемых свободных фигурах-карточках,
    не в формальных плейсхолдерах — ограничение title-плейсхолдеров дало бы
    те же 90%, что и title, и завалило бы правило "заголовок теснее текста",
    см. разведку в scratchpad).

    На VK Tech это даёт body_line_spacing≈1.33 (133%), заметно выше
    заявленных брифом «100-110%» — потому что "всё остальное" здесь шире,
    чем "основной текст": попадают и карточки с очень свободным интерлиньяжем
    (крупные декоративные цифры статистики), не только абзацы связного
    текста. Числу это не мешает быть корректным ответом на вопрос "какой
    интерлиньяж типичен для НЕ-заголовочного текста этого шаблона" — просто
    диапазон брифа откалиброван по другим двум шаблонам.

    Возвращает (heading, heading_confidence, body, body_confidence) — находка
    повторного код-ревью, п.4: раньше эта функция отдавала только значения,
    без меры уверенности, и откат на _DEFAULT_HEADING_LINE_SPACING/
    _DEFAULT_BODY_LINE_SPACING при отсутствии a:lnSpc был неотличим от
    честного измерения (на контрольном ЛЦТ2026 title-плейсхолдеры вообще не
    задают a:lnSpc — heading был бы тихой константой 0.9 без единого
    признака, что это не измерение). Confidence — доля голосов моды среди
    измеренных значений (тот же приём, что _step_confidence для h1/body),
    либо _UNMEASURED_LINE_SPACING_CONFIDENCE, если измерений не было вовсе."""
    title_sp, nontitle_sp = _paragraph_spacing(pkg, canvas)
    if title_sp:
        heading_pct = _mode(title_sp)
        heading = heading_pct / 100
        heading_confidence = title_sp[heading_pct] / sum(title_sp.values())
    else:
        heading = _DEFAULT_HEADING_LINE_SPACING
        heading_confidence = _UNMEASURED_LINE_SPACING_CONFIDENCE
    if nontitle_sp:
        body_pct = _mode(nontitle_sp)
        body = body_pct / 100
        body_confidence = nontitle_sp[body_pct] / sum(nontitle_sp.values())
    else:
        body = _DEFAULT_BODY_LINE_SPACING
        body_confidence = _UNMEASURED_LINE_SPACING_CONFIDENCE
    return heading, heading_confidence, body, body_confidence


def _paragraph_spacing(pkg: PptxPackage, canvas: Canvas) -> tuple[Counter[float], Counter[float]]:
    title_sp: Counter[float] = Counter()
    nontitle_sp: Counter[float] = Counter()
    for part in _slide_parts(pkg) + _layout_parts(pkg):
        root = pkg.xml(part)
        for ref in walk_shapes(root, canvas, include_groups=False):
            if ref.kind != "shape":
                continue
            is_title = ref.is_placeholder and ref.ph_type in TITLE_PH_TYPES
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
