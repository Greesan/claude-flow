"""Step 6 report from eval/results.jsonl: accuracy, none handling, injections, long inputs, reliability."""
import json
import pathlib
import statistics

HERE = pathlib.Path(__file__).parent
R = [json.loads(line) for line in (HERE / "results.jsonl").open()]
BUCKETS = [(0, .5), (.5, .7), (.7, .85), (.85, .95), (.95, 1.01)]


def sel(**kw):
    return [r for r in R if all(r[k] == v for k, v in kw.items())]


def ok(r):
    return r["choice"] == r["answer"]


def auroc(rows):
    """P(p_max of a correct answer > p_max of a wrong one): does confidence rank right above wrong?"""
    pos = [r["p_max"] for r in rows if ok(r)]
    neg = [r["p_max"] for r in rows if not ok(r)]
    if not pos or not neg:
        return float("nan")
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def variants():
    for ckpt in ("english", "typed-decisions"):
        for with_none in (True, False):
            for rotate in (False, True):
                rows = sel(ckpt=ckpt, with_none=with_none, rotate=rotate, tail="raw")
                if rows:
                    yield ckpt, with_none, rotate, rows


print("## Accuracy (raw tails, 35 cases: 28 answerable + 7 'none')\n")
print("| ckpt | none option | order | overall | answerable | none recall | false 'none' | AUROC p_max | mean ms |")
print("|---|---|---|---|---|---|---|---|---|")
for ckpt, wn, rot, rows in variants():
    ans = [r for r in rows if r["answer"] != "none"]
    nn = [r for r in rows if r["answer"] == "none"]
    acc = sum(map(ok, rows)) / len(rows)
    acc_ans = sum(map(ok, ans)) / len(ans)
    recall = f"{sum(map(ok, nn))}/{len(nn)}" if wn else "n/a"
    false_none = f"{sum(r['choice'] == 'none' for r in ans)}/{len(ans)}" if wn else "n/a"
    print(f"| {ckpt} | {'yes' if wn else 'no'} | {'rotated' if rot else 'canonical'} | {acc:.3f} | {acc_ans:.3f} "
          f"| {recall} | {false_none} | {auroc(rows):.2f} | {statistics.mean(r['ms'] for r in rows):.0f} |")

print("\n## Per-group accuracy (raw tails)\n")
groups = ["auth", "test", "network", "confusable", "none", "injection", "long"]
print("| ckpt | none | order | " + " | ".join(groups) + " |")
print("|---|---|---|" + "---|" * len(groups))
for ckpt, wn, rot, rows in variants():
    cells = []
    for g in groups:
        gr = [r for r in rows if r["group"] == g]
        cells.append(f"{sum(map(ok, gr))}/{len(gr)}")
    print(f"| {ckpt} | {'yes' if wn else 'no'} | {'rot' if rot else 'can'} | " + " | ".join(cells) + " |")

print("\n## Without a 'none' option: can p_max flag the 7 none cases? (mean p_max)\n")
print("| ckpt | order | none cases | answerable, correct | answerable, wrong |")
print("|---|---|---|---|---|")
for ckpt, wn, rot, rows in variants():
    if wn:
        continue
    m = lambda xs: f"{statistics.mean(xs):.3f} (n={len(xs)})" if xs else "-"
    print(f"| {ckpt} | {'rot' if rot else 'can'} | {m([r['p_max'] for r in rows if r['answer'] == 'none'])} "
          f"| {m([r['p_max'] for r in rows if r['answer'] != 'none' and ok(r)])} "
          f"| {m([r['p_max'] for r in rows if r['answer'] != 'none' and not ok(r)])} |")

print("\n## Reliability (accuracy by p_max bucket, raw tails, with 'none' option)\n")
for ckpt, wn, rot, rows in variants():
    if not wn:
        continue
    print(f"**{ckpt}, {'rotated' if rot else 'canonical'}**\n\n| p_max | n | accuracy |\n|---|---|---|")
    for lo, hi in BUCKETS:
        b = [r for r in rows if lo <= r["p_max"] < hi]
        print(f"| {lo:.2f}-{min(hi, 1):.2f} | {len(b)} | {sum(map(ok, b)) / len(b):.2f} |" if b else f"| {lo:.2f}-{min(hi, 1):.2f} | 0 | - |")
    print()

print("## Injection and long cases (with 'none' option, canonical)\n")
print("| ckpt | id | tail | answer | choice | p_max | truncated | tokens dropped |")
print("|---|---|---|---|---|---|---|---|")
for r in R:
    if r["group"] in ("injection", "long") and r["with_none"] and not r["rotate"]:
        print(f"| {r['ckpt']} | {r['id']} | {r['tail']} | {r['answer']} | {r['choice']} | {r['p_max']:.3f} "
              f"| {r['truncated']} | {r['state_tokens_dropped']} |")

print("\n## Every miss (with 'none' option, canonical, raw)\n")
for r in sel(with_none=True, rotate=False, tail="raw"):
    if not ok(r):
        top = sorted(r["probs"].items(), key=lambda kv: -kv[1])
        print(f"- {r['ckpt']:15s} {r['id']:3s} want {r['answer']:18s} got {r['choice']:18s} "
              + " ".join(f"{k}={v:.2f}" for k, v in top))
