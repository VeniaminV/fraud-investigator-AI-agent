# Fraud Case Investigator

An AI agent that investigates flagged credit card transactions. A simple rules engine flags suspicious activity (and plenty of false alarms). For each alert, the agent decides on its own which tools to call, digs through the account's history, and writes a case report with a verdict, a 0-100 risk score, and the evidence behind it.

**Results:** on a 28-case labeled eval set, the agent caught **100% of fraud** with **88% precision**, vs. 50% precision for the rules engine alone, at about **$0.002 per investigation** on Claude Haiku 5.5.

Built with Python, the Claude API (tool use), SQLite, and FastAPI. Packaged with Docker for deployment on AWS Lambda.

https://ecy3pdmp63ckr4rrlhhdnehrgy0ilsud.lambda-url.us-west-2.on.aws/
<!-- Add a screenshot of the demo page here: ![Demo](docs/demo.png) -->
<img width="1122" height="677" alt="image" src="https://github.com/user-attachments/assets/d1935fc7-3516-4305-87ce-34014fed887d" />

## Results

Evaluated on 28 alerts (14 real fraud, 14 legit-but-suspicious) against a hidden answer key. "Flagged" counts both `fraud` and `needs_review` verdicts, since both send the case to a human.

| Version | Accuracy | Precision | Recall | False alarms | Missed fraud | Cost (28 alerts) |
| --- | --- | --- | --- | --- | --- | --- |
| Rules engine alone | 50% | 50% | 100% | 14 | 0 | $0 |
| Agent v1 | 82% | 74% | 100% | 5 | 0 | $0.06 |
| **Agent v2** | **93%** | **88%** | **100%** | **2** | **0** | **$0.06** |

**v1 → v2:** v1 never missed fraud, but it was overcautious. It sent 5 legit cases (new phones, big planned purchases, a road trip) to `needs_review` even after finding the innocent explanation. v2 adds one general rule to the system prompt: use `needs_review` only when there's real evidence pointing both ways. That cut false alarms from 5 to 2 with no drop in recall.

Average 6.1 tool calls and about 9 seconds per investigation. Results vary slightly between runs because LLM output isn't deterministic.

## How it works

1. **Synthetic data** (`generate_data.py`): 60 customers and about 8,600 transactions over 3 months, each customer with their own spending habits. The last week has 14 planted fraud cases (card testing, account takeover, impossible travel, crypto and gift card sprees) and 12 innocent cases that look suspicious (travel with a notice, big planned purchases, new phones, road trips, high spenders).
2. **Rules engine** (`flagger.py`): 7 simple rules flag transactions and group them into 28 alerts. Half are false alarms.
3. **Agent** (`agent.py`): a hand-written tool-use loop. Claude gets the alert and picks from 7 tools in `tools.py`, the code runs them against SQLite, and results go back until Claude calls `submit_report` with a structured verdict.
4. **Evals** (`run_evals.py`): runs every alert and scores the agent against the labels.
5. **Web app** (`app.py`): FastAPI API plus a demo page. Mangum adapts it for AWS Lambda.

### Design decisions

- **Tools do the math.** Things like "22.6x the median" and "implied travel speed of 1,083 mph" are computed in Python, so the model judges evidence instead of doing arithmetic.
- **No peeking at the future.** Every tool query is cut off at the alert's timestamp.
- **No data leakage.** The answer key lives outside the database and is excluded from the Docker image.
- **Structured output.** The final report comes through a strict tool schema, so the verdict and score are always valid fields.
- **Guard rails.** Read-only database, a 12-step limit per investigation, and errors returned to the model so it can retry.

## Run it locally

```
python -m venv .venv
.venv\Scripts\activate          # Windows (Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # then paste your Claude API key into .env
python agent.py A004            # investigate one alert
python run_evals.py             # score all 28
python -m uvicorn app:app --reload   # demo page at http://127.0.0.1:8000
```

`data/fraud.db` and `data/labels.json` are included. To rebuild them, run `python generate_data.py`.

## Deployment

Planned: Docker image in Amazon ECR, running on AWS Lambda behind a Function URL, with the API key in Secrets Manager. <!-- Update this section once deployed, and add the live link. -->

## What I'd do next

- Compare models (Haiku vs. Sonnet) on accuracy, cost, and speed
- Add prompt caching for the system prompt and tool schemas
- Add a cross-account tool to catch devices shared across accounts (fraud rings)
- Add analyst approve/override buttons and log human decisions next to the agent's
