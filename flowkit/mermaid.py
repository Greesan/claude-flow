"""Mermaid flowchart export. Recovery paths are labelled with their status and evidence (from events)."""
import re

SHAPE = {"flow": ('[["', '"]]'), "run": ('["', '"]'), "confirm": ('{{"', '"}}'), "human": ('(["', '"])'), "decide": ('{"', '"}'),
         "end": ('(("', '"))')}


def _t(text, n=48):
    t = re.sub(r"\{\{\s*([\w.-]+)\s*\}\}", r"⟨\1⟩", re.sub(r"\s+", " ", str(text)).strip())  # `{{` is Mermaid syntax
    t = (t[: n - 1] + "…") if len(t) > n else t
    return t.replace('"', "'").replace("<", "‹").replace(">", "›").replace("|", "/").replace("`", "'")


def _success_path(flow):
    """Steps reachable from the start without taking a failure (`fail` edges and recovery paths)."""
    nodes, seen, todo = flow["nodes"], set(), [flow.get("start")]
    while todo:
        k = todo.pop()
        if k in seen or k not in nodes:
            continue
        seen.add(k)
        n = nodes[k]
        todo += [n[x] for x in ("ok", "next") if isinstance(n.get(x), str)] + list((n.get("branches") or {}).values())
    return seen


def to_mermaid(flow, stats=None, current=None, timing=None, child_heads=None):
    """child_heads = {called flow: its head version}, to mark pins that are behind."""
    stats = stats or {}
    nodes = flow["nodes"]
    status = {r["id"]: r.get("status", "promoted") for n in nodes.values() for r in n.get("recover") or []}
    L = ["flowchart TD"]
    for k, n in nodes.items():
        t = n.get("type", "run")
        o, c = SHAPE.get(t, SHAPE["run"])
        pin = (flow.get("uses") or {}).get(n.get("flow"), {})
        body = {"flow": f"flow: {n.get('flow')} v{pin.get('version', '?')}",
                "run": n.get("cmd") or f"script: {n.get('script', '')}", "confirm": n.get("say", ""),
                "human": n.get("say", ""), "decide": _t(n.get("question", "")), "end": n.get("say") or "end"}[t]
        extra = []
        if t == "decide":  # how the question gets answered: the diagnostic command, or a model
            how = ("check: " + _t(n["check"], 44) if n.get("check") else "script: " + n["script"] if n.get("script")
                   else "decided by the decision model" if n.get("jev") else "decided by Claude")
            body += "<br/>" + how + ("<br/><i>(prints the answer)</i>" if n.get("by") == "output" else "")
        if t == "flow":
            head = (child_heads or {}).get(n["flow"])
            if head and pin and head > pin.get("version", 0):
                extra.append(f"v{head} available")
            if n.get("inputs"):
                extra.append("with " + ", ".join(f"{k}={v}" for k, v in n["inputs"].items()))
        if t in ("run", "decide") and n.get("outputs"):
            extra.append("reports: " + ", ".join(n["outputs"]))
        if t == "run":
            tm = ((timing or {}).get("steps") or {}).get(k)
            if tm:
                extra.append(f"avg {tm['mean']}s, p90 {tm['p90']}s ({tm['runs']} runs)")
            if n.get("check"):
                extra.append("✓ check: " + n["check"])
            if n.get("secrets"):
                extra.append("secrets: " + ", ".join(n["secrets"]))
            if n.get("timeout"):
                extra.append(f"timeout {n['timeout']}s")
        L.append(f'  {k}{o}{k}<br/>{body if t == "decide" else _t(body)}{"<br/><i>" + _t("; ".join(extra), 60) + "</i>" if extra else ""}{c}')
    for k, n in nodes.items():
        t = n.get("type", "run")
        if t == "flow":
            L.append(f"  {k} -->|ok| {n['ok']}")
            if n.get("fail"):
                L.append(f"  {k} -.->|fails| {n['fail']}")
            L.append(f"  %% link {k} -> {n['flow']}")  # read by chart pages to jump to the called flow's chart
        if t == "run":
            L.append(f"  {k} -->|ok| {n['ok']}")
            f = n.get("fail")
            rec = [r for r in n.get("recover") or [] if r.get("status") != "dropped"]
            src = k
            if rec:  # failure handling is a decision too: draw it as one
                src = f"{k}__why"
                L.append(f'  {k} -.->|fails| {src}{{"what failed?"}}')
                for r in rec:
                    good, bad = stats.get(r["id"], (0, 0))
                    how = "regex" if r.get("match") else "model"
                    L.append(f'  {src} -- "{r["id"]} · {how} · {r.get("status", "promoted")} {good}✓ {bad}✗" --> {r["to"]}')
            edge = "-->|anything else|" if rec else "-.->|fails|"
            if isinstance(f, str) and f.startswith("human:"):
                L.append(f'  {src} {edge} {k}__human(["{_t(f[6:])}"])')
                L.append(f"  class {k}__human humanfail")
            elif f:
                L.append(f"  {src} {edge} {f}")
            elif rec:
                L.append(f'  {src} {edge} {k}__claude(["hand to Claude"])')
                L.append(f"  class {k}__claude humanfail")
        elif t in ("confirm", "human"):
            L.append(f"  {k} --> {n['next']}")
        elif t == "decide":
            opts = n.get("options") or list(n.get("branches") or {})
            for i, (opt, tgt) in enumerate((n.get("branches") or {}).items()):
                when = ((" (check passes)" if opts and opt == opts[0] else " (check fails)")
                        if n.get("check") and n.get("by", "exit") == "exit" else "")
                L.append(f'  {k} -->|"{_t(opt + when, 30)}"| {tgt}')  # quoted: brackets in a bare label break Mermaid
    # Fill = the step's role: where it starts, where it ends, the failure handling, and hand-offs to a person/Claude.
    # Border = where a step came from: learned from a fix (dashed until it proves itself, then solid).
    L += ["  classDef start fill:#dbe7fb,stroke:#2f6fbf,stroke-width:2px,color:#1f2328",
          "  classDef done fill:#d4edda,stroke:#2e7d32,stroke-width:2px,color:#1f2328",
          "  classDef failpath fill:#fde9c8,stroke:#b8741a,color:#1f2328",
          "  classDef humanfail fill:#f8d7da,stroke:#a33,color:#1f2328",
          "  classDef learned stroke:#b8860b,stroke-width:2px,stroke-dasharray:5 3",
          "  classDef promoted stroke:#b8860b,stroke-width:2.5px",
          "  classDef current stroke:#d00,stroke-width:4px"]
    happy = _success_path(flow)
    for k, n in nodes.items():
        role = ("start" if k == flow.get("start") else "done" if n.get("type") == "end" and k in happy
                else "failpath" if k not in happy else None)
        if role:
            L.append(f"  class {k} {role}")
        if n.get("type", "run") == "run" and [r for r in n.get("recover") or [] if r.get("status") != "dropped"]:
            L.append(f"  class {k}__why failpath")
        if n.get("learned_from"):
            L.append(f"  class {k} {'promoted' if status.get(n['learned_from']) == 'promoted' else 'learned'}")
    if current in nodes:
        L.append(f"  class {current} current")
    return "\n".join(L) + "\n"
