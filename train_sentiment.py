#!/opt/homebrew/bin/python3.14
"""Train a pump.fun sentiment classifier from the Pumpdotstudio dataset."""
import urllib.request, json, io, pickle, math
import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.metrics import classification_report

URL = "https://huggingface.co/datasets/Pumpdotstudio/pump-fun-sentiment-100k/resolve/main/data/train-2026-03-04T16-18-11.jsonl"
FEATURES = ["market_cap","volume_24h","liquidity","holder_count",
            "top10_holder_pct","buys_24h","sells_24h","bonding_progress"]
LABEL_MAP = {"bullish": 2, "neutral": 1, "bearish": 0}

print("Downloading dataset (50k rows)…")
X, y = [], []
with urllib.request.urlopen(URL, timeout=120) as resp:
    for raw in io.TextIOWrapper(resp, encoding="utf-8"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        label = d.get("sentiment", "")
        if label not in LABEL_MAP:
            continue
        row = []
        for f in FEATURES:
            v = d.get(f, 0) or 0
            # log-transform skewed financial features
            if f in ("market_cap","volume_24h","liquidity"):
                v = math.log1p(max(v, 0))
            row.append(float(v))
        X.append(row)
        y.append(LABEL_MAP[label])

print(f"Loaded {len(X)} samples. Label dist: {dict(zip(*np.unique(y, return_counts=True)))}")

X = np.array(X, dtype=np.float32)
y = np.array(y)

# Split 90/10 for quick eval
split = int(len(X) * 0.9)
X_tr, X_te = X[:split], X[split:]
y_tr, y_te = y[:split], y[split:]

print("Training GradientBoostingClassifier…")
model = Pipeline([
    ("scaler", StandardScaler()),
    ("clf", GradientBoostingClassifier(n_estimators=150, max_depth=4, learning_rate=0.1, random_state=42))
])
model.fit(X_tr, y_tr)

print("Eval on 10% holdout:")
print(classification_report(y_te, model.predict(X_te), target_names=["bearish","neutral","bullish"]))

out = "/Users/jpreddy/sniper/sentiment_model.pkl"
with open(out, "wb") as f:
    pickle.dump({"model": model, "features": FEATURES, "label_map": LABEL_MAP}, f)
print(f"Saved model to {out}")
