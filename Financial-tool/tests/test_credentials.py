"""Saving credentials from the Settings page.

The form writes secrets to disk and rewires a broker that can spend money, so
the properties worth pinning down are narrow and unglamorous:

  * the .env file survives editing -- comments, ordering and unrelated keys
  * a blank box means "leave alone", never "erase"; clearing is explicit
  * a stored secret never travels back to the browser
  * nothing on this form can arm live trading
"""
from __future__ import annotations

import pytest

from app import credentials, envfile


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A throwaway .env, so a test can never touch the real one."""
    path = tmp_path / ".env"
    path.write_text(
        "# Leading comment\n"
        "ALPACA_API_KEY=oldkey\n"
        "\n"
        "# A comment that documents the secret below.\n"
        "ALPACA_API_SECRET=oldsecret\n"
        "UNRELATED=keepme\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(envfile, "ENV_PATH", path)
    return path


# --------------------------------------------------------------------------
# The file survives being edited
# --------------------------------------------------------------------------
def test_write_preserves_comments_and_unrelated_keys(env):
    envfile.write_values({"ALPACA_API_KEY": "newkey"})
    text = env.read_text(encoding="utf-8")

    assert "# Leading comment" in text
    assert "# A comment that documents the secret below." in text
    assert "ALPACA_API_KEY=newkey" in text
    assert "ALPACA_API_SECRET=oldsecret" in text
    assert "UNRELATED=keepme" in text


def test_write_keeps_key_order(env):
    envfile.write_values({"ALPACA_API_SECRET": "s2"})
    keys = [
        line.split("=")[0]
        for line in env.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert keys == ["ALPACA_API_KEY", "ALPACA_API_SECRET", "UNRELATED"]


def test_unknown_key_is_appended_not_lost(env):
    envfile.write_values({"BRAND_NEW": "value"})
    assert envfile.read_values()["BRAND_NEW"] == "value"


def test_awkward_values_round_trip(env):
    for value in ("has spaces", "has#hash", 'has"quote'):
        envfile.write_values({"ALPACA_API_KEY": value})
        assert envfile.read_values()["ALPACA_API_KEY"] == value


def test_a_blank_value_does_not_swallow_the_next_comment(env):
    """Blank values must be written bare.

    A line like `KEY=   # note` parses the comment text as the value in
    python-dotenv, which is how a "cleared" key can come back as nonsense. We
    must never emit that shape.
    """
    envfile.write_values({"ALPACA_API_KEY": ""})
    line = [
        ln for ln in env.read_text(encoding="utf-8").splitlines()
        if ln.startswith("ALPACA_API_KEY")
    ][0]
    assert line == "ALPACA_API_KEY="
    assert envfile.read_values()["ALPACA_API_KEY"] == ""


# --------------------------------------------------------------------------
# Blank means leave alone; clearing is a separate, deliberate act
# --------------------------------------------------------------------------
def test_blank_submission_changes_nothing(env):
    changed, _ = credentials.save(
        {"ALPACA_API_KEY": "", "ALPACA_API_SECRET": "  "}, set()
    )
    assert changed == []
    assert envfile.read_values()["ALPACA_API_KEY"] == "oldkey"


def test_saving_one_key_leaves_the_other_alone(env):
    credentials.save({"ALPACA_API_KEY": "newkey", "ALPACA_API_SECRET": ""}, set())
    values = envfile.read_values()
    assert values["ALPACA_API_KEY"] == "newkey"
    assert values["ALPACA_API_SECRET"] == "oldsecret"


def test_clearing_needs_the_explicit_flag(env):
    credentials.save({"ALPACA_API_KEY": ""}, {"ALPACA_API_KEY"})
    assert envfile.read_values()["ALPACA_API_KEY"] == ""


def test_unknown_form_fields_are_ignored(env):
    changed, _ = credentials.save({"SOMETHING_ELSE": "value"}, set())
    assert changed == []
    assert "SOMETHING_ELSE" not in envfile.read_values()


def test_save_updates_the_live_settings_object(env):
    from app.config import settings

    original = settings.alpaca_api_key
    try:
        credentials.save({"ALPACA_API_KEY": "hotloaded"}, set())
        assert settings.alpaca_api_key == "hotloaded"
    finally:
        settings.alpaca_api_key = original


# --------------------------------------------------------------------------
# A stored secret does not come back out
# --------------------------------------------------------------------------
def test_mask_hides_everything_but_the_last_four():
    masked = envfile.mask("supersecretvalue1234")
    assert masked.endswith("1234")
    assert "supersecret" not in masked
    assert envfile.mask("") == ""
    assert len(envfile.mask("abc")) == 3


def test_state_never_carries_a_secret_value(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "alpaca_api_key", "PKVERYSECRETKEY9999")
    row = next(r for r in credentials.current_state() if r["key"] == "ALPACA_API_KEY")

    assert row["set"] is True
    assert "PKVERYSECRET" not in row["shown"]
    assert row["shown"].endswith("9999")
    assert "PKVERYSECRETKEY9999" not in repr(credentials.current_state())


def test_the_chat_id_is_shown_in_full_because_it_is_not_a_secret(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_chat_id", "123456789")
    row = next(r for r in credentials.current_state() if r["key"] == "TELEGRAM_CHAT_ID")
    assert row["secret"] is False
    assert row["shown"] == "123456789"


# --------------------------------------------------------------------------
# The live-trading lock is not reachable from this form
# --------------------------------------------------------------------------
def test_no_field_can_arm_live_trading():
    keys = {f.key for f in credentials.FIELDS}
    assert "ALPACA_LIVE" not in keys
    assert not any(f.attr == "alpaca_live" for f in credentials.FIELDS)


def test_live_flag_is_untouched_even_if_submitted(env):
    from app.config import settings

    credentials.save({"ALPACA_LIVE": "true"}, set())
    assert settings.alpaca_live is False
    assert "ALPACA_LIVE" not in envfile.read_values()


def test_approval_switches_are_not_on_the_form():
    keys = {f.key for f in credentials.FIELDS}
    assert "TELEGRAM_ALLOW_APPROVALS" not in keys
    assert "TELEGRAM_ALLOW_LIVE_APPROVALS" not in keys


# --------------------------------------------------------------------------
# A system environment variable outranks .env, and we say so
# --------------------------------------------------------------------------
def test_shadowing_is_detected(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "from-the-shell")
    assert envfile.shadowed("ALPACA_API_KEY") is True
    monkeypatch.delenv("ALPACA_API_KEY")
    assert envfile.shadowed("ALPACA_API_KEY") is False


def test_saving_a_shadowed_key_warns(env, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "from-the-shell")
    _, warnings = credentials.save({"ALPACA_API_KEY": "newkey"}, set())
    assert warnings and "environment variable" in warnings[0]


# --------------------------------------------------------------------------
# The audit trail records the change without recording the secret
# --------------------------------------------------------------------------
def test_audit_entry_does_not_contain_the_value(env):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import AuditLog

    credentials.save({"ALPACA_API_KEY": "PKDONOTLOGME"}, set())
    with session_scope() as s:
        row = (
            s.execute(
                select(AuditLog)
                .where(AuditLog.event == "credentials_saved")
                .order_by(AuditLog.id.desc())
            )
            .scalars()
            .first()
        )
        assert row is not None
        blob = f"{row.message} {row.detail_json}"
    assert "ALPACA_API_KEY" in blob
    assert "PKDONOTLOGME" not in blob
