"""Абстрактные интерфейсы провайдера LLM, независимые от конкретного облака."""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, TypedDict


class Msg(TypedDict):
    """Сообщение чата в формате, совместимом с OpenAI Chat Completions."""

    role: str
    content: Any


class LLMProvider(ABC):
    """Провайдер текстовых ответов LLM."""

    @abstractmethod
    def complete(
        self,
        messages: list[Msg],
        *,
        schema: dict | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.3,
    ) -> str:
        """Вернуть ответ модели строкой; если задан schema — строкой с JSON по этой схеме."""
        ...


class VisionProvider(ABC):
    """Провайдер мультимодальных (текст + изображение) ответов."""

    @abstractmethod
    def ask_image(self, png: bytes, prompt: str, *, max_tokens: int = 1024) -> str:
        """Задать вопрос по PNG-изображению и вернуть текстовый ответ модели."""
        ...
