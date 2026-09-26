"""Сборка `.pptx` — колода заводится ОТ САМОГО ФАЙЛА ШАБЛОНА
(`Presentation(template_path)`, не `Presentation()` с нуля): мастера,
лейауты, тема, встроенные шрифты и media остаются нативными, слайды-
примеры удаляются аккуратно (`p:sldIdLst` + relationships, см.
`_clear_sample_slides`) — так проверка аудита T04 «слайд собран не на
макете из шаблона» проходит по построению, а не по совпадению.

Единственное место в `compose/`, которое трогает и `textfit` (замер), и
python-pptx (рисование) одновременно — блоки контента (`compose.blocks`)
и декор (`compose.decor`) сами python-pptx не касаются.
"""
from __future__ import annotations
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field, replace
from typing import Callable
from pathlib import Path

from lxml import etree
from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.enum.text import PP_ALIGN
from pptx.enum.text import MSO_ANCHOR
from pptx.util import Emu, Pt

from deckforge.audit.config import AuditConfig
from deckforge.audit.deterministic import audit_slide_layout
from deckforge.compose.blocks import (
    DROPPED_ROLE_TITLES, Paragraph, SlotContent, assign_content, assign_content_with_drops,
    expand_decor, filled_repeat_units, find_bullet_char,
)
from deckforge.compose.charts import ChartSpec, Series, add_chart
from deckforge.compose.colorpick import slide_background_luminance
from deckforge.compose.clone import (
    allow_wrap, bind_text, clone_example_slide, fill_native_table, fix_duplicate_partnames,
    inherited_text_size, mark_slide, match_slots, native_table, prune_unfilled, remove_in_box,
    remove_sample_frames, remove_stray_text, replace_picture, sample_slides_by_number,
    set_native_table_geometry, set_shape_box, set_table_text_size, set_text_size, shape_text, slide_refs,
    table_cell_styles, template_row_heights_emu, text_style,
)
from deckforge.compose.decor import apply_decor
from deckforge.compose.tables import TableSpec, add_table, column_shares
from deckforge.compose.textfit import measure, register_template_fonts
from deckforge.ooxml.color import Color, resolve_color
from deckforge.ooxml.customprops import write_custom_property
from deckforge.ooxml.walk import walk_shapes
from deckforge.ooxml.geometry import Box, Canvas
from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.walk import walk_shapes
from deckforge.plan.spec import BulletBlock, CardBlock, DeckSpec, KpiBlock, QuoteBlock, SlideSpec, TextBlock
from deckforge.plan.variants import Variant
from deckforge.settings import Settings
from deckforge.template.grid import ColumnAxis, Grid
from deckforge.template.naming import MIN_CONTRAST
from deckforge.template.patterns import Capacity, DecorShape, Pattern, PatternSlot, RepeatSpec
from deckforge.template.profile import LayoutEntryModel, TemplateProfile
from deckforge.workflow.versions import manifest as workflow_manifest

APP_YAML_PATH = Path(__file__).resolve().parents[3] / "config" / "app.yaml"
EMU_PER_INCH = 914400

# Task 15 (ТЗ п.2.4): на защите отвечает на вопрос "а этот файл каким кодом
# собран" — имя пользовательского свойства документа (`docProps/custom.xml`,
# см. `deckforge.ooxml.customprops`), в которое пишется реестр версий
# промптов/конфигов, собравших конкретный .pptx.
WORKFLOW_PROPERTY_NAME = "deckforge_workflow"

# `Variant` теперь определён один раз в `plan.variants` (Task 13) — `compose`
# уже зависит от `plan` (импортирует `plan.spec` для типов содержания), тот
# же однонаправленный порядок зависимостей, только для варианта вёрстки, а
# не для схемы данных. Реэкспорт (`from ... import Variant` выше) сохраняет
# `deckforge.compose.builder.Variant` рабочим для существующего кода/тестов,
# которые импортировали его именно отсюда (Task 9-10, до этой задачи) —
# `Variant.dense`/`Variant.visual` те же самые объекты, что и в `plan.
# variants.Variant`, `Variant.airy` — новый третий вариант оттуда же.


@dataclass(frozen=True)
class Fit:
    """Лезет ли содержание слайда в раскладку паттерна — используется на
    выборе паттерна (см. `_pick_pattern`) и остаётся доступным вызывающему
    коду (Task 10a: "ни одна не подходит — слайд уходит в песочницу").

    Правка по итогам визуального ревью (отчёт задачи, находка №1): раньше
    `overflow_ratio` считался на СОБСТВЕННОМ (native) кегле паттерна, без
    ужимания — наивная проверка отбраковывала раскладки, которые
    `_draw_slot` потом УСПЕШНО укладывал, ужав шрифт по шкале `TypeScale`.
    Теперь `overflow_ratio` — лучший (наименьший) результат по ВСЕЙ шкале
    ужимания вплоть до `caption` (то же самое, что реально попробует
    `_draw_slot`), 0.0 — есть кегль шкалы, на котором слот не переполнен
    нигде. `reason` — какой слот и почему (плюс `"exact"`/`"fallback"` про
    шрифт замера, см. `font_source`, — пригодится отчёту/аудиту, чтобы
    отличать надёжный overflow от посчитанного с запасом на неточный
    шрифт).

    `fill_ratio` — доля площади холста, которую реально займёт содержание
    в этой раскладке (сумма площадей слотов, в которые попал контент, как
    доля холста 0..1) — нужен подборщику, чтобы не брать формально
    влезающую, но почти пустую раскладку (ТЗ D05: "слайд заполнен меньше
    четверти или больше трёх четвертей — брак"; пороги `_FILL_RATIO_MIN`/
    `_FILL_RATIO_MAX` те же 0.25/0.75, что и в плане будущего
    `config/audit.yaml` — см. `docs/superpowers/plans/2026-09-21-
    deckforge.md`, ещё не заведён отдельным конфигом в границах этой
    задачи)."""

    ok: bool
    overflow_ratio: float
    reason: str
    fill_ratio: float = 0.0
    font_source: str = "exact"


class BuildError(RuntimeError):
    """Структурная невозможность собрать слайд — лейаут паттерна пропал из
    шаблона (не должно случаться на профиле, разобранном с того же файла,
    но честная ошибка лучше тихого `None`)."""


_ALIGN_MAP = {"l": PP_ALIGN.LEFT, "ctr": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT, "just": PP_ALIGN.JUSTIFY}

# Допуск "влезает" — доля дюйма, компенсирующая округление EMU↔дюйм и
# integer-округление кегля в пикселях при замере (`textfit.measure` округляет
# `size_pt` до целого пикселя) — то же значение, что у контракта T04-теста
# брифа (`+0.05`), но взято с запасом впятеро меньше него: наш собственный
# порог "влезает" обязан быть строже потребительской проверки (тот же
# принцип, что `patterns._within_margins` строже `test_slots_respect_
# template_margins` — иначе плавающая погрешность может протащить то, что
# здесь прошло, но не пройдёт там).
_FIT_TOLERANCE_IN = 0.01

# Ступени шкалы, по которым ужимается текст — БЕЗ "micro" (брифом: "не ниже
# подписи", т.е. "caption" — жёсткий пол).
_SHRINK_STEPS = ("display", "h1", "h2", "body", "caption")

# Роли, для которых используется ЗАГОЛОВОЧНЫЙ интерлиньяж/начертание
# шаблона, а не текстовый — тот же список смысла, что `TITLE_PH_TYPES`
# в template/grid.py, только по ролям слота, не по типу плейсхолдера
# (`PatternSlot` не несёт `ph_type`).
_HEADING_ROLES = frozenset({"headline", "subhead", "quote", "card_title", "kpi_value"})

_ANCHOR_MAP = {"t": MSO_ANCHOR.TOP, "ctr": MSO_ANCHOR.MIDDLE, "b": MSO_ANCHOR.BOTTOM}

_ROLE_COLOR = {
    "headline": "on_surface", "subhead": "muted", "body": "on_surface", "bullets": "on_surface",
    "card_title": "on_surface", "card_body": "on_surface", "kpi_value": "brand", "kpi_label": "muted",
    "quote": "on_surface", "quote_author": "muted", "source": "muted", "caption": "muted",
}


# ---------------------------------------------------------------------------
# Публичный интерфейс
# ---------------------------------------------------------------------------


def build_deck(
    spec: DeckSpec, profile: TemplateProfile, template_path: Path, variant: Variant,
    *, user_photos: dict[str, Path] | None = None, clone_examples: bool | None = None,
) -> Path:
    """`clone_examples`: собирать ли слайды клоном слайда-примера
    (`compose.clone`, см. `_try_clone`); `None`: как велит
    `compose.clone_examples` в `config/app.yaml`.

    `user_photos` (Task 20) — словарь `Visual.photo_name -> путь на диске`
    фотографий контент-пакета (`plan.photos.ContentPhoto`), собранный
    вызывающим кодом (`cli.py`, после `plan.photos.assign_photos`); `None`
    (запасное значение) — ни один слайд не несёт `photo_name`, поведение
    не отличается от того, что было до этой задачи (все существующие
    вызовы `build_deck` в тестах/`api/jobs.py` продолжают работать без
    правок). Дальше уходит в `place_slide` -> `_place_visual` -> `_place_
    picture_visual`, единственное место, которое реально читает файл с
    диска и вставляет его вместо ассета каталога шаблона."""
    register_template_fonts(template_path)
    with PptxPackage.open(template_path) as pkg:
        bullet_char = find_bullet_char(pkg)

    prs = Presentation(str(template_path))
    if clone_examples is None:
        clone_examples = _clone_examples_enabled()
    # Примеры запоминаются ДО очистки: после неё их нет в `prs.slides`, но
    # части пакета живут в памяти, и клон берёт фигуры и связи прямо из них.
    source_slides = sample_slides_by_number(prs) if clone_examples else {}
    _clear_sample_slides(prs)
    # Картинки декора берутся из САМОГО шаблона — один открытый zip на всю
    # презентацию, с памятью на уже прочитанные части: одна и та же иконка
    # встречается на нескольких слайдах колоды.
    image_cache: dict[str, bytes | None] = {}

    def image_bytes(part_name: str) -> bytes | None:
        if part_name not in image_cache:
            try:
                with zipfile.ZipFile(template_path) as zf:
                    image_cache[part_name] = zf.read(part_name)
            except Exception:  # noqa: BLE001 — нет части/битый zip: декор просто не рисуется
                image_cache[part_name] = None
        return image_cache[part_name]

    canvas = Canvas(width_emu=profile.canvas_width_emu, height_emu=profile.canvas_height_emu)
    audit_config = AuditConfig.load()
    patterns = [_pattern_from_model(m) for m in profile.patterns]
    # Task 18: история выбора раскладки растёт по ходу цикла — вход штрафа
    # за повтор (`_diversity_penalty`, см. докстроку `_SelectionHistory`).
    # Большинство слайдов приходят с уже проставленным `slide_spec.
    # pattern_id` (`plan.variants.apply_variant`, реальный выбор — см. её
    # докстроку), и `_resolve_pattern` ставит его первым БЕЗУСЛОВНО (штраф
    # здесь на такие слайды не действует вовсе, это ожидаемо — история нужна
    # для случаев, когда `pattern_id` не проставлен или не прошёл аудит
    # `_place_best_candidate` и цикл довыбирает раскладку сам, см. докстроку
    # `_resolve_pattern`).
    history = _SelectionHistory()
    for slide_spec in spec.slides:
        candidates = _resolve_pattern(slide_spec, patterns, profile, variant, history)
        if not candidates:
            slide_spec.findings.append(
                f"Слайд {slide_spec.index}: для kind={slide_spec.kind!r} не нашлось ни одного "
                "паттерна этого шаблона — слайд не собран."
            )
            continue
        pattern, notes = _place_best_candidate(
            prs, slide_spec, candidates, profile, canvas, audit_config,
            bullet_char=bullet_char, user_photos=user_photos, image_bytes=image_bytes,
            source_slides=source_slides,
        )
        slide_spec.findings.extend(notes)
        _write_speaker_notes(prs.slides[-1], slide_spec.speaker_notes)
        history = history.with_choice(pattern.pattern_id)

    out_path = _output_path(spec, template_path, variant)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fix_duplicate_partnames(prs)
    prs.save(str(out_path))
    write_custom_property(out_path, WORKFLOW_PROPERTY_NAME, workflow_manifest().as_property_value())
    return out_path


def _write_speaker_notes(slide, text: str | None) -> None:
    """Кладёт текст докладчика на страницу заметок слайда.

    Заказчик 23 сентября 2026 назвал ожидаемым результатом «готовые слайды
    и текст к каждому слайду»: на защите по слайдам ещё и рассказывают.
    Поле `SlideSpec.speaker_notes` модель заполняла и раньше (схема ответа
    `agents/slide-writer/AGENT.md`), но дальше `plan.spec` оно не шло —
    терялось на сборке молча.

    Пустое поле не трогает слайд вовсе: обращение к `slide.notes_slide`
    СОЗДАЁТ страницу заметок (а при её отсутствии в шаблоне — ещё и мастер
    заметок), поэтому безусловный вызов приделал бы пустой лист заметок
    каждому слайду колоды, которого в исходном шаблоне не было."""
    if not text or not text.strip():
        return
    slide.notes_slide.notes_text_frame.text = text.strip()


def place_slide(
    prs, slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile, audit_config: AuditConfig,
    *, bullet_char: str = "•", user_photos: dict[str, Path] | None = None,
    image_bytes: Callable[[str], bytes | None] | None = None,
) -> None:
    layout = _find_layout(prs, pattern.layout_id)
    if layout is None:
        raise BuildError(f"лейаут {pattern.layout_id!r} не найден в открытом шаблоне")
    slide = prs.slides.add_slide(layout)

    canvas_width_emu = profile.canvas_width_emu
    canvas_height_emu = profile.canvas_height_emu
    grid = _grid_from_model(profile.grid)

    # Декор группы повтора (плашки карточек и т.п.) разворачивается под
    # фактическое число элементов ВМЕСТЕ с текстовыми слотами — правка по
    # итогам повторного визуального ревью (отчёт задачи, находка №1,
    # "главная находка"): раньше `pattern.decor` переносился статически,
    # независимо от того, сколько карточек реально легло на слайд, и
    # раскладка на шесть карточек под два элемента содержания оставляла
    # четыре пустые рамки. Декор вне группы повтора (`expand_decor` не
    # трогает `repeat_group=False`) переносится как и раньше.
    #
    # Укладка считается ДО декора (а не в цикле ниже, как было раньше),
    # потому что теперь она — вход для декора: плашка группы повтора
    # рисуется, только если в слот её единицы реально лёг текст
    # (`filled_repeat_units`). Находка ручной проверки: слайду с одним
    # заголовком досталась раскладка на три карточки, и три пустые белые
    # плашки 4×4 дюйма заняли больше половины слайда.
    contents, drops = assign_content_with_drops(slide_spec, pattern, grid)
    _note_drops(slide_spec, pattern, drops)
    decor = expand_decor(
        pattern, _repeat_item_count(slide_spec), grid, filled_repeat_units(pattern, contents),
    )
    apply_decor(slide, decor, canvas_width_emu, canvas_height_emu, image_bytes)
    # `_local_background_is_dark` ищет охватывающую плашку декора ПОД
    # слотом (см. её докстроку) — обязана видеть УЖЕ развёрнутые позиции
    # плашек (`decor`, не статический `pattern.decor`), иначе контраст
    # карточки №2 может посчитаться от плашки, стоявшей там на
    # исходном, ненамайненном слайде-примере.
    effective_pattern = pattern if decor is pattern.decor else replace(pattern, decor=decor)

    # Источник истины для фона под слотом — МАКЕТ, на который слайд реально
    # ставится (`profile.layouts`, разобран надёжно: наследование от
    # мастера, градиенты, фоновые фото — Task 6/8), а НЕ `pattern.is_dark`
    # (Task 10 отчёт, находка №1: раскладка, снятая со светлого
    # слайда-примера, но положенная на тёмный макет, несла `is_dark=False`
    # от примера — чёрный текст ложился на тёмно-фиолетовый фон макета,
    # нечитаемо). `layout_bg_luminance` — `None`, если макет пропал из
    # каталога (не должно случаться на профиле, разобранном с того же
    # файла) или у него самого не резолвился фон — тогда
    # `_local_background_luminance` честно падает на `pattern.is_dark` как
    # на последний осмысленный сигнал.
    layout_entry = _layout_entry(profile, pattern.layout_id)
    layout_bg_luminance = layout_entry.background.luminance if layout_entry is not None else None

    family = _primary_family(profile)
    for content in contents:
        # Текст-заглушка — та же проверка, что и аудит I02 (`audit.
        # deterministic._check_I02`, тот же `audit_config.integrity.
        # placeholder_patterns`), но здесь она стоит ДО отрисовки, не после
        # (Task 22, отчёт задачи: находка "текст-рыба шаблона утекает на
        # слайды" — контрольный ЛЦТ2026, первый слайд, заголовок «Титульный
        # слайд презентации»). Разбор задачи 22 показал, что этот конкретный
        # текст пришёл не из `PatternSlot.sample_text` раскладки (у
        # реально выбранного паттерна этот слот — `sample_text=None`,
        # слайд-пример шаблона был пуст), а из содержания, написанного
        # моделью, — но принцип "не клади на холст то, что похоже на
        # заглушку" не обязан знать, ОТКУДА взялся такой текст: что бы ни
        # прислало содержание для этого слота (сама модель, будущий
        # фолбэк на `sample_text`, ручной ввод), заглушечный текст сюда не
        # попадает вовсе — слот остаётся незаполненным (`_remove_empty_
        # placeholders` уберёт унаследованный плейсхолдер лейаута, тем же
        # путём, что и для контента, которого не пришло вовсе — см.
        # докстроку `blocks.assign_content`), а не заглушкой на слайде,
        # которую потом отдельно ловит только пост-фактум аудит.
        placeholder_hit = _placeholder_text_hit(content, audit_config)
        if placeholder_hit is not None:
            slide_spec.findings.append(
                f"Слайд {slide_spec.index}: текст слота «{content.role_hint}» похож на "
                f"текст-заглушку шаблона («{placeholder_hit}») — не отрисован, слот оставлен "
                "пустым."
            )
            continue
        # Наложение — не занято ли место декором раскладки (Task 10 отчёт,
        # находка №2: заголовок налез на плашку декора) — проверяется и
        # чинится (сдвигом) ДО замера/отрисовки, слот замеряется уже по
        # исправленному боксу.
        box = _avoid_decor_overlap(content.slot.box, effective_pattern.decor, grid)
        if box is not content.slot.box:
            content = replace(content, slot=replace(content.slot, box=box))
        # Контраст — от фона НЕПОСРЕДСТВЕННО под этим слотом (плашка декора,
        # если слот на ней стоит, иначе фон макета) — см.
        # `_local_background_luminance`.
        _draw_slot(
            slide, slide_spec, content, profile, family, bullet_char, canvas_width_emu, canvas_height_emu,
            _local_background_luminance(content.slot.box, effective_pattern, layout_bg_luminance),
        )

    _place_visual(slide, slide_spec, pattern, profile, user_photos, sole_content=_only_frame_text(contents))
    _remove_empty_placeholders(slide)


def _only_frame_text(contents: list[SlotContent]) -> bool:
    """На слайде нет текста содержания, только заголовок, подзаголовок и
    источник: визуал тогда единственный блок и может занять всю ширину."""
    return not any(c.role_hint not in _CLONE_FRAME_ROLES for c in contents)


def _note_drops(slide_spec: SlideSpec, pattern: Pattern, drops) -> None:
    """Содержание, которому в этой раскладке не нашлось слота, на слайд
    не попадает — это не ошибка сборки (слайд собирается), но и не
    повод молчать: расхождение между планом и файлом обязана назвать
    наша же проверка, а не глаз человека (см. докстроку
    `blocks.DroppedContent`). Общая для обоих путей сборки: клон теряет
    содержание по тем же ролям, что и сборка с нуля."""
    for drop in drops:
        slide_spec.findings.append(
            f"Слайд {slide_spec.index}: {DROPPED_ROLE_TITLES.get(drop.role, drop.role)} не попал "
            f"на слайд — в раскладке {pattern.pattern_id!r} нет слота под эту роль: "
            f"«{drop.text}»."
        )


def _placeholder_text_hit(content: SlotContent, audit_config: AuditConfig) -> str | None:
    """Совпал ли текст, который вот-вот ляжет в этот слот, с одним из
    маркеров текста-заглушки (`audit_config.integrity.placeholder_patterns`,
    `config/audit.yaml`) — регистронезависимое вхождение подстроки, ТА ЖЕ
    проверка, что `audit.deterministic._check_I02` делает постфактум по
    уже собранному файлу (намеренно тот же конфиг, не отдельный список —
    два места, где "похоже на заглушку" может разойтись по критерию, хуже
    одного). Возвращает найденный маркер (для текста находки) или `None`,
    если текст чистый.

    Проверяется ВЕСЬ склеенный текст слота (все параграфы, не только
    первый) — многострочный буллет-список, где заглушкой оказался только
    один пункт из трёх, всё равно достаточно испорчен, чтобы не класть его
    на слайд как есть (тот же принцип "решает код, не гадание по одному
    параграфу")."""
    text = " ".join(p.text for p in content.paragraphs).strip().lower()
    if not text:
        return None
    return next((p for p in audit_config.integrity.placeholder_patterns if p.lower() in text), None)


# ---------------------------------------------------------------------------
# Плейсхолдеры макета, унаследованные слайдом (Task 13, критичный дефект
# отчёта задачи, находка №1) — `prs.slides.add_slide(layout)` копирует на
# слайд ВСЕ плейсхолдеры лейаута (python-pptx, `SlideShapes.clone_layout_
# placeholders`), а этот модуль ни разу не пишет содержание НЕПОСРЕДСТВЕННО
# в плейсхолдер: `_draw_slot` всегда добавляет отдельный `add_textbox` по
# координатам слота (см. её комментарий про `tf.margin_*`), `_place_
# picture_visual`/`compose.charts.add_chart`/`compose.tables.add_table` —
# отдельные фигуры поверх. Незаполненный плейсхолдер в LibreOffice рисуется
# пустотой, в PowerPoint — видимой надписью "Щелкните, чтобы добавить
# текст"/"чтобы добавить рисунок" — на защите это видно на каждом слайде
# богатого макета (живой разбор ЛЦТ2026: слайд 4 макета "Содержание_1",
# девятнадцать пустых плейсхолдеров плюс четыре пустые карточные плашки
# декора — найдено координатором на живом рендере собранной колоды).
# ---------------------------------------------------------------------------

# Плейсхолдеры, которые PowerPoint заполняет САМ во время показа (номер
# слайда, дата, нижний колонтитул, шапка) — пустые в разметке файла, но
# осмысленные, брифом задачи явно требует их не трогать. python-pptx
# `clone_layout_placeholders` их и так не клонирует на слайд вовсе (её
# докстрока: "Latent placeholders (date, slide number, and footer) are not
# cloned") — проверка по типу здесь защитная, на случай будущей версии
# библиотеки или лейаута, который несёт такой плейсхолдер не-латентно.
_AUTO_FILLED_PLACEHOLDER_TYPES = frozenset({
    PP_PLACEHOLDER.SLIDE_NUMBER, PP_PLACEHOLDER.DATE, PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.HEADER,
})


def _placeholder_is_empty(shape) -> bool:
    """Плейсхолдер не несёт ни текста, ни картинки. Этот модуль никогда не
    пишет НИ ТО, НИ ДРУГОЕ прямо в унаследованный от лейаута плейсхолдер
    (см. докстроку раздела) — проверка на текст/картинку, а не безусловное
    удаление, оставлена честно: если однажды появится путь, кладущий
    контент прямо в плейсхолдер, эта функция не снесёт его."""
    if shape.has_text_frame and shape.text_frame.text.strip():
        return False
    if shape._element.findall(".//" + qn("a:blip")):
        return False
    return True


def _remove_empty_placeholders(slide) -> None:
    """Убирает из XML слайда плейсхолдеры лейаута, оставшиеся незаполненными
    после укладки содержания (см. докстроку раздела выше) — вызывается В
    КОНЦЕ `place_slide`, когда весь текст/визуал этого слайда уже
    отрисован, чтобы не удалить плейсхолдер раньше, чем стало известно,
    что он не понадобился."""
    for shape in list(slide.placeholders):
        if shape.placeholder_format.type in _AUTO_FILLED_PLACEHOLDER_TYPES:
            continue
        if _placeholder_is_empty(shape):
            shape._element.getparent().remove(shape._element)


# ---------------------------------------------------------------------------
# Визуал слайда (Task 13) — таблица/график/фото/иконка. `plan.spec.Visual`
# несёт только СОДЕРЖАНИЕ (числа таблицы, ряды графика, "какой ассет по
# смыслу"), КАК это лечь на холст — решает этот модуль, тем же принципом,
# что и текстовые слоты (`_draw_slot`): единственное место, трогающее и
# `TemplateProfile`, и python-pptx одновременно для визуала, — здесь, не в
# `plan/` и не в `compose.charts`/`compose.tables` (те двое уже готовы,
# Task 9-10, и НЕ знают о `plan.spec.Visual` вовсе — принимают свои
# собственные `ChartSpec`/`TableSpec`, этот модуль их строит).
# ---------------------------------------------------------------------------


def _visual_slot(pattern: Pattern, role: str) -> PatternSlot | None:
    """Самый ёмкий (по площади) слот `pattern` этой роли — тот же приём,
    что `blocks._slots_by_role` использует для текстовых слотов (см. её
    докстроку про Task 9 повторное ревью, находка №2): визуал кладётся в
    слот, который реально вмещает контент, а не в первый попавшийся."""
    candidates = [s for s in pattern.slots if s.role == role]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.box.width * s.box.height)


def _place_visual(
    slide, slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile,
    user_photos: dict[str, Path] | None = None, *, sole_content: bool = False,
) -> None:
    visual = slide_spec.visual
    if visual is None:
        return

    if visual.kind == "table" and visual.table is not None:
        _place_table_visual(slide, slide_spec, pattern, profile, visual.table, sole_content=sole_content)
    elif visual.kind == "chart" and visual.chart is not None:
        _place_chart_visual(slide, slide_spec, pattern, profile, visual.chart)
    elif visual.kind in ("photo", "icon"):
        _place_picture_visual(slide, slide_spec, pattern, profile, visual.kind, user_photos)


def _place_table_visual(
    slide, slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile, table, *, sole_content: bool = False,
) -> None:
    slot = _visual_slot(pattern, "table")
    if slot is None:
        slide_spec.findings.append(
            f"Слайд {slide_spec.index}: в раскладке {pattern.pattern_id!r} нет слота под таблицу "
            "— TableVisual не отрисован."
        )
        return
    if not table.rows:
        return

    header, body_rows = _table_rows_within_capacity(slide_spec, pattern, table, sole_content)
    align = _column_align(header, body_rows)
    canvas = Canvas(width_emu=profile.canvas_width_emu, height_emu=profile.canvas_height_emu)
    box, floor = slot.box, None
    if sole_content:
        box = _table_frame_box(slide, slot.box, profile, canvas)
        floor = profile.type_scale_pt("caption", 12.0)
    add_table(slide, box, TableSpec(header=header, rows=body_rows, align=align), profile, floor_pt=floor)


def _column_align(header: list[str], body_rows: list[list[str]]) -> list[str]:
    """Числовой столбец по правому краю, текстовый по левому. Судит тело:
    шапка числового столбца почти всегда слово («До», «После»), и с ней
    ни один столбец не выходил числовым."""
    align = []
    for i in range(len(header)):
        cells = [r[i] for r in body_rows if i < len(r) and r[i].strip()] or [header[i]]
        align.append("r" if all(_looks_numeric(c) for c in cells) else "l")
    return align


def _table_rows_within_capacity(
    slide_spec: SlideSpec, pattern: Pattern, table, sole_content: bool,
) -> tuple[list[str], list[list[str]]]:
    """Шапка и строки тела, усечённые до вместимости раскладки. Столбцы
    таблицы-единственного-блока не усекаются: вместимость по столбцам снята
    с узкой таблицы примера, а наша растянута на ширину слайда (п. 2
    задачи о родных таблицах), и срезать из-за неё «Изменение» значило бы
    потерять главный столбец."""
    header, body_rows = list(table.rows[0]), [list(r) for r in table.rows[1:]]
    cap = pattern.capacity
    truncated = False
    if cap.max_cols and len(header) > cap.max_cols and not sole_content:
        header = header[: cap.max_cols]
        body_rows = [row[: cap.max_cols] for row in body_rows]
        truncated = True
    max_body_rows = max(cap.max_rows - 1, 1) if cap.max_rows else len(body_rows)
    if len(body_rows) > max_body_rows:
        body_rows = body_rows[:max_body_rows]
        truncated = True
    if truncated:
        slide_spec.findings.append(
            f"Слайд {slide_spec.index}: таблица усечена до вместимости раскладки "
            f"({pattern.pattern_id!r}, max_rows={cap.max_rows}, max_cols={cap.max_cols})."
        )
    return header, body_rows


# Таблица-единственный блок слайда не уже стольких долей холста: узкая
# таблица рвёт слова в ячейках по буквам.
_TABLE_MIN_WIDTH = 0.6
# Зазор между рамкой таблицы и графикой или текстом рядом, доли холста.
_TABLE_GAP = 0.02
# Фигура крупнее стольких долей холста по обеим осям: фон, а не соседка.
_BACKGROUND_SHARE = 0.9
_AUTO_PH_TYPES = frozenset({"sldNum", "dt", "ftr", "hdr"})


def _table_frame_box(
    slide, slot_box: Box, profile: TemplateProfile, canvas: Canvas, *, exclude: list | tuple = (),
    min_width: float = _TABLE_MIN_WIDTH, max_right: float | None = None,
) -> Box:
    """Рамка таблицы: от левого края слота до правого поля сетки (не уже
    `min_width` холста, для этого левый край при нужде сдвигается влево) и
    от верха слота до нижнего поля. Узкая таблица на четверть слайда
    (прогон 26 сентября 2026) получалась из рамки примера, записанной
    Google Slides как 3 000 000 EMU при таблице втрое шире.

    Рамка не заходит на соседей: картинки и фигуры слайда и его лейаута
    (та же логика, что у сужения текстовой рамки клона от графики) и
    текст самого слайда. Соседка режет рамку с той стороны, где потеря
    площади меньше: справа (графика сбоку) или снизу (источник под
    таблицей). Если после этого рамка уже слота, остаётся слот."""
    grid = profile.grid
    right = 1 - grid.margin_right
    if max_right is not None:
        right = min(right, max_right)
    bottom = 1 - grid.margin_bottom
    top = slot_box.top
    exclude_ids = {id(e) for e in exclude}
    obstacles = _table_obstacles(slide, canvas, exclude_ids)
    # Левый край: от поля сетки, если слева от слота на высоте таблицы
    # ничего нет. Слот справа от текстовой колонки примера (VK Education
    # slide38: список слева, таблица справа), у которого колонка осталась
    # пустой и удалена, иначе давал таблицу с середины слайда и пустую
    # левую половину (прогон 26 сентября 2026, слайд 7).
    left = grid.margin_left
    for other in obstacles:
        ob = other.box
        if ob.right <= slot_box.left + 0.01 and ob.bottom > top and ob.top < bottom \
                and not (ob.width >= _BACKGROUND_SHARE and ob.height >= _BACKGROUND_SHARE):
            left = max(left, ob.right + _TABLE_GAP)
    left = min(left, slot_box.left)
    for other in obstacles:
        ob = other.box
        if ob.right <= left or ob.left >= right or ob.bottom <= top or ob.top >= bottom:
            continue
        if ob.width >= _BACKGROUND_SHARE and ob.height >= _BACKGROUND_SHARE:
            continue
        if _contains(ob, Box(left=left, top=top, width=right - left, height=bottom - top)):
            continue
        cut_right = (right - (ob.left - _TABLE_GAP)) * (bottom - top) if ob.left > left else float("inf")
        cut_bottom = (bottom - (ob.top - _TABLE_GAP)) * (right - left) if ob.top > top else float("inf")
        if cut_right == cut_bottom == float("inf"):
            continue
        if cut_right <= cut_bottom:
            right = ob.left - _TABLE_GAP
        else:
            bottom = ob.top - _TABLE_GAP
    if right - left < slot_box.width or bottom - top <= 0:
        return slot_box
    return Box(left=left, top=top, width=right - left, height=bottom - top)


def _table_obstacles(slide, canvas: Canvas, exclude_ids: set[int]) -> list:
    """Соседи таблицы: всё видимое на самом слайде (текст, картинки,
    фигуры), кроме самозаполняемых плейсхолдеров, и графика лейаута
    (картинки и фигуры без текста; плейсхолдеры лейаута на слайде не
    видны). Лейаут обязателен: графика VK Education часто лежит именно
    там."""
    own = [
        r for r in slide_refs(slide, canvas)
        if r.box is not None and id(r.element) not in exclude_ids
        and not (r.is_placeholder and r.ph_type in _AUTO_PH_TYPES)
        and (r.kind in ("picture", "graphic_frame") or shape_text(r.element).strip() or not r.is_placeholder)
    ]
    layout = [
        r for r in walk_shapes(slide.slide_layout._element, canvas)  # noqa: SLF001
        if r.box is not None and not r.is_placeholder
        and (r.kind == "picture" or (r.kind == "shape" and not shape_text(r.element).strip()))
    ]
    return own + layout


# Знак: и ASCII-дефис, и типографский минус «−» (U+2212), которым модель
# пишет изменения; хвост единиц может нести точки и пробелы («п.п.»).
_NUMERIC_CELL_RE = re.compile(r"^[+\-−±]?[\d\s.,%]+[a-zа-яё%.\s]*$", re.IGNORECASE)


def _looks_numeric(cell: str) -> bool:
    return bool(_NUMERIC_CELL_RE.match(cell.strip())) if cell and cell.strip() else False


def _place_chart_visual(slide, slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile, chart) -> None:
    slot = _visual_slot(pattern, "chart") or _visual_slot(pattern, "table") or _visual_slot(pattern, "image")
    if slot is None:
        slide_spec.findings.append(
            f"Слайд {slide_spec.index}: в раскладке {pattern.pattern_id!r} нет слота под график "
            "— ChartVisual не отрисован."
        )
        return
    if not chart.series:
        return

    cap = pattern.capacity
    series = chart.series
    if cap.max_series and len(series) > cap.max_series:
        series = series[: cap.max_series]
        slide_spec.findings.append(
            f"Слайд {slide_spec.index}: график усечён до {cap.max_series} рядов по вместимости "
            f"раскладки {pattern.pattern_id!r}."
        )

    # I05 (аудит): подписи осей/единиц обязательны для немаркерных типов —
    # чем бы ни ответила модель (или её не было вовсе), ось не должна
    # остаться безымянной, тот же принцип "код не доверяет слепо", что и у
    # `_color_for_role`/`_best_contrast_color` выше в этом файле.
    axis_titles = chart.axis_titles
    if not axis_titles or not axis_titles[0] or not axis_titles[1]:
        axis_titles = ("Категория", chart.unit or "Значение")

    spec = ChartSpec(
        kind=chart.kind, categories=list(chart.categories),
        series=[Series(name=s.name, values=list(s.values)) for s in series],
        unit=chart.unit, highlight_index=chart.highlight_index, axis_titles=axis_titles,
    )
    try:
        add_chart(slide, slot.box, spec, profile)
    except ValueError as exc:
        slide_spec.findings.append(f"Слайд {slide_spec.index}: график не построен ({exc}).")


def _contain_box(
    left: int, top: int, width: int, height: int, native_width: float, native_height: float,
) -> tuple[int, int, int, int]:
    """"Contain", не растяжение на весь слот (L07 аудита следит именно за
    отклонением placed_aspect/native_aspect) — картинка вписывается в слот
    целиком по большей стороне и центрируется по меньшей. Общая для
    ассетов каталога шаблона и пользовательских фотографий (Task 20) —
    один и тот же механизм вписывания, разное происхождение байтов."""
    native_aspect = native_width / native_height
    slot_aspect = width / height if height else native_aspect
    if native_aspect > slot_aspect:
        pic_width = width
        pic_height = round(width / native_aspect)
        pic_top = top + (height - pic_height) // 2
        pic_left = left
    else:
        pic_height = height
        pic_width = round(height * native_aspect)
        pic_left = left + (width - pic_width) // 2
        pic_top = top
    return pic_left, pic_top, pic_width, pic_height


def _place_picture_visual(
    slide, slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile, kind: str,
    user_photos: dict[str, Path] | None = None,
) -> None:
    slot = (
        _visual_slot(pattern, "image") if kind == "photo" else _visual_slot(pattern, "icon")
    ) or _visual_slot(pattern, "image") or _visual_slot(pattern, "icon")

    # Task 20: пользовательская фотография контент-пакета — вместо ассета
    # каталога шаблона, а не вдобавок к нему. `photo_name` заполняет
    # `plan.photos.assign_photos`, только когда решила, что ЭТОТ слайд
    # получит ИМЕННО эту фотографию (см. её докстроку) — если так, отсюда
    # и до конца функции обрабатывается ТОЛЬКО она; на каталог шаблона
    # код падает единственный раз, когда `photo_name` не проставлен вовсе
    # (обычный путь до этой задачи, ассет ШАБЛОНА — коллаж/иконка/лого).
    photo_name = slide_spec.visual.photo_name if slide_spec.visual is not None else None
    user_photo_path = (user_photos or {}).get(photo_name) if photo_name else None

    if slot is None:
        if user_photo_path is not None:
            # Раскладка под фото не нашлась (бриф задачи, п.3: "либо
            # выбирается раскладка, где такой слот есть, либо фотография
            # не ставится — но об этом надо сказать находкой") — в отличие
            # от ассета ШАБЛОНА (декоративная картинка, отсутствие слота
            # молча ожидаемо на бедной раскладке), пользователь принёс
            # ЭТУ фотографию специально, и её потеря должна быть видна.
            slide_spec.findings.append(
                f"Слайд {slide_spec.index}: в раскладке {pattern.pattern_id!r} нет слота под фото/"
                f"иконку — пользовательская фотография {photo_name!r} не вставлена."
            )
        return  # раскладка не несёт визуального слота вовсе — для ассета шаблона это не находка

    left, top, width, height = _emu_visual_box(slot.box, profile)

    if user_photo_path is not None:
        try:
            data = user_photo_path.read_bytes()
            with Image.open(io.BytesIO(data)) as img:
                native_width, native_height = img.size
        except Exception as exc:
            slide_spec.findings.append(
                f"Слайд {slide_spec.index}: пользовательская фотография {photo_name!r} "
                f"({user_photo_path}) не читается ({exc}) — не вставлена."
            )
            return
        pic_left, pic_top, pic_width, pic_height = (
            _contain_box(left, top, width, height, native_width, native_height)
            if native_width and native_height else (left, top, width, height)
        )
        slide.shapes.add_picture(
            io.BytesIO(data), Emu(pic_left), Emu(pic_top), Emu(max(1, pic_width)), Emu(max(1, pic_height)),
        )
        return

    catalog = list(profile.assets.photos if kind == "photo" else profile.assets.icons)
    if not catalog:
        catalog = list(profile.assets.photos) + list(profile.assets.icons)
    if not catalog or not profile.source_path:
        return  # у шаблона нет своих фото/иконок (или профиль без source_path) — честно ничего не подставляем

    asset = max(catalog, key=lambda a: a.confidence)

    try:
        with PptxPackage.open(Path(profile.source_path)) as pkg:
            data = pkg.part(asset.part_name)
    except Exception:
        return

    pic_left, pic_top, pic_width, pic_height = left, top, width, height
    if asset.width and asset.height:
        pic_left, pic_top, pic_width, pic_height = _contain_box(left, top, width, height, asset.width, asset.height)

    slide.shapes.add_picture(
        io.BytesIO(data), Emu(pic_left), Emu(pic_top), Emu(max(1, pic_width)), Emu(max(1, pic_height)),
    )


def count_embedded_photos(pptx_path: Path, user_photos: dict[str, Path]) -> int:
    """Сколько ПОЛЬЗОВАТЕЛЬСКИХ фотографий контент-пакета реально легли на
    слайды УЖЕ СОБРАННОГО `.pptx` — Task 22, отчёт задачи, находка "пайплайн
    рапортует не то, что в файле": `plan.photos.assign_photos` честно
    называет, сколько фотографий она РАСПРЕДЕЛИЛА по слайдам (`plan.spec.
    Visual.photo_name` проставлен), но раскладка, которую слайду в итоге
    выбрал `apply_variant`/`_place_best_candidate`, может не нести слота
    под картинку вовсе — тогда `_place_picture_visual` честно пишет находку
    в `slide_spec.findings` и не вставляет байты (см. её докстроку), а
    "распределено" и "вставлено" расходятся. Пользователь, читающий только
    итоговое число, видит план модели, а не то, что физически есть в файле,
    — этот счётчик отвечает на вопрос "а что физически в файле" по самому
    файлу, а не по плану.

    Сравнение по СОДЕРЖИМОМУ (sha256), не по имени — `python-pptx`
    переименовывает media-файлы при вставке (`ppt/media/imageN.ext`), имя
    файла контент-пакета внутри архива не сохраняется (тот же приём и то
    же обоснование, что `scripts/build_submission.py::_count_embedded_
    photos` использовал до переезда сюда — общее место для `cli.py` и
    submission-скрипта, а не два независимых куска одной и той же
    логики)."""
    if not user_photos:
        return 0
    wanted = set()
    for path in user_photos.values():
        try:
            wanted.add(hashlib.sha256(Path(path).read_bytes()).hexdigest())
        except OSError:
            continue
    if not wanted:
        return 0
    found = set()
    with zipfile.ZipFile(pptx_path) as zf:
        for name in zf.namelist():
            if not name.startswith("ppt/media/"):
                continue
            digest = hashlib.sha256(zf.read(name)).hexdigest()
            if digest in wanted:
                found.add(digest)
    return len(found)


def _emu_visual_box(box: Box, profile: TemplateProfile) -> tuple[int, int, int, int]:
    return (
        round(box.left * profile.canvas_width_emu), round(box.top * profile.canvas_height_emu),
        round(box.width * profile.canvas_width_emu), round(box.height * profile.canvas_height_emu),
    )


def fits(slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile) -> Fit:
    """Лезет ли содержание `slide_spec` в `pattern` — более лёгкая проверка,
    чем реальная укладка (`place_slide`), для выбора паттерна ДО того, как
    тратить время на построение слайда.

    Шкала ужимания та же, что реально попробует `_draw_slot`
    (`_shrink_sequence`) — иначе (см. докстроку `Fit`) эта проверка
    отбраковывала бы раскладки, которые сборка успешно укладывает ужатым
    шрифтом."""
    grid = _grid_from_model(profile.grid)
    assignments = assign_content(slide_spec, pattern, grid)
    canvas_width_in = profile.canvas_width_emu / EMU_PER_INCH
    canvas_height_in = profile.canvas_height_emu / EMU_PER_INCH
    family = _primary_family(profile)

    worst_ratio = 0.0
    worst_reason = ""
    font_source = "exact"
    canvas_area_in2 = canvas_width_in * canvas_height_in
    covered_area = 0.0
    for content in assignments:
        text = _joined_text(content.paragraphs)
        if not text.strip():
            continue
        box_width_in = content.slot.box.width * canvas_width_in
        box_height_in = content.slot.box.height * canvas_height_in
        if box_height_in <= 0:
            continue
        line_spacing = _line_spacing_for(content.role_hint, profile)
        slot_ratio = None
        best_height_in = box_height_in
        for size_pt in _shrink_sequence(profile, content.slot.size_pt):
            metrics = measure(text, family, size_pt, box_width_in, line_spacing=line_spacing)
            if metrics.font_source == "fallback":
                font_source = "fallback"
            ratio = metrics.height_in / box_height_in - 1.0
            if slot_ratio is None or ratio < slot_ratio:
                slot_ratio, best_height_in = ratio, metrics.height_in
            if slot_ratio <= 0.0:
                break  # нашли кегль шкалы, на котором слот не переполнен — дальше мельчить незачем
        slot_ratio = max(slot_ratio or 0.0, 0.0)
        if slot_ratio > worst_ratio:
            worst_ratio = slot_ratio
            worst_reason = (
                f"слот «{content.role_hint}» переполнен на {slot_ratio:.0%} даже на минимальном "
                "кегле шкалы (caption)"
            )
        # Доля холста, реально занятая ЧЕРНИЛАМИ этого слота — по факту
        # нарисованного текста (высота на выбранном кегле, отсечённая рамкой
        # слота), НЕ по площади самого слота. Находка визуального ревью
        # (отчёт задачи, №3, "слайд заполнен меньше четверти"): слот
        # `bullets`/`body` часто высокий по замыслу раскладки, но текст
        # начинается сверху и не растягивается на всю высоту (`_draw_slot`
        # не центрирует и не растягивает по вертикали) — если считать по
        # площади СЛОТА, а не по факту нарисованного текста, подборщик не
        # видит, что три коротких буллета оставляют низ слайда пустым.
        ink_height_in = min(best_height_in, box_height_in)
        if canvas_area_in2 > 0:
            covered_area += (ink_height_in * box_width_in) / canvas_area_in2

    missing = _missing_signals(slide_spec, assignments)
    if missing:
        worst_ratio = max(worst_ratio, 1.0)
        worst_reason = f"нет слота под роль(и): {', '.join(missing)}"

    ok = worst_ratio <= 0.0
    return Fit(
        ok=ok, overflow_ratio=max(worst_ratio, 0.0), reason=worst_reason or "содержание помещается",
        fill_ratio=covered_area, font_source=font_source,
    )


# Пороги "не слишком пусто / не слишком плотно" — те же числа, что ТЗ
# отводит будущей проверке аудита D05 (`fill_ratio_min`/`fill_ratio_max`,
# см. докстроку `Fit`) — подбор паттерна здесь пользуется теми же порогами,
# чтобы не выбирать раскладку, которую аудит потом всё равно забракует.
_FILL_RATIO_MIN = 0.25
_FILL_RATIO_MAX = 0.75


def _repeat_item_count(slide_spec: SlideSpec) -> int | None:
    """Число элементов, которые РЕАЛЬНО развернут `pattern.repeat` на этом
    слайде — сегодня это только `CardBlock.items` (единственный блок,
    зовущий `expand_repeat`/`expand_decor`, см. `_assign_cards` в
    `blocks.py`; `KpiBlock` раздаёт KPI-слоты напрямую, без пересчёта
    геометрии повтора). `None`, если на слайде нет такого блока — ни
    подбору паттерна (`_capacity_badness`), ни развороту декора
    (`place_slide`) не с чем сверять вместимость раскладки."""
    for block in slide_spec.blocks:
        if isinstance(block, CardBlock) and block.items:
            return len(block.items)
    return None


# Вес расхождения вместимости раскладки при ранжировании кандидатов —
# см. `_capacity_badness`: доля (не абсолютное число) намайненной
# `Capacity.max_items`, на которую она разошлась с фактическим объёмом
# содержания. Обоснование доли, а не абсолютной разницы (постановщик,
# "порог обоснуй долей, а не наблюдением за файлами"): сама ёмкость
# раскладки — величина шаблон-специфичная (3 карточки у одного шаблона,
# 8 у другого), абсолютная "на 4 карточки больше" ничего не говорит без
# знания масштаба самой раскладки, а доля от max_items сразу отвечает на
# вопрос "во сколько раз раскладка избыточна/недостаточна" одинаково на
# любом шаблоне: раскладка на 6 под 2 элемента даёт 4/6 ≈ 0.67 — почти
# такая же избыточность, что и раскладка на 3 под 1 элемент (2/3 ≈ 0.67),
# хотя абсолютная разница у них разная (4 против 2).
def _capacity_badness(pattern: Pattern, slide_spec: SlideSpec) -> float:
    """0.0, когда `Capacity.max_items` раскладки совпадает с фактическим
    числом элементов содержания слайда (или когда сравнивать не с чем —
    не card-слайд, см. `_repeat_item_count`), иначе — относительная доля
    расхождения. Используется только как ранжирующий сигнал `_pick_pattern`
    (не жёсткий отказ: раскладка с "неидеальной" вместимостью всё ещё
    может быть единственным кандидатом вида `kind` — лучше неидеальный
    выбор, чем никакого, тот же принцип, что и `_fill_badness`).

    Находка ревью ("главная находка"): раскладка, рассчитанная на шесть
    элементов, под два элемента содержания — плохой выбор, ДАЖЕ ЕСЛИ текст
    формально влезает (`fit.ok`) и не выглядит пустым по `fill_ratio`
    (`fill_ratio` меряет ЧЕРНИЛА фактически положенного текста, а не число
    пустых декоративных рамок вокруг него — это разные сигналы, `Capacity.
    max_items` нужен независимо)."""
    n = _repeat_item_count(slide_spec)
    if n is None or pattern.capacity.max_items <= 0:
        return 0.0
    return abs(pattern.capacity.max_items - n) / pattern.capacity.max_items


def _fill_badness(fill_ratio: float) -> float:
    """0.0, когда `fill_ratio` укладывается в [_FILL_RATIO_MIN,
    _FILL_RATIO_MAX], иначе — на сколько (в долях холста) он вышел за
    границу; используется только как ранжирующий сигнал `_pick_pattern`
    (не как жёсткий отказ — жёсткий отказ "слишком пусто/плотно" по всей
    колоде целиком, не по одному кандидату, работа будущего аудита D05)."""
    if fill_ratio < _FILL_RATIO_MIN:
        return _FILL_RATIO_MIN - fill_ratio
    if fill_ratio > _FILL_RATIO_MAX:
        return fill_ratio - _FILL_RATIO_MAX
    return 0.0


# ---------------------------------------------------------------------------
# Подбор паттерна (заглушка Task 13 — см. докстроку Variant)
# ---------------------------------------------------------------------------


def _missing_signals(slide_spec: SlideSpec, assignments: list[SlotContent]) -> list[str]:
    """Роли контента, для которых В ЭТОЙ раскладке не нашлось слота —
    `assign_content` молча пропускает контент без подходящей роли (см. её
    докстроку), а для выбора паттерна (`_pick_pattern`) это должно СЧИТАТЬСЯ
    переполнением: раскладка без слота под заголовок карточки не "лучше
    заполнена", чем раскладка, где заголовок карточки просто некуда
    положить (найдено тестом `test_cards_expand_to_the_actual_number_of_
    items` — без этой проверки `_pick_pattern` выбирал первый попавшийся
    "cards"-паттерн шаблона, даже если у него не было слота card_title, и
    заголовки карточек молча терялись)."""
    covered = {a.role_hint for a in assignments}
    missing: list[str] = []
    if slide_spec.headline and "headline" not in covered:
        missing.append("headline")
    for block in slide_spec.blocks:
        if isinstance(block, TextBlock) and "body" not in covered:
            missing.append("body")
        elif isinstance(block, BulletBlock) and "bullets" not in covered:
            missing.append("bullets")
        elif isinstance(block, QuoteBlock) and "quote" not in covered:
            missing.append("quote")
        elif isinstance(block, CardBlock):
            if block.items and "card_body" not in covered:
                missing.append("cards")
            if any(c.title for c in block.items) and "card_title" not in covered:
                missing.append("card_title")
        elif isinstance(block, KpiBlock):
            if block.items and "kpi_value" not in covered:
                missing.append("kpi_value")
            if any(k.label for k in block.items) and "kpi_label" not in covered:
                missing.append("kpi_label")
    return missing


@dataclass(frozen=True)
class _SelectionHistory:
    """Раскладки, уже выбранные для ПРЕДЫДУЩИХ слайдов ЭТОЙ же колоды —
    вход штрафа за повтор (`_diversity_penalty`, Task 18, находка №3 брифа:
    "ранжирование идёт по двум признаками: влезает ли текст и не слишком ли
    пусто... признака «на прошлом слайде уже была такая же» нет, поэтому
    берётся самая безопасная раз за разом"). Пусто по умолчанию — прямые
    вызовы `_pick_pattern`/`_ranked_candidates` без истории (существующие
    тесты Task 9-13, `tests/compose/test_builder.py`) ведут себя ровно как
    раньше, без единого штрафа: диверсификация — свойство ЦИКЛА по слайдам
    (`build_deck`), не отдельного вызова подбора."""

    counts: dict[str, int] = field(default_factory=dict)
    last_pattern_id: str | None = None

    def with_choice(self, pattern_id: str) -> "_SelectionHistory":
        counts = dict(self.counts)
        counts[pattern_id] = counts.get(pattern_id, 0) + 1
        return _SelectionHistory(counts=counts, last_pattern_id=pattern_id)


_EMPTY_HISTORY = _SelectionHistory()

# Штраф за повтор КОНКРЕТНОЙ раскладки (`Pattern.pattern_id`), не только её
# `kind` — Task 18 брифа буквально: "признака «на прошлом слайде уже была
# такая же» нет". Кандидаты `_ranked_candidates` УЖЕ отфильтрованы одним
# `kind` (`p.kind == slide_spec.kind` в её вызове ниже) — внутри одного
# вида в богатом шаблоне бывает несколько РАЗНЫХ раскладок (VK Education:
# 12 "bullets"), и без штрафа `-p.score` (самый частый решающий признак,
# когда `fit`/`capacity`/`fill` совпадают у нескольких кандидатов — частый
# случай, см. ниже) стабильно выбирает ОДНУ и ту же "самую безопасную"
# снова и снова: живой прогон задачи (обязательная проверка, VK Education)
# — 33 намайненных раскладки, а в готовой колоде использовано фактически
# два оформления.
#
# 1.0 — заведомо больше типичного разброса `-score` (`score` в [0, 1], см.
# `patterns._score`) и заведомо больше типичного разброса `-visual_bias`
# (декор считанными штуками) — гарантирует, что среди кандидатов, РАВНЫХ по
# fit/capacity/fill (позиции 0-3 кортежа ниже, штраф стоит СТРОГО ПОСЛЕ
# них и потому НИКОГДА не может пересилить настоящее переполнение/пустоту,
# см. докстроку `_pattern_rank_key`), тот же самый `pattern_id`, что и на
# прошлом слайде, не победит, если есть хоть один не менее пригодный
# альтернативный кандидат.
_REPEAT_PREV_PATTERN_PENALTY = 1.0

# Штраф за КАЖДОЕ предыдущее использование этой же раскладки где-либо в
# колоде (не только на прошлом слайде) — меньше штрафа за немедленный повтор
# (0.5 < 1.0): "раскладка уже стояла три слайда назад" — не так плохо, как
# "стоит второй слайд подряд", но раскладка, использованная уже 2 раза,
# всё равно должна уступить кандидату, использованному 0-1 раз, при
# сопоставимой пригодности — растягивает выбор по всем кандидатам вида, а
# не только избегает соседства.
_REPEAT_ANYWHERE_PENALTY_STEP = 0.5


def _diversity_penalty(pattern: Pattern, history: _SelectionHistory) -> float:
    penalty = _REPEAT_PREV_PATTERN_PENALTY if pattern.pattern_id == history.last_pattern_id else 0.0
    penalty += _REPEAT_ANYWHERE_PENALTY_STEP * history.counts.get(pattern.pattern_id, 0)
    return penalty


def _pattern_rank_key(
    slide_spec: SlideSpec, p: Pattern, profile: TemplateProfile, variant: Variant,
    history: _SelectionHistory = _EMPTY_HISTORY,
):
    """Ключ сортировки одного паттерна-кандидата `p` для `slide_spec` —
    вынесен из `_pick_pattern` (Task 13 продолжение) так, чтобы им мог
    пользоваться и он сам (единственный победитель), и `_ranked_candidates`
    (весь список по порядку — нужен циклу аудита `_place_best_candidate`,
    которому мало ОДНОГО выбора: если он не пройдёт проверку, нужен
    СЛЕДУЮЩИЙ по правильности кандидат, не случайный). Порядок ранжирования:

    1. влезает ли вообще (`fit.ok`) — не влезающие кандидаты хуже любого
       влезающего;
    2. среди не влезающих — наименьшее `overflow_ratio` (бриф: "если ни
       одна не подходит, выбирай ту, где переполнение наименьшее");
    3. вместимость раскладки (`_capacity_badness`, находка повторного
       визуального ревью, "главная находка") — раскладка, чья `Capacity.
       max_items` заметно расходится с фактическим числом элементов
       содержания (шесть слотов повтора под два элемента), хуже раскладки
       той же вместимости, ДАЖЕ КОГДА содержание формально влезает и не
       выглядит пустым по `fill_ratio` — иначе выбор физически нечем
       отличить раскладку, оставляющую четыре пустых декоративных рамки, от
       нормально заполненной;
    4. "не слишком пусто/плотно" (`_fill_badness`, ТЗ D05) — среди влезающих
       кандидатов раскладка, где содержание не тонет в пустоте и не
       перегружает холст, предпочтительнее формально влезающей, но
       занимающей четверть холста;
    5. штраф за повтор (`_diversity_penalty`, Task 18, находка №3 брифа) —
       ПОСЛЕ фит/вместимости/заполненности (позиции 0-3), а не вместо них:
       "лучше повторить раскладку, чем выдать слайд с вылезающим текстом"
       (брифом дословно) — раз штраф стоит СТРОГО после них в кортеже
       сравнения, он структурно не может пересилить настоящую разницу в
       fit/capacity/fill, независимо от величины своего веса, лексикографи-
       ческое сравнение кортежей просто не доходит до него, пока эти позиции
       не равны; ПЕРЕД `score`/декором — раз пригодность у кандидатов
       сопоставима, разнообразие важнее чистого вкуса майнинга;
    6. паттерн с более высоким `score` (майнинг увереннее в нём);
    7. `Variant.visual`/`Variant.dense` — тот же бонус/штраф за декор, что и
       раньше (временная эвристика Task 13, см. докстроку `Variant`)."""
    fit = fits(slide_spec, p, profile)
    # `airy` не смещает выбор по декору вовсе (0) — её плотность решает
    # `kind`, уже проставленный `plan.variants.apply_variant` ДО того,
    # как сюда дошёл вызов (см. докстроку `Variant` и `_resolve_pattern`
    # ниже: при заданном `pattern_id` он лишь переставлен первым в списке
    # кандидатов, ранжир всё равно считается для всех).
    if variant is Variant.visual:
        visual_bias = len(p.decor)
    elif variant is Variant.dense:
        visual_bias = -len(p.decor)
    else:
        visual_bias = 0
    return (
        0 if fit.ok else 1, fit.overflow_ratio, _capacity_badness(p, slide_spec),
        _fill_badness(fit.fill_ratio), _diversity_penalty(p, history), -p.score, -visual_bias,
    )


def _ranked_candidates(
    slide_spec: SlideSpec, patterns: list[Pattern], profile: TemplateProfile, variant: Variant,
    history: _SelectionHistory = _EMPTY_HISTORY,
) -> list[Pattern]:
    """Все кандидаты `pattern.kind == slide_spec.kind`, отсортированные от
    лучшего к худшему тем же ключом, что и `_pick_pattern` (см. докстроку
    `_pattern_rank_key`) — список, а не единственный выбор, потому что
    аудит внутри цикла сборки (`_place_best_candidate`, Task 13 продолжение)
    должен уметь пробовать ВТОРОГО/ТРЕТЬЕГО по качеству кандидата, если
    лучший на бумаге по `fits()` на деле дал наложение/выход за границы на
    РЕАЛЬНОЙ геометрии уже уложенного слайда."""
    candidates = [p for p in patterns if p.kind == slide_spec.kind]
    return sorted(candidates, key=lambda p: _pattern_rank_key(slide_spec, p, profile, variant, history))


def _pick_pattern(
    slide_spec: SlideSpec, patterns: list[Pattern], profile: TemplateProfile, variant: Variant,
    history: _SelectionHistory = _EMPTY_HISTORY,
) -> Pattern | None:
    """Перебирает кандидатов `pattern.kind == slide_spec.kind` и берёт того,
    в кого содержание влезает (`fits()`, шкала ужимания целиком, не только
    свой кегль) — не первого попавшегося (см. `_pattern_rank_key` про
    полный порядок ранжирования). Публичная обёртка над
    `_ranked_candidates` — сохраняет прежний интерфейс (единственный
    Pattern, не список) буквально ради существующих прямых тестов на неё
    (`tests/compose/test_builder.py`, Task 9-10) — `history` необязательна
    (пустая по умолчанию, Task 18), эти тесты вызывают функцию без истории
    и ведут себя ровно как раньше, без штрафа за повтор."""
    ranked = _ranked_candidates(slide_spec, patterns, profile, variant, history)
    return ranked[0] if ranked else None


def _resolve_pattern(
    slide_spec: SlideSpec, patterns: list[Pattern], profile: TemplateProfile, variant: Variant,
    history: _SelectionHistory = _EMPTY_HISTORY,
) -> list[Pattern]:
    """Кандидаты раскладки для `slide_spec`, в порядке предпочтения — Task
    13 продолжение сменило это с единственного выбора на СПИСОК: решение
    "эта раскладка подходит" теперь принимает не эвристика `fits()` заранее,
    а детерминированный аудит УЖЕ уложенного слайда (`_place_best_
    candidate`), и ему нужно из чего выбирать, если первый кандидат не
    пройдёт проверку.

    `slide_spec.pattern_id` (Task 13: проставлен `plan.variants.
    apply_variant`, детерминированно по вместимости, и/или уточнён `plan.
    writer.pick_patterns` моделью) — по-прежнему ПЕРВЫЙ кандидат в списке,
    если он существует в этом профиле и несёт тот же `kind`, но здесь он
    больше НЕ принимается слепо по `fits().ok`: окончательную проверку
    делает аудит уже уложенного слайда, а не оценка текстфита ДО укладки
    (тот же принцип, что раньше — "код не доверяет предложению слепо",
    просто проверка стала точнее).

    `history` (Task 18) — раскладки, уже выбранные для предыдущих слайдов
    ЭТОЙ колоды (`build_deck` передаёт её и накапливает по ходу цикла, см.
    `_SelectionHistory`) — двигает штрафуемых повторами кандидатов вниз
    списка ДО того, как `pattern_id`, проставленный `apply_variant`,
    переставлен первым: явное предпочтение плана всё ещё побеждает штраф
    (тот же принцип "план предлагает, сборка не отменяет предложение без
    причины", что и раньше), штраф работает только когда `pattern_id` не
    проставлен или его раскладки нет в профиле."""
    ranked = _ranked_candidates(slide_spec, patterns, profile, variant, history)
    if not slide_spec.pattern_id:
        return ranked
    preferred = next((p for p in ranked if p.pattern_id == slide_spec.pattern_id), None)
    if preferred is None:
        return ranked
    return [preferred, *(p for p in ranked if p.pattern_id != preferred.pattern_id)]


# ---------------------------------------------------------------------------
# Аудит внутри цикла сборки (Task 13, продолжение брифа: "Аудит — часть
# пайплайна, а не внешняя проверка"). После укладки КАЖДОГО кандидата
# раскладки слайд проверяется пятью детерминированными проверками
# (`audit.deterministic.audit_slide_layout` — L01/L02/L03/L04/D05, буквально
# то, что бриф называет "проблемами уровня ошибки": наложение, выход за
# границы, невлезающий текст, заполненность вне допуска). Раскладка,
# получившая хоть одну такую находку, считается неподходящей — берётся
# следующий кандидат.
#
# Бюджет попыток — обоснование числом (бриф прямо просит обосновать бюджет
# временем): контрольная проверка задачи меряет полный прогон всех 24
# проверок аудита на готовую колоду из 12-15 слайдов в ДОЛИ СЕКУНДЫ (см.
# отчёт задачи Task 13, таблица "Сборка+аудит" — 0.4-1.2с НА ВСЮ колоду);
# пять проверок ОДНОГО слайда кратно легче этого. Сама пересборка слайда
# тоже дешёвая — модель НЕ зовётся (содержание уже написано `plan.writer`,
# меняется только раскладка, тот же принцип, что и у `apply_variant`, не
# берущего `llm` в сигнатуре). При бюджете времени колоды в 5 минут (ТЗ) и
# доминирующей стоимости самого разбора/написания текста (~170-180с из
# 160-196с на шаблон, отчёт задачи) три полных пересборки-с-аудитом на
# слайд — миллисекунды, не минуты; смысла НЕ ограничивать попытки тоже нет:
# на некоторых шаблонах (ЛЦТ2026, WorkSpace) у одного `kind` бывает по
# 10+ паттернов, и исчерпывающий перебор всех кандидатов КАЖДОГО слайда
# рисковал бы не уложиться в бюджет на самой богатой колоде. 3 — верхняя
# граница диапазона брифа ("две-три"): даёт циклу шанс пропустить ОДНУ
# неудачную раскладку и ОДНУ пограничную, не тратя времени на длинный
# хвост скорее всего таких же плохих кандидатов дальше по ранжиру (после
# первых 2-3 по вместимости/стилю оставшиеся кандидаты почти всегда хуже
# по построению `_pattern_rank_key`, не лучше).
_MAX_LAYOUT_ATTEMPTS = 3


def _remove_last_slide(prs) -> None:
    """Убирает ПОСЛЕДНИЙ добавленный слайд из колоды — используется ТОЛЬКО
    `_place_best_candidate`, чтобы откатить отклонённого кандидата раскладки
    ДО того, как пробовать следующего. Тот же рецепт python-pptx, что и
    `_clear_sample_slides` (`Part.drop_rel` снижает счётчик ссылок на часть
    слайда; когда он доходит до нуля, сама часть и её relationships уходят
    вместе с ней)."""
    xml_slides = prs.slides._sldIdLst  # noqa: SLF001 — тот же приём, что и `_clear_sample_slides` выше в этом файле
    sld = xml_slides[-1]
    prs.part.drop_rel(sld.rId)
    xml_slides.remove(sld)


def _place_best_candidate(
    prs, slide_spec: SlideSpec, candidates: list[Pattern], profile: TemplateProfile, canvas: Canvas,
    audit_config: AuditConfig, *, bullet_char: str = "•", user_photos: dict[str, Path] | None = None,
    image_bytes: Callable[[str], bytes | None] | None = None,
    source_slides: dict[int, object] | None = None,
) -> tuple[Pattern, list[str]]:
    """Собрали слайд — проверили — не понравилось — взяли другую раскладку
    и пересобрали (бриф, дословно). Пробует кандидатов `candidates` по
    порядку (уже отранжированы `_resolve_pattern`/`_ranked_candidates` —
    от предпочтительного к худшему), не больше `_MAX_LAYOUT_ATTEMPTS`:
    укладывает слайд НА РЕАЛЬНЫЙ `prs`, гонит по нему `audit_slide_layout`
    (пять проверок уровня "ошибка" — см. докстроку раздела), и

    - если находок нет — оставляет слайд как есть, возвращает эту
      раскладку;
    - если находки есть — откатывает слайд (`_remove_last_slide`), логирует
      причину отказа и пробует следующего кандидата;
    - если В ПРЕДЕЛАХ БЮДЖЕТА не нашлось кандидата без находок — заново
      укладывает того, у кого находок оказалось МЕНЬШЕ ВСЕГО (бриф: "потом
      берётся кандидат с наименьшим числом ошибок") — слайд НИКОГДА не
      остаётся несобранным (бриф: "пустого слайда быть не должно никогда").

    Возвращает `(выбранная_раскладка, лог_попыток)` — лог уходит в
    `slide_spec.findings` вызывающим кодом (`build_deck`): "это пойдёт на
    защиту как доказательство, что аудит встроен, а не приделан" (бриф).

    `source_slides`: слайды-примеры шаблона по номеру (`build_deck`
    берёт их до очистки колоды). Если у кандидата есть свой пример, сначала
    пробуется клон (`_try_clone`), и только если он не собрался или не
    прошёл тот же аудит, кандидат собирается с нуля. `None`/пусто: только
    сборка с нуля, как было до клонирования."""
    tried = candidates[:_MAX_LAYOUT_ATTEMPTS]
    notes: list[str] = []
    best: tuple[int, Pattern, list[str]] | None = None  # (число находок, паттерн, коды находок)

    for attempt, pattern in enumerate(tried, start=1):
        cloned = _try_clone(
            prs, slide_spec, pattern, profile, canvas, audit_config, source_slides, notes,
            bullet_char=bullet_char, user_photos=user_photos,
        )
        if cloned is not None:
            notes.extend(cloned)
            return pattern, notes
        trial_spec = replace(slide_spec, findings=[])
        place_slide(
            prs, trial_spec, pattern, profile, audit_config, bullet_char=bullet_char,
            user_photos=user_photos, image_bytes=image_bytes,
        )
        errors = audit_slide_layout(prs.slides[-1], canvas, profile, audit_config, index=slide_spec.index)
        if not errors:
            if attempt > 1:
                notes.append(
                    f"Слайд {slide_spec.index}: раскладка {pattern.pattern_id!r} принята с попытки "
                    f"{attempt}/{len(tried)} (без находок уровня ошибки)."
                )
            notes.extend(trial_spec.findings)
            return pattern, notes
        ids = sorted({f.check_id for f in errors})
        notes.append(
            f"Слайд {slide_spec.index}: раскладка {pattern.pattern_id!r} отклонена (попытка "
            f"{attempt}/{len(tried)}) — {len(errors)} находок уровня ошибки: {', '.join(ids)}."
        )
        if best is None or len(errors) < best[0]:
            best = (len(errors), pattern, ids)
        _remove_last_slide(prs)

    best_errors, best_pattern, best_ids = best
    trial_spec = replace(slide_spec, findings=[])
    place_slide(
        prs, trial_spec, best_pattern, profile, audit_config, bullet_char=bullet_char,
        user_photos=user_photos, image_bytes=image_bytes,
    )
    notes.append(
        f"Слайд {slide_spec.index}: ни один из {len(tried)} проверенных кандидатов не прошёл аудит "
        f"без находок — выбрана раскладка {best_pattern.pattern_id!r} с наименьшим числом находок "
        f"({best_errors}: {', '.join(best_ids)})."
    )
    notes.extend(trial_spec.findings)
    return best_pattern, notes


# ---------------------------------------------------------------------------
# Сборка клоном слайда-примера (CLONE → BIND → ADAPT, см. `compose.clone`)
# ---------------------------------------------------------------------------

# Метка пути сборки в имени слайда (`p:cSld/@name`), её читает
# `scripts/inspect_deck.py`.
CLONE_MARK_PREFIX = "deckforge:clone:"

# Интерлиньяж, которым аудит (L03) меряет абзац без явного `a:lnSpc`
# (`audit.deterministic._DEFAULT_LINE_SPACING`). Клон меряет тем же
# числом: ужать текст по одному интерлиньяжу, а проверять по другому:
# ровно та рассинхронизация, из-за которой 25 сентября половина
# заголовков уехала на запасную раскладку (см. комментарий в `_draw_slot`).
_AUDIT_DEFAULT_LINE_SPACING = 1.2

_CLONE_PICTURE_ROLES = frozenset({"image", "icon"})

# Роли «рамки» слайда: заголовок, подзаголовок, сноска. Слайд, на который
# легли только они, содержания не несёт.
_CLONE_FRAME_ROLES = frozenset({"headline", "subhead", "source"})


@dataclass(frozen=True)
class CloneOutcome:
    """Итог `place_slide_by_clone`: `reason` равен `None`, если слайд
    собран (он последний в колоде), иначе это причина отказа (слайда в
    колоде нет)."""
    reason: str | None


def _clone_examples_enabled() -> bool:
    try:
        return Settings.load(APP_YAML_PATH).compose.clone_examples
    except Exception:  # noqa: BLE001: нет конфига (тесты на чужом дереве): поведение по умолчанию
        return True


def _clone_source(pattern: Pattern, source_slides: dict[int, object] | None):
    """(номер, слайд) примера, с которого снята раскладка, или `None`.
    Берётся первый номер: `patterns._dedup` схлопывает похожие примеры в
    один паттерн, и первый: тот, чьи коробки слотов лежат в профиле."""
    if not source_slides or not pattern.source_slide_index:
        return None
    number = pattern.source_slide_index[0]
    slide = source_slides.get(number)
    return (number, slide) if slide is not None else None


def _try_clone(
    prs, slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile, canvas: Canvas,
    audit_config: AuditConfig, source_slides: dict[int, object] | None, notes: list[str],
    *, bullet_char: str, user_photos: dict[str, Path] | None,
) -> list[str] | None:
    """Пробует собрать слайд клоном примера раскладки `pattern`. Успех:
    клон собрался (все слоты с содержимым нашли свою фигуру) и прошёл тот же
    аудит, что и сборка с нуля (`audit_slide_layout`, L01-L04, D05): тогда
    слайд остаётся в колоде, а функция отдаёт находки для `slide_spec`.
    Неудача: слайд убран, причина дописана в `notes`, возвращается `None`
    и вызывающий собирает этот же кандидат с нуля."""
    source = _clone_source(pattern, source_slides)
    if source is None:
        return None
    number, source_slide = source
    trial_spec = replace(slide_spec, findings=[])
    slides_before = len(prs.slides)
    try:
        outcome = place_slide_by_clone(
            prs, trial_spec, pattern, profile, audit_config, source_slide,
            bullet_char=bullet_char, user_photos=user_photos,
        )
    except Exception as exc:  # noqa: BLE001: незнакомая разметка примера: запасной путь, а не падение колоды
        if len(prs.slides) > slides_before:
            _remove_last_slide(prs)
        outcome = CloneOutcome(f"клон не собрался ({type(exc).__name__}: {exc})")
    reason = outcome.reason
    if reason is None:
        errors = _clone_errors(
            audit_slide_layout(prs.slides[-1], canvas, profile, audit_config, index=slide_spec.index),
        )
        if not errors:
            return [
                f"Слайд {slide_spec.index}: собран клоном слайда-примера №{number} шаблона "
                f"(раскладка {pattern.pattern_id!r}).",
                *trial_spec.findings,
            ]
        _remove_last_slide(prs)
        ids = sorted({f.check_id for f in errors})
        reason = f"аудит нашёл {len(errors)} ошибок уровня ошибки: {', '.join(ids)}"
    notes.append(
        f"Слайд {slide_spec.index}: клон слайда-примера №{number} (раскладка {pattern.pattern_id!r}) "
        f"не принят — {reason}; слайд собирается заново."
    )
    return None


def _clone_errors(findings: list) -> list:
    """Находки аудита, которые отклоняют клон: всё, кроме заполненности
    холста (D05). У клона она та же, что у примера, минус незаполненные
    единицы повтора: пустоватый титул в стиле шаблона лучше полного, но
    белого листа, который даёт сборка с нуля. Остальное (выход за край,
    наложения, переполнение рамки) судится строго, и по фигурам примера
    тоже: итоговый аудит колоды не различает, кто нарисовал фигуру, и
    слайд, который он забракует, лучше собрать с нуля (так на VK
    Education отсеивается пример с линией, заходящей за край холста)."""
    return [f for f in findings if f.check_id != "D05"]


def place_slide_by_clone(
    prs, slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile, audit_config: AuditConfig,
    source_slide, *, bullet_char: str = "•", user_photos: dict[str, Path] | None = None,
) -> CloneOutcome:
    """Клон примера `source_slide` с текстом `slide_spec` в слотах `pattern`.

    Возвращает `CloneOutcome`: собран ли слайд (он последний в `prs`) или
    почему нет; при отказе слайда в колоде нет. Аудит здесь не зовётся,
    решение о приёме принимает `_try_clone`.

    Содержание раскладывается по слотам тем же `assign_content_with_drops`,
    что и в `place_slide`, чтобы оба пути клали один и тот же текст в одни
    и те же роли. Отличается геометрия: клон не двигает фигуры, поэтому
    единицы повтора возвращаются на свои места в примере
    (`_native_repeat_contents`), а лишние единицы удаляются."""
    layout = _find_layout(prs, pattern.layout_id)
    if layout is None:
        return CloneOutcome(f"лейаут {pattern.layout_id!r} не найден в открытом шаблоне")
    canvas = Canvas(width_emu=profile.canvas_width_emu, height_emu=profile.canvas_height_emu)
    grid = _grid_from_model(profile.grid)

    contents, drops = assign_content_with_drops(slide_spec, pattern, grid)
    clean: list[SlotContent] = []
    for content in contents:
        hit = _placeholder_text_hit(content, audit_config)
        if hit is not None:
            slide_spec.findings.append(
                f"Слайд {slide_spec.index}: текст слота «{content.role_hint}» похож на "
                f"текст-заглушку шаблона («{hit}») — не отрисован, слот оставлен пустым."
            )
            continue
        clean.append(content)
    visual = slide_spec.visual
    table_rows = visual.table.rows if visual is not None and visual.kind == "table" and visual.table else None
    table_slot = _visual_slot(pattern, "table") if table_rows else None
    if slide_spec.blocks and _only_frame_text(clean) and table_slot is None:
        # Сборка с нуля потеряла бы то же самое, но её слайд с одним
        # заголовком отсеивает D05, а клон D05 не судит (см.
        # `_clone_errors`). Без этой проверки клон принимал слайд, где из
        # двух абзацев содержания не лёг ни один (два `TextBlock` на
        # раскладке, у которой слоты только под список). Таблица в слоте
        # под неё: содержание, слайд не пуст, а блок-пояснение уходит в
        # находки (`_note_drops`) так же, как у сборки с нуля.
        return CloneOutcome("ни один блок содержания не нашёл слота в раскладке")
    native = _native_repeat_contents(pattern, clean)
    if native is None:
        return CloneOutcome("элементов больше, чем единиц повтора в примере")

    slide = clone_example_slide(prs, source_slide, layout)
    matched = match_slots(slide, pattern.slots, canvas)
    index_of = {id(s): i for i, s in enumerate(pattern.slots)}
    bound = []
    for content in native:
        ref = matched.get(index_of.get(id(content.slot), -1))
        if ref is None:
            _remove_last_slide(prs)
            return CloneOutcome(f"в примере не нашлось фигуры под слот «{content.role_hint}»")
        bound.append((content, ref))

    family = _primary_family(profile)
    bound_elements = [ref.element for _, ref in bound]
    for content, ref in bound:
        ref = _shrink_frame_away_from_decor(slide, ref, bound_elements, canvas)
        bind_text(ref.element, content.paragraphs, bullet_char=bullet_char)
        _fit_cloned_text(slide, slide_spec, content, ref, profile, family, canvas)
        _fix_cloned_contrast(slide, ref, profile, canvas, audit_config)

    keep = [ref.element for _, ref in bound]
    table_ref = matched.get(index_of.get(id(table_slot), -1)) if table_slot is not None else None
    native_frame = table_ref.element if table_ref is not None and native_table(table_ref.element) is not None else None
    if native_frame is not None:
        keep.append(native_frame)
    filled = filled_repeat_units(pattern, native)
    kept_decor = expand_decor(pattern, None, grid, filled)
    kept_ids = {id(d) for d in kept_decor}
    bound_slots = {id(content.slot) for content, _ in bound}
    for slot in _sample_text_slots(pattern, filled):
        ref = matched.get(index_of[id(slot)])
        if id(slot) not in bound_slots and ref is not None:
            bound_slots.add(id(slot))
            keep.append(ref.element)
    prune_unfilled(
        slide,
        [d for d in pattern.decor if id(d) not in kept_ids],
        [s for s in pattern.slots if id(s) not in bound_slots and s.role not in _CLONE_PICTURE_ROLES],
        canvas, keep=keep, protect=[d.box for d in kept_decor],
    )
    remove_stray_text(slide, canvas, keep=keep, badge_boxes=[d.box for d in kept_decor if d.badge_text])
    remove_sample_frames(slide, keep=keep)
    if native_frame is not None:
        _fill_native_table_on_clone(
            slide, slide_spec, pattern, profile, canvas, native_frame, table_ref.box, _only_frame_text(clean),
        )
    else:
        _place_visual_on_clone(
            slide, slide_spec, pattern, profile, canvas, matched, user_photos, keep,
            sole_content=_only_frame_text(clean),
        )
    _remove_empty_placeholders(slide)
    _note_drops(slide_spec, pattern, drops)
    mark_slide(slide, CLONE_MARK_PREFIX + pattern.pattern_id)
    return CloneOutcome(None)


def _native_repeat_contents(pattern: Pattern, contents: list[SlotContent]) -> list[SlotContent] | None:
    """Возвращает содержание единиц повтора на РОДНЫЕ слоты примера.

    `blocks.expand_repeat` пересчитывает коробки единиц под фактическое
    число элементов (компактно от поля). Сборке с нуля это и нужно, а в
    клоне фигуры стоят там, где их поставил дизайнер: i-я развёрнутая
    единица: это i-я единица примера (нумерация по оси повтора, та же,
    что у `blocks.filled_repeat_units` и `DecorShape.repeat_index`). Слот
    внутри единицы выбирается по роли и положению поперёк оси.

    `None`: элементов больше, чем единиц в примере, клоном такое не
    собрать (лишним карточкам не на чем стоять)."""
    native_ids = {id(s) for s in pattern.slots}
    expanded = [c for c in contents if id(c.slot) not in native_ids]
    if not expanded:
        return contents
    repeat = pattern.repeat
    if repeat is None:
        return None
    along = (lambda b: b.left) if repeat.axis == "x" else (lambda b: b.top)
    across = (lambda b: (b.top, b.height, b.width)) if repeat.axis == "x" else (lambda b: (b.left, b.width, b.height))
    members = [s for s in pattern.slots if s.role in repeat.slot_roles]
    native_units = sorted({round(along(s.box), 3) for s in members})
    expanded_units = sorted({round(along(c.slot.box), 3) for c in expanded})
    if len(expanded_units) > len(native_units):
        return None

    result: list[SlotContent] = []
    used: set[int] = set()
    for content in contents:
        if id(content.slot) in native_ids:
            result.append(content)
            continue
        unit = native_units[expanded_units.index(round(along(content.slot.box), 3))]
        want = across(content.slot.box)
        candidates = [
            s for s in members
            if s.role == content.slot.role and round(along(s.box), 3) == unit and id(s) not in used
        ]
        if not candidates:
            return None
        slot = min(candidates, key=lambda s: sum(abs(a - b) for a, b in zip(across(s.box), want)))
        used.add(id(slot))
        result.append(replace(content, slot=slot))
    return result


_ORDINAL_RE = re.compile(r"\d{1,2}\.?")


def _sample_text_slots(pattern: Pattern, filled: set[int]) -> list[PatternSlot]:
    """Слоты, чей текст примера клон оставляет как есть: номер шага в
    кружке («1», «2» над карточкой) и постоянный текст шаблона («Спасибо за
    внимание»). Содержания под них нет по замыслу, и без этой оговорки клон
    удалял бы их как незаполненные слоты, оставляя карточку без номера и с
    дырой на его месте.

    Признак берётся из схемы слотов от модели (`PatternSlot.keeps_sample_
    text`); без модели номер узнаётся по тексту примера (одна-две цифры),
    как и до схемы. Слот внутри единицы повтора остаётся, только если сама
    единица заполнена: номер верен как есть, клон оставляет первые единицы
    повтора по порядку, i-я карточка стоит в i-й единице. Слот вне повтора
    со схемой остаётся всегда."""
    repeat = pattern.repeat
    members: list[PatternSlot] = []
    units: list[float] = []
    along = None
    if repeat is not None:
        along = (lambda b: b.left) if repeat.axis == "x" else (lambda b: b.top)
        members = [s for s in pattern.slots if s.role in repeat.slot_roles]
        units = sorted({round(along(s.box), 3) for s in members})
    member_ids = {id(s) for s in members}
    kept: list[PatternSlot] = []
    for slot in pattern.slots:
        if id(slot) not in member_ids:
            if slot.keeps_sample_text:
                kept.append(slot)
            continue
        marked = slot.keeps_sample_text or bool(
            slot.sample_text and _ORDINAL_RE.fullmatch(slot.sample_text.strip())
        )
        if marked and units.index(round(along(slot.box), 3)) in filled:
            kept.append(slot)
    return kept


# Зазор между суженной рамкой текста и графикой справа, доли холста.
_FRAME_GAP = 0.02
# Суженная рамка не уже стольких долей холста и половины исходной: иначе
# текст в столбик хуже наложения, и слайд честно уйдёт на сборку с нуля.
_FRAME_MIN_WIDTH = 0.3


def _frame_obstacles(slide, ref, bound_elements: list, canvas: Canvas) -> list:
    """Графика, с которой рамке текста нельзя пересекаться: картинки и
    фигуры без текста самого слайда и его лейаута. Лейаут обязателен:
    графика обложки VK Education лежит именно там, на слайде только два
    плейсхолдера."""
    own = [
        r for r in slide_refs(slide, canvas)
        if r.box is not None and r.element is not ref.element and r.element not in bound_elements
    ]
    layout = [
        r for r in walk_shapes(slide.slide_layout._element, canvas)  # noqa: SLF001
        if r.box is not None and not r.is_placeholder
    ]
    return [
        r for r in own + layout
        if r.kind == "picture" or (r.kind == "shape" and not shape_text(r.element).strip())
    ]


def _shrink_frame_away_from_decor(slide, ref, bound_elements: list, canvas: Canvas):
    """ADAPT: рамка примера часто шире своего текста. Обложка VK Education:
    плейсхолдер заголовка во всю ширину, справа половину слайда занимает
    графика, и заголовок длиннее образца заезжал на неё (прогон 26 сентября
    2026). Если справа от текста стоит графика (картинка или фигура без
    текста), которая частично перекрывает рамку, рамка сужается до неё, а
    кегль дальше подберёт `_fit_cloned_text` тем же замером. Фигура,
    целиком содержащая рамку (плашка-фон), помехой не считается."""
    box = ref.box
    if box is None:
        return ref
    new_right = box.right
    for other in _frame_obstacles(slide, ref, bound_elements, canvas):
        if _contains(other.box, box) or _contains(box, other.box):
            continue
        if _overlap_ratio(box, other.box) <= _OVERLAP_MIN_AREA_SHARE:
            continue
        if other.box.left > box.left + box.width * 0.3:
            new_right = min(new_right, other.box.left - _FRAME_GAP)
    if new_right >= box.right - 0.001:
        return ref
    width = new_right - box.left
    if width < max(_FRAME_MIN_WIDTH, box.width * 0.5):
        return ref
    shrunk = replace(box, width=width)
    set_shape_box(ref.element, shrunk, canvas)
    return replace(ref, box=shrunk)


def _fit_cloned_text(
    slide, slide_spec: SlideSpec, content: SlotContent, ref, profile: TemplateProfile, family: str, canvas: Canvas,
) -> None:
    """ADAPT: текст клона обязан влезть в рамку примера. Кегль примера
    остаётся, если текст помещается; иначе ужимается по той же шкале, что
    у сборки с нуля (`_shrink_sequence`, не ниже подписи). Если не влез и
    на подписи, кегль остаётся минимальным, а решение «не годится» примет
    аудит (L03) и отправит слайд на сборку с нуля."""
    style = text_style(ref.element)
    # Кегль, которым PowerPoint нарисует текст: свой у run, иначе
    # унаследованный от лейаута/мастера, и только если его нет, из профиля.
    size = style.size_pt or inherited_text_size(slide, ref.element) or profile.denorm_pt(content.slot.size_pt)
    fam = style.family or family
    spacing = style.line_spacing or _AUDIT_DEFAULT_LINE_SPACING
    left_in, top_in, right_in, bottom_in = style.insets_in
    width_in = ref.box.width * canvas.width_emu / EMU_PER_INCH - left_in - right_in
    height_in = ref.box.height * canvas.height_emu / EMU_PER_INCH - top_in - bottom_in
    text = _joined_text(content.paragraphs)
    if width_in <= 0 or height_in <= 0 or not text.strip():
        return
    if measure(text, fam, size, width_in, line_spacing=spacing).lines > len(content.paragraphs):
        allow_wrap(ref.element)
    sizes = [size] + [s for s in _shrink_sequence(profile, content.slot.size_pt) if s < size - 0.05]
    for candidate in sizes:
        if measure(text, fam, candidate, width_in, line_spacing=spacing).height_in <= height_in + _FIT_TOLERANCE_IN:
            # Кегль пишется, только если его пришлось ужать (ступень шкалы
            # шаблона). Влезший унаследованный кегль остаётся наследуемым:
            # записанный явно, он мог бы не совпасть ни с одной ступенью
            # шкалы (VK Tech: 47pt у заголовка лейаута, находка T02).
            if candidate != size:
                set_text_size(ref.element, candidate)
            return
    set_text_size(ref.element, sizes[-1])
    slide_spec.findings.append(
        f"Слайд {slide_spec.index}: текст слота «{content.role_hint}» не помещается в рамку "
        f"примера даже кеглем {sizes[-1]:.1f}pt."
    )


def _fix_cloned_contrast(slide, ref, profile: TemplateProfile, canvas: Canvas, audit_config: AuditConfig) -> None:
    """Текст клона обязан читаться. Шаблоны нередко набирают текст-образец
    светло-серым, как подсказку «здесь будет текст» (VK Tech: описания
    карточек серым по белому, 3.2:1), и наш текст в том же цвете аудит
    справедливо бракует (T06). Если контраст ниже порога аудита, цвет
    заменяется цветом палитры шаблона с лучшим контрастом, тем же
    `_best_contrast_color`, что у сборки с нуля.

    Фон ищется в том же порядке, что у аудита: своя заливка фигуры, самая
    маленькая залитая фигура под ней, фон слайда, фон макета. Цвет без
    явной заливки у run не трогается: аудит его тоже не судит, а
    унаследованный цвет шаблон подбирал под свой фон."""
    run = ref.element.find(".//" + qn("a:r"))
    r_pr = run.find(qn("a:rPr")) if run is not None else None
    fill = r_pr.find(qn("a:solidFill")) if r_pr is not None else None
    if fill is None:
        return
    scheme, clr_map = profile.theme.scheme, profile.theme.clr_map
    color = resolve_color(fill, scheme, clr_map)
    if not isinstance(color, Color):
        return
    bg_luminance = _clone_background_luminance(slide, ref, profile, canvas)
    ratio = _contrast_ratio_from_luminance(bg_luminance, _relative_luminance(color.hex))
    size = int(r_pr.get("sz")) / 100 if r_pr.get("sz") else 0.0
    cfg = audit_config.template
    is_large = size >= cfg.large_text_pt or (r_pr.get("b") == "1" and size >= cfg.large_bold_pt)
    if ratio >= (cfg.min_contrast_large if is_large else cfg.min_contrast_small):
        return
    better = _best_contrast_color(color.hex, bg_luminance, profile)
    for rpr in ref.element.iter(qn("a:rPr")):
        for old_fill in rpr.findall(qn("a:solidFill")):
            rpr.remove(old_fill)
        new_fill = etree.Element(qn("a:solidFill"))
        etree.SubElement(new_fill, qn("a:srgbClr")).set("val", better.lstrip("#").upper())
        # a:solidFill по схеме стоит сразу после a:ln (если он есть), до
        # эффектов и гарнитур.
        ln = rpr.find(qn("a:ln"))
        if ln is not None:
            ln.addnext(new_fill)
        else:
            rpr.insert(0, new_fill)


def _clone_background_luminance(slide, ref, profile: TemplateProfile, canvas: Canvas) -> float:
    scheme, clr_map = profile.theme.scheme, profile.theme.clr_map

    def solid(element) -> Color | None:
        sp_pr = element.find(qn("p:spPr"))
        fill = sp_pr.find(qn("a:solidFill")) if sp_pr is not None else None
        color = resolve_color(fill, scheme, clr_map) if fill is not None else None
        return color if isinstance(color, Color) else None

    own = solid(ref.element)
    if own is not None:
        return _relative_luminance(own.hex)
    plates = []
    for other in slide_refs(slide, canvas):
        if other.element is ref.element or other.kind != "shape" or other.box is None:
            continue
        color = solid(other.element)
        if color is not None and _contains(other.box, ref.box):
            plates.append((other.box.width * other.box.height, color))
    if plates:
        return _relative_luminance(min(plates, key=lambda t: t[0])[1].hex)
    bg_fill = slide._element.find(  # noqa: SLF001
        qn("p:cSld") + "/" + qn("p:bg") + "/" + qn("p:bgPr") + "/" + qn("a:solidFill")
    )
    bg = resolve_color(bg_fill, scheme, clr_map) if bg_fill is not None else None
    if isinstance(bg, Color):
        return _relative_luminance(bg.hex)
    return slide_background_luminance(slide, profile)


def _place_visual_on_clone(
    slide, slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile, canvas: Canvas,
    matched: dict, user_photos: dict[str, Path] | None, keep: list, *, sole_content: bool = False,
) -> None:
    """Визуал слайда на клоне. Таблица и график кладутся тем же кодом, что
    и при сборке с нуля, но место под них сперва освобождается от образца.
    Фото пользователя подменяет картинку в фигуре примера (рамка, обрезка
    по форме и эффекты остаются дизайнерскими). Без фото пользователя
    картинка примера остаётся как есть: это оформление шаблона, и класть
    поверх неё ассет каталога значило бы закрыть его чужим."""
    visual = slide_spec.visual
    if visual is None:
        return
    if visual.kind in ("table", "chart"):
        slot = (
            _visual_slot(pattern, "table") if visual.kind == "table"
            else _visual_slot(pattern, "chart") or _visual_slot(pattern, "table") or _visual_slot(pattern, "image")
        )
        if slot is not None:
            remove_in_box(slide, slot.box, canvas, keep=keep)
        _place_visual(slide, slide_spec, pattern, profile, user_photos, sole_content=sole_content)
        return
    if visual.kind not in ("photo", "icon"):
        return
    photo_name = visual.photo_name
    photo_path = (user_photos or {}).get(photo_name) if photo_name else None
    if photo_path is None:
        return
    slot = (
        _visual_slot(pattern, "image") if visual.kind == "photo" else _visual_slot(pattern, "icon")
    ) or _visual_slot(pattern, "image") or _visual_slot(pattern, "icon")
    ref = matched.get(next((i for i, s in enumerate(pattern.slots) if s is slot), -1)) if slot else None
    if ref is None:
        _place_picture_visual(slide, slide_spec, pattern, profile, visual.kind, user_photos)
        return
    try:
        replace_picture(slide, ref.element, Path(photo_path).read_bytes(), ref.box, canvas)
    except Exception as exc:  # noqa: BLE001: битый файл пользователя: картинка примера остаётся
        slide_spec.findings.append(
            f"Слайд {slide_spec.index}: пользовательская фотография {photo_name!r} ({photo_path}) "
            f"не вставлена ({exc})."
        )


# Ячейка примера с заливкой темнее этого (относительная яркость) выделена
# дизайнером: «Итого», строка-акцент. Светлые полосы чередования (EBF3F9 у
# VK Education, яркость около 0.9) выделением не считаются.
_HIGHLIGHT_LUMINANCE = 0.4
# Кегль ячейки, у которой своего нет: дефолт PowerPoint для таблицы.
_TABLE_DEFAULT_PT = 18.0
_TABLE_SHRINK = 0.9


def _fill_native_table_on_clone(
    slide, slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile, canvas: Canvas,
    frame, slot_box: Box, sole_content: bool,
) -> None:
    """BIND/ADAPT для таблицы: заполняется родная таблица примера (стиль,
    заливки, линии, кегль дизайнера), а не рисуется своя поверх её места.
    Идея из PPTAgent (EMNLP 2025): референсный слайд правится на месте.

    Геометрия пересчитывается: ширина от левого края слота до правого поля
    (таблица-единственный блок не уже 0.6 холста, иначе не уже таблицы
    примера), столбцы по содержанию (`tables.column_shares`), строки по
    замеру, но не ниже высоты строки примера, пока всё влезает до нижнего
    поля. Кегль не ниже ступени caption шкалы: у примера-расписания он 9 pt,
    и наши пять строк на всю ширину читались бы с трудом. Не влезло и на
    caption: кегль остаётся caption, а решение примет аудит."""
    header, body_rows = _table_rows_within_capacity(slide_spec, pattern, slide_spec.visual.table, sole_content)
    rows = [header] + body_rows
    scheme, clr_map = profile.theme.scheme, profile.theme.clr_map

    def is_highlight(tc) -> bool:
        tc_pr = tc.find(qn("a:tcPr"))
        fill = tc_pr.find(qn("a:solidFill")) if tc_pr is not None else None
        color = resolve_color(fill, scheme, clr_map) if fill is not None else None
        return isinstance(color, Color) and _relative_luminance(color.hex) < _HIGHLIGHT_LUMINANCE

    tbl = native_table(frame)
    example_width = sum(int(gc.get("w") or 0) for gc in tbl.iter(qn("a:gridCol"))) / canvas.width_emu
    fill_native_table(frame, rows, is_highlight=is_highlight, align=_column_align(header, body_rows))

    if sole_content:
        box = _table_frame_box(slide, slot_box, profile, canvas, exclude=[frame])
    else:
        box = _table_frame_box(
            slide, slot_box, profile, canvas, exclude=[frame], min_width=0.0,
            max_right=slot_box.left + max(slot_box.width, example_width),
        )
    width_in, height_in = box.width * canvas.width_in, box.height * canvas.height_in
    col_widths_in = [width_in * share for share in column_shares(rows)]
    family = _primary_family(profile)
    caption = profile.type_scale_pt("caption", 12.0)
    styles = table_cell_styles(frame)
    own_sizes = [max((c.size_pt or _TABLE_DEFAULT_PT) for c in row) for row in styles]
    sizes = [max(s, caption) for s in own_sizes]

    def measure_rows(row_sizes: list[float]) -> list[float]:
        heights = []
        for values, row_styles, size in zip(rows, styles, row_sizes):
            tallest = 0.0
            for c, style in enumerate(row_styles):
                l_in, t_in, r_in, b_in = style.margins_in
                text = str(values[c]) if c < len(values) else ""
                metrics = measure(
                    text, style.family or family, size, max(col_widths_in[c] - l_in - r_in, 0.1),
                    line_spacing=style.line_spacing or _AUDIT_DEFAULT_LINE_SPACING,
                )
                tallest = max(tallest, metrics.height_in + t_in + b_in)
            heights.append(tallest)
        return heights

    measured = measure_rows(sizes)
    while sum(measured) > height_in and any(s > caption for s in sizes):
        sizes = [max(s * _TABLE_SHRINK, caption) for s in sizes]
        measured = measure_rows(sizes)
    set_table_text_size(frame, [s if abs(s - own) > 0.01 else None for s, own in zip(sizes, own_sizes)])

    example_rows_in = [h / 914400 for h in template_row_heights_emu(frame)]
    roomy = [max(m, e) for m, e in zip(measured, example_rows_in)]
    heights_in = roomy if sum(roomy) <= height_in else measured
    set_native_table_geometry(
        frame, box,
        [round(w * 914400) for w in col_widths_in], [round(h * 914400) for h in heights_in], canvas,
    )


# ---------------------------------------------------------------------------
# Слайды-примеры шаблона
# ---------------------------------------------------------------------------


def _clear_sample_slides(prs) -> None:
    """Убирает все слайды-примеры шаблона из `p:sldIdLst` И из связей —
    рецепт python-pptx (`Part.drop_rel` снижает счётчик ссылок на часть
    слайда; когда он доходит до нуля, сама часть и её собственные
    relationships уходят вместе с ней). Мастера/лейауты/тема/media в
    `p:sldMasterIdLst` не затрагиваются вовсе — колода остаётся тем же
    файлом шаблона, лишь без слайдов-примеров."""
    xml_slides = prs.slides._sldIdLst
    for sld in list(xml_slides):
        prs.part.drop_rel(sld.rId)
        xml_slides.remove(sld)


def _find_layout(prs, layout_id: str):
    for master in prs.slide_masters:
        for layout in master.slide_layouts:
            stem = str(layout.part.partname).rsplit("/", 1)[-1]
            if stem.endswith(".xml"):
                stem = stem[: -len(".xml")]
            if stem == layout_id:
                return layout
    return None


# ---------------------------------------------------------------------------
# Адаптеры pydantic-зеркал профиля -> датаклассы разбора
# ---------------------------------------------------------------------------


def _box_from_model(b) -> Box:
    return Box(left=b.left, top=b.top, width=b.width, height=b.height)


def _pattern_from_model(model) -> Pattern:
    """`TemplateProfile.patterns` — JSON-совместимые зеркала (`PatternModel`,
    см. `template/profile.py`), не датаклассы `template.patterns.Pattern`,
    которых просит интерфейс задачи ("Consumes: ... Pattern"). Пересборка
    дешёвая (несколько списков небольшой длины на паттерн) — заново гонять
    майнинг паттернов по пакету ради тех же самых чисел не нужно."""
    slots = [
        PatternSlot(
            role=s.role, box=_box_from_model(s.box), size_pt=s.size_pt, color_hex=s.color_hex,
            align=s.align, max_chars=s.max_chars, wraps=s.wraps, sample_text=s.sample_text,
            anchor=s.anchor, purpose=s.purpose, content_hint=s.content_hint,
            max_words=s.max_words, ordinal=s.ordinal, fixed=s.fixed,
        )
        for s in model.slots
    ]
    repeat = None
    if model.repeat is not None:
        repeat = RepeatSpec(
            axis=model.repeat.axis, count=model.repeat.count, step=model.repeat.step,
            slot_roles=list(model.repeat.slot_roles), group_size=model.repeat.group_size,
        )
    decor = [
        DecorShape(
            kind=d.kind, box=_box_from_model(d.box), rotation=d.rotation, flip_h=d.flip_h, flip_v=d.flip_v,
            fill_hex=d.fill_hex, has_fill=d.has_fill, fill_kind=d.fill_kind,
            repeat_group=d.repeat_group, repeat_index=d.repeat_index,
            image_part=d.image_part, badge_text=d.badge_text,
            badge_size_pt=d.badge_size_pt, badge_color_hex=d.badge_color_hex, prst=d.prst,
        )
        for d in model.decor
    ]
    capacity = Capacity(
        max_items=model.capacity.max_items, max_chars_per_item=model.capacity.max_chars_per_item,
        max_bullets=model.capacity.max_bullets, max_series=model.capacity.max_series,
        max_rows=model.capacity.max_rows, max_cols=model.capacity.max_cols,
    )
    return Pattern(
        pattern_id=model.pattern_id, source_slide_index=list(model.source_slide_index),
        layout_id=model.layout_id, kind=model.kind, slots=slots, repeat=repeat, decor=decor,
        capacity=capacity, score=model.score, is_dark=model.is_dark,
        kind_confidence=model.kind_confidence,
    )


def _grid_from_model(model) -> Grid:
    columns = [
        ColumnAxis(center=c.center, count=c.count, confidence=c.confidence, source=c.source)
        for c in model.columns
    ]
    return Grid(
        margin_left=model.margin_left, margin_right=model.margin_right,
        margin_top=model.margin_top, margin_bottom=model.margin_bottom,
        columns=columns, gutter=model.gutter, anchors=dict(model.anchors),
        confidence=dict(model.confidence), skipped_no_box=model.skipped_no_box,
        native_guides_used=model.native_guides_used,
    )


# ---------------------------------------------------------------------------
# Отрисовка одного слота
# ---------------------------------------------------------------------------


def _primary_family(profile: TemplateProfile) -> str:
    return profile.type_scale.families[0] if profile.type_scale.families else "Arial"


def _line_spacing_for(role_hint: str, profile: TemplateProfile) -> float:
    if role_hint in _HEADING_ROLES:
        return profile.type_scale.heading_line_spacing
    return profile.type_scale.body_line_spacing


def _relative_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast_ratio_from_luminance(l_a: float, l_b: float) -> float:
    """Контраст WCAG между двумя ОТНОСИТЕЛЬНЫМИ ЯРКОСТЯМИ (не цветами) —
    та же формула, что `naming.contrast_ratio`, но берёт готовую яркость
    напрямую: фон под слотом не всегда несёт цвет (фоновое фото даёт только
    усреднённую яркость, см. `layouts.Background.luminance`), контраст
    WCAG зависит только от яркости, не от оттенка, так что усреднённого
    значения достаточно, чтобы честно посчитать пару."""
    lighter, darker = max(l_a, l_b), min(l_a, l_b)
    return (lighter + 0.05) / (darker + 0.05)


def _best_contrast_color(candidate_hex: str, bg_luminance: float, profile: TemplateProfile) -> str:
    """Цвет текста, дающий контраст WCAG не ниже `naming.MIN_CONTRAST`
    (4.5:1) к ФАКТИЧЕСКОЙ яркости фона под слотом — Task 10 отчёт, находка
    №1: раньше решение было бинарным ("тёмный/светлый фон" ->
    `surface`/`on_surface` полюс), не численным, и не проверяло реальный
    контраст итоговой пары — раскладка, снятая со светлого примера и
    положенная на тёмный макет, несла `is_dark=False`, полюс не менялся,
    чёрный текст ложился на тёмно-фиолетовый фон.

    Если `candidate_hex` (роль, которую предложил `_color_for_role`) уже
    даёт нужный контраст — используется он, чтобы не менять цвет там, где
    и так всё читаемо (кегль/роль сохраняют смысл: kpi_value остаётся
    брендовым цветом, если он и так контрастен). Иначе — берётся цвет
    ИЗ ПАЛИТРЫ ШАБЛОНА (`profile.palette_roles`, не произвольный чёрный/
    белый — брифом: "выбирай из палитры шаблона"), дающий НАИЛУЧШИЙ
    контраст к этому фону; если даже лучший из палитры не дотягивает до
    4.5:1 (редкий случай — фон декора вне откалиброванной пары
    surface/on_surface), берётся всё равно лучший из худших — это не хуже
    прежнего поведения и не изобретает цвет вне дизайн-системы шаблона."""
    palette = list(dict.fromkeys(v for v in profile.palette_roles.values() if v))
    if not palette:
        return candidate_hex

    def contrast(hex_c: str) -> float:
        return _contrast_ratio_from_luminance(bg_luminance, _relative_luminance(hex_c))

    if candidate_hex in palette and contrast(candidate_hex) >= MIN_CONTRAST:
        return candidate_hex
    return max(palette, key=contrast)


def _contains(outer: Box, inner: Box, tolerance: float = 0.01) -> bool:
    """`inner` целиком лежит внутри `outer` (с допуском на округление) —
    геометрический тест "слот стоит на этой плашке декора"."""
    return (
        outer.left - tolerance <= inner.left
        and outer.top - tolerance <= inner.top
        and inner.left + inner.width <= outer.left + outer.width + tolerance
        and inner.top + inner.height <= outer.top + outer.height + tolerance
    )


def _plaque_under(box: Box, decor: list[DecorShape]) -> DecorShape | None:
    """Самая маленькая (самая специфичная — не подложка всего слайда)
    плашка декора паттерна, полностью содержащая `box` и несущая цвет
    заливки — кандидат на "настоящий локальный фон" под содержимым слота
    (см. `_local_background_is_dark`)."""
    candidates = [
        d for d in decor
        if d.kind == "shape" and d.has_fill and d.fill_hex and _contains(d.box, box)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda d: d.box.width * d.box.height)


def _layout_entry(profile: TemplateProfile, layout_id: str) -> LayoutEntryModel | None:
    return next((entry for entry in profile.layouts if entry.layout_id == layout_id), None)


def _local_background_luminance(
    slot_box: Box, pattern: Pattern, layout_bg_luminance: float | None,
) -> float:
    """Относительная яркость WCAG фона НЕПОСРЕДСТВЕННО под слотом.

    Порядок источников (Task 10 отчёт, находка №1 — правка по итогам
    визуального ревью v3, ЛЦТ2026, "Риски раскатки (детали)": чёрный текст
    на тёмно-фиолетовом фоне; раскладка снята со светлого слайда-примера
    (`pattern.is_dark=False`), но положена на ТЁМНЫЙ макет шаблона):

    1. Плашка декора НЕПОСРЕДСТВЕННО под слотом (`_plaque_under`), если
       она есть — текст лежит на плашке, а не на фоне слайда, фоном
       служит ОНА (находка визуального ревью v2, ЛЦТ2026, "cards": белая
       плашка поверх тёмного фона паттерна).
    2. Иначе — фон МАКЕТА, на который слайд реально ставится
       (`layout_bg_luminance`, из `profile.layouts`, посчитан надёжно: с
       учётом наследования от мастера, градиентов и фоновых фото, Task
       6/8) — ИСТОЧНИК ИСТИНЫ, не `pattern.is_dark`: раскладка снята со
       слайда-примера и может не совпадать по фону с макетом, на который
       её ставит подбор паттерна.
    3. Только если фон макета в принципе не резолвился (`None` — не
       должно случаться на профиле, разобранном с того же файла, но
       честный крайний случай) — `pattern.is_dark`, как единственный
       оставшийся сигнал."""
    plaque = _plaque_under(slot_box, pattern.decor)
    if plaque is not None and plaque.fill_hex:
        return _relative_luminance(plaque.fill_hex)
    if layout_bg_luminance is not None:
        return layout_bg_luminance
    return 0.0 if pattern.is_dark else 1.0


def _color_for_role(role_hint: str, slot: PatternSlot, profile: TemplateProfile, bg_luminance: float) -> str:
    values = set(profile.palette_roles.values())
    if slot.color_hex and slot.color_hex in values:
        return _best_contrast_color(slot.color_hex, bg_luminance, profile)
    color = profile.palette_roles.get(_ROLE_COLOR.get(role_hint, "on_surface"))
    if color:
        return _best_contrast_color(color, bg_luminance, profile)
    return next(iter(values)) if values else "#000000"


# ---------------------------------------------------------------------------
# Наложение слота на декор раскладки
# ---------------------------------------------------------------------------

# Порог "площадь пересечения уже значимая, а не игра округления" — доля
# площади МЕНЬШЕЙ из двух фигур (тот же принцип, что `_FIT_TOLERANCE_IN`/
# `_LINE_THICKNESS_SHARE` в этом файле: округлая отсечка на порядок больше
# погрешности вычислений с плавающей точкой, не подгонка под конкретный
# файл). 10% площади меньшей фигуры — уже заметное на глаз наложение
# (буквы текста реально ложатся на чужой декор), пересечение на доли
# процента — то же самое, что перекрытие рамок при округлении EMU.
_OVERLAP_MIN_AREA_SHARE = 0.1


def _overlap_ratio(a: Box, b: Box) -> float:
    ix = max(0.0, min(a.left + a.width, b.left + b.width) - max(a.left, b.left))
    iy = max(0.0, min(a.top + a.height, b.top + b.height) - max(a.top, b.top))
    inter = ix * iy
    if inter <= 0.0:
        return 0.0
    return inter / min(a.width * a.height, b.width * b.height)


def _colliding_decor(box: Box, decor: list[DecorShape]) -> DecorShape | None:
    """Первая закрашенная фигура декора, которая ЧАСТИЧНО перекрывает
    `box` площадью выше `_OVERLAP_MIN_AREA_SHARE` — коллизия, которую
    нужно чинить (Task 10 отчёт, находка №2: заголовок налез на плашку
    декора). ПОЛНОЕ содержание (`_contains` в любую сторону) — не
    коллизия, а легитимный случай "слот стоит на своей плашке-фоне"
    (тот же тест, что уже применяет `_plaque_under` для контраста)."""
    for shape in decor:
        if not shape.has_fill:
            continue
        if _contains(shape.box, box) or _contains(box, shape.box):
            continue
        if _overlap_ratio(box, shape.box) > _OVERLAP_MIN_AREA_SHARE:
            return shape
    return None


def _avoid_decor_overlap(box: Box, decor: list[DecorShape], grid: Grid) -> Box:
    """Сдвигает `box`, если он значимо наезжает на декор раскладки — ТЗ
    (Task 10 отчёт, находка №2): "перед тем как положить текст в слот,
    проверь, не занято ли это место декором... если занято — сдвигай".
    Пробует по очереди: вниз (очистить нижний край мешающей фигуры),
    вправо, влево — первый сдвиг, который снимает коллизию и остаётся в
    полях шаблона, побеждает. Если ни один не помогает (декор шире всего
    слота целиком) — возвращает `box` как есть, это честный крайний
    случай, не более скрытый, чем прежнее поведение (наложение оставалось
    всегда)."""
    blocking = _colliding_decor(box, decor)
    if blocking is None:
        return box
    d = blocking.box
    margin_bottom_limit = 1.0 - grid.margin_bottom
    margin_right_limit = 1.0 - grid.margin_right

    below = replace(box, top=d.top + d.height)
    if below.top + below.height <= margin_bottom_limit + 0.001 and _colliding_decor(below, decor) is None:
        return below

    right = replace(box, left=d.left + d.width)
    if right.left + right.width <= margin_right_limit + 0.001 and _colliding_decor(right, decor) is None:
        return right

    left = replace(box, left=max(grid.margin_left, d.left - box.width))
    if left.left >= grid.margin_left - 0.001 and _colliding_decor(left, decor) is None:
        return left

    return box


def _shrink_sequence(profile: TemplateProfile, slot_size_pt: float) -> list[float]:
    """Кегли-кандидаты по убыванию, начиная с собственного кегля слота,
    затем ступени `TypeScale` не выше него и не ниже "caption" (брифом: "не
    ниже подписи"). И `PatternSlot.size_pt`, и `TypeScale.steps` нормированы
    к эталонному холсту 13.333″ (`Canvas.norm` — та же нормировка, что и в
    `typography.py`/`patterns._shape_dominant_size`: `sz_raw/100*canvas.
    norm`), а рисовать нужно РЕАЛЬНЫЙ кегль ЭТОГО холста — денормируем оба
    источника одним и тем же коэффициентом (`TemplateProfile.denorm_pt`/
    `type_scale_pt`, единственное место в проекте, которое имеет право
    делить нормированный кегль на `canvas_norm`) один раз здесь, а не
    порознь у вызывающего."""
    size_pt = profile.denorm_pt(slot_size_pt)
    raw_steps = {name: profile.type_scale_pt(name, 0.0) for name in _SHRINK_STEPS}
    caption_pt = raw_steps["caption"]

    seq = [size_pt]
    for name in _SHRINK_STEPS:
        value = raw_steps[name]
        if 0 < value < seq[-1] - 0.05:
            seq.append(value)
    if caption_pt > 0 and abs(seq[-1] - caption_pt) > 0.05:
        seq.append(caption_pt)
    return seq


def _joined_text(paragraphs: list[Paragraph]) -> str:
    return "\n".join(p.text for p in paragraphs)


def _split_back(text: str, original: list[Paragraph]) -> list[Paragraph]:
    bullet_flags = [p.bullet for p in original]
    bold_flags = [p.bold for p in original]
    last_flag = bullet_flags[-1] if bullet_flags else False
    lines = text.split("\n")
    return [
        Paragraph(
            line,
            bullet=(bullet_flags[i] if i < len(bullet_flags) else last_flag),
            bold=(bold_flags[i] if i < len(bold_flags) else False),
        )
        for i, line in enumerate(lines)
    ]


# Порог "усечение ещё косметическое, а не разрушительное" — доля исходной
# длины текста (в знаках, без многоточия), которую усечение ОБЯЗАНО
# сохранить, иначе оно не применяется вовсе (см. `_is_cosmetic_truncation`).
# Находка визуального ревью (отчёт задачи): «Доработка для закупок…» на
# VK Tech сохраняла ~40% исходной фразы и уже читалась как брак, а «…» без
# единого слова содержания — 0%. 0.7 — обоснование: усечение, вырезающее
# МЕНЬШЕ 30% знаков, типично отрезает только хвостовое уточнение/придаточное
# (для этих коротких карточных фраз главное — подлежащее+сказуемое —
# статистически укладывается в первые 60-70% предложения), смысл остаётся
# читаемым. Усечение, которое вырезало бы больше — уже не "подрезали хвост",
# а "переписали контент огрызком" — хуже, чем оставить текст целиком и
# позволить ему видимо вылезти за рамку (это поймает будущий аудит/
# песочница, Task 10a, а не тихо спрятанный обрубок).
_COSMETIC_TRUNCATION_MIN_RETAINED = 0.7


def _is_cosmetic_truncation(original: str, truncated: str) -> bool:
    """Сохраняет ли `truncated` (результат `_truncate_to_fit`, включая
    завершающее «…», если оно есть) не меньше `_COSMETIC_TRUNCATION_MIN_
    RETAINED` доли исходной длины `original` (в знаках)."""
    if not original:
        return True
    prefix = truncated[:-1] if truncated.endswith("…") else truncated
    return len(prefix) / len(original) >= _COSMETIC_TRUNCATION_MIN_RETAINED


def _truncate_to_fit(
    text: str, family: str, size_pt: float, box_width_in: float, box_height_in: float, line_spacing: float,
) -> tuple[str, bool]:
    """Самый длинный префикс, который ещё влезает по высоте — с многоточием
    на месте обрыва (брифом: "усекай, но оставь след", не тихий обрыв
    слова).

    Границы абзацев СОХРАНЯЮТСЯ. Раньше усечение резало текст по словам
    всего блока сразу, схлопывая его в одну строку, — и это было объявлено
    «честным упрощением аварийного пути». Живой прогон 25 сентября 2026 на
    VK WorkSpace показал цену: четыре отдельных факта («71 заявка ушла не
    тому согласующему», «48 заявок попали к сотруднику в отпуске»…) слиплись
    в одно нечитаемое предложение без единого разделителя. Слипшийся текст
    хуже, чем отброшенный хвост: читатель видит бессмыслицу вместо
    сокращения.

    Целые абзацы берутся, пока влезают; первый не влезший режется по словам
    и получает многоточие; остальные отбрасываются.

    Возвращает КАНДИДАТА на усечение и было ли оно вообще нужно — не
    решает, принять ли его: разрушительное усечение (меньше
    `_COSMETIC_TRUNCATION_MIN_RETAINED` исходной длины, включая усечение до
    голого «…») ОТКЛОНЯЕТ вызывающий код (`_draw_slot`,
    `_is_cosmetic_truncation`) — пустая/усечённая-до-точек карточка хуже
    видимого переполнения (находка визуального ревью, отчёт задачи)."""
    metrics = measure(text, family, size_pt, box_width_in, line_spacing=line_spacing)
    if metrics.height_in <= box_height_in + _FIT_TOLERANCE_IN:
        return text, False

    def fits(candidate: str) -> bool:
        m = measure(candidate, family, size_pt, box_width_in, line_spacing=line_spacing)
        return m.height_in <= box_height_in + _FIT_TOLERANCE_IN

    paragraphs = text.split("\n")
    kept: list[str] = []
    for i, para in enumerate(paragraphs):
        candidate = "\n".join([*kept, para])
        last = i == len(paragraphs) - 1
        if fits(candidate if last else candidate + "…"):
            kept.append(para)
            continue

        # Этот абзац целиком не влезает — режем ЕГО по словам, остальные
        # отбрасываем.
        words = para.split()
        lo, hi, best = 0, len(words), ""
        while lo <= hi:
            mid = (lo + hi) // 2
            head = " ".join(words[:mid])
            marked = head + "…" if (mid < len(words) or not last) else head
            if fits("\n".join([*kept, marked]) if kept else marked):
                best = marked
                lo = mid + 1
            else:
                hi = mid - 1
        if best:
            kept.append(best)
        break

    result = "\n".join(kept)
    return (result or "…"), True


def _apply_bullet(paragraph, bullet_char: str, family: str) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    for tag in ("a:buChar", "a:buAutoNum", "a:buNone"):
        el = p_pr.find(qn(tag))
        if el is not None:
            p_pr.remove(el)
    bu_font = etree.SubElement(p_pr, qn("a:buFont"))
    bu_font.set("typeface", family)
    bu_char = etree.SubElement(p_pr, qn("a:buChar"))
    bu_char.set("char", bullet_char)


def _draw_slot(
    slide, slide_spec: SlideSpec, content: SlotContent, profile: TemplateProfile, family: str,
    bullet_char: str, canvas_width_emu: int, canvas_height_emu: int, bg_luminance: float,
) -> None:
    box = content.slot.box
    left = round(box.left * canvas_width_emu)
    top = round(box.top * canvas_height_emu)
    width = max(1, round(box.width * canvas_width_emu))
    height = max(1, round(box.height * canvas_height_emu))

    textbox = slide.shapes.add_textbox(Emu(left), Emu(top), Emu(width), Emu(height))
    tf = textbox.text_frame
    tf.word_wrap = True
    # python-pptx даёт текстовой рамке ненулевые поля по умолчанию (0.1"
    # слева/справа, 0.05" сверху/снизу, OOXML `a:bodyPr` lIns/rIns/tIns/
    # bIns) — `measure()` меряет по ПОЛНОЙ ширине/высоте фигуры (см.
    # `box_width_in`/`box_height_in` ниже), без вычета полей. Найдено на
    # повторном визуальном ревью (после установки Play): заголовок стал
    # переноситься на строку больше, чем предсказал замер, и наезжать на
    # содержимое ниже — бокс, который меряет `measure()`, обязан совпадать
    # с боксом, в который реально льётся текст у PowerPoint/LibreOffice.
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = Emu(0)
    # Вертикальное выравнивание — то, что стояло в шаблоне. Без него текст
    # липнет к верху рамки: на карточных раскладках рамка высокая по
    # замыслу, текста две строки, и три четверти карточки пустуют (живой
    # рендер 25 сентября 2026, VK Tech, четыре карточки).
    anchor = _ANCHOR_MAP.get(content.slot.anchor)
    if anchor is not None:
        tf.vertical_anchor = anchor

    line_spacing = _line_spacing_for(content.role_hint, profile)
    box_width_in = width / EMU_PER_INCH
    box_height_in = height / EMU_PER_INCH

    full_text = _joined_text(content.paragraphs)
    sizes = _shrink_sequence(profile, content.slot.size_pt)

    chosen_size = sizes[-1]
    chosen_text = full_text
    fit_found = False
    for size_pt in sizes:
        metrics = measure(full_text, family, size_pt, box_width_in, line_spacing=line_spacing)
        if metrics.height_in <= box_height_in + _FIT_TOLERANCE_IN:
            chosen_size = size_pt
            fit_found = True
            break

    if not fit_found:
        truncated_text, truncated = _truncate_to_fit(
            full_text, family, chosen_size, box_width_in, box_height_in, line_spacing,
        )
        if truncated and _is_cosmetic_truncation(full_text, truncated_text):
            chosen_text = truncated_text
            slide_spec.findings.append(
                f"Слайд {slide_spec.index}: текст слота «{content.role_hint}» усечён — "
                f"не влезает даже кеглем подписи ({chosen_size:.1f}pt)."
            )
        elif truncated:
            # Усечение вырезало бы больше _COSMETIC_TRUNCATION_MIN_RETAINED
            # исходного текста (в пределе — до голого «…» или пустоты) —
            # находка ревью: пустая/усечённая-до-точек карточка хуже
            # переполненной. Оставляем текст ЦЕЛИКОМ на минимальном кегле:
            # он видимо вылезет за рамку слота, зато не потеряет смысл, и
            # это честно ловит finding, а не молча прячет контент.
            slide_spec.findings.append(
                f"Слайд {slide_spec.index}: текст слота «{content.role_hint}» не помещается даже "
                f"кеглем подписи ({chosen_size:.1f}pt), а усечение вырезало бы больше "
                f"{(1 - _COSMETIC_TRUNCATION_MIN_RETAINED):.0%} содержания — оставлен целиком "
                "(слот переполнен, эта раскладка не подходит для этого содержания)."
            )

    display_paragraphs = _split_back(chosen_text, content.paragraphs)
    color_hex = _color_for_role(content.role_hint, content.slot, profile, bg_luminance)
    align = _ALIGN_MAP.get(content.slot.align, PP_ALIGN.LEFT)
    bold = profile.type_scale.bold_is_idiomatic and content.role_hint in _HEADING_ROLES

    for i, para in enumerate(display_paragraphs):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        # Интерлиньяж записывается В ФАЙЛ тем же значением, которым мерили
        # текст выше.
        #
        # Раньше сборка мерила заголовок интерлиньяжем заголовков шаблона
        # (`type_scale.heading_line_spacing`, у VK Education 0.9), а в XML
        # ничего не писала. Аудит читает `a:lnSpc` из файла и, не найдя,
        # берёт типографскую норму 1.2 — тот же заголовок у него «не
        # помещается в рамку» (0.80″ против доступных 0.70″). Раскладка
        # отклонялась циклом сборки, слайд уезжал на запасную, и на
        # контрольном прогоне 25 сентября 2026 половина слайдов вышла с
        # заголовком кеглем текста вместо заголовочного.
        #
        # Расхождение сборщика с аудитом — ровно то, против чего заведён
        # единый замер (`compose.textfit.measure`): мерили одинаково, а
        # исходные данные для замера брали разные.
        p.line_spacing = line_spacing
        run = p.add_run()
        run.text = para.text
        run.font.size = Pt(chosen_size)
        run.font.name = family
        run.font.bold = bold or para.bold
        run.font.color.rgb = RGBColor.from_string(color_hex.lstrip("#"))
        if para.bullet:
            _apply_bullet(p, bullet_char, family)


# ---------------------------------------------------------------------------
# Путь сохранения
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^0-9a-zA-Zа-яА-ЯёЁ]+")


def _output_path(spec: DeckSpec, template_path: Path, variant: Variant) -> Path:
    try:
        settings = Settings.load(APP_YAML_PATH)
        base_dir = settings.paths.artifacts
        if not base_dir.is_absolute():
            base_dir = APP_YAML_PATH.parents[1] / base_dir
    except Exception:
        base_dir = APP_YAML_PATH.parents[1] / "artifacts"

    slug = _SLUG_RE.sub("-", spec.title).strip("-").lower() or "deck"
    template_slug = _SLUG_RE.sub("-", template_path.stem).strip("-").lower() or "template"
    return base_dir / f"{slug}__{template_slug}__{variant.value}.pptx"
