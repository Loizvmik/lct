import json, os, pytest
import httpx
from deckforge.provider.yandex import YandexProvider, MAX_TOKENS_BUDGET_CAP, MAX_BUDGET_ESCALATIONS

live = pytest.mark.skipif(not os.getenv("YANDEX_API_KEY"), reason="нет YANDEX_API_KEY")


@pytest.fixture
def provider():
    return YandexProvider(
        model="qwen3.6-35b-a3b",
        api_key=os.environ["YANDEX_API_KEY"],
        folder_id=os.environ["YANDEX_FOLDER_ID"],
    )


@live
def test_complete_returns_json_matching_schema(provider):
    schema = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    # max_tokens=1500, не 300: qwen3.6 — reasoning-модель и тратит бюджет в
    # первую очередь на reasoning_content, а не на сам JSON-ответ. 300 было
    # догадкой без замера и заметно флакало (не хватало на reasoning + ответ),
    # см. task-1-report.md. Не срезайте обратно "на глаз".
    out = provider.complete(
        [{"role": "user", "content": "Столица Франции. Ответь JSON с полем city."}],
        schema=schema, max_tokens=1500,
    )
    assert json.loads(out)["city"].lower().startswith("париж")


@live
def test_reasoning_content_is_not_mistaken_for_answer(provider):
    """qwen3.6 кладёт размышление в reasoning_content, а content бывает пустым.
    Клиент обязан дождаться непустого content, а не вернуть размышление."""
    out = provider.complete([{"role": "user", "content": "Скажи ровно: ОК"}], max_tokens=600)
    assert "ОК" in out
    assert "user" not in out.lower()


@live
def test_vision_reads_the_image(provider, tmp_path):
    from PIL import Image
    p = tmp_path / "blue.png"
    Image.new("RGB", (64, 64), (0, 0, 255)).save(p)
    answer = provider.ask_image(p.read_bytes(), "Одним словом: какого цвета изображение?")
    assert "син" in answer.lower()


def test_provider_refuses_disallowed_model():
    from deckforge.provider.registry import ModelNotAllowed
    with pytest.raises(ModelNotAllowed):
        YandexProvider(model="gpt-oss-120b", api_key="x", folder_id="y")


def _offline_provider():
    return YandexProvider(model="qwen3.6-35b-a3b", api_key="x", folder_id="y")


def _stub_post(monkeypatch, payload):
    """Подменяет сетевой _post заглушкой и возвращает список тел запросов."""
    captured = []

    def fake_post(self, body, attempts=4):
        captured.append(body)
        return payload

    monkeypatch.setattr(YandexProvider, "_post", fake_post)
    return captured


def _stub_post_sequence(monkeypatch, payloads):
    """Как _stub_post, но каждый вызов _post отдаёт следующий payload из
    списка — нужно, чтобы смоделировать первый ответ с пустым бюджетом и
    второй уже валидный (или наоборот, серию исчерпанных ответов)."""
    captured = []
    remaining = list(payloads)

    def fake_post(self, body, attempts=4):
        captured.append(body)
        if not remaining:
            raise AssertionError("_post вызван больше раз, чем ожидалось тестом")
        return remaining.pop(0)

    monkeypatch.setattr(YandexProvider, "_post", fake_post)
    return captured


def test_schema_merges_into_existing_leading_system_message(monkeypatch):
    """Если messages уже начинается с system-сообщения (промпт роли из
    agents/*/AGENT.md), инструкция про схему должна дописаться в него, а не
    стать вторым system-сообщением — иначе Yandex отвечает 400."""
    provider = _offline_provider()
    captured = _stub_post(
        monkeypatch, {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    )
    provider.complete(
        [
            {"role": "system", "content": "Ты пишешь план презентации."},
            {"role": "user", "content": "Привет"},
        ],
        schema={"type": "object"},
    )
    messages = captured[0]["messages"]
    system_messages = [m for m in messages if m["role"] == "system"]
    assert len(system_messages) == 1
    assert messages[0]["role"] == "system"
    assert "Ты пишешь план презентации." in system_messages[0]["content"]
    assert "type" in system_messages[0]["content"]
    assert messages[1]["role"] == "user"


def test_schema_adds_leading_system_message_when_absent(monkeypatch):
    provider = _offline_provider()
    captured = _stub_post(
        monkeypatch, {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    )
    provider.complete([{"role": "user", "content": "Привет"}], schema={"type": "object"})
    messages = captured[0]["messages"]
    assert messages[0]["role"] == "system"
    assert len(messages) == 2
    assert messages[1] == {"role": "user", "content": "Привет"}


def test_truncated_json_raises_informative_error(monkeypatch):
    """content непустой, но обрезан посреди JSON (бюджет max_tokens кончился
    не на пустом content, а на середине ответа) — ошибка должна быть внятной,
    а не голым JSONDecodeError."""
    provider = _offline_provider()
    _stub_post(
        monkeypatch,
        {
            "choices": [
                {"message": {"content": '{"city": "Пар'}, "finish_reason": "length"}
            ]
        },
    )
    with pytest.raises(RuntimeError) as exc_info:
        provider.complete(
            [{"role": "user", "content": "Столица Франции"}],
            schema={"type": "object"},
            max_tokens=10,
        )
    message = str(exc_info.value)
    assert "finish_reason=length" in message
    assert "max_tokens" in message


def _exhausted_payload():
    return {"choices": [{"message": {"content": None}, "finish_reason": "length"}]}


def test_budget_escalation_retries_with_doubled_max_tokens(monkeypatch):
    """Пустой content + finish_reason=length на первом ответе, валидный
    ответ на втором: complete() должен дождаться второго ответа, а второй
    запрос должен уйти с удвоенным max_tokens."""
    provider = _offline_provider()
    captured = _stub_post_sequence(
        monkeypatch,
        [
            _exhausted_payload(),
            {"choices": [{"message": {"content": "ОК"}, "finish_reason": "stop"}]},
        ],
    )
    out = provider.complete([{"role": "user", "content": "Скажи ОК"}], max_tokens=300)
    assert out == "ОК"
    assert len(captured) == 2
    assert captured[0]["max_tokens"] == 300
    assert captured[1]["max_tokens"] == 600


def test_budget_escalation_exhausted_raises_informative_error(monkeypatch):
    """Три подряд ответа с исчерпанным бюджетом: бросается информативная
    ошибка, и попыток ровно столько, сколько разрешено (1 исходная +
    MAX_BUDGET_ESCALATIONS эскалаций), не больше."""
    provider = _offline_provider()
    captured = _stub_post_sequence(
        monkeypatch, [_exhausted_payload(), _exhausted_payload(), _exhausted_payload()]
    )
    with pytest.raises(RuntimeError) as exc_info:
        provider.complete([{"role": "user", "content": "Скажи ОК"}], max_tokens=300)
    assert len(captured) == 1 + MAX_BUDGET_ESCALATIONS
    budgets = [body["max_tokens"] for body in captured]
    assert budgets == [300, 600, 1200]
    message = str(exc_info.value)
    assert "finish_reason=length" in message
    assert "300" in message and "600" in message and "1200" in message


def test_budget_escalation_never_exceeds_cap(monkeypatch):
    """Если исходный max_tokens близок к потолку, удвоение его не
    превышает — и не тратит попытку на повторную отправку того же
    значения, если дальше эскалировать уже некуда."""
    provider = _offline_provider()
    near_cap = MAX_TOKENS_BUDGET_CAP - 100
    captured = _stub_post_sequence(monkeypatch, [_exhausted_payload(), _exhausted_payload()])
    with pytest.raises(RuntimeError):
        provider.complete([{"role": "user", "content": "Скажи ОК"}], max_tokens=near_cap)
    assert all(body["max_tokens"] <= MAX_TOKENS_BUDGET_CAP for body in captured)
    assert captured[0]["max_tokens"] == near_cap
    assert captured[1]["max_tokens"] == MAX_TOKENS_BUDGET_CAP
    assert len(captured) == 2


def test_truncated_nonempty_content_does_not_trigger_escalation(monkeypatch):
    """Непустой, но обрезанный content (finish_reason=length) — это не тот
    случай, что пустой content: эскалация не должна запускаться, обработка
    идёт по старой ветке (JSONDecodeError -> внятная RuntimeError), и
    _post вызывается ровно один раз."""
    provider = _offline_provider()
    captured = _stub_post_sequence(
        monkeypatch,
        [{"choices": [{"message": {"content": '{"city": "Пар'}, "finish_reason": "length"}]}],
    )
    with pytest.raises(RuntimeError):
        provider.complete(
            [{"role": "user", "content": "Столица Франции"}],
            schema={"type": "object"},
            max_tokens=10,
        )
    assert len(captured) == 1


def test_http_500_retries_do_not_consume_budget_escalation_attempts(monkeypatch):
    """Ретрай по HTTP-статусам (429/5xx) и эскалация бюджета — разные
    счётчики. Серия HTTP 500 должна быть полностью поглощена ретраем внутри
    _post (transport-уровень, без сети — httpx.MockTransport) и не должна
    расходовать попытки, отведённые на эскалацию бюджета."""
    import deckforge.provider.yandex as yandex_module

    monkeypatch.setattr(yandex_module.time, "sleep", lambda _seconds: None)

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"error": "внутренняя ошибка"})

    provider = _offline_provider()
    provider._client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        provider.complete([{"role": "user", "content": "Скажи ОК"}], max_tokens=300)

    # _post с дефолтным attempts=4 исчерпал HTTP-ретрай сам, полностью внутри
    # одного обращения _post_with_budget_escalation -> _post; до проверки
    # finish_reason дело не дошло вовсе, попытки эскалации бюджета не тронуты.
    assert len(calls) == 4
    for request in calls:
        body = json.loads(request.content)
        assert body["max_tokens"] == 300
