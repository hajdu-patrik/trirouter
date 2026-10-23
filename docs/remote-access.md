# Remote Access

Send prompts to your computer from a phone or another device through each tool's own remote feature. The session
runs on your computer, so the router, skills and agents work exactly as when you type locally.

```bash
trirouter remote --name "My Workstation"                   # name defaults to the hostname
trirouter remote --name "My Workstation" --workdir ~/code  # folder remote Claude sessions start in
trirouter remote --start-session=on                        # also pre-create a Claude session at logon
trirouter remote --remove                                  # undo
```

**Only the connection opens at logon.** Each tool keeps a background connection; you open a session when you need one:

- **Claude:** Claude app → **Code**, or [claude.ai/code](https://claude.ai/code) → *name* → new session (it starts
  in `--workdir`). A terminal session joins with `/remote-control`. `--start-session=on` pre-creates one session at
  logon instead (also what a Claude Code too old for connection-only mode gets; `claude update`).
- **Codex:** the ChatGPT app, after the one-time pairing below.
- **Antigravity:** [antigravity.google.com](https://antigravity.google.com) → *name*.

The name and folder are stored in `~/.trirouter/config.json`. Claude Code only serves folders whose trust dialog
was accepted, never the home directory: run `claude` once in `--workdir` and accept the dialog first.

## What gets set up

| Tool | On the computer (from logon) | On the other device |
| --- | --- | --- |
| **Claude Code** | `claude remote-control` as a scheduled task (Windows), launchd agent (macOS) or `systemd --user` service (Linux) | Claude app → Code, or claude.ai/code |
| **Antigravity** | `agy remote-control start --name <name> --session` (the CLI registers its own autostart) | antigravity.google.com |
| **Codex** | macOS / Linux: `codex remote-control start`; Windows: `codex app-server --remote-control` as a scheduled task | ChatGPT app |

On Windows everything runs windowless (`conhost.exe --headless`), the Antigravity autostart entry included, and the
task `\Automations\Trirouter - Watchdog` restarts a stopped service every 30 minutes. `trirouter doctor` shows whether
every task and server runs and the Codex connection state.

### One-time pairing for Codex

- **Windows:** ChatGPT desktop app → Settings → **Connections** → *Control this PC* → scan the QR code with the ChatGPT
  mobile app (or `codex remote-control pair`). The pairing belongs to the computer, so the background server uses it.
- **macOS / Linux:** `codex remote-control pair`.

A computer has one Codex remote connection: while the ChatGPT app holds it, the background server waits and takes
over when the app closes.

## Requirements

- The computer is switched on, awake and logged in (use the tools' keep-awake options or the OS power settings).
- Each tool is logged in with the account you use on the other device; Claude Remote Control needs a subscription
  that includes Claude Code, Codex and Antigravity remote control depend on plan and region.

## Cloud sessions (computer switched off)

The tools' cloud sandboxes work on a GitHub copy of the repository: Claude Code on the web
([claude.ai/code](https://claude.ai/code), give the environment a name distinct from your machine; this repository's
project hook routes there with `--cloud-only`) and Codex Cloud ([chatgpt.com/codex](https://chatgpt.com/codex),
`codex cloud exec --env <id> "<task>"`).
