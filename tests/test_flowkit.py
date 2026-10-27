"""Unit tests for flowkit. Run: uv run --with pytest --with pyyaml pytest -q flowtests/test_flowkit.py"""
import multiprocessing
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowkit import engine, secrets  # noqa: E402
from flowkit.model import FlowError, resolve_inputs, validate  # noqa: E402
from flowkit.ops import OpError, apply_op  # noqa: E402
from flowkit.store import FlowStore  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for v in ("DECISION_MODEL_URL", "DECIDER_URL", "JEV_URL"):
        monkeypatch.delenv(v, raising=False)
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
    res = s.apply([{"op": "replace_graph", "graph": graph}], "test", "create")
    assert res["status"] == "applied", res
    return s


def linear(cmd="echo hi", **extra):
    return {"start": "a", "nodes": {"a": {"type": "run", "cmd": cmd, "ok": "done", **extra},
                                    "done": {"type": "end", "say": "{{ last_output }}"}}}


def start(s, inputs=None):
    flow = s.load()
    return engine.run(s, s.new_run(flow["version"], resolve_inputs(flow, inputs or {}), flow["start"]))


# ---------- validation ----------
def test_validate_catches_structural_problems():
    g = linear(effect="read")
    g["nodes"]["a"]["cmd"] = "echo {{ inputs.nope }}"
    g["nodes"]["a"]["bogus"] = 1
    errs = validate(g)
    assert any("undeclared input" in e for e in errs) and any("unknown key 'bogus'" in e for e in errs)


def test_effect_is_required_and_once_needs_confirm():
    g = linear(cmd="npx wrangler deploy")
    assert any("declare 'effect' (once)" in e for e in validate(g))
    g["nodes"]["a"]["effect"] = "once"
    assert any("entered only from a confirm" in e for e in validate(g))
    g["nodes"]["a"]["effect"] = "idempotent"
    assert validate(g) == []


# ---------- ops ----------
def test_remove_node_refuses_while_referenced_and_rewires():
    g = linear()
    g["nodes"]["b"] = {"type": "run", "cmd": "true", "ok": "done"}
    g["nodes"]["a"]["ok"] = "b"
    with pytest.raises(OpError, match="still referenced"):
        apply_op(g, {"op": "remove_node", "id": "b"})
    g2, _ = apply_op(g, {"op": "remove_node", "id": "b", "rewire": "done"})
    assert g2["nodes"]["a"]["ok"] == "done" and "b" not in g2["nodes"]


def test_rename_rewrites_edges_and_output_templates():
    g = linear(effect="read")
    g["nodes"]["done"]["say"] = "{{ outputs.a.x }}"
    g2, _ = apply_op(g, {"op": "rename_node", "id": "a", "to": "build"})
    assert g2["start"] == "build" and g2["nodes"]["done"]["say"] == "{{ outputs.build.x }}"


def test_remove_input_refuses_while_used():
    g = linear(cmd="echo {{ inputs.x }}")
    g["inputs"] = {"x": {"type": "string", "default": "1", "help": "x"}}
    with pytest.raises(OpError, match="used by"):
        apply_op(g, {"op": "remove_input", "name": "x"})


# ---------- versions, history, hand edits ----------
def test_versions_history_and_revert():
    s = make("f", linear())
    assert s.apply([{"op": "update_node", "id": "a", "set": {"cmd": "echo v2"}}], "t", "r")["version"] == 2
    assert s.apply([{"op": "replace_graph", "graph": s.load(1)}], "t", "revert")["version"] == 3
    assert s.load(3)["nodes"]["a"]["cmd"] == "echo hi"
    assert [h["version"] for h in s.history()] == [1, 2, 3]


def test_invalid_edit_is_rejected_and_nothing_written():
    s = make("f", linear())
    res = s.apply([{"op": "set_edge", "id": "a", "kind": "ok", "to": None}], "t", "break it")
    assert res["status"] == "rejected" and s.head_version() == 1


def test_hand_edit_must_be_committed_and_bad_commit_keeps_the_edit():
    s = make("f", linear())
    f = s.dir / "flow.yaml"
    f.write_text(f.read_text().replace("echo hi", "echo edited"))
    assert s.apply([{"op": "update_node", "id": "a", "set": {"note": "x"}}], "t", "r")["status"] == "rejected"
    assert s.commit_hand_edit("t", "hand")["version"] == 2
    f.write_text(f.read_text().replace("ok: done", "ok: nowhere"))
    assert s.commit_hand_edit("t", "bad")["status"] == "rejected"
    assert "nowhere" in f.read_text() and s.head_version() == 2


# ---------- concurrency: rebase, proposals, merges ----------
def test_additive_ops_rebase_and_conflicting_edits_become_proposals():
    g = linear()
    g["nodes"]["b"] = {"type": "run", "cmd": "true", "ok": "done", "effect": "read"}
    g["nodes"]["a"]["ok"] = "b"
    s = make("f", g)                                                            # v1
    assert s.apply([{"op": "update_node", "id": "a", "set": {"cmd": "echo A"}}], "run1", "x", base=1)["version"] == 2
    learn = {"op": "learn_path", "at": "b", "when": "b breaks", "cmds": ["make fix"]}
    r = s.apply([learn], "run2", "learned", base=1)                             # additive: rebases onto v2
    assert r["status"] == "applied" and r["rebased"] and r["version"] == 3
    r = s.apply([{"op": "update_node", "id": "a", "set": {"cmd": "echo B"}}], "run3", "x", base=1)
    assert r["status"] == "proposed" and s.head_version() == 3                  # same node changed since v1
    pid = r["proposal"]
    assert s.resolve_proposal(pid, True, "me")["status"] == "accepted"
    assert s.load()["nodes"]["a"]["cmd"] == "echo B" and s.proposals() == []


def test_identical_learned_paths_merge():
    s = make("f", linear(effect="read"))
    learn = {"op": "learn_path", "at": "a", "when": "w", "cmds": ["fix it"]}
    assert s.apply([learn], "r1", "w", base=1)["version"] == 2
    r = s.apply([learn], "r2", "w", base=1)
    assert r["status"] == "unchanged" and "merged" in r["notes"][0]


def _writer(proj, home, i):
    os.environ["CLAUDE_PROJECT_DIR"], os.environ["HOME"] = proj, home
    s = FlowStore("f")
    res = s.apply([{"op": "add_node", "id": f"n{i}", "node": {"type": "end", "say": "x"}},
                   {"op": "set_edge", "id": "a", "kind": "fail", "to": f"n{i}"}], f"w{i}", "race", base=1)
    return res["status"]


def test_parallel_writers_never_lose_updates(isolated):
    make("f", linear())
    with multiprocessing.get_context("fork").Pool(8) as pool:
        res = pool.starmap(_writer, [(str(isolated), str(isolated / "home"), i) for i in range(8)])
    s = FlowStore("f")
    # set_edge on `a` conflicts after the first writer: one applies, the rest queue as proposals
    assert res.count("applied") == 1 and res.count("proposed") == 7
    assert s.head_version() == 2 and len(s.proposals()) == 7
    assert len({p.name for p in (s.dir / "versions").glob("*.yaml")}) == 2


def test_parallel_additive_writers_all_land(isolated):
    g = linear()
    g["nodes"]["a"]["fail"] = "gave_up"
    g["nodes"]["gave_up"] = {"type": "end"}
    make("f", g)

    def add(i):
        return {"op": "add_recover", "node": "a", "entry": {"when": f"case {i}", "to": "done"}}
    with multiprocessing.get_context("fork").Pool(8) as pool:
        res = pool.starmap(_apply_one, [(str(isolated), str(isolated / "home"), add(i)) for i in range(8)])
    s = FlowStore("f")
    assert all(r["status"] == "applied" for r in res) and s.head_version() == 9
    assert len(s.load()["nodes"]["a"]["recover"]) == 8 and len({r["id"] for r in s.load()["nodes"]["a"]["recover"]}) == 8


def _apply_one(proj, home, op):
    os.environ["CLAUDE_PROJECT_DIR"], os.environ["HOME"] = proj, home
    return FlowStore("f").apply([op], "w", "race", base=1)


# ---------- running ----------
def test_run_is_pinned_to_its_version():
    g = linear(cmd="echo from-v1")
    g["nodes"]["c"] = {"type": "confirm", "say": "go?", "next": "a"}
    g["start"] = "c"
    s = make("f", g)
    r = start(s)
    assert r["status"] == "needs_confirm"
    s.apply([{"op": "update_node", "id": "a", "set": {"cmd": "echo from-v2"}}], "t", "edit mid-run")
    st = s.load_run(r["run"])
    assert engine.resume(s, st, ["--approve"])["say"] == "from-v1"
    assert start(s)["status"] == "needs_confirm"  # new runs get v2


def test_inputs_are_checked_and_shell_quoted(isolated):
    g = linear(cmd="echo {{ inputs.name }}")
    g["inputs"] = {"name": {"type": "string", "help": "who"},
                   "mode": {"type": "enum", "values": ["a", "b"], "default": "a", "help": "how"}}
    s = make("f", g)
    with pytest.raises(FlowError, match="missing required"):
        start(s)
    with pytest.raises(FlowError, match="must be one of"):
        start(s, {"name": "x", "mode": "c"})
    r = start(s, {"name": "x; touch pwned"})
    assert r["say"] == "x; touch pwned" and not (isolated / "pwned").exists()


def test_step_env_strips_credentials_and_sets_defaults(monkeypatch):
    monkeypatch.setenv("FAKE_API_TOKEN", "leakme123")
    monkeypatch.setenv("KEEP_TOKEN", "allowed123")
    s = make("f", linear(cmd="env | grep -E '^(FAKE_API|KEEP_TOKEN|GIT_TERMINAL_PROMPT|FLOW_RUN|MINE)=' | sort",
                         env_passthrough=["KEEP_TOKEN"], env={"MINE": "{{ run.flow }}"}))
    out = start(s)["say"]
    assert "leakme123" not in out and "KEEP_TOKEN=" in out and "GIT_TERMINAL_PROMPT=0" in out
    assert "FLOW_RUN=1" in out and "MINE=f" in out


def test_doppler_secrets_are_injected_masked_and_never_stored(isolated, monkeypatch):
    bin_ = isolated / "bin"
    bin_.mkdir()
    (bin_ / "doppler").write_text('#!/bin/sh\n[ "$3" = MY_TOKEN ] && echo "s3cr3t-value" && exit 0\nexit 1\n')
    (bin_ / "doppler").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    g = linear(cmd='echo "token is $MY_TOKEN"; echo "other=$OTHER"', secrets=["MY_TOKEN"])
    g["nodes"]["b"] = {"type": "run", "cmd": 'echo "b sees [$MY_TOKEN]"', "ok": "done"}
    g["nodes"]["a"]["ok"] = "b"
    s = make("f", g)
    r = start(s)
    assert r["status"] == "done" and "b sees []" in r["say"]
    st_text = (s.runs_dir / f"{r['run']}.json").read_text()
    assert "s3cr3t-value" not in st_text and "<secret:MY_TOKEN>" in st_text


def test_missing_secret_pauses_for_a_human_without_leaking(isolated, monkeypatch):
    bin_ = isolated / "bin"
    bin_.mkdir()
    (bin_ / "doppler").write_text("#!/bin/sh\necho 'Doppler Error: secret not found' >&2\nexit 1\n")
    (bin_ / "doppler").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    r = start(make("f", linear(secrets=["NOPE"])))
    assert r["status"] == "needs_human" and "NOPE" in r["say"]


def test_failure_order_match_then_fail_edge_then_human():
    g = linear(cmd="echo 'boom: disk full' >&2; exit 3", effect="read")
    g["nodes"]["fix"] = {"type": "run", "cmd": "true", "ok": "done", "effect": "read"}
    g["nodes"]["catch"] = {"type": "end", "say": "caught"}
    g["nodes"]["a"]["recover"] = [{"id": "disk", "when": "disk full", "match": "disk full", "to": "fix"}]
    g["nodes"]["a"]["fail"] = "catch"
    s = make("f", g)
    assert [e.get("by") for e in start(s)["steps"] if "recover" in e] == ["match"]   # regex beats fail edge
    s.apply([{"op": "update_node", "id": "a", "set": {"cmd": "exit 3"}}], "t", "no match")
    assert start(s)["say"] == "caught"                                              # then the catch-all
    assert s.apply([{"op": "set_edge", "id": "a", "kind": "fail", "to": "human: look at it"},
                    {"op": "remove_node", "id": "catch"}], "t", "x")["status"] == "applied"
    assert start(s)["status"] == "needs_human"


def test_no_fail_edge_with_paths_goes_to_claude_and_choose_labels():
    g = linear(cmd="exit 1", effect="read")
    g["nodes"]["p1"] = {"type": "run", "cmd": "true", "ok": "done", "effect": "read"}
    g["nodes"]["a"]["recover"] = [{"id": "x", "when": "x", "to": "p1"}, {"id": "y", "when": "y", "to": "p1"}]
    s = make("f", g)
    r = start(s)
    assert r["status"] == "needs_claude" and r["options"] == ["x", "y", "none"]


def test_step_timeout():
    r = start(make("f", linear(cmd="sleep 3", timeout=1, fail="human: too slow")))
    assert r["status"] == "needs_human" and r["steps"][0]["exit"] == 124


def test_outputs_from_flow_output_file_and_regex():
    g = linear(cmd='echo "path=/x/y.apk" >> "$FLOW_OUTPUT"; echo "size 12M"',
               outputs={"path": {"help": "the apk"}, "size": {"regex": r"size (\S+)", "help": "its size"}})
    g["nodes"]["done"]["say"] = "{{ outputs.a.path }} {{ outputs.a.size }}"
    g["outputs"] = {"apk": {"from": "a.path", "help": "where the apk is"}}
    r = start(make("f", g))
    assert r["say"] == "/x/y.apk 12M" and r["outputs"] == {"apk": "/x/y.apk"} and r["ended"] == "done"
    assert r["step_outputs"]["a"]["size"] == "12M"


def test_templates_and_flow_outputs_may_only_use_declared_outputs():
    g = linear(cmd='echo "path=/x" >> "$FLOW_OUTPUT"')
    g["nodes"]["done"]["say"] = "{{ outputs.a.path }}"
    assert any("does not declare output 'path'" in e for e in validate(g))
    g["nodes"]["a"]["outputs"] = {"path": {}}
    assert any("needs 'help'" in e for e in validate(g))
    g["nodes"]["a"]["outputs"] = {"path": {"help": "p"}}
    g["outputs"] = {"apk": {"from": "a.nope", "help": "x"}}
    assert any("is not an output a step declares" in e for e in validate(g))
    g["inputs"] = {"x": {"type": "string"}}
    assert any("add a 'help' line" in e for e in validate(g))


def test_a_step_that_does_not_report_a_declared_output_fails():
    g = linear(cmd="true", outputs={"path": {"help": "p"}}, fail="human: no path")
    r = start(make("f", g))
    assert r["status"] == "needs_human" and "did not report path" in r["output_tail"]


def test_learned_path_is_promoted_after_three_proven_fixes(isolated):
    flag = isolated / "ok"
    g = linear(cmd=f"test -f {flag} || {{ echo 'flag missing'; exit 1; }}", effect="read")
    s = make("f", g)
    s.apply([{"op": "learn_path", "at": "a", "when": "flag missing", "cmds": [f"touch {flag}"], "match": "flag missing"}], "t", "x")
    rid = s.load()["nodes"]["a"]["recover"][0]["id"]
    for i in range(3):
        flag.unlink(missing_ok=True)
        r = start(s)
        assert r["status"] == "needs_confirm"                    # provisional paths sit behind a gate
        r = engine.resume(s, s.load_run(r["run"]), ["--approve"])
        assert r["status"] == "done", r
    entry = s.load()["nodes"]["a"]["recover"][0]
    assert entry["status"] == "promoted" and "gate" not in entry and s.recover_stats()[rid] == (3, 0)
    flag.unlink()
    assert start(s)["status"] == "done"                          # promoted: no gate any more


# ---------- decisions ----------
def picker(prints):
    return {"start": "which", "nodes": {
        "which": {"type": "decide", "question": "Which one?", "by": "output", "options": ["a", "b", "c"],
                  "check": f'echo "note=n1" >> "$FLOW_OUTPUT"; echo thinking; echo {prints}',
                  "outputs": {"note": {"help": "a note"}},
                  "branches": {"a": "done_a", "b": "done_b", "c": "done_b"}},
        "done_a": {"type": "end", "say": "A {{ outputs.which.choice }} {{ outputs.which.note }}"},
        "done_b": {"type": "end", "say": "B"}}}


def test_decide_by_output_takes_the_printed_option_and_keeps_outputs():
    assert start(make("p", picker("a")))["say"] == "A a n1"
    assert start(make("q", picker("c")))["say"] == "B"


def test_decide_by_output_hands_unexpected_answers_to_claude():
    r = start(make("p", picker("banana")))
    assert r["status"] == "needs_claude" and r["options"] == ["a", "b", "c"] and "banana" in r["output_tail"]


def test_more_than_two_options_needs_by_output():
    g = picker("a")
    del g["nodes"]["which"]["by"]
    assert any("needs `by: output`" in e for e in validate(g))


def test_fix_and_recheck_loop_through_a_human_step_is_allowed():
    g = {"start": "clean", "nodes": {
        "clean": {"type": "decide", "question": "Clean?", "check": "true", "options": ["yes", "no"],
                  "branches": {"yes": "done", "no": "fix"}},
        "fix": {"type": "human", "say": "commit first", "next": "clean"},
        "done": {"type": "end"}}}
    assert validate(g) == []
    g["nodes"]["fix"] = {"type": "run", "cmd": "true", "effect": "read", "ok": "clean"}   # no person in the loop
    assert any("cycle on the success path" in e for e in validate(g))


def test_chart_draws_failure_handling_as_a_diamond():
    from flowkit.mermaid import to_mermaid
    g = linear(effect="read")
    g["nodes"]["a"]["recover"] = [{"id": "r1", "when": "it broke", "match": "x", "to": "done"}]
    chart = to_mermaid(g)
    assert 'a__why{"what failed?"}' in chart and "r1 · regex" in chart and "a__claude" in chart
