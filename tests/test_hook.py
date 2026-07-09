"""Unit tests for kitten_voice.py — stdlib only, no audio, no TTS deps.

Run:  python3 tests/test_hook.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "kv", os.path.join(ROOT, "kitten_voice.py"))
kv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kv)

TMP = tempfile.mkdtemp(prefix="kitten_test_")
fails = []


def check(name, got, want):
    ok = got == want
    print(("PASS" if ok else "FAIL"), name, "" if ok else f"-> got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)


def write_jsonl(path, entries):
    with open(path, "w") as f:
        for e in entries:
            f.write((e if isinstance(e, str) else json.dumps(e)) + "\n")


def asst(mid, blocks, sidechain=False):
    e = {"type": "assistant", "message": {"id": mid, "content": blocks}}
    if sidechain:
        e["isSidechain"] = True
    return e


def cop(text, tools=()):
    return {"type": "assistant.message",
            "data": {"messageId": "x", "content": text, "toolRequests": list(tools)}}


# ---- Claude Code transcript format ------------------------------------------

p = os.path.join(TMP, "t1.jsonl")
write_jsonl(p, [
    asst("m1", [{"type": "text", "text": "Let me check the settings file."}]),
    asst("m1", [{"type": "tool_use", "id": "t", "name": "Read", "input": {}}]),
    {"type": "user", "message": {"content": "tool result"}},
    asst("m2", [{"type": "thinking", "thinking": "hmm"}]),
    asst("m2", [{"type": "text", "text": "All done."}]),
    asst("m2", [{"type": "text", "text": "The voice now works."}]),
    {"type": "system"},
])
check("claude-split-final-joined", kv.final_message_text(p),
      "All done.\nThe voice now works.")

p = os.path.join(TMP, "t2.jsonl")
write_jsonl(p, [
    asst("m1", [{"type": "text", "text": "Stale mid-turn narration."}]),
    asst("m2", [{"type": "tool_use", "id": "t", "name": "AskUserQuestion", "input": {}}]),
])
check("claude-tool-only-final-empty", kv.final_message_text(p), "")

p = os.path.join(TMP, "t3.jsonl")
write_jsonl(p, [
    asst("m1", [{"type": "text", "text": "Main agent final answer."}]),
    asst("sub1", [{"type": "text", "text": "subagent internals"}], sidechain=True),
])
check("claude-sidechain-skipped", kv.final_message_text(p), "Main agent final answer.")

p = os.path.join(TMP, "t4.jsonl")
write_jsonl(p, [
    asst("m1", [{"type": "text", "text": "Complete message."}]),
    '{"type":"assistant","message":{"id":"m2","content":[{"type":"te',
])
check("claude-half-written-ignored", kv.final_message_text(p), "Complete message.")

check("missing-file-none", kv.final_message_text(os.path.join(TMP, "nope.jsonl")), None)

# ---- Copilot CLI events.jsonl format -----------------------------------------

p = os.path.join(TMP, "c1.jsonl")
write_jsonl(p, [
    {"type": "session.start", "data": {}},
    {"type": "user.message", "data": {"content": "do the thing"}},
    {"type": "assistant.turn_start", "data": {"turnId": "0"}},
    cop("Working on it.", tools=[{"name": "bash"}]),
    cop("Finished: the thing is done and tests pass."),
    {"type": "assistant.turn_end", "data": {"turnId": "0"}},
])
check("copilot-last-message", kv.final_message_text(p),
      "Finished: the thing is done and tests pass.")

p = os.path.join(TMP, "c2.jsonl")
write_jsonl(p, [cop("Stale."), cop("", tools=[{"name": "bash"}])])
check("copilot-empty-final-empty", kv.final_message_text(p), "")

# unrecognized format -> stop_text chimes rather than staying silent
p = os.path.join(TMP, "c3.jsonl")
write_jsonl(p, [{"type": "mystery.event", "data": {"content": "???"}}])
check("unknown-format-chimes", kv.stop_text(p), kv.CHIME)

# ---- shared stop_text behavior ------------------------------------------------

# the flush race: the final message lands AFTER the stop event fires
p = os.path.join(TMP, "t6.jsonl")
write_jsonl(p, [asst("m1", [{"type": "text", "text": "Stale narration."}])])

def late_append():
    time.sleep(0.8)
    with open(p, "a") as f:
        f.write(json.dumps(asst("m2", [{"type": "text", "text": "Real final message."}])) + "\n")

t = threading.Thread(target=late_append)
t.start()
check("race-late-flush-caught", kv.stop_text(p), "Real final message.")
t.join()

p = os.path.join(TMP, "t7.jsonl")
write_jsonl(p, [asst("m2", [{"type": "tool_use", "id": "t", "name": "X", "input": {}}])])
check("stop-text-chime", kv.stop_text(p), kv.CHIME)

p = os.path.join(TMP, "t8.jsonl")
write_jsonl(p, [asst("m1", [{"type": "text",
    "text": "It **works** now. See `settings.json`.\n\n```json\n{\"x\":1}\n```"}])])
check("short-read-all-cleaned", kv.stop_text(p), "It works now. See settings.json.")

check("stop-text-no-path-silent", kv.stop_text(None), None)

# ---- inline-message path (Codex / Gemini / OpenCode) --------------------------

check("render-empty-chimes", kv.render_stop_text(""), kv.CHIME)
check("render-short-cleaned",
      kv.render_stop_text("Renamed `foo` to **bar**. All tests pass."),
      "Renamed foo to bar. All tests pass.")

# codex payload: argv JSON with the final message inline
codex_payload = json.dumps({
    "type": "agent-turn-complete",
    "turn-id": "1",
    "input-messages": ["do the thing"],
    "last-assistant-message": "The thing is done.",
})
check("codex-payload-parses",
      json.loads(codex_payload).get("last-assistant-message"), "The thing is done.")
check("codex-argv-detected",
      codex_payload.startswith("{"), True)

# ---- native-tool summarizer invocations ---------------------------------------

cmd, stdin = kv._summarizer_invocation("claude", "/x/claude", "P", "B")
check("summarize-claude", (cmd, stdin),
      (["/x/claude", "-p", "P", "--model", kv.SUMMARY_MODEL], "B"))
cmd, stdin = kv._summarizer_invocation("copilot", "/x/copilot", "P", "B")
check("summarize-copilot", (cmd, stdin),
      (["/x/copilot", "-p", "P\n\nB", "--log-level", "none"], ""))
cmd, stdin = kv._summarizer_invocation("codex", "/x/codex", "P", "B")
check("summarize-codex", (cmd, stdin),
      (["/x/codex", "exec", "--skip-git-repo-check", "P"], "B"))
cmd, stdin = kv._summarizer_invocation("gemini", "/x/gemini", "P", "B")
check("summarize-gemini", (cmd, stdin), (["/x/gemini", "-p", "P"], "B"))
cmd, stdin = kv._summarizer_invocation("opencode", "/x/opencode", "P", "B")
check("summarize-opencode", (cmd, stdin), (["/x/opencode", "run", "P\n\nB"], ""))

# ---- local (extractive) summarizer --------------------------------------------

MD_MSG = """All three services are migrated and deployed. Here's the summary:

## What changed

- Moved the auth middleware into a shared package
- Added retry logic with exponential backoff

```python
def retry(fn):
    return backoff(fn)
```

| Service | Status |
|---|---|
| api | migrated |
| worker | migrated |

The staging benchmarks show throughput up forty percent. Some filler text
about the weather that says nothing important at all for anyone. All 214
tests pass and the linter is clean. More filler prose that just describes
scenery and adds nothing of value to the report whatsoever, honestly.
You need to delete the old cron entries once you are confident."""

out = kv.summarize_local(MD_MSG)
check("extract-nonempty", bool(out and len(out) > 20), True)
check("extract-within-budget", len(out) <= kv.MAX_CHARS, True)
check("extract-no-code", "backoff" in out or "def retry" in out, False)
check("extract-no-table", "|" in out, False)
check("extract-has-outcome", "All 214 tests pass and the linter is clean." in out, True)
check("extract-has-action", "You need to delete the old cron entries once you are confident." in out, True)
check("extract-leads-with-outcome",
      out.startswith("All three services are migrated and deployed."), True)

# faithfulness: every emitted sentence is verbatim from the source candidates
cands = set(kv._candidate_sentences(MD_MSG))
import re as _re
emitted = _re.split(r"(?<=[.!?])\s+", out)
check("extract-faithful", all(sent in cands for sent in emitted), True)

# document order is preserved
idx = [next(i for i, c in enumerate(kv._candidate_sentences(MD_MSG)) if c == sent)
       for sent in emitted]
check("extract-in-order", idx == sorted(idx), True)

# bullets become sentences
check("extract-bullets-usable",
      "Moved the auth middleware into a shared package." in cands, True)

# filler lead is not chosen over real content when it carries no signal
FILLER_MSG = ("Here's what I found after digging in. " * 1) + \
    ("Padding sentence with no purpose here. " * 12) + \
    "The deploy failed because the token expired. You should rotate it and rerun."
fout = kv.summarize_local(FILLER_MSG)
check("extract-skips-filler", fout.startswith("Here's what I found"), False)
check("extract-finds-failure", "The deploy failed because the token expired." in fout, True)

# degenerate input -> None (caller falls back to truncation)
check("extract-empty-none", kv.summarize_local("###\n| a | b |\n```x```"), None)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
