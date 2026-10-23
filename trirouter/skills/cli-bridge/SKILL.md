---
name: cli-bridge
description: Run a task or get an independent second opinion from the Codex (ChatGPT) or Antigravity (Google Gemini) CLI. Use when the [router] context mentions cli-bridge, or the user writes #codex or #antigravity.
---

# cli-bridge

1. The other model does not see this conversation: write a complete, self-contained prompt (task, relevant file
   names and snippets, expected output shape, the language to answer in) into a NEW uniquely named file under
   `~/.trirouter/tmp/`. Never put a secret in it; the script deletes the file once read.
2. Run it from any folder, with `--model` / `--effort` from the [router] context when it names them (else omit them):
   `python ~/.skills/cli-bridge/ask_cli.py codex --model <model> --effort <effort> --prompt-file <file>`
   `python ~/.skills/cli-bridge/ask_cli.py antigravity --model <model> --prompt-file <file>`
3. The other CLI runs read-only. For a verify pass compare its answer with your own result, list the differences and
   justify which is correct; never blindly overwrite your own.
4. Exit code 2 or 3 (not installed, cloud sandbox, timeout, unreadable prompt file): report it in one line and continue
   with the `opus-worker-high` subagent (Claude) or on your own.
