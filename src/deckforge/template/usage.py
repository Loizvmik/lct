"""Фактическая палитра и шрифты шаблона — по слайдам, лейаутам и мастерам.

Тема (theme.py) даёт номинальную палитру и шрифты бренда, но на реальных
шаблонах Google-экспорта этого мало: соотношение прямых srgbClr к ссылкам
schemeClr на слайдах — от 1074:509 (Education) до 268:570 (WorkSpace), то
есть у части шаблонов прямых цветов на слайдах больше, чем ссылок на тему.
Палитра, собранная только из a:clrScheme, потеряет половину реального
визуального языка. Этот модуль обходит всё дерево шейпов и текста и
считает, что реально нарисовано, а не что заявлено в теме.

Тема резолвится ОТДЕЛЬНО для каждой части пакета, по графу связей (см.
_ThemeGraph и докстроку collect_usage) — не одна тема на весь пакет. У VK
Tech и VK Education по два мастера; если у второстепенного мастера есть
свои макеты или слайды, их schemeClr обязаны резолвиться через его
собственную тему, а не через тему первичного мастера — иначе цвет молча
получается неверным без единой записи в unresolved (это не тот случай,
который unresolved вообще может поймать: цвет резолвится успешно, просто
не в тот hex).

Шрифт бренда виден только в a:latin внутри run'ов (fontScheme в теме —
тоже заглушка Google, см. theme.py) — поэтому шрифт каждого run'а читается
напрямую из a:rPr/a:latin, без обращения к lstStyle/txStyles: и то, и
другое либо заглушка, либо требует наследования по уровню/типу плейсхолдера,
которое в масштабе всей задачи не окупается — сигнала без него достаточно.
Токены темы (+mj-lt/+mn-lt) резолвятся в major_font/minor_font темы ВСЕГДА,
вне зависимости от ThemeInfo.font_scheme_degraded — это прямое указание
формата ("шрифт заголовков/текста из темы"), а не эвристика, обнулять его
из-за подозрения на заглушку значит терять реальные данные о шрифте.

То же самое ограничение — не только для шрифта: размер (sz), цвет заливки
текста (solidFill), bold/italic читаются исключительно из a:rPr самого
run'а. Run без хотя бы одного из этих свойств (a:rPr нет вовсе, либо он
пуст содержательно — например, только lang/dirty от системы правки) не
попадает в счётчики sz/шрифта/цвета/bold/italic — наследование по цепочке
run → абзац → lstStyle шейпа → плейсхолдер макета → txStyles мастера → тема
в этой задаче не реализуется, это работа следующих задач. Но количество
символов такого run'а не теряется: оно всегда попадает либо в
Usage.explicit_chars, либо в Usage.inherited_chars (см. _has_explicit_props)
— так видно, какая доля текста разобрана явно, а какая осталась за
скобками, вместо того чтобы делать вид, что непокрытого текста не было
вовсе. На трёх реальных шаблонах доля inherited_chars — 8.5% (Education) —
16.4% (VK Tech), ненулевая на каждом; для нативного шаблона с защиты она
может быть выше (там чаще встречается run без a:rPr буквально, чего в этих
трёх Google-экспортах не нашлось ни разу — там rPr технически присутствует,
но иногда пуст).

Известное ограничение: полноценный разбор таблиц (a:tbl) не реализован —
геометрия колонок/строк, объединённые ячейки (gridSpan/rowSpan/hMerge/
vMerge), стили таблицы (a:tblPr/a:tableStyleId → styles.xml) не читаются.
Но текст и заливка каждой ячейки (a:tc) собираются в те же счётчики, что и
у обычных шейпов (см. _collect_table) — иначе на table-heavy шаблоне
(Education — 3 таблицы) статистика молча теряет часть текста и палитры.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass, field

from deckforge.ooxml.color import Color, UnresolvedColor, resolve_color
from deckforge.ooxml.geometry import Canvas
from deckforge.ooxml.ns import NS, local_name, qn
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.walk import sp_tree_of
from deckforge.template.theme import (
    EMPTY_THEME, ThemeInfo, pick_primary_master, read_theme, refine_font_scheme_degraded,
)

# Единственный активный дочерний элемент заливки по схеме OOXML (CT_FillProperties
# — xs:choice, одновременно их быть не может).
_FILL_TAGS = {"noFill", "solidFill", "gradFill", "grpFill", "pattFill", "blipFill"}

# Токены темы вместо явного имени шрифта — резолвятся в major_font/minor_font
# темы всегда (см. докстроку модуля и _resolve_font_family).
_MAJOR_FONT_TOKEN = "+mj-lt"
_MINOR_FONT_TOKEN = "+mn-lt"

# Пространства имён, которые этот модуль реально понимает содержательно: мы
# читаем геометрию, цвет и текст — то есть базовый DrawingML (a) и
# PresentationML (p) из ECMA-376 Part 1. Расширения вроде p14/p15
# (PowerPoint 2010/2013), a14 (DrawingML 2010) мы нигде не разбираем — ни
# один обходчик модуля не ищет их теги/атрибуты, значит выбор такой ветки
# mc:Choice дал бы тихо неполные данные вместо честного ухода в mc:Fallback
# (см. _pick_alternate_content_branch).
_UNDERSTOOD_MC_NAMESPACES = {NS["a"], NS["p"]}


@dataclass(frozen=True)
class _NoBackground:
    """Маркер-значение: ни макет, ни его мастер не задают p:bg — фон
    презентации не определён, а не просто пропущен при обходе. Отдельный
    тип, не Color, чтобы код светлый/тёмный лейаут + аудит контраста не
    спутал "фон неизвестен" с каким-то конкретным резолвленным цветом."""


NO_BACKGROUND = _NoBackground()


@dataclass
class FontUsage:
    family: str
    chars: int = 0
    runs: int = 0
    embedded: bool = False
    bold_available: bool = False
    italic_available: bool = False


@dataclass
class Usage:
    fill: Counter = field(default_factory=Counter)
    text: Counter = field(default_factory=Counter)
    line: Counter = field(default_factory=Counter)
    layout_bg: Counter = field(default_factory=Counter)
    fonts: dict[str, FontUsage] = field(default_factory=dict)
    sizes_pt: Counter = field(default_factory=Counter)
    line_spacing: Counter = field(default_factory=Counter)
    align: Counter = field(default_factory=Counter)
    bold_runs: int = 0
    italic_runs: int = 0
    total_runs: int = 0
    # Не входят в перечень полей брифа — добавлены по прямым требованиям
    # текста Step 4, а не только кода: "noFill учитывается отдельным
    # счётчиком" и общее требование задачи не терять нераспознанные цвета
    # молча (см. докстроку UnresolvedColor в ooxml/color.py).
    no_fill_shapes: int = 0
    unresolved: list[UnresolvedColor] = field(default_factory=list)
    # Task 3 код-ревью (п.5): число символов run'ов, у которых свойства
    # (sz/latin/fill/b/i) были явно заданы на самом run'е, и число символов
    # тех, у кого их нет вовсе (наследуются от контекста, который этот
    # модуль не резолвит) — см. докстроку модуля.
    explicit_chars: int = 0
    inherited_chars: int = 0
    # Task 3 код-ревью (п.2): тема первичного мастера — раньше единственный
    # параметр collect_usage(theme=...), теперь вычисляется внутри и
    # уточняется по фактическому употреблению шрифта (см.
    # theme.refine_font_scheme_degraded). Тема КАЖДОЙ ОТДЕЛЬНОЙ части
    # пакета резолвится своя (см. _ThemeGraph) — это поле про "одну" тему
    # шаблона в целом, для потребителей, которым нужен единый ответ
    # (например, отчёт о деградации).
    primary_theme: ThemeInfo | None = None


def collect_usage(pkg: PptxPackage, canvas: Canvas) -> Usage:
    """Обходит все slideLayout*.xml, slideMaster*.xml и slide*.xml пакета.

    Контракт (изменён по итогам код-ревью Task 3, находка «одна тема на все
    макеты», п.2 — решение автора брифа, отменяющее исходный контракт с
    параметром `theme`): тема резолвится ОТДЕЛЬНО для каждой части, по
    графу связей, а не передаётся одна на весь пакет. У VK Tech и VK
    Education по два мастера — раньше schemeClr любого макета резолвился
    через тему ПЕРВИЧНОГО мастера, и макеты/слайды второстепенного мастера
    молча получали чужую палитру.

    Граф резолва: p:sld → его p:sldLayout (relationship slideLayout) →
    его p:sldMaster (relationship slideMaster) → тема мастера (relationship
    theme); p:sldLayout сам по себе — так же, через свой мастер; сам
    p:sldMaster — по своей теме напрямую. Часть, для которой связь не
    резолвится (нет мастера/лейаута в rels — сирота или синтетический
    фрагмент пакета в тестах), получает тему первичного мастера как честный
    фолбэк, а не падает; пакет вовсе без единого мастера получает
    `theme.EMPTY_THEME` (см. её докстроку).

    Тема первичного мастера (раньше — единственный аргумент `theme`)
    доступна отдельно как `Usage.primary_theme` — уточнённая по фактическому
    тексту: `font_scheme_degraded` в ней учитывает не только структуру
    fontScheme, но и то, какой шрифт реально набирает большинство
    собранного текста (см. `theme.refine_font_scheme_degraded`).
    """
    usage = Usage()
    embedded_fonts = _read_embedded_fonts(pkg)
    theme_graph = _ThemeGraph(pkg)

    for part_name in _all_shape_bearing_parts(pkg):
        root = pkg.xml(part_name)
        theme = theme_graph.theme_for(part_name)
        if part_name.startswith("ppt/slideLayouts/"):
            _collect_layout_bg(theme_graph, part_name, root, theme, usage)
        _collect_shapes(root, canvas, theme, usage, embedded_fonts)

    font_chars = {family: fu.chars for family, fu in usage.fonts.items()}
    usage.primary_theme = refine_font_scheme_degraded(theme_graph.primary_theme, font_chars)
    return usage


class _ThemeGraph:
    """Тема для каждой части пакета — по связям, не по единственному
    "первичному" мастеру (см. докстроку collect_usage)."""

    def __init__(self, pkg: PptxPackage) -> None:
        self._pkg = pkg
        presentation_part = pkg.presentation_part()
        self._master_theme: dict[str, ThemeInfo] = {
            master: read_theme(pkg, master)
            for master in pkg.related(presentation_part, "slideMaster")
        }
        try:
            primary_master = pick_primary_master(pkg)
            self.primary_theme = self._master_theme.get(primary_master) or read_theme(pkg, primary_master)
        except ValueError:
            # Пакет вовсе без мастера — невозможно для настоящего .pptx (по
            # OOXML мастер обязателен), но синтетическим фрагментам пакета в
            # тестах (обход mc:AlternateContent/blipFill и т.п.) мастер не
            # нужен вовсе — не должны падать на резолве темы, которая им
            # безразлична.
            self.primary_theme = EMPTY_THEME
        self._layout_master_cache: dict[str, str | None] = {}

    def theme_for(self, part_name: str) -> ThemeInfo:
        if part_name in self._master_theme:
            return self._master_theme[part_name]
        if part_name.startswith("ppt/slideLayouts/"):
            return self._layout_theme(part_name)
        if part_name.startswith("ppt/slides/"):
            return self._slide_theme(part_name)
        return self.primary_theme

    def master_root_for_layout(self, layout_part: str):
        """XML-корень мастера лейаута — для наследования фона (см.
        _collect_layout_bg). None, если связь не резолвится."""
        master = self._master_of_layout(layout_part)
        return self._pkg.xml(master) if master is not None else None

    def _layout_theme(self, layout_part: str) -> ThemeInfo:
        master = self._master_of_layout(layout_part)
        if master is not None and master in self._master_theme:
            return self._master_theme[master]
        return self.primary_theme

    def _slide_theme(self, slide_part: str) -> ThemeInfo:
        layouts = self._pkg.related(slide_part, "slideLayout")
        if not layouts:
            return self.primary_theme
        return self._layout_theme(layouts[0])

    def _master_of_layout(self, layout_part: str) -> str | None:
        if layout_part not in self._layout_master_cache:
            related = self._pkg.related(layout_part, "slideMaster")
            self._layout_master_cache[layout_part] = related[0] if related else None
        return self._layout_master_cache[layout_part]


def _all_shape_bearing_parts(pkg: PptxPackage) -> list[str]:
    prefixes = ("ppt/slideLayouts/slideLayout", "ppt/slideMasters/slideMaster", "ppt/slides/slide")
    return sorted(
        name for name in pkg.names()
        if name.endswith(".xml") and any(name.startswith(p) for p in prefixes)
    )


def _collect_layout_bg(
    theme_graph: _ThemeGraph, layout_part: str, layout_root, theme: ThemeInfo, usage: Usage,
) -> None:
    """Фон лейаута: свой p:bg, а если его нет — фон мастера через связь
    slideLayout → slideMaster (Task 3 код-ревью, п.6). Ни там, ни там —
    NO_BACKGROUND, а не молчаливый пропуск лейаута (было — 5 из 39 лейаутов
    VK Tech терялись именно так, теряя сигнал для светлый/тёмный лейаут и
    аудита контраста «ниже 4.5:1»)."""
    bg = _bg_element(layout_root)
    if bg is None:
        bg = _bg_element(theme_graph.master_root_for_layout(layout_part))
    if bg is None:
        usage.layout_bg[NO_BACKGROUND] += 1
        return
    _record_layout_bg(bg, theme, usage)


def _bg_element(root):
    if root is None:
        return None
    c_sld = root.find(qn("p:cSld"))
    return c_sld.find(qn("p:bg")) if c_sld is not None else None


def _record_layout_bg(bg, theme: ThemeInfo, usage: Usage) -> None:
    bg_pr = bg.find(qn("p:bgPr"))
    if bg_pr is not None:
        fill_el = _pick_fill_element(bg_pr)
        if fill_el is None or local_name(fill_el) == "noFill":
            return
        for color in _fill_colors(fill_el, theme, usage.unresolved):
            usage.layout_bg[color] += 1
        return

    bg_ref = bg.find(qn("p:bgRef"))
    if bg_ref is not None:
        color_el = next(iter(bg_ref), None)
        resolved = resolve_color(color_el, theme.scheme, theme.clr_map) if color_el is not None else None
        if isinstance(resolved, Color):
            usage.layout_bg[resolved] += 1
        elif isinstance(resolved, UnresolvedColor):
            usage.unresolved.append(resolved)


def _collect_shapes(
    tree_root, canvas: Canvas, theme: ThemeInfo, usage: Usage, embedded_fonts: dict[str, tuple[bool, bool]],
) -> None:
    # Свой обход, не walk_shapes: тому нужна геометрия (её здесь не считаем),
    # а часть шейпов, важных для палитры/шрифтов (например, плейсхолдеры без
    # своего a:xfrm), в walk_shapes отдаётся с box=None — но это ровно те
    # шейпы, чьи цвет/текст/шрифт нам и нужны, отбрасывать их нельзя.
    sp_tree = sp_tree_of(tree_root)
    for element in _iter_shape_elements(sp_tree):
        tag = local_name(element)
        if tag == "sp":
            _collect_shape_fill(element, theme, usage)
            _collect_line(element, theme, usage)
            _collect_text_body(element, canvas, theme, usage, embedded_fonts)
        elif tag == "cxnSp":
            # Коннектор — линия: у него нет "заливки-карточки" в смысле
            # брифа ("невидимые контейнеры" — про p:sp), noFill здесь почти
            # всегда норма, а не сигнал карточной вёрстки. Обводка (a:ln) —
            # его единственное содержательное визуальное свойство.
            _collect_line(element, theme, usage)
        elif tag == "graphicFrame":
            # Диаграммы/OLE через graphicFrame не разбираем вовсе (не наша
            # задача), но таблицу (a:tbl) — частично, см. докстроку модуля.
            _collect_table(element, canvas, theme, usage, embedded_fonts)


def _iter_shape_elements(container):
    """Рекурсивно отдаёт p:sp/p:cxnSp/p:graphicFrame внутри дерева,
    разворачивая группы.

    mc:AlternateContent — не ещё один контейнер шейпов, а развилка:
    mc:Choice и mc:Fallback — взаимоисключающие альтернативы ОДНОГО и того
    же содержимого (ECMA-376 Part 3, Markup Compatibility), а не два разных
    шейпа. Слепой `.iter()` по локальным именам спускается в обе ветки и
    удваивает счётчики для любого шейпа, обёрнутого в такую развилку —
    паттерн, которым нативный PowerPoint пользуется регулярно (например,
    для скруглённых прямоугольников с одним настраиваемым радиусом угла),
    в отличие от Google-экспорта, где он не встретился ни разу. Берём ровно
    одну ветку — см. _pick_alternate_content_branch.

    p:pic (картинки) не даёт того, что здесь собираем, — пропускается без
    рекурсии (по схеме вложенных p:sp/p:cxnSp у него и быть не может).
    p:graphicFrame (таблицы/диаграммы/OLE) — тоже не контейнер шейпов по
    схеме, но отдаётся вызывающему (см. _collect_shapes): внутри может быть
    таблица, чьи ячейки мы частично разбираем отдельно (_collect_table).
    """
    for element in container:
        tag = local_name(element)
        if tag == "AlternateContent":
            branch = _pick_alternate_content_branch(element)
            if branch is not None:
                yield from _iter_shape_elements(branch)
            continue
        if tag in ("sp", "cxnSp", "graphicFrame"):
            yield element
        elif tag == "grpSp":
            yield from _iter_shape_elements(element)


def _pick_alternate_content_branch(alt_element):
    """Первый mc:Choice, чьё @Requires целиком резолвится в понятные модулю
    пространства имён (Markup Compatibility, ECMA-376 Part 3, §10.2): по
    спеку консьюмер обязан выбрать ПЕРВЫЙ Choice, который он понимает, и
    только если ни один не подошёл — уйти в mc:Fallback. Нативный
    PowerPoint штатно пишет несколько mc:Choice под разные версии
    (например, DrawingML 2010 (a14) с фолбэком на базовый DrawingML) — и
    "просто взять первый Choice, если он есть" (без чтения @Requires) эту
    развилку не соблюдает: молча возьмёт ветку в расширении, которое этот
    модуль не разбирает, вместо честного mc:Fallback с понятной разметкой.
    """
    for child in alt_element:
        if local_name(child) != "Choice":
            continue
        requires = child.get("Requires")
        if requires and _requires_understood(child, requires):
            return child
    return next((c for c in alt_element if local_name(c) == "Fallback"), None)


def _requires_understood(choice_element, requires: str) -> bool:
    """@Requires — список префиксов через пробел (ST_Requires), каждый из
    которых должен резолвиться (через nsmap, накопленный по предкам
    элемента) в пространство имён, которое модуль понимает содержательно —
    см. _UNDERSTOOD_MC_NAMESPACES. Требование без прописанного в документе
    префикса или ссылающееся хоть на одно непонятное пространство имён —
    Choice не подходит целиком (see spec: "AND" по всем токенам)."""
    nsmap = choice_element.nsmap
    prefixes = requires.split()
    if not prefixes:
        return False
    return all(nsmap.get(prefix) in _UNDERSTOOD_MC_NAMESPACES for prefix in prefixes)


def _pick_fill_element(container):
    for child in container:
        if local_name(child) in _FILL_TAGS:
            return child
    return None


def _fill_colors(fill_el, theme: ThemeInfo, unresolved: list[UnresolvedColor]) -> list[Color]:
    """Цвет(а) заливки. Для gradFill — цвета всех точек градиента: resolve_color
    сам градиенты не разбирает (см. его докстроку в ooxml/color.py), это
    сделано здесь, отдельно.

    blipFill (заливка изображением) — не «нераспознанный цвет», а легитимное
    отсутствие единого цвета, тот же случай по смыслу, что noFill/grpFill в
    самом resolve_color (там их для этого и заводили). Без явного отсечения
    здесь resolve_color не отличает blipFill от реально неизвестного тега и
    добавляет в unresolved шум "нераспознанный тег" на каждый шейп с
    картинкой-заливкой — подтверждено на контрольном шаблоне защиты."""
    tag = local_name(fill_el)
    if tag == "blipFill":
        return []
    if tag == "gradFill":
        colors: list[Color] = []
        gs_lst = fill_el.find(qn("a:gsLst"))
        if gs_lst is None:
            return colors
        for gs in gs_lst:
            color_el = next(iter(gs), None)
            if color_el is None:
                continue
            resolved = resolve_color(color_el, theme.scheme, theme.clr_map)
            if isinstance(resolved, Color):
                colors.append(resolved)
            elif isinstance(resolved, UnresolvedColor):
                unresolved.append(resolved)
        return colors

    resolved = resolve_color(fill_el, theme.scheme, theme.clr_map)
    if isinstance(resolved, Color):
        return [resolved]
    if isinstance(resolved, UnresolvedColor):
        unresolved.append(resolved)
    return []


def _collect_shape_fill(element, theme: ThemeInfo, usage: Usage) -> None:
    """Заливка и noFill-счётчик — только для p:sp. См. комментарий в
    _collect_shapes про то, почему коннекторы сюда не попадают."""
    sp_pr = element.find(qn("p:spPr"))
    if sp_pr is None:
        return

    fill_el = _pick_fill_element(sp_pr)
    if fill_el is None:
        return  # заливка не задана вовсе — не выдумываем ни цвет, ни noFill
    if local_name(fill_el) == "noFill":
        usage.no_fill_shapes += 1
        return
    for color in _fill_colors(fill_el, theme, usage.unresolved):
        usage.fill[color] += 1


def _collect_line(element, theme: ThemeInfo, usage: Usage) -> None:
    """Обводка — общая для p:sp и p:cxnSp."""
    sp_pr = element.find(qn("p:spPr"))
    if sp_pr is None:
        return
    ln = sp_pr.find(qn("a:ln"))
    if ln is None:
        return
    ln_fill_el = _pick_fill_element(ln)
    if ln_fill_el is None or local_name(ln_fill_el) == "noFill":
        return
    for color in _fill_colors(ln_fill_el, theme, usage.unresolved):
        usage.line[color] += 1


def _collect_text_body(
    sp_element, canvas: Canvas, theme: ThemeInfo, usage: Usage, embedded_fonts: dict[str, tuple[bool, bool]],
) -> None:
    tx_body = sp_element.find(qn("p:txBody"))
    if tx_body is None:
        return
    _collect_paragraphs(tx_body, canvas, theme, usage, embedded_fonts)


def _collect_paragraphs(
    tx_body, canvas: Canvas, theme: ThemeInfo, usage: Usage, embedded_fonts: dict[str, tuple[bool, bool]],
) -> None:
    """Общая для p:txBody шейпа и a:txBody ячейки таблицы (a:tc) — оба несут
    те же a:p/a:r по схеме, разница только в имени/пространстве имён
    обёртки, которую резолвит вызывающий (_collect_text_body / _collect_table)."""
    for p in tx_body.findall(qn("a:p")):
        _collect_paragraph_props(p, usage)
        for r in p.findall(qn("a:r")):
            usage.total_runs += 1
            _collect_run(r, canvas, theme, usage, embedded_fonts)


def _collect_paragraph_props(p, usage: Usage) -> None:
    p_pr = p.find(qn("a:pPr"))
    if p_pr is None:
        return
    algn = p_pr.get("algn")
    if algn:
        usage.align[algn] += 1
    lnSpc = p_pr.find(qn("a:lnSpc"))
    spc_pct = lnSpc.find(qn("a:spcPct")) if lnSpc is not None else None
    if spc_pct is not None and spc_pct.get("val") is not None:
        usage.line_spacing[int(spc_pct.get("val")) / 1000] += 1


def _collect_table(
    graphic_frame, canvas: Canvas, theme: ThemeInfo, usage: Usage, embedded_fonts: dict[str, tuple[bool, bool]],
) -> None:
    """Текст и заливка ячеек a:tbl — не полноценный разбор таблицы (см.
    докстроку модуля: без геометрии колонок/строк и объединённых ячеек),
    только то, что нужно для палитры/типографики: иначе на table-heavy
    шаблоне статистика молча теряет часть текста и цвета."""
    tbl = _find_table(graphic_frame)
    if tbl is None:
        return
    for tc in tbl.iter(qn("a:tc")):
        _collect_cell_fill(tc, theme, usage)
        tx_body = tc.find(qn("a:txBody"))
        if tx_body is not None:
            _collect_paragraphs(tx_body, canvas, theme, usage, embedded_fonts)


def _find_table(graphic_frame):
    graphic = graphic_frame.find(qn("a:graphic"))
    graphic_data = graphic.find(qn("a:graphicData")) if graphic is not None else None
    return graphic_data.find(qn("a:tbl")) if graphic_data is not None else None


def _collect_cell_fill(tc, theme: ThemeInfo, usage: Usage) -> None:
    """Заливка ячейки (a:tcPr) — в общий счётчик fill, тот же смысл, что и
    заливка p:sp. noFill/отсутствие заливки не считаем (дефолт большинства
    ячеек таблицы — не сигнал карточной вёрстки, как у p:sp, поэтому
    no_fill_shapes здесь не трогаем)."""
    tc_pr = tc.find(qn("a:tcPr"))
    if tc_pr is None:
        return
    fill_el = _pick_fill_element(tc_pr)
    if fill_el is None or local_name(fill_el) == "noFill":
        return
    for color in _fill_colors(fill_el, theme, usage.unresolved):
        usage.fill[color] += 1


def _has_explicit_props(r_pr) -> bool:
    """Есть ли в a:rPr хоть одно из свойств, которые этот модуль читает
    (sz/b/i/latin/заливка) — не просто "a:rPr присутствует как тег".
    `<a:rPr lang="ru-RU" dirty="0"/>` без sz/latin/fill/b/i технически
    присутствует, но не даёт прочитать ничего: по эффекту для сборщика это
    то же самое, что и полное отсутствие a:rPr, и должно точно так же
    попадать в inherited_chars, а не в explicit_chars."""
    if r_pr is None:
        return False
    return (
        r_pr.get("sz") is not None
        or r_pr.get("b") is not None
        or r_pr.get("i") is not None
        or r_pr.find(qn("a:latin")) is not None
        or _pick_fill_element(r_pr) is not None
    )


def _collect_run(
    r, canvas: Canvas, theme: ThemeInfo, usage: Usage, embedded_fonts: dict[str, tuple[bool, bool]],
) -> None:
    t_el = r.find(qn("a:t"))
    text = t_el.text or "" if t_el is not None else ""
    chars = len(text)

    r_pr = r.find(qn("a:rPr"))
    if not _has_explicit_props(r_pr):
        # Ни sz, ни latin, ни заливки, ни b/i — либо a:rPr нет вовсе, либо
        # он пуст содержательно. Свойства наследуются от лейаута/мастера/
        # темы, что этот модуль не резолвит (см. докстроку). Символы при
        # этом не теряются: идут в inherited_chars, а не пропадают из
        # статистики молча (Task 3 код-ревью, п.5 — раньше run без rPr не
        # учитывался нигде).
        usage.inherited_chars += chars
        return
    usage.explicit_chars += chars

    if r_pr.get("b") == "1":
        usage.bold_runs += 1
    if r_pr.get("i") == "1":
        usage.italic_runs += 1

    sz_raw = r_pr.get("sz")
    if sz_raw is not None:
        pt = int(sz_raw) / 100 * canvas.norm
        usage.sizes_pt[round(pt * 2) / 2] += 1

    latin_el = r_pr.find(qn("a:latin"))
    family = _resolve_font_family(latin_el, theme) if latin_el is not None else None
    if family:
        fu = usage.fonts.get(family)
        if fu is None:
            bold_av, italic_av = embedded_fonts.get(family, (False, False))
            fu = FontUsage(
                family=family, embedded=family in embedded_fonts,
                bold_available=bold_av, italic_available=italic_av,
            )
            usage.fonts[family] = fu
        fu.chars += chars
        fu.runs += 1

    fill_el = _pick_fill_element(r_pr)
    if fill_el is not None and local_name(fill_el) != "noFill" and chars:
        for color in _fill_colors(fill_el, theme, usage.unresolved):
            usage.text[color] += chars


def _resolve_font_family(latin_el, theme: ThemeInfo) -> str | None:
    """Токены темы (+mj-lt/+mn-lt) резолвятся в major_font/minor_font темы
    БЕЗУСЛОВНО (Task 3 код-ревью, п.4) — это прямое указание формата, не
    эвристика: раньше при font_scheme_degraded=True токен резолвился в
    None, то есть шрифт таких run'ов терялся, хотя нативный PowerPoint (в
    отличие от Google-экспорта, где токенов не встречается вовсе) пишет
    шрифт именно так — на нативном шаблоне это большие куски текста."""
    typeface = latin_el.get("typeface")
    if not typeface:
        return None
    if typeface == _MAJOR_FONT_TOKEN:
        return theme.major_font or None
    if typeface == _MINOR_FONT_TOKEN:
        return theme.minor_font or None
    return typeface


def _read_embedded_fonts(pkg: PptxPackage) -> dict[str, tuple[bool, bool]]:
    """typeface → (доступен ли bold, доступен ли italic) из p:embeddedFontLst
    в presentation.xml. Курсива не бывает ни у одного из трёх шаблонов —
    p:italic там просто нет."""
    root = pkg.xml(pkg.presentation_part())
    lst = root.find(qn("p:embeddedFontLst"))
    if lst is None:
        return {}

    result: dict[str, tuple[bool, bool]] = {}
    for embedded_font in lst.findall(qn("p:embeddedFont")):
        font_el = embedded_font.find(qn("p:font"))
        family = font_el.get("typeface") if font_el is not None else None
        if not family:
            continue
        # p:boldItalic — совмещённый вариант; его наличие тоже означает, что
        # у семейства есть встроенный жирный и встроенный курсивный рисунок,
        # даже если отдельных p:bold/p:italic нет (в трёх шаблонах такого
        # нет, но контракт CT_EmbeddedFontListEntry такое допускает).
        has_bold_italic = embedded_font.find(qn("p:boldItalic")) is not None
        has_bold = embedded_font.find(qn("p:bold")) is not None or has_bold_italic
        has_italic = embedded_font.find(qn("p:italic")) is not None or has_bold_italic
        result[family] = (has_bold, has_italic)
    return result
