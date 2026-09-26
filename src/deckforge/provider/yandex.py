from __future__ import annotations
import base64, json, logging, time
from typing import Any, Callable
import httpx
from .base import LLMProvider, VisionProvider, Msg
from .registry import assert_allowed

logger = logging.getLogger(__name__)

ENDPOINT = "https://llm.api.cloud.yandex.net/v1/chat/completions"
_RETRY_STATUS = {429, 500, 502, 503, 504}

# Дедлайн одного вызова complete()/ask_image() по wall-clock (секунды):
# считает всё внутри одного вызова — HTTP-ретраи (_post) и эскалации
# бюджета max_tokens (_post_with_budget_escalation) вместе, а не по
# отдельности, иначе они суммируются и съедают минуты (см. комментарий у
# MAX_BUDGET_ESCALATIONS: 4 HTTP-попытки x 3 бюджета = 12 попыток).
# Совпадает по умолчанию с config/app.yaml (llm.deadline_seconds) —
# вывод числа см. там (300с на колоду, живой замер стоимости потолка
# max_tokens по времени).
DEFAULT_DEADLINE_SECONDS = 60.0

# Ниже этого остатка времени до дедлайна новую сетевую попытку не начинаем
# вовсе (кидаем ошибку сразу): меньше секунды современный HTTPS-запрос до
# Yandex Cloud (TLS + сама генерация) практически гарантированно не успеет,
# а попытка всё равно спишет время и добавит путаницы в логи.
_MIN_MEANINGFUL_REQUEST_SECONDS = 1.0

# Сколько раз удваиваем max_tokens, когда весь бюджет ушёл в reasoning_content
# и content пуст (finish_reason="length"): одна эскалация закрывает почти все
# случаи перекоса бюджета у qwen3.6 (замерено — на max_tokens=1500 падало
# 2 из 5, то есть повторного перекоса на удвоенном бюджете почти не бывает),
# вторая — редкий повторный перекос. Дальше это уже не "бюджета мало", а
# другая проблема, и слать запросы до бесконечности бессмысленно.
MAX_BUDGET_ESCALATIONS = 2

# Жёсткий потолок max_tokens при эскалации бюджета: пайплайн должен уложить
# колоду 10-15 слайдов в 5 минут ТЗ, и один запрос, разогнанный до бесконечности,
# этот бюджет времени съест сам.
#
# Было 8192 — взято из рассуждения, без замера. Живой замер (см.
# task-1-report.md): промпт, специально устроенный рассуждать (JSON-план на
# 12 слайдов, просьба сравнить 7 альтернативных структур), с max_tokens=8192
# занял 72.3с и вернул finish_reason=length с ПУСТЫМ content — весь бюджет
# ушёл в reasoning, результата ноль, а потрачено 72.3с из 300с бюджета
# колоды (24% на одном вызове из десятков). Несовместимо с ТЗ — снижено.
#
# 6144 = 1.5 x 4096 (дефолт max_tokens в LLMProvider.complete): не 4096,
# потому что если потолок равен дефолтному max_tokens, эскалация от дефолта
# упирается в потолок на первом же шаге (next_tokens = min(4096*2, CAP) =
# CAP <= 4096 -> escalation сразу останавливается без единого доп. запроса)
# — фича мертва для самого частого случая. 6144 даёт ровно один реальный
# шаг эскалации даже от дефолта. По измеренной скорости генерации в худшем
# прогоне (8192 ток / 72.3с = ~113 ток/с) потолок в 6144 ограничивает
# честную (не зависшую) генерацию на одном вызове примерно 54 секундами —
# ещё немало, но не 72, и с запасом укладывается в DEFAULT_DEADLINE_SECONDS.
#
# 6144 -> 7168 (Task 19, "заодно"): два исполнителя подряд (`template.
# naming.py`/`_PALETTE_NAMING_INITIAL_MAX_TOKENS`, task-18-report.md,
# "Пункт 2") упёрлись в этот потолок и обошли его в своём периметре,
# потому что сам потолок был вне их периметра. Живой замер (17 точек,
# именование палитры, `llm.complete(..., max_tokens=16000)` на реальных
# кандидатах палитры четырёх шаблонов включая контрольный, `finish_
# reason=stop` на всех — не заниженный потолок исказил число):
#
#   4074, 4298, 4569, 4683, 4782, 4894, 4993, 5116, 5150, 5181, 5237, 5324,
#   5603, 5779, 6180, 6304, 6406
#
# Максимум — 6406, выше старого потолка 6144 (три точки из семнадцати —
# 6180/6304/6406 — при старом потолке эскалация доезжает только до 6144 и
# всё равно не дотягивается, роль честно уходит в fallback без необходимости
# — воспроизведено живьём, task-18-report.md, "Обязательная проверка",
# прогон 2: `palette_roles_source=fallback`). 7168 = 6144 + 1024: с запасом
# ~12% над измеренным максимумом (не впритык к нему — 17 точек не исчерпывают
# распределение, следующий живой ответ на другом шаблоне может оказаться
# чуть выше 6406), и по-прежнему далеко от прежнего 8192, который живой
# замер (см. комментарий выше) уже показал непригодным по времени (72.3с на
# одном вызове). Поднимает потолок ДЛЯ ВСЕХ ролей провайдера, не только
# `palette_namer` — `writer`/`WRITER_MAX_TOKENS` (`plan/writer.py`) тоже
# стартует ровно на старом потолке 6144 (её же комментарий: "эскалация до
# потолка срабатывала практически на каждом вызове slide-writer"), значит
# получает тот же лишний шаг эскалации, что и `namer`, без отдельной правки.
# Протокольная обвязка, не инструкция роли (см. `tests/test_no_hardcoded_
# prompts.py`: содержательные промпты живут в `agents/*/AGENT.md`, короткая
# техническая обвязка — в коде). Тем же размером и тем же тоном, что уже
# стоящее рядом "Ответь одним объектом JSON по схеме, без markdown-ограды:".
_JSON_ONLY_NUDGE = "Ответь ТОЛЬКО объектом JSON по схеме, без рассуждений."
# Та же подсказка наружу: повтор вызова по картинке (`template.vision_kind`,
# схема слотов) добавляет её к запросу, когда первый ответ ушёл в
# рассуждения целиком.
JSON_ONLY_NUDGE = _JSON_ONLY_NUDGE

MAX_TOKENS_BUDGET_CAP = 7168

# Потолок выше можно запросить на КОНКРЕТНЫЙ вызов (`complete(...,
# budget_cap=...)`). Замер выше снят на именовании палитры — роли с самым
# коротким входом: ей дают список цветов, и рассуждать там почти не о чем.
# У писателя слайдов вход на порядок больше (бриф, источники, каталог видов
# раскладки с вместимостями, схема ответа с инструментами), и модель —
# ризонинг: живой прогон 25 сентября 2026 на VK Education показал ЧЕТЫРЕ
# отказа из двенадцати слайдов с диагнозом «весь бюджет ушёл на
# reasoning_content». Потолок, снятый на короткой роли, для длинной мал, и
# поднимать его ГЛОБАЛЬНО незачем — короткие роли платили бы временем за
# чужую нужду.
WRITER_BUDGET_CAP = 16384


class YandexProvider(LLMProvider, VisionProvider):
    """OpenAI-совместимый клиент Yandex AI Studio.

    Модель проверяется реестром в конструкторе: запрос к модели вне ТЗ
    не должен уйти в сеть ни разу.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None,
        folder_id: str | None,
        *,
        timeout: float = 180.0,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        now: Callable[[], float] = time.monotonic,
    ):
        # Settings.load() намеренно не проверяет секреты (config/app.yaml
        # должен грузиться и без .env — этим пользуются тесты, которые
        # сознательно запускаются без ключа). Первая точка, которой секреты
        # реально нужны, — конструктор клиента: падать должна попытка
        # создать провайдера, а не загрузка конфига, иначе первая внятная
        # ошибка приходит уже с сервера как "Api-Key None".
        missing = [
            name
            for name, value in (("YANDEX_API_KEY", api_key), ("YANDEX_FOLDER_ID", folder_id))
            if not value
        ]
        if missing:
            raise ValueError(
                f"{', '.join(missing)} не заданы (пусто или None). Секретам место в "
                ".env (см. .env.example), а не в config/app.yaml — убедитесь, что .env "
                "существует и переменные заполнены, либо передайте значения явно."
            )
        self.card = assert_allowed(model)
        self.model_uri = f"gpt://{folder_id}/{model}/latest"
        self._timeout = timeout
        self._deadline_seconds = deadline_seconds
        self._now = now
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"},
        )

    def complete(self, messages: list[Msg], *, schema: dict | None = None,
                 max_tokens: int = 4096, temperature: float = 0.3,
                 budget_cap: int = MAX_TOKENS_BUDGET_CAP) -> str:
        # Дедлайн отсчитывается от начала вызова и действует на всё, что
        # происходит внутри — HTTP-ретраи и эскалации бюджета max_tokens.
        deadline_at = self._now() + self._deadline_seconds
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
                leading_content = leading["content"]
                if isinstance(leading_content, list):
                    # Msg.content: Any допускает мультимодальный content
                    # (base.py) — f-string по списку дал бы мусор вида
                    # "[{'type': ...}]\n\n...". Дописываем инструкцию
                    # отдельным text-блоком, как в ask_image ниже.
                    merged_content: Any = [
                        *leading_content, {"type": "text", "text": instruction}
                    ]
                else:
                    merged_content = f"{leading_content}\n\n{instruction}"
                merged = {**leading, "content": merged_content}
                messages = [merged, *rest]
            else:
                messages = [{"role": "system", "content": instruction}, *messages]
        body: dict[str, Any] = {
            "model": self.model_uri, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature,
        }
        payload, tried_budgets = self._post_with_budget_escalation(
            body, deadline_at=deadline_at, expects_json=schema is not None, budget_cap=budget_cap,
        )
        if schema is not None and self._answer_is_unusable(payload):
            # Модель отдала одни рассуждения и остановилась сама
            # (`finish_reason=stop`, `content` пуст или не JSON). Эскалация
            # бюджета сюда не помогает: модель не упёрлась в потолок, она
            # решила, что закончила.
            #
            # Живой прогон 25 сентября 2026: из двенадцати слайдов три ушли
            # в запасной вариант, и часть — именно по этой причине, а не по
            # нехватке бюджета. Одна повторная попытка с прямым указанием
            # отвечать только JSON дешевле, чем потерянный слайд: пустой
            # слайд в презентации стоит дороже одного лишнего вызова.
            retry_body = {
                **body,
                "messages": [*messages, {"role": "user", "content": _JSON_ONLY_NUDGE}],
            }
            payload, tried_budgets = self._post_with_budget_escalation(
                retry_body, deadline_at=deadline_at, expects_json=True, budget_cap=budget_cap,
            )
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
        deadline_at = self._now() + self._deadline_seconds
        url = "data:image/png;base64," + base64.b64encode(png).decode()
        body = {
            "model": self.model_uri, "max_tokens": max_tokens, "temperature": 0.0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": url}},
            ]}],
        }
        payload, tried_budgets = self._post_with_budget_escalation(body, deadline_at=deadline_at)
        return self._extract(payload, tried_budgets=tried_budgets)

    def _post(
        self,
        body: dict,
        *,
        deadline_at: float,
        tried_budgets: list[int],
        attempt_counter: list[int],
        attempts: int = 4,
    ) -> dict:
        # Ретрай по HTTP-статусам (429/5xx, сетевой/серверный сбой) — счётчик
        # `attempts` целиком внутри одного вызова _post и не пересекается со
        # счётчиком эскалации бюджета в _post_with_budget_escalation ниже:
        # сетевой сбой не должен списывать попытки, отведённые на эскалацию.
        #
        # Дедлайн (deadline_at, monotonic) поверх этого: перед каждой
        # попыткой и перед каждой паузой бэкоффа проверяем остаток. Если
        # остатка не хватает на осмысленную попытку — не начинаем её.
        # attempt_counter — общий на весь complete()/ask_image() счётчик
        # реальных сетевых попыток (через все эскалации бюджета), нужен
        # только для диагностики в сообщении об истёкшем дедлайне.
        delay = 1.0
        for attempt in range(attempts):
            remaining = deadline_at - self._now()
            if remaining <= _MIN_MEANINGFUL_REQUEST_SECONDS:
                raise self._deadline_error(
                    remaining, attempt_counter[0], tried_budgets,
                    f"остаток {remaining:.1f}с недостаточен для новой попытки "
                    f"(минимум {_MIN_MEANINGFUL_REQUEST_SECONDS:.1f}с)",
                )
            request_timeout = min(self._timeout, remaining)
            attempt_counter[0] += 1
            response = self._client.post(ENDPOINT, json=body, timeout=request_timeout)
            if response.status_code in _RETRY_STATUS and attempt < attempts - 1:
                remaining = deadline_at - self._now()
                if remaining <= 0:
                    raise self._deadline_error(
                        remaining, attempt_counter[0], tried_budgets,
                        "дедлайн истёк во время паузы между попытками",
                    )
                time.sleep(min(delay, remaining))
                delay *= 2
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("недостижимо")

    def _deadline_error(
        self, remaining: float, network_attempts: int, tried_budgets: list[int], reason: str
    ) -> RuntimeError:
        elapsed = self._deadline_seconds - remaining
        return RuntimeError(
            f"дедлайн вызова ({self._deadline_seconds:.0f}с) исчерпан: прошло "
            f"{elapsed:.1f}с, сетевых попыток сделано {network_attempts}, испробованы "
            f"бюджеты max_tokens: {tried_budgets}. {reason}."
        )

    def _post_with_budget_escalation(
        self, body: dict, *, deadline_at: float, expects_json: bool = False,
        budget_cap: int = MAX_TOKENS_BUDGET_CAP,
    ) -> tuple[dict, list[int]]:
        """Отправляет запрос; если бюджет весь ушёл в reasoning_content и
        результат непригоден, повторяет с удвоенным max_tokens — не больше
        MAX_BUDGET_ESCALATIONS раз и не выше MAX_TOKENS_BUDGET_CAP.
        Возвращает финальный payload и список испробованных max_tokens (для
        диагностики в _extract и в логах). deadline_at действует на все
        попытки внутри — и HTTP-ретраи, и эскалации бюджета — единым
        отсчётом от начала complete()/ask_image().

        Признак «бюджет исчерпан» (Task 8 код-ревью, находка 1) — это
        finish_reason="length" и результат непригоден: либо content пуст,
        либо (когда запрошена схема, expects_json=True) content не
        разбирается как JSON. Второй случай раньше не запускал эскалацию —
        content непустой сам по себе ещё не значит, что бюджета хватило,
        если это обрезанный на середине JSON. Без запрошенной схемы судить
        о пригодности обрезанного текста нечем — expects_json=False держит
        эскалацию выключенной для этого случая, как и раньше."""
        tried_budgets = [body["max_tokens"]]
        attempt_counter = [0]
        payload = self._post(
            body, deadline_at=deadline_at, tried_budgets=tried_budgets,
            attempt_counter=attempt_counter,
        )
        for _ in range(MAX_BUDGET_ESCALATIONS):
            if not self._budget_exhausted(payload, expects_json=expects_json):
                if len(tried_budgets) > 1:
                    logger.warning(
                        "ответ получен после эскалации бюджета max_tokens: %s",
                        tried_budgets,
                    )
                return payload, tried_budgets
            next_tokens = min(body["max_tokens"] * 2, budget_cap)
            if next_tokens <= body["max_tokens"]:
                # Уже на потолке — повторный запрос с тем же max_tokens ничего
                # не изменит, дальше эскалировать некуда.
                break
            logger.warning(
                "бюджет max_tokens исчерпан при finish_reason=length "
                "(max_tokens=%s) — поднимаю бюджет до %s",
                body["max_tokens"], next_tokens,
            )
            body = {**body, "max_tokens": next_tokens}
            tried_budgets.append(next_tokens)
            payload = self._post(
                body, deadline_at=deadline_at, tried_budgets=tried_budgets,
                attempt_counter=attempt_counter,
            )
        return payload, tried_budgets

    @staticmethod
    def _budget_exhausted(payload: dict, *, expects_json: bool = False) -> bool:
        message = payload["choices"][0]["message"]
        content = (message.get("content") or "").strip()
        finish_reason = payload["choices"][0].get("finish_reason")
        if finish_reason != "length":
            return False
        if not content:
            return True
        if expects_json:
            try:
                json.loads(content)
            except json.JSONDecodeError:
                return True
        return False

    @staticmethod
    def _answer_is_unusable(payload: dict) -> bool:
        """Ответ непригоден, хотя модель остановилась сама: `content` пуст
        или не разбирается как JSON при `finish_reason=stop`.

        Именно `stop` — при `length` работает эскалация бюджета, она умнее
        повтора с тем же бюджетом."""
        choice = payload.get("choices", [{}])[0]
        if choice.get("finish_reason") != "stop":
            return False
        content = (choice.get("message", {}).get("content") or "").strip()
        if not content:
            return True
        try:
            json.loads(content)
        except json.JSONDecodeError:
            return True
        return False

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
