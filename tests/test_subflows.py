"""Flows that call other flows: the call, pauses through the parent, pins, organisation across projects."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowkit import engine  # noqa: E402
from flowkit.mermaid import to_mermaid  # noqa: E402
from flowkit.model import FlowError, validate  # noqa: E402
from flowkit.store import FlowStore, callers, resolver  # noqa: E402
from test_flowkit import isolated, start, with_effects  # noqa: E402,F401


def save(name, graph, scope=None):
    s = FlowStore(name, scope=scope or "project")
    res = s.apply([{"op": "replace_graph", "graph": with_effects(graph)}], "test", "create")
    assert res["status"] == "applied", res
    return s


def child(confirm=False, out="v1"):
    """push-like child: optionally asks before its step; reports `pushed`."""
    nodes = {"go": {"type": "run", "cmd": f'echo "pushed={{{{ inputs.branch }}}}-{out}" >> "$FLOW_OUTPUT"',
                    "outputs": {"pushed": {"help": "what was pushed"}}, "ok": "done"},
             "done": {"type": "end", "say": "pushed"}}
    if confirm:
        nodes["ask"] = {"type": "confirm", "say": "push it?", "next": "go"}
    return {"start": "ask" if confirm else "go",
            "inputs": {"branch": {"type": "string", "help": "branch to push"}},
            "outputs": {"pushed": {"from": "go.pushed", "help": "what was pushed"}}, "nodes": nodes}


def parent(**step):
    return {"start": "push", "outputs": {"shipped": {"from": "push.pushed", "help": "what shipped"}}, "nodes": {
        "push": {"type": "flow", "flow": "pusher", "inputs": {"branch": "main"}, "ok": "done", **step},
        "done": {"type": "end", "say": "shipped {{ outputs.push.pushed }}"}}}


def test_parent_calls_child_and_gets_its_declared_outputs():
    save("pusher", child(), "global")
    p = save("ship", parent())
    assert p.load()["uses"] == {"pusher": {"scope": "global", "version": 1}}
    r = start(p)
    assert r["status"] == "done" and r["say"] == "shipped main-v1" and r["outputs"] == {"shipped": "main-v1"}
    assert r["steps"][0]["flow"] == "pusher" and r["steps"][0]["ended"] == "done"


def test_templates_are_checked_against_the_childs_contract():
    save("pusher", child(), "global")
    g = parent()
    g["nodes"]["done"]["say"] = "{{ outputs.push.nope }}"
    assert any("does not declare output 'nope'" in e for e in validate(g, None, resolver()))
    g = parent(inputs={})
    assert any("needs input 'branch'" in e for e in validate(g, None, resolver()))
    g = parent(inputs={"branch": "main", "bogus": "x"})
    assert any("has no input 'bogus'" in e for e in validate(g, None, resolver()))


def test_a_childs_confirm_pauses_the_parent_and_one_resume_finishes_both():
    save("pusher", child(confirm=True), "global")
    p = save("ship", parent())
    r = start(p)
    assert r["status"] == "needs_confirm" and r["say"] == "push it?" and r["in_flow"]["flow"] == "pusher"
    kid = FlowStore("pusher", scope="global")
    with pytest.raises(FlowError, match="resume that instead"):
        engine.resume(kid, kid.load_run(r["in_flow"]["run"]), ["--approve"])
    done = engine.resume(p, p.load_run(r["run"]), ["--approve"])
    assert done["status"] == "done" and done["outputs"] == {"shipped": "main-v1"}
    assert kid.load_run(r["in_flow"]["run"])["status"] == "done"


def test_abort_on_the_parent_aborts_the_child():
    save("pusher", child(confirm=True), "global")
    p = save("ship", parent())
    r = start(p)
    assert engine.resume(p, p.load_run(r["run"]), ["--abort"])["status"] == "aborted"
    assert FlowStore("pusher", scope="global").load_run(r["in_flow"]["run"])["status"] == "aborted"


def test_a_child_that_fails_takes_the_parents_fail_edge_or_pauses_for_claude():
    g = child()
    g["nodes"]["go"]["cmd"] = "echo boom; exit 3"   # no recovery path in the child
    save("pusher", g, "global")
    p = save("ship", {**parent(), "outputs": {}})
    r = start(p)                                     # no fail edge: Claude fixes it in the child, learning goes there
    assert r["status"] == "failed" and r["in_flow"]["flow"] == "pusher" and "flow learn pusher" in r["hint"]
    g = {**parent(fail="fallback"), "outputs": {}}
    g["nodes"]["fallback"] = {"type": "end", "say": "fell back: {{ last_output }}"}
    q = save("ship2", g)
    r = start(q)
    assert r["status"] == "done" and r["ended"] == "fallback" and "boom" in r["say"]


def test_a_name_in_both_project_and_global_is_ambiguous():
    save("pusher", child(), "global")
    save("pusher", child())  # a project flow with the same name
    res = FlowStore("ship").apply([{"op": "replace_graph", "graph": with_effects(parent())}], "t", "x")
    assert res["status"] == "rejected" and "exists both in this project and in ~/.claude/flows" in res["error"]


def test_call_cycles_are_rejected():
    save("a", {"start": "x", "nodes": {"x": {"type": "run", "cmd": "true", "ok": "done"}, "done": {"type": "end"}}}, "global")
    save("b", {"start": "c", "nodes": {"c": {"type": "flow", "flow": "a", "ok": "done"}, "done": {"type": "end"}}}, "global")
    a = FlowStore("a")
    res = a.apply([{"op": "replace_graph", "graph": {"start": "c", "nodes": {
        "c": {"type": "flow", "flow": "b", "ok": "done"}, "done": {"type": "end"}}}}], "t", "x")
    assert res["status"] == "rejected" and "call cycle" in res["error"]


def test_callers_stay_pinned_and_upgrade_is_checked_against_the_new_contract():
    kid = save("pusher", child(), "global")
    p = save("ship", parent())
    kid.apply([{"op": "replace_graph", "graph": with_effects(child(out="v2"))}], "t", "v2")
    assert start(p)["outputs"] == {"shipped": "main-v1"}                  # still pinned to v1
    assert callers(kid) == [{"dir": str(p.dir), "flow": "ship", "version": 1}]
    assert p.apply([{"op": "pin_flow", "flow": "pusher", "version": 2}], "t", "upgrade")["status"] == "applied"
    assert start(p)["outputs"] == {"shipped": "main-v2"}
    broken = child(out="v3")
    broken["outputs"] = {}
    broken["nodes"]["go"]["outputs"] = {"other": {"help": "x"}}
    broken["nodes"]["go"]["cmd"] = 'echo "other=1" >> "$FLOW_OUTPUT"'
    kid.apply([{"op": "replace_graph", "graph": with_effects(broken)}], "t", "drop pushed")
    res = p.apply([{"op": "pin_flow", "flow": "pusher", "version": 3}], "t", "upgrade")
    assert res["status"] == "rejected" and "does not declare output 'pushed'" in res["error"]


def test_promote_moves_a_project_flow_to_global_and_repins_callers(isolated):
    save("pusher", child())
    p = save("ship", parent())
    assert p.load()["uses"]["pusher"]["scope"] == "project"
    res = FlowStore("pusher").promote()
    assert res["status"] == "promoted" and (isolated / "home" / ".claude" / "flows" / "pusher" / "flow.yaml").exists()
    assert p.load()["uses"]["pusher"] == {"scope": "global", "version": 1}
    assert start(p)["status"] == "done"


def test_chart_draws_a_called_flow_as_a_linked_subroutine():
    save("pusher", child(), "global")
    p = save("ship", parent())
    chart = to_mermaid(p.load())
    assert 'push[["push<br/>flow: pusher v1' in chart and "%% link push -> pusher" in chart
    assert json.loads(json.dumps(p.load()["uses"]))


def test_chart_labels_with_brackets_are_quoted():
    """Mermaid reads `(`, `[` or `{` inside a bare |label| as a shape and refuses the whole chart."""
    g = {"start": "c", "nodes": {
        "c": {"type": "decide", "question": "Clean?", "check": "true", "options": ["yes", "no"],
              "branches": {"yes": "e", "no": "e"}}, "e": {"type": "end"}}}
    import re
    bare = [m for m in re.findall(r"\|([^|]*)\|", to_mermaid(g)) if re.search(r"[()\[\]{}]", m) and not m.startswith('"')]
    assert bare == []


def test_chart_colors_start_done_and_failure_steps():
    g = {"start": "a", "nodes": {
        "a": {"type": "run", "cmd": "x", "effect": "read", "ok": "done", "fail": "cleanup",
              "recover": [{"id": "r1", "when": "w", "match": "m", "to": "fix"}]},
        "fix": {"type": "run", "cmd": "y", "effect": "read", "ok": "a"},
        "cleanup": {"type": "end", "say": "gave up"},
        "done": {"type": "end"}}}
    chart = to_mermaid(g)
    for line in ("class a start", "class done done", "class fix failpath", "class cleanup failpath"):
        assert line in chart, line
    assert "class a__why" not in chart                                  # the diamond keeps its own color


def test_ok_and_fail_to_the_same_step_is_rejected():
    g = {"start": "a", "nodes": {"a": {"type": "run", "cmd": "x", "effect": "read", "ok": "b", "fail": "b"},
                                 "b": {"type": "end"}}}
    assert any("result is ignored" in e for e in validate(g))
