"""Свой ключ Yandex AI Studio из заголовков запроса (задача K).

Модель в сеть не зовётся: `YandexProvider` в `api.jobs` подменён фейком,
который запоминает аргументы конструктора и отказывается строиться
(`ValueError`, как настоящий без ключа), так что пайплайн идёт запасными
путями, а тест видит, с каким ключом его пытались построить. Проверка
ключа идёт через `httpx.MockTransport`."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from deckforge.api import credentials as user_credentials
from deckforge.api import jobs as jobs_module
from deckforge.api.app import create_app
from deckforge.api.jobs import JobStore

from .conftest import TEMPLATE_PATH, _poll_job

USER_KEY = "AQVN-user-secret-key-7f3a9c"
USER_FOLDER = "b1g-user-folder"
HEADERS = {"X-Yandex-Api-Key": USER_KEY, "X-Yandex-Folder-Id": USER_FOLDER}
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class _RecordingProvider:
    calls: list[dict] = []

    def __init__(self, **kwargs) -> None:
        type(self).calls.append(kwargs)
        raise ValueError("фейк: в тестах модель не строится")


@pytest.fixture
def recorded(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(_RecordingProvider, "calls", calls)
    monkeypatch.setattr(jobs_module, "YandexProvider", _RecordingProvider)
    return calls


def test_role_provider_uses_server_key_without_headers(recorded, monkeypatch):
    monkeypatch.setenv("YANDEX_API_KEY", "server-env-key")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "server-folder")
    assert jobs_module._build_role_provider("writer") is None
    assert recorded[-1]["api_key"] == "server-env-key"
    assert recorded[-1]["folder_id"] == "server-folder"


def test_role_provider_prefers_user_key_from_context(recorded, monkeypatch):
    monkeypatch.setenv("YANDEX_API_KEY", "server-env-key")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "server-folder")
    token = user_credentials.use(user_credentials.YandexCredentials(USER_KEY, USER_FOLDER))
    try:
        jobs_module._build_role_provider("outline")
    finally:
        user_credentials.reset(token)
    assert recorded[-1]["api_key"] == USER_KEY and recorded[-1]["folder_id"] == USER_FOLDER
    jobs_module._build_role_provider("outline")
    assert recorded[-1]["api_key"] == "server-env-key", "после сброса снова ключ сервера"


def test_half_of_the_header_pair_is_ignored():
    assert user_credentials.from_headers(USER_KEY, None) is None
    assert user_credentials.from_headers("", USER_FOLDER) is None
    assert USER_KEY not in repr(user_credentials.from_headers(USER_KEY, USER_FOLDER))


def test_job_with_header_key_builds_every_provider_on_it_and_never_stores_it(recorded, monkeypatch, tmp_path):
    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    store = JobStore(root=tmp_path / "artifacts")
    with TestClient(create_app(store=store)) as client:
        with TEMPLATE_PATH.open("rb") as fh:
            upload = client.post(
                "/api/templates", files={"file": (TEMPLATE_PATH.name, fh, PPTX_MIME)}, headers=HEADERS,
            )
        assert upload.status_code == 200, upload.text
        template_calls = list(recorded)
        assert template_calls, "разбор шаблона пытался построить модель"
        assert all(c["api_key"] == USER_KEY and c["folder_id"] == USER_FOLDER for c in template_calls)

        response = client.post("/api/decks/batch", headers=HEADERS, json={
            "template_id": upload.json()["template_id"], "brief": "Задержки очереди: причины и план",
            "sources": ["Очередь задач растёт в часы пик, p95 задержки 40 с."], "target_slides": 5,
            "autofix": True, "styles": ["dense"],
        })
        assert response.status_code == 200, response.text
        job_id = response.json()["job_ids"][0]
        result = _poll_job(client, job_id)
        assert result["status"] == "done", result

        roles_seen = len(recorded) - len(template_calls)
        assert roles_seen >= 3, "структура, писатель и аудит по картинке строились в задании"
        assert all(c["api_key"] == USER_KEY for c in recorded), "ни одна роль не ушла на ключ сервера"

        listing = client.get("/api/jobs")
        assert USER_KEY not in listing.text and USER_KEY not in client.get(f"/api/jobs/{job_id}").text

    leaks = [
        path for path in store.root.rglob("*")
        if path.is_file() and USER_KEY.encode() in path.read_bytes()
    ]
    assert not leaks, f"ключ пользователя лёг на диск: {leaks}"


@pytest.fixture
def plain_client(tmp_path):
    with TestClient(create_app(store=JobStore(root=tmp_path))) as client:
        yield client


@pytest.mark.parametrize(("status", "body", "ok", "message"), [
    (200, '{"choices": [{"message": {"content": "pong"}}]}', True, user_credentials.MSG_OK),
    (401, '{"error": "Unknown api key"}', False, user_credentials.MSG_BAD_KEY),
    (403, '{"error": "Permission denied"}', False, user_credentials.MSG_BAD_KEY),
    (404, '{"error": "folder b1g not found"}', False, user_credentials.MSG_BAD_FOLDER),
    (400, '{"error": "model gpt://x/qwen is not available"}', False, user_credentials.MSG_BAD_FOLDER),
])
def test_credentials_check_translates_yandex_answers(plain_client, monkeypatch, status, body, ok, message):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, text=body)

    monkeypatch.setattr(user_credentials, "CHECK_TRANSPORT", httpx.MockTransport(handler))
    response = plain_client.post("/api/credentials/check", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": ok, "message": message}
    assert seen[0].headers["authorization"] == f"Api-Key {USER_KEY}"
    sent = seen[0].read().decode()
    assert f"gpt://{USER_FOLDER}/qwen3.6-35b-a3b/latest" in sent and '"max_tokens":8' in sent.replace(" ", "")


def test_credentials_check_reports_network_failure(plain_client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("нет сети", request=request)

    monkeypatch.setattr(user_credentials, "CHECK_TRANSPORT", httpx.MockTransport(handler))
    response = plain_client.post("/api/credentials/check", headers=HEADERS)
    assert response.json() == {"ok": False, "message": user_credentials.MSG_NO_NETWORK}


def test_credentials_check_without_headers_is_400(plain_client):
    assert plain_client.post("/api/credentials/check").status_code == 400


def test_health_tells_whether_server_has_a_key(plain_client, monkeypatch):
    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    assert plain_client.get("/api/health").json()["server_key"] is False
    monkeypatch.setenv("YANDEX_API_KEY", "k")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "f")
    assert plain_client.get("/api/health").json()["server_key"] is True


def test_cors_lets_the_key_headers_through(plain_client):
    response = plain_client.options("/api/decks", headers={
        "Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-yandex-api-key,x-yandex-folder-id,content-type",
    })
    assert response.status_code == 200
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "x-yandex-api-key" in allowed and "x-yandex-folder-id" in allowed


def test_health_reports_the_generation_model_without_secrets(client):
    """Настройки интерфейса показывают модель генерации из реестра."""
    body = client.get("/api/health").json()
    model = body["model"]
    assert model["id"] == "qwen3.6-35b-a3b"
    assert model["license"] == "Apache-2.0" and model["params_b"] == 35 and model["active_params_b"] == 3
    assert model["provider"] == "Yandex AI Studio"
    assert "key" not in " ".join(model.keys()).lower()


def test_missing_key_gives_a_clear_error_when_the_server_requires_one(client, monkeypatch):
    """Сервер без своего ключа (`DECKFORGE_REQUIRE_MODEL_KEY=1`) не начинает
    работу без ключа пользователя и говорит, где его ввести."""
    from deckforge.api import app as app_module

    monkeypatch.setenv("DECKFORGE_REQUIRE_MODEL_KEY", "1")
    monkeypatch.setattr(app_module, "_server_has_key", lambda: False)
    assert client.get("/api/health").json()["key_required"] is True
    response = client.post("/api/templates", files={"file": ("t.pptx", b"PK", "application/octet-stream")})
    assert response.status_code == 400
    assert "Не указан ключ Yandex AI Studio" in response.json()["detail"]
    assert "Настройки" in response.json()["detail"]


def test_rejected_key_gives_a_clear_error_and_is_checked_once(client, monkeypatch):
    from deckforge.api import app as app_module
    from deckforge.api import credentials as creds_module

    calls = []

    def fake_check(creds):
        calls.append(1)
        return False, creds_module.MSG_BAD_KEY

    monkeypatch.setenv("DECKFORGE_REQUIRE_MODEL_KEY", "1")
    monkeypatch.setattr(app_module, "_server_has_key", lambda: False)
    monkeypatch.setattr(creds_module, "check_credentials", fake_check)
    monkeypatch.setattr(app_module, "_key_checks", {})
    headers = {"X-Yandex-Api-Key": "bad-key-123", "X-Yandex-Folder-Id": "b1gfolder"}
    for _ in range(2):
        response = client.post("/api/templates", headers=headers,
                               files={"file": ("t.pptx", b"PK", "application/octet-stream")})
        assert response.status_code == 400
        assert "Неверный API-ключ" in response.json()["detail"]
    assert len(calls) == 1, "проверка ключа кэшируется"
    assert all("bad-key-123" not in k for k in app_module._key_checks), "в кэше отпечаток, не ключ"


def test_no_key_check_when_not_required(client, monkeypatch):
    monkeypatch.delenv("DECKFORGE_REQUIRE_MODEL_KEY", raising=False)
    assert client.get("/api/health").json()["key_required"] is False
