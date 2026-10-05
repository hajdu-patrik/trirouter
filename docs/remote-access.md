# Remote Access Guide

Send prompts to your computer from a phone or another device through each tool's own remote
feature. The router, skills and agents work exactly as when you type locally, because the session
runs on your computer.

```bash
trirouter remote --name "My Workstation"                  # name defaults to the hostname
trirouter remote --name "My Workstation" --workdir ~/code  # folder remote sessions start in
trirouter remote --remove                                 # undo
```

(`trirouter` exists after the first `python install.py` setup; until then, or if it is not on PATH,
use `python install.py remote ...`, which does the same. `trirouter help remote` lists the options.)

The name and folder are stored in `~/.jev-router/config.json`; the name is shown on your other
devices. Use the same name for every tool so you always recognize the machine.

**Working folder:** remote Claude sessions start in `--workdir` (default: the jev-router folder).
Claude Code only serves folders whose workspace-trust dialog was accepted, and never the home
directory: run `claude` once in that folder and accept the dialog before setting up remote access.

## What gets set up

| Tool | On the computer (starts automatically at logon) | On the phone / another device |
| --- | --- | --- |
| **Claude Code** | `claude remote-control --name <name>` – Windows scheduled task `JevRouter-ClaudeRemote`, macOS launchd agent `com.jev-router.claude-remote`, Linux `systemd --user` service `jev-router-claude-remote` | Claude app → **Code**, or [claude.ai/code](https://claude.ai/code) → *<name>* |
| **Antigravity** | `agy remote-control start --name <name> --session` (the CLI registers its own autostart) | [antigravity.google.com](https://antigravity.google.com) → *<name>* (can be installed as a web app for notifications) |
| **Codex** | macOS / Linux: `codex remote-control start`. Windows: `codex app-server --remote-control --listen off`, kept running by the scheduled task `JevRouter-CodexRemote` | ChatGPT app, after a one-time pairing (below) |

**Windows: no windows, no apps.** Everything runs in the background from logon: the console
programs run under `conhost.exe --headless`, so no terminal window opens (a plain `cmd.exe` task or
autostart entry opens one in Windows Terminal, the default terminal on Windows 11). The installer
also wraps Antigravity's own autostart entry this way. The desktop apps (Claude, ChatGPT,
Antigravity) are not started – open them whenever you like, they work as usual.

**Checking and self-repair.** `trirouter doctor` shows whether every task and server runs,
whether the Antigravity autostart entry is hidden, the Codex remote connection state and whether
the installed Codex still accepts `app-server --remote-control` (an experimental flag). On Windows
the task `JevRouter-Watchdog` runs at logon and every 30 minutes: it re-hides the Antigravity
autostart entry after an agy update rewrote it, and restarts a daemon or remote task that is not
running.

**Packaged apps (Windows).** A terminal inside a packaged app – for example the Claude desktop app's
Code tab – only sees the app's private copy of the registry. The installer therefore makes registry
changes and registers Antigravity in a short-lived scheduled task, which runs outside the app.

### One-time pairing for Codex

- **Windows:** ChatGPT desktop app → Settings → **Connections** → turn on *Control this PC* → scan
  the QR code with the ChatGPT mobile app (or run `codex remote-control pair`). The pairing belongs
  to the computer, so the background server uses it too.
- **macOS / Linux:** run `codex remote-control pair` and follow the instructions.

The pairing persists. Pair again only after signing out, reinstalling the app, switching phones or
revoking the device.

Why not `codex remote-control start` on Windows: it must detach a background daemon, and on
Windows builds where every process (Explorer included) runs inside a Job Object without breakaway
permission it cannot detach from any launcher. The app server runs in the foreground instead.
A computer has one Codex remote connection: while the ChatGPT app is open and holds it, the
background server waits and retries, and takes over when the app closes – remote access works
either way, with the same sessions.

## Requirements

- The computer must be **switched on, awake and logged in** – remote services start at logon.
  Use the tools' own keep-awake options (Claude app, ChatGPT *Keep this PC awake*, Antigravity
  *Prevent Sleep*) or your OS power settings.
- Each tool must be logged in with the same account you use on the other device.
- Claude Remote Control requires a Claude subscription that includes Claude Code; Codex remote and
  Antigravity remote control depend on your plan and region.

## Cloud sessions (computer switched off)

Remote control needs your computer. To run tasks when it is off, use the tools' cloud sandboxes;
they work on a GitHub copy of your repository, not on this computer:

- **Claude Code on the web:** connect GitHub at [claude.ai/code](https://claude.ai/code) and create
  an environment. Give it a name distinct from your machine (for example *Cloud*), so cloud and
  local sessions are never confused. This repository's project hook (`--cloud-only`) routes there too.
- **Codex Cloud:** create an environment at [chatgpt.com/codex](https://chatgpt.com/codex), then use
  `codex cloud exec --env <id> "<task>"` or the ChatGPT app.
