#!/usr/bin/env bash
# KittenCodeTTS — one-shot installer.
#
# Gives Claude Code and/or GitHub Copilot CLI a spoken voice: notifications
# and end-of-turn messages are read aloud by a tiny local TTS model.
#
#   from a checkout:   bash install.sh
#   from anywhere:     curl -fsSL https://raw.githubusercontent.com/lozenge0/KittenCodeTTS/main/install.sh | bash
#
# Flags: --claude --copilot --both   preselect targets (skips the menu)
#        --no-test                   skip the spoken confirmation at the end
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

# --- flags -----------------------------------------------------------------
WANT_CLAUDE=0 WANT_COPILOT=0 ASKED=0 NO_TEST=0
for a in "$@"; do
  case "$a" in
    --claude)  WANT_CLAUDE=1; ASKED=1 ;;
    --copilot) WANT_COPILOT=1; ASKED=1 ;;
    --both)    WANT_CLAUDE=1; WANT_COPILOT=1; ASKED=1 ;;
    --no-test) NO_TEST=1 ;;
    *) die "unknown flag: $a (use --claude, --copilot, --both, --no-test)" ;;
  esac
done

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

HAVE_CLAUDE=0 HAVE_COPILOT=0
command -v claude  >/dev/null 2>&1 && HAVE_CLAUDE=1
command -v copilot >/dev/null 2>&1 && HAVE_COPILOT=1
[ "$HAVE_CLAUDE" = 1 ] || [ "$HAVE_COPILOT" = 1 ] \
  || die "neither 'claude' nor 'copilot' found on PATH - install one first"

# --- choose targets ----------------------------------------------------------
if [ "$ASKED" = 0 ]; then
  say "Detected CLIs:"
  [ "$HAVE_CLAUDE" = 1 ]  && note "claude  (Claude Code)"
  [ "$HAVE_COPILOT" = 1 ] && note "copilot (GitHub Copilot CLI)"
  if [ "$HAVE_CLAUDE" = 1 ] && [ "$HAVE_COPILOT" = 1 ]; then
    printf 'Install the voice for [1] Claude Code, [2] Copilot CLI, or [3] both? [3] '
    read -r pick < /dev/tty || pick=3
    case "${pick:-3}" in
      1) WANT_CLAUDE=1 ;;
      2) WANT_COPILOT=1 ;;
      3|"") WANT_CLAUDE=1; WANT_COPILOT=1 ;;
      *) die "unrecognized choice: $pick" ;;
    esac
  else
    WANT_CLAUDE=$HAVE_CLAUDE; WANT_COPILOT=$HAVE_COPILOT
  fi
fi
[ "$WANT_CLAUDE" = 1 ] && [ "$HAVE_CLAUDE" = 0 ] && die "--claude requested but 'claude' not found on PATH"
[ "$WANT_COPILOT" = 1 ] && [ "$HAVE_COPILOT" = 0 ] && die "--copilot requested but 'copilot' not found on PATH"

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

# --- wire up Claude Code ------------------------------------------------------
if [ "$WANT_CLAUDE" = 1 ]; then
  say "Configuring Claude Code (~/.claude/settings.json)"
  mkdir -p "$HOME/.claude"
  HOOK_CMD="$HOOK_CMD" "$PY" - <<'PYEOF'
import json, os, shutil

path = os.path.expanduser("~/.claude/settings.json")
cmd = os.environ["HOOK_CMD"]
settings = {}
if os.path.exists(path):
    shutil.copy(path, path + ".kitten-backup")
    with open(path) as f:
        settings = json.load(f)

hooks = settings.setdefault("hooks", {})
for event in ("Notification", "Stop"):
    matchers = hooks.setdefault(event, [])
    # drop any previous kitten entries, then add the current one
    for m in matchers:
        m["hooks"] = [h for h in m.get("hooks", [])
                      if "kitten_voice.py" not in h.get("command", "")]
    matchers[:] = [m for m in matchers if m.get("hooks")]
    matchers.append({"hooks": [{"type": "command", "command": cmd}]})
settings.setdefault("env", {}).setdefault("KITTEN_STOP_MODE", "summary")

with open(path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
print("    updated", path, "(backup: settings.json.kitten-backup)")
PYEOF
fi

# --- wire up Copilot CLI ------------------------------------------------------
if [ "$WANT_COPILOT" = 1 ]; then
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

# --- confirm ------------------------------------------------------------------
if [ "$NO_TEST" = 0 ]; then
  say "Testing the voice (you should hear it)"
  echo '{"text":"The kitten voice is installed.","voice":"Kiki"}' \
    | "$PY" "$KITTEN_HOME/kitten_voice.py" --worker \
    || note "test playback failed - check $KITTEN_HOME/kitten_voice.log"
fi

say "Done. Final steps:"
[ "$WANT_CLAUDE" = 1 ]  && note "Claude Code: restart it (or open /hooks once) to load the new hooks."
[ "$WANT_COPILOT" = 1 ] && note "Copilot CLI: restart any running session; hooks load from ~/.copilot/hooks."
note "Long replies are summarized via 'claude -p' when available; otherwise the opening sentences are read."
note "Tune with env vars (KITTEN_VOICE, KITTEN_STOP_MODE, ...) - see the README."
