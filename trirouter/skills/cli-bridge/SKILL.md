---
name: cli-bridge
description: Run a task or get an independent second opinion from the Codex (ChatGPT) or Antigravity (Google Gemini) CLI. Use when the [router] context mentions cli-bridge, or the user writes #codex or #antigravity.
---

# cli-bridge

1. The other model does not see this conversation. Write a complete, self-contained prompt into
   `~/.trirouter/tmp/cli_prompt.txt`: the task, relevant file names and code snippets, the
   expected output shape, and the language to answer in (the user's language). Never put a
   secret (key, password, token) in it.
2. Run it (works from any folder; `~/.skills/cli-bridge` is the shared skill hub):
   `python ~/.skills/cli-bridge/ask_cli.py codex --model <model> --effort <effort> < ~/.trirouter/tmp/cli_prompt.txt`
   `python ~/.skills/cli-bridge/ask_cli.py antigravity --model <model> < ~/.trirouter/tmp/cli_prompt.txt`
   Take `--model` / `--effort` from the [router] context when it names them; otherwise omit them
   (the CLI's default model is used).
3. For a verify pass, compare it against your own result. List the differences and justify which
   one is correct. Do not blindly overwrite your own.
4. Exit code 2 or 3 (not installed, cloud sandbox, timeout): report it in one line, and continue
   with the `opus-worker-high` subagent (Claude) or on your own.
5. The other CLI runs read-only: we ask for an opinion, not for file writes.
