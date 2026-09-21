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
    out = provider.complete(
        [{"role": "user", "content": "Столица Франции. Ответь JSON с полем city."}],
        schema=schema, max_tokens=300,
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
