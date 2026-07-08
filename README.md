# KittenCodeTTS 🐱🔊

A spoken voice for your terminal AI coding agents — **Claude Code**,
**GitHub Copilot CLI**, **OpenAI Codex CLI**, **Gemini CLI**, and
**OpenCode**. Your agent talks to you:

- **"Claude needs your permission to run…"** — hear notifications the moment
  the agent wants you, without watching the terminal.
- **End of turn** — the agent's final message is read aloud. Short replies are
  read in full (markdown and code stripped); long ones are first condensed to
  1–2 spoken sentences. Turns that end on a tool call get a short "Done."
  chime instead.

Speech is generated **locally** by [KittenTTS](https://github.com/KittenML/KittenTTS),
a ~15M-parameter open-source TTS model — no audio ever leaves your machine.

Works on **macOS and Linux**, with any combination of supported tools:

| Tool | Wired via | Status |
|---|---|---|
| Claude Code | `~/.claude/settings.json` (Notification + Stop hooks) | ✅ verified end-to-end |
| GitHub Copilot CLI 1.x | `~/.copilot/hooks/kitten-voice.json` (notification + agentStop) | ✅ verified end-to-end |
| OpenAI Codex CLI | `~/.codex/config.toml` (`notify`, agent-turn-complete) | 🧪 built from official docs, not yet live-verified |
| Gemini CLI | `~/.gemini/settings.json` (Notification + AfterAgent hooks) | 🧪 built from official docs, not yet live-verified |
| OpenCode | `~/.config/opencode/plugin/kitten-voice.js` (session.idle) | 🧪 built from official docs, not yet live-verified |

If a 🧪 tool misbehaves for you, please open an issue with the relevant
config/payload — the adapters are small and easy to fix. (Codex and Gemini
deliver the final message inline in their payloads, so those adapters skip
transcript parsing entirely; Codex fires only on completed turns, so it has
no notification speech.)

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/lozenge0/KittenCodeTTS/main/install.sh | bash
```

or from a checkout: `bash install.sh`

The installer detects which CLIs you have, asks which to wire up
(`--claude`, `--copilot`, `--codex`, `--gemini`, `--opencode`, or `--all`
skip the menu; `--no-test` skips the spoken confirmation), then:

1. builds a self-contained engine in `~/.kitten-voice/` (venv ~300 MB; the
   voice model, ~80 MB, is fetched from Hugging Face and cached),
2. wires the hooks per the table above. Config files that hold your other
   settings (Claude, Gemini, Codex) get a `.kitten-backup` copy first, are
   merged rather than overwritten, and re-running never duplicates entries.
   If Codex already has a `notify` program configured (it allows only one),
   yours is left untouched and the line to add manually is printed.

The only manual step: **restart your CLI** (or open `/hooks` once in Claude
Code) so the new hook config is loaded. Codex needs no restart.

Requirements: python3 ≥ 3.9; on Linux, one of `paplay`, `aplay`, or `ffplay`
for playback (macOS uses the built-in `afplay`). Copilot CLI must be 1.x+ —
older versions ignore hooks silently (`copilot update`).

## Uninstall

Delete `~/.kitten-voice`, then remove whichever of these you installed: the
`kitten_voice.py` hook entries in `~/.claude/settings.json` and
`~/.gemini/settings.json`, the `notify` line in `~/.codex/config.toml`,
`~/.copilot/hooks/kitten-voice.json`, and
`~/.config/opencode/plugin/kitten-voice.js`.

## How it works

`kitten_voice.py` runs in two stages so the agent is never blocked: the hook
reads the event JSON and instantly hands off to a detached worker. The worker
waits for the transcript file to stop growing (stop events can fire before
the final message is flushed — reading too early speaks a stale mid-turn
message), extracts the final message with a format-specific parser (Claude
Code stores one JSONL line per content block; Copilot stores
`assistant.message` events), cleans or summarizes it, synthesizes with
KittenTTS, and plays it under a file lock so overlapping turns don't talk
over each other.

Copilot's hook payloads carry no event name, so its hooks are registered with
an explicit `--event agentStop` / `--event notification` argument. Aborted
Copilot turns (`stopReason` ≠ `end_turn`) stay silent. The hook never crashes
a session: every error path exits 0 and is logged to
`~/.kitten-voice/kitten_voice.log`.

The TTS engine is pip-installed from upstream `KittenML/KittenTTS`, pinned to
a known-good commit (override with `KITTEN_ENGINE_REF=<sha>`).

## Privacy & cost — read this once

- Speech synthesis is fully local. Nothing is sent anywhere to make audio.
- **Long replies are summarized by the same tool that produced them**: a
  Claude Code reply is condensed by `claude -p` (a small Haiku call), a
  Copilot reply by `copilot -p` (a premium request), Gemini by `gemini -p`,
  Codex by `codex exec` (read-only sandbox), OpenCode by `opencode run` —
  so nothing leaves the AI tool you were already talking to, and billing
  stays put. If the native tool fails, one other installed CLI is tried;
  if none work, the opening sentences are read instead — no network
  involved. Set `KITTEN_MAX_CHARS` higher to summarize less often, or
  `KITTEN_STOP_MODE=chime` to never send anything.
- The log file records a snippet of each spoken line for debugging. It stays
  on your machine; delete it whenever.

## Tuning (env vars)

For Claude Code set these in the `env` block of `~/.claude/settings.json`;
for Copilot export them in your shell profile. Or edit the defaults at the
top of `~/.kitten-voice/kitten_voice.py`.

| Var | Default | Meaning |
|---|---|---|
| `KITTEN_VOICE` | `Kiki` | Voice: Bella, Jasper, Luna, Bruno, Rosie, Hugo, Kiki, Leo |
| `KITTEN_MODEL` | `KittenML/kitten-tts-mini-0.8` | Any KittenTTS HF model id (nano=fastest, mini=best quality) |
| `KITTEN_STOP_MODE` | `summary` | `summary` (read short msgs whole, LLM-summarize long ones) · `chime` (fixed phrase) · `full` (whole msg, can be slow) · `off` |
| `KITTEN_CHIME` | `Done.` | Phrase for `chime` mode and for turns that end on a tool call |
| `KITTEN_NOTIFY` | `on` | `off` disables notification speech |
| `KITTEN_MAX_CHARS` | `400` | Messages up to this long are read whole; longer ones get summarized |
| `KITTEN_SUMMARY_MODEL` | `haiku` | Model passed to `claude -p` for summaries |
| `KITTEN_DISABLE` | unset | `1` silences the hook entirely (also set internally on nested summarizer calls so they can't re-trigger the hook) |

## Manual test

```bash
# speak a phrase directly (worker mode)
echo '{"text":"Hello from Kitten.","voice":"Luna"}' | ~/.kitten-voice/venv/bin/python ~/.kitten-voice/kitten_voice.py --worker

# full stop-event path against a real transcript
echo '{"hook_event_name":"Stop","transcript_path":"/path/to/session.jsonl"}' | ~/.kitten-voice/venv/bin/python ~/.kitten-voice/kitten_voice.py
```

Unit tests (stdlib only, no audio): `python3 tests/test_hook.py`

## Known quirks

- espeak-ng (the phonemizer backend) has a ~160-char internal path buffer;
  the installer refuses unusually deep `$HOME` paths rather than fail
  mysteriously.
- Copilot CLI versions before 1.x ignore hooks silently — run `copilot update`.

## Credits & license

Speech by [KittenTTS](https://github.com/KittenML/KittenTTS) (Apache 2.0) —
this project just gives it a job. KittenCodeTTS itself is [MIT](LICENSE).
