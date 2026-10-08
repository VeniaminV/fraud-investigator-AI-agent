from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

FMT = "%Y-%m-%d %H:%M:%S"


def check_rules(txn, merchant, history):
    amount = txn["amount"]
    ts = datetime.strptime(txn["timestamp"], FMT)
    amounts = [h["amount"] for h in history]
    rules = []

    if amount >= 1500:
        rules.append("HIGH_AMOUNT")
    if amounts and amount > 5 * median(amounts) and amount > 250:
        rules.append("AMOUNT_SPIKE")
    if txn["channel"] == "online" and txn["device_id"] not in {h["device_id"] for h in history}:
        rules.append("NEW_DEVICE")
    if txn["channel"] == "in_store" and txn["city"] not in {h["city"] for h in history}:
        rules.append("NEW_LOCATION")
    recent = [h for h in history if ts - datetime.strptime(h["timestamp"], FMT) <= timedelta(minutes=30)]
    if len(recent) >= 3:
        rules.append("VELOCITY")
    if merchant["risk_level"] == "high" and amount > 100:
        rules.append("HIGH_RISK_MERCHANT")
    if 1 <= ts.hour <= 4 and amount > 200:
        rules.append("ODD_HOUR")
    return rules


def build_alerts(con, review_start, end):
    con.row_factory = lambda c, r: {col[0]: r[i] for i, col in enumerate(c.description)}
    merchants = {m["merchant_id"]: m for m in con.execute("SELECT * FROM merchants")}
    history = defaultdict(list)
    flagged = defaultdict(list)

    for txn in con.execute("SELECT * FROM transactions ORDER BY timestamp"):
        ts = datetime.strptime(txn["timestamp"], FMT)
        if review_start <= ts < end:
            rules = check_rules(txn, merchants[txn["merchant_id"]], history[txn["account_id"]])
            if rules:
                flagged[txn["account_id"]].append((txn, rules))
        history[txn["account_id"]].append(txn)

    alerts = []
    for account_id, hits in flagged.items():
        groups = []
        for txn, rules in hits:
            ts = datetime.strptime(txn["timestamp"], FMT)
            if groups and ts - groups[-1]["last"] <= timedelta(hours=2):
                groups[-1]["items"].append((txn, rules))
                groups[-1]["last"] = ts
            else:
                groups.append({"items": [(txn, rules)], "last": ts})
        for g in groups:
            primary = max(g["items"], key=lambda x: x[0]["amount"])[0]
            alerts.append(dict(
                account_id=account_id,
                primary_txn_id=primary["txn_id"],
                related_txn_ids=[t["txn_id"] for t, _ in g["items"]],
                rules=sorted({r for _, rs in g["items"] for r in rs}),
                created_at=g["last"].strftime(FMT),
            ))

    alerts.sort(key=lambda a: a["created_at"])
    for i, a in enumerate(alerts, 1):
        a["alert_id"] = f"A{i:03d}"
    con.row_factory = None
    return alerts
