"""
WSP Yotpo Review Dashboard
==========================
Flask app that pulls reviews from Yotpo API, serves a live dashboard,
auto-refreshes on the 1st of each month, and emails the team a link.

Environment variables (set in Render dashboard):
    YOTPO_APP_KEY       — your Yotpo App Key
    YOTPO_SECRET_KEY    — your Yotpo Secret Key
    EMAIL_SENDER        — Gmail address to send from
    EMAIL_PASSWORD      — Gmail App Password (myaccount.google.com/apppasswords)
    EMAIL_RECIPIENTS    — comma-separated recipient emails
    DASHBOARD_URL       — public URL, e.g. https://wsp-reviews.onrender.com
"""

import os
import json
import time
import smtplib
import requests
import threading
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, render_template, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

YOTPO_APP_KEY    = os.environ.get("YOTPO_APP_KEY", "")
YOTPO_SECRET     = os.environ.get("YOTPO_SECRET_KEY", "")
EMAIL_SENDER     = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD   = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_RECIPIENTS = [e.strip() for e in os.environ.get("EMAIL_RECIPIENTS", "").split(",") if e.strip()]
DASHBOARD_URL    = os.environ.get("DASHBOARD_URL", "http://localhost:5000")

_cache = {"data": None, "refreshed_at": None}
_lock  = threading.Lock()

# ── Yotpo API ─────────────────────────────────────────────────────────────────

def get_yotpo_token():
    url = "https://api.yotpo.com/oauth/token"
    r = requests.post(url, json={
        "client_id": YOTPO_APP_KEY,
        "client_secret": YOTPO_SECRET,
        "grant_type": "client_credentials"
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_all_reviews(token, days_back=1825):
    since = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    reviews = []
    page = 1
    while True:
        r = requests.get(
            f"https://api.yotpo.com/v1/apps/{YOTPO_APP_KEY}/reviews",
            params={"utoken": token, "count": 100, "page": page,
                    "since_date": since},
            timeout=30
        )
        r.raise_for_status()
        batch = r.json().get("reviews", [])
        if not batch:
            break
        reviews.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        time.sleep(0.2)
    return reviews


def fetch_sku_map(token):
    """Fetch all products and return a dict of {sku: product_name}."""
    sku_map = {}
    page = 1
    while True:
        try:
            r = requests.get(
                f"https://api.yotpo.com/v1/apps/{YOTPO_APP_KEY}/products",
                params={"utoken": token, "count": 100, "page": page},
                timeout=30
            )
            r.raise_for_status()
            body = r.json()
            # Log first page so we can see the structure
            if page == 1:
                print(f"Products API response keys: {list(body.keys())}")
                sample = (body.get("products") or body.get("response", {}).get("products") or [])
                if sample:
                    print(f"Sample product keys: {list(sample[0].keys())}")

            products = (body.get("products") or
                        body.get("response", {}).get("products") or [])
            if not products:
                break
            for p in products:
                # Try every plausible SKU field
                sku = (str(p.get("external_product_id") or p.get("external_id") or
                           p.get("sku") or p.get("domain_key") or p.get("id") or "")).strip()
                name = (p.get("name") or p.get("title") or "").strip()
                if sku and name:
                    sku_map[sku] = name
            if len(products) < 100:
                break
            page += 1
            time.sleep(0.2)
        except Exception as e:
            print(f"Error fetching products page {page}: {e}")
            break
    print(f"Loaded {len(sku_map)} products from Yotpo. Keys sample: {list(sku_map.items())[:5]}")
    return sku_map


# ── Data Processing ───────────────────────────────────────────────────────────

def process_reviews(reviews, sku_map=None):
    if sku_map is None:
        sku_map = {}
    now       = datetime.utcnow()
    ytd_start = datetime(now.year, 1, 1)
    monthly   = defaultdict(lambda: {"count":0,"scores":[],"pos":0,"neg":0,"neut":0,"rows":[]})
    ytd_rows  = []
    all_rows  = []

    for r in reviews:
        try:
            created = datetime.strptime(r["created_at"][:10], "%Y-%m-%d")
        except Exception:
            continue
        score     = int(r.get("score", 0))
        sku       = str(r.get("sku") or r.get("product_id") or "")
        product   = sku_map.get(sku) or r.get("product_title") or (f"SKU {sku}" if sku else "Unknown")
        sentiment = float(r.get("sentiment", 0) or 0)
        row = {
            "score": score, "product": product,
            "status": r.get("status", ""),
            "sentiment": sentiment, "created": created,
            "date_str": created.strftime("%Y-%m-%d"),
            "name":    r.get("name") or r.get("reviewer", {}).get("display_name", "Anonymous"),
            "title":   r.get("title", ""),
            "content": r.get("content", ""),
        }
        all_rows.append(row)
        if created >= ytd_start:
            ytd_rows.append(row)
        key = created.strftime("%Y-%m")
        monthly[key]["count"] += 1
        monthly[key]["scores"].append(score)
        monthly[key]["rows"].append(row)
        if score >= 4:   monthly[key]["pos"]  += 1
        elif score <= 2: monthly[key]["neg"]  += 1
        else:            monthly[key]["neut"] += 1

    last12_keys = sorted(monthly.keys())[-12:]
    monthly_data = []
    for k in last12_keys:
        m = monthly[k]
        avg = round(sum(m["scores"]) / len(m["scores"]), 2) if m["scores"] else 0
        low = sorted([r for r in m["rows"] if r["score"] <= 2],
                     key=lambda x: x["created"], reverse=True)
        prod_scores = defaultdict(list)
        for r in m["rows"]:
            prod_scores[r["product"]].append(r["score"])
        top = sorted([(p, round(sum(s)/len(s),2), len(s)) for p,s in prod_scores.items()],
                     key=lambda x: -x[2])[:10]
        monthly_data.append({
            "month": k,
            "label": datetime.strptime(k, "%Y-%m").strftime("%b %y"),
            "count": m["count"], "avg": avg,
            "pos": m["pos"], "neg": m["neg"], "neut": m["neut"],
            "low_reviews": [{"score":r["score"],"name":r["name"],"date":r["date_str"],
                             "product":r["product"],"title":r["title"],
                             "content":r["content"][:200]} for r in low],
            "top_products": [[p,a,c] for p,a,c in top],
        })

    def summarise(rows):
        if not rows:
            return {}
        scores   = [r["score"] for r in rows]
        sents    = [r["sentiment"] for r in rows]
        products = defaultdict(list)
        for r in rows:
            products[r["product"]].append(r["score"])
        top_products = sorted(
            [(p, round(sum(s)/len(s),2), len(s)) for p,s in products.items()],
            key=lambda x: -x[2]
        )[:10]
        low_reviews = sorted(
            [r for r in rows if r["score"] <= 2],
            key=lambda x: x["created"], reverse=True
        )[:10]
        total = len(scores)
        pos   = sum(1 for s in scores if s >= 4)
        neut  = sum(1 for s in scores if s == 3)
        neg   = sum(1 for s in scores if s <= 2)
        return {
            "total": total,
            "avg_score":     round(sum(scores)/total, 2),
            "avg_sentiment": round(sum(sents)/total, 3) if sents else 0,
            "positive": pos, "neutral": neut, "negative": neg,
            "pos_pct":  round(pos/total*100, 1),
            "neg_pct":  round(neg/total*100, 1),
            "stars":    {str(k): v for k,v in Counter(scores).items()},
            "statuses": dict(Counter(r["status"] for r in rows)),
            "top_products": [[p,a,c] for p,a,c in top_products],
            "low_reviews": [
                {"score": r["score"], "name": r["name"], "date": r["date_str"],
                 "product": r["product"], "title": r["title"],
                 "content": r["content"][:200]}
                for r in low_reviews
            ],
        }

    return {
        "ytd":     summarise(ytd_rows),
        "full":    summarise(all_rows),
        "monthly": monthly_data,
        "refreshed_at": datetime.utcnow().strftime("%b %d, %Y %H:%M UTC"),
        "year":    now.year,
    }


# ── Cache ─────────────────────────────────────────────────────────────────────

def refresh_data():
    print(f"[{datetime.utcnow()}] Refreshing Yotpo data...")
    try:
        token   = get_yotpo_token()
        reviews = fetch_all_reviews(token)
        sku_map = fetch_sku_map(token)
        data    = process_reviews(reviews, sku_map)
        with _lock:
            _cache["data"]         = data
            _cache["refreshed_at"] = datetime.utcnow()
        print(f"[{datetime.utcnow()}] Done — {data['full']['total']} reviews loaded.")
        return True
    except Exception as e:
        print(f"[{datetime.utcnow()}] ERROR: {e}")
        return False


def load_sample_data():
    monthly = [
        {"month":"2025-05","label":"May 25","count":622,"avg":4.80,"pos":594,"neg":20,"neut":8},
        {"month":"2025-06","label":"Jun 25","count":504,"avg":4.71,"pos":469,"neg":19,"neut":16},
        {"month":"2025-07","label":"Jul 25","count":500,"avg":4.80,"pos":479,"neg":12,"neut":9},
        {"month":"2025-08","label":"Aug 25","count":411,"avg":4.76,"pos":387,"neg":14,"neut":10},
        {"month":"2025-09","label":"Sep 25","count":314,"avg":4.82,"pos":301,"neg":8, "neut":5},
        {"month":"2025-10","label":"Oct 25","count":306,"avg":4.75,"pos":287,"neg":10,"neut":9},
        {"month":"2025-11","label":"Nov 25","count":281,"avg":4.81,"pos":267,"neg":6, "neut":8},
        {"month":"2025-12","label":"Dec 25","count":346,"avg":4.74,"pos":326,"neg":14,"neut":6},
        {"month":"2026-01","label":"Jan 26","count":355,"avg":4.80,"pos":339,"neg":9, "neut":7},
        {"month":"2026-02","label":"Feb 26","count":257,"avg":4.78,"pos":244,"neg":7, "neut":6},
        {"month":"2026-03","label":"Mar 26","count":265,"avg":4.78,"pos":253,"neg":6, "neut":6},
        {"month":"2026-04","label":"Apr 26","count":157,"avg":4.87,"pos":151,"neg":2, "neut":4},
    ]
    summary = {
        "total":4318,"avg_score":4.79,"avg_sentiment":0.78,
        "positive":4133,"neutral":47,"negative":138,
        "pos_pct":95.8,"neg_pct":3.2,
        "stars":{"5":"3900","4":"233","3":"47","2":"72","1":"66"},
        "statuses":{"Published":2100,"Pending":1400,"Rejected":818},
        "top_products":[
            ["Wall Street Prep Premium Package",4.86,4347],
            ["Financial Statement Modeling",4.88,2523],
            ["Accounting Crash Course",4.84,2403],
            ["Excel Crash Course",4.73,2199],
            ["Excel Basics (Mac)",4.94,1411],
            ["DCF Modeling",4.90,1259],
            ["Real Estate Financial Modeling",4.88,860],
            ["Private Equity Masterclass",4.78,680],
            ["LBO Modeling",4.70,643],
            ["PowerPoint Crash Course",4.93,562],
        ],
        "low_reviews":[
            {"score":2,"name":"Chloe T.","date":"2026-04-04","product":"Financial Statement Modeling","title":"More practice needed","content":"More excel practices needed"},
            {"score":1,"name":"Matthew C.","date":"2026-04-01","product":"Analyzing Financial Reports","title":"Not engaging","content":"It was boring"},
        ],
    }
    return {
        "ytd":summary,"full":summary,"monthly":monthly,
        "refreshed_at":"Sample data — add API keys to load live data",
        "year":datetime.utcnow().year,
    }


def get_data():
    with _lock:
        if _cache["data"]:
            return _cache["data"]
    if YOTPO_APP_KEY and YOTPO_SECRET:
        refresh_data()
        with _lock:
            if _cache["data"]:
                return _cache["data"]
    return load_sample_data()


# ── Email ─────────────────────────────────────────────────────────────────────

def send_monthly_email():
    if not EMAIL_SENDER or not EMAIL_RECIPIENTS:
        return
    now  = datetime.utcnow().strftime("%B %Y")
    data = get_data()
    ytd  = data["ytd"]
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family:Georgia,serif;max-width:600px;margin:0 auto;padding:32px 24px;color:#1a1a2e;background:#fafaf8">
  <div style="border-bottom:2px solid #1a1a2e;padding-bottom:20px;margin-bottom:28px">
    <h1 style="margin:0;font-size:22px;font-weight:normal">Monthly Review Report</h1>
    <p style="margin:6px 0 0;color:#666;font-size:14px">Wall Street Prep · {now}</p>
  </div>
  <table style="width:100%;border-collapse:collapse;margin-bottom:28px"><tr>
    <td style="padding:16px;background:#1a1a2e;border-radius:8px;text-align:center;width:22%">
      <div style="font-size:26px;font-weight:bold;color:#fff">{ytd.get('total',0):,}</div>
      <div style="font-size:11px;color:#aaa;margin-top:4px">Total reviews</div></td>
    <td style="width:4%"></td>
    <td style="padding:16px;background:#eaf7f1;border-radius:8px;text-align:center;width:22%">
      <div style="font-size:26px;font-weight:bold;color:#1D9E75">{ytd.get('avg_score',0)}</div>
      <div style="font-size:11px;color:#0F6E56;margin-top:4px">Avg rating / 5</div></td>
    <td style="width:4%"></td>
    <td style="padding:16px;background:#e6f1fb;border-radius:8px;text-align:center;width:22%">
      <div style="font-size:26px;font-weight:bold;color:#378ADD">{ytd.get('pos_pct',0)}%</div>
      <div style="font-size:11px;color:#185FA5;margin-top:4px">Positive rate</div></td>
    <td style="width:4%"></td>
    <td style="padding:16px;background:#fcebeb;border-radius:8px;text-align:center;width:22%">
      <div style="font-size:26px;font-weight:bold;color:#E24B4A">{ytd.get('negative',0)}</div>
      <div style="font-size:11px;color:#A32D2D;margin-top:4px">Need attention</div></td>
  </tr></table>
  <div style="text-align:center;margin:28px 0">
    <a href="{DASHBOARD_URL}" style="background:#1a1a2e;color:#fff;padding:14px 32px;border-radius:6px;text-decoration:none;font-size:15px">Open Dashboard →</a>
  </div>
  <div style="border-top:1px solid #e8e8e5;padding-top:16px;font-size:12px;color:#aaa;text-align:center">
    WSP Review Dashboard · Auto-generated {datetime.utcnow().strftime("%Y-%m-%d")}
  </div>
</body></html>"""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"WSP Monthly Review Report — {now}"
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = ", ".join(EMAIL_RECIPIENTS)
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENTS, msg.as_string())
        print(f"Email sent to {EMAIL_RECIPIENTS}")
    except Exception as e:
        print(f"Email error: {e}")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/api/data")
def api_data():
    return jsonify(get_data())

@app.route("/api/data/month/<int:year>/<int:month>")
def api_data_month(year, month):
    data = get_data()
    # Filter all_rows isn't stored — re-summarise from monthly list
    # Find matching monthly entry
    key = f"{year}-{month:02d}"
    monthly_entry = next((m for m in data["monthly"] if m["month"] == key), None)
    if not monthly_entry:
        return jsonify({"error": "No data for that month"}), 404
    return jsonify(monthly_entry)

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    success = refresh_data()
    return jsonify({"ok": success})

@app.route("/api/send-email", methods=["POST"])
def api_send_email():
    send_monthly_email()
    return jsonify({"ok": True})

@app.route("/api/debug-review")
def api_debug_review():
    try:
        token = get_yotpo_token()
        r = requests.get(
            f"https://api.yotpo.com/v1/apps/{YOTPO_APP_KEY}/reviews",
            params={"utoken": token, "count": 1, "page": 1},
            timeout=30
        )
        r.raise_for_status()
        reviews = r.json().get("reviews", [])
        return jsonify(reviews[0] if reviews else {})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/debug-products")
def api_debug_products():
    try:
        token = get_yotpo_token()
        results = {}
        # Try endpoint 1 — v1 products
        r1 = requests.get(
            f"https://api.yotpo.com/v1/apps/{YOTPO_APP_KEY}/products",
            params={"utoken": token, "count": 3, "page": 1},
            timeout=30
        )
        results["v1_products_status"] = r1.status_code
        results["v1_products"] = r1.text[:500]

        # Try endpoint 2 — product by SKU
        r2 = requests.get(
            f"https://api.yotpo.com/v1/widget/{YOTPO_APP_KEY}/products/3018/reviews.json",
            timeout=30
        )
        results["widget_sku_status"] = r2.status_code
        try:
            body2 = r2.json()
            prods = body2.get("response", {}).get("products", [])
            results["widget_product"] = prods[0] if prods else body2
        except Exception:
            results["widget_sku_raw"] = r2.text[:300]

        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)})


# ── Boot ──────────────────────────────────────────────────────────────────────

def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(refresh_data,       "cron", day=1, hour=6, minute=0)
    scheduler.add_job(send_monthly_email, "cron", day=1, hour=7, minute=0)
    scheduler.start()

if __name__ == "__main__":
    start_scheduler()
    if YOTPO_APP_KEY and YOTPO_SECRET:
        threading.Thread(target=refresh_data, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
