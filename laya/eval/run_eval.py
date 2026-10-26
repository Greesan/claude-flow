"""Step 6: run every case in eval/cases.jsonl through Laya under each variant; write eval/results.jsonl.

Variants per case: none option {explicit, absent} x order {canonical, rotation-averaged}
x tail {raw, last 1200 chars} (long cases only) x checkpoint {english, typed-decisions}.
Raw logits for the canonical-order runs go to eval/records.jsonl for the Step 7 temperature fit.
Results are appended per case, so a stopped run resumes where it left off.

usage: run_eval.py --ckpt english|typed-decisions
"""
import argparse
import json
import os
import pathlib
import time

os.environ.setdefault("LAYA_REVISION", "reviewed")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import laya
from laya.calibrate import records_from_labeled
from build_cases import NONE, PATHS

HERE = pathlib.Path(__file__).parent
TAIL_CHARS = 1200
CHECKPOINTS = {"english": None, "typed-decisions": "typed-decisions"}


def question(opts, with_none, order=None):
    criteria = {o: PATHS[o] for o in opts}
    if with_none:
        criteria["none"] = NONE
    q = {"type": "choice", "instructions": "Which recovery path fits this failure?", "criteria": criteria}
    if order is not None:
        q["option_order"] = order
    return q


def ask(agent, state, opts, with_none, rotate):
    """One forward pass. With rotate, every option sits in every slot once and probabilities are averaged."""
    k = len(opts) + with_none
    if rotate:
        qs = {f"r{r}": question(opts, with_none, [(i + r) % k for i in range(k)]) for r in range(k)}
    else:
        qs = {"q": question(opts, with_none)}
    t0 = time.perf_counter()
    res = agent.predict(state, qs)
    ms = (time.perf_counter() - t0) * 1000
    answers = list(res["answers"].values())
    probs = {lab: sum(a["probabilities"][lab] for a in answers) / len(answers) for lab in answers[0]["probabilities"]}
    choice = max(probs, key=probs.get)
    return {"choice": choice, "p_max": probs[choice], "probs": probs, "ms": ms,
            "entropy_conf": answers[0]["confidence"] if not rotate else None,
            "truncated": res["usage"].get("truncated"), "state_tokens_dropped": res["usage"].get("state_tokens_dropped")}


ap = argparse.ArgumentParser()
ap.add_argument("--ckpt", choices=list(CHECKPOINTS), required=True)
args = ap.parse_args()

cases = [json.loads(line) for line in (HERE / "cases.jsonl").open()]
res_path, rec_path = HERE / "results.jsonl", HERE / "records.jsonl"
done = set()
if res_path.exists():
    done = {(r["ckpt"], r["id"]) for r in map(json.loads, res_path.open())}
todo = [c for c in cases if (args.ckpt, c["id"]) not in done]
print(f"[{args.ckpt}] {len(cases) - len(todo)} cases already done, {len(todo)} to run", flush=True)

sub = CHECKPOINTS[args.ckpt]
t0 = time.perf_counter()
agent = laya.load("convaiinnovations/laya", subfolder=sub) if sub else laya.load("convaiinnovations/laya")
print(f"[{args.ckpt}] loaded revision {agent.revision} in {time.perf_counter() - t0:.1f}s", flush=True)
agent.predict({"error_tail": "warmup"}, {"q": question(["reauth", "fix_tests"], False)})

start = time.perf_counter()
with res_path.open("a") as res_f, rec_path.open("a") as rec_f:
    for n, c in enumerate(todo, 1):
        rows, recs = [], []
        tails = {"raw": c["error_tail"]}
        if c["group"] == "long":
            tails["last1200"] = c["error_tail"][-TAIL_CHARS:]
        for tail_kind, tail in tails.items():
            state = {"command": c["command"], "error_tail": tail}
            for with_none in (True, False):
                for rotate in (False, True):
                    r = ask(agent, state, c["options"], with_none, rotate)
                    rows.append({"ckpt": args.ckpt, "id": c["id"], "group": c["group"], "answer": c["answer"],
                                 "with_none": with_none, "rotate": rotate, "tail": tail_kind, **r})
                # Raw logits for temperature fitting (canonical order, raw tail; skip cases with no correct option).
                labels = c["options"] + (["none"] if with_none else [])
                if tail_kind == "raw" and c["answer"] in labels:
                    target = [1.0 if lab == c["answer"] else 0.0 for lab in labels]
                    (qt, logits, tgt, k), = records_from_labeled(agent, [(state, {"q": question(c["options"], with_none)}, {"q": target})])
                    recs.append({"ckpt": args.ckpt, "with_none": with_none, "id": c["id"], "qtype": qt,
                                 "logits": list(map(float, logits)), "target": list(map(float, tgt)), "k": k})
        # Records first, results last: a case counts as done only once both are on disk.
        rec_f.writelines(json.dumps(r) + "\n" for r in recs)
        rec_f.flush()
        res_f.writelines(json.dumps(r) + "\n" for r in rows)
        res_f.flush()
        el = time.perf_counter() - start
        print(f"[{args.ckpt}] {c['id']} done ({n}/{len(todo)}, {el / 60:.1f} min, ETA {el / n * (len(todo) - n) / 60:.1f} min)", flush=True)
