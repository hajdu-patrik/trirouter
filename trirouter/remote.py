#!/usr/bin/env python3
"""Remote access from a phone or another device through each tool's own remote feature, started at
logon under one machine name.

  Claude       `claude remote-control` (Windows: scheduled task, macOS: launchd, Linux: systemd --user), as a
               server that only holds the connection: no session is created at logon (`--no-create-session-in-dir`);
               sessions start on demand from the Claude app or claude.ai/code. `remote --start-session=on`
               restores the pre-created session.
  Antigravity  `agy remote-control start` (registers its own autostart)
  Codex        macOS/Linux: `codex remote-control start`. Windows: `codex app-server --remote-control`
               in a scheduled task, because every process there runs inside a Job Object without
               breakaway permission, so the `start` daemon cannot detach.

On Windows console programs run under `conhost.exe --headless`, so nothing opens a window.
"""
import base64
import json
import os
import plistlib
import re
import time
from pathlib import Path

from . import legacy, platforms as P

STATE = legacy.state_dir(P.HOME)  # the old ~/.jev-router until migrated (cli.prepare_state)
BIN = STATE / "bin"
RUN_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_KEY = "HKCU:\\" + RUN_SUBKEY  # the same key as PowerShell names it
AGY_RUN_VALUE = "AntigravityCliDaemon"
CONFIG = STATE / "config.json"
# Windows: the scheduled tasks live in this Task Scheduler folder as "Trirouter - <what>", each with a Description
TASK_FOLDER = "\\Automations\\"
TASK_CLAUDE, TASK_CODEX, TASK_WATCHDOG = "Trirouter - Claude Remote", "Trirouter - Codex Remote", "Trirouter - Watchdog"
TASK_ONCE = "Trirouter - Setup"
TASK_DESCRIPTIONS = {
    TASK_CLAUDE: "trirouter: keeps the Claude Code Remote Control server running hidden from logon (restart with backoff).",
    TASK_CODEX: "trirouter: keeps the Codex app server with remote control running hidden from logon (restart with backoff).",
    TASK_WATCHDOG: "trirouter: at logon and every 30 minutes re-hides the Antigravity autostart entry and restarts "
                   "a stopped remote-access task.",
}
# the names before the tasks moved into TASK_FOLDER (root folder): replaced by setup, reported by doctor
PREV_TASKS = {"claude": "Trirouter-ClaudeRemote", "codex": "Trirouter-CodexRemote", "watchdog": "Trirouter-Watchdog"}
PREV_TASK_ONCE = "Trirouter-Setup"
# tasks from before the router had its own prefix, and the whole pre-rename (JevRouter-*) set
LEGACY_TASKS = ("ClaudeRemoteControl", "ChatGPTAutostart", "CodexRemoteControl", "JevRouter-ChatGPT")
OLD_TASKS = {"claude": legacy.TASK_CLAUDE, "codex": legacy.TASK_CODEX, "watchdog": legacy.TASK_WATCHDOG}
LAUNCHD_LABEL = "com.trirouter.claude-remote"
LAUNCHD = P.HOME / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
LOG = STATE / "logs" / "claude-remote.log"
SYSTEMD = P.HOME / ".config" / "systemd" / "user" / "trirouter-claude-remote.service"
OLD_LAUNCHD_LABEL = legacy.LAUNCHD_LABEL
OLD_LAUNCHD = P.HOME / "Library" / "LaunchAgents" / f"{OLD_LAUNCHD_LABEL}.plist"
OLD_SYSTEMD = P.HOME / ".config" / "systemd" / "user" / legacy.SYSTEMD_UNIT


def load_config():
    return P.read_json(CONFIG, {})


def save_name(name, workdir=None, start_session=None):
    cfg = load_config()
    cfg["remote_name"] = name
    if workdir:
        cfg["remote_workdir"] = str(workdir)
    if start_session is not None:
        cfg["remote_start_session"] = bool(start_session)
    P.atomic_write(CONFIG, json.dumps(cfg, indent=2), mode=0o600)


def start_session_saved():
    """config.json remote_start_session: pre-create a Claude session at logon (default: no, only the connection)."""
    return load_config().get("remote_start_session") is True


_RC_HELP = {}


def _rc_help(claude):
    """`claude remote-control --help`, read once per executable ("" when it cannot be read)."""
    if claude not in _RC_HELP:
        code, out = P.run([claude, "remote-control", "--help"], timeout=60)
        _RC_HELP[claude] = out if code == 0 else ""
    return _RC_HELP[claude]


def claude_rc_args(claude, name, start_session=False):
    """The arguments after `claude`. By default the server only holds the connection: no session is pre-created in
    the folder, and on-demand sessions are named "<name>-...". A Claude Code too old for these flags (or with
    start_session) gets the classic `--name`, which pre-creates one session under that name."""
    help_text = _rc_help(claude)
    if start_session or "create-session-in-dir" not in help_text:  # listed as --[no-]create-session-in-dir
        return ["remote-control", "--name", str(name)]
    args = ["remote-control", "--no-create-session-in-dir"]
    if "--remote-control-session-name-prefix" in help_text:
        args += ["--remote-control-session-name-prefix", str(name)]
    return args


_PLAIN_ARG = re.compile(r"^[\w./:=-]+$")


def _cmd_args(args):
    """Arguments for a .cmd line: anything but a plain word is quoted (a name may contain spaces)."""
    return " ".join(a if _PLAIN_ARG.match(a) else '"' + a.replace('"', "") + '"' for a in args)


def _ps(script):
    return P.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], timeout=60)


def _task(name):
    """-TaskPath/-TaskName of one of our scheduled tasks (in TASK_FOLDER)."""
    return f"-TaskPath '{TASK_FOLDER}' -TaskName \"{name}\""


def _conhost():
    """A console that is never shown, not even handed to Windows Terminal (a plain `cmd.exe` task
    opens a terminal window at every logon)."""
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "conhost.exe")


def _hidden_ps(script):
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    return _conhost(), f"--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"


def _run_once(script, wait_s=30):
    """Last output line of a PowerShell script run by the Task Scheduler ("" on timeout). Needed for
    HKCU writes: a process inside a packaged (MSIX) app, such as the Claude desktop app, writes only to a
    private copy of HKCU, so a Run entry written from there never takes effect. The result goes through a
    file because conhost does not pass the exit code on."""
    result = STATE / "state" / "setup-task.txt"
    result.parent.mkdir(parents=True, exist_ok=True)
    result.unlink(missing_ok=True)
    literal = str(result).replace("'", "''")
    exe, args = _hidden_ps(f"& {{ {script} }} | Select-Object -Last 1 | Set-Content -LiteralPath '{literal}'")
    _ps(f'$a = New-ScheduledTaskAction -Execute "{exe}" -Argument "{args}"; '
        f'Register-ScheduledTask {_task(TASK_ONCE)} -Action $a -RunLevel Limited -Force | Out-Null; '
        f'Start-ScheduledTask {_task(TASK_ONCE)}')
    for _ in range(int(wait_s * 2)):
        time.sleep(0.5)
        if result.exists() and (out := result.read_text(encoding="utf-8", errors="replace").strip()):
            break
    else:
        out = ""
    _ps(f'Unregister-ScheduledTask {_task(TASK_ONCE)} -Confirm:$false -ErrorAction SilentlyContinue')
    result.unlink(missing_ok=True)
    return out


def _psq(value):
    return "'" + str(value).replace("'", "''") + "'"


# agy registers a console program under HKCU\...\Run, which opened a terminal window at every logon.
_PS_WRAP_AGY = (f"$k = {_psq(RUN_KEY)}; $v = (Get-ItemProperty $k -ErrorAction SilentlyContinue).{AGY_RUN_VALUE}; "
                f"if ($v -and $v -notlike '*--headless*') {{ $v = '\"' + $conhost + '\" --headless ' + $v; "
                f"Set-ItemProperty $k {AGY_RUN_VALUE} $v }}")
# Restart from the wrapped entry: a daemon without a console makes Windows open a terminal window
# for every console program it runs (hooks, MCP).
_PS_RESTART_AGY = ("Get-CimInstance Win32_Process -Filter \"Name='agy.exe'\" | "
                   "? { $_.CommandLine -match 'remote-control serve' } | % { Stop-Process -Id $_.ProcessId -Force }; "
                   "Start-Process -FilePath $conhost -ArgumentList ('--headless ' + ($v -replace '^\"[^\"]+\" --headless ', ''))")


def _setup_agy_windows(agy, name):
    """Runs in a scheduled task, outside any MSIX container (see _run_once)."""
    out = _run_once(
        f"$conhost = {_psq(_conhost())}; $agy = {_psq(agy)}; "
        "& $agy remote-control stop *> $null; "
        f"$o = & $agy remote-control start --name {_psq(name)} --session 2>&1 | Out-String; "
        "if ($LASTEXITCODE -ne 0) { 'error: ' + ($o -replace '\\s+', ' ').Trim(); return }; "
        f"{_PS_WRAP_AGY}; "
        "if (-not $v) { 'no autostart entry'; return }; "
        f"{_PS_RESTART_AGY}; 'ok'", wait_s=120)
    if out == "ok":
        return True, f"daemon \"{name}\" (antigravity.google.com), starts hidden at logon"
    return False, out or "no answer from the setup task within 2 minutes"


def _watchdog_script():
    """Runs at logon and every 30 minutes: an agy update rewrites the autostart entry, and a daemon
    or remote task can stop."""
    return f"""# generated by trirouter's installer - keeps remote access running and windowless
$ErrorActionPreference = 'SilentlyContinue'
$conhost = {_psq(_conhost())}
# at logon (explorer just started): let the autostart entries and tasks start first
$e = (Get-Process explorer | Sort-Object StartTime | Select-Object -First 1).StartTime
if (-not $e -or ((Get-Date) - $e).TotalMinutes -lt 3) {{ Start-Sleep -Seconds 60 }}
{_PS_WRAP_AGY}
if ($v -and -not (Get-CimInstance Win32_Process -Filter "Name='agy.exe'" | ? {{ $_.CommandLine -match 'remote-control serve' }})) {{
    {_PS_RESTART_AGY}
}}
foreach ($t in {_psq(TASK_CLAUDE)}, {_psq(TASK_CODEX)}) {{
    $s = Get-ScheduledTask -TaskPath {_psq(TASK_FOLDER)} -TaskName $t
    if ($s -and $s.State -ne 'Running') {{ Start-ScheduledTask -TaskPath {_psq(TASK_FOLDER)} -TaskName $t }}
}}
"""


def _setup_watchdog():
    script = BIN / "remote-watchdog.ps1"
    P.atomic_write(script, _watchdog_script())
    _drop_old_task("watchdog")
    return _win_task(TASK_WATCHDOG, _conhost(), f'--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script}"',
                     str(P.HOME), repeat_min=30)


def _drop_old_task(kind):
    """Windows: stop and unregister the pre-rename (JevRouter-*) and the pre-folder (root Trirouter-*) task of this
    kind ("claude", "codex", "watchdog"). _win_loop stops their processes."""
    for task in (OLD_TASKS[kind], PREV_TASKS[kind]):
        _ps(f'Stop-ScheduledTask -TaskName "{task}" -ErrorAction SilentlyContinue; '
            f'Unregister-ScheduledTask -TaskName "{task}" -Confirm:$false -ErrorAction SilentlyContinue')


def _drop_old_launchd():
    """macOS: unload and delete the pre-rename launchd agent."""
    if OLD_LAUNCHD.exists():
        P.run(["launchctl", "bootout", f"gui/{os.getuid()}/{OLD_LAUNCHD_LABEL}"])  # fails harmlessly when not loaded
        OLD_LAUNCHD.unlink()


def _drop_old_systemd():
    """Linux: stop, disable and delete the pre-rename systemd user unit."""
    if OLD_SYSTEMD.exists():
        P.run(["systemctl", "--user", "disable", "--now", OLD_SYSTEMD.name])
        OLD_SYSTEMD.unlink()


def _loop_body(command):
    """cmd restart loop: wait 30 s, doubling to 10 min while `command` exits fast; reset after a run >= 5 min."""
    secs = 'for /f "tokens=1-3 delims=:.," %%a in ("%time: =0%") do set /a {}=(1%%a-100)*3600+(1%%b-100)*60+(1%%c-100)'
    return ("rem backoff: 30 s, doubling to 10 min while it exits fast; reset after a run >= 5 min\n"
            'set "w=15"\n:loop\n' + secs.format("t0") + "\n" + command + "\n" + secs.format("t1") + "\n"
            'set /a d=t1-t0\nif %d% lss 0 set /a d+=86400\nif %d% geq 300 set "w=15"\n'
            'set /a w*=2\nif %w% gtr 600 set "w=600"\ntimeout /t %w% /nobreak >nul\ngoto loop\n')


def _win_loop(task, script_name, workdir, command, kill):
    """Keeps `command` running from logon in a restart loop. `kill` matches a previous instance's
    processes: the scheduler ignores Start while the old loop still runs."""
    script = BIN / script_name
    P.atomic_write(script, f'@echo off\ncd /d "{workdir}"\n' + _loop_body(command), newline="\r\n")
    _ps(f'Stop-ScheduledTask {_task(task)} -ErrorAction SilentlyContinue; '
        f'Get-CimInstance Win32_Process | ? {{ ($_.Name -eq "cmd.exe" -and $_.CommandLine -match "{script_name}") -or ({kill}) }} '
        '| % { Stop-Process -Id $_.ProcessId -Force }')
    return _win_task(task, _conhost(), f'--headless cmd.exe /c "{script}"', str(workdir))


def _win_task(name, execute, argument, workdir, repeat_min=None):
    argument = argument.replace("'", "''")  # inside a single-quoted PowerShell string
    repeat = (f'$t.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) '
              f'-RepetitionInterval (New-TimeSpan -Minutes {repeat_min})).Repetition; ') if repeat_min else ""
    description = _psq(TASK_DESCRIPTIONS.get(name, "trirouter remote access"))
    return _ps(
        f'$a = New-ScheduledTaskAction -Execute "{execute}" -Argument \'{argument}\' -WorkingDirectory "{workdir}"; '
        '$t = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\\$env:USERNAME"; ' + repeat +
        '$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero); '
        f'Register-ScheduledTask {_task(name)} -Description {description} -Action $a -Trigger $t -Settings $s '
        '-RunLevel Limited -Force | Out-Null; '
        f'Start-ScheduledTask {_task(name)}')


def claude_trusts(folder):
    """Remote Control refuses untrusted folders, and the home directory is never trusted."""
    try:
        projects = json.loads((P.HOME / ".claude.json").read_text(encoding="utf-8")).get("projects", {})
    except (OSError, ValueError):
        return False

    def norm(p):
        return str(p).replace("\\", "/").rstrip("/").lower()
    return any(norm(k) == norm(folder) and v.get("hasTrustDialogAccepted") for k, v in projects.items())


def _launchd_plist(name, claude, workdir, args=None):
    """PATH is copied from this process so an npm/Homebrew/nvm `claude` script finds node."""
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(claude), *(args or ["remote-control", "--name", str(name)])],
        "WorkingDirectory": str(workdir),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    })


def _one_line(value):
    return " ".join(str(value).splitlines())


def _sd_quote(value, dollar=True):
    """`$` is doubled only where the setting expands variables (ExecStart=)."""
    value = _one_line(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if dollar:
        value = value.replace("$", "$$")
    return f'"{value}"'


def _systemd_unit(name, claude, workdir, args=None):
    path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    args = args or ["remote-control", "--name", str(name)]
    argv = " ".join(a if _PLAIN_ARG.match(a) else _sd_quote(a) for a in args)
    return f"""[Unit]
Description=Claude Code Remote Control ({_one_line(name).replace("%", "%%")})

[Service]
ExecStart={_sd_quote(claude)} {argv}
WorkingDirectory={_one_line(workdir).replace("%", "%%")}
Environment={_sd_quote("PATH=" + path, dollar=False)}
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
"""


def _setup_claude(name, claude, workdir, args):
    if P.IS_WINDOWS:
        for old in LEGACY_TASKS:
            _ps(f'Unregister-ScheduledTask -TaskName "{old}" -Confirm:$false -ErrorAction SilentlyContinue')
        _drop_old_task("claude")
        code, out = _win_loop(TASK_CLAUDE, "claude-remote.cmd", workdir, f'"{claude}" {_cmd_args(args)}',
                              '($_.Name -like "claude*.exe" -and $_.CommandLine -match " remote-control ") -or '
                              '($_.Name -eq "cmd.exe" -and $_.CommandLine -match "start-rc\\.cmd")')
    elif P.IS_MAC:
        _drop_old_launchd()
        LAUNCHD.parent.mkdir(parents=True, exist_ok=True)
        LOG.parent.mkdir(parents=True, exist_ok=True)  # launchd does not create the log folder
        P.atomic_write(LAUNCHD, _launchd_plist(name, claude, workdir, args))
        domain = f"gui/{os.getuid()}"
        P.run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"])  # fails harmlessly when not loaded
        out = ""
        for _ in range(3):  # bootout finishes asynchronously; bootstrap may briefly fail right after it
            _, out = P.run(["launchctl", "bootstrap", domain, str(LAUNCHD)])
            code, _ = P.run(["launchctl", "print", f"{domain}/{LAUNCHD_LABEL}"])
            if code == 0:
                break
            time.sleep(1)
    else:
        _drop_old_systemd()
        SYSTEMD.parent.mkdir(parents=True, exist_ok=True)
        P.atomic_write(SYSTEMD, _systemd_unit(name, claude, workdir, args))
        P.run(["systemctl", "--user", "daemon-reload"])
        P.run(["systemctl", "--user", "enable", SYSTEMD.name])
        # restart, not start: a changed name or workdir must take effect on re-run
        code, out = P.run(["systemctl", "--user", "restart", SYSTEMD.name])
    return code == 0, out


def _remote_claude(name, workdir, start_session=None):
    claude = P.find_exe("claude")
    if not claude:
        return None
    if not claude_trusts(workdir):
        return ("claude", False, f"{workdir} is not a trusted Claude Code folder: run `claude` there once, "
                                 f"accept the trust dialog, then re-run `{P.command_hint('remote')}`")
    start_session = start_session_saved() if start_session is None else start_session
    args = claude_rc_args(claude, name, start_session)
    ok, out = _setup_claude(name, claude, workdir, args)
    if "--no-create-session-in-dir" in args:
        what = "only the connection opens at logon; start sessions from the Claude app or claude.ai/code"
    elif start_session:
        what = "one session is pre-created at logon"
    else:
        what = "one session is pre-created at logon (this Claude Code is too old for connection-only: `claude update`)"
    return ("claude", ok, f"Remote Control server \"{name}\" in {workdir}: {what}" if ok else out[:200])


def _remote_agy(name):
    agy = P.find_exe("agy")
    if not agy:
        return None
    if P.IS_WINDOWS:
        ok, msg = _setup_agy_windows(agy, name)
        return ("antigravity", ok, msg[:200])
    P.run([agy, "remote-control", "stop"], timeout=60)
    code, out = P.run([agy, "remote-control", "start", "--name", name, "--session"], timeout=120)
    return ("antigravity", code == 0, f"daemon \"{name}\" (antigravity.google.com)" if code == 0 else out[:200])


def _remote_codex(workdir):
    codex = P.find_exe("codex")
    if not codex:
        return None
    if P.IS_WINDOWS:
        # one remote connection per computer: while the ChatGPT app holds it, this one retries
        _drop_old_task("codex")
        code, out = _win_loop(TASK_CODEX, "codex-remote.cmd", workdir,
                              f'call "{codex}" app-server --remote-control --listen off',
                              '$_.Name -in "codex.exe", "node.exe" -and $_.CommandLine -match "app-server --remote-control"')
        done = ("remote app server runs hidden from logon; pair once: ChatGPT app > Settings > "
                "Connections > Control this PC, or `codex remote-control pair`")
    else:
        code, out = P.run([codex, "remote-control", "start"], timeout=120)
        done = "daemon started; pair a phone: codex remote-control pair"
    return ("codex", code == 0, done if code == 0 else out[:200])


def setup(name, providers, apply=True, workdir=None, start_session=None):
    """[(tool, ok, message)]. `workdir` must be a folder Claude Code trusts. start_session None: the saved choice."""
    workdir = workdir or P.HOME
    if not apply:
        return [(p, True, f"would set up remote access as \"{name}\"") for p in providers]
    save_name(name, workdir, start_session)
    report = []
    if "claude" in providers:
        report.append(_remote_claude(name, workdir, start_session))
    if "antigravity" in providers:
        report.append(_remote_agy(name))
    if "codex" in providers:
        report.append(_remote_codex(workdir))
    report = [line for line in report if line]
    if P.IS_WINDOWS and any(ok for _, ok, _ in report):
        code, out = _setup_watchdog()
        report.append(("watchdog", code == 0, "checks every 30 min and at logon that everything runs, windowless"
                                              if code == 0 else out[:200]))
    return report


def codex_connection(minutes=30):
    """(ok, detail) from the newest remote-control status in Codex's logs, or None."""
    import sqlite3
    dbs = sorted((P.HOME / ".codex").glob("logs_*.sqlite"), key=lambda p: p.stat().st_mtime)
    if not dbs:
        return None
    try:
        con = sqlite3.connect(f"file:{dbs[-1].as_posix()}?mode=ro", uri=True, timeout=2)
        rows = con.execute("SELECT feedback_log_body FROM logs WHERE ts > ? AND target LIKE '%remote_control%' "
                           "AND (feedback_log_body LIKE '%next_status=%' OR feedback_log_body LIKE '%failed to connect%') "
                           "ORDER BY id DESC LIMIT 20", (int(time.time()) - minutes * 60,)).fetchall()
        con.close()
    except sqlite3.Error:
        return None
    states = [s for (body,) in rows for s in re.findall(r"next_status=(\w+)", body)]
    if "Connected" in states[:3]:
        return True, "connected"
    if any("already online" in body for (body,) in rows):
        return True, "another process on this computer holds the connection (e.g. the ChatGPT app) - this one waits"
    if states:
        return False, f"status: {states[0]}"
    return None


def status():
    """Read-only (tool, ok, detail) lines for `doctor`."""
    if not P.IS_WINDOWS:
        service, old_service = (LAUNCHD, OLD_LAUNCHD) if P.IS_MAC else (SYSTEMD, OLD_SYSTEMD)
        lines = [("claude", service.exists(), str(service) if service.exists() else "not set up")]
        if old_service.exists():
            lines.append(("claude", False, f"{old_service} is a pre-rename service: run `{P.command_hint('remote')}` to replace it"))
        return lines
    _, out = _ps(f'Get-ScheduledTask | ? {{ $_.TaskName -like "Trirouter*" -or $_.TaskName -like "{legacy.TASK_PATTERN}" }} '
                 '| % { $_.TaskName + "=" + $_.State }')
    tasks = {task: state for task, sep, state in (l.partition("=") for l in out.splitlines()) if sep}
    _, out = _ps('Get-CimInstance Win32_Process | ? { $_.CommandLine -match "remote-control|app-server --remote-control" } '
                 '| % { $_.Name + "|" + $_.CommandLine }')
    procs = out.lower()
    lines = _server_status(tasks, procs)
    if TASK_CODEX in tasks:
        lines += _codex_status()
    if P.find_exe("agy"):
        lines.append(_agy_status(procs))
    lines.append(("watchdog", TASK_WATCHDOG in tasks, f"task {TASK_WATCHDOG}: {tasks.get(TASK_WATCHDOG, 'missing')}"))
    lines += [("remote", False, f"pre-rename task {task} still exists: run `{P.command_hint('remote')}` to replace it")
              for task in OLD_TASKS.values() if task in tasks]
    lines += [("remote", False, f"task {task} (from before the {TASK_FOLDER} folder) still exists: "
                                f"run `{P.command_hint('remote')}` to replace it")
              for task in PREV_TASKS.values() if task in tasks]
    return lines


def _server_status(tasks, procs):
    lines = []
    # one "name|command line" per process; agy's own `remote-control serve` must not count as Claude's server
    for tool, task, marker in (("claude", TASK_CLAUDE, r"^claude[^|\n]*\|[^\n]* remote-control(?: |$)"),
                               ("codex", TASK_CODEX, r"app-server --remote-control")):
        if task in tasks:
            server = bool(re.search(marker, procs, re.M))
            lines.append((tool, tasks[task] == "Running" and server,
                          f"task {task}: {tasks[task]}, server {'running' if server else 'NOT running'}"))
    return lines


def _codex_status():
    codex = P.find_exe("codex")
    flag = codex and P.run([codex, "app-server", "--remote-control", "--listen", "off", "--help"], timeout=60)[0] == 0
    conn = codex_connection()
    return [("codex", bool(flag), "`app-server --remote-control` supported" if flag else
             "this Codex version no longer accepts `app-server --remote-control` - see docs/remote-access.md"),
            ("codex", conn[0], f"remote connection: {conn[1]}") if conn else
            ("codex", False, "no remote-control activity in the Codex log in the last 30 minutes")]


def _autostart_state(entry):
    if not entry:
        return "not registered"
    return "hidden" if "--headless" in entry else "opens a window"


def _agy_autostart_entry():
    """agy's autostart entry (its HKCU Run value), "" when there is none. A read needs no scheduled task: the private
    HKCU copy of a packaged app only matters for writes that must take effect system-wide (see _run_once)."""
    import winreg  # Windows only
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_SUBKEY) as key:
            return winreg.QueryValueEx(key, AGY_RUN_VALUE)[0]
    except OSError:
        return ""


def _agy_status(procs):
    entry = _agy_autostart_entry()
    serve = "remote-control serve" in procs
    return ("antigravity", bool(entry) and "--headless" in entry and serve,
            f"daemon {'running' if serve else 'NOT running'}, autostart {_autostart_state(entry)}")


def remove():
    report = []
    if P.IS_WINDOWS:
        for task in (TASK_WATCHDOG, TASK_CLAUDE, TASK_CODEX, TASK_ONCE):
            _ps(f'Stop-ScheduledTask {_task(task)} -ErrorAction SilentlyContinue; '
                f'Unregister-ScheduledTask {_task(task)} -Confirm:$false -ErrorAction SilentlyContinue')
        for task in (*PREV_TASKS.values(), PREV_TASK_ONCE, *OLD_TASKS.values(), legacy.TASK_ONCE) + LEGACY_TASKS:
            _ps(f'Stop-ScheduledTask -TaskName "{task}" -ErrorAction SilentlyContinue; '
                f'Unregister-ScheduledTask -TaskName "{task}" -Confirm:$false -ErrorAction SilentlyContinue')
        _ps('Get-CimInstance Win32_Process | ? { ($_.Name -eq "cmd.exe" -and $_.CommandLine -match "(claude|codex)-remote\\.cmd") '
            '-or ($_.Name -like "claude*.exe" -and $_.CommandLine -match " remote-control ") '
            '-or ($_.Name -in "codex.exe", "node.exe" -and $_.CommandLine -match "app-server --remote-control") } '
            '| % { Stop-Process -Id $_.ProcessId -Force }')
        report.append(("claude/codex", True, "scheduled tasks removed, servers stopped"))
    elif P.IS_MAC and (LAUNCHD.exists() or OLD_LAUNCHD.exists()):
        _drop_old_launchd()
        if LAUNCHD.exists():
            P.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])  # fails when not loaded
            LAUNCHD.unlink()
        report.append(("claude", True, "launchd agent removed"))
    elif SYSTEMD.exists() or OLD_SYSTEMD.exists():
        _drop_old_systemd()
        if SYSTEMD.exists():
            P.run(["systemctl", "--user", "disable", "--now", SYSTEMD.name])
            SYSTEMD.unlink()
        P.run(["systemctl", "--user", "daemon-reload"])
        report.append(("claude", True, "systemd user service removed"))
    if agy := P.find_exe("agy"):
        if P.IS_WINDOWS:
            _run_once(f"& {_psq(agy)} remote-control stop *> $null; 'ok'", wait_s=60)
        else:
            P.run([agy, "remote-control", "stop"], timeout=60)
        report.append(("antigravity", True, "daemon stopped"))
    if not P.IS_WINDOWS and (codex := P.find_exe("codex")):
        P.run([codex, "remote-control", "stop"], timeout=60)
        report.append(("codex", True, "daemon stopped"))
    return report


def legacy_services():
    """Which pre-rename remote-access services exist: a subset of {"claude", "codex", "watchdog"}."""
    if P.IS_WINDOWS:
        _, out = _ps(f'Get-ScheduledTask -TaskName "{legacy.TASK_PATTERN}" | % {{ $_.TaskName }}')
        present = {line.strip() for line in out.splitlines()}
        return {kind for kind, task in OLD_TASKS.items() if task in present}
    return {"claude"} if (OLD_LAUNCHD if P.IS_MAC else OLD_SYSTEMD).exists() else set()


def migrate(name, workdir, apply=True):
    """Re-creates the pre-rename services under the new names and paths: their scripts lived in the old state
    folder, so after the folder moved they point nowhere. Only what existed is re-created. [(tool, ok, message)]"""
    found = legacy_services()
    if not found:
        return []
    if not apply:
        return [("remote", True, f"would re-point the remote-access service(s) ({', '.join(sorted(found))}) to the new names")]
    report = []
    if "claude" in found:
        report.append(_remote_claude(name, workdir))
    if "codex" in found:
        report.append(_remote_codex(workdir))
    report = [line for line in report if line]
    if P.IS_WINDOWS and ("watchdog" in found or any(ok for _, ok, _ in report)):
        code, out = _setup_watchdog()
        report.append(("watchdog", code == 0, "re-created under its new name" if code == 0 else out[:200]))
    return report
