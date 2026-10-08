import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent import MODEL, investigate
from tools import list_alerts

LABELS = json.loads((Path(__file__).parent / "data" / "labels.json").read_text())


def score(results):
    tp = fp = tn = fn = 0
    for r in results:
        flagged = r["report"]["verdict"] != "legitimate"
        truth = LABELS[r["alert_id"]]["is_fraud"]
        tp += flagged and truth
        fp += flagged and not truth
        tn += not flagged and not truth
        fn += not flagged and truth
    n = len(results)
    return {
        "alerts": n,
        "accuracy": round((tp + tn) / n, 3),
        "precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "confusion": {"caught_fraud": tp, "false_alarm": fp, "correctly_cleared": tn, "missed_fraud": fn},
        "needs_review": sum(r["report"]["verdict"] == "needs_review" for r in results),
        "avg_tool_calls": round(sum(len(r["steps"]) for r in results) / n, 1),
        "total_cost_usd": round(sum(r["est_cost_usd"] for r in results), 4),
        "avg_seconds": round(sum(r["seconds"] for r in results) / n, 1),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=MODEL)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args()

    ids = [a["alert_id"] for a in list_alerts()][:args.limit]
    print(f"Running {len(ids)} alerts on {args.model}...")
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda i: investigate(i, model=args.model), ids))

    print(f"\n{'alert':6} {'scenario':22} {'truth':6} {'verdict':13} {'risk':>4}")
    for r in results:
        lab = LABELS[r["alert_id"]]
        v = r["report"]["verdict"]
        wrong = (v != "legitimate") != lab["is_fraud"]
        print(f"{r['alert_id']:6} {lab['scenario']:22} {'fraud' if lab['is_fraud'] else 'legit':6} "
              f"{v:13} {r['report']['risk_score']:>4}{'   <-- wrong' if wrong else ''}")

    summary = score(results)
    rules_only = sum(LABELS[i]["is_fraud"] for i in ids) / len(ids)
    print(f"\nRules engine alone (every alert = fraud): precision {rules_only:.2f}")
    print(json.dumps(summary, indent=2))

    out = Path("evals") / f"results_{args.model}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print(f"Saved to {out}")


if __name__ == "__main__":
    main()
