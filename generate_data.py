import json
import math
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from flagger import build_alerts

SEED = 42
START = datetime(2026, 7, 1)
REVIEW_START = datetime(2026, 9, 24)
END = datetime(2026, 10, 1)
DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "fraud.db"
LABELS_PATH = DATA_DIR / "labels.json"

rng = random.Random(SEED)

CITIES = {
    "Portland": ("OR", "US", 45.5152, -122.6784),
    "Seattle": ("WA", "US", 47.6062, -122.3321),
    "Chicago": ("IL", "US", 41.8781, -87.6298),
    "Milwaukee": ("WI", "US", 43.0389, -87.9065),
    "Austin": ("TX", "US", 30.2672, -97.7431),
    "San Antonio": ("TX", "US", 29.4241, -98.4936),
    "Denver": ("CO", "US", 39.7392, -104.9903),
    "Colorado Springs": ("CO", "US", 38.8339, -104.8214),
    "New York": ("NY", "US", 40.7128, -74.0060),
    "Philadelphia": ("PA", "US", 39.9526, -75.1652),
    "Miami": ("FL", "US", 25.7617, -80.1918),
    "Las Vegas": ("NV", "US", 36.1699, -115.1398),
    "Honolulu": ("HI", "US", 21.3069, -157.8583),
    "Los Angeles": ("CA", "US", 34.0522, -118.2437),
    "London": ("", "GB", 51.5074, -0.1278),
    "Toronto": ("ON", "CA", 43.6532, -79.3832),
}
HOME_CITIES = ["Portland", "Seattle", "Chicago", "Austin", "Denver", "New York"]
NEIGHBOR = {"Portland": "Seattle", "Seattle": "Portland", "Chicago": "Milwaukee",
            "Austin": "San Antonio", "Denver": "Colorado Springs", "New York": "Philadelphia"}
FAR_CITIES = ["Miami", "Las Vegas", "London", "Los Angeles", "Toronto"]

LOCAL_CATEGORIES = {
    "grocery": (["GreenLeaf Market", "Daily Basket", "FreshWay Foods"], 55),
    "restaurant": (["Lantern Kitchen", "Blue Fig Bistro", "Taco Norte", "Oak & Ember"], 32),
    "coffee": (["Steamline Coffee", "Bean Theory"], 6),
    "gas": (["FuelStop", "Highway 9 Gas"], 45),
    "pharmacy": (["CareRx Pharmacy"], 24),
    "retail": (["Northline Outfitters", "Home & Hearth"], 65),
    "electronics": (["VoltBox Electronics"], 140),
    "luxury": (["Maison Verre"], 400),
    "hotel": (["Harborview Hotel"], 260),
}
ONLINE_MERCHANTS = [
    ("ShopNest Online", "online_retail", "low", 45),
    ("ParcelPeak", "online_retail", "low", 60),
    ("StreamFlix Plus", "streaming", "low", 15.99),
    ("TuneWave Music", "streaming", "low", 10.99),
    ("SkyRoute Travel", "travel", "low", 420),
    ("ByteHub Direct", "electronics", "medium", 180),
    ("GiftNow Cards", "gift_cards", "high", 50),
    ("CardVault Gifts", "gift_cards", "high", 50),
    ("CoinSwift Exchange", "crypto", "high", 300),
    ("QuickGive Donations", "donations", "high", 10),
]
FIRST = ["Ava", "Liam", "Maya", "Noah", "Zoe", "Ethan", "Lena", "Omar", "Priya", "Jonah",
         "Sofia", "Mateo", "Hana", "Caleb", "Nora", "Ravi", "Iris", "Felix", "Tessa", "Diego"]
LAST = ["Kim", "Patel", "Nguyen", "Garcia", "Okafor", "Larsen", "Cohen", "Silva", "Reyes",
        "Brooks", "Ivanova", "Tanaka", "Moreau", "Haddad", "Novak", "Quinn"]


def make_merchants():
    merchants = []
    mid = 1

    def add(name, category, city, risk):
        nonlocal mid
        state, country, _, _ = CITIES[city] if city != "Online" else ("", "", 0, 0)
        merchants.append(dict(merchant_id=f"M{mid:03d}", name=name, category=category,
                              city=city, state=state, country=country, risk_level=risk))
        mid += 1

    for city in CITIES:
        for category, (names, _) in LOCAL_CATEGORIES.items():
            for name in names:
                add(f"{name} - {city}", category, city, "low")
    for name, category, risk, _ in ONLINE_MERCHANTS:
        add(name, category, "Online", risk)
    return merchants


MERCHANTS = make_merchants()
BY_ID = {m["merchant_id"]: m for m in MERCHANTS}
TYPICAL = {**{c: v[1] for c, v in LOCAL_CATEGORIES.items()},
           **{n: amt for n, _, _, amt in ONLINE_MERCHANTS}}


def local(city, category):
    return [m for m in MERCHANTS if m["city"] == city and m["category"] == category]


def online(category=None, name=None):
    return [m for m in MERCHANTS if m["city"] == "Online"
            and (category is None or m["category"] == category)
            and (name is None or m["name"] == name)]


def new_device():
    return "dev_" + "".join(rng.choice("abcdef0123456789") for _ in range(6))


def make_accounts(n=60):
    accounts = []
    for i in range(n):
        home = HOME_CITIES[i % len(HOME_CITIES)]
        accounts.append(dict(
            account_id=f"ACC{1001 + i}",
            name=f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            home_city=home,
            home_state=CITIES[home][0],
            opened_date=f"{rng.randint(2014, 2025)}-{rng.randint(1, 12):02d}-01",
            spend_level=round(rng.lognormvariate(0, 0.35), 2),
            peak_hour=rng.choice([9, 12, 13, 17, 18, 19, 20]),
            online_share=round(rng.uniform(0.1, 0.45), 2),
            devices=[new_device() for _ in range(rng.randint(1, 2))],
            weights={"grocery": 5, "restaurant": 4, "coffee": rng.randint(1, 6), "gas": rng.randint(1, 4),
                     "pharmacy": 1, "retail": 2, "electronics": 0.2},
        ))
    return accounts


txns = []
fraud_ids = set()
scenario_of = {}


def add_txn(acc, merchant, amount, ts, device=None, fraud=False, scenario=None):
    txn_id = f"T{len(txns) + 1:06d}"
    online_m = merchant["city"] == "Online"
    city = acc["home_city"] if online_m else merchant["city"]
    txns.append(dict(
        txn_id=txn_id, account_id=acc["account_id"], merchant_id=merchant["merchant_id"],
        amount=round(amount, 2), timestamp=ts.strftime("%Y-%m-%d %H:%M:%S"),
        city="Online" if online_m else city,
        state="" if online_m else CITIES[city][0],
        country="" if online_m else CITIES[city][1],
        channel="online" if online_m else "in_store",
        device_id=(device or rng.choice(acc["devices"])) if online_m else None,
    ))
    if fraud:
        fraud_ids.add(txn_id)
    if scenario:
        scenario_of[txn_id] = scenario
    return txn_id


def amount_for(category_or_name, level):
    base = TYPICAL[category_or_name]
    if category_or_name in ("StreamFlix Plus", "TuneWave Music"):
        return base
    return max(1.5, rng.lognormvariate(math.log(base * level), 0.45))


def rand_time(day, peak):
    hour = int(min(23, max(6, rng.gauss(peak, 3))))
    return day.replace(hour=hour, minute=rng.randint(0, 59), second=rng.randint(0, 59))


def normal_history(acc, start, end, skip_days=()):
    favs = {c: rng.sample(local(acc["home_city"], c), min(2, len(local(acc["home_city"], c))))
            for c in acc["weights"]}
    stream = rng.choice(online("streaming"))
    day = start
    while day < end:
        if day.date() in skip_days:
            day += timedelta(days=1)
            continue
        if day.day == 3:
            add_txn(acc, stream, TYPICAL[stream["name"]], day.replace(hour=4, minute=12))
        for _ in range(rng.choice([0, 1, 1, 2, 2, 3])):
            if rng.random() < acc["online_share"]:
                m = rng.choice(online("online_retail"))
                add_txn(acc, m, amount_for(m["name"], acc["spend_level"]), rand_time(day, acc["peak_hour"] + 2))
            else:
                cats, w = zip(*acc["weights"].items())
                cat = rng.choices(cats, w)[0]
                m = rng.choice(favs[cat])
                add_txn(acc, m, amount_for(cat, acc["spend_level"]), rand_time(day, acc["peak_hour"]))
        day += timedelta(days=1)


def review_day():
    return REVIEW_START + timedelta(days=rng.randint(0, 5))


def at(day, hour, minute=0):
    return day.replace(hour=hour, minute=minute, second=rng.randint(0, 59))


# fraud scenarios

def card_testing(acc):
    dev = new_device()
    t = at(review_day(), rng.randint(1, 3), rng.randint(0, 20))
    donate = online(name="QuickGive Donations")[0]
    for _ in range(rng.randint(5, 7)):
        t += timedelta(minutes=rng.randint(1, 3))
        add_txn(acc, donate, rng.uniform(0.5, 1.99), t, dev, True, "card_testing")
    t += timedelta(minutes=rng.randint(4, 10))
    add_txn(acc, rng.choice(online("gift_cards")), rng.choice([250, 300, 500]), t, dev, True, "card_testing")


def account_takeover(acc):
    dev = new_device()
    t = at(review_day(), rng.randint(2, 4), rng.randint(0, 50))
    shop = online(name="ByteHub Direct")[0]
    add_txn(acc, shop, rng.uniform(1300, 2400), t, dev, True, "account_takeover")
    add_txn(acc, shop, rng.uniform(600, 1100), t + timedelta(minutes=rng.randint(6, 15)), dev, True, "account_takeover")


def impossible_travel(acc):
    day = review_day()
    t = at(day, rng.randint(11, 16), rng.randint(0, 59))
    add_txn(acc, rng.choice(local(acc["home_city"], "grocery")), rng.uniform(25, 90), t)
    far = rng.choice(FAR_CITIES)
    t2 = t + timedelta(minutes=rng.randint(35, 80))
    add_txn(acc, local(far, "electronics")[0], rng.uniform(700, 1600), t2, fraud=True, scenario="impossible_travel")
    add_txn(acc, local(far, "luxury")[0], rng.uniform(500, 1200), t2 + timedelta(minutes=rng.randint(10, 25)),
            fraud=True, scenario="impossible_travel")


def crypto_new_device(acc):
    dev = new_device()
    t = at(review_day(), rng.randint(20, 23), rng.randint(0, 59))
    add_txn(acc, online("crypto")[0], rng.uniform(1500, 3000), t, dev, True, "crypto_new_device")


def gift_card_spree(acc):
    dev = new_device()
    t = at(review_day(), rng.randint(19, 22), rng.randint(0, 20))
    for _ in range(rng.randint(3, 4)):
        add_txn(acc, rng.choice(online("gift_cards")), rng.choice([200, 250, 400, 500]), t, dev, True, "gift_card_spree")
        t += timedelta(minutes=rng.randint(5, 15))


# legit scenarios that still trip the rules

travel_notices = []


def travel_with_notice(acc):
    start = review_day().replace(hour=0, minute=0, second=0)
    end = start + timedelta(days=4)
    travel_notices.append(dict(account_id=acc["account_id"], destination="Honolulu, HI",
                               start_date=start.strftime("%Y-%m-%d"), end_date=end.strftime("%Y-%m-%d")))
    add_txn(acc, online(name="SkyRoute Travel")[0], rng.uniform(650, 950),
            at(start - timedelta(days=rng.randint(20, 40)), 20), scenario="travel_with_notice")
    t = at(start, 18, rng.randint(0, 40))
    add_txn(acc, local("Honolulu", "hotel")[0], rng.uniform(700, 1100), t, scenario="travel_with_notice")
    add_txn(acc, rng.choice(local("Honolulu", "restaurant")), rng.uniform(60, 140),
            t + timedelta(minutes=rng.randint(60, 100)), scenario="travel_with_notice")
    for d in range(1, 4):
        cat = rng.choice(["restaurant", "retail", "coffee"])
        add_txn(acc, rng.choice(local("Honolulu", cat)), amount_for(cat, acc["spend_level"]),
                rand_time(start + timedelta(days=d), 13), scenario="travel_with_notice")
    return {(start + timedelta(days=d)).date() for d in range(4)}


def big_planned_purchase(acc):
    store = local(acc["home_city"], "electronics")[0]
    add_txn(acc, store, rng.uniform(25, 60), at(review_day() - timedelta(days=rng.randint(10, 30)), 15))
    add_txn(acc, store, rng.uniform(1600, 2300), at(review_day(), rng.randint(11, 17), rng.randint(0, 59)),
            scenario="big_planned_purchase")


def new_phone(acc):
    day = review_day()
    add_txn(acc, local(acc["home_city"], "electronics")[0], rng.uniform(800, 1100),
            at(day, rng.randint(12, 16), rng.randint(0, 59)), scenario="new_phone")
    dev = new_device()
    acc["devices"].append(dev)
    m = online(name="ShopNest Online")[0]
    nxt = day + timedelta(days=1)
    add_txn(acc, m, rng.uniform(30, 80), at(nxt, 19, rng.randint(0, 59)), dev, scenario="new_phone")


def drive_to_neighbor(acc):
    day = review_day()
    t = at(day, rng.randint(8, 10), rng.randint(0, 59))
    add_txn(acc, rng.choice(local(acc["home_city"], "gas")), rng.uniform(40, 70), t)
    dest = NEIGHBOR[acc["home_city"]]
    t2 = t + timedelta(hours=rng.randint(3, 4), minutes=rng.randint(0, 50))
    add_txn(acc, rng.choice(local(dest, "restaurant")), rng.uniform(30, 75), t2, scenario="drive_to_neighbor")
    add_txn(acc, local(dest, "hotel")[0], rng.uniform(180, 320), t2 + timedelta(hours=2), scenario="drive_to_neighbor")


def high_spender(acc):
    acc["spend_level"] = 3.5
    shop = local(acc["home_city"], "luxury")[0]
    for _ in range(rng.randint(4, 6)):
        add_txn(acc, shop, rng.uniform(900, 1800), at(START + timedelta(days=rng.randint(0, 80)), rng.randint(12, 18)))
    add_txn(acc, shop, rng.uniform(1600, 2100), at(review_day(), rng.randint(12, 18), rng.randint(0, 59)),
            scenario="high_spender")


FRAUD_SCENARIOS = [card_testing] * 3 + [account_takeover] * 3 + [impossible_travel] * 3 + \
                  [crypto_new_device] * 2 + [gift_card_spree] * 3
LEGIT_SCENARIOS = [travel_with_notice] * 3 + [big_planned_purchase] * 3 + [new_phone] * 2 + \
                  [drive_to_neighbor] * 2 + [high_spender] * 2


def build_db():
    DATA_DIR.mkdir(exist_ok=True)
    accounts = make_accounts()
    picked = rng.sample(accounts, len(FRAUD_SCENARIOS) + len(LEGIT_SCENARIOS))
    plan = dict(zip([a["account_id"] for a in picked], FRAUD_SCENARIOS + LEGIT_SCENARIOS))

    for acc in accounts:
        scenario = plan.get(acc["account_id"])
        skip = set()
        if scenario is high_spender:
            scenario(acc)
            scenario = None
        if scenario is travel_with_notice:
            skip = scenario(acc)
            scenario = None
        normal_history(acc, START, END, skip)
        if scenario:
            scenario(acc)

    txns.sort(key=lambda t: t["timestamp"])
    renamed = {}
    for i, t in enumerate(txns, 1):
        renamed[t["txn_id"]] = t["txn_id"] = f"T{i:06d}"
    new_fraud = {renamed[i] for i in fraud_ids}
    new_scen = {renamed[i]: s for i, s in scenario_of.items()}
    fraud_ids.clear(); fraud_ids.update(new_fraud)
    scenario_of.clear(); scenario_of.update(new_scen)
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE accounts (account_id TEXT PRIMARY KEY, name TEXT, home_city TEXT, home_state TEXT,
                               opened_date TEXT);
        CREATE TABLE merchants (merchant_id TEXT PRIMARY KEY, name TEXT, category TEXT, city TEXT,
                                state TEXT, country TEXT, risk_level TEXT);
        CREATE TABLE transactions (txn_id TEXT PRIMARY KEY, account_id TEXT, merchant_id TEXT, amount REAL,
                                   timestamp TEXT, city TEXT, state TEXT, country TEXT, channel TEXT,
                                   device_id TEXT);
        CREATE TABLE travel_notices (account_id TEXT, destination TEXT, start_date TEXT, end_date TEXT);
        CREATE TABLE cities (city TEXT PRIMARY KEY, state TEXT, country TEXT, lat REAL, lon REAL);
        CREATE TABLE alerts (alert_id TEXT PRIMARY KEY, account_id TEXT, primary_txn_id TEXT,
                             related_txn_ids TEXT, rules TEXT, created_at TEXT);
        CREATE INDEX idx_txn_account ON transactions(account_id, timestamp);
    """)
    con.executemany("INSERT INTO accounts VALUES (?,?,?,?,?)",
                    [(a["account_id"], a["name"], a["home_city"], a["home_state"], a["opened_date"]) for a in accounts])
    con.executemany("INSERT INTO merchants VALUES (:merchant_id,:name,:category,:city,:state,:country,:risk_level)",
                    MERCHANTS)
    con.executemany("INSERT INTO transactions VALUES (:txn_id,:account_id,:merchant_id,:amount,:timestamp,"
                    ":city,:state,:country,:channel,:device_id)", txns)
    con.executemany("INSERT INTO travel_notices VALUES (:account_id,:destination,:start_date,:end_date)",
                    travel_notices)
    con.executemany("INSERT INTO cities VALUES (?,?,?,?,?)", [(c, *v) for c, v in CITIES.items()])
    con.commit()

    alerts = build_alerts(con, REVIEW_START, END)
    con.executemany("INSERT INTO alerts VALUES (:alert_id,:account_id,:primary_txn_id,:related_txn_ids,"
                    ":rules,:created_at)",
                    [{**a, "related_txn_ids": json.dumps(a["related_txn_ids"]), "rules": json.dumps(a["rules"])}
                     for a in alerts])
    con.commit()
    con.close()

    labels = {}
    for a in alerts:
        ids = a["related_txn_ids"]
        scen = next((scenario_of[i] for i in ids if i in scenario_of), "normal_noise")
        labels[a["alert_id"]] = {"is_fraud": any(i in fraud_ids for i in ids), "scenario": scen}
    LABELS_PATH.write_text(json.dumps(labels, indent=2))

    n_fraud = sum(v["is_fraud"] for v in labels.values())
    print(f"{len(accounts)} accounts, {len(txns)} transactions, {len(alerts)} alerts "
          f"({n_fraud} fraud, {len(alerts) - n_fraud} legit)")


if __name__ == "__main__":
    build_db()
