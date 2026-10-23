# Speech-to-Text (dictation)

Dictate prompts into Claude, ChatGPT/Codex, Antigravity, a terminal or any other app. A system-wide dictation tool
types the recognized text into whatever field has focus, in any language Whisper supports, offline and free.

## Desktop: Handy (offline)

[Handy](https://github.com/cjpais/Handy) is an open-source push-to-talk dictation app running Whisper models locally.

| OS | Install |
| --- | --- |
| Windows | `winget install --id cjpais.Handy -e` (`trirouter setup` offers it) |
| macOS | the `.dmg` from the [releases](https://github.com/cjpais/Handy/releases) |
| Linux | the `.AppImage` or `.deb` from the [releases](https://github.com/cjpais/Handy/releases) |

Allow microphone access on first start (macOS: also the Accessibility permission Handy needs to type into other apps).

| Model (Handy → Models) | Size | Hardware |
| --- | --- | --- |
| Whisper Medium | 0.83 GB | any CPU with 8 GB RAM; good balance without a GPU |
| Whisper Large v3 Turbo | 0.89 GB | GPU with ≥ 4 GB VRAM or Apple Silicon; near Large v3 quality, much faster |

Every model also runs on the CPU, only slower; English-only models (`*.en`) are faster but transcribe nothing else.

Configure: **General → Language** set explicitly (*Auto* guesses wrong on short phrases); the shortcut (default
`Ctrl+Space`, hold to talk); **Advanced → Launch on Startup**, **Start Hidden** and **Custom Words** for names and
jargon (*Claude, Codex, Antigravity, commit, deploy*). Then click into any text field, hold the shortcut, speak, release.

Fallbacks without extra software: Windows `Win+H`, macOS Dictation (press Fn/🌐 twice).

## Phone

Use the keyboard's own microphone, not the AI app's voice button: Android Gboard → Settings → Voice typing →
Languages; iOS Settings → General → Keyboard → Dictation. Dictate into the Claude, ChatGPT or Antigravity app (or its
remote session to your computer); the prompt is sent like typed text and routed as usual.
