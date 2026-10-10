#!/usr/bin/env python3
"""Read-only health report (`trirouter doctor`)."""
import json
import os
import re
from pathlib import Path

from . import integrations, legacy, platforms as P, remote, skillscan

HOME = P.HOME
STATE = legacy.state_dir(HOME)
CLAUDE_EVENTS = integrations.HOOK_EVENTS["claude"]


def line(ok, label, detail=""):
    print(f"[{'OK' if ok else '!!'}]  {label:<34} {detail}")


def jload(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _commands(cfg, event):
    return [h.get("command", "") for g in (cfg or {}).get("hooks", {}).get(event, []) for h in g.get("hooks", [])]


def has_hook(cfg, event):
    """A router hook for the event, under the current or the pre-rename shim path (the latter is reported
    separately by report_legacy)."""
    return any(integrations.is_router_cmd(c) for c in _commands(cfg, event))


def mcp_entry(cfg):
    """The router's entry in an MCP config: the current name, else the pre-rename one."""
    servers = (cfg or {}).get("mcpServers", {})
    return servers.get(integrations.MCP_NAME) or servers.get(legacy.MCP_NAME)


def interpreters(claude_settings, codex_toml):
    """A Python update that removes the registered interpreter silently stops every hook."""
    found = []
    hook = next((c for c in _commands(claude_settings, "UserPromptSubmit") if integrations.is_router_cmd(c)), "")
    if hook:
        m = re.match(r'\s*"([^"]+)"|\s*\'([^\']+)\'|\s*(\S+)', hook)
        found.append(("Claude hook", next(g for g in m.groups() if g)))
    for label, cfg in (("Antigravity MCP", jload(P.PATHS["agy_mcp"])), ("Claude desktop MCP", jload(P.claude_desktop_config()))):
        cmd = (mcp_entry(cfg) or {}).get("command")
        if cmd:
            found.append((label, cmd))
    names = "|".join(re.escape(n) for n in (integrations.MCP_NAME, legacy.MCP_NAME))
    m = re.search(r'\[mcp_servers\.(?:' + names + r')\]\ncommand = "([^"]+)"', codex_toml or "")
    if m:
        found.append(("Codex MCP", m.group(1)))
    return found


def report_tools():
    print("== Tools (Antigravity is asked for its model list to check its login: this can take a minute)")
    for info in P.detect().values():
        li = {True: "logged in", False: "NOT logged in", None: ""}[info["logged_in"]]
        line(info["installed"], info["label"], f"{info['version'] or 'not installed'}  {li}".strip())


def report_hooks():
    print("\n== Router hooks")
    line((STATE / "bin" / "run_hook.py").is_file(), "hook shim", "~/.trirouter/bin/run_hook.py")
    cs, cx = jload(P.PATHS["claude_settings"]), jload(P.PATHS["codex_hooks"])
    agy = (jload(P.PATHS["agy_hooks"]) or {}).get("router", {})
    missing = [e for e in CLAUDE_EVENTS if not has_hook(cs, e)]
    line(not missing, "Claude prompt/stop/session hooks",
         f"missing {', '.join(missing)} - re-run: {P.command_hint('setup --yes')}" if missing else "")
    line(has_hook(cx, "UserPromptSubmit") and has_hook(cx, "Stop"), "Codex UserPromptSubmit + Stop", "(trust once: codex -> /hooks)")
    line("PreInvocation" in agy and "Stop" in agy, "Antigravity PreInvocation + Stop")
    return cs


def report_mcp():
    print("\n== MCP router (hook-less modes)")
    ok = bool(mcp_entry(jload(P.claude_desktop_config())))
    line(ok, "Claude desktop (Chat/Cowork)", "" if ok else "missing - the app rewrites its config from memory: "
         f"close the Claude app, run {P.command_hint('setup --yes')}, reopen it")
    cfg_toml = P.PATHS["codex_config"].read_text(encoding="utf-8") if P.PATHS["codex_config"].exists() else ""
    line(any(f"[mcp_servers.{n}]" in cfg_toml for n in (integrations.MCP_NAME, legacy.MCP_NAME)), "Codex")
    line(bool(mcp_entry(jload(P.PATHS["agy_mcp"]))), "Antigravity")
    return cfg_toml


def report_legacy(claude_settings, cfg_toml):
    """Everything from before the rename that is still around. Printed only when there is something."""
    cmds = [c for event in integrations.ROUTER_EVENTS for c in _commands(claude_settings, event)]
    cmds += [c for event in ("UserPromptSubmit", "Stop") for c in _commands(jload(P.PATHS["codex_hooks"]), event)]
    cmds += [h.get("command", "") for spec in (jload(P.PATHS["agy_hooks"]) or {}).values() if isinstance(spec, dict)
             for hooks in spec.values() if isinstance(hooks, list) for h in hooks if isinstance(h, dict)]
    old_mcp = (legacy.MCP_NAME in (jload(P.claude_desktop_config()) or {}).get("mcpServers", {})
               or legacy.MCP_NAME in (jload(P.PATHS["agy_mcp"]) or {}).get("mcpServers", {})
               or f"[mcp_servers.{legacy.MCP_NAME}]" in cfg_toml)
    problems = []
    if legacy.old_state(HOME).is_dir():
        problems.append(f"state folder ~/{legacy.STATE_NAME} (the new one is ~/{legacy.NEW_STATE_NAME})")
    if any(integrations.is_legacy_cmd(c) for c in cmds):
        problems.append("hook entries that call the old shim")
    if old_mcp:
        problems.append(f"MCP server entry '{legacy.MCP_NAME}' (now '{integrations.MCP_NAME}')")
    if problems:
        print("\n== Left over from before the rename to trirouter")
        for p in problems:
            line(False, "old", p)
        line(False, "fix", f"{P.command_hint('setup --yes')} (it migrates the state folder and rewrites all of these)")


def count_in(folder, test):
    return sum(1 for p in folder.iterdir() if test(p)) if folder.is_dir() else 0


def report_skills(cfg_toml):
    print("\n== Skill hub + agents")
    hub = HOME / ".skills"
    n_hub = count_in(hub, lambda p: (p / "SKILL.md").is_file())
    line(n_hub > 0, "~/.skills", f"{n_hub} skills")
    for label, base in [("Claude links", P.PATHS["claude_skills"]), ("Codex links", P.PATHS["codex_skills"])]:
        n = count_in(base, P.is_link)
        line(n > 0, label, f"{n} links")
    agy_skills = jload(P.PATHS["agy_skills_json"]) or {}
    line(any(e.get("path", "").endswith("/.skills") for e in agy_skills.get("entries", [])), "Antigravity skills.json")
    cat = jload(hub / "catalog.json") or {}
    line(bool(cat.get("skills")), "catalog.json", f"{len(cat.get('skills', []))} skills, generated {cat.get('generated', '-')}")
    ca = list(P.PATHS["claude_agents"].glob("*-worker-*.md")) if P.PATHS["claude_agents"].is_dir() else []
    line(len(ca) > 0, "Claude worker agents", f"{len(ca)}")
    line("[agents." in cfg_toml, "Codex worker roles", f"{cfg_toml.count('[agents.')}")
    n_agy = count_in(P.PATHS["agy_agents"], lambda p: (p / "agent.md").is_file())
    line(n_agy > 0, "Antigravity worker agents", f"{n_agy}")


def report_command():
    print("\n== trirouter command and quarantine")
    ok, detail = integrations.launcher_status()
    line(ok, "trirouter command", detail)
    try:
        rows = skillscan.entries(apply=False)
    except Exception as exc:  # noqa: BLE001 - a read-only report never fails
        line(False, "quarantine", f"unreadable ({type(exc).__name__})")
        return
    if not rows:
        line(True, "quarantine", "empty")
        return
    due = [r for r in rows if r["purge_at"]]
    nxt = min(due, key=lambda r: r["purge_at"]) if due else None
    tail = f"next purge {nxt['purge_at']:%Y-%m-%d} ({nxt['name']})" if nxt else "retention off, never purged"
    line(True, "quarantine", f"{len(rows)} skill(s); {tail}")


def report_config():
    print("\n== Configuration (~/.trirouter/config.json)")
    cfg = jload(STATE / "config.json") or {}
    if os.environ.get("TYPESAFE_API_KEY") or cfg.get("typesafe_api_key"):
        backend = "JEV (TypeSafe token)"
    elif os.environ.get("JEV_OPENROUTER_API_KEY") or cfg.get("openrouter_api_key"):
        backend = "JEV through OpenRouter"
    else:
        backend = f"built-in local model ({P.command_hint('setup --jev-token=<TypeSafe token or OpenRouter key>')})"
    line(True, "Decision backend", backend)
    line(True, "Remote access name", cfg.get("remote_name") or f"not set up ({P.command_hint('remote')})")
    line(True, "Per-account model overrides", "yes" if (STATE / "models.local.json").exists()
         else f"no ({P.command_hint('models')})")
    report_discovery(cfg)
    return cfg


def report_discovery(cfg):
    """The daily model check: on or off, and when it last ran with what outcome."""
    try:
        from . import discovery
        auto = discovery.auto_enabled(cfg)
        last = discovery.last_check()
    except Exception as exc:  # noqa: BLE001 - a read-only report never fails
        line(False, "Daily model check", f"unreadable ({type(exc).__name__})")
        return
    mode = "on" if auto else f"off ({P.command_hint('models --auto=on')})"
    if not last:
        line(True, "Daily model check", f"{mode}; no check yet ({P.command_hint('models --discover')})")
        return
    when, changed, results = last
    unchecked = sorted(p for p, r in results.items() if r.get("status") != "checked")
    detail = f"{mode}; last {when.replace('T', ' ')}, {'models changed' if changed else 'no change'}"
    if unchecked:
        detail += f"; could not check {', '.join(unchecked)}"
    line(True, "Daily model check", detail)


def report_interpreters(claude_settings, cfg_toml):
    print("\n== Interpreter used by hooks and MCP")
    for label, exe in interpreters(claude_settings, cfg_toml):
        line(Path(exe).is_file(), label, exe if Path(exe).is_file() else
             f"{exe} is gone (Python updated or removed?) - re-run: {P.command_hint('setup --yes')}")


def report_remote(cfg):
    if not cfg.get("remote_name"):
        return
    print(f"\n== Remote access \"{cfg['remote_name']}\"")
    for tool, ok, detail in remote.status():
        line(ok, tool, detail)


def last_prompts(log):
    last = {}
    if not log.exists():
        return last
    for raw in log.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]:
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        last[e.get("provider")] = e.get("ts")
    return last


def _jsonl(path, limit):
    rows = []
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
            try:
                rows.append(json.loads(raw))
            except ValueError:
                continue
    return rows


def delegation_compliance(routed, subagents):
    """{"followed", "elsewhere", "in_session"} over the routed Claude turns that named a worker: did the
    model start that worker before the session's next prompt? Matched by prompt_id when both have one."""
    counts = {"followed": 0, "elsewhere": 0, "in_session": 0}
    turns = [e for e in routed if e.get("provider") == "claude" and e.get("session") and e.get("ts")]
    for i, e in enumerate(turns):
        if not e.get("target_agent"):
            continue
        nxt = next((t["ts"] for t in turns[i + 1:] if t["session"] == e["session"]), "9999")
        started = [s.get("agent_type") for s in subagents if s.get("session") == e["session"]
                   and ((s.get("prompt_id") == e["prompt_id"]) if s.get("prompt_id") and e.get("prompt_id")
                        else e["ts"] <= s.get("ts", "") < nxt)]
        key = "followed" if e["target_agent"] in started else "elsewhere" if started else "in_session"
        counts[key] += 1
    return counts


def report_activity():
    print("\n== Router activity (~/.trirouter/logs/routing.jsonl)")
    last = last_prompts(STATE / "logs" / "routing.jsonl")
    for p, ts in sorted(last.items()):
        line(True, f"last {p} prompt", ts)
    if not last:
        line(False, "no routed prompt logged yet")
    sub_log = STATE / "logs" / "subagents.jsonl"
    if not sub_log.exists():
        line(True, "Delegation compliance", "no SubagentStart data yet")
        return
    c = delegation_compliance(_jsonl(STATE / "logs" / "routing.jsonl", 500), _jsonl(sub_log, 2000))
    total = sum(c.values())
    line(True, "Delegation compliance", f"{c['followed']}/{total} turns used the advised worker, "
         f"{c['elsewhere']} another agent, {c['in_session']} answered in-session" if total else "no advised worker yet")


def main():
    report_tools()
    claude_settings = report_hooks()
    cfg_toml = report_mcp()
    report_skills(cfg_toml)
    report_command()
    cfg = report_config()
    report_interpreters(claude_settings, cfg_toml)
    report_remote(cfg)
    report_legacy(claude_settings, cfg_toml)
    report_activity()
