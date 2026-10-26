"""The decision model: picks a recovery path when no regex matches, through a Jev-shaped POST /v1/systemone
(a local Laya server or hosted Jev). Configured with DECISION_MODEL_URL; off when unset."""
import json
import os
import re
import secrets as _rand
import socket
import time
import urllib.parse
import urllib.request


def env(name, default=""):
    """DECISION_MODEL_URL, _NAME, _KEY, _THRESHOLD, _TIMEOUT; the older DECIDER_* / JEV_* names (with MODEL for
    NAME) still work."""
    for var in ("DECISION_MODEL_" + name, "DECIDER_" + ("MODEL" if name == "NAME" else name),
                "JEV_" + ("MODEL" if name == "NAME" else name)):
        if os.environ.get(var):
            return os.environ[var]
    return default


THRESH = float(env("THRESHOLD", "0.70"))  # Laya typed-decisions, answer_confidence; see laya/eval/calibration.json
NOISE = re.compile(r"[━█▏▎▍▌▋▊▉]|#{8,}|\b\d{1,3}%\s*[|\[]|\beta \d+:\d\d|^\s*(Downloading|Collecting|Receiving objects|"
                   r"Resolving deltas|remote: (Counting|Compressing|Enumerating))\b")


def clean_tail(text, limit=1200):
    """Error tail for the decision model: drop progress-bar noise, collapse repeated lines, keep the END (Laya keeps the start)."""
    lines, prev = [], None
    for ln in text.splitlines():
        if NOISE.search(ln) or ln == prev:
            continue
        lines.append(ln)
        prev = ln
    return "\n".join(lines)[-limit:]


def ask(store, node, question, criteria, state):
    """(decision_id, choice, answer_confidence) or (decision_id, None, None).

    criteria: {option_id: description}. Never send a 'none' option: abstention is the threshold.
    Needs >= 2 options (with one, any model is 'certain'). No answer on error, timeout or truncated input.
    """
    url = env("URL")
    if not url or len(criteria) < 2:
        return None, None, None
    did = "d-" + _rand.token_hex(4)
    body = {"model": env("NAME", "typed-decisions"), "state": state,
            "questions": {"q": {"type": "choice", "instructions": question, "criteria": criteria}}}
    headers = {"Content-Type": "application/json"}
    if env("KEY"):
        headers["Authorization"] = "Bearer " + env("KEY")
    t0 = time.time()
    rec = {"id": did, "ts": t0, "flow": store.name, "node": node, "options": list(criteria), "threshold": THRESH,
           "state": state}
    choice = conf = None
    try:
        # Fail fast when the decision model is down: under WSL mirrored networking a closed local port
        # hangs instead of refusing, which would cost the full request timeout on every decision.
        u = urllib.parse.urlsplit(url)
        socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), 0.5).close()
        req = urllib.request.Request(url, json.dumps(body).encode(), headers)
        r = json.load(urllib.request.urlopen(req, timeout=float(env("TIMEOUT", "5"))))
        a = r["answers"]["q"]
        # Laya: answer_confidence = max(p). Jev (strict) has no such field: max(p) is the same quantity.
        c = float(a.get("answer_confidence", max(a["probabilities"].values())))
        rec.update(choice=a["choice"], conf=c, probs=a["probabilities"],
                   truncated=bool(r.get("usage", {}).get("truncated")))
        if not rec["truncated"]:
            choice, conf = a["choice"], c
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
    rec["ms"] = round((time.time() - t0) * 1000)
    rec["acted"] = choice is not None and conf >= THRESH and choice in criteria
    store.decision(rec)
    return (did, choice, conf) if rec["acted"] else (did, None, conf)
