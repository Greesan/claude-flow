"""Running a flow, one step at a time, pinned to the version the run started on.

When a run step exits non-zero, the next move is chosen in this order:
  1. a recovery path whose `match` regex matches the output (first in list order)
  2. the decision model (optional) over the untried recovery paths, acting only at >= DECISION_MODEL_THRESHOLD
  3. the node's `fail` edge (catch-all): a node, or "human: <hint>" to stop for you
  4. Claude (needs_claude when recovery paths exist, otherwise failed)

A run step with a `check` runs check -> command -> check: an already-true check skips the command, and a
command that exits 0 while its check still fails counts as a failure. Learned steps record whether they
were needed; a learned step that was never needed in PRUNE_AFTER uses is pruned from the flow.
"""
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from . import decide, secrets
from .model import FlowError, is_human_fail, render
from .store import project_dir, script_hashes
from .trace import scrub

MAX_STEPS, PROMOTE_AFTER, DROP_AFTER, PRUNE_AFTER, TAIL, DEFAULT_TIMEOUT = 50, 3, 2, 3, 1500, 600
# Every step gets these: no prompts that hang, no colour codes or pagers in captured output.
DEFAULT_ENV = {"CI": "1", "NO_COLOR": "1", "TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat",
               "GIT_TERMINAL_PROMPT": "0", "FLOW_RUN": "1"}
# Inherited variables that look like credentials are withheld unless a flow or step lists them in env_passthrough.
SECRETISH = re.compile(r"(?i)(token|secret|passw|api_?key|private_?key|credential)")


def ctx_for(st, last=""):
    return {"inputs": st["inputs"], "outputs": st["outputs"], "last_output": last.strip(),
            "run": {"id": st["run"], "dir": str(project_dir()), "flow": st["flow"]}}


def step_env(flow, n, ctx, st, node_id, out_file, secret_values):
    keep = set(flow.get("env_passthrough") or []) | set(n.get("env_passthrough") or [])
    env = {k: v for k, v in os.environ.items() if k in keep or not SECRETISH.search(k)}
    env.update(DEFAULT_ENV)
    env.update(FLOW_RUN_ID=st["run"], FLOW_NAME=st["flow"], FLOW_NODE=node_id, FLOW_OUTPUT=out_file)
    for scope in (flow.get("env") or {}, n.get("env") or {}):
        env.update({k: render(str(v), ctx) for k, v in scope.items()})
    env.update(secret_values)  # only the secrets this step declared
    return env


def run_step(store, flow, node_id, n, st):
    """(exit_code, output_tail, outputs). Secret values never leave this function unmasked."""
    ctx = ctx_for(st, st.get("last_out", ""))
    cwd_s = render(n.get("cwd") or "", ctx)
    cwd = project_dir() / Path(cwd_s).expanduser() if cwd_s else project_dir()
    try:
        svals = secrets.resolve(n.get("secrets") or [], flow, cwd)
    except secrets.SecretError as e:
        return "secret", str(e), {}
    fd, out_file = tempfile.mkstemp(prefix="flow-out-")
    os.close(fd)
    if n.get("script"):
        argv = ["bash", "-o", "pipefail", str(store.dir / "steps" / n["script"]), *[render(a, ctx) for a in n.get("args", [])]]
    else:
        argv = ["bash", "-o", "pipefail", "-c", render(n["cmd"], ctx, quote=True)]
    timeout = n.get("timeout") or flow.get("timeout") or DEFAULT_TIMEOUT
    try:
        r = subprocess.run(argv, cwd=cwd, env=step_env(flow, n, ctx, st, node_id, out_file, svals),
                           capture_output=True, text=True, timeout=timeout)
        code, text = r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        part = (e.stdout or b"") + (e.stderr or b"")
        code = 124
        text = (part.decode(errors="replace") if isinstance(part, bytes) else part) + f"\n[flow] timed out after {timeout}s"
    except OSError as e:
        code, text = 127, f"[flow] could not start step: {e}"
    text = secrets.mask(scrub(text), svals)  # scrub first: its patterns would mangle the <secret:NAME> markers
    outputs = {}
    for line in Path(out_file).read_text(errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            outputs[k.strip()] = secrets.mask(scrub(v.strip()), svals)
    os.unlink(out_file)
    for k, spec in (n.get("outputs") or {}).items():
        m = re.search(spec["regex"], text, re.M)
        if m:
            outputs[k] = m.group(1) if m.groups() else m.group(0)
    return code, text[-TAIL:], outputs


def run_check(store, flow, node_id, n, st):
    """Exit code of a node's `check`, run with the node's cwd, env and secrets."""
    probe = {k: n[k] for k in ("cwd", "env", "env_passthrough", "secrets") if k in n}
    probe.update(cmd=n["check"], timeout=min(n.get("timeout") or 60, 60))
    code, tail, _ = run_step(store, flow, node_id, probe, st)
    return code, tail


def _summary(log):
    return [{k: v for k, v in e.items() if k != "tail"} for e in log]


def pause(store, st, status, pending, **extra):
    flag = {"needs_confirm": "--approve", "needs_human": "--done", "failed": "--done (retries the step)",
            "needs_claude": "--choose <option>"}[status]
    res = {"status": status, "run": st["run"], "node": st["current"], **extra,
           "resume": f"flow resume {st['flow']} --run {st['run']} {flag}   (or --abort)", "steps": _summary(st["log"])}
    st.update(status="paused", wait=status, pending=pending, paused_at=time.time(), result=res)
    store.save_run(st)  # the result is kept so a detached run can be read back with `flow status --wait`
    return res


def finish(store, st, status, **extra):
    res = {"status": status, "run": st["run"], **extra, "outputs": st["outputs"], "steps": _summary(st["log"])}
    st.update(status=status, finished=time.time(), result=res)
    st.pop("pending", None)
    store.save_run(st)
    return res


def _at_node(flow, rid):
    for k, n in flow["nodes"].items():
        if any(r["id"] == rid for r in n.get("recover") or []):
            return k
    return None


def record_outcome(store, st, at, rid, ok, by, decision=None):
    """Evidence for one use of a recovery path; promotes or drops provisional paths at the head."""
    store.event(type="recover_outcome", run=st["run"], version=st["version"], node=at, rid=rid, ok=ok, by=by,
                decision=decision)
    if decision:
        store.event(type="decision_label", decision=decision, label=rid, correct=ok)
    good, bad = store.recover_stats().get(rid, (0, 0))
    head = store.load()
    hn = _at_node(head, rid)
    entry = next((r for r in head["nodes"][hn].get("recover") or [] if r["id"] == rid), None) if hn else None
    if not entry or entry.get("status") != "provisional":
        return
    if good >= PROMOTE_AFTER:
        store.apply([{"op": "promote_recover", "node": hn, "rid": rid}], "evidence", f"{good} proven fixes")
    elif bad >= DROP_AFTER:
        store.apply([{"op": "drop_recover", "node": hn, "rid": rid}], "evidence", f"{bad} failures")


def record_step(store, st, node_id, n, needed, worked):
    """Evidence for one use of a learned step; prunes it from the head when it is never needed."""
    store.event(type="step_outcome", run=st["run"], node=node_id, rid=n["learned_from"], needed=needed, worked=worked)
    uses = [e for e in store.events() if e.get("type") == "step_outcome" and e["node"] == node_id]
    if len(uses) >= PRUNE_AFTER and not any(e["needed"] for e in uses):
        head = store.load()
        hn = head["nodes"].get(node_id)
        if hn and hn.get("learned_from") == n["learned_from"]:
            store.apply([{"op": "remove_node", "id": node_id, "rewire": hn["ok"]}], "evidence",
                        f"learned step '{node_id}' was already satisfied in all {len(uses)} uses")


def run(store, st):
    flow = store.load(st["version"])  # pinned: edits to the head never change a run in flight
    nodes, log = flow["nodes"], st["log"]
    st["status"] = "running"
    for _ in range(MAX_STEPS):
        cur = st["current"]
        n = nodes.get(cur)
        if n is None:
            return finish(store, st, "error", error=f"unknown node {cur}")
        t = n.get("type", "run")
        if t == "end":
            return finish(store, st, "done", say=render(n.get("say", ""), ctx_for(st, st.get("last_out", ""))))
        if t == "run":
            t0 = time.time()
            if n.get("script") and not st.setdefault("script_ok", {}).get(cur):
                pinned = (flow.get("script_hashes") or {}).get(n["script"])
                now = script_hashes({"nodes": {cur: n}}, store.dir).get(n["script"])
                if pinned and now != pinned:
                    return pause(store, st, "needs_human", {"type": "script_ok", "node": cur},
                                 say=f"steps/{n['script']} changed since v{st['version']} (the version this run is pinned "
                                     f"to). Review it, then `flow commit {st['flow']}` for future runs; resume --done to run "
                                     f"the current script in this run anyway.")
            if n.get("check"):
                pre, _ = run_check(store, flow, cur, n, st)
                if pre == 0:  # already true: nothing to do
                    log.append({"node": cur, "skipped": "check already passes"})
                    if n.get("learned_from"):
                        record_step(store, st, cur, n, needed=False, worked=True)
                    st["current"] = n["ok"]
                    continue
            code, tail, outs = run_step(store, flow, cur, n, st)
            if code == "secret":
                return pause(store, st, "needs_human", {"type": "retry", "node": cur}, say=tail)
            if code == 0 and n.get("check"):
                post, ctail = run_check(store, flow, cur, n, st)
                if post != 0:
                    code, tail = 1, (tail + f"\n[flow] the step exited 0 but its check still fails: {n['check']}\n"
                                     + ctail)[-TAIL:]
            if n.get("check") and n.get("learned_from"):
                record_step(store, st, cur, n, needed=True, worked=code == 0)
            if outs:
                st["outputs"][cur] = {**st["outputs"].get(cur, {}), **outs}
            st["last_out"] = tail
            log.append({"node": cur, "exit": code, "s": round(time.time() - t0, 2), "tail": tail[-300:]})
            prob = st["probation"].pop(cur, None)
            if prob:  # a recovery path ran and this retry is its proof
                record_outcome(store, st, cur, prob["rid"], code == 0, prob["by"], prob.get("decision"))
            if code == 0:
                st["current"] = n["ok"]
                continue
            if n.get("learned_from"):  # a command inside a learned path failed: evidence against that path
                at = _at_node(flow, n["learned_from"])
                if at:
                    record_outcome(store, st, at, n["learned_from"], False, "path")
            tried = st["tried"].setdefault(cur, [])
            rec = [r for r in n.get("recover") or [] if r.get("status") != "dropped" and r["id"] not in tried]
            pick, how, did = None, None, None
            for r in rec:
                if r.get("match") and re.search(r["match"], tail):
                    pick, how = r, "match"
                    break
            if not pick and rec:
                shown = n.get("cmd") or f"script {n.get('script')}"
                did, choice, conf = decide.ask(store, cur, "Which recovery path fits this failure?",
                                               {r["id"]: r["when"] for r in rec},
                                               {"command": render(shown, ctx_for(st)), "error_tail": decide.clean_tail(tail)})
                if choice:
                    pick, how = next(r for r in rec if r["id"] == choice), "model"
                    log.append({"node": cur, "model_conf": conf})
            if pick:
                log.append({"node": cur, "recover": pick["id"], "by": how})
                tried.append(pick["id"])
                st["probation"][cur] = {"rid": pick["id"], "by": how, "decision": did}
                st["current"] = pick["to"]
                continue
            f = n.get("fail")
            if f and not is_human_fail(f):
                log.append({"node": cur, "fail_edge": f})
                st["current"] = f
                continue
            if is_human_fail(f):
                return pause(store, st, "needs_human", {"type": "retry", "node": cur}, say=f[6:].strip(), output_tail=tail)
            if rec:
                return pause(store, st, "needs_claude", {"type": "recover", "node": cur, "decision": did},
                             output_tail=tail, options=[r["id"] for r in rec] + ["none"],
                             known_paths={r["id"]: r["when"] for r in rec},
                             hint="pick a path, or `none` to handle it yourself and then `flow learn --propose`")
            return pause(store, st, "failed", {"type": "retry", "node": cur}, exit=code, output_tail=tail,
                         hint=f"fix it, then `flow learn {st['flow']} --run {st['run']} --propose` to turn what you did "
                              f"into a verified patch, or resume --done to just retry")
        elif t == "decide":
            choice = None
            if n.get("check"):
                code, _ = run_check(store, flow, cur, n, st)
                choice = n["options"][0] if code == 0 else n["options"][1]
                log.append({"node": cur, "by": "check", "choice": choice})
            elif n.get("jev"):
                did, c, conf = decide.ask(store, cur, n["question"], n.get("criteria") or {o: o for o in n["options"]},
                                          {"context": decide.clean_tail(st.get("last_out", ""))})
                if c:
                    choice = c
                    log.append({"node": cur, "by": "model", "choice": c, "conf": conf})
            if choice is None:
                return pause(store, st, "needs_claude", {"type": "decide", "node": cur}, question=n["question"],
                             options=n["options"])
            st["current"] = n["branches"][choice]
        elif t in ("confirm", "human"):
            return pause(store, st, "needs_confirm" if t == "confirm" else "needs_human", {"type": t, "node": cur},
                         say=render(n["say"], ctx_for(st, st.get("last_out", ""))))
    return finish(store, st, "error", error="step limit hit (loop?)")


def resume(store, st, flags):
    p = st.get("pending") or {}
    flow = store.load(st["version"])
    n = flow["nodes"][st["current"]]
    if "--abort" in flags:
        return finish(store, st, "aborted")
    choose = flags[flags.index("--choose") + 1] if "--choose" in flags and flags.index("--choose") + 1 < len(flags) else None
    kind = p.get("type")
    if kind == "confirm":
        if "--approve" not in flags:
            raise FlowError("this step needs --approve")
        st["current"] = n["next"]
    elif kind == "human":
        if "--done" not in flags:
            raise FlowError("this step needs --done")
        st["current"] = n["next"]
    elif kind == "retry":
        if "--done" not in flags:
            raise FlowError("resume with --done to retry the step")
    elif kind == "script_ok":
        if "--done" not in flags:
            raise FlowError("resume with --done to run the changed script")
        st.setdefault("script_ok", {})[st["current"]] = True
    elif kind == "decide":
        if choose not in n["options"]:
            raise FlowError(f"--choose one of {n['options']}")
        st["log"].append({"node": st["current"], "by": "claude", "choice": choose})
        st["current"] = n["branches"][choose]
    elif kind == "recover":
        rec = {r["id"]: r for r in n.get("recover") or []}
        if choose not in list(rec) + ["none"]:
            raise FlowError(f"--choose one of {list(rec) + ['none']}")
        if p.get("decision"):  # the decision model abstained; Claude's pick labels that decision
            store.event(type="decision_label", decision=p["decision"], label=choose, by="claude")
        if choose == "none":
            st["pending"] = {"type": "retry", "node": st["current"]}
            st["wait"] = "failed"
            store.save_run(st)
            return {"status": "failed", "run": st["run"], "hint": f"handle it, then `flow learn {st['flow']} --run "
                    f"{st['run']} --propose`, or resume --done to retry"}
        st["tried"].setdefault(st["current"], []).append(choose)
        st["probation"][st["current"]] = {"rid": choose, "by": "claude", "decision": p.get("decision")}
        st["current"] = rec[choose]["to"]
    else:
        raise FlowError("this run is not paused")
    st.pop("pending", None)
    return run(store, st)
