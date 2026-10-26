"""Read what Claude did while a run was paused, straight from Claude Code's session transcripts.

The pause point is the flow's own tool result (it prints `"run": "<id>"` with a needs_*/failed status), so no
per-tool-call hook is needed. Transcripts keep every record across compaction (compaction only appends a
`compact_boundary`), so the slice is complete even when Claude's context was compacted mid-fix.
"""
import json
import os
import re
from pathlib import Path

from .trace import cmd_keys, is_diagnostic, is_flow_call, scrub, squash

PROJECTS = Path.home() / ".claude" / "projects"
PAUSED = ("needs_claude", "failed", "needs_human", "needs_confirm")


class TranscriptError(Exception):
    pass


def content_blocks(rec):
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in (c or []) if isinstance(b, dict)]


def result_text(b):
    c = b.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def project_folder(project_dir):
    """Claude Code's folder name for a project path: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(project_dir))


def candidate_files(project_dir=None):
    """This project's transcripts (sessions and subagents) newest first, then every other project's."""
    root = PROJECTS
    if not root.is_dir():
        return []
    mine_dir = root / project_folder(project_dir or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    first = sorted(mine_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if mine_dir.is_dir() else []
    rest = sorted((p for p in root.rglob("*.jsonl") if mine_dir not in p.parents), key=lambda p: p.stat().st_mtime,
                  reverse=True)
    return first + rest


def records(path):
    out, types = [], set()
    with open(path, errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
                types.add(r.get("type"))
    if out and not types & {"user", "assistant"}:
        raise TranscriptError(f"transcript format changed? {path} has no user/assistant records "
                              f"(saw types {sorted(map(str, types))[:8]}, keys {sorted(out[0])[:10]})")
    return out


def _pause_index(recs, run_id, statuses):
    """Index of the LAST tool result in which the flow reported this run paused with one of `statuses`."""
    needle = f'"run": "{run_id}"'
    best = None
    for i, r in enumerate(recs):
        if r.get("type") != "user":
            continue
        for b in content_blocks(r):
            if b.get("type") == "tool_result":
                t = result_text(b)
                if needle in t and any(f'"status": "{s}"' in t for s in statuses):
                    best = i
    return best


def find_run(run_id, project_dir=None, statuses=PAUSED, hint=None):
    """(transcript path, record index of the pause) or (None, None). `hint` = a transcript to try first."""
    files = ([Path(hint)] if hint else []) + candidate_files(project_dir)
    seen = set()
    for f in files:
        if f in seen or not f.exists():
            continue
        seen.add(f)
        try:
            if run_id not in f.read_text(errors="replace"):
                continue
            recs = records(f)
        except OSError:
            continue
        i = _pause_index(recs, run_id, statuses)
        if i is not None:
            return f, i
    return None, None


def _entry(name, inp, text, bad, cwd):
    e = {"tool": name, "ok": not bad, "cwd": re.sub(r"/home/[^/\s]+", "~", cwd or ""), "out": squash(text, 300, 300)}
    if name == "Bash":
        c = inp.get("command", "")
        e.update(cmd=scrub(c), keys=cmd_keys(c)[0],
                 kind="flow" if is_flow_call(c) else "diagnostic" if is_diagnostic(c) else "action")
    elif name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        e.update(kind="edit", path=inp.get("file_path") or inp.get("notebook_path"))
    elif name in ("Read", "Grep", "Glob", "WebFetch", "WebSearch", "LS"):
        e.update(kind="diagnostic", path=inp.get("file_path") or inp.get("path") or inp.get("url"),
                 pattern=scrub(inp.get("pattern") or inp.get("query")))
    else:
        e.update(kind="other", input=squash(json.dumps(inp, default=str), 200, 0))
    return e


def slice_after(path, start, stop_at_learn=True):
    """Tool calls after record `start`, each matched with its result. Flow calls are left out; the slice ends
    at the first `flow learn` call (that is Claude saying "I'm done fixing")."""
    recs = records(path)
    pending, out = {}, []
    for r in recs[start + 1:]:
        t = r.get("type")
        if t == "assistant":
            for b in content_blocks(r):
                if b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    if stop_at_learn and b.get("name") == "Bash" and re.search(r"\bflow(\.py)?\s+learn\b", inp.get("command", "")):
                        return _numbered(out)
                    pending[b.get("id")] = (b.get("name"), inp, r.get("cwd"), r.get("timestamp"))
        elif t == "user":
            for b in content_blocks(r):
                if b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                    name, inp, cwd, ts = pending.pop(b["tool_use_id"])
                    text = result_text(b)
                    bad = bool(b.get("is_error")) or bool(re.match(r"\s*Exit code [1-9]", text))
                    e = _entry(name, inp, text, bad, cwd)
                    if e["kind"] != "flow":
                        out.append({**e, "ts": ts})
                    m = re.search(r"agentId:\s*([A-Za-z0-9]+)", text) if name in ("Agent", "Task") else None
                    sub = Path(path).with_suffix("") / "subagents" / f"agent-{m.group(1)}.jsonl" if m else None
                    if sub and sub.exists():  # what the subagent did counts as evidence too
                        out += [{**x, "via": m.group(1)} for x in slice_after(sub, -1, stop_at_learn=False)]
        # system records (compact_boundary, turn_duration, ...) and summaries carry no tool calls: skipped
    return _numbered(out)


def _numbered(out):
    for i, e in enumerate(out):
        e["i"] = i
    return out


def index_before_time(path, epoch):
    """Index of the last record at or before `epoch` (seconds), so user_messages_after() returns what came later."""
    from datetime import datetime
    last = -1
    for i, r in enumerate(records(path)):
        ts = r.get("timestamp")
        if isinstance(ts, str):
            try:
                if datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() > epoch:
                    break
            except ValueError:
                continue
        last = i
    return last


def user_messages_after(path, start):
    """[(kind, text)] after record `start`: typed messages ('typed') and AskUserQuestion answers ('answer')."""
    recs = records(path)
    asks, out = set(), []
    for r in recs[start + 1:]:
        if r.get("type") == "assistant":
            for b in content_blocks(r):
                if b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                    asks.add(b.get("id"))
        elif r.get("type") == "user":
            meta = r.get("isMeta") or r.get("isCompactSummary") or r.get("isVisibleInTranscriptOnly")
            for b in content_blocks(r):
                if b.get("type") == "text" and not meta:
                    txt = b.get("text") or ""
                    if not txt.lstrip().startswith(("<", "[Request interrupted", "Caveat:")):
                        out.append(("typed", txt))
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in asks:
                    out.append(("answer", result_text(b)))
    return out
