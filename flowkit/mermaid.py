"""Mermaid flowchart export. Recovery paths are labelled with their status and evidence (from events)."""
import re

SHAPE = {"run": ('["', '"]'), "confirm": ('{{"', '"}}'), "human": ('(["', '"])'), "decide": ('{"', '"}'),
         "end": ('(("', '"))')}


def _t(text, n=48):
    t = re.sub(r"\{\{\s*([\w.-]+)\s*\}\}", r"⟨\1⟩", re.sub(r"\s+", " ", str(text)).strip())  # `{{` is Mermaid syntax
    t = (t[: n - 1] + "…") if len(t) > n else t
    return t.replace('"', "'").replace("<", "‹").replace(">", "›").replace("|", "/").replace("`", "'")


def to_mermaid(flow, stats=None, current=None, timing=None):
    stats = stats or {}
    nodes = flow["nodes"]
    status = {r["id"]: r.get("status", "promoted") for n in nodes.values() for r in n.get("recover") or []}
    L = ["flowchart TD"]
    for k, n in nodes.items():
        t = n.get("type", "run")
        o, c = SHAPE.get(t, SHAPE["run"])
        body = {"run": n.get("cmd") or f"script: {n.get('script', '')}", "confirm": n.get("say", ""),
                "human": n.get("say", ""), "decide": _t(n.get("question", "")), "end": n.get("say") or "end"}[t]
        extra = []
        if t == "decide":  # how the question gets answered: the diagnostic command, or a model
            body += "<br/>" + ("check: " + _t(n["check"], 44) if n.get("check") else "decided by the decision model" if n.get("jev")
                               else "decided by Claude")
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
        L.append(f'  {k}{o}{k}<br/>{body if t == "decide" else _t(body)}{"<br/><i>" + _t("; ".join(extra), 48) + "</i>" if extra else ""}{c}')
    for k, n in nodes.items():
        t = n.get("type", "run")
        if t == "run":
            L.append(f"  {k} -->|ok| {n['ok']}")
            f = n.get("fail")
            if isinstance(f, str) and f.startswith("human:"):
                L.append(f'  {k} -.->|fail| {k}_fail(["{_t(f[6:])}"])')
                L.append(f"  class {k}_fail humanfail")
            elif f:
                L.append(f"  {k} -.->|fail: any other error| {f}")
            for r in n.get("recover") or []:
                if r.get("status") == "dropped":
                    continue
                good, bad = stats.get(r["id"], (0, 0))
                how = "regex" if r.get("match") else "model"
                L.append(f'  {k} -. "{r["id"]} ({how}, {r.get("status", "promoted")} {good}✓ {bad}✗): '
                         f'{_t(r["when"], 34)}" .-> {r["to"]}')
        elif t in ("confirm", "human"):
            L.append(f"  {k} --> {n['next']}")
        elif t == "decide":
            opts = n.get("options") or list(n.get("branches") or {})
            for i, (opt, tgt) in enumerate((n.get("branches") or {}).items()):
                when = (" (check passes)" if opts and opt == opts[0] else " (check fails)") if n.get("check") else ""
                L.append(f"  {k} -->|{_t(opt + when, 30)}| {tgt}")
    L += ["  classDef learned fill:#fff3cd,stroke:#b8860b,color:#1f2328",
          "  classDef promoted fill:#d4edda,stroke:#2e7d32,color:#1f2328",
          "  classDef humanfail fill:#f8d7da,stroke:#a33,color:#1f2328",
          "  classDef current stroke:#d00,stroke-width:4px"]
    for k, n in nodes.items():
        if n.get("learned_from"):
            L.append(f"  class {k} {'promoted' if status.get(n['learned_from']) == 'promoted' else 'learned'}")
    if current in nodes:
        L.append(f"  class {current} current")
    return "\n".join(L) + "\n"
