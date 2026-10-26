"""Transcript slices, subagents, compaction, lifecycle hooks, approve-guard, detach, script hashes, usage."""
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from flowkit import cli, engine, learn, hooks, mine, secrets, transcripts  # noqa: E402
from flowkit.store import FlowStore  # noqa: E402
from transcript_fixture import Transcript  # noqa: E402



@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for v in ("DECISION_MODEL_URL", "DECIDER_URL", "JEV_URL"):
        monkeypatch.delenv(v, raising=False)
    projects = tmp_path / "home" / ".claude" / "projects"
    monkeypatch.setattr(mine, "MINE", tmp_path / "home" / ".claude" / "flows" / ".mine")
    monkeypatch.setattr(mine, "PROJECTS", projects)
    monkeypatch.setattr(transcripts, "PROJECTS", projects)
    monkeypatch.setattr(hooks, "LOG", tmp_path / "hooks.log")
    secrets._cache.clear()
    return tmp_path


def make(name, g):
    for n in g["nodes"].values():
        if n.get("type", "run") == "run":
            n.setdefault("effect", "idempotent")
    s = FlowStore(name)
    assert s.apply([{"op": "replace_graph", "graph": g}], "t", "create")["status"] == "applied"
    return s


def start(s):
    g = s.load()
    return engine.run(s, s.new_run(g["version"], {}, g["start"]))


def failing(isolated):
    fixed = isolated / "fixed"
    return make("f", {"start": "a", "nodes": {
        "a": {"type": "run", "cmd": f"test -f {fixed} || {{ echo nope; exit 1; }}", "effect": "read", "ok": "done"},
        "done": {"type": "end"}}}), fixed


def confirming():
    return make("c", {"start": "ask", "nodes": {"ask": {"type": "confirm", "say": "Deploy to prod?", "next": "go"},
                                                "go": {"type": "run", "cmd": "true", "effect": "once", "ok": "done"},
                                                "done": {"type": "end"}}})


# ---------- transcript slices ----------
def test_slice_survives_compaction(isolated):
    s, fixed = failing(isolated)
    r = start(s)
    Transcript(isolated).flow("flow run f", r).bash("ls").compact().bash(f"touch {fixed}").write()
    t = learn.build_trace(s, s.load_run(r["run"]))
    assert [e.get("cmd") for e in t] == ["ls", f"touch {fixed}"]


def test_subagent_work_is_part_of_the_trace(isolated):
    s, fixed = failing(isolated)
    r = start(s)
    sub = Transcript(isolated, "ignored").bash("git remote -v").bash(f"touch {fixed}")
    Transcript(isolated).flow("flow run f", r).agent("abc123", sub).write()
    t = learn.build_trace(s, s.load_run(r["run"]))
    via = [e for e in t if e.get("via") == "abc123"]
    assert [e["cmd"] for e in via] == ["git remote -v", f"touch {fixed}"] and t[0]["tool"] == "Agent"


def test_run_paused_inside_a_subagent_is_found(isolated):
    s, fixed = failing(isolated)
    r = start(s)
    sub = Transcript(isolated, "ignored").flow("flow run f", r).bash(f"touch {fixed}")
    Transcript(isolated).agent("sub9", sub).write()
    path, _ = transcripts.find_run(r["run"])
    assert path.name == "agent-sub9.jsonl"
    assert [e["cmd"] for e in learn.build_trace(s, s.load_run(r["run"]))] == [f"touch {fixed}"]


def test_unknown_transcript_format_is_a_clear_error(isolated):
    s, _ = failing(isolated)
    r = start(s)
    d = transcripts.PROJECTS / transcripts.project_folder(isolated)
    d.mkdir(parents=True)
    (d / "x.jsonl").write_text(json.dumps({"kind": "msg", "body": f'"run": "{r["run"]}"'}) + "\n")
    res = learn.learn_propose(s, s.load_run(r["run"]))
    assert res["status"] == "error" and "format changed" in res["error"]


# ---------- lifecycle hooks ----------
def run_hook(name, payload, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = hooks.run(name)
    return code, capsys.readouterr()


def test_session_start_lists_flows_and_runs_within_limit(isolated, monkeypatch, capsys):
    s, _ = failing(isolated)
    r = start(s)
    for i in range(8):
        make(f"g{i}", {"start": "done", "nodes": {"done": {"type": "end"}}})
    code, io_ = run_hook("session-start", {"source": "compact"}, monkeypatch, capsys)
    lines = io_.out.strip().splitlines()
    assert code == 0 and len(lines) <= hooks.MAX_LINES
    assert any("Saved flows" in x for x in lines) and any(r["run"] in x and "flow learn f" in x for x in lines)


def test_session_start_is_silent_with_nothing_to_say(monkeypatch, capsys):
    code, io_ = run_hook("session-start", {"source": "startup"}, monkeypatch, capsys)
    assert code == 0 and io_.out == ""


def test_hooks_never_break_a_session(isolated, monkeypatch, capsys):
    s, _ = failing(isolated)
    start(s)
    next(s.runs_dir.glob("*.json")).write_text("{not json")
    code, _ = run_hook("session-start", {}, monkeypatch, capsys)
    assert code == 0 and "session-start" in hooks.LOG.read_text()


def test_pre_compact_snapshots_paused_runs(isolated, monkeypatch, capsys):
    s, fixed = failing(isolated)
    r = start(s)
    tp = Transcript(isolated).flow("flow run f", r).bash(f"touch {fixed}").write()
    code, _ = run_hook("pre-compact", {"transcript_path": str(tp)}, monkeypatch, capsys)
    assert code == 0 and [e["cmd"] for e in s.trace(r["run"])] == [f"touch {fixed}"]


# ---------- approve-guard ----------
def guard(isolated, monkeypatch, capsys, said=(), answer=None, cmd=None):
    s = confirming()
    r = start(s)
    assert r["status"] == "needs_confirm"
    t = Transcript(isolated).user("yes")  # an approval BEFORE the pause must not count
    t.flow("flow run c", r)
    for x in said:
        t.user(x)
    if answer:
        t.ask("Deploy to prod?", answer)
    tp = t.write()
    cmd = cmd or f"flow resume c --run {r['run']} --approve"
    return run_hook("approve-guard", {"tool_name": "Bash", "tool_input": {"command": cmd}, "transcript_path": str(tp)},
                    monkeypatch, capsys)


@pytest.mark.parametrize("said,answer,code", [
    ((), None, 2), (("yes",), None, 0), (("ok go ahead",), None, 0), (("no wait",), None, 2),
    (("yes", "actually no"), None, 2), ((), "Yes, deploy", 0), ((), "No, hold off", 2)])
def test_approve_guard(isolated, monkeypatch, capsys, said, answer, code):
    got, io_ = guard(isolated, monkeypatch, capsys, said, answer)
    assert got == code and (code == 0 or "approve-guard" in io_.err)


def test_approve_guard_ignores_other_commands(isolated, monkeypatch, capsys):
    assert guard(isolated, monkeypatch, capsys, cmd="git push")[0] == 0


def test_bash_fast_path_skips_python_for_ordinary_commands(isolated):
    """The PreToolUse command is plain bash: only input containing --approve reaches `flow` (here a fake on PATH)."""
    bin_ = isolated / "fakebin"
    bin_.mkdir()
    (bin_ / "flow").write_text("#!/bin/sh\necho reached-python >&2\nexit 2\n")
    (bin_ / "flow").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}
    t0 = time.perf_counter()
    r = subprocess.run(["bash", "-c", hooks.APPROVE_GUARD], input='{"tool_name":"Bash","tool_input":{"command":"ls -la"}}',
                       text=True, capture_output=True, env=env)
    assert r.returncode == 0 and "reached-python" not in r.stderr and time.perf_counter() - t0 < 0.2
    r = subprocess.run(["bash", "-c", hooks.APPROVE_GUARD], input='{"tool_input":{"command":"flow resume c --approve"}}',
                       text=True, capture_output=True, env=env)
    assert r.returncode == 2 and "reached-python" in r.stderr


# ---------- detach, script hashes ----------
def test_detach_and_wait(capsys):
    make("slow", {"start": "a", "nodes": {"a": {"type": "run", "cmd": "sleep 2; echo finished", "ok": "done"},
                                          "done": {"type": "end", "say": "{{ last_output }}"}}})
    cli.main(["run", "slow", "--detach"])
    res = json.loads(capsys.readouterr().out)
    assert res["status"] == "running"
    cli.main(["status", "slow", "--run", res["run"], "--wait", "30"])
    done = json.loads(capsys.readouterr().out)
    assert done["status"] == "done" and done["say"] == "finished"


def test_changed_script_pauses_a_pinned_run_until_committed():
    s = FlowStore("sc")
    (s.dir / "steps").mkdir(parents=True)
    (s.dir / "steps" / "x.sh").write_text("echo v1\n")
    s.apply([{"op": "replace_graph", "graph": {"start": "a", "nodes": {
        "a": {"type": "run", "script": "x.sh", "effect": "read", "ok": "done"},
        "done": {"type": "end", "say": "{{ last_output }}"}}}}], "t", "c")
    assert start(s)["say"] == "v1"
    (s.dir / "steps" / "x.sh").write_text("echo v2\n")
    assert s.uncommitted()
    r = start(s)
    assert r["status"] == "needs_human" and "changed since v1" in r["say"]
    assert engine.resume(s, s.load_run(r["run"]), ["--done"])["say"] == "v2"
    assert s.commit_hand_edit("t", "script v2")["version"] == 2 and start(s)["say"] == "v2"


# ---------- miner: subagents, usage, mention rule ----------
def test_miner_counts_subagents_and_flow_usage(isolated):
    proj = isolated / "repo"
    for i, sess in enumerate(("s1", "s2")):
        sub = Transcript(proj, "x").flow("uv run flow.py run apk-build", {"status": "done", "run": f"r{i}"})
        t = Transcript(proj, sess).bash("npm run build").bash("npx wrangler deploy")
        t.flow("flow run git-push-recovery", {"status": "needs_claude", "run": f"q{i}"})
        t.agent(f"ag{i}", sub).write()
    assert mine.mine()["sessions"] == 4
    u = mine.usage()["usage"]
    assert u["apk-build"]["by_subagent"] == 2 and u["apk-build"]["statuses"] == {"done": 2}
    assert u["git-push-recovery"]["by_main"] == 2 and u["git-push-recovery"]["sessions"] == 2
    seq = next(c for c in mine.list_candidates() if c["kind"] == "sequence")
    assert "flow" not in seq["title"] and seq["sessions"] == 2 and mine.worth_mentioning({**seq, "status": "new"})


def test_single_session_without_corrections_is_not_mentioned():
    c = {"status": "new", "covered_by": None, "sessions": 1, "corrected": 0}
    assert not mine.worth_mentioning(c) and mine.worth_mentioning({**c, "corrected": 1})


def test_timing_marks_slow_flows_from_measured_runs(monkeypatch, capsys):
    s = make("t", {"start": "a", "nodes": {"a": {"type": "run", "cmd": "true", "ok": "done"}, "done": {"type": "end"}}})
    for _ in range(3):
        start(s)
    tm = s.timing()
    assert tm["runs"] == 3 and tm["p90"] < 1 and set(tm["steps"]) == {"a"} and tm["steps"]["a"]["runs"] == 3
    assert "avg" in __import__("flowkit.mermaid").mermaid.to_mermaid(s.load(), timing=tm)
    monkeypatch.setattr(hooks, "SLOW_AFTER", -1)
    out = run_hook("session-start", {"source": "resume"}, monkeypatch, capsys)[1].out
    assert "- t (avg 0s over 3 runs):" in out and "Slow (p90 > -1s here): t" in out


APPROVE_FLAG = "--" + "approve"  # spelled out so this file's own text never looks like an approval call


@pytest.mark.parametrize("cmd,hit", [
    (f"flow resume c --run R1 {APPROVE_FLAG}", ("c", "R1", None)),
    (f"CLAUDE_PROJECT_DIR=/x uv run --quiet /p/flow.py resume c {APPROVE_FLAG}", ("c", None, "/x")),
    (f"cd /x && flow resume c --run R2 {APPROVE_FLAG} | jq .", ("c", "R2", None)),
    (f"SB=/sand && CLAUDE_PROJECT_DIR=$SB flow resume c {APPROVE_FLAG}", ("c", None, "/sand")),
    (f"python3 -c \"print('flow resume c {APPROVE_FLAG}')\"", None),            # text inside a quoted string
    (f"cat > t.py <<'EOF'\nflow resume c {APPROVE_FLAG}\nEOF", None),            # text inside a heredoc
    (f"git commit -m 'guard flow resume {APPROVE_FLAG}'", None),
    ("flow resume c --run R3 --done", None),
])
def test_approve_guard_only_fires_on_real_calls(cmd, hit):
    assert hooks.approve_call(cmd) == hit


def test_approve_guard_uses_pause_time_when_output_was_reformatted(isolated, monkeypatch, capsys):
    s = confirming()
    r = start(s)
    t = Transcript(isolated).bash("flow run c | jq .status", '"needs_confirm"')   # no run id in the transcript
    t.recs[-1]["timestamp"] = t.recs[-2]["timestamp"] = "2000-01-01T00:00:00Z"   # before the pause
    t.user("yes")
    t.recs[-1]["timestamp"] = "2999-01-01T00:00:00Z"                             # after the pause
    tp = t.write()
    payload = {"tool_name": "Bash", "transcript_path": str(tp),
               "tool_input": {"command": f"flow resume c --run {r['run']} {APPROVE_FLAG}"}}
    assert run_hook("approve-guard", payload, monkeypatch, capsys)[0] == 0
    t.recs.pop()
    t.write()
    assert run_hook("approve-guard", payload, monkeypatch, capsys)[0] == 2
