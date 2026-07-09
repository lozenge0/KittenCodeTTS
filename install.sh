#!/usr/bin/env bash
# KittenCodeTTS — one-shot installer.
#
# Gives your terminal AI coding agents a spoken voice: notifications and
# end-of-turn messages are read aloud by a tiny local TTS model.
#
# Supported: Claude Code, GitHub Copilot CLI, OpenAI Codex CLI, Gemini CLI,
# OpenCode.
#
#   from a checkout:   bash install.sh
#   from anywhere:     curl -fsSL https://raw.githubusercontent.com/lozenge0/KittenCodeTTS/main/install.sh | bash
#
# Flags: --claude --copilot --codex --gemini --opencode   preselect targets
#        --all        wire up every detected CLI (skips the menu)
#        --both       shorthand for --claude --copilot
#        --no-test    skip the spoken confirmation at the end
#
# Everything lands in ~/.kitten-voice (script, venv, log). macOS and Linux.
set -euo pipefail

# The TTS engine (the kittentts package) comes from the public upstream
# repo, pinned to a known-good commit so upstream changes can't break us.
ENGINE_REF="${KITTEN_ENGINE_REF:-9f3e0d8b6600b56ebe1b4d7b6d8e1e020077d1f2}"
ENGINE_URL="https://github.com/KittenML/KittenTTS/archive/$ENGINE_REF.tar.gz"
# Where to fetch the hook script when running via `curl | bash`.
HOOK_URL="${KITTEN_HOOK_URL:-https://raw.githubusercontent.com/lozenge0/KittenCodeTTS/main/kitten_voice.py}"

KITTEN_HOME="$HOME/.kitten-voice"
VENV="$KITTEN_HOME/venv"
PY="$VENV/bin/python"
# expanded per-machine at install time; literal $HOME would depend on every
# CLI shell-expanding its hook commands
HOOK_CMD="$HOME/.kitten-voice/venv/bin/python $HOME/.kitten-voice/kitten_voice.py"

say()  { printf '\033[1;36m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# macOS ships bash 3.2 (no associative arrays), so tool selection uses
# eval-built scalar variables: WANT_claude, HAVE_codex, ...
TOOLS="claude copilot codex gemini opencode"
tool_desc() {
  case "$1" in
    claude) echo "Claude Code" ;;
    copilot) echo "GitHub Copilot CLI" ;;
    codex) echo "OpenAI Codex CLI" ;;
    gemini) echo "Gemini CLI" ;;
    opencode) echo "OpenCode" ;;
  esac
}
want() { eval "echo \$WANT_$1"; }
have() { eval "echo \$HAVE_$1"; }

# --- flags -----------------------------------------------------------------
for t in $TOOLS; do eval "WANT_$t=0 HAVE_$t=0"; done
ASKED=0 ALL=0 NO_TEST=0 SUMMARIZER="" EXPECT_SUM=0
for a in "$@"; do
  if [ "$EXPECT_SUM" = 1 ]; then SUMMARIZER="$a"; EXPECT_SUM=0; continue; fi
  case "$a" in
    --claude|--copilot|--codex|--gemini|--opencode) eval "WANT_${a#--}=1"; ASKED=1 ;;
    --both)    WANT_claude=1; WANT_copilot=1; ASKED=1 ;;
    --all)     ALL=1; ASKED=1 ;;
    --no-test) NO_TEST=1 ;;
    --summarizer)   EXPECT_SUM=1 ;;
    --summarizer=*) SUMMARIZER="${a#*=}" ;;
    *) die "unknown flag: $a (use --claude, --copilot, --codex, --gemini, --opencode, --all, --summarizer local|native, --no-test)" ;;
  esac
done
case "$SUMMARIZER" in ""|local|native) ;; *) die "--summarizer must be 'local' or 'native'" ;; esac

# --- preflight ---------------------------------------------------------------
case "$(uname)" in
  Darwin) ;;
  Linux)
    command -v paplay >/dev/null 2>&1 || command -v aplay >/dev/null 2>&1 \
      || command -v ffplay >/dev/null 2>&1 \
      || die "no audio player found - install one first (e.g. apt install pulseaudio-utils, alsa-utils, or ffmpeg)" ;;
  *) die "unsupported OS: $(uname) (macOS and Linux only)" ;;
esac
command -v python3 >/dev/null 2>&1 || die "python3 not found - install it first"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "python3 is too old - need 3.9+"
# espeak-ng (the phonemizer backend) uses fixed ~160-char path buffers and
# dies silently past them; site-packages adds ~75 chars to $HOME.
[ ${#HOME} -le 80 ] || die "\$HOME is too deep for espeak-ng's path buffer (${#HOME} chars, max ~80)"

DETECTED=""
for t in $TOOLS; do
  if command -v "$t" >/dev/null 2>&1; then eval "HAVE_$t=1"; DETECTED="$DETECTED $t"; fi
done
DETECTED="${DETECTED# }"
[ -n "$DETECTED" ] || die "no supported CLI found on PATH (claude, copilot, codex, gemini, opencode)"
set -- $DETECTED; NDET=$#

# --- choose targets ----------------------------------------------------------
if [ "$ALL" = 1 ]; then
  for t in $DETECTED; do eval "WANT_$t=1"; done
elif [ "$ASKED" = 0 ]; then
  if [ "$NDET" -eq 1 ]; then
    eval "WANT_$DETECTED=1"
  else
    say "Detected CLIs:"
    i=1
    for t in $DETECTED; do
      note "[$i] $t ($(tool_desc "$t"))"
      i=$((i+1))
    done
    printf 'Install the voice for which? (numbers separated by spaces, or "all") [all] '
    read -r pick < /dev/tty || pick=all
    if [ -z "$pick" ] || [ "$pick" = "all" ]; then
      for t in $DETECTED; do eval "WANT_$t=1"; done
    else
      for n in $pick; do
        case "$n" in
          *[!0-9]*) die "unrecognized choice: $n" ;;
        esac
        [ "$n" -ge 1 ] && [ "$n" -le "$NDET" ] || die "choice out of range: $n"
        eval "WANT_$(eval "echo \${$n}")=1"
      done
    fi
  fi
fi
for t in $TOOLS; do
  [ "$(want "$t")" = 1 ] && [ "$(have "$t")" = 0 ] && die "--$t requested but '$t' not found on PATH"
done

# --- choose how long replies get summarized -----------------------------------
if [ -z "$SUMMARIZER" ]; then
  say "How should long replies be summarized before being spoken?"
  note "[1] locally - the key sentences are extracted on-device: instant, offline, nothing is ever sent anywhere"
  note "[2] with the coding tool itself (claude -p, copilot -p, ...) - richer summaries, uses your plan's tokens"
  printf 'Choice [1] '
  read -r spick < /dev/tty || spick=1
  case "${spick:-1}" in
    1|"") SUMMARIZER=local ;;
    2)    SUMMARIZER=native ;;
    *) die "unrecognized choice: $spick" ;;
  esac
fi

# --- install the engine --------------------------------------------------------
say "Setting up $KITTEN_HOME (self-contained venv, ~300 MB)"
mkdir -p "$KITTEN_HOME"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
say "Installing the KittenTTS engine (pinned to KittenML/KittenTTS@${ENGINE_REF:0:7})"
"$VENV/bin/pip" install --quiet "$ENGINE_URL"

# The hook script: next to this installer in a checkout, downloaded otherwise.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo "")"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/kitten_voice.py" ]; then
  cp "$SCRIPT_DIR/kitten_voice.py" "$KITTEN_HOME/kitten_voice.py"
else
  say "Downloading the hook script"
  curl -fsSL "$HOOK_URL" -o "$KITTEN_HOME/kitten_voice.py" || die "could not fetch kitten_voice.py"
fi

say "Downloading the voice model (~80 MB, one-time, cached by Hugging Face)"
"$PY" -c "from kittentts import KittenTTS; import os; KittenTTS(os.environ.get('KITTEN_MODEL','KittenML/kitten-tts-mini-0.8'))" \
  || note "model download failed (offline?) - it will retry on first use"

printf '{\n  "summarizer": "%s"\n}\n' "$SUMMARIZER" > "$KITTEN_HOME/config.json"

# Merge our hook entries into a Claude-style settings.json without touching
# anything else. Used for Claude Code and Gemini CLI (same schema).
merge_json_hooks() {  # $1=settings path  $2=space-separated events  $3=1 to set env
  SETTINGS_PATH="$1" HOOK_EVENTS="$2" SET_ENV="${3:-0}" HOOK_CMD="$HOOK_CMD" "$PY" - <<'PYEOF'
import json, os, shutil

path = os.path.expanduser(os.environ["SETTINGS_PATH"])
cmd = os.environ["HOOK_CMD"]
events = os.environ["HOOK_EVENTS"].split()
settings = {}
if os.path.exists(path):
    shutil.copy(path, path + ".kitten-backup")
    with open(path) as f:
        settings = json.load(f)

hooks = settings.setdefault("hooks", {})
for event in events:
    matchers = hooks.setdefault(event, [])
    # drop any previous kitten entries, then add the current one
    for m in matchers:
        m["hooks"] = [h for h in m.get("hooks", [])
                      if "kitten_voice.py" not in h.get("command", "")]
    matchers[:] = [m for m in matchers if m.get("hooks")]
    matchers.append({"hooks": [{"type": "command", "command": cmd}]})
if os.environ.get("SET_ENV") == "1":
    settings.setdefault("env", {}).setdefault("KITTEN_STOP_MODE", "summary")

os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
print("    updated", path, "(backup: " + os.path.basename(path) + ".kitten-backup)")
PYEOF
}

# --- wire up Claude Code ------------------------------------------------------
if [ "$WANT_claude" = 1 ]; then
  say "Configuring Claude Code (~/.claude/settings.json)"
  merge_json_hooks "$HOME/.claude/settings.json" "Notification Stop" 1
fi

# --- wire up Copilot CLI ------------------------------------------------------
if [ "$WANT_copilot" = 1 ]; then
  say "Configuring Copilot CLI (~/.copilot/hooks/kitten-voice.json)"
  mkdir -p "$HOME/.copilot/hooks"
  cat > "$HOME/.copilot/hooks/kitten-voice.json" <<EOF
{
  "version": 1,
  "hooks": {
    "agentStop": [
      { "type": "command", "bash": "$HOOK_CMD --event agentStop", "timeoutSec": 30 }
    ],
    "notification": [
      { "type": "command", "bash": "$HOOK_CMD --event notification", "timeoutSec": 30 }
    ]
  }
}
EOF
  # Hooks arrived in Copilot CLI 1.x; 0.0.x versions silently ignore them.
  case "$(copilot --version 2>/dev/null)" in
    *" 0."*) note "your copilot version looks old - run: copilot update" ;;
  esac
fi

# --- wire up Codex CLI ---------------------------------------------------------
if [ "$WANT_codex" = 1 ]; then
  say "Configuring Codex CLI (~/.codex/config.toml)"
  mkdir -p "$HOME/.codex"
  CFG="$HOME/.codex/config.toml"
  touch "$CFG"
  cp "$CFG" "$CFG.kitten-backup"
  NOTIFY_LINE="notify = [\"$VENV/bin/python\", \"$KITTEN_HOME/kitten_voice.py\", \"--event\", \"codex-notify\"]"
  if grep -q '^notify *=' "$CFG"; then
    if grep '^notify *=' "$CFG" | grep -q 'kitten_voice.py'; then
      NOTIFY_LINE="$NOTIFY_LINE" "$PY" - "$CFG" <<'PYEOF'
import os, re, sys
path = sys.argv[1]
with open(path) as f:
    s = f.read()
s = re.sub(r'^notify *=.*kitten_voice\.py.*$', os.environ["NOTIFY_LINE"].replace("\\", "\\\\"), s, flags=re.M)
with open(path, "w") as f:
    f.write(s)
print("    updated", path, "(backup: config.toml.kitten-backup)")
PYEOF
    else
      note "config.toml already has a 'notify' program (Codex allows one) - left untouched."
      note "To use the voice, point it at: $NOTIFY_LINE"
    fi
  else
    # top-level TOML keys must appear before any [section]; prepend
    printf '%s\n%s\n' "$NOTIFY_LINE" "$(cat "$CFG")" > "$CFG.tmp" && mv "$CFG.tmp" "$CFG"
    note "updated $CFG (backup: config.toml.kitten-backup)"
  fi
fi

# --- wire up Gemini CLI --------------------------------------------------------
if [ "$WANT_gemini" = 1 ]; then
  say "Configuring Gemini CLI (~/.gemini/settings.json)"
  merge_json_hooks "$HOME/.gemini/settings.json" "Notification AfterAgent" 0
fi

# --- wire up OpenCode ----------------------------------------------------------
if [ "$WANT_opencode" = 1 ]; then
  say "Configuring OpenCode (~/.config/opencode/plugin/kitten-voice.js)"
  mkdir -p "$HOME/.config/opencode/plugin"
  cat > "$HOME/.config/opencode/plugin/kitten-voice.js" <<EOF
// KittenCodeTTS voice plugin - speaks the final message when a session goes
// idle. Generated by install.sh; safe to delete to uninstall.
import { spawn } from "node:child_process"

const PY = "$VENV/bin/python"
const SCRIPT = "$KITTEN_HOME/kitten_voice.py"

function speak(job) {
  try {
    const p = spawn(PY, [SCRIPT, "--worker"],
      { stdio: ["pipe", "ignore", "ignore"], detached: true })
    p.on("error", () => {})
    p.stdin.write(JSON.stringify(job))
    p.stdin.end()
    p.unref()
  } catch {}
}

export const KittenVoice = async ({ client }) => {
  const lastText = new Map() // sessionID -> latest streamed assistant text
  return {
    event: async ({ event }) => {
      // A nested summarizer run must not re-trigger the voice.
      if (process.env.KITTEN_DISABLE === "1") return
      try {
        if (event.type === "message.part.updated") {
          const part = event.properties?.part
          if (part?.type === "text" && part.sessionID) {
            lastText.set(part.sessionID, part.text ?? "")
          }
        } else if (event.type === "session.idle") {
          const id = event.properties?.sessionID ?? event.properties?.session?.id
          let text = ""
          try {
            const res = await client.session.messages({ path: { id } })
            const msgs = (res?.data ?? res ?? []).filter(
              (m) => (m.info?.role ?? m.role) === "assistant")
            const last = msgs[msgs.length - 1]
            const parts = last?.parts ?? []
            text = parts.filter((p) => p.type === "text")
              .map((p) => p.text ?? "").join("\n").trim()
          } catch {}
          if (!text && id) text = (lastText.get(id) ?? "").trim()
          speak({ mode: "message", text, source: "opencode" })
          if (id) lastText.delete(id)
        }
      } catch {}
    },
  }
}
EOF
fi

# --- confirm ------------------------------------------------------------------
if [ "$NO_TEST" = 0 ]; then
  say "Testing the voice (you should hear it)"
  echo '{"text":"The kitten voice is installed.","voice":"Kiki"}' \
    | "$PY" "$KITTEN_HOME/kitten_voice.py" --worker \
    || note "test playback failed - check $KITTEN_HOME/kitten_voice.log"
fi

say "Done. Final steps:"
[ "$WANT_claude" = 1 ]   && note "Claude Code: restart it (or open /hooks once) to load the new hooks."
[ "$WANT_copilot" = 1 ]  && note "Copilot CLI: restart any running session; hooks load from ~/.copilot/hooks."
[ "$WANT_codex" = 1 ]    && note "Codex CLI: no restart needed; notify fires on each completed turn."
[ "$WANT_gemini" = 1 ]   && note "Gemini CLI: restart any running session to load the new hooks."
[ "$WANT_opencode" = 1 ] && note "OpenCode: restart it to load the plugin."
if [ "$SUMMARIZER" = "local" ]; then
  note "Long replies are summarized on-device; nothing leaves your machine. (Switch: KITTEN_SUMMARIZER=native)"
else
  note "Long replies are summarized by the same CLI that produced them. (Switch: KITTEN_SUMMARIZER=local)"
fi
note "Tune with env vars (KITTEN_VOICE, KITTEN_STOP_MODE, ...) - see the README."
