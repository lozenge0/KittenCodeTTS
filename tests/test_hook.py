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

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
