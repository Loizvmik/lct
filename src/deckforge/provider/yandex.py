from __future__ import annotations
import base64, json, time
from typing import Any
import httpx
from .base import LLMProvider, VisionProvider, Msg
from .registry import assert_allowed

ENDPOINT = "https://llm.api.cloud.yandex.net/v1/chat/completions"
_RETRY_STATUS = {429, 500, 502, 503, 504}


class YandexProvider(LLMProvider, VisionProvider):
    """OpenAI-совместимый клиент Yandex AI Studio.

    Модель проверяется реестром в конструкторе: запрос к модели вне ТЗ
    не должен уйти в сеть ни разу.
    """

    def __init__(self, model: str, api_key: str, folder_id: str, *, timeout: float = 180.0):
        self.card = assert_allowed(model)
        self.model_uri = f"gpt://{folder_id}/{model}/latest"
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"},
        )

    def complete(self, messages: list[Msg], *, schema: dict | None = None,
                 max_tokens: int = 4096, temperature: float = 0.3) -> str:
        if schema is not None:
            # Yandex AI Studio требует system-сообщение первым в списке messages
            # (проверено живым запросом, HTTP 400 "System message must be at the
            # beginning") — и неизвестно, значит ли это "ровно одно на позиции 0".
            # Дальше по проекту messages уже может начинаться с системного промпта
            # роли (agents/*/AGENT.md): дописываем инструкцию про схему в него,
            # а не добавляем второе system-сообщение.
            instruction = (
                "Ответь одним объектом JSON по схеме, без markdown-ограды:\n"
                + json.dumps(schema, ensure_ascii=False)
            )
            if messages and messages[0].get("role") == "system":
                leading, *rest = messages
                merged = {**leading, "content": f"{leading['content']}\n\n{instruction}"}
                messages = [merged, *rest]
            else:
                messages = [{"role": "system", "content": instruction}, *messages]
        body: dict[str, Any] = {
            "model": self.model_uri, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature,
        }
        payload = self._post(body)
        content = self._extract(payload)
        if schema is not None:
            # Второй вид нехватки бюджета: content непустой, но обрезан на
            # середине и не парсится как JSON. Отдавать наверх голый
            # JSONDecodeError нельзя — диагноз должен быть таким же внятным,
            # как для пустого content выше.
            try:
                json.loads(content)
            except json.JSONDecodeError as exc:
                finish_reason = payload["choices"][0].get("finish_reason")
                raise RuntimeError(
                    "модель вернула обрезанный или невалидный JSON "
                    f"(finish_reason={finish_reason}): {exc}. Увеличьте max_tokens."
                ) from exc
        return content

    def ask_image(self, png: bytes, prompt: str, *, max_tokens: int = 1024) -> str:
        if not self.card.vision:
            raise RuntimeError(f"{self.card.id} не мультимодальна, vision-аудит ей недоступен")
        url = "data:image/png;base64," + base64.b64encode(png).decode()
        return self._extract(self._post({
            "model": self.model_uri, "max_tokens": max_tokens, "temperature": 0.0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": url}},
            ]}],
        }))

    def _post(self, body: dict, attempts: int = 4) -> dict:
        delay = 1.0
        for attempt in range(attempts):
            response = self._client.post(ENDPOINT, json=body)
            if response.status_code in _RETRY_STATUS and attempt < attempts - 1:
                time.sleep(delay)
                delay *= 2
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("недостижимо")

    @staticmethod
    def _extract(payload: dict) -> str:
        message = payload["choices"][0]["message"]
        content = (message.get("content") or "").strip()
        if content:
            return content
        # content пуст, когда весь бюджет ушёл в reasoning_content: это не ответ,
        # а обрезанное размышление, и отдавать его наверх нельзя.
        raise RuntimeError(
            "модель не вернула ответ: весь бюджет токенов ушёл на reasoning_content, "
            f"finish_reason={payload['choices'][0].get('finish_reason')}. Увеличьте max_tokens."
        )
