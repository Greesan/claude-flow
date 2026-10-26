"""Tests for checks, tracing, patch learning and the miner. Run: uv run --with pytest --with pyyaml pytest -q flowtests"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from flowkit import engine, learn, mine, secrets, transcripts  # noqa: E402
from flowkit.store import FlowStore  # noqa: E402
from transcript_fixture import Transcript  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for v in ("DECISION_MODEL_URL", "DECIDER_URL", "JEV_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(mine, "MINE", tmp_path / "home" / ".claude" / "flows" / ".mine")
    monkeypatch.setattr(mine, "PROJECTS", tmp_path / "home" / ".claude" / "projects")
    monkeypatch.setattr(transcripts, "PROJECTS", tmp_path / "home" / ".claude" / "projects")
    secrets._cache.clear()
    return tmp_path


def with_effects(g):
    """Test convenience: run nodes without an explicit effect get `idempotent` (validate requires one)."""
    for n in g["nodes"].values():
        if n.get("type", "run") == "run":
            n.setdefault("effect", "idempotent")
    return g


def make(name, graph):
    graph = with_effects(graph)
    s = FlowStore(name)
    assert s.apply([{"op": "replace_graph", "graph": graph}], "test", "create")["status"] == "applied"
    return s


def start(s):
    g = s.load()
    return engine.run(s, s.new_run(g["version"], {}, g["start"]))


# ---------- checks ----------
def test_check_skips_done_work_and_catches_false_success(isolated):
    marker = isolated / "m"
    g = {"start": "a", "nodes": {"a": {"type": "run", "cmd": f"touch {marker}", "check": f"test -f {marker}", "ok": "done"},
                                 "done": {"type": "end"}}}
    s = make("f", g)
    assert start(s)["steps"][0]["exit"] == 0 and marker.exists()
    assert start(s)["steps"][0] == {"node": "a", "skipped": "check already passes"}
    marker.unlink()
    s.apply([{"op": "update_node", "id": "a", "set": {"cmd": "true", "fail": "human: x"}}], "t", "lying step")
    r = start(s)
    assert r["status"] == "needs_human" and r["steps"][0]["exit"] == 1     # exit 0 but check failed


def test_learned_step_never_needed_is_pruned():
    g = {"start": "a", "nodes": {"a": {"type": "run", "cmd": "true", "check": "true", "ok": "done", "learned_from": "r9"},
                                 "done": {"type": "end"}}}
    s = make("f", g)
    for _ in range(3):
        assert start(s)["status"] == "done"
    assert "a" not in s.load()["nodes"] and s.load()["start"] == "done"
    assert s.history()[-1]["author"] == "evidence"


# ---------- trace = transcript slice ----------
def failing_flow(isolated):
    fixed = isolated / "fixed"
    g = {"start": "push", "nodes": {
        "push": {"type": "run", "cmd": f"test -f {fixed} || {{ echo 'fatal: Authentication failed'; exit 1; }}",
                 "effect": "read", "ok": "done"},
        "done": {"type": "end", "say": "pushed"}}}
    return make("f", g), fixed


def test_slice_labels_tools_and_skips_flow_calls(isolated):
    s, _ = failing_flow(isolated)
    r = start(s)
    assert r["status"] == "failed"
    (Transcript(isolated).bash("git status").flow("uv run flow.py run f", r)
     .bash("git remote -v", "origin https://x").tool("Read", {"file_path": "/x/.git/config"}, "url = https://x")
     .bash("uv run --no-project /p/flow.py status f").bash("npm cache clean --force")
     .tool("Edit", {"file_path": "/x/a.py"}, "ok").bash("uv run flow.py learn f --propose")
     .bash("echo after learn").write())
    t = learn.build_trace(s, s.load_run(r["run"]))
    assert [e["kind"] for e in t] == ["diagnostic", "diagnostic", "action", "edit"]   # before the pause, flow calls
    assert [e["i"] for e in t] == [0, 1, 2, 3] and s.trace(r["run"]) == t            # and after `learn` left out


def paused_with_trace(isolated):
    s, fixed = failing_flow(isolated)
    r = start(s)
    (Transcript(isolated).flow("flow run f", r).bash("git remote -v")       # look
     .bash("npm cache clean --force")                                      # dead end
     .bash(f"touch {fixed}").write())                                      # real fix
    fixed.touch()  # the transcript only records it; Claude's `touch` really ran
    return s, fixed, s.load_run(r["run"])


def patch_for(fixed, **over):
    p = {"rationale": "push failed on auth; trace #0 showed an https remote; #2 fixed it; #1 was a dead end",
         "evidence": {"fix_auth": [0, 2], "auth": [0]},
         "ops": [{"op": "add_node", "id": "fix_auth", "node": {"type": "run", "cmd": f"touch {fixed}", "effect": "idempotent",
                                                               "check": f"test -f {fixed}", "ok": "push"}},
                 {"op": "add_recover", "node": "push", "entry": {"id": "auth", "when": "auth refused",
                                                                 "match": "Authentication failed", "to": "fix_auth"}}]}
    p.update(over)
    return p


def test_propose_shows_failure_and_trace(isolated):
    s, _, st = paused_with_trace(isolated)
    pr = learn.learn_propose(s, st)
    assert pr["failed_node"] == "push" and "Authentication failed" in pr["failure_tail"]
    assert [e["kind"] for e in pr["trace"]] == ["diagnostic", "action", "action"] and pr["rules"]


def test_verified_patch_lands_provisional_behind_a_gate(isolated):
    s, fixed, st = paused_with_trace(isolated)
    r = learn.learn_patch(s, st, patch_for(fixed))
    assert r["status"] == "applied" and r["witness"] == "passed" and r["checks"] == {"fix_auth": True}
    g = s.load()
    entry = g["nodes"]["push"]["recover"][0]
    assert entry["status"] == "provisional" and entry["to"] == "auth_gate" and g["nodes"]["auth_gate"]["next"] == "fix_auth"
    assert g["nodes"]["fix_auth"]["learned_from"] == "auth"
    assert s.history()[-1]["meta"]["evidence"] == {"fix_auth": [0, 2], "auth": [0]}
    fixed.unlink()                                                  # next time: regex -> gate -> fix -> push
    r = start(s)
    assert r["status"] == "needs_confirm"
    assert engine.resume(s, s.load_run(r["run"]), ["--approve"])["status"] == "done"


@pytest.mark.parametrize("bad,why", [
    ({"rationale": ""}, "rationale"),
    ({"evidence": {"auth": [0]}}, "cite trace evidence"),
    ({"evidence": {"fix_auth": [99], "auth": [0]}}, "trace indices"),
])
def test_patch_needs_rationale_and_evidence(isolated, bad, why):
    s, fixed, st = paused_with_trace(isolated)
    r = learn.learn_patch(s, st, patch_for(fixed, **bad))
    assert r["status"] == "rejected" and why in r["error"]


def test_patch_rejects_steps_without_checks_and_wrong_checks(isolated):
    s, fixed, st = paused_with_trace(isolated)
    p = patch_for(fixed)
    del p["ops"][0]["node"]["check"]
    assert "needs a `check`" in learn.learn_patch(s, st, p)["error"]
    p = patch_for(fixed)
    p["ops"][0]["node"]["check"] = "test -f /nope"
    assert "fails now" in learn.learn_patch(s, st, p)["error"]


def test_main_path_change_is_verified_then_queued(isolated):
    s, fixed, st = paused_with_trace(isolated)
    p = {"rationale": "check auth before pushing", "evidence": {"pre": [0]},
         "ops": [{"op": "add_node", "id": "pre", "node": {"type": "run", "cmd": f"touch {fixed}", "check": f"test -f {fixed}",
                                                          "effect": "idempotent", "ok": "push"}},
                 {"op": "set_meta", "start": "pre"}]}
    assert "not allowed" in learn.learn_patch(s, st, p)["error"]
    p["ops"][1] = {"op": "update_node", "id": "push", "set": {"note": "pre-checked"}}
    p["ops"].insert(1, {"op": "set_edge", "id": "push", "kind": "fail", "to": "pre"})
    r = learn.learn_patch(s, st, p)
    assert r["status"] == "proposed" and s.head_version() == 1 and s.proposals()[0]["meta"]["witness"] == "passed"


# ---------- miner ----------
def transcript(path, sessions_cmds):
    lines, n = [], 0
    for cmd, ok, said in sessions_cmds:
        n += 1
        ts = f"2026-10-01T00:00:{n:02d}Z"
        lines.append({"type": "assistant", "timestamp": ts, "cwd": "/home/u/repo", "message": {"content": [
            {"type": "tool_use", "id": f"{path.stem}-t{n}", "name": "Bash", "input": {"command": cmd}}]}})
        lines.append({"type": "user", "timestamp": ts, "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"{path.stem}-t{n}", "content": "ok" if ok else "Exit code 1\nerror",
             "is_error": not ok}]}})
        if said:
            lines.append({"type": "user", "timestamp": ts, "message": {"content": said}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def test_miner_finds_struggles_and_sequences_and_marks_coverage():
    proj = mine.PROJECTS / "-home-u-repo"
    for i in range(2):
        transcript(proj / f"s{i}.jsonl", [
            ("git push origin main", False, None),
            ("git remote set-url origin git@github.com:a/b.git", True, "no, use ssh not https"),
            ("git push origin main", True, None),
            ("npm run build", True, None), ("npx wrangler deploy", True, None)])
    res = mine.mine()
    assert res["parsed"] == 2 and res["sessions"] == 2
    cs = mine.list_candidates()
    struggle = next(c for c in cs if c["kind"] == "struggle")
    assert struggle["occurrences"] == 2 and struggle["title"].startswith("git push")
    assert any(c["kind"] == "sequence" and "npx wrangler deploy" in c["title"] for c in cs)
    full = mine.get_candidate(struggle["id"])
    assert "no, use ssh not https" in full["evidence"][0]["corrections"][0]
    assert mine.mine()["parsed"] == 0                                 # incremental: nothing changed
    # a flow that runs build + deploy now covers the sequence
    make("deploy", {"start": "b", "nodes": {"b": {"type": "run", "cmd": "npm run build", "ok": "d"},
                                            "d": {"type": "run", "cmd": "npx wrangler deploy", "effect": "idempotent",
                                                  "ok": "done"}, "done": {"type": "end"}}})
    mine.mine([str(proj / "s0.jsonl")])
    seq = [mine.get_candidate(c["id"]) for c in mine.list_candidates(show_all=True)
           if c["kind"] == "sequence" and "deploy" in c["title"]]
    # the repeated run is push-recovery + build + deploy: the deploy flow covers 2 of its 4 distinct steps
    assert seq and all(c["covered_by"] is None and c["overlap"] == ["deploy", 0.5] for c in seq)
    brief = mine.draft_brief(full, "push-fix")
    assert brief["skeleton"]["nodes"]["step1"]["check"] and brief["evidence"]
