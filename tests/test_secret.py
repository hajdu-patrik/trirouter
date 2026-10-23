"""Protected storage of the API keys in config.json (secret.py): DPAPI on Windows, owner-only files elsewhere.
No network; the key strings are fake."""
import json
import os
import sys

import pytest

from trirouter import cli, core, doctor, secret

WIN = sys.platform.startswith("win")
KEY = "sk-or-v1-" + "a1b2c3d4" * 6  # fake


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)
    monkeypatch.setattr(cli, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(doctor, "STATE", tmp_path)
    monkeypatch.setattr(cli, "DRY", False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_OPENROUTER_API_KEY", raising=False)
    return tmp_path


def write_cfg(state, **cfg):
    (state / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def read_cfg(state):
    return json.loads((state / "config.json").read_text(encoding="utf-8"))


@pytest.mark.skipif(not WIN, reason="DPAPI is Windows only")
def test_dpapi_round_trip():
    blob = secret.protect(KEY)
    assert isinstance(blob, dict) and set(blob) == {"dpapi"}
    assert KEY not in json.dumps(blob)
    assert secret.reveal(blob) == KEY
    assert secret.status(blob) == secret.PROTECTED
    assert secret.reveal(secret.protect("ü-ключ-鍵")) == "ü-ключ-鍵"  # not just ASCII


def test_plaintext_is_still_readable_and_non_secrets_pass_through():
    assert secret.reveal(KEY) == KEY and secret.status(KEY) == secret.PLAIN
    assert secret.reveal(None) == "" and secret.status(None) == secret.NONE and secret.status("") == secret.NONE
    assert secret.protect("") == "" and secret.protect(None) is None


@pytest.mark.parametrize("blob", [{"dpapi": "!!not base64!!"}, {"dpapi": "AAAA"}, {"dpapi": 5}, {"other": "x"}, [1], 7])
def test_undecryptable_blob_is_no_key_and_never_raises(blob):
    assert secret.reveal(blob) == ""
    assert secret.status(blob) == secret.UNREADABLE


def test_foreign_blob_makes_no_endpoint_and_no_exception(state):
    write_cfg(state, typesafe_api_key={"dpapi": "AAAA"}, openrouter_api_key={"dpapi": "BBBB"})
    assert core.jev_endpoint() is None and core.jev_key() is None


def test_environment_wins_over_a_stored_key(state, monkeypatch):
    write_cfg(state, typesafe_api_key={"dpapi": "AAAA"}, openrouter_api_key=KEY)
    assert core.jev_key() == KEY  # the broken TypeSafe blob does not hide the OpenRouter key
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-token")
    assert core.jev_key() == "env-token"


def test_endpoint_reads_plaintext_config(state):
    write_cfg(state, openrouter_api_key=KEY)
    assert core.jev_endpoint()[0] == "openrouter" and core.jev_key() == KEY


@pytest.mark.skipif(not WIN, reason="DPAPI is Windows only")
def test_endpoint_reads_protected_config(state):
    write_cfg(state, openrouter_api_key=secret.protect(KEY))
    assert core.jev_endpoint()[0] == "openrouter" and core.jev_key() == KEY


def test_save_config_protects_where_possible(state):
    cli.save_config({"openrouter_api_key": KEY, "remote_name": "box"})
    cfg = read_cfg(state)
    assert cfg["remote_name"] == "box"
    if WIN:
        assert KEY not in (state / "config.json").read_text(encoding="utf-8")
        assert secret.reveal(cfg["openrouter_api_key"]) == KEY
    assert core.jev_key() == KEY  # whichever form was stored


def test_setup_save_removes_plaintext_from_old_backups(state, monkeypatch):
    write_cfg(state, openrouter_api_key=KEY)
    (state / "config.json.bak").write_text(json.dumps({"openrouter_api_key": KEY}), encoding="utf-8")
    assert secret.scrub_backups(state / "config.json", apply=True)[0][1] == ["openrouter_api_key"]
    assert KEY not in (state / "config.json.bak").read_text(encoding="utf-8")


def test_doctor_reports_states_without_printing_keys(state, capsys):
    write_cfg(state, typesafe_api_key={"dpapi": "AAAA"})
    (state / "config.json.bak").write_text(json.dumps({"openrouter_api_key": KEY}), encoding="utf-8")
    doctor.report_config()
    out = capsys.readouterr().out
    assert KEY not in out
    assert "cannot be decrypted on this account" in out and "setup --jev-token" in out
    assert "Plaintext key in config.json.bak" in out
    write_cfg(state, openrouter_api_key=KEY)
    doctor.report_config()
    assert KEY not in capsys.readouterr().out


@pytest.mark.skipif(WIN, reason="POSIX file modes")
def test_posix_config_and_backups_are_owner_only(state):
    write_cfg(state, openrouter_api_key=KEY)
    os.chmod(state / "config.json", 0o644)
    (state / "config.json.bak").write_text(json.dumps({"openrouter_api_key": KEY}), encoding="utf-8")
    os.chmod(state / "config.json.bak", 0o644)
    cli.save_config({"openrouter_api_key": KEY})  # what setup does
    secret.scrub_backups(state / "config.json", apply=True)
    for name in ("config.json", "config.json.bak"):
        assert (state / name).stat().st_mode & 0o077 == 0, name
    assert json.loads((state / "config.json").read_text(encoding="utf-8"))["openrouter_api_key"] == KEY
    assert KEY not in (state / "config.json.bak").read_text(encoding="utf-8")
