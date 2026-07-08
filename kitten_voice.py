#!/usr/bin/env python3
"""KittenCodeTTS — a spoken voice for AI coding agents in your terminal.

Speaks notifications (when the agent needs your attention) and the agent's
final message when a turn ends, using KittenTTS — a tiny local text-to-speech
model. Install with install.sh, which wires this script into:

  Claude Code   ~/.claude/settings.json          events: Notification, Stop
  Copilot CLI   ~/.copilot/hooks/*.json          events: notification, agentStop
  Codex CLI     ~/.codex/config.toml             notify: agent-turn-complete
  Gemini CLI    ~/.gemini/settings.json          events: Notification, AfterAgent
  OpenCode      ~/.config/opencode/plugin/*.js   event: session.idle

Claude Code, Copilot, and Gemini pipe a JSON payload on stdin (Copilot's
carries no event name, so its hooks are registered with an explicit
`--event <name>` argument). Codex passes its payload as a trailing argv
argument and includes the final message inline; Gemini includes it as
prompt_response; OpenCode's plugin extracts it and calls --worker directly.

Two modes:
  hook mode (default): read the payload, then spawn a detached worker and
      return immediately so the agent is never blocked.
  --worker: read a job JSON from stdin. For stop jobs the worker (not the
      hook) extracts the final message from the transcript — it can afford to
      wait out the write race where the stop event fires before the
      transcript is fully flushed.

What gets spoken on stop (KITTEN_STOP_MODE=summary, the default):
  - message fits in KITTEN_MAX_CHARS -> read the whole thing
  - longer -> summarize the FULL message, two ways (chosen at install time,
    stored in config.json next to this script):
      local  -> a quantized DistilBART ONNX model on this machine (~230 MB,
                1-2s on CPU, nothing leaves the machine, works offline)
      native -> ask the tool the message came from (a Copilot session
                summarizes with `copilot -p`, a Gemini session with
                `gemini -p`, ...), trying one other installed CLI if the
                native one fails
    Either way a failure falls back to speaking the opening sentences.
  - turn ended on a tool call with no final text -> speak the chime instead
    of hunting backwards for stale mid-turn text

Design rule: this must NEVER break the session. Every failure path exits 0.
Errors (and each spoken line) are appended to kitten_voice.log next to this
file.

Tuning (all optional env vars):
  KITTEN_VOICE          voice name (default: Kiki). One of Bella, Jasper,
                        Luna, Bruno, Rosie, Hugo, Kiki, Leo.
  KITTEN_MODEL          HF model id (default: KittenML/kitten-tts-mini-0.8).
  KITTEN_STOP_MODE      summary | full | chime | off  (default: summary)
  KITTEN_CHIME          chime phrase (default: "Done.")
  KITTEN_NOTIFY         on | off  (default: on)
  KITTEN_MAX_CHARS      read-it-all threshold / spoken cap (default: 400)
  KITTEN_SUMMARIZER     local | native - overrides the install-time choice
  KITTEN_LOCAL_MODEL    HF repo of the local ONNX summarizer
                        (default: Xenova/distilbart-cnn-6-6)
  KITTEN_SUMMARY_MODEL  model passed to `claude -p` in native mode
                        (default: haiku)
  KITTEN_DISABLE        set to 1 to silence the hook entirely; also set on
                        nested `claude -p`/`copilot -p` calls so summarizing
                        a message can't recursively trigger this hook
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "kitten_voice.log")

VOICE = os.environ.get("KITTEN_VOICE", "Kiki")
MODEL = os.environ.get("KITTEN_MODEL", "KittenML/kitten-tts-mini-0.8")
STOP_MODE = os.environ.get("KITTEN_STOP_MODE", "summary").lower()
CHIME = os.environ.get("KITTEN_CHIME", "Done.")
NOTIFY = os.environ.get("KITTEN_NOTIFY", "on").lower()
MAX_CHARS = int(os.environ.get("KITTEN_MAX_CHARS", "400"))
SUMMARY_MODEL = os.environ.get("KITTEN_SUMMARY_MODEL", "haiku")


def _config():
    """Install-time choices written by install.sh next to this script."""
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


CONFIG = _config()
# "local" = on-device ONNX model, never calls any agent CLI; "native" = the
# CLI the message came from. Env var overrides the installed choice.
SUMMARIZER = os.environ.get(
    "KITTEN_SUMMARIZER", CONFIG.get("summarizer", "native")).lower()
LOCAL_MODEL = os.environ.get(
    "KITTEN_LOCAL_MODEL",
    CONFIG.get("local_model", "Xenova/distilbart-cnn-6-6"))

STOP_EVENTS = {"Stop", "agentStop"}
NOTIFY_EVENTS = {"Notification", "notification"}


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def clean_for_speech(text):
    """Strip markdown/code so the synthesizer speaks prose, not syntax."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)   # fenced code blocks
    text = re.sub(r"`([^`]*)`", r"\1", text)               # inline code
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text) # links/images -> label
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)  # headers
    text = re.sub(r"[*_>#]+", " ", text)                   # emphasis/quote marks
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)           # "bar ." -> "bar."
    return text.strip()


def truncate_sentences(text):
    """Cut cleaned text to MAX_CHARS, ending on a sentence boundary."""
    if len(text) <= MAX_CHARS:
        return text
    out = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if out and len(out) + len(sentence) + 1 > MAX_CHARS:
            break
        out = (out + " " + sentence).strip()
    if not out:  # first "sentence" already too long -> hard cut on a word
        out = text[:MAX_CHARS].rsplit(" ", 1)[0]
    return out


# ---------------------------------------------------------------------------
# Transcript reading (worker side)
# ---------------------------------------------------------------------------

def wait_for_stable(path, settle=1.0, timeout=8.0):
    """Wait until the transcript has stopped growing for `settle` seconds.

    Stop events can fire while the CLI is still flushing the final message
    to the transcript. The worker is detached, so a ~1s pause before
    synthesis is imperceptible — trade latency for not reading a stale
    message.
    """
    deadline = time.time() + timeout
    last_size = -1
    last_change = time.time()
    while time.time() < deadline:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = -1
        now = time.time()
        if size != last_size:
            last_size, last_change = size, now
        elif now - last_change >= settle:
            return
        time.sleep(0.1)


def _read_jsonl(path):
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except Exception:
                continue  # half-written trailing line etc.
    return entries


def _final_claude(entries):
    """Claude Code transcript: one line per content block of a message.

    Collect text across every line sharing the final message's id (grabbing
    just the newest text-bearing line can return stale mid-turn narration),
    and skip subagent (sidechain) lines so a background agent can't hijack
    the voice. Returns None if there are no main-thread assistant entries.
    """
    entries = [e for e in entries
               if e.get("type") == "assistant" and not e.get("isSidechain")]
    if not entries:
        return None
    final_id = entries[-1].get("message", {}).get("id")
    if final_id:
        group = [e for e in entries
                 if e.get("message", {}).get("id") == final_id]
    else:
        group = [entries[-1]]
    parts = []
    for e in group:
        content = e.get("message", {}).get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts += [b.get("text", "") for b in content
                      if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def _final_copilot(entries):
    """Copilot CLI events.jsonl: assistant text lives in assistant.message.

    Each turn's reply is {"type": "assistant.message", "data": {"content":
    "..."}}. Take the last one; empty content means the turn ended without
    final text. Returns None if there are no assistant.message entries.
    """
    msgs = [e for e in entries if e.get("type") == "assistant.message"]
    if not msgs:
        return None
    content = msgs[-1].get("data", {}).get("content")
    return content.strip() if isinstance(content, str) else ""


def final_message_text(path):
    """Text of the agent's final message, whichever CLI wrote the transcript.

    Returns None if the transcript is unreadable or matches no known format,
    and "" if the final message contained no text (turn ended on a tool
    call).
    """
    if not path or not os.path.exists(path):
        return None
    try:
        entries = _read_jsonl(path)
    except Exception as e:
        log(f"transcript read error: {e}")
        return None
    for parse in (_final_claude, _final_copilot):
        text = parse(entries)
        if text is not None:
            return text
    return None


# ---------------------------------------------------------------------------
# Summarization (worker side)
# ---------------------------------------------------------------------------

def _which(name, extra=()):
    import shutil
    exe = shutil.which(name)
    if exe:
        return exe
    for candidate in extra:
        candidate = os.path.expanduser(candidate)
        if os.access(candidate, os.X_OK):
            return candidate
    return None


TOOL_EXTRA_PATHS = {
    "claude": ("~/.claude/local/claude", "~/.local/bin/claude",
               "/opt/homebrew/bin/claude", "/usr/local/bin/claude"),
    "copilot": ("/opt/homebrew/bin/copilot", "/usr/local/bin/copilot"),
    "codex": ("/opt/homebrew/bin/codex", "/usr/local/bin/codex"),
    "gemini": ("/opt/homebrew/bin/gemini", "/usr/local/bin/gemini"),
    "opencode": ("/opt/homebrew/bin/opencode", "/usr/local/bin/opencode"),
}


def _summarizer_invocation(tool, exe, prompt, body):
    """Headless one-shot invocation for each supported CLI.

    claude/gemini/codex take the instruction as an argument and the message
    as piped stdin; copilot/opencode get both combined in the argument.
    codex exec streams progress to stderr and prints only the final message
    to stdout, in a read-only sandbox by default.
    """
    if tool == "claude":
        return [exe, "-p", prompt, "--model", SUMMARY_MODEL], body
    if tool == "copilot":
        return [exe, "-p", prompt + "\n\n" + body, "--log-level", "none"], ""
    if tool == "codex":
        return [exe, "exec", "--skip-git-repo-check", prompt], body
    if tool == "gemini":
        return [exe, "-p", prompt], body
    if tool == "opencode":
        return [exe, "run", prompt + "\n\n" + body], ""
    return None, None


def summarize_with_llm(text, source=None):
    """Real summary of the full message via a CLI. None on any failure.

    Uses the tool the message came from (a Copilot session summarizes with
    `copilot -p`, a Gemini session with `gemini -p`, ...) so billing and
    behavior stay within the tool you were already using. If the native
    tool is missing or fails, one other installed CLI is tried; after that
    the caller falls back to truncation. KITTEN_DISABLE=1 on the child
    stops the nested run's own stop hook / plugin from re-entering this
    script (infinite speech loop otherwise).
    """
    prompt = (
        "Condense the following assistant reply into at most two short plain "
        "sentences to be read aloud by a text-to-speech voice. Lead with the "
        "outcome. No markdown, no preamble, no quotes - reply with only the "
        "summary."
    )
    body = text[:8000]
    env = dict(os.environ, KITTEN_DISABLE="1")

    order = [t for t in ("claude", "copilot", "gemini", "codex", "opencode")
             if t != source]
    if source:
        order.insert(0, source)
    attempts = 0
    for tool in order:
        if attempts >= 2:  # native + one fallback keeps latency bounded
            break
        exe = _which(tool, TOOL_EXTRA_PATHS.get(tool, ()))
        if not exe:
            continue
        cmd, stdin = _summarizer_invocation(tool, exe, prompt, body)
        attempts += 1
        try:
            r = subprocess.run(cmd, input=stdin, capture_output=True,
                               text=True, timeout=60, env=env)
        except Exception as e:
            log(f"summarizer {tool} error: {e}")
            continue
        if r.returncode != 0:
            log(f"summarizer {tool} exit {r.returncode}: "
                f"{(r.stderr or '').strip()[:200]}")
            continue
        out = clean_for_speech(r.stdout)
        if out:
            return out
    log("summarizer: no CLI produced a summary, falling back to truncation")
    return None


def _dedupe_sentences(text):
    """Drop near-duplicate sentences (small seq2seq models love repeating)."""
    kept, seen_words = [], []
    for s in re.split(r"(?<=[.!?])\s+", text):
        words = set(re.findall(r"[a-z']+", s.lower()))
        if not words:
            continue
        if any(len(words & prev) / len(words) > 0.6 for prev in seen_words):
            continue
        kept.append(s.strip())
        seen_words.append(words)
    return " ".join(kept)


def _hf_file(repo, filename):
    from huggingface_hub import hf_hub_download
    try:  # prefer the cache so summarization works fully offline
        return hf_hub_download(repo, filename, local_files_only=True)
    except Exception:
        return hf_hub_download(repo, filename)


def summarize_local(text, min_new=20, max_new=90):
    """Fully on-device summary via a quantized DistilBART ONNX model.

    Greedy seq2seq decode with no-repeat-trigram blocking, then sentence
    dedupe. ~230 MB of model, 1-2s on CPU, nothing leaves the machine.
    Returns None on any failure (caller falls back to truncation, never to
    a cloud CLI - "local" must mean local).
    """
    try:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        enc = ort.InferenceSession(
            _hf_file(LOCAL_MODEL, "onnx/encoder_model_quantized.onnx"), opts,
            providers=["CPUExecutionProvider"])
        dec = ort.InferenceSession(
            _hf_file(LOCAL_MODEL, "onnx/decoder_model_quantized.onnx"), opts,
            providers=["CPUExecutionProvider"])
        tok = Tokenizer.from_file(_hf_file(LOCAL_MODEL, "tokenizer.json"))

        ids = tok.encode(text[:4000]).ids[:512]
        input_ids = np.array([ids], dtype=np.int64)
        attn = np.ones_like(input_ids)
        hidden = enc.run(None, {"input_ids": input_ids,
                                "attention_mask": attn})[0]
        out = [2, 0]  # BART: decoder_start (eos), then bos
        for step in range(max_new):
            logits = dec.run(None, {
                "input_ids": np.array([out], dtype=np.int64),
                "encoder_hidden_states": hidden,
                "encoder_attention_mask": attn,
            })[0][0, -1]
            if step < min_new:
                logits[2] = -1e9  # block EOS: force a fuller summary
            if len(out) >= 2:  # no-repeat-trigram blocking
                last2 = (out[-2], out[-1])
                for j in range(len(out) - 2):
                    if (out[j], out[j + 1]) == last2:
                        logits[out[j + 2]] = -1e9
            nxt = int(np.argmax(logits))
            if nxt == 2:
                break
            out.append(nxt)
        raw = tok.decode(out[2:], skip_special_tokens=True)
        return _dedupe_sentences(clean_for_speech(raw)) or None
    except Exception as e:
        log(f"local summarizer error: {e}")
        return None


def stop_text(transcript_path, source=None):
    """Decide what a stop event should say. None means stay silent."""
    if not transcript_path:
        return None
    wait_for_stable(transcript_path)
    msg = final_message_text(transcript_path)
    if msg is None:  # one late-flush retry before giving up
        time.sleep(1.0)
        msg = final_message_text(transcript_path)
    if msg is None:
        if os.path.exists(transcript_path):
            log("stop: unrecognized transcript format, chiming instead")
            return CHIME
        return None
    return render_stop_text(msg, source)


def render_stop_text(msg, source=None):
    """Turn a raw final message ('' = turn ended with no text) into speech."""
    if not msg:
        return CHIME
    text = clean_for_speech(msg)
    if STOP_MODE == "full" or len(text) <= MAX_CHARS:
        return text
    if SUMMARIZER == "local":
        summary = summarize_local(msg)
    else:
        summary = summarize_with_llm(msg, source)
    return truncate_sentences(summary if summary else text)


# ---------------------------------------------------------------------------
# Synthesis + playback (worker side)
# ---------------------------------------------------------------------------

def _player_cmd(wav):
    """First available audio player: macOS afplay, then Linux options."""
    import shutil
    if shutil.which("afplay"):
        return ["afplay", wav]
    if shutil.which("paplay"):
        return ["paplay", wav]
    if shutil.which("aplay"):
        return ["aplay", "-q", wav]
    if shutil.which("ffplay"):
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", wav]
    return None


def speak(text, voice):
    """Synthesize `text` and play it, serialized so turns don't overlap."""
    text = (text or "").strip()
    if not text:
        return
    import soundfile as sf
    from kittentts import KittenTTS

    model = KittenTTS(MODEL)
    audio = model.generate(text, voice=voice, clean_text=True)

    fd, wav = tempfile.mkstemp(prefix="kitten_", suffix=".wav")
    os.close(fd)
    try:
        sf.write(wav, audio, 24000)
        cmd = _player_cmd(wav)
        if not cmd:
            log("no audio player found (need afplay, paplay, aplay, or ffplay)")
            return
        # Serialize playback across concurrent workers with a file lock, so
        # two quick turns don't talk over each other.
        import fcntl
        lock = os.path.join(tempfile.gettempdir(), "kitten_voice.lock")
        with open(lock, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            log(f"speak: {text[:100]}")
            subprocess.run(cmd)
    finally:
        try:
            os.remove(wav)
        except Exception:
            pass


def run_worker():
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        log(f"worker payload error: {e}")
        return
    try:
        if payload.get("mode") == "stop":
            text = stop_text(payload.get("transcript_path"),
                             payload.get("source"))
        elif payload.get("mode") == "message":
            # final message delivered inline (Codex, Gemini, OpenCode)
            text = render_stop_text((payload.get("text") or "").strip(),
                                    payload.get("source"))
        else:
            text = payload.get("text", "")
        if text:
            speak(text, payload.get("voice", VOICE))
    except Exception as e:
        log(f"worker error: {e}")


# ---------------------------------------------------------------------------
# Hook entry (must return instantly)
# ---------------------------------------------------------------------------

def spawn_worker(job):
    try:
        p = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        p.stdin.write(json.dumps(job).encode())
        p.stdin.close()
    except Exception as e:
        log(f"spawn error: {e}")


def spawn_stop_message(text, source):
    """Speak a final message that arrived inline (no transcript involved)."""
    if STOP_MODE == "off":
        return
    if STOP_MODE == "chime":
        spawn_worker({"mode": "speak", "text": CHIME, "voice": VOICE})
        return
    spawn_worker({"mode": "message", "text": text or "", "voice": VOICE,
                  "source": source})


def run_hook(forced_event=None, argv_payload=None):
    if os.environ.get("KITTEN_DISABLE") == "1":
        return

    if forced_event == "codex-notify":
        # Codex passes the payload as a trailing argv argument, not stdin,
        # and its notify hook only fires for completed turns.
        try:
            data = json.loads(argv_payload or "{}")
        except Exception:
            return
        if data.get("type") != "agent-turn-complete":
            return
        spawn_stop_message(data.get("last-assistant-message"), "codex")
        return

    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    event = forced_event or data.get("hook_event_name", "")

    if event == "AfterAgent":
        # Gemini CLI includes the final response text in the payload.
        spawn_stop_message(data.get("prompt_response"), "gemini")
    elif event in NOTIFY_EVENTS:
        if NOTIFY != "on":
            return
        text = (data.get("message") or data.get("title") or data.get("body")
                or data.get("text") or "The agent needs your attention.")
        spawn_worker({"mode": "speak", "text": text, "voice": VOICE})
    elif event in STOP_EVENTS:
        if STOP_MODE == "off":
            return
        # Copilot reports why the turn ended; don't narrate aborted turns.
        reason = data.get("stopReason")
        if reason and reason not in ("end_turn", "stop", "completed"):
            return
        if STOP_MODE == "chime":
            spawn_worker({"mode": "speak", "text": CHIME, "voice": VOICE})
            return
        # All transcript reading happens in the worker: it can wait out the
        # race where the stop event fires before the final message is
        # flushed to disk.
        path = data.get("transcript_path") or data.get("transcriptPath")
        source = "copilot" if event == "agentStop" else "claude"
        spawn_worker({"mode": "stop", "transcript_path": path, "voice": VOICE,
                      "source": source})


def main(argv):
    if "--worker" in argv:
        run_worker()
        return
    forced_event = None
    if "--event" in argv:
        i = argv.index("--event")
        if i + 1 < len(argv):
            forced_event = argv[i + 1]
    # Codex appends its JSON payload as the final argv argument.
    argv_payload = argv[-1] if argv and argv[-1].startswith("{") else None
    run_hook(forced_event, argv_payload)


if __name__ == "__main__":
    main(sys.argv[1:])
