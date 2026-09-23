"""Инфраструктура тестов Task 14 (`test_html.py`) — `PROFILE`/`PPTX`/`DECK`
собраны на РЕАЛЬНОМ шаблоне (`dataset/templates/Шаблон презентации VK
Education.pptx`), не на синтетике: это единственный из трёх учебных
шаблонов, у которого `TemplateProfile.patterns` несёт паттерны с ролями
`table`/`chart`/`kpi` (см. `template/patterns.py` — VK Tech и WorkSpace их
не намайнили вовсе, см. отчёт задачи), а `to_html` обязан честно
воспроизводить ИМЕННО ТО, что реально легло на слайд, включая таблицу и
график — синтетический `ShapeRef`-мок этого не проверил бы.

`DECK`/`PPTX` собираются один раз на сессию (`build_deck` — не бесплатный
вызов: аудит слайда встроен в цикл сборки, см. `compose.builder`) и
переиспользуются всеми тестами модуля."""
from __future__ import annotations
import functools
from pathlib import Path

import pytest

from deckforge.compose.builder import Variant, build_deck
from deckforge.plan.spec import (
    BulletBlock, Card, CardBlock, ChartSeriesData, ChartVisual, DeckSpec, Kpi, KpiBlock, SlideSpec,
    TableVisual, Visual, ensure_valid_deck_spec,
)
from deckforge.template.profile import TemplateProfile

TEMPLATE = Path("dataset/templates/Шаблон презентации VK Education.pptx")


@functools.lru_cache(maxsize=None)
def _profile() -> TemplateProfile:
    return TemplateProfile.from_file(TEMPLATE, cache_dir=None)


@pytest.fixture(scope="session")
def PROFILE() -> TemplateProfile:
    return _profile()


def _deck_spec() -> DeckSpec:
    return DeckSpec(
        title="Экспорт: проверка форматов",
        language="ru",
        slides=[
            SlideSpec(
                index=0, kind="section", headline="Экспорт в HTML, PDF и PPTX",
                speaker_notes="Тут сказать, что слайд — не картинка, а текст.",
            ),
            SlideSpec(
                index=1, kind="bullets", headline="Где уходит время",
                blocks=[BulletBlock(items=[
                    "Пункт один про экспорт", "Пункт два про рендер", "Пункт три про формат",
                ])],
                source_note="Источник: тестовые данные task-14",
            ),
            SlideSpec(
                index=2, kind="kpi", headline="Метрики выгрузки",
                blocks=[KpiBlock(items=[
                    Kpi(value="3", label="формата"), Kpi(value="4", label="шаблона"),
                    Kpi(value="110", label="dpi превью"), Kpi(value="5 мин", label="бюджет"),
                ])],
                source_note="Источник: тестовые данные task-14",
            ),
            SlideSpec(
                index=3, kind="cards", headline="Три формата выгрузки",
                blocks=[CardBlock(items=[
                    Card(title="HTML", body="Самодостаточная страница без сети"),
                    Card(title="PDF", body="Через LibreOffice, свой профиль на вызов"),
                    Card(title="PPTX", body="Нативные объекты, не растр"),
                ])],
            ),
            SlideSpec(
                index=4, kind="table", headline="Сравнение форматов",
                visual=Visual(kind="table", table=TableVisual(rows=[
                    ["Формат", "Текст", "Навигация"],
                    ["HTML", "да", "стрелки"], ["PDF", "да", "страницы"], ["PPTX", "да", "слайды"],
                ])),
                source_note="Источник: тестовые данные task-14",
            ),
            SlideSpec(
                index=5, kind="table", headline="Время выгрузки по шаблонам",
                visual=Visual(kind="chart", chart=ChartVisual(
                    kind="bar", categories=["VK Tech", "WorkSpace", "Education"],
                    series=[ChartSeriesData(name="Секунды", values=[4.0, 5.0, 6.0])], unit="с",
                )),
                source_note="Источник: замер на этой машине",
            ),
        ],
    )


@functools.lru_cache(maxsize=None)
def _built() -> tuple[DeckSpec, Path]:
    deck = _deck_spec()
    ensure_valid_deck_spec(deck)
    pptx_path = build_deck(deck, _profile(), TEMPLATE, Variant.dense)
    return deck, pptx_path


@pytest.fixture(scope="session")
def DECK() -> DeckSpec:
    return _built()[0]


@pytest.fixture(scope="session")
def PPTX() -> Path:
    return _built()[1]
