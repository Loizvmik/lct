"""Свои учётные данные Yandex AI Studio от пользователя интерфейса.

Ключ приходит заголовками `X-Yandex-Api-Key`/`X-Yandex-Folder-Id` с каждым
запросом, который запускает модель, и живёт только в памяти процесса, пока
идёт этот запрос и порождённые им задания. Держится он в `ContextVar`, а не
в полях `JobRecord`/`TemplateRecord`: запись задания отдаётся наружу снимком
и частично ложится на диск, и ключу там взяться неоткуда, если его туда не
кладут. `asyncio.create_task` копирует контекст в момент создания задачи,
`asyncio.to_thread` тоже, поэтому фоновое задание видит ключ того запроса,
который его создал, а соседние запросы других людей его не видят.

Проверка ключа (`check_credentials`) идёт отдельным коротким запросом к
модели: интерфейсу надо сказать человеку, что именно не так (ключ, каталог
или сеть), до того как он потратит пять минут на генерацию."""
from __future__ import annotations
from contextvars import ContextVar, Token
from dataclasses import dataclass

import httpx

from deckforge.provider.registry import assert_allowed
from deckforge.provider.yandex import ENDPOINT

# Модель проверки: та, что есть у всех ролей реестра, самым коротким ответом.
CHECK_MODEL = "qwen3.6-35b-a3b"
CHECK_TIMEOUT_SECONDS = 20.0

# Транспорт httpx для проверки: `None` значит настоящая сеть. Тесты
# подставляют `httpx.MockTransport`, чтобы не звать Yandex Cloud.
CHECK_TRANSPORT: httpx.BaseTransport | None = None

MSG_OK = "Ключ работает"
MSG_BAD_KEY = "Неверный API-ключ"
MSG_BAD_FOLDER = "Каталог не найден или нет доступа к модели"
MSG_NO_NETWORK = "Нет связи с Yandex Cloud"


@dataclass(frozen=True)
class YandexCredentials:
    api_key: str
    folder_id: str

    def __repr__(self) -> str:
        # Запись может попасть в трассировку исключения или отладочный
        # вывод; ключа там быть не должно ни при каких обстоятельствах.
        return f"YandexCredentials(folder_id={self.folder_id!r}, api_key=<скрыт>)"

    __str__ = __repr__


_USER_CREDENTIALS: ContextVar[YandexCredentials | None] = ContextVar(
    "deckforge_user_yandex_credentials", default=None,
)


def from_headers(api_key: str | None, folder_id: str | None) -> YandexCredentials | None:
    """Учётные данные из пары заголовков. Половина пары не считается:
    ключ без каталога (или наоборот) всё равно не даст сделать запрос, а
    смешивать ключ пользователя с каталогом сервера нельзя."""
    key = (api_key or "").strip()
    folder = (folder_id or "").strip()
    if not key or not folder:
        return None
    return YandexCredentials(api_key=key, folder_id=folder)


def current() -> YandexCredentials | None:
    return _USER_CREDENTIALS.get()


def use(credentials: YandexCredentials | None) -> Token:
    return _USER_CREDENTIALS.set(credentials)


def reset(token: Token) -> None:
    _USER_CREDENTIALS.reset(token)


def check_credentials(credentials: YandexCredentials) -> tuple[bool, str]:
    """Минимальный запрос к модели: 8 токенов без рассуждений. Ответ
    переводится в одно из четырёх сообщений для человека; текст ответа
    Yandex наружу не отдаётся, в нём бывают идентификаторы облака."""
    model = assert_allowed(CHECK_MODEL).id
    body = {
        "model": f"gpt://{credentials.folder_id}/{model}/latest",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8, "temperature": 0.0, "reasoning_effort": "none",
    }
    try:
        with httpx.Client(
            transport=CHECK_TRANSPORT, timeout=CHECK_TIMEOUT_SECONDS,
            headers={"Authorization": f"Api-Key {credentials.api_key}"},
        ) as client:
            response = client.post(ENDPOINT, json=body)
    except httpx.HTTPError:
        return False, MSG_NO_NETWORK
    status = response.status_code
    if status < 300:
        return True, MSG_OK
    text = response.text.lower()
    about_folder = "folder" in text or "model" in text
    # 403 бывает и у верного ключа, когда у сервисного аккаунта нет роли в
    # этом каталоге; тогда Yandex называет каталог в ответе.
    if status == 401 or (status == 403 and not about_folder):
        return False, MSG_BAD_KEY
    if status in (400, 403, 404) and about_folder:
        return False, MSG_BAD_FOLDER
    return False, f"Yandex Cloud ответил кодом {status}"
