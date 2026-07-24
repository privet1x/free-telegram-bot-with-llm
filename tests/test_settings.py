import pytest
from pydantic import ValidationError

from app.settings import (
    Settings,
    production_bot_config_errors,
    production_config_errors,
)


def test_env_example_is_a_valid_settings_template():
    configured = Settings(_env_file=".env.example")

    assert configured.SUPER_ADMIN_ID is None
    assert configured.TELEGRAM_ALLOWED_CHAT_ID is None
    assert configured.LLM_MODEL == "deepseek-ai/deepseek-v4-flash"
    assert configured.LLM_MODEL_VISION == "google/gemma-4-31b-it"
    assert configured.QSTASH_URL == "https://qstash.upstash.io"
    assert configured.HISTORY_RETENTION_SECONDS == 2_592_000
    assert configured.JOB_RETENTION_SECONDS == 604_800
    assert configured.WORKER_BUDGET_SECONDS == 240
    assert configured.JOB_LEASE_SECONDS == 270


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("HISTORY_RETENTION_SECONDS", 0),
        ("JOB_RETENTION_SECONDS", 0),
        ("WORKER_BUDGET_SECONDS", True),
        ("JOB_LEASE_SECONDS", False),
    ],
)
def test_bounded_cost_and_retention_settings(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def _ticket_02_settings(**overrides):
    values = {
        "TELEGRAM_BOT_TOKEN": "telegram-token",
        "TELEGRAM_BOT_USERNAME": "test_bot",
        "TELEGRAM_WEBHOOK_SECRET": "webhook-secret",
        "TELEGRAM_ALLOWED_CHAT_ID": -100,
        "PUBLIC_BASE_URL": "https://bot.example",
        "UPSTASH_REDIS_REST_URL": "https://redis.example",
        "UPSTASH_REDIS_REST_TOKEN": "redis-token",
        "NVIDIA_API_KEY": "nvidia-key",
        "QSTASH_URL": "https://qstash.upstash.io",
        "QSTASH_TOKEN": "qstash-token",
        "QSTASH_CURRENT_SIGNING_KEY": "current-key",
        "QSTASH_NEXT_SIGNING_KEY": "next-key",
        "CRON_SECRET": "cron-secret",
        "SUPER_ADMIN_ID": 5,
        "SESSION_SECRET": "0123456789abcdef" * 2,
        "TELEGRAM_OIDC_CLIENT_ID": "oidc-client",
        "TELEGRAM_OIDC_CLIENT_SECRET": "oidc-secret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_ticket_02_production_configuration_is_ready():
    assert production_config_errors(_ticket_02_settings()) == []


def test_bot_readiness_is_independent_from_admin_oidc_configuration():
    configured = _ticket_02_settings(
        SUPER_ADMIN_ID=None,
        SESSION_SECRET="",
        TELEGRAM_OIDC_CLIENT_ID="",
        TELEGRAM_OIDC_CLIENT_SECRET="",
    )

    assert production_bot_config_errors(configured) == []
    assert {
        "SUPER_ADMIN_ID",
        "SESSION_SECRET",
        "TELEGRAM_OIDC_CLIENT_ID",
        "TELEGRAM_OIDC_CLIENT_SECRET",
    }.issubset(production_config_errors(configured))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"WORKER_BUDGET_SECONDS": 270}, "WORKER_BUDGET_SECONDS"),
        ({"JOB_LEASE_SECONDS": 240}, "JOB_LEASE_SECONDS"),
        ({"JOB_LEASE_SECONDS": 300}, "JOB_LEASE_SECONDS"),
        ({"QSTASH_URL": "http://qstash.example"}, "QSTASH_URL"),
        ({"QSTASH_CURRENT_SIGNING_KEY": ""}, "QSTASH_CURRENT_SIGNING_KEY"),
        ({"QSTASH_NEXT_SIGNING_KEY": ""}, "QSTASH_NEXT_SIGNING_KEY"),
        ({"NVIDIA_API_KEY": ""}, "NVIDIA_API_KEY"),
        ({"LLM_MODEL": ""}, "LLM_MODEL"),
        ({"LLM_MODEL_VISION": ""}, "LLM_MODEL_VISION"),
        ({"JOB_RETENTION_SECONDS": 3_599}, "JOB_RETENTION_SECONDS"),
        (
            {
                "JOB_RETENTION_SECONDS": 3_600,
                "AUTO_TRIGGER_COOLDOWN_SECONDS": 3_601,
            },
            "AUTO_TRIGGER_COOLDOWN_SECONDS",
        ),
    ],
)
def test_ticket_02_production_configuration_rejects_unsafe_values(
    overrides, expected
):
    assert expected in production_config_errors(_ticket_02_settings(**overrides))
