"""Catalog-only skills: hub skills kept out of Claude's and Codex's skill folders, served by the router instead."""
import json

import pytest

from trirouter import catalog, core, hub, legacy, platforms as P, skillscan, sync

GOOGLE = ("gke-basics", "google-cloud-waf-security", "cloud-run-basics")
OTHERS = ("pdf", "finding-google-skills")
ALL_TOOLS = ["claude", "codex", "antigravity"]


def make_skill(root, name, description="test"):
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n", encoding="utf-8")
    return root / name


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A hub with Google and other skills, one bundled skill, empty tool folders, a temp state folder."""
    hub_dir, repo, claude, codex = tmp_path / ".skills", tmp_path / "repo-skills", tmp_path / "claude", tmp_path / "codex"
    for name in GOOGLE + OTHERS:
        make_skill(hub_dir, name)
    make_skill(repo, "gke-bundled")  # matches gke-*, but ships with trirouter
    P.link_dir(hub_dir / "gke-bundled", repo / "gke-bundled")
    claude.mkdir()
    codex.mkdir()
    for name, value in (("HUB", hub_dir), ("REPO_SKILLS", repo), ("CLAUDE_SKILLS", claude), ("CODEX_SKILLS", codex),
                        ("LEGACY_CODEX_SKILLS", tmp_path / "legacy"), ("PROVIDERS", ("claude", "codex")),
                        ("APPLY", True), ("SCAN", {"llm": False, "allow": ()}), ("CHOOSE", lambda items: None),
                        ("LINK", {"catalog_only": ()})):
        monkeypatch.setattr(hub, name, value)
    monkeypatch.setattr(skillscan, "find_scanner", lambda: None)
    monkeypatch.setattr(catalog, "HUB", hub_dir)
    monkeypatch.setattr(catalog, "CATALOG", hub_dir / "catalog.json")
    monkeypatch.setattr(catalog, "SOURCES", [(hub_dir, False, catalog.PROVIDERS, None)])
    monkeypatch.setattr(catalog, "BUNDLED", repo)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv(legacy.ENV_HOME_NEW, str(state))
    return tmp_path


def linked(folder):
    return sorted(p.name for p in folder.iterdir())


def native_in(items):
    return {s["name"]: s["native_in"] for s in items}


def test_parse_patterns():
    assert catalog.parse_patterns("gke-*, Google-* ,,gcloud,gke-*") == ["gke-*", "google-*", "gcloud"]
    assert catalog.parse_patterns("") == [] and catalog.parse_patterns("  , ") == []
    assert catalog.parse_patterns(["A*", 3, " b "]) == ["a*", "b"]
    assert catalog.parse_patterns(None) == []


def test_matches_preview_skips_bundled(machine):
    assert hub.catalog_only_matches(["gke-*", "google-*"]) == ["gke-basics", "google-cloud-waf-security"]
    assert hub.catalog_only_matches("cloud-*,PDF") == ["cloud-run-basics", "pdf"]
    assert hub.catalog_only_matches([]) == []


def test_linking_skips_matching_skills_and_removes_our_old_links(machine, monkeypatch):
    hub.cmd_link()
    everything = sorted(GOOGLE + OTHERS + ("gke-bundled",))
    assert linked(hub.CLAUDE_SKILLS) == everything == linked(hub.CODEX_SKILLS)
    # a foreign link and a real folder that happen to match must survive the cleanup
    make_skill(machine / "elsewhere", "gke-foreign")
    P.link_dir(hub.CLAUDE_SKILLS / "gke-foreign", machine / "elsewhere" / "gke-foreign")
    make_skill(hub.CODEX_SKILLS, "gke-real")
    monkeypatch.setitem(hub.LINK, "catalog_only", ("gke-*", "google-*"))
    hub.cmd_link()
    assert linked(hub.CLAUDE_SKILLS) == ["cloud-run-basics", "finding-google-skills", "gke-bundled", "gke-foreign", "pdf"]
    assert linked(hub.CODEX_SKILLS) == ["cloud-run-basics", "finding-google-skills", "gke-bundled", "gke-real", "pdf"]
    assert (hub.HUB / "gke-basics" / "SKILL.md").is_file()  # still in the hub
    assert (hub.CODEX_SKILLS / "gke-real" / "SKILL.md").is_file()  # a real folder is never removed


def test_dry_run_changes_nothing(machine, monkeypatch, capsys):
    hub.cmd_link()
    monkeypatch.setattr(hub, "APPLY", False)
    monkeypatch.setitem(hub.LINK, "catalog_only", ("gke-*",))
    hub.cmd_link()
    assert "gke-basics" in linked(hub.CLAUDE_SKILLS)
    assert f"[DRY] remove stale/legacy link {hub.CLAUDE_SKILLS / 'gke-basics'}" in capsys.readouterr().out


def test_catalog_native_in_without_claude_and_codex(machine):
    by = native_in(catalog.build_catalog(["gke-*", "google-*"]))
    assert by["gke-basics"] == ["antigravity"] == by["google-cloud-waf-security"]
    assert by["gke-bundled"] == ALL_TOOLS  # bundled: never catalog-only
    assert by["pdf"] == by["cloud-run-basics"] == ALL_TOOLS
    assert all(v == ALL_TOOLS for v in native_in(catalog.build_catalog()).values())


def test_catalog_learns_the_patterns_from_config_json(machine):
    cfg = machine / "state" / "config.json"
    cfg.write_text(json.dumps({"skills": {"catalog_only": ["gke-*"]}}), encoding="utf-8")
    assert catalog.catalog_only_patterns() == ["gke-*"]
    assert native_in(catalog.build_catalog())["gke-basics"] == ["antigravity"]
    cfg.write_text(json.dumps({"skills": {"catalog_only": "gke-*, pdf"}}), encoding="utf-8")
    assert catalog.catalog_only_patterns() == ["gke-*", "pdf"]
    for broken in ("{not json", json.dumps({"skills": None}), json.dumps({"skills": {"catalog_only": 5}}), "[]"):
        cfg.write_text(broken, encoding="utf-8")
        assert catalog.catalog_only_patterns() == []
    cfg.unlink()
    assert catalog.catalog_only_patterns() == []


def test_hub_catalog_prefers_the_installer_patterns_over_config(machine, monkeypatch):
    (machine / "state" / "config.json").write_text(json.dumps({"skills": {"catalog_only": ["pdf"]}}), encoding="utf-8")
    hub.cmd_catalog()  # no LINK patterns: config.json decides
    saved = native_in(json.loads(catalog.CATALOG.read_text(encoding="utf-8"))["skills"])
    assert saved["pdf"] == ["antigravity"] and saved["gke-basics"] == ALL_TOOLS
    monkeypatch.setitem(hub.LINK, "catalog_only", ("gke-*",))
    hub.cmd_catalog()
    saved = native_in(json.loads(catalog.CATALOG.read_text(encoding="utf-8"))["skills"])
    assert saved["pdf"] == ALL_TOOLS and saved["gke-basics"] == ["antigravity"]


def test_sync_builds_the_catalog_with_the_saved_patterns(machine, monkeypatch):
    cfg = machine / "state" / "config.json"
    cfg.write_text(json.dumps({"skills": {"catalog_only": ["gke-*"]}}), encoding="utf-8")
    monkeypatch.setitem(hub.LINK, "catalog_only", ("gke-*",))
    hub.cmd_catalog()
    monkeypatch.setitem(hub.LINK, "catalog_only", ())  # the daily check is a process of its own: only config.json is left
    assert not sync._skill_catalog(apply=False)[0][1]  # (label, stale, detail)
    cfg.write_text("{}", encoding="utf-8")
    assert sync._skill_catalog(apply=False)[0][1]


def test_router_can_pick_a_catalog_only_skill_and_points_claude_at_its_file(machine, monkeypatch, tmp_path):
    (hub.HUB / "cloud-run-basics" / "SKILL.md").write_text(
        "---\nname: cloud-run-basics\ndescription: Deploy and manage Cloud Run services, jobs and worker pools.\n---\n",
        encoding="utf-8")
    catalog.write_catalog(catalog_only=["cloud-*"])
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(core, "BACKEND", "local")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    prompt = "Deploy my service to Cloud Run with the cloud-run-basics skill"
    path = str(hub.HUB / "cloud-run-basics").replace("\\", "/")
    for provider in ("claude", "codex"):
        d, text, _, _ = core.route(prompt, provider)
        assert d["skill"] == "cloud-run-basics" and d["skill_native"] == ["antigravity"]
        assert f"(from another tool) - read and follow {path}/SKILL.md before starting." in text
        assert "use it." not in text
    _, text, _, _ = core.route(prompt, "antigravity")
    assert "Relevant skill: `cloud-run-basics` - use it." in text


def test_doctor_reports_the_catalog_only_count(machine, monkeypatch, capsys):
    hub.cmd_doctor()
    assert "catalog-only" not in capsys.readouterr().out
    monkeypatch.setitem(hub.LINK, "catalog_only", ("gke-*", "google-*"))
    hub.cmd_doctor()
    assert "2 of 6 hub skill(s) are catalog-only" in capsys.readouterr().out
    monkeypatch.setitem(hub.LINK, "catalog_only", ())
    (machine / "state" / "config.json").write_text(json.dumps({"skills": {"catalog_only": ["pdf"]}}), encoding="utf-8")
    hub.cmd_doctor()
    assert "1 of 6 hub skill(s) are catalog-only" in capsys.readouterr().out
