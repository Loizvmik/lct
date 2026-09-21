"""Координаты OOXML в долях холста.

python-pptx отдаёт координаты детей группы в системе координат группы и не
применяет преобразование. На шаблонах из разведки это 6, 28 и 19 групп,
всё их содержимое встало бы не на место, и без единого исключения.
"""
from __future__ import annotations
from dataclasses import dataclass

EMU_PER_INCH = 914400


@dataclass(frozen=True)
class Canvas:
    width_emu: int
    height_emu: int

    @property
    def width_in(self) -> float:
        return self.width_emu / EMU_PER_INCH

    @property
    def height_in(self) -> float:
        return self.height_emu / EMU_PER_INCH

    @property
    def ratio(self) -> float:
        return self.width_emu / self.height_emu

    @property
    def norm(self) -> float:
        """Множитель приведения кегля к холсту 13.333″.

        У VK Tech холст 10″, и 16pt на нём читаются как 21.3pt на обычном.
        Без нормировки типографические шкалы разных шаблонов несравнимы.
        """
        return 12192000 / self.width_emu


@dataclass(frozen=True)
class GroupFrame:
    off: tuple[int, int]
    ext: tuple[int, int]
    ch_off: tuple[int, int]
    ch_ext: tuple[int, int]


def resolve_point(x: int, y: int, chain: list[GroupFrame]) -> tuple[int, int]:
    """Применяет цепочку групп от внешней к внутренней."""
    for frame in reversed(chain):
        sx = frame.ext[0] / frame.ch_ext[0] if frame.ch_ext[0] else 0.0
        sy = frame.ext[1] / frame.ch_ext[1] if frame.ch_ext[1] else 0.0
        x = round(frame.off[0] + (x - frame.ch_off[0]) * sx)
        y = round(frame.off[1] + (y - frame.ch_off[1]) * sy)
    return x, y


@dataclass(frozen=True)
class Box:
    left: float
    top: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height

    @property
    def area(self) -> float:
        return self.width * self.height

    def intersect(self, other: "Box") -> "Box | None":
        left = max(self.left, other.left)
        top = max(self.top, other.top)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return Box(left, top, right - left, bottom - top)


def box_from_emu(left: int, top: int, width: int, height: int, canvas: Canvas) -> Box:
    return Box(left / canvas.width_emu, top / canvas.height_emu,
               width / canvas.width_emu, height / canvas.height_emu)
