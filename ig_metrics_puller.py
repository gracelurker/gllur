#!/usr/bin/env python3
"""
Instagram metrics puller  —  the data layer for the dashboard.

Built to scale from "just my own account" to "many client accounts" without a
rewrite. Every account is one entry in accounts.json; the same code runs whether
there's 1 or 100. Nothing about a specific account is hardcoded.

What it does per run, per account:
  1. (optional) refreshes the long-lived token so it never hits the 60-day wall
  2. pulls account-level numbers (followers, reach, profile views, ...)
  3. pulls the recent posts + per-post insights (saves, shares, reach, ER)
  4. writes a full snapshot JSON the dashboard reads for its "latest" view
  5. appends one compact row to a history file  ->  this is what powers the
     trend lines and forecasting. The API only ever gives you "right now";
     the history file is how "right now" turns into "over time".

Setup you do once (see the chat for the full walkthrough):
  - IG account = Business or Creator, linked to a Facebook Page
  - a Meta "Business" app with instagram_basic + instagram_manage_insights
  - put your app id/secret in env vars, and each account's id/token in accounts.json

Run:  python ig_metrics_puller.py
"""

import os
import json
import time
import datetime as dt
from pathlib import Path

import requests  # pip install requests

# ── config ──────────────────────────────────────────────────────────────────
GRAPH_VERSION = "v22.0"                       # pin the version; Meta drops metrics between versions
GRAPH_HOST    = "https://graph.facebook.com"  # Facebook-Login flow. IG-Login flow uses graph.instagram.com
BASE          = f"{GRAPH_HOST}/{GRAPH_VERSION}"

# App creds live in env vars, never in the file (this file may end up in git).
APP_ID     = os.environ.get("META_APP_ID", "")
APP_SECRET = os.environ.get("META_APP_SECRET", "")

DATA_DIR = Path(__file__).parent / "data"     # everything gets written here
ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"

# Account-level metrics to try. Each is requested independently and failures are
# skipped, so if Meta retires one in a future version the rest still come back.
ACCOUNT_METRICS = ["reach", "profile_views", "website_clicks", "follower_count"]

# Per-post metrics. Reels expose a slightly different set, handled below.
MEDIA_METRICS_FEED  = ["reach", "saved", "shares", "total_interactions"]
MEDIA_METRICS_REEL  = ["reach", "saved", "shares", "total_interactions", "plays"]

POSTS_PER_PULL = 25   # how many recent posts to grab each run
SLEEP_BETWEEN_ACCOUNTS = 1.0   # be polite to the rate limiter


# ── low-level helpers ───────────────────────────────────────────────────────
def _get(path, params):
    """One GET against the graph. Returns parsed JSON, raises on hard errors."""
    params = {**params}
    r = requests.get(f"{BASE}/{path}", params=params, timeout=30)
    body = r.json()
    if "error" in body:
        raise RuntimeError(body["error"].get("message", str(body["error"])))
    return body


def refresh_long_lived_token(current_token):
    """
    Re-exchange a long-lived token for a fresh 60-day one. Safe to run every
    time — Meta lets you refresh any time after the first 24h. Returns the new
    token (or the old one if refresh isn't configured / fails).
    """
    if not (APP_ID and APP_SECRET):
        return current_token  # can't refresh without app creds; fine for quick tests
    try:
        body = _get("oauth/access_token", {
            "grant_type": "fb_exchange_token",
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
            "fb_exchange_token": current_token,
        })
        return body.get("access_token", current_token)
    except Exception as e:
        print(f"    token refresh skipped: {e}")
        return current_token


# ── account-level pulls ─────────────────────────────────────────────────────
def get_profile(ig_user_id, token):
    """Static-ish profile counts: followers, following, total posts, username."""
    body = _get(ig_user_id, {
        "fields": "username,followers_count,follows_count,media_count",
        "access_token": token,
    })
    return {
        "username": body.get("username"),
        "followers_count": body.get("followers_count"),
        "follows_count": body.get("follows_count"),
        "media_count": body.get("media_count"),
    }


def get_account_insights(ig_user_id, token):
    """
    Try each account metric on its own so one retired metric can't sink the run.
    Returns {metric: value} using the most recent data point for each.
    """
    out = {}
    for metric in ACCOUNT_METRICS:
        try:
            body = _get(f"{ig_user_id}/insights", {
                "metric": metric,
                "period": "day",
                "access_token": token,
            })
            values = body.get("data", [{}])[0].get("values", [])
            if values:
                out[metric] = values[-1].get("value")
        except Exception as e:
            print(f"    account metric '{metric}' unavailable: {e}")
    return out


# ── post-level pulls ────────────────────────────────────────────────────────
def get_recent_media(ig_user_id, token, limit=POSTS_PER_PULL):
    body = _get(f"{ig_user_id}/media", {
        "fields": "id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count",
        "limit": limit,
        "access_token": token,
    })
    return body.get("data", [])


def get_media_insights(media, token):
    is_reel = media.get("media_product_type") == "REELS"
    metrics = MEDIA_METRICS_REEL if is_reel else MEDIA_METRICS_FEED
    out = {}
    try:
        body = _get(f"{media['id']}/insights", {
            "metric": ",".join(metrics),
            "access_token": token,
        })
        for item in body.get("data", []):
            vals = item.get("values", [{}])
            out[item["name"]] = vals[0].get("value") if vals else None
    except Exception as e:
        # Reels/feed metric sets shift; retry with the safe common set once.
        try:
            body = _get(f"{media['id']}/insights", {
                "metric": "reach,saved",
                "access_token": token,
            })
            for item in body.get("data", []):
                vals = item.get("values", [{}])
                out[item["name"]] = vals[0].get("value") if vals else None
        except Exception as e2:
            print(f"    post {media['id']} insights unavailable: {e2}")
    return out


def engagement_rate(media, insights, followers):
    """
    ER = interactions / reach, in %. Falls back to interactions / followers when
    reach is missing (older posts sometimes drop reach). Returns None if neither.
    """
    interactions = (media.get("like_count") or 0) + (media.get("comments_count") or 0) \
                   + (insights.get("saved") or 0) + (insights.get("shares") or 0)
    denom = insights.get("reach") or followers
    if not denom:
        return None
    return round(interactions / denom * 100, 2)


# ── orchestration ───────────────────────────────────────────────────────────
def pull_account(acct):
    """Full pull for one account. Returns (snapshot_dict, updated_token)."""
    label = acct.get("label", acct["ig_user_id"])
    print(f"  pulling {label} ...")

    token = refresh_long_lived_token(acct["access_token"])

    profile = get_profile(acct["ig_user_id"], token)
    followers = profile.get("followers_count") or 0
    acct_insights = get_account_insights(acct["ig_user_id"], token)

    media_rows = []
    for m in get_recent_media(acct["ig_user_id"], token):
        ins = get_media_insights(m, token)
        media_rows.append({
            "id": m["id"],
            "timestamp": m.get("timestamp"),
            "media_type": m.get("media_product_type") or m.get("media_type"),
            "caption": (m.get("caption") or "")[:280],
            "permalink": m.get("permalink"),
            "like_count": m.get("like_count"),
            "comments_count": m.get("comments_count"),
            "reach": ins.get("reach"),
            "saved": ins.get("saved"),
            "shares": ins.get("shares"),
            "plays": ins.get("plays"),
            "engagement_rate": engagement_rate(m, ins, followers),
        })

    snapshot = {
        "label": label,
        "ig_user_id": acct["ig_user_id"],
        "pulled_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "profile": profile,
        "account_insights": acct_insights,
        "media": media_rows,
    }
    return snapshot, token


def _merge_preserve(existing, fresh):
    """
    Overlay freshly-pulled values onto whatever is already saved, WITHOUT wiping
    fields the API couldn't return. This is what lets the auto-feed update the
    allowed data (followers, post count, per-post likes/comments) while keeping
    manually-entered insights (reach, engagement, best-times, the facebook block)
    intact until App Review unlocks them.
    """
    if not existing:
        return fresh
    out = dict(existing)                       # start from saved (keeps manual keys)
    out["label"] = fresh.get("label", out.get("label"))
    out["ig_user_id"] = fresh.get("ig_user_id", out.get("ig_user_id"))
    out["pulled_at"] = fresh.get("pulled_at")
    # profile: overlay non-null pulled values
    out["profile"] = {**existing.get("profile", {}),
                      **{k: v for k, v in fresh.get("profile", {}).items() if v is not None}}
    # account insights: overlay only metrics that actually came back (non-null)
    out["account_insights"] = {**existing.get("account_insights", {}),
                               **{k: v for k, v in fresh.get("account_insights", {}).items() if v is not None}}
    # media: if we pulled any posts, use them (real likes/comments); else keep saved
    if fresh.get("media"):
        out["media"] = fresh["media"]
    return out


def write_outputs(acct, snapshot):
    """Write the full 'latest' snapshot, and append one row to the history series."""
    acct_dir = DATA_DIR / acct["ig_user_id"]
    acct_dir.mkdir(parents=True, exist_ok=True)

    latest_file = acct_dir / "latest.json"
    existing = json.loads(latest_file.read_text()) if latest_file.exists() else None
    snapshot = _merge_preserve(existing, snapshot)

    # full latest snapshot (what the dashboard reads for current KPIs + post lists)
    latest_file.write_text(json.dumps(snapshot, indent=2))

    # compact daily row appended to history  ->  feeds trends + forecasting
    history_file = acct_dir / "history.json"
    history = json.loads(history_file.read_text()) if history_file.exists() else []
    ins = snapshot["account_insights"]
    history.append({
        "date": dt.date.today().isoformat(),
        "followers": snapshot["profile"].get("followers_count"),
        "posts": snapshot["profile"].get("media_count"),
        "reach": ins.get("reach"),
        "profile_views": ins.get("profile_views"),
        "website_clicks": ins.get("website_clicks"),
    })
    history_file.write_text(json.dumps(history, indent=2))


def load_accounts():
    """
    Where the account list comes from, in priority order:
      1. env GLLUR_ACCOUNTS  — a JSON array string (used by GitHub Actions secrets)
      2. env IG_USER_ID + IG_ACCESS_TOKEN — the simplest single-account cloud setup
      3. accounts.json on disk — local use
    Returns (accounts, persist). persist is False for the env cases so tokens are
    never written to disk / committed to a repo in CI.
    """
    env_json = os.environ.get("GLLUR_ACCOUNTS")
    if env_json:
        return json.loads(env_json), False

    if os.environ.get("IG_USER_ID") and os.environ.get("IG_ACCESS_TOKEN"):
        return [{
            "label": os.environ.get("IG_LABEL", "Account"),
            "ig_user_id": os.environ["IG_USER_ID"],
            "access_token": os.environ["IG_ACCESS_TOKEN"],
        }], False

    if ACCOUNTS_FILE.exists():
        return json.loads(ACCOUNTS_FILE.read_text()), True

    # first-run scaffold so it's obvious what to fill in
    ACCOUNTS_FILE.write_text(json.dumps([
        {
            "label": "My Test Account",
            "ig_user_id": "REPLACE_WITH_YOUR_IG_USER_ID",
            "access_token": "REPLACE_WITH_YOUR_LONG_LIVED_TOKEN"
        }
    ], indent=2))
    print(f"Created {ACCOUNTS_FILE.name} — fill in your ig_user_id + token, then re-run.")
    return None, True


def main():
    accounts, persist = load_accounts()
    if accounts is None:
        return

    changed = False
    for acct in accounts:
        try:
            snapshot, new_token = pull_account(acct)
            write_outputs(acct, snapshot)
            if new_token != acct["access_token"]:
                acct["access_token"] = new_token   # persist refreshed token
                changed = True
            print(f"    ok — {len(snapshot['media'])} posts, "
                  f"{snapshot['profile'].get('followers_count')} followers")
        except Exception as e:
            print(f"    FAILED for {acct.get('label', acct['ig_user_id'])}: {e}")
        time.sleep(SLEEP_BETWEEN_ACCOUNTS)

    if changed and persist:
        ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2))

    print("Done. Snapshots + history written under ./data/")


if __name__ == "__main__":
    main()
