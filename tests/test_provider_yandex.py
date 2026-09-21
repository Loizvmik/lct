import json, os, pytest
from deckforge.provider.yandex import YandexProvider

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
