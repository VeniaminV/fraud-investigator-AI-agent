import json
import os
import sys
import time
from functools import lru_cache

import anthropic
from dotenv import load_dotenv

from tools import TOOL_SCHEMAS, Toolkit, get_alert

load_dotenv()

MODEL = os.getenv("MODEL", "claude-haiku-5-5")
MAX_STEPS = 12

# approx $ per million tokens (input, output) - check the pricing page, these change
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2, 10), "claude-opus-5-5": (4, 20)}

SYSTEM_PROMPT = """You are a fraud analyst at a retail bank. A rules engine flagged a card transaction and you \
have to decide if it is really fraud.

The rules engine is noisy: lots of its alerts are normal customers doing something new (traveling, buying a \
laptop, getting a new phone). Your job is to separate real fraud from false alarms using evidence, not vibes.

How to work:
1. Look at the flagged transaction(s) and the account's normal behavior.
2. Pick the checks that matter for THIS alert. Common fraud patterns: card testing (several tiny charges then a \
bigger one), account takeover (new device, odd hour, big online purchase), impossible travel (in-person purchases \
in two far-apart places too close in time), sprees at gift card or crypto merchants. Common innocent explanations: \
a travel notice, a drivable trip, a big purchase that fits the customer's history, a new device with normal spending.
3. Be efficient. Most cases need 3-6 tool calls. Don't call a tool just to call it.
4. Finish by calling submit_report exactly once. Use specific numbers from the tools as evidence.

Scoring: 0-30 clearly legitimate, 31-69 genuinely unclear (needs_review), 70-100 likely fraud.
Use needs_review only when you found real evidence pointing both ways. If your checks found an innocent explanation and no fraud signal, choose legitimate.
"""


def run_tool(kit, name, args):
    try:
        return json.dumps(getattr(kit, name)(**args), default=str), False
    except Exception as e:
        return f"Error: {e}", True


def investigate(alert_id, client=None, model=MODEL, verbose=False):
    client = client or anthropic.Anthropic(api_key=get_api_key())
    alert = get_alert(alert_id)
    kit = Toolkit(as_of=alert["created_at"])
    messages = [{"role": "user", "content": f"Investigate this alert:\n{json.dumps(alert, indent=2)}"}]
    steps, usage = [], {"input_tokens": 0, "output_tokens": 0}
    start = time.time()

    try:
        for _ in range(MAX_STEPS):
            resp = client.messages.create(model=model, max_tokens=2000, system=SYSTEM_PROMPT,
                                          tools=TOOL_SCHEMAS, messages=messages)
            usage["input_tokens"] += resp.usage.input_tokens
            usage["output_tokens"] += resp.usage.output_tokens
            messages.append({"role": "assistant", "content": resp.content})

            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls:
                messages.append({"role": "user", "content": "Please finish by calling submit_report."})
                continue

            results = []
            for call in calls:
                if call.name == "submit_report":
                    return finish(alert, call.input, steps, usage, model, start)
                output, is_error = run_tool(kit, call.name, call.input)
                steps.append({"tool": call.name, "input": call.input, "error": is_error})
                if verbose:
                    print(f"  -> {call.name}({call.input})" + ("  [error]" if is_error else ""))
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": output, "is_error": is_error})
            messages.append({"role": "user", "content": results})
    finally:
        kit.close()

    report = {"verdict": "needs_review", "risk_score": 50, "summary": "Agent hit the step limit.",
              "key_evidence": [], "recommended_action": "contact_customer_to_verify"}
    return finish(alert, report, steps, usage, model, start)


def finish(alert, report, steps, usage, model, start):
    report["risk_score"] = max(0, min(100, int(report["risk_score"])))
    price_in, price_out = PRICES.get(model, (0, 0))
    cost = (usage["input_tokens"] * price_in + usage["output_tokens"] * price_out) / 1_000_000
    return {"alert_id": alert["alert_id"], "account_id": alert["account_id"], "report": report,
            "steps": steps, "model": model, "usage": usage, "est_cost_usd": round(cost, 5),
            "seconds": round(time.time() - start, 1)}


@lru_cache
def get_api_key():
    if os.getenv("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    secret_name = os.getenv("SECRET_NAME")
    if not secret_name:
        raise RuntimeError("Set ANTHROPIC_API_KEY (local) or SECRET_NAME (AWS)")
    import boto3
    return boto3.client("secretsmanager").get_secret_value(SecretId=secret_name)["SecretString"]


if __name__ == "__main__":
    alert_id = sys.argv[1] if len(sys.argv) > 1 else "A001"
    print(f"Investigating {alert_id} with {MODEL}...")
    result = investigate(alert_id, verbose=True)
    print(json.dumps(result["report"], indent=2))
    print(f"{len(result['steps'])} tool calls, {result['usage']}, ~${result['est_cost_usd']}, {result['seconds']}s")
