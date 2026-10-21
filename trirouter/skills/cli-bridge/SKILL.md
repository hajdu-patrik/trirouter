---
name: cli-bridge
description: Run a task or get an independent second opinion from the Codex (ChatGPT) or Antigravity (Google Gemini) CLI. Use when the [router] context mentions cli-bridge, or the user writes #codex or #antigravity.
---

# cli-bridge

1. The other model does not see this conversation. Write a complete, self-contained prompt into a
   NEW file with a unique name under `~/.trirouter/tmp/` (e.g. `cli_prompt_<timestamp>_<random>.txt`;
   parallel bridges must never share a file): the task, relevant file names and code snippets, the
   expected output shape, and the language to answer in (the user's language). Never put a
   secret (key, password, token) in it. The script deletes the file once it has read it.
2. Run it (works from any folder; `~/.skills/cli-bridge` is the shared skill hub):
   `python ~/.skills/cli-bridge/ask_cli.py codex --model <model> --effort <effort> --prompt-file <file>`
   `python ~/.skills/cli-bridge/ask_cli.py antigravity --model <model> --prompt-file <file>`
   Take `--model` / `--effort` from the [router] context when it names them; otherwise omit them
   (the CLI's default model is used). Without `--prompt-file` the prompt is read from stdin.
3. For a verify pass, compare it against your own result. List the differences and justify which
   one is correct. Do not blindly overwrite your own.
4. Exit code 2 or 3 (not installed, cloud sandbox, timeout, a prompt file that cannot be read):
   report it in one line, and continue with the `opus-worker-high` subagent (Claude) or on your own.
5. The other CLI runs read-only: we ask for an opinion, not for file writes.
