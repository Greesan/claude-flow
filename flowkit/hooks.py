"""Claude Code hook entry points (`flow hook <name>`). All local, no model calls, bounded in time.

session-start  print what Claude should know in this project: saved flows, runs in progress, long-run advice,
               and (on startup) one line about new automation candidates. Also runs after /compact.
pre-compact    mine this transcript so far; snapshot the trace of any paused run worked on in this session.
session-end    mine the transcript that just ended.
approve-guard  PreToolUse(Bash): block `flow resume ... --approve` unless the user approved after the confirm.

Every hook except approve-guard exits 0 even when something breaks (the error goes to ~/.claude/flows/.hooks.log).
approve-guard fails closed: if it cannot prove an approval it blocks, and the user can still run the resume
themselves with `! flow resume ... --approve` (a `!` command is not a tool call, so no hook sees it).
"""
import json
import os
import re
import shlex
import signal
import sys
import time
import traceback
from pathlib import Path

from . import charts, learn, mine, transcripts
from .store import list_flows, project_dir
from .trace import split_steps, strip_heredocs

LOG = Path.home() / ".claude" / "flows" / ".hooks.log"
MAX_LINES = 12
# A flow is "slow" when its measured p90 exceeds half of the Bash tool's 120 s default timeout, so one slower-than-usual
# run still fits; those flows should be started with run_in_background.
SLOW_AFTER = 60
APPROVE = re.compile(r"^\s*(y|yes|yep|yeah|yup|ok|okay|k|sure|approve[sd]?|go( ahead)?|proceed|do it|lgtm|ship it|"
                     r"confirm(ed)?)\b(?!\s*(,\s*)?(but|wait|not|no)\b)", re.I)


def _payload():
    try:
        return json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return {}


def _log(name, err):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%F %T')} {name}: {err}\n")
    except OSError:
        pass


def _next_step(name, r):
    run = f"--run {r['run']}"
    return {"needs_claude": f"after fixing it: `flow learn {name} {run} --propose`; or `flow resume {name} {run} --choose <option>`",
            "failed": f"after fixing it: `flow learn {name} {run} --propose`; or `flow resume {name} {run} --done` to retry",
            "needs_confirm": f"ask the user; after they approve: `flow resume {name} {run} --approve`",
            "needs_human": f"the user acts, then `flow resume {name} {run} --done`"}.get(
        r.get("wait"), f"`flow status {name} {run} --wait 540`")


def session_start(p):
    flows, lines = list_flows(), []
    if flows:
        lines.append("[flow] Saved flows for this project. When a task matches one, run it (`flow run <name> -i key=value`) "
                     "instead of doing the steps yourself; you don't need to ask first, risky steps stop for approval:")
        slow = []
        for s in flows[:6]:
            g, tm = s.load(), s.timing()
            facts = ([f"inputs: {', '.join(g['inputs'])}"] if g.get("inputs") else []) + \
                ([f"avg {tm['mean']:.0f}s over {tm['runs']} runs"] if tm and tm["runs"] else [])
            if tm and tm["runs"] and tm["p90"] > SLOW_AFTER:
                slow.append(s.name)
            lines.append(f"  - {s.name}{' (' + '; '.join(facts) + ')' if facts else ''}: {g.get('about', '')[:100]}")
    live = [(s, r) for s in flows for r in s.runs() if r.get("status") in ("paused", "running")]
    if live:
        lines.append("[flow] Runs in progress:")
        for s, r in live[-3:]:
            lines.append(f"  - {s.name} run {r['run']} {r.get('wait') or r['status']} at `{r['current']}`: {_next_step(s.name, r)}")
    if flows and slow:
        lines.append(f"[flow] Slow (p90 > {SLOW_AFTER}s here): {', '.join(slow)}. Start these with Bash run_in_background "
                     f"so the 2-minute tool timeout cannot cut a step off.")
    if flows:
        lines.append("[flow] Subagents don't see this list: when delegating, tell them which flow to run.")
        if any(s.scope() == "project" for s in flows):
            charts.register_project(project_dir())
        charts.refresh()
        page = charts.status()
        if page["stale"] and page["artifact"]:
            lines.append(f"[flow] The flow charts page changed since it was last published: republish {page['path']} to "
                         f"{page['artifact']} (Artifact tool, url=...), then `flow charts --published {page['artifact']}`.")
    n, n_cands = mine.nudge() if p.get("source", "startup") == "startup" else ("", 0)
    if n:
        lines.append(f"[{n}")
    if not lines:
        return 0
    # Claude gets the full text as context; the user gets one visible line saying it happened.
    seen = [f"{len(flows)} saved flow{'s' * (len(flows) != 1)}"] if flows else []
    seen += [f"{len(live)} run{'s' * (len(live) != 1)} in progress"] if live else []
    seen += [f"{n_cands} procedure{'s' * (n_cands != 1)} worth saving"] if n else []
    print(json.dumps({"systemMessage": "flow: " + " · ".join(seen),
                      "hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": "\n".join(lines[:MAX_LINES])}}))
    return 0


def pre_compact(p):
    tp = p.get("transcript_path")
    if tp:
        mine.mine([tp])
        text = Path(tp).read_text(errors="replace") if Path(tp).exists() else ""
        for s in list_flows():
            for r in s.runs():
                if r.get("status") == "paused" and r["run"] in text:
                    learn.build_trace(s, r, hint=tp)
    return 0


def session_end(_p):
    # Sweep every transcript, not just this one: re-reading is skipped for files whose size and time are unchanged,
    # so this is cheap, and it also picks up sessions that ended without this hook (a crash or a killed terminal).
    mine.mine()
    return 0


def approve_call(cmd):
    """(flow, run_id or None) when the command really RUNS `flow resume <flow> ... --approve`, else None.
    Tokenised, so the words inside a quoted string or a heredoc (e.g. a test or a commit message) do not count."""
    assigned = {}  # `SB=/x && CLAUDE_PROJECT_DIR=$SB flow resume ...`: expand what this command itself assigned
    for seg, _ in split_steps(strip_heredocs(cmd or "")):
        try:
            toks = shlex.split(seg)
        except ValueError:
            continue
        toks = [re.sub(r"\$\{?([A-Za-z_]\w*)\}?", lambda m: assigned.get(m.group(1), os.environ.get(m.group(1), m.group(0))), t)
                for t in toks]
        if len(toks) == 1 and re.fullmatch(r"[A-Za-z_]\w*=.*", toks[0]):
            k, v = toks[0].split("=", 1)
            assigned[k] = v
            continue
        for i, tok in enumerate(toks[:-1]):
            if tok.rsplit("/", 1)[-1] in ("flow", "flow.py") and toks[i + 1] == "resume" and "--approve" in toks:
                rest = toks[i + 2:]
                name = next((x for x in rest if not x.startswith("-")), None)
                run_id = rest[rest.index("--run") + 1] if "--run" in rest[:-1] else None
                env = dict(t.split("=", 1) for t in toks[:i] if re.fullmatch(r"[A-Za-z_]\w*=.*", t))
                return name, run_id, env.get("CLAUDE_PROJECT_DIR")
    return None


def approve_guard(p):
    hit = approve_call((p.get("tool_input") or {}).get("command", "")) if p.get("tool_name") == "Bash" else None
    if not hit:
        return 0
    name, run_id, project = hit
    if project:  # `CLAUDE_PROJECT_DIR=... flow resume ...`: the run lives in that project, not the session's
        os.environ["CLAUDE_PROJECT_DIR"] = project
    say, st = "", None
    if name:
        from .store import FlowStore
        st = FlowStore(name).load_run(run_id)
        if st:
            run_id, say = st["run"], (st.get("result") or {}).get("say", "")
    if not run_id:
        return _block("no paused run found to approve")
    path, idx = transcripts.find_run(run_id, statuses=("needs_confirm",), hint=p.get("transcript_path"))
    if path is None and st and st.get("paused_at") and p.get("transcript_path"):
        # the flow's JSON may not be in the transcript verbatim (e.g. piped through a formatter): use the pause time
        path = Path(p["transcript_path"])
        idx = transcripts.index_before_time(path, st["paused_at"])
    if path is None:
        return _block(f"could not find when run {run_id} asked for confirmation")
    msgs = transcripts.user_messages_after(path, idx)
    latest = None
    for kind, text in msgs:  # the most recent thing the user said or answered decides
        if kind == "typed":
            latest = text
        else:
            answers = re.findall(r'"=\s*"([^"]*)"', text)
            latest = answers[-1] if answers else text
    if latest is not None and APPROVE.search(latest.strip()):
        return 0
    return _block("the user has not approved this step since the flow asked" + (f" ({say[:160]})" if say else ""))


def _block(why):
    print(f"flow approve-guard: {why}. Show the user what the step will do and ask; resume with --approve only "
          f"after they say yes (or they can run `! flow resume ... --approve` themselves).", file=sys.stderr)
    return 2


# PreToolUse(Bash) fast path: plain bash, so only commands containing `--approve` start Python.
APPROVE_GUARD = 'input=$(cat); case "$input" in *--approve*) printf "%s" "$input" | flow hook approve-guard ;; esac'


def config():
    """The settings.json entries flow's hooks need (merge them in; nothing is installed by printing this)."""
    def cmd(c):
        return [{"hooks": [{"type": "command", "command": c, "timeout": 30}]}]
    return {"cleanupPeriodDays": 180, "hooks": {
        "SessionStart": cmd("flow hook session-start"),
        "PreCompact": cmd("flow hook pre-compact"),
        "SessionEnd": cmd("flow hook session-end"),
        "PreToolUse": [{"matcher": "Bash", "hooks": cmd(APPROVE_GUARD)[0]["hooks"]}]},
        "note": "needs `flow` on PATH (uv tool install .); cleanupPeriodDays keeps transcripts long enough to learn from"}


HOOKS = {"session-start": session_start, "pre-compact": pre_compact, "session-end": session_end,
         "approve-guard": approve_guard}


def run(name):
    """Run one hook; returns the process exit code."""
    fn = HOOKS.get(name)
    if fn is None:
        print(f"unknown hook {name}; hooks: {sorted(HOOKS)}", file=sys.stderr)
        return 0
    p = _payload()

    def _timeout(*_):
        raise TimeoutError("hook time limit")

    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(20)
    try:
        return fn(p)
    except Exception as e:  # never break a session because of flow
        _log(name, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
        return _block(f"internal error ({type(e).__name__})") if name == "approve-guard" else 0
    finally:
        signal.alarm(0)
