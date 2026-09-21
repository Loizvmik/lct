"""Тема оформления мастера и детект того, что она — заглушка Google-экспорта.

Все три реальных шаблона (VK Tech, WorkSpace, Education) — экспорт из Google
Slides. У Google Slides `a:fontScheme` в теме всегда `name="Office"` с
major/minor = Arial, а `p:txStyles` в мастере — одна и та же тройка
(sz/latin/clr) на всех девяти уровнях titleStyle и bodyStyle. Настоящая
типографика бренда сидит не там, а в `a:latin` внутри run'ов на слайдах —
это уже работа usage.py, не этого модуля.

Тема резолвится строго через relationships мастера, не по имени файла: у
шаблонов есть «сиротские» темы (`theme1.xml` у WorkSpace, `theme3.xml` у
VK Tech, `theme2.xml` у Education) — стоковая «Тема Office», ни к одному
мастеру не привязанная. `pick_primary_master` идёт от presentation.xml к
мастеру и его теме через rels, а не гадает по порядку файлов в архиве.
"""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from deckforge.ooxml.color import Color, UnresolvedColor, resolve_color
from deckforge.ooxml.ns import local_name, qn
from deckforge.ooxml.package import PptxPackage

# Шесть акцентных цветов стоковой темы Office 2013+ («Стандартная»/«Тема
# Office») — ровно то, что видно во всех «сиротских» темах трёх шаблонов.
# Сверяем только акценты: dk1/lt1 почти у любой темы чёрный/белый и ничего
# не отличают, а вот совпадение сразу всех шести акцентов с этим набором —
# случайность практически исключена.
_STOCK_OFFICE_ACCENTS = {
    "accent1": "#4472C4", "accent2": "#ED7D31", "accent3": "#A5A5A5",
    "accent4": "#FFC000", "accent5": "#5B9BD5", "accent6": "#70AD47",
}

# Порог «заметно» для третьего условия деградации (см. refine_font_scheme_
# degraded): шрифт темы должен набрать БОЛЬШИНСТВО реального текста (>50%
# символов с явно резолвящимся шрифтом), чтобы считаться настоящим, а не
# заглушкой. Не более мягкий порог: у VK Education (Task 3 код-ревью, п.4)
# Arial встречается в четверти-трети текста — заметно, но не доминирует —
# и деградация обязана остаться True, потому что реально доминирует Play.
# Только "шрифт темы — большинство" однозначно отличает случай "тема и
# текст согласны" (синтетика: 100% Arial → не деградация) от случая
# "шрифт темы просто где-то мелькает, но не описывает шаблон".
_THEME_FONT_MAJORITY_SHARE = 0.5

_CLR_SCHEME_SLOTS = (
    "dk1", "lt1", "dk2", "lt2",
    "accent1", "accent2", "accent3", "accent4", "accent5", "accent6",
    "hlink", "folHlink",
)


@dataclass(frozen=True)
class ThemeInfo:
    scheme: dict[str, str]
    clr_map: dict[str, str]
    major_font: str
    minor_font: str
    scheme_name: str
    # Как отдаёт read_theme() — только структурный сигнал: fontScheme/@name
    # == "Office" И majorFont == minorFont. Этого достаточно, чтобы решить,
    # что тема ПОХОЖА на заглушку Google-экспорта, но недостаточно для
    # уверенности: Arial/Calibri в корпоративном шаблоне бывают осознанным
    # выбором дизайнера. Третье, решающее условие — "шрифт темы не
    # встречается в фактическом тексте заметно" — требует текста слайдов,
    # которого read_theme не видит (это работа usage.py). Поэтому это поле
    # здесь — предварительное значение; окончательное получается вызовом
    # refine_font_scheme_degraded(theme, font_chars) после collect_usage
    # (см. Usage.primary_theme в usage.py, где это уже сделано автоматически).
    # major_font/minor_font при этом резолвятся из темы всегда, независимо
    # от значения этого флага — флаг ничего не обнуляет, только объясняет.
    font_scheme_degraded: bool
    text_styles_degraded: bool
    is_stock_office_palette: bool
    # Task 3 повторное код-ревью, п.3: доля символов, набранных шрифтом
    # темы (theme.major_font), от всего текста с явно резолвящимся шрифтом
    # — та же величина, которую refine_font_scheme_degraded сравнивает с
    # _THEME_FONT_MAJORITY_SHARE, но выведенная наружу рядом с итоговым
    # флагом. Порог 0.5 откалиброван по одному наблюдению (см. докстроку
    # _THEME_FONT_MAJORITY_SHARE) — без самой доли пограничный случай
    # (шрифт темы набрал 45% при доминировании другого) неотличим снаружи
    # от уверенного (шрифт темы набрал 5%): оба дают одно и то же
    # font_scheme_degraded=True. None — refine_font_scheme_degraded не
    # вызывался, либо структурного сигнала не было изначально (нечего
    # уточнять), либо текста не было вовсе (сравнивать не с чем) — см. её
    # докстроку.
    theme_font_share: float | None = None
    # Не входит в перечень полей брифа — добавлено, потому что общее
    # требование задачи прямое: нераспознанные цвета не выбрасывать молча,
    # а собирать отдельным списком на просмотр человеку (см. докстроку
    # UnresolvedColor в ooxml/color.py). Для clrScheme темы это скорее
    # подстраховка: на трёх реальных шаблонах здесь всегда чистый srgbClr.
    unresolved: list[UnresolvedColor] = field(default_factory=list)


EMPTY_THEME = ThemeInfo(
    scheme={}, clr_map={}, major_font="", minor_font="", scheme_name="",
    font_scheme_degraded=False, text_styles_degraded=False, is_stock_office_palette=False,
)
"""Нейтральный фолбэк для частей пакета, у которых нет резолвимой темы (см.
usage.py::_ThemeGraph) — не «правильный ответ» ни для какого настоящего
.pptx (мастер там обязателен по OOXML), а честный нейтральный элемент:
schemeClr не резолвится (уйдёт в unresolved), токены шрифта не резолвятся
ни во что осмысленное, флаги деградации — False (нечего деградировать)."""


def _is_font_scheme_stub_shaped(font_scheme_name: str, major_font: str, minor_font: str) -> bool:
    """Структурный сигнал «похоже на заглушку Google-экспорта»: имя схемы
    шрифтов — дефолтное "Office", и majorFont совпадает с minorFont (Google
    всегда пишет туда Arial/Arial). Сам по себе не значит деградацию —
    финальное решение см. refine_font_scheme_degraded."""
    return font_scheme_name == "Office" and bool(major_font) and major_font == minor_font


def refine_font_scheme_degraded(theme: ThemeInfo, font_chars: Mapping[str, int]) -> ThemeInfo:
    """Уточняет ThemeInfo.font_scheme_degraded по фактическому употреблению шрифта.

    read_theme даёт только структурный сигнал (fontScheme/@name == "Office" и
    majorFont == minorFont) — этого достаточно, чтобы заподозрить заглушку, но
    недостаточно для уверенности: в корпоративном шаблоне Arial/Calibri тоже
    бывают осознанным выбором дизайнера. Третье условие требует фактического
    текста слайдов, которого read_theme не видит, — поэтому вызывается
    отдельно, уже после collect_usage (в usage.py это делается автоматически
    для Usage.primary_theme).

    `font_chars` — {имя_шрифта: число_символов}, как в Usage.fonts (после
    разрешения токенов +mj-lt/+mn-lt — см. usage.py, они резолвятся в имя
    шрифта темы всегда, вне зависимости от текущего значения этого флага).
    Если шрифт темы набирает больше `_THEME_FONT_MAJORITY_SHARE` (см. её
    докстроку) всего текста с явно резолвящимся шрифтом — это его реальное
    большинство, не заглушка, и флаг снимается, даже если структурный сигнал
    был True. Нет структурного сигнала — снимать нечего, флаг остаётся как
    есть (False). Текста нет вовсе — сравнивать не с чем, доверяем
    структурному сигналу без изменений.

    Task 3 повторное код-ревью, п.3: рядом с вердиктом (флагом) наружу
    выводится и сама доля — `ThemeInfo.theme_font_share` — чтобы человек,
    читающий отчёт о разборе, видел не только итог, но и насколько он
    близок к порогу `_THEME_FONT_MAJORITY_SHARE` (0.45 при доминировании
    другого шрифта выглядит так же уверенно, как 0.05, если смотреть только
    на булев флаг). Заполняется в обеих ветках (флаг снят или остался),
    как только доля вообще была посчитана; остаётся `None`, если считать
    было не с чем (см. случаи выше).
    """
    if not theme.font_scheme_degraded:
        return theme
    total = sum(font_chars.values())
    if total == 0:
        return theme
    theme_font_chars = font_chars.get(theme.major_font, 0)
    share = theme_font_chars / total
    if share > _THEME_FONT_MAJORITY_SHARE:
        return replace(theme, font_scheme_degraded=False, theme_font_share=share)
    return replace(theme, theme_font_share=share)


def is_stock_office(scheme: dict[str, str]) -> bool:
    """True, если акцентные цвета совпадают со стоковой темой Office 2013+.

    Принимает произвольный dict с хотя бы ключами accent1..accent6 (полный
    ThemeInfo.scheme тоже подходит) — сравнение идёт только по ним.
    """
    return all(
        scheme.get(slot, "").upper() == expected
        for slot, expected in _STOCK_OFFICE_ACCENTS.items()
    )


def pick_primary_master(pkg: PptxPackage) -> str:
    """Часть мастера с брендовой темой — не первая попавшаяся.

    Отбрасывает мастеров, чья тема прошла is_stock_office (стоковая тема,
    привязанная напрямую к мастеру — такое в разведанных файлах не
    встретилось, но пренебрегать этим нельзя: разведка это не гарантирует
    для незнакомого шаблона с защиты). Среди оставшихся выбирает мастер с
    наибольшим числом лейаутов; при равенстве — тот, на который ссылается
    больше слайдов.

    Известный, сознательно принятый компромисс (Task 3 повторное код-ревью,
    находка adversarial-reviewer): мастер, чья тема физически не читается
    (нет relationship theme, битый Target, невалидный XML), для
    `_is_master_stock` — не "стоковый" (см. её докстроку), значит попадает
    в пул кандидатов наравне с читаемыми и может выиграть тай-брейк по
    числу лейаутов, даже если рядом есть полностью годный мастер с меньшим
    их числом. Если такой мастер станет первичным, `_ThemeGraph` в usage.py
    упадёт на прямом `read_theme()` с внятным сообщением — это ожидаемое,
    заданное самим контрактом Task 3 поведение ("первичный мастер — особый
    случай: если не читается он, падать правильно"), не тихая порча.
    Чинить выбор первичного так, чтобы он всегда обходил нечитаемые темы в
    пользу читаемых с меньшим числом лейаутов, в рамках этой починки не
    стали — это отдельное усиление алгоритма ранжирования, не тот класс
    бага ("посторонний мастер роняет весь разбор"), который здесь чинили;
    посторонний (непервичный) мастер с битой темой по-прежнему не роняет
    разбор (см. _ThemeGraph.__init__ в usage.py).
    """
    presentation_part = pkg.presentation_part()
    masters = pkg.related(presentation_part, "slideMaster")
    if not masters:
        raise ValueError(f"{presentation_part}: в презентации нет ни одного мастера слайдов")

    scored = [
        (master, _is_master_stock(pkg, master), len(pkg.related(master, "slideLayout")))
        for master in masters
    ]
    non_stock = [row for row in scored if not row[1]]
    pool = non_stock if non_stock else scored

    max_layouts = max(row[2] for row in pool)
    tied = [row[0] for row in pool if row[2] == max_layouts]
    if len(tied) == 1:
        return tied[0]

    slide_counts = {master: _slides_under_master(pkg, presentation_part, master) for master in tied}
    return max(tied, key=lambda master: slide_counts[master])


def read_theme(pkg: PptxPackage, master_part: str) -> ThemeInfo:
    """Тема мастера `master_part` плюс детект заглушки Google-экспорта."""
    master_root = pkg.xml(master_part)
    theme_part = _master_theme_part(pkg, master_part)
    scheme, scheme_name, major_font, minor_font, font_scheme_name, unresolved = (
        _read_theme_elements(pkg, theme_part)
    )

    clr_map_el = master_root.find(qn("p:clrMap"))
    clr_map = dict(clr_map_el.attrib) if clr_map_el is not None else {}

    tx_styles = master_root.find(qn("p:txStyles"))
    title_style = tx_styles.find(qn("p:titleStyle")) if tx_styles is not None else None
    body_style = tx_styles.find(qn("p:bodyStyle")) if tx_styles is not None else None
    text_styles_degraded = _is_style_degenerate(title_style) and _is_style_degenerate(body_style)

    font_scheme_degraded = _is_font_scheme_stub_shaped(font_scheme_name, major_font, minor_font)

    return ThemeInfo(
        scheme=scheme,
        clr_map=clr_map,
        major_font=major_font,
        minor_font=minor_font,
        scheme_name=scheme_name,
        font_scheme_degraded=font_scheme_degraded,
        text_styles_degraded=text_styles_degraded,
        is_stock_office_palette=is_stock_office(scheme),
        unresolved=unresolved,
    )


def _master_theme_part(pkg: PptxPackage, master_part: str) -> str:
    themes = pkg.related(master_part, "theme")
    if not themes:
        raise ValueError(f"{master_part}: не найдена связанная тема (relationship theme)")
    return themes[0]


def _is_master_stock(pkg: PptxPackage, master_part: str) -> bool:
    """True, если тема мастера — стоковая палитра Office.

    Task 3 повторное код-ревью, п.2: тема, которая не читается вовсе (нет
    relationship theme, часть без a:themeElements), — не стоковая по
    определению, не "неизвестно" и не повод падать: тот же принцип
    асимметричной безопасности, что уже описан у is_stock_office
    (неполные/нерезолвленные данные всегда дают False, никогда не мешают
    отбору). Без этого один посторонний мастер с битой связью темы ронял
    бы pick_primary_master целиком, хотя для отбора первичного мастера эта
    проверка вообще не обязана уметь читать его тему безупречно —
    непрочитавшаяся тема ПЕРВИЧНОГО мастера, если он всё же будет выбран,
    проявится позже и честно, при прямом read_theme() в _ThemeGraph.

    Ловим `Exception`, не только `ValueError` (Task 3 повторное код-ревью,
    находка adversarial-reviewer поверх п.2): relationship theme может
    физически существовать, но вести в никуда — Target указывает на файл,
    которого нет в архиве (`PptxPackage.xml`/`.part` бросает `KeyError` при
    чтении из zip), либо файл есть, но не well-formed XML (`lxml` бросает
    `lxml.etree.XMLSyntaxError`, не `ValueError`). Для непрочитанного
    постороннего мастера, который сюда попал именно потому что его тема
    как-то не в порядке, перечислять заранее все конкретные виды порчи
    архива — заведомо неполный список; здесь сознательно ловится широко,
    ровно как задумано для этой конкретной, изолированной оценки одного
    мастера (не для чтения ПЕРВИЧНОГО мастера — там любая ошибка обязана
    дойти до вызывающего и уронить разбор честно, см. read_theme в
    _ThemeGraph)."""
    try:
        theme_part = _master_theme_part(pkg, master_part)
        scheme, *_ = _read_theme_elements(pkg, theme_part)
    except Exception:
        return False
    return is_stock_office(scheme)


def _slides_under_master(pkg: PptxPackage, presentation_part: str, master_part: str) -> int:
    """Число слайдов, чей лейаут в итоге ссылается на данный мастер.

    Нужен только для разрешения ничьей по числу лейаутов — в разведанных
    файлах ничьей нет ни разу, но правило в брифе есть, и молчаливо его
    не реализовать значило бы гадать на первом попавшемся мастере вместо
    честного счёта.
    """
    count = 0
    for slide in pkg.related(presentation_part, "slide"):
        layouts = pkg.related(slide, "slideLayout")
        if not layouts:
            continue
        masters = pkg.related(layouts[0], "slideMaster")
        if masters and masters[0] == master_part:
            count += 1
    return count


def _read_theme_elements(
    pkg: PptxPackage, theme_part: str,
) -> tuple[dict[str, str], str, str, str, str, list[UnresolvedColor]]:
    """(scheme, scheme_name, major_font, minor_font, font_scheme_name, unresolved)."""
    theme_root = pkg.xml(theme_part)
    theme_elements = theme_root.find(qn("a:themeElements"))
    if theme_elements is None:
        raise ValueError(f"{theme_part}: нет a:themeElements — это не тема DrawingML")

    clr_scheme = theme_elements.find(qn("a:clrScheme"))
    scheme: dict[str, str] = {}
    unresolved: list[UnresolvedColor] = []
    scheme_name = clr_scheme.get("name", "") if clr_scheme is not None else ""
    if clr_scheme is not None:
        for slot in _CLR_SCHEME_SLOTS:
            slot_el = clr_scheme.find(qn(f"a:{slot}"))
            if slot_el is None:
                continue
            color_el = next(iter(slot_el), None)
            resolved = resolve_color(color_el, {}, {}) if color_el is not None else None
            if isinstance(resolved, Color):
                scheme[slot] = resolved.hex
            elif isinstance(resolved, UnresolvedColor):
                unresolved.append(resolved)

    font_scheme = theme_elements.find(qn("a:fontScheme"))
    font_scheme_name = font_scheme.get("name", "") if font_scheme is not None else ""
    major_font = _latin_typeface(font_scheme, "majorFont") if font_scheme is not None else ""
    minor_font = _latin_typeface(font_scheme, "minorFont") if font_scheme is not None else ""

    return scheme, scheme_name, major_font, minor_font, font_scheme_name, unresolved


def _latin_typeface(font_scheme, group_tag: str) -> str:
    group = font_scheme.find(qn(f"a:{group_tag}"))
    latin = group.find(qn("a:latin")) if group is not None else None
    return latin.get("typeface", "") if latin is not None else ""


def _style_level_triples(style_group) -> list[tuple[str | None, str | None, tuple | None] | None]:
    """(sz, latin, (fill_tag, fill_val)) для a:lvl1pPr..a:lvl9pPr, либо None,
    если уровня или a:defRPr в нём нет вовсе (в отличие от a:defPPr — это не
    уровень, а групповые дефолты абзаца, свой triple у него не считается)."""
    levels: list[tuple | None] = []
    for i in range(1, 10):
        lvl = style_group.find(qn(f"a:lvl{i}pPr"))
        def_rpr = lvl.find(qn("a:defRPr")) if lvl is not None else None
        if def_rpr is None:
            levels.append(None)
            continue
        sz = def_rpr.get("sz")
        latin_el = def_rpr.find(qn("a:latin"))
        latin = latin_el.get("typeface") if latin_el is not None else None
        fill = def_rpr.find(qn("a:solidFill"))
        color_repr = None
        if fill is not None and len(fill):
            color_el = fill[0]
            color_repr = (local_name(color_el), color_el.get("val"))
        levels.append((sz, latin, color_repr))
    return levels


def _is_style_degenerate(style_group) -> bool:
    """Все девять уровней присутствуют и дают одинаковую тройку (sz, latin, clr)."""
    if style_group is None:
        return False
    levels = _style_level_triples(style_group)
    if any(level is None for level in levels):
        return False
    return len(set(levels)) == 1
