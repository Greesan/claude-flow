"""Learning from Claude: turn what Claude did while a run was paused into a verified change to the flow.

  learn_propose  the failure plus the trace of Claude's tool calls since the pause (read from the transcript)
  learn_patch    check a Claude-written patch (rationale, trace evidence, a check per new step), verify it
                 against reality, then apply it (new recovery paths, behind an approval gate) or queue it
                 (changes to the main path)
  learn_replay   fallback: learn the exact state-changing commands Claude ran
"""
import json

from . import transcripts
from .engine import PROMOTE_AFTER, run_check, run_step
from .model import FlowError, clone, node_side, recover_ids, validate
from .ops import ADDITIVE, OpError, apply_op
from .store import _atomic_write, resolver
from .trace import has_secret, is_diagnostic, is_flow_call

LEARN_OPS = {"add_node", "add_recover", "add_input", "update_node", "set_edge", "remove_node"}

PATCH_FORMAT = {
    "rationale": "one paragraph: what went wrong, which trace entries show it, why this patch fixes it",
    "evidence": {"<new node id or recover id>": ["<trace index i>", "..."]},
    "ops": [
        {"op": "add_node", "id": "fix_remote", "node": {
            "type": "run", "cmd": "git remote set-url {{ inputs.remote }} \"$(...)\"",
            "check": "git config --get remote.{{ inputs.remote }}.url | grep -q '^git@'", "ok": "push"}},
        {"op": "add_recover", "node": "push", "entry": {
            "when": "HTTPS auth refused", "match": "Authentication failed", "to": "fix_remote"}},
    ],
}
PATCH_RULES = [
    "Learn the intent, not the transcript: skip dead ends, keep only the steps that caused the fix.",
    "Every new run node that changes something needs a `check`: a command that exits 0 once the step's effect is in "
    "place. Diagnostics you ran (kind=diagnostic) are the best source for checks and for decide-node `check`s.",
    "Use {{ inputs.x }} instead of literal values that vary between runs; add_input if a new input is needed.",
    "Recovery paths (add_recover) need a `match` regex from the failure output when one exists; they are added "
    "provisional behind an approval gate and promoted only by evidence.",
    "Changes to the main path (update_node, set_edge, remove_node), e.g. a pre-check that prevents the failure, are "
    "verified the same way and then queued as a proposal for approval.",
    "If the fix was a code change (kind=edit), do not encode it as steps: add a `human:` fail hint instead.",
    "Cite trace indices in `evidence` for every new node and recovery path.",
]


def build_trace(store, st, hint=None):
    """Snapshot what Claude did since this run paused, read from the session transcript (survives compaction).
    The snapshot in .runs/ is what patch evidence is checked against, and it outlives transcript cleanup."""
    path, idx = transcripts.find_run(st["run"], hint=hint)
    if path is None:
        return None
    entries = transcripts.slice_after(path, idx)
    _atomic_write(store.trace_path(st["run"]), "".join(json.dumps(e) + "\n" for e in entries))
    st["transcript"] = str(path)
    store.save_run(st)
    return entries


def learn_propose(store, st):
    """Everything Claude needs to write a patch: the failure, what Claude did since the pause, the format and rules."""
    flow = store.load(st["version"])
    at = (st.get("pending") or {}).get("node") or st["current"]
    try:
        trace = build_trace(store, st)
    except transcripts.TranscriptError as e:
        return {"status": "error", "error": str(e)}
    if trace is None:
        trace = store.trace(st["run"])  # a PreCompact snapshot, if the transcript is gone
        if not trace:
            return {"status": "error", "error": f"no transcript mentions run {st['run']} and no snapshot exists; "
                    "use `flow learn --replay --cmd ...` instead"}
    return {"status": "propose", "run": st["run"], "version": st["version"], "failed_node": at,
            "node": flow["nodes"].get(at), "failure_tail": st.get("last_out", ""),
            "inputs": flow.get("inputs") or {}, "trace": trace, "transcript": st.get("transcript"),
            "patch_format": PATCH_FORMAT, "rules": PATCH_RULES,
            "next": f"flow learn {st['flow']} --run {st['run']} --patch-json '<patch>'   (or --patch file.json)"}


def _process_patch(flow, patch, n_trace):
    """Check a patch and add approval gates in front of new recovery paths. Returns (ops, error)."""
    ops, evidence = patch.get("ops") or [], patch.get("evidence") or {}
    if not (patch.get("rationale") or "").strip():
        return None, "a patch needs a rationale"
    if not ops:
        return None, "a patch needs ops"
    bad = sorted({o.get("op") for o in ops} - LEARN_OPS)
    if bad:
        return None, f"ops not allowed when learning: {bad} (allowed: {sorted(LEARN_OPS)})"
    if has_secret(json.dumps(ops)):
        return None, "an op contains what looks like a secret; declare it under `secrets:` and use the env variable"
    for label, idx in evidence.items():
        if not idx or any(not isinstance(i, int) or not 0 <= i < n_trace for i in idx):
            return None, f"evidence for '{label}' must list trace indices 0..{n_trace - 1}"
    g, out, new_nodes, new_rids = flow, [], [], []
    for op in ops:
        op = clone(op)
        if op["op"] == "add_node":
            n = op.get("node") or {}
            if n.get("type", "run") == "run" and n.get("effect") != "read" and not n.get("check"):
                return None, f"new run node '{op.get('id')}' changes something, so it needs a `check`"
            new_nodes.append(op["id"])
        if op["op"] == "add_recover":
            e = op["entry"]
            e["status"] = "provisional"
            if not e.get("id") or e["id"] in recover_ids(g) or e["id"] in g["nodes"]:
                used = set(recover_ids(g)) | set(g["nodes"])
                e["id"] = next(f"r{i}" for i in range(1, 10_000) if f"r{i}" not in used)
            gate = f"{e['id']}_gate"
            gate_op = {"op": "add_node", "id": gate, "node": {
                "type": "confirm", "learned_from": e["id"], "next": e["to"],
                "say": f"Learned path (unverified): {e['when']}. Approve to try it; it is promoted after "
                       f"{PROMOTE_AFTER} proven fixes."}}
            g, _ = apply_op(g, gate_op)
            out.append(gate_op)
            e["to"], e["gate"] = gate, gate
            new_rids.append(e["id"])
        try:
            g, _ = apply_op(g, op)
        except OpError as err:
            return None, f"{op['op']}: {err}"
        out.append(op)
    missing = [x for x in new_nodes + new_rids if not evidence.get(x)]
    if missing:
        return None, f"cite trace evidence for: {missing}"
    if new_rids:  # nodes added alongside a recovery path belong to it: they are dropped with it
        for op in out:
            if op["op"] == "add_node" and op["id"] in new_nodes:
                op["node"].setdefault("learned_from", new_rids[0])
    return out, None


def learn_patch(store, st, patch):
    """Verify a Claude-written patch against reality, then apply it (recovery) or queue it (main-path change)."""
    flow = store.load(st["version"])
    at = (st.get("pending") or {}).get("node") or st["current"]
    trace = store.trace(st["run"]) or build_trace(store, st) or []
    ops, err = _process_patch(flow, patch, len(trace))
    if err:
        return {"status": "rejected", "error": err}
    g = flow
    for op in ops:
        g, _ = apply_op(g, op)
    g = {**g, "version": flow["version"]}
    errs = validate(g, store.dir, resolver(g.get("uses")))
    if errs:
        return {"status": "rejected", "error": "patched flow would be invalid: " + "; ".join(errs)}
    checks = {}
    for op in ops:  # every new step's check must already pass: the fix was just applied by hand
        n = op.get("node") or {}
        if op["op"] == "add_node" and n.get("check"):
            code, tail = run_check(store, g, op["id"], g["nodes"][op["id"]], st)
            checks[op["id"]] = code == 0
            if code != 0:
                return {"status": "rejected", "error": f"check for '{op['id']}' fails now, so it does not describe the "
                        f"fix you applied: {n['check']}", "output_tail": tail, "checks": checks}
    witness = "skipped (side-effect step)"
    if at in g["nodes"] and g["nodes"][at].get("type", "run") == "run" and not node_side(g["nodes"][at]):
        code, tail, _ = run_step(store, g, at, g["nodes"][at], st)
        if code != 0:
            return {"status": "rejected", "error": f"'{at}' still fails, so the problem is not fixed yet",
                    "output_tail": tail}
        witness = "passed"
    meta = {"rationale": patch["rationale"], "evidence": patch.get("evidence"), "run": st["run"],
            "trace": str(store.trace_path(st["run"])), "checks": checks, "witness": witness}
    if any(op["op"] not in ADDITIVE for op in ops):
        res = store.propose(ops, "learn", patch["rationale"], st["version"],
                            "changes the main path of the flow; verified, needs approval", meta)
    else:
        res = store.apply(ops, "learn", patch["rationale"], base=st["version"], meta=meta)
        if res["status"] == "applied":
            st["version"] = res["version"]  # this run authored the patch, so it continues on the new version
            store.save_run(st)
    return {**res, "checks": checks, "witness": witness, "next": f"flow resume {st['flow']} --run {st['run']} --done"}


def learn_replay(store, st, at, when, cmds, from_trace, match=None):
    """Fallback: learn the exact state-changing commands. Prefer --propose / --patch."""
    flow = store.load(st["version"])
    if at not in flow["nodes"] or flow["nodes"][at].get("type", "run") != "run":
        raise FlowError("--at must be an existing run node")
    cmds = list(cmds or [])
    if from_trace:
        trace = build_trace(store, st) or store.trace(st["run"])
        cmds += [e["cmd"] for e in trace if e.get("tool") == "Bash" and e.get("ok") and e.get("kind") == "action"]
    if any(has_secret(c) for c in cmds):
        return {"status": "rejected", "error": "a command contains what looks like a secret"}
    cmds = [c for i, c in enumerate(cmds) if not is_diagnostic(c) and not is_flow_call(c) and (i == 0 or c != cmds[i - 1])]
    if not cmds:
        return {"status": "error", "error": "no state-changing commands to learn"}
    op = {"op": "learn_path", "at": at, "when": when, "cmds": cmds, "at_side": node_side(flow["nodes"][at])}
    if match:
        op["match"] = match
    res = store.apply([op], "learn (replay)", when, base=st["version"])
    if res["status"] == "applied":
        st["version"] = res["version"]
        store.save_run(st)
    return res
