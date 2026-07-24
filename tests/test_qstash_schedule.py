from __future__ import annotations

from types import SimpleNamespace

from app.settings import settings
from scripts import set_qstash_schedule as schedule


class _FakeSchedule:
    def __init__(self):
        self.created: dict[str, object] | None = None
        self.deleted: str | None = None

    def create_json(self, **kwargs):
        self.created = kwargs
        return "kulajaj-banter-20m"

    def get(self, schedule_id):
        assert schedule_id == schedule.SCHEDULE_ID
        return SimpleNamespace(
            schedule_id=schedule_id,
            destination="https://bot.example/api/cron/banter",
            cron=schedule.CRON_EXPRESSION,
            paused=False,
            next_schedule_time=123,
        )

    def delete(self, schedule_id):
        self.deleted = schedule_id


class _FakeQStash:
    schedule_api = _FakeSchedule()

    def __init__(self, token, *, base_url):
        assert token == "qstash-token"
        assert base_url == "https://qstash.example"
        self.schedule = self.schedule_api


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://bot.example")
    monkeypatch.setattr(settings, "QSTASH_URL", "https://qstash.example")
    monkeypatch.setattr(settings, "QSTASH_TOKEN", "qstash-token")
    monkeypatch.setattr(settings, "CRON_SECRET", "cron-secret")
    _FakeQStash.schedule_api = _FakeSchedule()
    monkeypatch.setattr(schedule, "QStash", _FakeQStash)


def test_set_qstash_schedule_forwards_existing_cron_secret(monkeypatch, capsys):
    _configure(monkeypatch)

    assert schedule.main(["set"]) == 0

    created = _FakeQStash.schedule_api.created
    assert created is not None
    assert created["destination"] == "https://bot.example/api/cron/banter"
    assert created["cron"] == "*/20 * * * *"
    assert created["method"] == "POST"
    assert created["headers"] == {"Authorization": "Bearer cron-secret"}
    assert created["schedule_id"] == "kulajaj-banter-20m"
    assert "cron-secret" not in capsys.readouterr().out


def test_qstash_schedule_info_and_delete(monkeypatch, capsys):
    _configure(monkeypatch)

    assert schedule.main(["info"]) == 0
    info = capsys.readouterr().out
    assert "schedule id" in info
    assert "https://bot.example/api/cron/banter" in info
    assert "cron-secret" not in info

    assert schedule.main(["delete"]) == 0
    assert _FakeQStash.schedule_api.deleted == schedule.SCHEDULE_ID


def test_qstash_schedule_usage_returns_error(capsys):
    assert schedule.main([]) == 2
    assert "set|info|delete" in capsys.readouterr().out


def test_qstash_schedule_rejects_insecure_provider_url(monkeypatch, capsys):
    _configure(monkeypatch)
    monkeypatch.setattr(settings, "QSTASH_URL", "http://qstash.example")

    try:
        schedule.main(["info"])
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("insecure QSTASH_URL was accepted")
    assert "HTTPS" in capsys.readouterr().out
