from __future__ import annotations
import base64, json, logging, time
from typing import Any
import httpx
from .base import LLMProvider, VisionProvider, Msg
from .registry import assert_allowed

logger = logging.getLogger(__name__)

ENDPOINT = "https://llm.api.cloud.yandex.net/v1/chat/completions"
_RETRY_STATUS = {429, 500, 502, 503, 504}

# Сколько раз удваиваем max_tokens, когда весь бюджет ушёл в reasoning_content
# и content пуст (finish_reason="length"): одна эскалация закрывает почти все
# случаи перекоса бюджета у qwen3.6 (замерено — на max_tokens=1500 падало
# 2 из 5, то есть повторного перекоса на удвоенном бюджете почти не бывает),
# вторая — редкий повторный перекос. Дальше это уже не "бюджета мало", а
# другая проблема, и слать запросы до бесконечности бессмысленно.
MAX_BUDGET_ESCALATIONS = 2

# Жёсткий потолок max_tokens при эскалации бюджета: пайплайн должен уложить
# колоду 10-15 слайдов в 5 минут ТЗ, и один запрос, разогнанный до бесконечности,
# этот бюджет времени съест сам. 8192 — с запасом на reasoning_content и сам
# ответ, но не безграничный рост.
MAX_TOKENS_BUDGET_CAP = 8192


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
        payload, tried_budgets = self._post_with_budget_escalation(body)
        content = self._extract(payload, tried_budgets=tried_budgets)
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
        body = {
            "model": self.model_uri, "max_tokens": max_tokens, "temperature": 0.0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": url}},
            ]}],
        }
        payload, tried_budgets = self._post_with_budget_escalation(body)
        return self._extract(payload, tried_budgets=tried_budgets)

    def _post(self, body: dict, attempts: int = 4) -> dict:
        # Ретрай по HTTP-статусам (429/5xx, сетевой/серверный сбой) — счётчик
        # `attempts` целиком внутри одного вызова _post и не пересекается со
        # счётчиком эскалации бюджета в _post_with_budget_escalation ниже:
        # сетевой сбой не должен списывать попытки, отведённые на эскалацию.
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

    def _post_with_budget_escalation(self, body: dict) -> tuple[dict, list[int]]:
        """Отправляет запрос; если бюджет весь ушёл в reasoning_content
        (content пуст, finish_reason="length"), повторяет с удвоенным
        max_tokens — не больше MAX_BUDGET_ESCALATIONS раз и не выше
        MAX_TOKENS_BUDGET_CAP. Возвращает финальный payload и список
        испробованных max_tokens (для диагностики в _extract и в логах)."""
        tried_budgets = [body["max_tokens"]]
        payload = self._post(body)
        for _ in range(MAX_BUDGET_ESCALATIONS):
            if not self._budget_exhausted(payload):
                if len(tried_budgets) > 1:
                    logger.warning(
                        "ответ получен после эскалации бюджета max_tokens: %s",
                        tried_budgets,
                    )
                return payload, tried_budgets
            next_tokens = min(body["max_tokens"] * 2, MAX_TOKENS_BUDGET_CAP)
            if next_tokens <= body["max_tokens"]:
                # Уже на потолке — повторный запрос с тем же max_tokens ничего
                # не изменит, дальше эскалировать некуда.
                break
            logger.warning(
                "модель вернула пустой content при finish_reason=length "
                "(max_tokens=%s) — поднимаю бюджет до %s",
                body["max_tokens"], next_tokens,
            )
            body = {**body, "max_tokens": next_tokens}
            tried_budgets.append(next_tokens)
            payload = self._post(body)
        return payload, tried_budgets

    @staticmethod
    def _budget_exhausted(payload: dict) -> bool:
        message = payload["choices"][0]["message"]
        content = (message.get("content") or "").strip()
        finish_reason = payload["choices"][0].get("finish_reason")
        return not content and finish_reason == "length"

    @staticmethod
    def _extract(payload: dict, *, tried_budgets: list[int] | None = None) -> str:
        message = payload["choices"][0]["message"]
        content = (message.get("content") or "").strip()
        if content:
            return content
        # content пуст, когда весь бюджет ушёл в reasoning_content: это не ответ,
        # а обрезанное размышление, и отдавать его наверх нельзя. Если до этой
        # точки уже дошла эскалация бюджета (_post_with_budget_escalation),
        # tried_budgets содержит больше одного значения — перечисляем их, чтобы
        # на защите было видно, что бюджета не хватило даже после удвоения.
        budgets_note = ""
        if tried_budgets and len(tried_budgets) > 1:
            budgets_note = f" Испробованы бюджеты max_tokens: {tried_budgets}."
        raise RuntimeError(
            "модель не вернула ответ: весь бюджет токенов ушёл на reasoning_content, "
            f"finish_reason={payload['choices'][0].get('finish_reason')}. "
            f"Увеличьте max_tokens.{budgets_note}"
        )
