import pytest
from deckforge.provider.registry import assert_allowed, ModelNotAllowed


def test_qwen35b_is_allowed():
    card = assert_allowed("qwen3.6-35b-a3b")
    assert card.license == "Apache-2.0"
    assert card.params_b <= 35
    assert card.vision is True


def test_model_over_35b_is_rejected():
    with pytest.raises(ModelNotAllowed, match="35B"):
        assert_allowed("gpt-oss-120b")


def test_proprietary_model_is_rejected():
    with pytest.raises(ModelNotAllowed, match="лицензи"):
        assert_allowed("yandexgpt-5-pro")


def test_unknown_model_is_rejected():
    with pytest.raises(ModelNotAllowed, match="не описана"):
        assert_allowed("some-model-nobody-declared")
