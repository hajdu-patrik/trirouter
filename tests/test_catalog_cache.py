"""On-disk token-index cache of the skill catalog (no network)."""
import json
import os

import pytest

from trirouter import catalog


@pytest.fixture
def cat_env(tmp_path, monkeypatch):
    cat_file = tmp_path / "hub" / "catalog.json"
    cat_file.parent.mkdir()
    skills = [
        {"name": "pdf", "description": "Read and fill pdf documents", "path": "/x/pdf", "native_in": ["claude"]},
        {"name": "test-driven-development", "description": "Write tests before the code", "path": "/x/tdd",
         "native_in": ["claude"]},
    ]
    cat_file.write_text(json.dumps({"skills": skills}), encoding="utf-8")
    monkeypatch.setattr(catalog, "CATALOG", cat_file)
    monkeypatch.setattr(catalog.P, "STATE", tmp_path / "state")
    catalog._INDEX_MEMO.clear()
    return cat_file


def names(prompt):
    return [s["name"] for _, s in catalog.prefilter(prompt, catalog.load_catalog(), min_score=0.5)]


def test_cache_written_and_reused(cat_env, monkeypatch):
    assert names("fill this pdf") == ["pdf"]
    idx = catalog._index_path()
    assert idx.is_file()
    catalog._INDEX_MEMO.clear()
    calls = []
    real = catalog._build_index
    monkeypatch.setattr(catalog, "_build_index", lambda c: calls.append(1) or real(c))
    assert names("fill this pdf") == ["pdf"]
    assert calls == []  # served from disk


def test_invalidated_when_catalog_changes(cat_env):
    assert names("fill this pdf") == ["pdf"]
    data = json.loads(cat_env.read_text(encoding="utf-8"))
    data["skills"].append({"name": "spreadsheet", "description": "Edit excel spreadsheet files",
                           "path": "/x/s", "native_in": ["claude"]})
    cat_env.write_text(json.dumps(data), encoding="utf-8")
    st = cat_env.stat()
    os.utime(cat_env, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    catalog._INDEX_MEMO.clear()
    assert "spreadsheet" in names("edit the excel spreadsheet")


def test_corrupt_cache_falls_back(cat_env):
    names("fill this pdf")
    catalog._index_path().write_text("{not json", encoding="utf-8")
    catalog._INDEX_MEMO.clear()
    assert names("fill this pdf") == ["pdf"]
    json.loads(catalog._index_path().read_text(encoding="utf-8"))  # rewritten, valid again


def test_unwritable_state_dir_is_silent(cat_env, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only")
    monkeypatch.setattr(catalog.P, "atomic_write", boom)
    assert names("fill this pdf") == ["pdf"]


def test_plain_list_catalog_still_works(cat_env):
    cat = list(catalog.load_catalog())
    assert [s["name"] for _, s in catalog.prefilter("fill this pdf", cat, min_score=0.5)] == ["pdf"]
