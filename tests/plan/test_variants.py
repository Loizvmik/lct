"""Тесты `plan.variants.apply_variant` (Task 13, Step 1 брифа — тесты
приведены дословно, фикстуры `DECK`/`PROFILE`/`TEMPLATE`/`CONFIG` брифом не
заданы буквально (только сигнатуры и поведение), собраны здесь тем же
приёмом, что `tests/compose/test_builder.py::SAMPLE_SPEC` — реальным
содержанием пакета `fixtures/content-packs/queue-latency` на реальном
шаблоне VK Tech (20 паттернов: bullets×11, cards×4, two_col×3, section×2,
см. `tests/audit/conftest.py`)."""
from __future__ import annotations
import re
from itertools import combinations
from pathlib import Path

from deckforge.audit.config import AuditConfig
from deckforge.compose.builder import build_deck
from deckforge.plan.spec import BulletBlock, Card, CardBlock, DeckSpec, SlideSpec, TextBlock
from deckforge.plan.variants import MAX_SLIDES, MIN_SLIDES, Variant, apply_variant
from deckforge.audit.deterministic import run_deterministic
from deckforge.template.profile import TemplateProfile

TEMPLATE = Path("dataset/templates/VK Tech шаблон.pptx")
PROFILE = TemplateProfile.from_file(TEMPLATE)
CONFIG = AuditConfig.load()

DECK = DeckSpec(
    title="Сокращение времени согласования заявок",
    language="ru",
    slides=[
        SlideSpec(index=0, kind="section", headline="Автоматическая маршрутизация заявок"),
        SlideSpec(
            index=1, kind="bullets", headline="Где уходит время",
            blocks=[BulletBlock(items=[
                "Ожидание первого согласующего — медиана 18 часов",
                "Ожидание второго согласующего — медиана 11 часов",
                "Чистая работа людей — 28 минут из 31,5 часа сквозного цикла",
            ])],
            source_note="Источник: замер 1 240 заявок, март—май 2026",
        ),
        SlideSpec(
            index=2, kind="two_col", headline="Причины ожидания",
            blocks=[
                TextBlock(text="71 заявка ушла не туда"),
                TextBlock(text="48 заявок — без подмены"),
            ],
            source_note="Источник: разбор 180 самых медленных заявок",
        ),
        SlideSpec(
            index=3, kind="cards", headline="Что нужно для раскатки",
            blocks=[CardBlock(items=[
                Card(title="Правила закупок", body="Доработка правил для закупок свыше 5 млн ₽ — 3 недели"),
                Card(title="Кадровая система", body="Интеграция для автоподмены отсутствующих — 2 недели"),
                Card(title="Обучение", body="340 согласующих, 4 вебинара по 40 минут"),
                Card(title="Бюджет", body="2,4 млн ₽, из них 1,9 млн ₽ ФОТ"),
            ])],
            source_note="Источник: план раскатки, сентябрь 2026",
        ),
        SlideSpec(
            index=4, kind="bullets", headline="Пилот дал результат",
            blocks=[BulletBlock(items=[
                "Сквозная медиана сократилась с 31,5 до 6,2 часа",
                "Доля переназначений вручную упала с 39% до 4%",
                "Заявок с просрочкой SLA — с 23% до 6%",
                "Оценка удобства авторами — с 2,8 до 4,1 из 5",
            ])],
            source_note="Источник: пилот, июнь—август 2026, 410 заявок",
        ),
        SlideSpec(
            index=5, kind="cards", headline="Отрицательный результат тоже был",
            blocks=[CardBlock(items=[
                Card(title="Нетиповые закупки", body="У 11 заявок правило сработало неверно — маршрут правили руками"),
                Card(title="Причина", body="Все 11 — нетиповые закупки свыше 5 млн ₽"),
            ])],
            source_note="Источник: пилот, июнь—август 2026",
        ),
        SlideSpec(
            index=6, kind="two_col", headline="Риски раскатки",
            blocks=[
                TextBlock(text="Задержка данных — сутки"),
                TextBlock(text="4 из 11 — свои регламенты"),
            ],
            source_note="Источник: анализ рисков, сентябрь 2026",
        ),
        SlideSpec(
            index=7, kind="bullets", headline="Причины ожидания по разбору",
            blocks=[BulletBlock(items=[
                "39 заявок ждали, пока автор допишет обоснование",
                "22 заявки застряли из-за дублирующего согласования двух отделов",
            ])],
            source_note="Источник: разбор 180 самых медленных заявок",
        ),
        SlideSpec(
            index=8, kind="cards", headline="Команда и бюджет",
            # Без title (Card.title="" по умолчанию, план это разрешает
            # прямо — см. докстроку CardBlock в plan/spec.py) — короткие
            # карточки-факты без отдельного заголовка одинаково хорошо
            # ложатся на БОЛЬШЕ раскладок шаблона, чем title+тело, см.
            # разбор в отчёте задачи (обязательная проверка): у части
            # `cards`-раскладок VK Tech нет отдельного слота под заголовок
            # карточки вовсе, а те, что есть, вмещают заметно меньше знаков.
            blocks=[CardBlock(items=[
                Card(body="Команда — 2 человека на доработку правил закупок"),
                Card(body="Срок — полная раскатка к 15 декабря 2026"),
                Card(body="Бюджет — 2,4 млн ₽"),
            ])],
            source_note="Источник: план раскатки, сентябрь 2026",
        ),
        SlideSpec(
            index=9, kind="bullets", headline="Замер текущего процесса",
            blocks=[BulletBlock(items=[
                "Оформление заявки автором — медиана 12 минут",
                "Ожидание первого согласующего — медиана 18 часов, 90-й процентиль 74 часа",
                "Исполнение — медиана 2 часа",
            ])],
            source_note="Источник: замер 1 240 заявок, 4 подразделения, март—май 2026",
        ),
        SlideSpec(
            index=10, kind="two_col", headline="Что дальше",
            blocks=[
                TextBlock(text="Раскатка — к 15.12.2026"),
                TextBlock(text="4 подразделения — в 2027"),
            ],
            source_note="Источник: план раскатки, сентябрь 2026",
        ),
        SlideSpec(index=11, kind="section", headline="Вопросы"),
    ],
)


_NUMBER_RE = re.compile(r"\d[\d\s.,]*")


def _numbers(spec: DeckSpec) -> list[str]:
    """Мультимножество числовых токенов колоды — используется, чтобы
    убедиться, что три варианта несут ОДНО И ТО ЖЕ содержание (брифом:
    "различаться должна вёрстка, а не содержание"). Собирает текст со всех
    полей, где `apply_variant` мог бы его переставить/потерять: заголовки,
    подзаголовки, все виды блоков, source_note — НЕ учитывает синтетические
    слайды-разделители `airy` (их заголовки — фиксированные слова без единой
    цифры, см. `plan.variants._DIVIDER_LABELS`, так что они и так не добавят
    ни одного числа — явного исключения не нужно)."""
    texts: list[str] = []
    for slide in spec.slides:
        texts.append(slide.headline or "")
        texts.append(slide.subhead or "")
        texts.append(slide.source_note or "")
        for block in slide.blocks:
            if isinstance(block, TextBlock):
                texts.append(block.text)
            elif isinstance(block, BulletBlock):
                texts.extend(block.items)
            elif isinstance(block, CardBlock):
                for card in block.items:
                    texts.append(card.title)
                    texts.append(card.body)
    numbers = []
    for text in texts:
        numbers.extend(m.strip() for m in _NUMBER_RE.findall(text))
    return sorted(numbers)


def profile_fixture_without_image_patterns() -> TemplateProfile:
    """Копия `PROFILE` без единого паттерна `kind="image"` — тест
    брифа `test_variant_falls_back_when_template_lacks_a_pattern_kind`
    проверяет, что `visual` (предпочитающий именно `image`) деградирует на
    доступные `kind`, а не падает и не оставляет `pattern_id` пустым.
    VK Tech и без фильтра не несёт паттернов `kind="image"` (20 паттернов:
    bullets/cards/two_col/section, см. докстроку модуля) — фильтр
    гарантирует это явно, а не полагается на случайный состав шаблона."""
    return PROFILE.model_copy(update={"patterns": [p for p in PROFILE.patterns if p.kind != "image"]})


def test_three_variants_are_visually_distinct():
    specs = [apply_variant(DECK, PROFILE, v) for v in Variant]
    pattern_sets = [{s.pattern_id for s in spec.slides} for spec in specs]
    for a, b in combinations(pattern_sets, 2):
        assert len(a ^ b) >= 3, "варианты используют почти одни и те же паттерны"


def test_three_variants_carry_the_same_facts():
    """Различаться должна вёрстка, а не содержание: иначе сравнивать нечего."""
    facts = [_numbers(spec) for spec in (apply_variant(DECK, PROFILE, v) for v in Variant)]
    assert facts[0] == facts[1] == facts[2]


def test_every_variant_stays_within_the_slide_budget():
    for variant in Variant:
        count = len(apply_variant(DECK, PROFILE, variant).slides)
        assert MIN_SLIDES <= count <= MAX_SLIDES


def test_every_variant_passes_the_deterministic_audit():
    """Одинаково соответствуют правилам шаблона — это проверяется, а не
    декларируется. Брифом дословно фильтрует `severity == "error"` — в
    этом кодовом дереве `Finding.severity` несёт три значения
    (`"critical"/"major"/"minor"`, см. `audit/findings.py`), не `"error"`;
    "критично для пользователя" здесь — `critical`/`major` (видимый брак
    или расхождение с дизайн-системой шаблона), `minor` — отклонение,
    которое может быть осознанным решением (см. докстроку `Finding`) и
    само ТЗ уже мирится с ним у любой собранной колоды."""
    for variant in Variant:
        path = build_deck(apply_variant(DECK, PROFILE, variant), PROFILE, TEMPLATE, variant)
        errors = [f for f in run_deterministic(path, PROFILE, CONFIG) if f.severity in ("critical", "major")]
        assert errors == [], f"{variant}: {errors}"


def test_variant_falls_back_when_template_lacks_a_pattern_kind():
    """У шаблона может не быть ни одного image-паттерна. Вариант `visual`
    обязан деградировать на доступные, а не упасть."""
    poor = profile_fixture_without_image_patterns()
    spec = apply_variant(DECK, poor, Variant.visual)
    assert all(s.pattern_id for s in spec.slides)
