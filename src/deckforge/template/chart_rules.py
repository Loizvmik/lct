"""Правила оформления графиков, которые шаблон пишет прямо на слайде.

VK Education на слайде 51 «Оформление диаграмм» перечисляет их текстом:
«Перекрытие рядов = ±50%. Боковой зазор = 0%. Избавляемся от линий
сетки, засечек». Родных графиков в шаблоне нет, и если строить наш
график по умолчаниям python-pptx, он выйдет с сеткой и толстыми зазорами,
вопреки тому, что шаблон прямо просит. Здесь снимается только то, что
читается однозначно: число после «перекрытие рядов» и «боковой зазор»,
просьба убрать сетку, просьба подписывать значения («метки данных»).
Выбор вида графика остаётся слою данных (`plan.data_types`).

Без модели и без координат: чистый разбор текста слайдов пакета."""
from __future__ import annotations
import re
from dataclasses import dataclass, replace

from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage

# «Перекрытие рядов = ±50%», «перекрытие рядов 50 %». Знак «±» дизайнер
# пишет, когда имеет в виду величину, а не направление; ряды при этом
# расходятся, а не прячутся друг за друга, поэтому «±» читается как минус.
_OVERLAP_RE = re.compile(r"перекрыти\w*\s+ряд\w*\s*[=:—–-]?\s*(±|\+/-|[+\-−])?\s*(\d{1,3})\s*%", re.IGNORECASE)
_GAP_RE = re.compile(r"боков\w*\s+зазор\w*\s*[=:—–-]?\s*(\d{1,3})\s*%", re.IGNORECASE)
# «Метки данных вместо вертикальной оси»: подписи значений на столбиках.
_DATA_LABELS_RE = re.compile(r"метк\w*\s+данных|подпис\w*\s+значени", re.IGNORECASE)
# «Избавляемся от линий сетки», «без линий сетки», «убрать сетку».
_NO_GRID_RE = re.compile(
    r"(избав\w*|без|убира\w*|убер\w*|убрать|не\s+использ\w*)\s+(от\s+)?(лини\w+\s+)?сетк", re.IGNORECASE,
)


@dataclass(frozen=True)
class ChartRules:
    """`None`: шаблон про это ничего не сказал, сборка оставляет своё.
    `source_slide`: номер слайда, с которого сняты правила, для отчёта."""
    overlap: int | None = None
    gap_width: int | None = None
    no_gridlines: bool = False
    data_labels: bool = False
    source_slide: int | None = None

    @property
    def empty(self) -> bool:
        return self.overlap is None and self.gap_width is None and not self.no_gridlines and not self.data_labels


_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")


def _slide_text(pkg: PptxPackage, name: str) -> str:
    root = pkg.xml(name)
    paragraphs = []
    for p in root.iter(qn("a:p")):
        paragraphs.append("".join(t.text or "" for t in p.iter(qn("a:t"))))
    return " ".join(paragraphs)


def parse_chart_rules(text: str) -> ChartRules:
    """Правила из одного текста. Отдельно от обхода пакета: так их
    проверяют тесты без файла шаблона."""
    overlap = gap = None
    m = _OVERLAP_RE.search(text)
    if m:
        sign, value = m.group(1), int(m.group(2))
        overlap = -value if sign in ("±", "+/-", "-", "−") else value
        overlap = max(-100, min(100, overlap))
    m = _GAP_RE.search(text)
    if m:
        gap = max(0, min(500, int(m.group(1))))
    return ChartRules(
        overlap=overlap, gap_width=gap, no_gridlines=bool(_NO_GRID_RE.search(text)),
        data_labels=bool(_DATA_LABELS_RE.search(text)),
    )


def find_chart_rules(pkg: PptxPackage) -> ChartRules:
    """Первый слайд-пример, на котором нашлось хоть одно правило. Слайд с
    правилами один на шаблон, и собирать правила по разным слайдам значило
    бы склеить советы из разных контекстов."""
    slides = sorted(
        ((int(m.group(1)), n) for n in pkg.names() if (m := _SLIDE_RE.match(n))),
    )
    for number, name in slides:
        text = _slide_text(pkg, name)
        if not re.search(r"диаграмм|график|chart", text, re.IGNORECASE):
            continue
        rules = parse_chart_rules(text)
        if not rules.empty:
            return replace(rules, source_slide=number)
    return ChartRules()
