# Speech-to-Text (dictation) Guide

Dictate prompts by voice into Claude, ChatGPT/Codex, Antigravity, a terminal or any other app.
Every step here is free; nothing leaves your computer on the desktop path.

The built-in voice buttons of some AI apps support only a limited set of languages. A system-wide
dictation tool avoids that limitation: it types the recognized text into whatever field has focus.

---

## Desktop: Handy (recommended, offline)

[Handy](https://github.com/cjpais/Handy) is an open-source push-to-talk dictation app that runs
Whisper-family models locally.

### 1. Install

| OS | Command / download |
| --- | --- |
| Windows | `winget install --id cjpais.Handy -e` (the installer offers this during `trirouter setup`, or `python install.py` on the first run) |
| macOS | download the `.dmg` from the [Handy releases](https://github.com/cjpais/Handy/releases) |
| Linux | download the `.AppImage` or `.deb` from the [Handy releases](https://github.com/cjpais/Handy/releases) |

On first start, allow microphone access (and on macOS the Accessibility permission Handy needs to
type into other apps).

### 2. Choose a model for your hardware

Handy → **Models**. Sizes are for the Q8 (8-bit) Whisper builds Handy downloads.

| Model | Download | Suggested hardware (guideline) | Notes |
| --- | --- | --- | --- |
| Whisper Small | 0.27 GB | any modern CPU | fastest multilingual option, lowest accuracy |
| Whisper Medium | 0.83 GB | CPU with 8 GB RAM, or any GPU | good balance on laptops without a dedicated GPU |
| Whisper Large v3 Turbo | 0.89 GB | GPU with ≥ 4 GB VRAM or Apple Silicon | near Large v3 quality, much faster |
| Whisper Large v3 | 1.67 GB | GPU with ≥ 6 GB VRAM or Apple Silicon (16 GB) | best accuracy, especially for less common languages and technical terms |

A GPU is optional: every model runs on the CPU, only slower. English-only models (`*.en`,
Parakeet) are faster but cannot transcribe other languages.

### 3. Configure

1. **Models:** select the downloaded model (click it after the download reaches 100 %).
2. **General → Language:** set your language explicitly. *Auto* detection often guesses wrong on
   short phrases.
3. **General → shortcut:** default `Ctrl+Space` (hold to talk, or press once to toggle).
4. **Advanced → Launch on Startup** and **Start Hidden:** on, so dictation is always available.
5. **Advanced → Custom Words:** add names and jargon you use often (for example *Claude, Codex,
   Antigravity, commit, deploy*).
6. **Advanced → Unload Model:** *Never* keeps it instant (the model stays in memory); choose a
   timeout if you need the memory for other workloads.

### 4. Use

Click into any text field → hold the shortcut → speak → release. The text is pasted at the cursor.

### Troubleshooting

| Symptom | Fix |
| --- | --- |
| Nothing is typed | check the microphone permission and that the correct input device is selected in Handy |
| Wrong language / gibberish | set the language explicitly instead of *Auto* |
| Slow transcription | use Large v3 Turbo or Medium, or enable GPU acceleration in Handy's settings |
| Technical terms misspelled | add them under *Custom Words* |

Fallbacks without extra software: Windows `Win+H` (Voice Typing), macOS *Dictation*
(press the Fn/🌐 key twice).

---

## Phone

Use the **keyboard's own microphone**, not the AI app's voice button:

- **Android:** Gboard → Settings → Voice typing → Languages: add your language.
- **iOS:** Settings → General → Keyboard → Dictation on, and add a keyboard in your language.

Open the Claude, ChatGPT or Antigravity app (or its remote session to your computer), tap the text
field, tap the keyboard's microphone and speak. The dictated prompt is sent like typed text – when
you control your computer remotely, it runs there with the router as usual.
