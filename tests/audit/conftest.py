"""Общая инфраструктура тестов Task 11 (детерминированный аудит).

`PROFILE`/`TEMPLATE` — тот же файл и тот же приём, что и `tests/compose/
test_builder.py` (VK Tech, 20 паттернов: bullets×11, cards×4, two_col×3,
section×2). `CLEAN_SPEC`/`clean_deck` — заведомо ЧИСТЫЙ (без единой
находки по всем 24 проверкам, см. `test_clean_deck_produces_no_findings`)
однослайдовый deck, поверх которого большинство тестов ОДНОЙ проверки
добавляют РОВНО один дефект (`deck_with`) — так исключается риск, что
тестовый дефект случайно зацепит соседнюю проверку (пустая рамка задевает
"пустой слайд", лишняя фигура задевает наложение и т.д.).

`CLEAN_SPEC` намеренно не берёт "section" (слайд-разделитель с ОДНИМ
заголовком и без остального содержания) — легитимный по дизайну, но
неотличимый от находки I03 чисто по файлу без плана; секционные слайды
устройство I03 явно допускает по кеглю (см. докстроку `_check_I03`), но
заводить эту двусмысленность в единственный "образцовый чистый" слайд тестов
незачем."""
from __future__ import annotations
import uuid
from pathlib import Path

import pytest
from pptx import Presentation

from deckforge.audit.config import AuditConfig
from deckforge.compose.builder import Variant, build_deck
from deckforge.plan.spec import BulletBlock, DeckSpec, SlideSpec
from deckforge.template.profile import TemplateProfile

TEMPLATES_DIR = Path("dataset/templates")
TEMPLATE = TEMPLATES_DIR / "VK Tech шаблон.pptx"
PROFILE = TemplateProfile.from_file(TEMPLATE, cache_dir=None)
# Порог заполненности ниже, чем в `config/audit.yaml`: чистая колода без
# декора заполнена на 21% (см. докстроку `clean_deck_path`), а тестам нужна
# колода без единой находки. Нижнюю границу D05 проверяет свой тест на
# заведомо пустом слайде, ему 0.15 хватает с запасом.
def _test_config():
    loaded = AuditConfig.load()
    return loaded.model_copy(update={"density": loaded.density.model_copy(update={"fill_ratio_min": 0.15})})


CONFIG = _test_config()

# Макет шаблона без единого плейсхолдера (разведано: `slideLayout18.xml`,
# имя "DEFAULT") — слайды на нём начинаются пустыми, ничего постороннего не
# наследуется, что важно для тестов, которым нужен слайд ровно с одним
# намеренным содержимым (таблица/график/картинка/дубликат).
BLANK_LAYOUT_PART = "ppt/slideLayouts/slideLayout18.xml"

# Списочная раскладка VK Tech со списком в правой половине (см. докстроку
# `clean_deck_path`).
_CLEAN_PATTERN = "slide9"

CLEAN_SPEC = DeckSpec(
    title="Аудит — чистая колода",
    language="ru",
    slides=[
        SlideSpec(
            index=0, kind="bullets", pattern_id=_CLEAN_PATTERN,
            headline="Где уходит время на согласование заявок",
            blocks=[
                BulletBlock(items=[
                    "Ожидание первого согласующего — медиана 18 часов по данным марта-мая",
                    "Ожидание второго согласующего — медиана 11 часов, часто дольше вечером",
                    "Чистая работа людей — всего 28 минут из общего цикла в 31 час",
                    "Повторные согласования из-за неполных заявок — до 20% случаев",
                    "Ожидание ответственного из другого подразделения — медиана 6 часов",
                ]),
            ],
            source_note="Источник: замер 1240 заявок, март-май 2026",
        ),
    ],
)


def _blank_layout(prs):
    for master in prs.slide_masters:
        for layout in master.slide_layouts:
            if str(layout.part.partname).lstrip("/") == BLANK_LAYOUT_PART:
                return layout
    raise AssertionError(f"макет {BLANK_LAYOUT_PART!r} не найден в шаблоне")


def _clear_sample_slides(prs) -> None:
    """Тот же рецепт, что `compose.builder._clear_sample_slides` (не
    импортируется — приватная деталь соседнего модуля, здесь всего три
    строки)."""
    xml_slides = prs.slides._sldIdLst
    for sld in list(xml_slides):
        prs.part.drop_rel(sld.rId)
        xml_slides.remove(sld)


@pytest.fixture(scope="session")
def profile_fixture():
    return PROFILE


@pytest.fixture
def clean_deck_path() -> Path:
    """Заведомо чистая колода для тестов, которые сами вносят РОВНО ОДИН
    дефект и проверяют, что аудит нашёл ровно его.

    Декоративные картинки из профиля вырезаются. С 25 сентября 2026 сборка
    переносит фирменную графику шаблона на слайд (иконки, орнаменты — см.
    `compose.decor._add_picture`), и это правильно для продукта, но ломает
    именно ЭТУ фикстуру: тест кладёт свой блок в произвольное место холста,
    задевает краем иконку и получает вдобавок L02 (наложение). Проверяется
    при этом плотность/типографика, а не композиция.

    Вырезается весь декор, а раскладка задана явно (`_CLEAN_PATTERN`,
    список справа, левая половина холста свободна под блоки тестов). До
    27 сентября 2026 колода без явного выбора садилась на слайд-пример с
    кодом (slide31: список справа, слева пусто), тесты писались под эту
    геометрию. Слайды с примером кода больше не раскладки, ближайшая по
    геометрии slide9 несёт плашку слева, ровно там, куда тесты кладут свои
    блоки: без плашки L02 не мешает, а заполненность 21% ниже порога D05
    только для `CONFIG` из `config/audit.yaml`; тесты берут `CONFIG`
    с порогом 0.15 (см. ниже), сам порог D05 проверяет отдельный тест на
    заведомо пустом слайде. Тесты про плашки добавляют свои плашки сами.

    Сборка идёт с нуля (`clone_examples=False`): с тех пор как клон
    перебирается у всех кандидатов раньше сборки с нуля, слайд вставал
    клоном второй раскладки, а клон несёт графику примера целиком, мимо
    вырезанного выше декора (полноэкранная картинка, D05 и L06). Фикстуре
    нужна колода без находок, а не лучший слайд продукта."""
    profile = PROFILE.model_copy(update={
        "patterns": [
            p.model_copy(update={"decor": []})
            for p in PROFILE.patterns
        ],
    })
    return build_deck(CLEAN_SPEC, profile, TEMPLATE, Variant.dense, clone_examples=False)


@pytest.fixture
def deck_with(tmp_path, clean_deck_path):
    """`deck_with(fn, slide_index=0) -> Path` — открывает заведомо чистую
    колоду (`CLEAN_SPEC`), применяет `fn(slide)` (добавляет/меняет ровно
    один дефект средствами `python-pptx`) и сохраняет копию во временный
    каталог теста."""
    def _make(fn, *, slide_index: int = 0) -> Path:
        prs = Presentation(str(clean_deck_path))
        fn(prs.slides[slide_index])
        out = tmp_path / f"mut-{uuid.uuid4().hex[:8]}.pptx"
        prs.save(str(out))
        return out
    return _make


@pytest.fixture
def blank_deck(tmp_path):
    """`blank_deck() -> Presentation` — новая презентация НА ФАЙЛЕ шаблона
    (мастера/лейауты/тема родные — T04 не сработает случайно), без единого
    слайда-примера; `.save_as(name)` возвращает `Path` для передачи в
    `run_deterministic`. Тест сам добавляет слайд(ы) на `_blank_layout`
    (0 плейсхолдеров) — ничего постороннего не наследуется."""
    class _Deck:
        def __init__(self) -> None:
            self.prs = Presentation(str(TEMPLATE))
            _clear_sample_slides(self.prs)

        def add_slide(self):
            return self.prs.slides.add_slide(_blank_layout(self.prs))

        def save_as(self, name: str) -> Path:
            out = tmp_path / name
            self.prs.save(str(out))
            return out

    return _Deck()


@pytest.fixture
def foreign_deck(tmp_path):
    """Колода на СОВСЕМ ДРУГОМ (стоковом `python-pptx`) шаблоне — ни один
    её макет не входит в `PROFILE.layouts` (VK Tech) по построению, нужна
    T04."""
    def _make(fn) -> Path:
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])  # "Blank", 0 плейсхолдеров на слайде
        fn(slide)
        out = tmp_path / f"foreign-{uuid.uuid4().hex[:8]}.pptx"
        prs.save(str(out))
        return out
    return _make
