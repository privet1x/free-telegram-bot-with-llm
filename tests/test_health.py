def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["service"] == "tg-llm-bot"
    # without Upstash creds, tests use the in-memory backend
    assert body["store"] == "memory"


def test_health_fails_closed_on_vercel_without_required_config(client, monkeypatch):
    from app.settings import settings

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_USERNAME", "")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "")
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_ID", None)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "")

    response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert "TELEGRAM_ALLOWED_CHAT_ID" in response.json()["warning"]
    assert "persistent Upstash Redis" in response.json()["warning"]


def test_health_checks_store_connectivity(client, monkeypatch):
    import app.server as server

    class BrokenStore:
        def backend(self):
            return "upstash"

        def ping(self):
            raise RuntimeError("unavailable")

    monkeypatch.setattr(server, "get_store", lambda: BrokenStore())

    response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert "did not answer PING" in response.json()["warning"]


def test_health_reports_invalid_production_value_names_only(client, monkeypatch):
    import app.server as server
    from app.settings import settings

    class HealthyUpstash:
        def backend(self):
            return "upstash"

        def ping(self):
            return True

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_USERNAME", "test_bot")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "bad secret space")
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_ID", 100)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "http://not-https.test")
    monkeypatch.setattr(settings, "UPSTASH_REDIS_REST_URL", "https://redis.test")
    monkeypatch.setattr(settings, "UPSTASH_REDIS_REST_TOKEN", "token")
    monkeypatch.setattr(server, "get_store", lambda: HealthyUpstash())

    response = client.get("/api/health")

    assert response.status_code == 503
    warning = response.json()["warning"]
    assert "PUBLIC_BASE_URL" in warning
    assert "TELEGRAM_WEBHOOK_SECRET" in warning
    assert "bad secret space" not in warning


def test_health_handles_store_construction_failure(client, monkeypatch):
    import app.server as server

    def broken_store_factory():
        raise RuntimeError("adapter import/configuration failed")

    monkeypatch.setattr(server, "get_store", broken_store_factory)

    response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert response.json()["store"] == "unavailable"
    assert "did not answer PING" in response.json()["warning"]


def test_health_ticket_02_readiness_names_only_never_secret_values(
    client, monkeypatch
):
    import app.server as server
    from app.settings import settings

    class HealthyUpstash:
        def backend(self):
            return "upstash"

        def ping(self):
            return True

    canary = "DO_NOT_EXPOSE_SECRET_CANARY"
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", canary)
    monkeypatch.setattr(settings, "TELEGRAM_BOT_USERNAME", "test_bot")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "valid-secret")
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_CHAT_ID", -100)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://bot.example")
    monkeypatch.setattr(settings, "UPSTASH_REDIS_REST_URL", "https://redis.example")
    monkeypatch.setattr(settings, "UPSTASH_REDIS_REST_TOKEN", canary)
    monkeypatch.setattr(settings, "NVIDIA_API_KEY", "")
    monkeypatch.setattr(settings, "QSTASH_TOKEN", canary)
    monkeypatch.setattr(settings, "QSTASH_CURRENT_SIGNING_KEY", canary)
    monkeypatch.setattr(settings, "QSTASH_NEXT_SIGNING_KEY", canary)
    monkeypatch.setattr(server, "get_store", lambda: HealthyUpstash())

    response = client.get("/api/health")

    assert response.status_code == 503
    serialized = response.text
    assert "NVIDIA_API_KEY" in serialized
    assert canary not in serialized
