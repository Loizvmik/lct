"""Встраивание фотографий пользователя (Task 20) — контент-пакет может
нести не только `brief.md`/`sources.md`, но и сами фотографии, и это
модуль отвечает за ДВЕ вещи: прочитать их (`load_content_pack_photos`) и
решить, какая фотография какому слайду достанется (`assign_photos`).

**Это не генерация картинок моделью** (та задача — отдельная, со
звёздочкой): фотографии уже существуют, приходят от пользователя внутри
контент-пакета, а модель здесь только РАСПРЕДЕЛЯЕТ уже готовые файлы по
уже написанным слайдам — по одному вызову на всю презентацию (не на
слайд, см. докстроку `assign_photos`), тем же принципом "модель
предлагает, код проверяет", что и `outline.build_outline`/`writer.pick_
patterns`: имя фотографии, которого не было в списке присланных, код
отвергает; одна фотография на двух слайдах разом — тоже (см. `assign_
photos`).

**Архитектурная граница (бриф задачи, "plan/ не импортирует python-pptx и
не знает координат")**: `ContentPhoto.path` — путь к файлу на диске, не
байты и не пиксельные размеры — их читает `compose.builder` в момент
вставки (тот же модуль, что уже вписывает ассеты ШАБЛОНА в слот с
сохранением пропорций, см. `_place_picture_visual`). Этот модуль ни разу
не открывает сам файл фотографии — только имя, путь и (если есть) подпись
рядом, ровно то, что докстрока `plan.spec.Visual.photo_name` называет
"содержанием, не пикселями"."""
from __future__ import annotations
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from deckforge.plan.spec import DeckSpec, SlideSpec, Visual
from deckforge.provider.base import LLMProvider

AGENT_PATH = Path(__file__).resolve().parents[3] / "agents" / "photo-picker" / "AGENT.md"

# Только форматы, которые умеет вставлять библиотека сборки (бриф задачи,
# "Что сделать", п.1) — `python-pptx`/Pillow читают и то, и другое без
# конвертации; всё остальное (подписи `.txt`, случайный мусор в каталоге)
# молча пропускается перечислением, а не падением.
_PHOTO_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})

# Один вызов модели на ВСЮ колоду (бриф задачи, "Время": "распределение
# фотографий — одно обращение к модели на презентацию, не на слайд") — в
# отличие от `writer.write_slides`/`pick_patterns` (по вызову на слайд),
# здесь модели сразу показывают все кандидаты-слайды и все фотографии —
# бюджет ответа больше, чем у `_PICKER_SCHEMA` (там один `pattern_id`),
# но по числу сетевых кругов сама операция многократно дешевле, чем писать
# текст слайдов.
PHOTO_PICKER_MAX_TOKENS = 2048


@dataclass(frozen=True)
class ContentPhoto:
    """Одна фотография контент-пакета. `name` — имя файла (с расширением)
    ровно как оно лежит на диске: единственный идентификатор, по которому
    модель ссылается на фотографию в ответе и по которому `compose.builder`
    находит байты для вставки (словарь `name -> path`, собранный вызывающим
    кодом, см. докстроку `assign_photos`) — бриф задачи прямо просит
    сохранить имя файла как подсказку о содержимом ("имя файла — подсказка
    о содержимом, её стоит сохранить"). `caption` — короткая подпись, если
    рядом лежит файл `<имя без расширения>.txt` (бриф: "если рядом лежит
    подпись — тем более"), иначе `None`."""
    name: str
    path: Path
    caption: str | None = None


@dataclass(frozen=True)
class PhotoAssignmentReport:
    """Честный отчёт о распределении — бриф задачи, "Требования к
    работе"/"Обязательная проверка" повторяют по всему проекту одно и то
    же правило: молчаливая тишина хуже отсутствия. `notes` — человекочитаемые
    строки о том, что произошло и почему (нет модели, слайд без слота под
    фото, фотография вне списка и т.п.) — вызывающий код (`cli.py`) печатает
    их так же честно, как печатает пропуск модельного аудита по картинке."""
    assigned: dict[int, str] = field(default_factory=dict)  # slide.index -> ContentPhoto.name
    unused_photos: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def placed_count(self) -> int:
        return len(self.assigned)


def load_content_pack_photos(pack_dir: Path) -> list[ContentPhoto]:
    """Фотографии контент-пакета — каталог `<pack_dir>/photos/`, если он
    есть (контент-пакет без фотографий — обычный случай, `fixtures/content-
    packs/*` до этой задачи, см. докстроку модуля про честную деградацию
    "без ключа/без фото — как раньше"). Файлы читаются в отсортированном
    порядке (детерминизм вызова модели ниже — один и тот же контент-пакет
    должен давать один и тот же payload между прогонами)."""
    photos_dir = pack_dir / "photos"
    if not photos_dir.is_dir():
        return []

    photos = []
    for path in sorted(photos_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _PHOTO_EXTENSIONS:
            continue
        caption_path = path.with_suffix(".txt")
        caption = None
        if caption_path.is_file():
            text = caption_path.read_text(encoding="utf-8").strip()
            caption = text or None
        photos.append(ContentPhoto(name=path.name, path=path, caption=caption))
    return photos


def _load_agent_prompt(path: Path = AGENT_PATH) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        raise ValueError(f"{path}: ожидался YAML-фронтматтер, ограниченный `---`")
    meta = yaml.safe_load(parts[1]) or {}
    return meta, parts[2].strip()


_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "photo": {"type": "string"},
                    "slide_index": {"type": "integer"},
                },
                "required": ["photo", "slide_index"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["assignments"],
    "additionalProperties": False,
}


def _slide_already_has_visual_slot(slide: SlideSpec) -> bool:
    """Слайд уже занял свой (единственный) визуальный слот чем-то другим —
    таблицей/графиком (`compose.builder._place_visual` рисует РОВНО ОДИН
    визуал на слайд, `if/elif` по `visual.kind`, см. её докстроку) или
    иконкой (иной физический размер и роль, чем фото, см. `plan.spec.
    Visual.kind` — контент-пакет несёт фотографии, не пиктограммы). Слайд с
    `visual.kind == "photo"` — НЕ занят в этом смысле: это уже слайд,
    просивший фото/мокап (slide-writer, `agents/slide-writer/AGENT.md`),
    ровно тот случай, где подстановка фотографии пользователя вместо
    ассета шаблона наиболее естественна."""
    if slide.visual is None:
        return False
    return slide.visual.kind in ("table", "chart", "icon")


def _content_summary(slide: SlideSpec) -> str:
    """Короткая сводка содержания слайда для модели — заголовок обычно
    достаточен, чтобы понять тему, но не всегда (пустой заголовок-вывод
    редкость, но подзаголовок/первая строка текста добавляют смысла без
    раздувания payload на весь текст слайда)."""
    parts = [slide.headline]
    if slide.subhead:
        parts.append(slide.subhead)
    return " — ".join(p for p in parts if p)


def _candidate_slides(deck: DeckSpec) -> list[SlideSpec]:
    """Слайды, которым физически МОЖЕТ достаться фотография — ЛЮБОЙ слайд,
    чей единственный визуальный слот ещё не занят таблицей/графиком/иконкой
    (см. `_slide_already_has_visual_slot`). Не сужено до "только у кого уже
    kind=photo_text" НАРОЧНО (Task 20 брифом дословно: "сервис сам решает,
    на какой слайд её поставить" — решение по смыслу, а не только среди
    слайдов, которые writer уже заранее пометил под фото): `plan.variants.
    apply_variant._compatible_kinds` САМА переводит слайд с `visual.kind ==
    "photo"` в `photo_text`/`image`, КАКИМ БЫ ни был исходный `kind`, если
    в содержании нет карточек/KPI/таблицы/цитаты (см. её докстроку) — этот
    модуль не обязан заранее знать, попадёт ли фото в реальный слот
    раскладки: если раскладка не найдётся, `compose.builder._place_picture_
    visual` честно допишет находку (бриф, п.3), а не промолчит."""
    return [s for s in deck.slides if not _slide_already_has_visual_slot(s)]


def assign_photos(
    deck: DeckSpec, photos: list[ContentPhoto], llm: LLMProvider | None,
) -> tuple[DeckSpec, PhotoAssignmentReport]:
    """Распределяет `photos` по слайдам `deck` — один вызов модели на всю
    колоду (см. `PHOTO_PICKER_MAX_TOKENS`), не по одному на слайд.

    Без фотографий вовсе — деку не за что трогать, пустой отчёт без единой
    находки (обычный случай контент-пакета без `photos/`, см. `load_
    content_pack_photos`). Без модели (бриф, "Ограничения": "без ключа
    работает по-старому") или если ни один слайд не может принять фото
    (`_candidate_slides` пуст) — фотографии не распределяются, но отчёт
    честно называет причину (бриф, п.4: "сказать, если фотографии пришли,
    а на слайдах их нет" — не только когда модель не нашла соответствия,
    но и когда распределять было физически не на что/нечем).

    Проверки ответа модели (бриф, п.2: "модель предлагает, код проверяет"):
    - `photo` обязан быть именем файла из присланного списка — иначе
      отклонено;
    - `slide_index` обязан быть индексом одного из слайдов-кандидатов —
      иначе отклонено;
    - фотография не ставится на два слайда разом (первое предложение
      побеждает, повтор отклонён) — бриф дословно: "одна фотография не
      ставится на два слайда, если это не осмысленно"; "осмысленно" здесь
      код не умеет отличить от случайного повтора, поэтому трактует ЛЮБОЙ
      повтор как не осмысленный (та же осторожность, что и у `pick_
      patterns`: ответ вне ожидаемой формы код отвергает, а не гадает);
    - на один слайд — не больше одной фотографии (второе предложение для
      уже занятого слайда отклонено) — `Visual` несёт ровно одно
      `photo_name`, второй одновременно физически некуда положить."""
    if not photos:
        return deck, PhotoAssignmentReport()

    candidates = _candidate_slides(deck)
    if not candidates:
        return deck, PhotoAssignmentReport(
            unused_photos=[p.name for p in photos],
            notes=[
                f"Пришло {len(photos)} фотографий контент-пакета, но ни один слайд не "
                "может принять фото (визуальный слот каждого слайда уже занят таблицей/"
                "графиком/иконкой) — фотографии не размещены."
            ],
        )

    if llm is None:
        return deck, PhotoAssignmentReport(
            unused_photos=[p.name for p in photos],
            notes=[
                f"Пришло {len(photos)} фотографий контент-пакета, но нет модели (нет "
                "ключа/секретов) — распределение фото по слайдам не выполнялось, как и "
                "остальные роли модели в этом прогоне."
            ],
        )

    _meta, prompt_body = _load_agent_prompt()
    payload = {
        "slides": [
            {"index": s.index, "kind": s.kind, "content": _content_summary(s)}
            for s in candidates
        ],
        "photos": [{"name": p.name, "caption": p.caption} for p in photos],
    }
    messages = [
        {"role": "system", "content": prompt_body},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    assignments: dict[int, str] = {}
    notes: list[str] = []
    try:
        raw = llm.complete(messages, schema=_SCHEMA, max_tokens=PHOTO_PICKER_MAX_TOKENS)
        data = json.loads(raw)
        raw_assignments = data["assignments"]
        if not isinstance(raw_assignments, list):
            raise ValueError(f"'assignments' должен быть списком, получено {type(raw_assignments).__name__}")

        valid_names = {p.name for p in photos}
        valid_indices = {s.index for s in candidates}
        for item in raw_assignments:
            if not isinstance(item, dict):
                continue
            photo_name = item.get("photo")
            slide_index = item.get("slide_index")
            if photo_name not in valid_names:
                notes.append(f"Модель предложила фотографию {photo_name!r} вне присланного списка — отклонено.")
                continue
            if slide_index not in valid_indices:
                notes.append(
                    f"Модель поставила фотографию {photo_name!r} на слайд {slide_index!r}, который не "
                    "может принять фото — отклонено."
                )
                continue
            if photo_name in assignments.values():
                notes.append(
                    f"Фотография {photo_name!r} предложена больше чем на один слайд — оставлена на "
                    "первом предложенном, повтор отклонён."
                )
                continue
            if slide_index in assignments:
                notes.append(
                    f"На слайд {slide_index} модель предложила больше одной фотографии — "
                    f"оставлена первая, {photo_name!r} отклонена."
                )
                continue
            assignments[slide_index] = photo_name
    except Exception as exc:
        notes.append(
            f"Распределение фотографий не выполнено (сбой вызова/разбора ответа модели: {exc}) "
            "— фотографии не размещены."
        )

    if not assignments:
        notes.append(f"Пришло {len(photos)} фотографий контент-пакета, ни одна не была поставлена ни на один слайд.")

    photos_by_name = {p.name: p for p in photos}
    new_slides = []
    for slide in deck.slides:
        photo_name = assignments.get(slide.index)
        if photo_name is None:
            new_slides.append(slide)
            continue
        photo = photos_by_name[photo_name]
        if slide.visual is not None:
            new_visual = replace(
                slide.visual, kind="photo", photo_name=photo.name,
                caption=slide.visual.caption or photo.caption,
            )
        else:
            new_visual = Visual(kind="photo", caption=photo.caption, photo_name=photo.name)
        new_slides.append(replace(slide, visual=new_visual))

    unused = [name for name in photos_by_name if name not in assignments.values()]
    new_deck = replace(deck, slides=new_slides)
    return new_deck, PhotoAssignmentReport(assigned=dict(assignments), unused_photos=unused, notes=notes)
