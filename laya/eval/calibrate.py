"""Step 7: fit an act/fallback threshold (and optionally a temperature) with 2-fold held-out validation.

Scope: typed-decisions, no 'none' option, canonical order, raw tails -- the variant Step 6 recommended.
'none' cases have no correct option, so any answer to them counts as wrong: the gate must stop them.

Logits: shipped probabilities p = softmax(z / T_ship), so z = T_ship * log p (up to a constant, which
softmax ignores). That lets the 7 'none' cases (no saved records) be rescored too; the 28 saved raw
records check the reconstruction. Writes eval/calibration.json.
"""
import glob
import json
import os
import pathlib

import numpy as np
from laya.calibrate import fit_one_temperature
from laya.common import temp_bucket

HERE = pathlib.Path(__file__).parent
CKPT = "typed-decisions"
TARGET_PRECISION = 0.90
MIN_ACTED = 3

cfg_path = glob.glob(os.path.expanduser(
    "~/.cache/huggingface/hub/models--convaiinnovations--laya/snapshots/*/typed-decisions/rl_agent_config.json"))[0]
cfg = json.load(open(cfg_path))
CHOICE = 0  # qtype index for choice


def t_ship(k):
    return cfg["temperature_by_options"].get(temp_bucket(CHOICE, k), cfg["temperature"][CHOICE])


def softmax(z):
    e = np.exp(z - z.max())
    return e / e.sum()


rows = [r for r in map(json.loads, (HERE / "results.jsonl").open())
        if r["ckpt"] == CKPT and not r["with_none"] and not r["rotate"] and r["tail"] == "raw"]
records = {r["id"]: r for r in map(json.loads, (HERE / "records.jsonl").open())
           if r["ckpt"] == CKPT and not r["with_none"]}

cases = []
worst = 0.0
for r in rows:
    labels = list(r["probs"])
    k = len(labels)
    z = t_ship(k) * np.log(np.clip([r["probs"][lab] for lab in labels], 1e-4, None))
    if r["id"] in records:  # reconstruction check against the true raw logits
        raw = np.array(records[r["id"]]["logits"])
        worst = max(worst, float(np.abs(softmax(z / t_ship(k)) - softmax(raw / t_ship(k))).max()))
        z = raw
    target = labels.index(r["answer"]) if r["answer"] in labels else None
    cases.append({"id": r["id"], "group": r["group"], "z": z, "k": k, "target": target})
print(f"logit reconstruction: max prob diff vs saved raw logits = {worst:.4f} (rounding only)\n")


def score(cs, temp_of_k):
    """(p_max, correct) per case with the given temperature function."""
    out = []
    for c in cs:
        p = softmax(c["z"] / temp_of_k(c["k"]))
        out.append((float(p.max()), c["target"] is not None and int(p.argmax()) == c["target"], c))
    return out


def pick_threshold(scored):
    """Lowest cut whose acted-on set (>= MIN_ACTED cases) reaches TARGET_PRECISION; None if none does."""
    for t in sorted({round(p, 4) for p, _, _ in scored}):
        acted = [ok for p, ok, _ in scored if p >= t]
        if len(acted) >= MIN_ACTED and sum(acted) / len(acted) >= TARGET_PRECISION:
            return t
    return None


def evaluate(scored, t):
    acted = [(ok, c) for p, ok, c in scored if t is not None and p >= t]
    leaked = [c["id"] for ok, c in acted if c["target"] is None]
    right = sum(ok for ok, _ in acted)
    return {"acted": len(acted), "n": len(scored), "right": right,
            "precision": round(right / len(acted), 3) if acted else None, "none_leaked": leaked}


# Stratified 2-fold split: within each group, alternate cases by id.
folds = {0: [], 1: []}
for g in sorted({c["group"] for c in cases}):
    for i, c in enumerate(sorted((c for c in cases if c["group"] == g), key=lambda c: c["id"])):
        folds[i % 2].append(c)

summary = {}
for name, use_refit in (("shipped", False), ("refit", True)):
    print(f"## {name} temperature")
    held = []
    for fit_i, test_i in ((0, 1), (1, 0)):
        fit, test = folds[fit_i], folds[test_i]
        if use_refit:
            pairs = [(c["z"], np.eye(c["k"])[c["target"]]) for c in fit if c["target"] is not None]
            T = fit_one_temperature(pairs, min_n=10)
            temp_of_k = lambda k, T=T: T
        else:
            T, temp_of_k = None, t_ship
        t = pick_threshold(score(fit, temp_of_k))
        ev = evaluate(score(test, temp_of_k), t)
        held.append(ev)
        print(f"  fit fold {fit_i} (n={len(fit)}) T={T if T is None else round(T, 3)} threshold={t} "
              f"-> held-out fold {test_i}: acted {ev['acted']}/{ev['n']}, right {ev['right']}, "
              f"precision {ev['precision']}, none leaked {ev['none_leaked']}")
    acted = sum(h["acted"] for h in held)
    right = sum(h["right"] for h in held)
    summary[name] = {"held_out_acted": acted, "held_out_n": sum(h["n"] for h in held), "held_out_right": right,
                     "held_out_precision": round(right / acted, 3) if acted else None}
    print(f"  pooled held-out: acted {acted}/{summary[name]['held_out_n']}, right {right}, "
          f"precision {summary[name]['held_out_precision']}\n")

# Final threshold on all 35 cases with shipped temperatures (no client-side rescaling needed).
all_scored = score(cases, t_ship)
final_t = pick_threshold(all_scored)
print("## all 35 cases, shipped temperature: act-if p >= t")
for t in (0.5, 0.55, 0.6, 0.65, 0.7, 0.75):
    ev = evaluate(all_scored, t)
    print(f"  t={t:.2f}: acted {ev['acted']:2d}/35 right {ev['right']:2d} precision {ev['precision']} none leaked {ev['none_leaked']}")
print(f"\nlowest cut reaching {TARGET_PRECISION:.0%} on all 35: {final_t}")

out = {
    "checkpoint": CKPT, "revision": "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851",
    "field": "answers.<qid>.answer_confidence", "question_shape": "choice, 2-4 options, no 'none' option",
    "target_precision": TARGET_PRECISION, "act_threshold_fit_all": final_t,
    "cross_validation": summary, "n_cases": len(cases),
    "note": "35 hand-written cases; thresholds are indicative. Refit on logged real decisions.",
}
(HERE / "calibration.json").write_text(json.dumps(out, indent=2))
print("wrote eval/calibration.json")
