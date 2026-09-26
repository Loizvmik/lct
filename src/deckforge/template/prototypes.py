"""Слайды шаблона, которые не раскладки содержания: образцы оформления
графиков, правила, листы ассетов, инструкции дизайнера.

VK Education на слайдах 47-50 показывает, как у него выглядит график
(картинка графика с заголовком «Пример оформления графика»), а на слайде
51 пишет правила («Перекрытие рядов = ±50%, без линий сетки»). WorkSpace
на слайдах 19-21 «Графики» делает то же картинками. Родных графиков в этих
шаблонах нет. Как раскладки содержания такие слайды бесполезны или вредны
(картинку-график примера нельзя оставить на слайде про другое), а как
образец стиля они единственный источник того, каким должен быть наш
график: где он стоит, в каких цветах, с сеткой или без.

Здесь, без модели:
- `ChartStylePrototype`: образец графика (рамка, вид, палитра с самой
  картинки, сетка, подписи значений, зазор и перекрытие из правил);
- `classify_slides`: класс слайда-примера у каждого паттерна
  (`SLIDE_CLASSES`), по нему планировщик не ставит образцы и правила под
  обычное содержание."""
from __future__ import annotations
import colorsys
import io
import re
from dataclasses import dataclass, field, replace

from PIL import Image

from deckforge.ooxml.geometry import Box, Canvas
from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.walk import walk_shapes
from deckforge.template.chart_rules import ChartRules
from deckforge.template.patterns import CHART_FRAME_MIN_AREA, Pattern, slide_mentions_chart

SLIDE_CLASSES = ("content_pattern", "style_guide", "asset_sheet", "visual_prototype", "instruction")

# Картинка крупнее этой доли холста: фон слайда, а не рамка графика
# (WorkSpace, слайд 19: кольцевая диаграмма нарисована на картинке во весь
# слайд, и рамки под наш график у неё нет).
_BACKGROUND_AREA = 0.9
_PALETTE_MAX = 5
# Пиксель цветной, если насыщенность и яркость не ниже этих: белый фон,
# чёрные подписи и серая сетка в палитру ряда не идут.
_MIN_SATURATION = 0.35
_MIN_VALUE = 0.25
# Цвет в палитру, если он занимает не меньше этой доли цветных пикселей:
# сглаживание краёв даёт десятки промежуточных оттенков по чуть-чуть.
_MIN_SHARE = 0.05
# Два цвета ближе этого (евклидово по RGB 0..255) считаются одним.
_MIN_DISTANCE = 60.0

# Вид графика по словам слайда: только однозначные слова. «График» и
# «диаграмма» вида не называют, тогда вид решают данные.
_TYPE_WORDS = (
    (re.compile(r"гистограмм|столбч|столбик", re.IGNORECASE), "bar"),
    (re.compile(r"област", re.IGNORECASE), "area"),
    (re.compile(r"кольцев|бублик|doughnut|donut", re.IGNORECASE), "doughnut"),
    (re.compile(r"кругов|\bpie\b", re.IGNORECASE), "pie"),
    (re.compile(r"линейн|\bline\b", re.IGNORECASE), "line"),
)

# «Оформление таблиц» у VK Education одновременно правило и единственная
# раскладка таблицы, поэтому «оформление» здесь не признак: правилами
# считается слайд, с которого сняты правила графиков, или прямо
# названный гайд.
_STYLE_GUIDE_RE = re.compile(r"^\s*(правила|гайдлайн|guideline|style\s*guide)", re.IGNORECASE)
# Инструкция к шаблону: «Как пользоваться шаблоном», «Памятка».
_INSTRUCTION_RE = re.compile(r"как\s+(пользоваться|работать)|инструкц|памятк|how\s+to\s+use", re.IGNORECASE)
# Повелительное наклонение само не признак: рыба VK Tech «Оцените высокий
# уровень защищённости» стоит в обычных карточках.
# Лист ассетов: много картинок И текст об ассетах. Одних картинок мало:
# карточки VK Tech с иконкой в каждой (слайд 20) обычная раскладка.
_ASSET_SHEET_RE = re.compile(r"иконк|пиктограм|иллюстрац|логотип|\bicons?\b|ассет", re.IGNORECASE)
_ASSET_MIN_PICTURES = 8


@dataclass(frozen=True)
class ChartStylePrototype:
    """Образец графика со слайда шаблона. `pattern_id`: раскладка, снятая с
    того же слайда (её клон даёт рамку и фон), `None`, если слайд в
    раскладки не попал. `frame_box`: рамка картинки-графика в долях
    холста. `chart_type`: вид по словам слайда или `None`. `palette`:
    цвета рядов с самой картинки, по убыванию площади. `axis_style`:
    "data_labels", если правила просят подписи значений вместо оси, иначе
    "axes"."""
    source_slide: int
    frame_box: Box
    pattern_id: str | None = None
    chart_type: str | None = None
    palette: list[str] = field(default_factory=list)
    gridlines: bool = True
    show_values: bool = False
    gap_width: int | None = None
    overlap: int | None = None
    axis_style: str = "axes"


_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")


def _slide_text(root) -> str:
    return " ".join("".join(t.text or "" for t in p.iter(qn("a:t"))) for p in root.iter(qn("a:p")))


def _chart_type(text: str) -> str | None:
    hits = [(m.start(), kind) for rx, kind in _TYPE_WORDS if (m := rx.search(text))]
    return min(hits)[1] if hits else None


def image_palette(blob: bytes) -> list[str]:
    """Цвета рядов с картинки графика: цветные пиксели, сведённые в
    крупные корзины, по убыванию площади. Пустой список: картинка не
    читается или цветного на ней почти нет."""
    try:
        with Image.open(io.BytesIO(blob)) as img:
            # Прозрачный фон PNG без подложки читается чёрным, и тонкая
            # линия графика, смешанная с ним при уменьшении, темнеет до
            # бордового (VK Education, слайд 48). Подложка белая, как слайд.
            rgba = img.convert("RGBA")
            flat = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            flat.alpha_composite(rgba)
            flat = flat.convert("RGB")
            flat.thumbnail((96, 96))
            pixels = list(flat.getdata())
    except Exception:  # noqa: BLE001: svg, битый файл: палитры нет, не ошибка разбора
        return []
    buckets: dict[tuple[int, int, int], list[tuple[int, int, int]]] = {}
    for r, g, b in pixels:
        _h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < _MIN_SATURATION or v < _MIN_VALUE:
            continue
        buckets.setdefault((r // 48, g // 48, b // 48), []).append((r, g, b))
    colored = sum(len(v) for v in buckets.values())
    if not colored or colored < 0.02 * len(pixels):
        return []
    palette: list[tuple[int, int, int]] = []
    for members in sorted(buckets.values(), key=len, reverse=True):
        if len(members) < _MIN_SHARE * colored:
            break
        mean = tuple(round(sum(c[i] for c in members) / len(members)) for i in range(3))
        if all(sum((a - b) ** 2 for a, b in zip(mean, other)) ** 0.5 >= _MIN_DISTANCE for other in palette):
            palette.append(mean)
        if len(palette) >= _PALETTE_MAX:
            break
    return ["#{:02X}{:02X}{:02X}".format(*c) for c in palette]


def _picture_blob(pkg: PptxPackage, slide_part: str, pic_element) -> bytes | None:
    blip = pic_element.find(".//" + qn("a:blip"))
    rid = blip.get(qn("r:embed")) if blip is not None else None
    target = pkg.rels(slide_part).get(rid) if rid else None
    if not target:
        return None
    try:
        return pkg.part(target)
    except KeyError:
        return None


def _clip(box: Box) -> Box:
    left, top = max(0.0, box.left), max(0.0, box.top)
    right, bottom = min(1.0, box.left + box.width), min(1.0, box.top + box.height)
    return Box(left=left, top=top, width=max(0.0, right - left), height=max(0.0, bottom - top))


def find_chart_prototypes(
    pkg: PptxPackage, canvas: Canvas, patterns: list[Pattern], rules: ChartRules | None,
) -> list[ChartStylePrototype]:
    """Образцы графиков шаблона по возрастанию номера слайда: слайд, чей
    текст говорит о графике, с картинкой не меньше `CHART_FRAME_MIN_AREA`
    холста и не во весь слайд. Рамка: самая крупная такая картинка."""
    rules = rules or ChartRules()
    by_slide = {n: p.pattern_id for p in patterns for n in p.source_slide_index[:1]}
    slides = sorted((int(m.group(1)), name) for name in pkg.names() if (m := _SLIDE_RE.match(name)))
    out: list[ChartStylePrototype] = []
    for number, name in slides:
        root = pkg.xml(name)
        text = _slide_text(root)
        if not slide_mentions_chart(text):
            continue
        frames = []
        for ref in walk_shapes(root, canvas, include_groups=False):
            if ref.kind != "picture" or ref.box is None:
                continue
            box = _clip(ref.box)
            if CHART_FRAME_MIN_AREA <= box.area < _BACKGROUND_AREA:
                frames.append((box.area, box, ref.element))
        if not frames:
            continue
        _area, box, element = max(frames, key=lambda t: t[0])
        blob = _picture_blob(pkg, name, element)
        out.append(ChartStylePrototype(
            source_slide=number, frame_box=box, pattern_id=by_slide.get(number),
            chart_type=_chart_type(text), palette=image_palette(blob) if blob else [],
            gridlines=not rules.no_gridlines, show_values=rules.data_labels,
            gap_width=rules.gap_width, overlap=rules.overlap,
            axis_style="data_labels" if rules.data_labels else "axes",
        ))
    return out


def slide_class_of(pattern: Pattern, rules: ChartRules | None, prototype_slides: set[int]) -> str:
    """Класс слайда-примера (`SLIDE_CLASSES`) по его тексту и составу.
    Порядок проверок от самого узнаваемого: образец графика, правила,
    лист ассетов, инструкция; всё прочее раскладка содержания."""
    first = pattern.source_slide_index[0] if pattern.source_slide_index else None
    if first in prototype_slides or any(getattr(s, "chart_frame", False) for s in pattern.slots):
        return "visual_prototype"
    headline = next((s.sample_text or "" for s in pattern.slots if s.role == "headline"), "")
    if (rules is not None and first is not None and first == rules.source_slide) or _STYLE_GUIDE_RE.search(headline):
        return "style_guide"
    pictures = sum(1 for s in pattern.slots if s.role in ("image", "icon"))
    pictures += sum(1 for d in pattern.decor if getattr(d, "kind", "") == "picture")
    texts = " ".join(s.sample_text or "" for s in pattern.slots)
    if pictures >= _ASSET_MIN_PICTURES and _ASSET_SHEET_RE.search(texts):
        return "asset_sheet"
    if _INSTRUCTION_RE.search(headline):
        return "instruction"
    return "content_pattern"


def classify_slides(
    patterns: list[Pattern], rules: ChartRules | None, prototypes: list[ChartStylePrototype],
) -> list[Pattern]:
    slides = {p.source_slide for p in prototypes}
    return [replace(p, slide_class=slide_class_of(p, rules, slides)) for p in patterns]
