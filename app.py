import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from mangum import Mangum

from agent import investigate
from tools import get_alert, list_alerts

app = FastAPI(title="Fraud Case Investigator")
INDEX = Path(__file__).parent / "static" / "index.html"


@app.get("/", response_class=HTMLResponse)
def home():
    return INDEX.read_text()


@app.get("/alerts")
def alerts():
    return list_alerts()


@app.get("/alerts/{alert_id}")
def alert(alert_id: str):
    try:
        return get_alert(alert_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/investigate/{alert_id}")
def run_investigation(alert_id: str, x_demo_key: str | None = Header(default=None)):
    demo_key = os.getenv("DEMO_KEY")
    if demo_key and x_demo_key != demo_key:
        raise HTTPException(401, "Wrong or missing demo key")
    try:
        get_alert(alert_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return investigate(alert_id)


@app.get("/health")
def health():
    return {"ok": True}


handler = Mangum(app, lifespan="off")
