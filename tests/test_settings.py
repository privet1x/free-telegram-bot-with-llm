import pytest
from pydantic import ValidationError

from app.settings import Settings


def test_env_example_is_a_valid_settings_template():
    configured = Settings(_env_file=".env.example")

    assert configured.SUPER_ADMIN_ID is None
    assert configured.TELEGRAM_ALLOWED_CHAT_ID is None
    assert configured.LLM_MODEL_FAST == "deepseek-ai/deepseek-v4-flash"
    assert configured.LLM_MODEL_SMART == "deepseek-ai/deepseek-v4-pro"
    assert configured.FACT_CHECK_MAX_QUERIES == 3
    assert configured.QSTASH_URL == "https://qstash.upstash.io"
    assert configured.HISTORY_RETENTION_SECONDS == 2_592_000


@pytest.mark.parametrize(
    ("field", "value"),
    [("HISTORY_RETENTION_SECONDS", 0), ("FACT_CHECK_MAX_QUERIES", 4)],
)
def test_bounded_cost_and_retention_settings(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
