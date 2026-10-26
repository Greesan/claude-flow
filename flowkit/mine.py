"""Find flows worth automating in Claude Code session transcripts (ported from parse_sessions.py).

Reads session .jsonl files (and <session>/subagents/*.jsonl) under ~/.claude/projects, turns every Bash
call into a scrubbed, named step with a pass/fail status, and finds:
  struggles  a step failed, then needed fixes (or corrections from you) before it worked
  sequences  runs of steps that repeat within or across sessions
  loops      the same step back-to-back
These become *candidates* (de-duplicated, scored, marked `covered` when an existing flow already runs those
steps). `flow draft <id>` hands Claude the evidence to write the flow. Parsing is incremental (a file is
re-read only when it changed) and nothing here calls a model or leaves this machine.
"""
import hashlib
import json
import os
import re
import time
from collections import Counter
from pathlib import Path

from .store import _atomic_write, list_flows
from .transcripts import content_blocks, result_text
from .trace import bash_paths_re, cmd_keys, is_diagnostic, is_flow_call, norm_cmd, scrub, squash

MINE = Path.home() / ".claude" / "flows" / ".mine"
PROJECTS = Path.home() / ".claude" / "projects"

# ------------------------------------------------------------ classification

INJECTED_PREFIXES = ("<command-name>", "<command-message>", "<local-command", "<system-reminder>",
                     "<user-prompt-submit-hook>", "<bash-input>", "<bash-stdout>", "Caveat:", "<task-notification>",
                     "<agent-message")
FILLER = r"(?:(?:k|ok|okay|hmm|well|yeah|yes|um)[,.\s]+)?"
STRONG_RE = re.compile(
    r"^\s*" + FILLER + r"(?:"
    r"no(?=\s*[,.!…]|\s*$|\s+(?:i|we|you|it|its|it's|that|that's|thats|this|the|so|but|just|use|please|not|my|a|an)\b)"
    r"|nope|nah|stop|wait|wrong|actually|undo|revert|that'?s not|not what|don'?t|why did you"
    r"|you (?:should|shouldn'?t|forgot|missed)|i (?:said|meant|asked)|i don'?t want"
    r")(?!\w)", re.I)
WEAK_RE = re.compile(r"\b(no|nope|don'?t|do not|stop|wrong|incorrect|actually|instead|not what|that'?s not|undo|revert|"
                     r"why did you|you (should|shouldn'?t|forgot|missed)|i (said|meant|asked))\b", re.I)


def classify(text):
    t = text.strip()
    if t.startswith("[Request interrupted"):
        return "interrupt"
    if t.startswith(INJECTED_PREFIXES):
        return "injected"
    return "human"


def hints(text):
    """(strong, weak) keyword hints for 'this message corrects the agent'."""
    strong = bool(STRONG_RE.search(text[:120]))
    return strong, (not strong) and bool(WEAK_RE.search(text[:300]))


# ----------------------------------------------------------------- parsing

def parse_file(path, is_sub, parent):
    s = {"session": path.stem, "parent": parent, "subagent": is_sub, "file": str(path),
         "project": (path.parents[2] if is_sub else path.parent).name, "title": None,
         "first_ts": None, "last_ts": None, "human": [], "failures": [], "commands": [], "flow_calls": [],
         "tools": Counter(), "files_read": Counter(), "files_edited": Counter(), "bash_paths": Counter(),
         "skills": Counter(), "kinds": Counter()}
    pending, cmds_by_tid, last_asst = {}, {}, ""
    with path.open(errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            t, ts = rec.get("type"), rec.get("timestamp")
            if isinstance(ts, str):
                s["first_ts"] = min(x for x in (s["first_ts"], ts) if x)
                s["last_ts"] = max(x for x in (s["last_ts"], ts) if x)
            if t == "ai-title":
                s["title"] = scrub(rec.get("aiTitle") or rec.get("title") or rec.get("summary") or s["title"])
            elif t == "assistant":
                for b in content_blocks(rec):
                    if b.get("type") == "text" and b.get("text"):
                        last_asst = b["text"]
                    elif b.get("type") == "tool_use":
                        name, inp, tid = b.get("name"), b.get("input") or {}, b.get("id")
                        s["tools"][name] += 1
                        pending[tid] = (name, inp)
                        if name == "Bash" and is_flow_call(inp.get("command")):
                            m = re.search(r"\bflow(?:\.py)?\s+(\w+)\s+([\w-]+)", inp["command"])
                            fc = {"ts": ts, "verb": m.group(1) if m else "?", "flow": m.group(2) if m else "?",
                                  "subagent": is_sub, "status": None, "tid": tid}
                            s["flow_calls"].append(fc)
                            cmds_by_tid[tid] = fc  # gets the run's status from the tool result below
                        elif name == "Bash":
                            raw = inp.get("command") or ""
                            keys, piped = cmd_keys(raw)
                            entry = {"i": len(s["commands"]), "ts": ts, "cwd": re.sub(r"/home/[^/\s]+", "~", rec.get("cwd") or ""),
                                     "cmd": norm_cmd(raw), "keys": keys or ["(noise)"], "piped": piped, "status": None,
                                     "tid": tid}
                            s["commands"].append(entry)
                            cmds_by_tid[tid] = entry
                            for p in bash_paths_re(scrub(raw)):
                                s["bash_paths"][p] += 1
                        elif name == "Read":
                            s["files_read"][inp.get("file_path")] += 1
                        elif name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                            s["files_edited"][inp.get("file_path") or inp.get("notebook_path")] += 1
                        elif name == "Skill":
                            s["skills"][inp.get("skill")] += 1
            elif t == "user":
                for b in content_blocks(rec):
                    bt = b.get("type")
                    if bt == "text" and b.get("text") and not is_sub:
                        meta = bool(rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isVisibleInTranscriptOnly"))
                        kind = "injected" if meta else classify(b["text"])
                        s["kinds"][kind] += 1
                        if kind != "injected":
                            e = {"ts": ts, "kind": kind, "correction_hint": False, "weak_hint": False,
                                 "prev_assistant": scrub(last_asst)[-300:]}
                            if kind != "interrupt":
                                e["correction_hint"], e["weak_hint"] = hints(b["text"])
                                e["text"] = squash(b["text"])
                            s["human"].append(e)
                    elif bt == "tool_result":
                        tid, text = b.get("tool_use_id"), result_text(b)
                        bad = bool(b.get("is_error")) or bool(re.match(r"\s*Exit code [1-9]", text))
                        if tid in cmds_by_tid:
                            ent = cmds_by_tid[tid]
                            if "verb" in ent:  # a flow call: keep the flow's own status (done, needs_confirm, ...)
                                ms = re.search(r'"status":\s*"(\w+)"', text)
                                ent["status"] = ms.group(1) if ms else ("error" if bad else "?")
                            else:
                                ent["status"] = "fail" if bad else "ok"
                        if bad:
                            name, inp = pending.get(tid, (None, {}))
                            s["failures"].append({"tool": name, "command": squash(inp.get("command"), 200, 0)
                                                  if name == "Bash" else None, "error": squash(text, 150, 150)})
    for k in ("tools", "files_read", "files_edited", "bash_paths", "skills", "kinds"):
        s[k] = dict(s[k].most_common())
    s["struggles"] = find_struggles(s["commands"], s["human"])
    return s


def is_noise(c):
    return c["keys"] == ["(noise)"]


def call_label(c):
    return " + ".join(c["keys"])


def action_keys(c):
    """The steps of a command that change something (diagnostics like ls/grep/git status are not struggles)."""
    return [k for k in c["keys"] if k != "(noise)" and not is_diagnostic(k)]


def _seconds(t0, t1):
    from datetime import datetime
    try:
        return round((datetime.fromisoformat(t1.replace("Z", "+00:00")) - datetime.fromisoformat(t0.replace("Z", "+00:00")))
                     .total_seconds())
    except (AttributeError, TypeError, ValueError):
        return None


def find_struggles(cmds, human):
    """One per failing command: its first action step failed, then whatever it took to get that step to succeed.
    Commands whose steps are all diagnostics are skipped: `grep`/`ls`/`which` exiting 1 usually means "not found"."""
    out, seen = [], set()
    typed = [h for h in human if h.get("kind") == "human" and h.get("ts")]
    for i, c in enumerate(cmds):
        acts = action_keys(c)
        if c["status"] != "fail" or not acts or acts[0] in seen:
            continue
        k = acts[0]
        seen.add(k)
        j = next((j for j in range(i + 1, len(cmds)) if k in cmds[j]["keys"] and cmds[j]["status"] == "ok"), None)
        span = cmds[i:(j + 1 if j is not None else len(cmds))]
        attempts = [x for x in span if k in x["keys"]]
        between = [x for x in span if k not in x["keys"] and not is_noise(x)]
        t0, t1 = c["ts"], (cmds[j]["ts"] if j is not None else None)
        said = [h for h in typed if t0 and h["ts"] > t0 and (t1 is None or h["ts"] < t1)]
        out.append({
            "key": k, "first_fail_index": i, "resolved": j is not None, "attempts": len(attempts),
            "fails": sum(1 for x in attempts if x["status"] == "fail"),
            "calls_until_success": len(span) - 1 if j is not None else None,
            "fix_steps": list(dict.fromkeys(a for x in between for a in action_keys(x)))[:6],
            "looked_at": list(dict.fromkeys(d for x in between for d in x["keys"]
                                            if d != "(noise)" and is_diagnostic(d)))[:6],
            "typed_prompts_between": len(said),
            # only messages that open like a correction ("no", "wait", "you forgot"...): the user had to step in
            "corrections": [h["text"] for h in said if h.get("correction_hint")][:3],
            "elapsed_s": _seconds(t0, t1),
            "uncertain": any(x["piped"] for x in span), "cmd_example": c["cmd"],
            "window": [{k2: x[k2] for k2 in ("cmd", "cwd", "status")} for x in span if not is_noise(x)][:12],
        })
    return out


def collapse_with_entries(cmds):
    out = []
    for c in cmds:
        if is_noise(c):
            continue
        lbl = call_label(c)
        if not out or out[-1][0] != lbl:
            out.append((lbl, c))
    return out


def recurring_sequences(sessions, nmin=2, nmax=5, limit=40):
    """Contiguous runs of step labels that repeat (within or across sessions)."""
    counts, where, examples = Counter(), {}, {}
    for s in sessions:
        if not s["commands"]:
            continue
        seq = collapse_with_entries(s["commands"])
        labels = [x[0] for x in seq]
        for n in range(nmin, nmax + 1):
            for i in range(len(labels) - n + 1):
                g = tuple(labels[i:i + n])
                counts[g] += 1
                where.setdefault(g, set()).add(s["parent"] or s["session"])
                if len(examples.setdefault(g, [])) < 3:
                    examples[g].append({"session": s["session"][:8], "project": s["project"],
                                        "steps": [{k: e[k] for k in ("cmd", "cwd", "status")} for _, e in seq[i:i + n]]})
    rows = [(g, c, len(where[g])) for g, c in counts.items() if c >= 2
            and any(not is_diagnostic(k) for lbl in g for k in lbl.split(" + "))]

    def inside(small, big):
        return len(big) > len(small) and any(big[i:i + len(small)] == small for i in range(len(big) - len(small) + 1))
    rows = [r for r in rows if not any(inside(r[0], o[0]) and o[1] == r[1] for o in rows)]
    rows.sort(key=lambda r: (-(r[1] * len(r[0])), r[0]))
    return [{"steps": list(g), "occurrences": c, "sessions": n, "examples": examples[g]} for g, c, n in rows[:limit]]


def loops(sessions, min_run=3):
    """Same step repeated back-to-back, e.g. an edit-test-deploy loop."""
    out = []
    for s in sessions:
        labels = [call_label(c) for c in s["commands"] if not is_noise(c)]
        i = 0
        while i < len(labels):
            j = i
            while j + 1 < len(labels) and labels[j + 1] == labels[i]:
                j += 1
            if j - i + 1 >= min_run and any(not is_diagnostic(k) for k in labels[i].split(" + ")):
                out.append({"step": labels[i], "run": j - i + 1, "at": i, "session": s["session"][:8], "project": s["project"]})
            i = j + 1
    return sorted(out, key=lambda r: -r["run"])


# --------------------------------------------------------------- candidates

def _sig(*parts):
    return hashlib.sha1(json.dumps(parts).encode()).hexdigest()[:8]


def flow_keys():
    """{flow name: set of step names its run nodes execute}, for `covered` checks."""
    out = {}
    for s in list_flows():
        g = s.load()
        keys = set()
        for n in g["nodes"].values():
            if n.get("cmd"):
                keys |= set(cmd_keys(re.sub(r"\{\{[^}]*\}\}", "x", n["cmd"]))[0])
        out[s.name] = keys
    return out


def candidates(sessions):
    fk = flow_keys()

    def overlap(keys):
        """[flow, share of these steps that flow already runs] for the best-matching flow, or None."""
        keys = {k for k in keys if k}
        best = max(((len(keys & v) / len(keys), name) for name, v in fk.items()), default=(0, None)) if keys else (0, None)
        return [best[1], round(best[0], 2)] if best[0] > 0 else None

    def covered(keys):
        o = overlap(keys)
        return o[0] if o and o[1] >= 0.6 else None

    out, groups = [], {}
    for s in sessions:
        for g in s["struggles"]:
            groups.setdefault((g["key"], tuple(g["fix_steps"][:4])), []).append((s, g))
    for (key, fix), items in groups.items():
        gs = [g for _, g in items]
        n_sess = len({s["parent"] or s["session"] for s, _ in items})
        score = sum(g["attempts"] + 2 * g["typed_prompts_between"] + 3 * len(g["corrections"]) + (3 if not g["resolved"] else 0)
                    for g in gs) * n_sess
        out.append({"id": _sig("struggle", key, fix), "kind": "struggle", "title": f"{key} fails, then {', '.join(fix) or '?'}",
                    "steps": [key, *fix], "score": score, "occurrences": len(gs),
                    "sessions": n_sess, "projects": sorted({s["project"] for s, _ in items}),
                    "corrected": sum(len(g["corrections"]) for g in gs),
                    "calls_lost": sum(g["calls_until_success"] or 0 for g in gs if g["resolved"]),
                    "time_lost_s": sum(g.get("elapsed_s") or 0 for g in gs if g["resolved"]),
                    "resolved": sum(g["resolved"] for g in gs), "covered_by": covered({key, *fix}),
                    "overlap": overlap({key, *fix}),
                    "evidence": [{"session": s["session"][:8], "project": s["project"], "attempts": g["attempts"],
                                  "resolved": g["resolved"], "corrections": g["corrections"], "looked_at": g["looked_at"],
                                  "window": g["window"]}
                                 for s, g in items[:3]]})
    for r in recurring_sequences(sessions):
        out.append({"id": _sig("sequence", r["steps"]), "kind": "sequence", "title": " -> ".join(r["steps"]),
                    "steps": r["steps"], "score": r["occurrences"] * len(r["steps"]) * r["sessions"],
                    "occurrences": r["occurrences"], "sessions": r["sessions"],
                    "projects": sorted({e["project"] for e in r["examples"]}),
                    "covered_by": covered(set(k for st in r["steps"] for k in st.split(" + "))),
                    "overlap": overlap(set(k for st in r["steps"] for k in st.split(" + "))), "evidence": r["examples"]})
    for r in loops(sessions)[:20]:
        out.append({"id": _sig("loop", r["step"], r["session"], r["at"]), "kind": "loop", "title": f"{r['step']} x{r['run']}",
                    "steps": [r["step"]], "score": r["run"], "occurrences": r["run"], "sessions": 1,
                    "projects": [r["project"]], "covered_by": covered(set(r["step"].split(" + "))),
                    "overlap": overlap(set(r["step"].split(" + "))), "evidence": [r]})
    return sorted(out, key=lambda c: -c["score"])


# ------------------------------------------------------------- store + api

def _load(name, default):
    p = MINE / name
    return json.loads(p.read_text()) if p.exists() else default


def dedupe(sessions):
    """Count each tool call once. If Claude Code ever copies history into a new transcript (e.g. on resume), the copied
    calls keep their ids; only the earliest file keeps them, so one session can't look like two."""
    seen = set()
    for s in sorted(sessions, key=lambda s: s.get("first_ts") or ""):
        for key in ("commands", "flow_calls"):
            fresh = [c for c in s.get(key, []) if not c.get("tid") or c["tid"] not in seen]
            seen |= {c["tid"] for c in s.get(key, []) if c.get("tid")}
            if len(fresh) != len(s.get(key, [])):
                s[key] = fresh
                if key == "commands":
                    s["struggles"] = find_struggles(fresh, s["human"])
    return sessions


def mine(paths=None):
    """Parse new/changed transcripts (incremental) and rebuild candidates. Returns a short summary."""
    MINE.mkdir(parents=True, exist_ok=True)
    cache_dir = MINE / "sessions"
    cache_dir.mkdir(exist_ok=True)
    index = _load("index.json", {})
    cfg = _load("config.json", None)
    if cfg is None:  # by default, skip the sessions where flow itself is developed: they are mostly tests
        cfg = {"exclude_projects": [re.sub(r"[^A-Za-z0-9]", "-", str(Path(__file__).resolve().parents[1]))]}
        _atomic_write(MINE / "config.json", json.dumps(cfg, indent=1))
    skip = set(cfg.get("exclude_projects") or [])
    files = [Path(p) for p in paths] if paths else sorted(PROJECTS.rglob("*.jsonl"))
    files = [f for f in files if not skip & set(f.parts)]
    parsed = 0
    for f in files:
        if not f.exists():
            continue
        st = f.stat()
        stamp = [st.st_size, int(st.st_mtime)]
        if index.get(str(f), {}).get("stamp") == stamp:
            continue
        is_sub = "subagents" in f.parts
        s = parse_file(f, is_sub, f.parent.parent.name if is_sub else None)
        key = hashlib.sha1(str(f).encode()).hexdigest()[:16]
        _atomic_write(cache_dir / f"{key}.json", json.dumps(s))
        index[str(f)] = {"stamp": stamp, "cache": key}
        parsed += 1
    _atomic_write(MINE / "index.json", json.dumps(index))
    sessions = dedupe([json.loads((cache_dir / f"{v['cache']}.json").read_text()) for k, v in index.items()
                       if (cache_dir / f"{v['cache']}.json").exists() and not skip & set(Path(k).parts)])
    status = _load("status.json", {})
    cands = candidates(sessions)
    for c in cands:
        c["status"] = status.get(c["id"], {}).get("status", "new")
    _atomic_write(MINE / "candidates.json", json.dumps({"ts": time.time(), "sessions": len(sessions), "candidates": cands}))
    fresh = [c for c in cands if c["status"] == "new" and not c["covered_by"]]
    return {"status": "ok", "parsed": parsed, "sessions": len(sessions), "candidates": len(cands),
            "new_uncovered": len(fresh), "top": [{k: c[k] for k in ("id", "kind", "title", "score")} for c in fresh[:5]]}


def list_candidates(show_all=False, limit=15):
    data = _load("candidates.json", {"candidates": []})
    cs = data["candidates"] if show_all else [c for c in data["candidates"] if c["status"] in ("new", "drafting")
                                              and not c["covered_by"]]
    return [{k: c[k] for k in ("id", "kind", "title", "score", "occurrences", "sessions", "projects", "covered_by", "status")}
            for c in cs[:limit]]


def get_candidate(cid):
    for c in _load("candidates.json", {"candidates": []})["candidates"]:
        if c["id"] == cid:
            return c
    return None


def set_status(cid, status, note=""):
    st = _load("status.json", {})
    st[cid] = {"status": status, "note": note, "ts": time.time()}
    MINE.mkdir(parents=True, exist_ok=True)
    _atomic_write(MINE / "status.json", json.dumps(st, indent=1))


def draft_brief(c, name):
    """What Claude needs to write the flow: evidence, a skeleton, and the rules. Writes nothing."""
    skeleton = {"about": f"<one line: what this automates> (mined: {c['title']})", "inputs": {},
                "start": "step1", "nodes": {}}
    steps = c["steps"]
    for i, st in enumerate(steps, 1):
        nid = f"step{i}"
        skeleton["nodes"][nid] = {"type": "run", "cmd": f"<{st}: use the evidence, replace literals with inputs>",
                                  "check": "<command that exits 0 when this step's effect is in place>",
                                  "ok": f"step{i + 1}" if i < len(steps) else "done"}
    skeleton["nodes"]["done"] = {"type": "end", "say": "<what to report>"}
    return {"status": "draft", "candidate": {k: c[k] for k in ("id", "kind", "title", "steps", "occurrences", "sessions",
                                                                  "projects", "covered_by")},
            "evidence": c["evidence"], "skeleton": skeleton,
            "rules": ["Model the intent, not one transcript: the evidence shows several attempts; keep the path that worked.",
                      "Failed steps in the evidence become recovery paths (with a `match` regex from the error) or "
                      "decide nodes whose `check` is the diagnostic that told them apart.",
                      "Corrections from the user in the evidence are rules: encode them (e.g. a confirm, a pre-check).",
                      "Steps that change shared things (deploy, push, restart) need effect: once behind a confirm, or "
                      "effect: idempotent when repeating them is harmless.",
                      "Use {{ inputs.x }} for anything that varies; give every state-changing step a `check`."],
            "next": [f"write the YAML, then: flow import {name} <file.yaml> -m 'mined from candidate {c['id']}'",
                     f"flow validate {name}; flow export {name}; then test it on a real case",
                     f"flow candidate done {c['id']}   (or: flow candidate dismiss {c['id']})"]}


def worth_mentioning(c):
    """Only procedures that needed the user, have a known fix, and cost real work, so a flow clearly saves time:
      - the user stepped in: a real correction while Claude was stuck
      - there is a clear fix: the failing step succeeded in the end, so the fix is in the transcript
      - it cost work: the fix took at least a fix step and a retry (2+ calls a single flow call would replace)
    Repeated sequences and loops without intervention are listed by `flow candidates` but never pushed."""
    return (c["status"] == "new" and not c["covered_by"] and c["kind"] == "struggle" and c.get("corrected", 0) > 0
            and c.get("resolved", 0) > 0 and c.get("calls_lost", 0) >= 2)


def _evidence(c):
    return {"sessions": c["sessions"], "occurrences": c["occurrences"], "corrected": c.get("corrected", 0)}


def _cost(c):
    mins = (c.get("time_lost_s") or 0) / 60
    took = f"~{mins:.0f} min" if mins >= 1 else f"{c.get('time_lost_s') or 0}s"
    return f"you stepped in {c['corrected']}x; it took {c['calls_lost']} extra steps, {took}"


def _has_new_evidence(c, shown):
    """True if never mentioned, or seen in more sessions / more often / corrected more since the last mention.
    Reopening a session adds no evidence, so nothing repeats; heavy use adds evidence, so mentions keep coming."""
    before = shown.get(c["id"])
    if not isinstance(before, dict):
        return True
    return any(v > before.get(k, 0) for k, v in _evidence(c).items())


def nudge():
    """One line for a SessionStart hook: only when there is something new worth automating."""
    data = _load("candidates.json", {"candidates": []})
    shown = _load("nudged.json", {})
    cs = [c for c in data["candidates"] if worth_mentioning(c) and _has_new_evidence(c, shown)]
    if not cs:
        return ""
    proj = os.environ.get("CLAUDE_PROJECT_DIR", "")
    here = [c for c in cs if any(proj.replace("/", "-").endswith(p.lstrip("-")) or p.endswith(proj.replace("/", "-"))
                                 for p in c["projects"])] if proj else []
    pick = sorted(here or cs, key=lambda c: -(c.get("time_lost_s") or 0))
    shown.update({c["id"]: _evidence(c) for c in pick})
    MINE.mkdir(parents=True, exist_ok=True)
    _atomic_write(MINE / "nudged.json", json.dumps(shown))
    listed = "; ".join(f"{c['id']} {c['title'][:60]} ({_cost(c)})" for c in pick)
    return ("flow miner: these procedures needed the user to step in and have a clear fix, but no flow yet: " + listed
            + ". When there is a natural pause, turn them into flows yourself (`flow draft <id> --as <name>`, write it, "
            "test it), then tell the user what you saved.")


def usage(flow=None):
    """How flows are actually used, from transcripts: runs by main agent vs subagents, and how they ended."""
    out = {}
    cached = [json.loads(f.read_text()) for f in (MINE / "sessions").glob("*.json")] if (MINE / "sessions").is_dir() else []
    for s in dedupe(cached):
        for fc in s.get("flow_calls", []):
            if flow and fc["flow"] != flow:
                continue
            u = out.setdefault(fc["flow"], {"calls": 0, "by_main": 0, "by_subagent": 0, "verbs": Counter(),
                                            "statuses": Counter(), "sessions": set()})
            u["calls"] += 1
            u["by_subagent" if fc["subagent"] else "by_main"] += 1
            u["verbs"][fc["verb"]] += 1
            u["statuses"][fc["status"]] += 1
            u["sessions"].add(s["parent"] or s["session"])
    return {"status": "ok", "usage": {k: {**v, "verbs": dict(v["verbs"]), "statuses": dict(v["statuses"]),
                                          "sessions": len(v["sessions"])} for k, v in out.items()},
            "note": "from mined transcripts; run `flow mine` first for the latest"}
