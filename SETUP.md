# WSP Review Dashboard — Setup Guide

## What this is
A Flask web app that:
- Pulls reviews from the Yotpo API automatically
- Serves a live dashboard at a permanent URL
- Refreshes data on the 1st of every month
- Emails your team a link after each refresh

---

## Step 1 — Push to GitHub

1. Create a new repo at github.com (e.g. `wsp-reviews`)
2. Upload all files in this folder to it (drag & drop works in the GitHub UI)
   - app.py
   - requirements.txt
   - Procfile
   - templates/dashboard.html

---

## Step 2 — Deploy on Render

1. Go to render.com and sign up (free)
2. Click **New → Web Service**
3. Connect your GitHub repo
4. Fill in:
   - **Name**: wsp-reviews (or anything you like)
   - **Runtime**: Python 3
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4`
5. Click **Create Web Service**

Render will give you a URL like: `https://wsp-reviews.onrender.com`

---

## Step 3 — Add environment variables

In Render dashboard → your service → **Environment** tab, add:

| Key                | Value                                      |
|--------------------|--------------------------------------------|
| YOTPO_APP_KEY      | Your Yotpo App Key                         |
| YOTPO_SECRET_KEY   | Your Yotpo Secret Key                      |
| EMAIL_SENDER       | your-email@gmail.com                       |
| EMAIL_PASSWORD     | Your Gmail App Password (see note below)   |
| EMAIL_RECIPIENTS   | email1@co.com,email2@co.com                |
| DASHBOARD_URL      | https://wsp-reviews.onrender.com           |

**Gmail App Password**: Go to myaccount.google.com/apppasswords
(You need 2FA enabled on your Google account first)

**Yotpo API keys**: Yotpo dashboard → Settings → Store Settings → API

---

## Step 4 — Test it

1. Visit your Render URL — you should see the dashboard with sample data
2. Click **↻ Refresh** in the top right to trigger a live Yotpo API pull
3. Send a test email: the scheduler runs on the 1st of each month automatically,
   or you can test it by temporarily calling `send_monthly_email()` from the Python shell

---

## How data refreshes work

- **Automatic**: On the 1st of every month at 6am UTC, the app fetches fresh data
  from Yotpo and updates the dashboard. At 7am UTC it emails the team a link.
- **Manual**: Click the **↻ Refresh** button on the dashboard at any time,
  or POST to `/api/refresh`

---

## Free tier notes (Render)

Render's free tier spins down after 15 minutes of inactivity.
The first visit after a sleep takes ~30 seconds to wake up.

To avoid this, upgrade to the $7/month "Starter" plan, which keeps the
service always-on. For an internal team dashboard this is worth it.
