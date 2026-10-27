"""Flow definitions: YAML I/O, input checking, templating and static validation."""
import copy
import re
import shlex
from pathlib import Path

import yaml

NODE_TYPES = {"run", "decide", "confirm", "human", "end", "flow"}
INPUT_TYPES = {"string", "enum", "int", "bool", "path"}
EFFECTS = {"read", "idempotent", "once"}
STATUSES = {"provisional", "promoted", "dropped"}
ID = re.compile(r"^[A-Za-z_][\w-]*$")
SECRET_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
TEMPLATE = re.compile(r"\{\{\s*([A-Za-z_][\w.-]*)\s*\}\}")
DEFAULT_SIDE = re.compile(r"\b(deploy|publish|push|release|delete|rm|drop|secret put|apply|migrate)\b")

KEYS = {
    "run": {"type", "cmd", "script", "args", "cwd", "env", "env_passthrough", "secrets", "timeout", "effect",
            "outputs", "ok", "fail", "recover", "learned_from", "note", "check"},
    "decide": {"type", "question", "options", "criteria", "branches", "check", "script", "args", "by", "cwd", "env",
               "env_passthrough", "secrets", "timeout", "jev", "outputs", "learned_from", "note"},
    "confirm": {"type", "say", "next", "learned_from", "note"},
    "human": {"type", "say", "next", "learned_from", "note"},
    "end": {"type", "say", "note"},
    "flow": {"type", "flow", "inputs", "ok", "fail", "note", "learned_from"},
}
MAX_CALL_DEPTH = 4
TOP_KEYS = {"name", "version", "about", "inputs", "outputs", "uses", "env", "env_passthrough", "secrets_from", "timeout", "start", "nodes", "script_hashes"}
OUTPUT_KEYS = {"help", "regex"}
INPUT_KEYS = {"type", "default", "required", "help", "values", "pattern"}
RECOVER_KEYS = {"id", "when", "match", "to", "status", "gate", "cmds", "learned_at"}


class FlowError(Exception):
    pass


# ---------- YAML ----------
class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper, s):
    return dumper.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None)


_Dumper.add_representer(str, _str)


def load_yaml(text):
    g = yaml.safe_load(text)
    if not isinstance(g, dict):
        raise FlowError("flow file is not a YAML mapping")
    return g


def dump_yaml(graph):
    return yaml.dump(graph, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=120)


def clone(g):
    return copy.deepcopy(g)


# ---------- inputs ----------
def resolve_inputs(flow, given):
    """Check and coerce run inputs against the flow's `inputs:` spec. Returns {name: value}."""
    spec = flow.get("inputs") or {}
    unknown = sorted(set(given) - set(spec))
    if unknown:
        raise FlowError(f"unknown input(s) {unknown}; this flow takes {sorted(spec) or 'none'}")
    out = {}
    for name, s in spec.items():
        v = given.get(name, s.get("default"))
        if v is None:
            if s.get("required", True):
                raise FlowError(f"missing required input '{name}'" + (f": {s['help']}" if s.get("help") else ""))
            continue
        t = s.get("type", "string")
        try:
            if t == "int":
                v = int(v)
            elif t == "bool":
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "y", "on")
            elif t == "path":
                v = str(Path(str(v)).expanduser())
            else:
                v = str(v)
        except ValueError:
            raise FlowError(f"input '{name}' must be {t}, got {v!r}")
        if t == "enum" and v not in s.get("values", []):
            raise FlowError(f"input '{name}' must be one of {s.get('values')}, got {v!r}")
        if s.get("pattern") and not re.fullmatch(s["pattern"], str(v)):
            raise FlowError(f"input '{name}' does not match {s['pattern']!r}")
        out[name] = v
    return out


# ---------- templating ----------
def render(text, ctx, quote=False):
    """Fill {{ inputs.x }}, {{ outputs.node.key }}, {{ run.id }}, {{ last_output }}.

    quote=True shell-quotes each value: use it for `cmd`, where inputs must never become shell syntax.
    """
    if not isinstance(text, str):
        return text

    def sub(m):
        v = ctx
        for part in m.group(1).split("."):
            if not isinstance(v, dict) or part not in v:
                raise FlowError(f"template {{{{ {m.group(1)} }}}} has no value here")
            v = v[part]
        s = ("true" if v else "false") if isinstance(v, bool) else str(v)
        return shlex.quote(s) if quote else s

    return TEMPLATE.sub(sub, text)


def template_refs(node):
    """Every {{ ref }} used by a node's strings."""
    strs = []
    for k in ("cmd", "cwd", "say", "question", "check"):
        if isinstance(node.get(k), str):
            strs.append(node[k])
    strs += [a for a in node.get("args", []) if isinstance(a, str)]
    strs += [v for v in (node.get("env") or {}).values() if isinstance(v, str)]
    if node.get("type") == "flow":
        strs += [v for v in (node.get("inputs") or {}).values() if isinstance(v, str)]
    return [m.group(1) for s in strs for m in TEMPLATE.finditer(s)]


# ---------- outputs ----------
def produces(n, child=None):
    """The output keys a step promises: its declared `outputs`, `choice` for a decide step, and for a flow step the
    called flow's declared `outputs` (child = that flow's graph; unknown without a resolver)."""
    if n.get("type") == "flow":
        return set((child or {}).get("outputs") or {})
    return set(n.get("outputs") or {}) | ({"choice"} if n.get("type") == "decide" else set())


def called(g):
    """Names of the flows this flow's `flow` steps call."""
    return sorted({n["flow"] for n in (g.get("nodes") or {}).values()
                   if isinstance(n, dict) and n.get("type") == "flow" and isinstance(n.get("flow"), str)})


def _output_errors(k, n):
    errs = []
    for o, spec in (n.get("outputs") or {}).items():
        if not ID.match(o) or o == "choice":
            errs.append(f"{k}: output '{o}': bad name")
        if not isinstance(spec, dict) or set(spec) - OUTPUT_KEYS or not spec.get("help"):
            errs.append(f"{k}: output '{o}' needs 'help' (and optionally 'regex'; without one the step writes "
                        f"{o}=<value> to $FLOW_OUTPUT)")
            continue
        if "regex" in spec:
            try:
                re.compile(spec["regex"])
            except (TypeError, re.error):
                errs.append(f"{k}: output '{o}': bad regex")
    return errs


def _flow_step_errors(g, k, n, resolve, children):
    errs = []
    name = n.get("flow")
    if not isinstance(name, str) or not ID.match(name):
        return [f"{k}: flow step needs 'flow: <name>'"]
    if name == g.get("name"):
        return [f"{k}: a flow cannot call itself"]
    if not isinstance(n.get("inputs") or {}, dict):
        return [f"{k}: inputs must be a mapping of the called flow's inputs"]
    if resolve is None:
        return errs
    pin = (g.get("uses") or {}).get(name) or {}
    try:
        child, _ = resolve(name, pin.get("version"))
    except FlowError as e:
        return [f"{k}: {e}"]
    children[k] = child
    spec = child.get("inputs") or {}
    given = n.get("inputs") or {}
    errs += [f"{k}: {name} has no input '{x}' (it takes {sorted(spec) or 'none'})" for x in sorted(set(given) - set(spec))]
    errs += [f"{k}: {name} needs input '{x}' ({spec[x].get('help', '')})" for x in sorted(spec)
             if x not in given and spec[x].get("default") is None and spec[x].get("required", True)]
    # no call cycles: follow what the child calls, at its pinned versions
    stack, depth = [(name, child, 1)], 0
    while stack:
        cname, cg, depth = stack.pop()
        if depth > MAX_CALL_DEPTH:
            return errs + [f"{k}: flows nest deeper than {MAX_CALL_DEPTH} calls"]
        for sub in called(cg):
            if sub == g.get("name"):
                return errs + [f"{k}: call cycle {g.get('name')} -> {name} -> ... -> {cname} -> {sub}"]
            try:
                stack.append((sub, resolve(sub, ((cg.get("uses") or {}).get(sub) or {}).get("version"))[0], depth + 1))
            except FlowError as e:
                errs.append(f"{k}: {cname} calls {sub}: {e}")
    return errs


def flow_outputs(flow, step_outputs):
    """The flow's declared results, {name: value or None when the path taken never produced it}."""
    return {name: (step_outputs.get(spec["from"].split(".", 1)[0]) or {}).get(spec["from"].split(".", 1)[1])
            for name, spec in (flow.get("outputs") or {}).items()}


# ---------- graph helpers ----------
def is_human_fail(v):
    return isinstance(v, str) and v.startswith("human:")


def edges(n):
    """(kind, target) for every edge leaving a node; dropped recovery paths excluded."""
    e = [(k, n[k]) for k in ("ok", "next") if isinstance(n.get(k), str)]
    if isinstance(n.get("fail"), str) and not is_human_fail(n["fail"]):
        e.append(("fail", n["fail"]))
    e += [(f"branch:{o}", t) for o, t in (n.get("branches") or {}).items()]
    e += [(f"recover:{r['id']}", r["to"]) for r in n.get("recover") or [] if r.get("status") != "dropped"]
    return e


def node_side(n):
    """Does this step change something that must not be repeated without asking? `effect: once` says so;
    validate() requires every run node to declare its effect, and an undeclared one counts as `once`."""
    return n.get("effect") not in ("read", "idempotent")


def recover_ids(g):
    return [r["id"] for n in g["nodes"].values() for r in n.get("recover") or []]


# ---------- validation ----------
def validate(g, flow_dir=None, resolve=None):
    """Static checks. Returns a list of problems (empty = valid).

    resolve(name, version) -> (graph, scope) loads a called flow (raising FlowError when it is missing or ambiguous);
    without it, `flow` steps are checked only for shape."""
    errs = []
    children = {}  # node id -> the called flow's graph, when it could be loaded
    for k in set(g) - TOP_KEYS:
        errs.append(f"unknown top-level key '{k}'")
    nodes = g.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return errs + ["'nodes' must be a non-empty mapping"]
    if g.get("start") not in nodes:
        errs.append(f"start node '{g.get('start')}' does not exist")
    inputs = g.get("inputs") or {}
    for name, s in inputs.items():
        if not ID.match(name):
            errs.append(f"input '{name}': bad name")
        if not isinstance(s, dict) or s.get("type", "string") not in INPUT_TYPES:
            errs.append(f"input '{name}': type must be one of {sorted(INPUT_TYPES)}")
        elif s.get("type") == "enum" and not s.get("values"):
            errs.append(f"input '{name}': enum needs 'values'")
        if isinstance(s, dict):
            errs += [f"input '{name}': unknown key '{x}'" for x in sorted(set(s) - INPUT_KEYS)]
            if not s.get("help"):
                errs.append(f"input '{name}': add a 'help' line saying what it is")
    for name in g.get("secrets_from", {}) or {}:
        if name != "doppler":
            errs.append(f"secrets_from '{name}': only 'doppler' is supported")

    seen_rid = set()
    for k, n in nodes.items():
        if not ID.match(k):
            errs.append(f"{k}: bad node id")
        if not isinstance(n, dict):
            errs.append(f"{k}: node must be a mapping")
            continue
        t = n.get("type", "run")
        if t not in NODE_TYPES:
            errs.append(f"{k}: unknown type '{t}'")
            continue
        for extra in set(n) - KEYS[t]:
            errs.append(f"{k}: unknown key '{extra}' for a {t} node")
        if t == "flow":
            errs += _flow_step_errors(g, k, n, resolve, children)
        if t == "run":
            if bool(n.get("cmd")) == bool(n.get("script")):
                errs.append(f"{k}: a run node needs exactly one of 'cmd' or 'script'")
            if not n.get("effect"):
                hint = "once" if DEFAULT_SIDE.search(n.get("cmd") or "") else "read | idempotent | once"
                errs.append(f"{k}: declare 'effect' ({hint}): read = only looks, idempotent = safe to repeat, "
                            f"once = changes something shared and must sit behind a confirm")
            if n.get("script"):
                if flow_dir and not (Path(flow_dir) / "steps" / n["script"]).is_file():
                    errs.append(f"{k}: script steps/{n['script']} not found")
            if "check" in n and not (isinstance(n["check"], str) and n["check"].strip()):
                errs.append(f"{k}: check must be a shell command (exit 0 = the step's effect is in place)")
            if n.get("effect") and n["effect"] not in EFFECTS:
                errs.append(f"{k}: effect must be one of {sorted(EFFECTS)}")
            if "timeout" in n and (not isinstance(n["timeout"], int) or n["timeout"] <= 0):
                errs.append(f"{k}: timeout must be a positive integer (seconds)")
            if not n.get("ok"):
                errs.append(f"{k}: run node needs an 'ok' edge")
            elif n.get("fail") == n["ok"]:
                errs.append(f"{k}: 'ok' and 'fail' both go to {n['ok']}, so the result is ignored; give the failure its "
                            f"own step, or end the command with `|| true` if it really doesn't matter")
            for s in n.get("secrets") or []:
                if not SECRET_NAME.match(s):
                    errs.append(f"{k}: secret name '{s}' must be UPPER_SNAKE")
            for r in n.get("recover") or []:
                miss = {"id", "when", "to"} - set(r)
                if miss:
                    errs.append(f"{k}: recover entry missing {sorted(miss)}")
                    continue
                for extra in set(r) - RECOVER_KEYS:
                    errs.append(f"{k}: recover '{r['id']}': unknown key '{extra}'")
                if r["id"] in seen_rid:
                    errs.append(f"{k}: recover id '{r['id']}' is used twice in this flow")
                seen_rid.add(r["id"])
                if r.get("status", "promoted") not in STATUSES:
                    errs.append(f"{k}: recover '{r['id']}': bad status")
                if r.get("match"):
                    try:
                        re.compile(r["match"])
                    except re.error as e:
                        errs.append(f"{k}: recover '{r['id']}': bad regex ({e})")
        elif t == "decide":
            opts = n.get("options") or []
            if len(opts) < 2 or set(n.get("branches") or {}) != set(opts):
                errs.append(f"{k}: decide needs >= 2 options and one branch per option")
            if n.get("criteria") and set(n["criteria"]) != set(opts):
                errs.append(f"{k}: criteria keys must equal options")
            if n.get("check") and n.get("script"):
                errs.append(f"{k}: a decide step takes a `check` command or a `script`, not both")
            if n.get("by", "exit") not in ("exit", "output"):
                errs.append(f"{k}: by must be `exit` (exit 0 = first option) or `output` (the check prints the option)")
            if (n.get("check") or n.get("script")) and len(opts) > 2 and n.get("by") != "output":
                errs.append(f"{k}: more than two options needs `by: output` (the check prints which option to take)")
            if n.get("script") and flow_dir and not (Path(flow_dir) / "steps" / n["script"]).is_file():
                errs.append(f"{k}: script steps/{n['script']} not found")
        elif t in ("confirm", "human"):
            if not n.get("say") or not n.get("next"):
                errs.append(f"{k}: {t} node needs 'say' and 'next'")
        if t in ("run", "decide"):
            errs += _output_errors(k, n)
        for kind, tgt in edges(n):
            if tgt not in nodes:
                errs.append(f"{k} -{kind}-> {tgt}: no such node")
        for ref in template_refs(n):
            parts = ref.split(".")
            if parts[0] == "inputs" and (len(parts) != 2 or parts[1] not in inputs):
                errs.append(f"{k}: template '{ref}' names an undeclared input")
            elif parts[0] == "outputs" and (len(parts) != 3 or parts[1] not in nodes):
                errs.append(f"{k}: template '{ref}' must be outputs.<node>.<key>")
            elif parts[0] == "outputs" and (nodes[parts[1]].get("type") != "flow" or parts[1] in children) \
                    and parts[2] not in produces(nodes[parts[1]], children.get(parts[1])):
                errs.append(f"{k}: template '{ref}': step '{parts[1]}' does not declare output '{parts[2]}'")
            elif parts[0] == "run" and (len(parts) != 2 or parts[1] not in ("id", "dir", "flow")):
                errs.append(f"{k}: template '{ref}' must be run.id, run.dir or run.flow")
            elif parts[0] not in ("inputs", "outputs", "run", "last_output"):
                errs.append(f"{k}: unknown template '{ref}'")
        if t == "flow" and not n.get("ok"):
            errs.append(f"{k}: flow step needs an 'ok' edge")
        if t != "end" and not edges(n) and not (t == "run" and is_human_fail(n.get("fail"))):
            errs.append(f"{k}: no way out")
    for name, spec in (g.get("outputs") or {}).items():
        src = spec.get("from", "") if isinstance(spec, dict) else ""
        node, _, key = src.partition(".")
        if not ID.match(name):
            errs.append(f"output '{name}': bad name")
        if not isinstance(spec, dict) or set(spec) - {"from", "help"} or not spec.get("help"):
            errs.append(f"output '{name}': needs 'from: <step>.<key>' and 'help'")
        elif node not in nodes or ((nodes[node].get("type") != "flow" or node in children)
                                   and key not in produces(nodes[node], children.get(node))):
            errs.append(f"output '{name}': from '{src}' is not an output a step declares")
    if resolve or g.get("uses"):
        if sorted(g.get("uses") or {}) != called(g):
            errs.append(f"uses must pin exactly the flows called ({called(g)}), found {sorted(g.get('uses') or {})}")
    if errs:
        return errs

    # reachability, end, success-path cycles, confirm-before-side-effect
    seen, stack = set(), [g["start"]]
    while stack:
        k = stack.pop()
        if k not in seen:
            seen.add(k)
            stack += [t for _, t in edges(nodes[k])]
    errs += [f"{k}: unreachable" for k in nodes if k not in seen]
    if not any(nodes[k].get("type") == "end" for k in seen):
        errs.append("no reachable end node")
    color = {}

    def dfs(k):
        color[k] = 1
        n = nodes[k]
        # a loop back through a `human` step (fix it, then check again) is fine: a person decides each pass
        nxt = [] if n.get("type") == "human" else [n[x] for x in ("ok", "next") if isinstance(n.get(x), str)]
        for t in nxt + list((n.get("branches") or {}).values()):
            if color.get(t) == 1:
                errs.append(f"cycle on the success path via {k} -> {t}")
            elif t not in color:
                dfs(t)
        color[k] = 2

    dfs(g["start"])
    incoming = {}
    for k, n in nodes.items():
        for _, t in edges(n):
            incoming.setdefault(t, []).append(k)
    for k, n in nodes.items():
        if n.get("type", "run") == "run" and node_side(n):
            srcs = incoming.get(k, [])
            if not srcs or any(nodes[s].get("type") != "confirm" for s in srcs):
                errs.append(f"{k}: an effect: once step must be entered only from a confirm node "
                            f"(or declare effect: idempotent if repeating it is harmless)")
    return errs
