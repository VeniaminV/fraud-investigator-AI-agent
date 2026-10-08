import json
import math
import os
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).parent / "data" / "fraud.db"))
FMT = "%Y-%m-%d %H:%M:%S"


def connect():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    return con


def rows(con, sql, *args):
    return [dict(r) for r in con.execute(sql, args)]


def miles(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, [a["lat"], a["lon"], b["lat"], b["lon"]])
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3959 * 2 * math.asin(math.sqrt(h))


def list_alerts():
    with closing(connect()) as con:
        out = rows(con, """
            SELECT a.alert_id, a.account_id, a.rules, a.created_at, t.amount, m.name AS merchant, t.city
            FROM alerts a JOIN transactions t ON t.txn_id = a.primary_txn_id
            JOIN merchants m ON m.merchant_id = t.merchant_id ORDER BY a.alert_id""")
    for a in out:
        a["rules"] = json.loads(a["rules"])
    return out


def get_alert(alert_id):
    with closing(connect()) as con:
        found = rows(con, "SELECT * FROM alerts WHERE alert_id = ?", alert_id)
    if not found:
        raise ValueError(f"No alert with id {alert_id}")
    a = found[0]
    a["rules"] = json.loads(a["rules"])
    a["related_txn_ids"] = json.loads(a["related_txn_ids"])
    return a


class Toolkit:
    """All the tools the agent can call. Every query is cut off at `as_of` so the agent can't see the future."""

    def __init__(self, as_of):
        self.as_of = as_of
        self.con = connect()

    def close(self):
        self.con.close()

    def _history(self, account_id, before=None):
        op, cutoff = ("<", before) if before else ("<=", self.as_of)
        return rows(self.con, f"""
            SELECT t.*, m.name AS merchant, m.category FROM transactions t
            JOIN merchants m ON m.merchant_id = t.merchant_id
            WHERE t.account_id = ? AND t.timestamp {op} ? ORDER BY t.timestamp""", account_id, cutoff)

    def _txn(self, txn_id):
        found = rows(self.con, """
            SELECT t.*, m.name AS merchant, m.category, m.risk_level AS merchant_risk FROM transactions t
            JOIN merchants m ON m.merchant_id = t.merchant_id
            WHERE t.txn_id = ? AND t.timestamp <= ?""", txn_id, self.as_of)
        if not found:
            raise ValueError(f"No transaction with id {txn_id}")
        return found[0]

    def _city(self, name):
        found = rows(self.con, "SELECT * FROM cities WHERE city = ?", name)
        return found[0] if found else None

    def get_transaction(self, txn_id):
        return self._txn(txn_id)

    def get_account_profile(self, account_id):
        acct = rows(self.con, "SELECT * FROM accounts WHERE account_id = ?", account_id)
        if not acct:
            raise ValueError(f"No account with id {account_id}")
        hist = self._history(account_id)
        amounts = sorted(h["amount"] for h in hist)
        hours = sorted(int(h["timestamp"][11:13]) for h in hist if h["channel"] == "in_store")
        return {
            **acct[0],
            "transactions_last_90_days": len(hist),
            "median_amount": round(median(amounts), 2),
            "p95_amount": amounts[int(len(amounts) * 0.95)],
            "max_amount": amounts[-1],
            "top_categories": Counter(h["category"] for h in hist).most_common(6),
            "cities_used_in_store": Counter(h["city"] for h in hist if h["channel"] == "in_store").most_common(),
            "known_devices": len({h["device_id"] for h in hist if h["device_id"]}),
            "online_share": round(sum(h["channel"] == "online" for h in hist) / len(hist), 2),
            "typical_in_store_hours": f"{hours[len(hours) // 10]}:00-{hours[len(hours) * 9 // 10]}:00",
        }

    def get_account_history(self, account_id, days):
        days = max(1, min(days, 90))
        start = (datetime.strptime(self.as_of, FMT) - timedelta(days=days)).strftime(FMT)
        hist = [h for h in self._history(account_id) if h["timestamp"] >= start]
        keep = ["txn_id", "timestamp", "merchant", "category", "amount", "city", "channel", "device_id"]
        return {"count": len(hist), "showing_latest": min(len(hist), 60),
                "transactions": [{k: h[k] for k in keep} for h in hist[-60:]]}

    def get_nearby_transactions(self, txn_id, minutes):
        minutes = max(1, min(minutes, 24 * 60))
        txn = self._txn(txn_id)
        ts = datetime.strptime(txn["timestamp"], FMT)
        lo = (ts - timedelta(minutes=minutes)).strftime(FMT)
        hi = min((ts + timedelta(minutes=minutes)).strftime(FMT), self.as_of)
        near = rows(self.con, """
            SELECT t.txn_id, t.timestamp, m.name AS merchant, m.category, t.amount, t.city, t.channel, t.device_id
            FROM transactions t JOIN merchants m ON m.merchant_id = t.merchant_id
            WHERE t.account_id = ? AND t.timestamp BETWEEN ? AND ? ORDER BY t.timestamp""",
                    txn["account_id"], lo, hi)
        return {"window_minutes": minutes, "count": len(near), "transactions": near}

    def compare_to_baseline(self, txn_id):
        txn = self._txn(txn_id)
        hist = self._history(txn["account_id"], before=txn["timestamp"])
        amounts = [h["amount"] for h in hist]
        med = median(amounts)
        hour = int(txn["timestamp"][11:13])
        result = {
            "txn_id": txn_id,
            "amount": txn["amount"],
            "account_median_amount": round(med, 2),
            "times_median": round(txn["amount"] / med, 1),
            "percentile_vs_history": round(100 * sum(a < txn["amount"] for a in amounts) / len(amounts), 1),
            "prior_txns_same_merchant": sum(h["merchant_id"] == txn["merchant_id"] for h in hist),
            "prior_txns_same_category": sum(h["category"] == txn["category"] for h in hist),
            "share_of_history_within_2h_of_this_hour": round(
                sum(abs(int(h["timestamp"][11:13]) - hour) <= 2 for h in hist) / len(hist), 2),
        }
        if txn["channel"] == "online":
            seen = [h for h in hist if h["device_id"] == txn["device_id"]]
            result["device_seen_before"] = bool(seen)
            if seen:
                first = datetime.strptime(seen[0]["timestamp"], FMT)
                result["device_first_seen_hours_before"] = round(
                    (datetime.strptime(txn["timestamp"], FMT) - first).total_seconds() / 3600, 1)
        else:
            result["prior_in_store_txns_in_this_city"] = sum(h["city"] == txn["city"] for h in hist)
            prev = next((h for h in reversed(hist) if h["channel"] == "in_store" and h["city"] != txn["city"]),
                        None)
            here, there = self._city(txn["city"]), self._city(prev["city"]) if prev else None
            if prev and here and there:
                gap_min = (datetime.strptime(txn["timestamp"], FMT) -
                           datetime.strptime(prev["timestamp"], FMT)).total_seconds() / 60
                dist = miles(here, there)
                result["last_in_store_txn_in_another_city"] = {
                    "txn_id": prev["txn_id"], "city": prev["city"], "minutes_before": round(gap_min),
                    "distance_miles": round(dist),
                    "implied_speed_mph": round(dist / (gap_min / 60)) if gap_min > 0 else None,
                }
        return result

    def check_travel_notices(self, account_id):
        notices = rows(self.con, "SELECT destination, start_date, end_date FROM travel_notices WHERE account_id = ?",
                       account_id)
        return {"notices": notices, "count": len(notices)}

    def get_merchant_info(self, merchant_id):
        found = rows(self.con, "SELECT * FROM merchants WHERE merchant_id = ?", merchant_id)
        if not found:
            raise ValueError(f"No merchant with id {merchant_id}")
        week_ago = (datetime.strptime(self.as_of, FMT) - timedelta(days=7)).strftime(FMT)
        stats = rows(self.con, """
            SELECT COUNT(*) AS txns_last_7_days, COUNT(DISTINCT account_id) AS accounts_last_7_days
            FROM transactions WHERE merchant_id = ? AND timestamp BETWEEN ? AND ?""",
                     merchant_id, week_ago, self.as_of)[0]
        return {**found[0], **stats}


def _schema(props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


TXN_ID = {"type": "string", "description": "Transaction id, e.g. T004512"}
ACCOUNT_ID = {"type": "string", "description": "Account id, e.g. ACC1025"}

TOOL_SCHEMAS = [
    {"name": "get_transaction",
     "description": "Get full details for one transaction: amount, time, merchant, category, merchant risk level, "
                    "city, channel (online or in_store) and device id for online purchases.",
     "input_schema": _schema({"txn_id": TXN_ID})},
    {"name": "get_account_profile",
     "description": "Get the customer's profile and 90-day spending baseline: home city, account age, median and "
                    "95th percentile amounts, top categories, cities used in person, number of known devices, "
                    "online share and typical in-store hours. Good first call to learn what normal looks like.",
     "input_schema": _schema({"account_id": ACCOUNT_ID})},
    {"name": "get_account_history",
     "description": "List the account's transactions from the last N days before the alert (newest 60 shown). "
                    "Use it to look for past purchases at the same merchant, earlier travel, or a pattern change.",
     "input_schema": _schema({"account_id": ACCOUNT_ID,
                              "days": {"type": "integer", "description": "How many days back to look, 1 to 90"}})},
    {"name": "get_nearby_transactions",
     "description": "List every transaction on the same account within N minutes before or after a transaction. "
                    "Use it to spot card testing (many tiny charges), rapid sprees, or two in-person purchases far "
                    "apart in a short time.",
     "input_schema": _schema({"txn_id": TXN_ID,
                              "minutes": {"type": "integer", "description": "Window size in minutes, 1 to 1440"}})},
    {"name": "compare_to_baseline",
     "description": "Compare one transaction to the account's history: how many times the median it is, its "
                    "percentile, whether the merchant/category/device/city was seen before, whether the hour is "
                    "typical, and for in-store purchases the distance and implied travel speed from the last "
                    "in-store purchase in a different city (over ~500 mph means the card was in two places at once).",
     "input_schema": _schema({"txn_id": TXN_ID})},
    {"name": "check_travel_notices",
     "description": "Check whether the customer told the bank they would be traveling, with destination and dates.",
     "input_schema": _schema({"account_id": ACCOUNT_ID})},
    {"name": "get_merchant_info",
     "description": "Get merchant details: category, location, risk level (low, medium, high) and how many "
                    "transactions and distinct accounts it had in the last 7 days.",
     "input_schema": _schema({"merchant_id": {"type": "string", "description": "Merchant id, e.g. M042"}})},
    {"name": "submit_report",
     "description": "Submit your final case report. Call this exactly once, when you are done investigating.",
     "input_schema": _schema({
         "verdict": {"type": "string", "enum": ["fraud", "legitimate", "needs_review"]},
         "risk_score": {"type": "integer",
                        "description": "0-100. 0-30 clearly legitimate, 31-69 unclear, 70-100 likely fraud."},
         "summary": {"type": "string", "description": "2-4 sentence plain-English explanation of what happened."},
         "key_evidence": {"type": "array", "items": {"type": "string"},
                          "description": "3-6 specific facts with numbers that support the verdict."},
         "recommended_action": {"type": "string",
                                "enum": ["block_card_and_contact_customer", "contact_customer_to_verify",
                                         "close_alert_no_action"]},
     })},
]
for t in TOOL_SCHEMAS:
    t["strict"] = True
