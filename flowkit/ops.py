"""Graph edit operations: the only way a flow changes.

An op is {"op": <name>, ...args}. `apply_op(graph, op)` returns (new_graph, note) or raises OpError when a
precondition fails. Ops never validate the whole graph; the store does that after applying a batch.

ADDITIVE ops only add (fresh ids are allocated at apply time), so they rebase onto a newer head without
conflict. Every other op edits something that already exists: if that thing changed since the op's base
version, the store queues the op as a proposal instead of applying it.
"""
import re

from .model import DEFAULT_SIDE, ID, clone, is_human_fail, recover_ids

ADDITIVE = {"add_node", "add_recover", "learn_path", "add_input"}
OPS = {"add_node", "update_node", "remove_node", "rename_node", "set_edge", "add_recover", "remove_recover",
       "set_recover_status", "promote_recover", "drop_recover", "learn_path", "add_input", "remove_input",
       "set_meta", "pin_flow", "replace_graph"}


class OpError(Exception):
    pass


class Merged(Exception):
    """The op duplicates something already in the graph; nothing to change."""


def touched(op):
    """What an op edits, for conflict checks: {("node", id) | ("input", name) | ("meta",) | ("graph",)}."""
    o = op["op"]
    if o == "replace_graph":
        return {("graph",)}
    if o in ("set_meta",):
        return {("meta",)}
    if o == "pin_flow":
        return {("uses", op["flow"])}
    if o in ("add_input", "remove_input"):
        return {("input", op["name"])}
    if o == "rename_node":
        return {("node", op["id"]), ("node", op["to"])}
    if o in ("add_recover", "remove_recover", "set_recover_status", "promote_recover", "drop_recover"):
        return {("node", op["node"])}
    if o == "learn_path":
        return {("node", op["at"])}
    return {("node", op["id"])}


def _need(g, nid):
    if nid not in g["nodes"]:
        raise OpError(f"no node '{nid}'")
    return g["nodes"][nid]


def _recover(g, nid, rid):
    for r in _need(g, nid).get("recover") or []:
        if r["id"] == rid:
            return r
    raise OpError(f"node '{nid}' has no recovery path '{rid}'")


def _refs(g, target):
    """[(node_id, kind)] of every reference to `target`."""
    out = []
    for k, n in g["nodes"].items():
        for f in ("ok", "next", "fail"):
            if n.get(f) == target:
                out.append((k, f))
        for o, t in (n.get("branches") or {}).items():
            if t == target:
                out.append((k, f"branch:{o}"))
        for r in n.get("recover") or []:
            if r.get("to") == target or r.get("gate") == target:
                out.append((k, f"recover:{r['id']}"))
    if g.get("start") == target:
        out.append(("<start>", "start"))
    return out


def _retarget(g, old, new):
    for n in g["nodes"].values():
        for f in ("ok", "next", "fail"):
            if n.get(f) == old:
                n[f] = new
        for o, t in list((n.get("branches") or {}).items()):
            if t == old:
                n["branches"][o] = new
        for r in n.get("recover") or []:
            if r.get("to") == old:
                r["to"] = new
            if r.get("gate") == old:
                r["gate"] = new
    if g.get("start") == old:
        g["start"] = new


def _fresh_rid(g):
    used = set(recover_ids(g)) | set(g["nodes"])
    i = 1
    while f"r{i}" in used:
        i += 1
    return f"r{i}"


def _uses_input(g, name):
    pat = re.compile(r"\{\{\s*inputs\." + re.escape(name) + r"\s*\}\}")
    return [k for k, n in g["nodes"].items() if any(pat.search(str(v)) for v in _strings(n))]


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def apply_op(graph, op):
    o = op.get("op")
    if o not in OPS:
        raise OpError(f"unknown op '{o}'; ops are {sorted(OPS)}")
    g = clone(graph)
    nodes = g["nodes"]
    note = ""

    if o == "add_node":
        if not ID.match(op["id"]):
            raise OpError(f"bad node id '{op['id']}'")
        if op["id"] in nodes:
            raise OpError(f"node '{op['id']}' already exists")
        nodes[op["id"]] = clone(op["node"])

    elif o == "update_node":
        n = _need(g, op["id"])
        for k, v in op["set"].items():
            if v is None:
                n.pop(k, None)
            else:
                n[k] = v

    elif o == "remove_node":
        _need(g, op["id"])
        refs = [r for r in _refs(g, op["id"]) if r[0] != op["id"]]
        if refs and not op.get("rewire"):
            raise OpError(f"'{op['id']}' is still referenced by {refs}; pass rewire=<node> or remove those edges")
        if op.get("rewire"):
            _need(g, op["rewire"])
            _retarget(g, op["id"], op["rewire"])
        del nodes[op["id"]]

    elif o == "rename_node":
        _need(g, op["id"])
        if op["to"] in nodes or not ID.match(op["to"]):
            raise OpError(f"cannot rename to '{op['to']}'")
        nodes[op["to"]] = nodes.pop(op["id"])
        _retarget(g, op["id"], op["to"])
        pat = re.compile(r"(\{\{\s*outputs\.)" + re.escape(op["id"]) + r"\.")
        for n in nodes.values():
            for k in ("cmd", "cwd", "say", "question"):
                if isinstance(n.get(k), str):
                    n[k] = pat.sub(r"\g<1>" + op["to"] + ".", n[k])

    elif o == "set_edge":
        n, kind, to = _need(g, op["id"]), op["kind"], op.get("to")
        if to is not None and not is_human_fail(to):
            _need(g, to)
        if kind in ("ok", "next", "fail"):
            if to is None:
                n.pop(kind, None)
            else:
                n[kind] = to
        elif kind.startswith("branch:"):
            opt = kind[7:]
            if opt not in (n.get("branches") or {}):
                raise OpError(f"'{op['id']}' has no branch '{opt}'")
            n["branches"][opt] = to
        else:
            raise OpError("kind must be ok | next | fail | branch:<option>")

    elif o == "add_recover":
        n = _need(g, op["node"])
        if n.get("type", "run") != "run":
            raise OpError("recovery paths attach to run nodes only")
        r = clone(op["entry"])
        r.setdefault("status", "provisional")
        if not r.get("id") or r["id"] in recover_ids(g):
            note = f"id '{r.get('id')}' taken; " if r.get("id") else ""
            r["id"] = _fresh_rid(g)
            note += f"allocated '{r['id']}'"
        _need(g, r["to"])
        n.setdefault("recover", []).append(r)

    elif o == "remove_recover":
        n = _need(g, op["node"])
        _recover(g, op["node"], op["rid"])
        n["recover"] = [r for r in n["recover"] if r["id"] != op["rid"]]

    elif o == "set_recover_status":
        _recover(g, op["node"], op["rid"])["status"] = op["status"]

    elif o == "promote_recover":
        r = _recover(g, op["node"], op["rid"])
        if r.get("status") != "provisional":
            raise Merged(f"'{op['rid']}' is already {r.get('status')}")
        r["status"] = "promoted"
        gate = r.pop("gate", None)
        if gate and gate in nodes:  # proven: drop the "unverified path" confirm in front of it
            r["to"] = nodes[gate]["next"]
            del nodes[gate]

    elif o == "drop_recover":
        r = _recover(g, op["node"], op["rid"])
        if r.get("status") == "dropped":
            raise Merged(f"'{op['rid']}' is already dropped")
        r["status"] = "dropped"
        r.pop("gate", None)
        for k in [k for k, n in nodes.items() if n.get("learned_from") == op["rid"]]:
            del nodes[k]

    elif o == "learn_path":
        at = _need(g, op["at"])
        if at.get("type", "run") != "run":
            raise OpError("--at must be a run node")
        cmds = list(op["cmds"])
        for r in at.get("recover") or []:
            if r.get("status") != "dropped" and r.get("cmds") == cmds:
                raise Merged(f"same commands as existing path '{r['id']}' on '{op['at']}'")
        rid = _fresh_rid(g)
        new, chain = {}, []
        gate = f"{rid}_gate"
        new[gate] = {"type": "confirm", "learned_from": rid,
                     "say": f"Learned path (unverified) for: {op['when']}. It will run: " + " ; ".join(cmds)
                            + " ; then retry the failed step."}
        chain.append(gate)
        for i, c in enumerate(cmds, 1):
            if DEFAULT_SIDE.search(c):
                new[f"{rid}_confirm{i}"] = {"type": "confirm", "learned_from": rid, "say": f"Run `{c}`?"}
                chain.append(f"{rid}_confirm{i}")
            new[f"{rid}_{i}"] = {"type": "run", "cmd": c, "learned_from": rid,
                                 "effect": "once" if DEFAULT_SIDE.search(c) else "idempotent"}
            chain.append(f"{rid}_{i}")
        if op.get("at_side"):
            new[f"{rid}_retry_confirm"] = {"type": "confirm", "learned_from": rid,
                                           "say": "Fix applied. Retry the failed step?"}
            chain.append(f"{rid}_retry_confirm")
        for i, k in enumerate(chain):
            nxt = chain[i + 1] if i + 1 < len(chain) else op["at"]  # rejoin by retrying the failed step
            new[k]["next" if new[k]["type"] == "confirm" else "ok"] = nxt
        nodes.update(new)
        entry = {"id": rid, "when": op["when"], "to": gate, "gate": gate, "status": "provisional", "cmds": cmds}
        if op.get("match"):
            entry["match"] = op["match"]
        at.setdefault("recover", []).append(entry)
        note = f"learned path '{rid}'"

    elif o == "add_input":
        g.setdefault("inputs", {})
        if op["name"] in g["inputs"]:
            raise OpError(f"input '{op['name']}' already exists")
        g["inputs"][op["name"]] = clone(op["spec"])

    elif o == "remove_input":
        users = _uses_input(g, op["name"])
        if users:
            raise OpError(f"input '{op['name']}' is used by {users}")
        (g.get("inputs") or {}).pop(op["name"], None)

    elif o == "set_meta":
        for k in ("about", "start", "outputs", "env", "env_passthrough", "secrets_from", "timeout"):
            if k in op:
                if op[k] is None:
                    g.pop(k, None)
                else:
                    g[k] = op[k]

    elif o == "pin_flow":  # move a called flow's pin (flow upgrade); the store re-validates against that version
        pin = (g.get("uses") or {}).get(op["flow"])
        if not pin:
            raise OpError(f"this flow does not call '{op['flow']}'")
        if not isinstance(op.get("version"), int) or op["version"] < 1:
            raise OpError("pin_flow needs an integer version")
        if pin["version"] == op["version"]:
            raise Merged(f"already pinned to v{op['version']}")
        g["uses"][op["flow"]] = {**pin, "version": op["version"]}

    elif o == "replace_graph":
        g = clone(op["graph"])

    return g, note

