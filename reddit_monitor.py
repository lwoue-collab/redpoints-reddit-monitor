"""
Red Points Reddit LLM Thread Monitor
=====================================
Weekly script that:
  1. Pulls Omnia citation data across all monitored prompts
  2. Filters for Reddit source URLs only
  3. Identifies net-new threads vs. already seen
  4. Tags each thread TOFU / MOFU / BOFU from the citing prompt
  5. Flags competitive gaps (competitor cited, RP absent)
  6. Generates a suggested commenting angle via Claude API
  7. Checks Slack Canvas action log for already-actioned threads
  8. Builds a filterable HTML dashboard (GitHub Pages)
  9. Sends a weekly Slack alert to the team group DM

Canvas action log: https://red-points.slack.com/docs/T071J43AM/F0B1EAQ99U6

Run: python reddit_monitor.py
Deploy: GitHub Actions (see .github/workflows/reddit_monitor.yml)
"""

import os
import json
import datetime
import logging
import re
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

OMNIA_TOKEN       = os.getenv("OMNIA_TOKEN")
OMNIA_BRAND_ID    = os.getenv("OMNIA_BRAND_ID", "03adaaca-5265-404e-b4b1-bbaea0ce73f9")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")    # webhook for the group DM channel
SLACK_BOT_TOKEN   = os.getenv("SLACK_BOT_TOKEN")      # xoxb- token, needs canvases:read scope
REPORT_URL        = os.getenv("REPORT_URL", "https://lwoue-collab.github.io/redpoints-reddit-monitor")

# Slack Canvas — actioned threads log
# Canvas: Reddit Thread Action Log (shared in the team group DM)
SLACK_CANVAS_ID   = "F0B1EAQ99U6"

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# Competitors to flag when present in a thread (without Red Points)
COMPETITORS = [
    "brandshield", "corsearch", "marqvision", "convey iq",
    "ip-watch", "clarivate", "incopro", "unifiedpatents",
    "netcraft", "zerofox", "bolster", "opsec", "bustem",
    "red points", "redpoints",   # include RP so we can detect RP presence
]
RP_NAMES = {"red points", "redpoints"}

# Prompt funnel tags — maps Omnia topic IDs to TOFU/MOFU/BOFU
# Pulled from your existing tagging system
TOPIC_FUNNEL_MAP = {
    "a13b271a": "TOFU",   # fake-products
    "63b51e32": "TOFU",   # brand-impersonation
    "dc2f691a": "MOFU",   # unauthorized-sellers
    "2ed520c0": "MOFU",   # manual-enforcement
    "d97621e2": "MOFU",   # global-enforcement
    "d37f3a38": "BOFU",   # category-aware
    "b049770f": "BOFU",   # competitive
    "cd604a39": "BOFU",   # branded-direct
}

# Max threads to surface in the dashboard active feed (ranked by priority)
MAX_ACTIVE_THREADS = 12

# Subreddits with high LLM citation probability — flagged as priority
HIGH_VALUE_SUBREDDITS = [
    "ecommerce", "entrepreneur", "smallbusiness", "fraud",
    "legal", "business", "startups", "aws", "saas",
    "brands", "marketplaces", "amazonmerchants", "etsy",
]


# ---------------------------------------------------------------------------
# DATE HELPERS
# ---------------------------------------------------------------------------

def last_complete_week() -> tuple[datetime.date, datetime.date]:
    today = datetime.date.today()
    days_since_sunday = (today.weekday() + 1) % 7
    last_sunday = today - datetime.timedelta(days=days_since_sunday)
    if (today - last_sunday).days < 3:
        last_sunday -= datetime.timedelta(weeks=1)
    last_monday = last_sunday - datetime.timedelta(days=6)
    return last_monday, last_sunday

def week_key(monday: datetime.date) -> str:
    return monday.strftime("%Y-%m-%d")

def load_history() -> dict:
    history = {}
    for f in sorted(DATA_DIR.glob("week-*.json")):
        try:
            with open(f) as fp:
                data = json.load(fp)
            history[data["week_start"]] = data
        except Exception as e:
            log.warning(f"Could not load {f}: {e}")
    return history

def save_week_data(week_start: str, data: dict):
    path = DATA_DIR / f"week-{week_start}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    log.info(f"Saved week data to {path}")


# ---------------------------------------------------------------------------
# OMNIA — fetch Reddit citations across all prompts
# ---------------------------------------------------------------------------

def fetch_omnia_prompts() -> list[dict]:
    """Fetches all monitored prompts for the brand with their topic metadata."""
    if not OMNIA_TOKEN:
        log.warning("OMNIA_TOKEN not set")
        return []

    prompts = []
    page = 1
    while True:
        try:
            resp = requests.get(
                f"https://app.useomnia.com/api/v1/brands/{OMNIA_BRAND_ID}/prompts",
                headers={"Authorization": f"Bearer {OMNIA_TOKEN}"},
                params={"page": page, "pageSize": 100},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("data", {}).get("prompts", [])
            prompts.extend(batch)
            total = data.get("pagination", {}).get("totalItems", 0)
            if page * 100 >= total:
                break
            page += 1
        except Exception as e:
            log.error(f"Omnia prompts fetch failed (page {page}): {e}")
            break

    log.info(f"Omnia: {len(prompts)} prompts fetched")
    return prompts


def fetch_reddit_citations_for_prompt(
    prompt_id: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """
    Fetches citation sources for a specific prompt and returns only Reddit URLs.
    """
    if not OMNIA_TOKEN:
        return []
    try:
        resp = requests.get(
            f"https://app.useomnia.com/api/v1/prompts/{prompt_id}/citations/aggregates",
            headers={"Authorization": f"Bearer {OMNIA_TOKEN}"},
            params={
                "startDate": start_date,
                "endDate": end_date,
                "pageSize": 100,
                "sortBy": "total_citations",
                "sortDirection": "desc",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for agg in data.get("data", {}).get("aggregates", []):
            url = agg.get("url", "")
            if "reddit.com" in url and "/comments/" in url:
                results.append({
                    "url": url,
                    "citations": agg.get("totalCitations", 0),
                })
        return results
    except Exception as e:
        log.warning(f"  Omnia citations failed for prompt {prompt_id}: {e}")
        return []


def fetch_all_reddit_threads(
    prompts: list[dict],
    start_date: str,
    end_date: str,
) -> list[dict]:
    """
    Iterates all prompts, collects Reddit citations, deduplicates by URL,
    and aggregates which prompts cited each thread.
    Returns a list of thread dicts with all metadata.
    """
    thread_map = {}  # url → thread dict

    for prompt in prompts:
        prompt_id   = prompt.get("id", "")
        prompt_text = prompt.get("text", "")
        topic_id    = prompt.get("topicId", "")
        funnel      = TOPIC_FUNNEL_MAP.get(topic_id, "TOFU")

        reddit_hits = fetch_reddit_citations_for_prompt(prompt_id, start_date, end_date)

        for hit in reddit_hits:
            url = hit["url"]
            if url not in thread_map:
                thread_map[url] = {
                    "url": url,
                    "subreddit": extract_subreddit(url),
                    "thread_title": "",          # fetched later if needed
                    "total_citations": 0,
                    "prompts": [],
                    "funnel_tags": set(),
                    "first_seen": start_date,
                }
            thread_map[url]["total_citations"] += hit["citations"]
            thread_map[url]["prompts"].append({
                "id": prompt_id,
                "text": prompt_text,
                "funnel": funnel,
                "topic_id": topic_id,
            })
            thread_map[url]["funnel_tags"].add(funnel)

    # Convert sets to sorted lists for JSON serialisation
    threads = []
    for t in thread_map.values():
        t["funnel_tags"] = sorted(t["funnel_tags"],
                                  key=lambda x: ["TOFU","MOFU","BOFU"].index(x))
        # Primary funnel = highest-intent tag present (BOFU > MOFU > TOFU)
        for stage in ["BOFU", "MOFU", "TOFU"]:
            if stage in t["funnel_tags"]:
                t["primary_funnel"] = stage
                break
        else:
            t["primary_funnel"] = "TOFU"
        threads.append(t)

    log.info(f"Total Reddit threads found: {len(threads)}")
    return threads


def extract_subreddit(url: str) -> str:
    """Extracts subreddit name from a Reddit URL."""
    match = re.search(r"reddit\.com/r/([^/]+)", url)
    return match.group(1) if match else "unknown"


# ---------------------------------------------------------------------------
# COMPETITIVE GAP DETECTION
# ---------------------------------------------------------------------------

def detect_competitors(thread: dict) -> dict:
    """
    Checks thread URL and prompt texts for competitor and RP mentions.
    Returns {competitors_found, rp_mentioned, is_gap}.
    """
    # Check prompt texts that cited this thread
    search_text = " ".join(p["text"].lower() for p in thread.get("prompts", []))
    search_text += " " + thread.get("url", "").lower()
    search_text += " " + thread.get("thread_title", "").lower()

    competitors_found = [
        c for c in COMPETITORS
        if c in search_text and c not in RP_NAMES
    ]
    rp_mentioned = any(rp in search_text for rp in RP_NAMES)

    return {
        "competitors_found": list(set(competitors_found)),
        "rp_mentioned": rp_mentioned,
        "is_competitive_gap": len(competitors_found) > 0 and not rp_mentioned,
    }


# ---------------------------------------------------------------------------
# NET-NEW DETECTION
# ---------------------------------------------------------------------------

def identify_new_threads(
    current_threads: list[dict],
    history: dict,
) -> list[dict]:
    """
    Marks each thread as new_this_week = True if it wasn't seen in any prior week.
    """
    all_seen_urls = set()
    for week_data in history.values():
        for t in week_data.get("threads", []):
            all_seen_urls.add(t.get("url", ""))

    for thread in current_threads:
        thread["new_this_week"] = thread["url"] not in all_seen_urls

    new_count = sum(1 for t in current_threads if t["new_this_week"])
    log.info(f"{new_count} net-new Reddit threads this week")
    return current_threads


# ---------------------------------------------------------------------------
# GOOGLE SHEET — actioned threads
# ---------------------------------------------------------------------------

def fetch_actioned_urls() -> set[str]:
    """
    Reads the Slack Canvas action log and returns a set of actioned thread URLs.
    Canvas: Reddit Thread Action Log (F0B1EAQ99U6) shared in the team group DM.
    Requires SLACK_BOT_TOKEN with canvases:read scope.
    """
    if not SLACK_BOT_TOKEN:
        log.warning("SLACK_BOT_TOKEN not set — skipping actioned check")
        return set()

    try:
        resp = requests.post(
            "https://slack.com/api/canvases.sections.lookup",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "canvas_id": SLACK_CANVAS_ID,
                "criteria": {"section_types": ["any_ordered_list", "table"]},
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if not data.get("ok"):
            log.warning(f"Slack Canvas API returned error: {data.get('error', 'unknown')}")
            return set()

        urls = set()
        for section in data.get("sections", []):
            content = section.get("content", "")
            for line in content.split("\n"):
                if "reddit.com" in line:
                    match = re.search(r'https://(?:www\.)?reddit\.com/r/[^\s|>]+', line)
                    if match:
                        urls.add(match.group(0).rstrip("/").strip())

        log.info(f"Slack Canvas: {len(urls)} actioned URLs loaded")
        return urls

    except Exception as e:
        log.error(f"Slack Canvas read failed: {e}")
        return set()


# ---------------------------------------------------------------------------
# CLAUDE API — generate suggested angle per thread
# ---------------------------------------------------------------------------

def generate_angle(thread: dict, comp_info: dict) -> str:
    """
    Calls Claude API to generate a 1–2 sentence suggested commenting angle
    for a given Reddit thread, based on the citing prompts and context.
    """
    if not ANTHROPIC_API_KEY:
        return "Suggested angle unavailable — ANTHROPIC_API_KEY not set."

    # Build context from the prompts that cited this thread
    prompt_texts = "\n".join(
        f"- {p['text']} ({p['funnel']})"
        for p in thread.get("prompts", [])[:5]
    )
    comp_context = ""
    if comp_info.get("competitors_found"):
        comp_context = f"Competitors mentioned in context: {', '.join(comp_info['competitors_found'])}. Red Points is NOT mentioned."
    elif comp_info.get("rp_mentioned"):
        comp_context = "Red Points is already mentioned in this context."

    prompt = f"""You are an SEO and LLM visibility strategist at Red Points, the AI Brand Protection Company.

A Reddit thread is being cited by LLMs when answering brand protection queries. You need to suggest a commenting angle for the Red Points team.

THREAD URL: {thread['url']}
SUBREDDIT: r/{thread['subreddit']}
PRIMARY FUNNEL STAGE: {thread['primary_funnel']}
LLM PROMPTS THAT CITED THIS THREAD:
{prompt_texts}
{comp_context}

Red Points context:
- Fully managed service (IP-Ops specialists handle enforcement — not self-serve software)
- Unlimited enforcements, flat-fee pricing
- 5,000+ marketplaces, 2.7B monthly data points
- Revenue Recovery Program (risk-free litigation)
- Key differentiator vs competitors: managed service vs DIY tool

Write exactly 2 sentences:
1. What the commenter should lead with (genuinely helpful, matches the funnel stage — TOFU = educational, MOFU = validate frustration + escalation path, BOFU = direct comparison)
2. How and when to mention Red Points (never in the same breath as the helpful info — only after solving the immediate problem)

Rules:
- No corporate speak, no pitching in sentence 1
- BOFU threads: it is OK to mention Red Points directly since the person is evaluating vendors
- TOFU threads: solve the problem first, mention RP only as a way to scale if it keeps happening
- Keep it under 50 words total
- Plain text only, no bullet points"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 150,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = "".join(
            b["text"] for b in resp.json().get("content", [])
            if b.get("type") == "text"
        )
        return text.strip()
    except Exception as e:
        log.error(f"Claude API failed for {thread['url']}: {e}")
        return "Suggested angle unavailable — API error."


# ---------------------------------------------------------------------------
# PRIORITY SCORING
# ---------------------------------------------------------------------------

def score_thread(thread: dict, comp_info: dict) -> int:
    """
    Returns a priority score (higher = more urgent).
    Used to rank the active feed and cap at MAX_ACTIVE_THREADS.
    """
    score = 0
    # Funnel stage
    score += {"BOFU": 30, "MOFU": 20, "TOFU": 10}.get(thread["primary_funnel"], 0)
    # Competitive gap
    if comp_info.get("is_competitive_gap"):
        score += 40
    # Net-new this week
    if thread.get("new_this_week"):
        score += 20
    # High-value subreddit
    if thread["subreddit"].lower() in HIGH_VALUE_SUBREDDITS:
        score += 15
    # Citation volume
    score += min(thread.get("total_citations", 0) * 2, 20)
    return score


# ---------------------------------------------------------------------------
# SLACK ALERT
# ---------------------------------------------------------------------------

def send_slack_alert(
    threads: list[dict],
    actioned_urls: set[str],
    week_end: str,
):
    """Sends a concise weekly summary to the team group DM."""
    if not SLACK_WEBHOOK_URL:
        log.warning("SLACK_WEBHOOK_URL not set — skipping Slack")
        return

    active = [t for t in threads if t["url"] not in actioned_urls]
    gaps   = [t for t in active if t.get("comp_info", {}).get("is_competitive_gap")]
    new    = [t for t in active if t.get("new_this_week")]

    # Top 3 threads to highlight
    top3 = sorted(active, key=lambda t: t.get("priority_score", 0), reverse=True)[:3]
    thread_lines = ""
    for t in top3:
        funnel = t.get("primary_funnel", "TOFU")
        emoji  = {"BOFU": "🔴", "MOFU": "🟡", "TOFU": "🟢"}.get(funnel, "⚪")
        gap_tag = " · *⚡ Competitor gap*" if t.get("comp_info", {}).get("is_competitive_gap") else ""
        new_tag = " · `new`" if t.get("new_this_week") else ""
        thread_lines += (
            f"\n{emoji} *r/{t['subreddit']}*{gap_tag}{new_tag}\n"
            f"<{t['url']}|View thread> · {t.get('total_citations', 0)} LLM citations · {funnel}\n"
            f"_{t.get('angle', '')[:120]}..._\n"
        )

    payload = {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"📡 *Reddit LLM Monitor — Week ending {week_end}*\n"
                        f"*{len(new)} new threads* · "
                        f"*{len(gaps)} competitor gaps* · "
                        f"*{len(actioned_urls)} actioned*\n\n"
                        f"{thread_lines}\n"
                        f"<{REPORT_URL}|→ View full dashboard>"
                    )
                }
            }
        ]
    }

    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code == 200:
            log.info("Slack alert sent")
        else:
            log.error(f"Slack failed: {resp.status_code} {resp.text}")
    except Exception as e:
        log.error(f"Slack error: {e}")


# ---------------------------------------------------------------------------
# HTML DASHBOARD
# ---------------------------------------------------------------------------

def generate_html(
    threads: list[dict],
    actioned_urls: set[str],
    history: dict,
    week_start: str,
    week_end: str,
) -> str:
    active   = [t for t in threads if t["url"] not in actioned_urls]
    actioned = [t for t in threads if t["url"] in actioned_urls]
    gaps     = [t for t in active if t.get("comp_info", {}).get("is_competitive_gap")]
    new      = [t for t in active if t.get("new_this_week")]

    # Add current week to history summary
    history_summary = {}
    for wk, wdata in sorted(history.items(), reverse=True)[:4]:
        history_summary[wk] = {
            "week_end": wdata.get("week_end", ""),
            "new_count": len([t for t in wdata.get("threads", []) if t.get("new_this_week")]),
            "gap_count": len([t for t in wdata.get("threads", []) if t.get("comp_info", {}).get("is_competitive_gap")]),
        }
    history_summary[week_start] = {
        "week_end": week_end,
        "new_count": len(new),
        "gap_count": len(gaps),
    }

    def thread_card(t: dict, is_actioned: bool = False) -> str:
        comp  = t.get("comp_info", {})
        funnel = t.get("primary_funnel", "TOFU")
        sub    = t.get("subreddit", "unknown")
        cits   = t.get("total_citations", 0)
        angle  = t.get("angle", "")
        url    = t["url"]
        new_badge   = '<span class="badge new-badge">New this week</span>' if t.get("new_this_week") else ""
        comp_badge  = '<span class="badge gap-badge">Competitor gap</span>' if comp.get("is_competitive_gap") else ""
        funnel_cls  = funnel.lower()
        funnel_badge = f'<span class="badge {funnel_cls}-badge">{funnel}</span>'
        act_opacity = 'style="opacity:0.55"' if is_actioned else ""
        border_color = "#dc2626" if comp.get("is_competitive_gap") else ("#2563eb" if t.get("new_this_week") else "#16a34a" if is_actioned else "#94a3b8")

        # Prompts list
        prompt_texts = "".join(
            f'<div class="prompt-pill">{p["text"][:80]}{"…" if len(p["text"])>80 else ""}</div>'
            for p in t.get("prompts", [])[:3]
        )

        # Competitors found
        comp_tags = "".join(
            f'<span class="comp-name">{c}</span>'
            for c in comp.get("competitors_found", [])
        )
        rp_tag = '<span class="rp-present">RP present</span>' if comp.get("rp_mentioned") else ""

        action_btn = (
            '<span class="actioned-label">Actioned</span>'
            if is_actioned
            else f'<button class="action-btn" onclick="copyUrl(\'{url}\')">Copy URL to log</button>'
        )

        return f"""
        <div class="thread-card" {act_opacity} style="border-left-color:{border_color}">
          <div class="card-top">
            <div class="card-badges">{comp_badge}{new_badge}{funnel_badge}</div>
            <div class="card-meta">{cits} LLM citation{"s" if cits != 1 else ""}</div>
          </div>
          <a href="{url}" class="thread-link" target="_blank">r/{sub} &nbsp;·&nbsp; {url[28:90]}{"…" if len(url)>90 else ""}</a>
          <div class="prompt-list">{prompt_texts}</div>
          {f'<div class="comp-row">{comp_tags}{rp_tag}</div>' if comp_tags or rp_tag else ""}
          <div class="angle-box">
            <div class="angle-label">Suggested angle</div>
            <div class="angle-text">{angle}</div>
          </div>
          <div class="card-bottom">{action_btn}</div>
        </div>"""

    gaps_html    = "".join(thread_card(t) for t in sorted(gaps, key=lambda t: -t.get("priority_score", 0)))
    comment_html = "".join(thread_card(t) for t in sorted([t for t in active if not t.get("comp_info", {}).get("is_competitive_gap")], key=lambda t: -t.get("priority_score", 0)))
    actioned_html = "".join(thread_card(t, is_actioned=True) for t in actioned[:5])

    history_btns = "".join(
        f'<button class="week-btn" data-week="{wk}">'
        f'w/{wk} &nbsp;·&nbsp; {v["new_count"]} new · {v["gap_count"]} gaps'
        f'</button>'
        for wk, v in sorted(history_summary.items(), reverse=True)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Red Points · Reddit LLM Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'DM Sans',sans-serif;background:#f8fafc;color:#1e293b}}
.header{{background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);padding:28px 32px;color:white}}
.header h1{{font-size:20px;font-weight:600}}
.header p{{color:#94a3b8;font-size:13px;margin-top:4px}}
.container{{max-width:960px;margin:0 auto;padding:24px}}
.week-bar{{display:flex;gap:8px;overflow-x:auto;margin-bottom:20px}}
.week-btn{{background:white;border:1px solid #e2e8f0;border-radius:8px;padding:8px 14px;cursor:pointer;font-size:12px;font-family:'DM Sans',sans-serif;color:#64748b;white-space:nowrap}}
.week-btn.active,.week-btn:hover{{background:#0f172a;color:white;border-color:#0f172a}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}}
.stat{{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:16px;text-align:center}}
.stat-n{{font-family:'JetBrains Mono',monospace;font-size:28px;font-weight:600}}
.stat-l{{font-size:11px;color:#64748b;margin-top:3px}}
.stat-n.red{{color:#dc2626}}
.stat-n.blue{{color:#2563eb}}
.stat-n.amber{{color:#d97706}}
.stat-n.green{{color:#16a34a}}
.filters{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:20px;align-items:center}}
.filter-lbl{{font-size:11px;color:#64748b;margin-right:4px}}
.fpill{{background:white;border:1px solid #e2e8f0;border-radius:20px;padding:5px 12px;font-size:11px;font-weight:500;color:#64748b;cursor:pointer;font-family:'DM Sans',sans-serif}}
.fpill.active{{background:#0f172a;color:white;border-color:#0f172a}}
.section-lbl{{font-size:11px;font-weight:600;color:#475569;text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px;margin-top:4px}}
.thread-list{{display:flex;flex-direction:column;gap:10px;margin-bottom:24px}}
.thread-card{{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:16px 18px;border-left:4px solid #94a3b8}}
.card-top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
.card-badges{{display:flex;gap:5px;flex-wrap:wrap}}
.card-meta{{font-size:11px;color:#94a3b8;font-family:'JetBrains Mono',monospace}}
.badge{{font-size:10px;padding:3px 8px;border-radius:20px;font-weight:500}}
.gap-badge{{background:#fef2f2;color:#991b1b;border:1px solid #fca5a5}}
.new-badge{{background:#eff6ff;color:#1d4ed8;border:1px solid #93c5fd}}
.tofu-badge{{background:#f0fdf4;color:#166534;border:1px solid #86efac}}
.mofu-badge{{background:#fffbeb;color:#92400e;border:1px solid #fcd34d}}
.bofu-badge{{background:#fef2f2;color:#991b1b;border:1px solid #fca5a5}}
.thread-link{{font-size:13px;font-weight:500;color:#2563eb;text-decoration:none;display:block;margin-bottom:8px;line-height:1.4}}
.thread-link:hover{{text-decoration:underline}}
.prompt-list{{margin-bottom:8px;display:flex;flex-direction:column;gap:3px}}
.prompt-pill{{font-size:11px;color:#475569;background:#f8fafc;border:1px solid #e2e8f0;border-radius:4px;padding:3px 8px;line-height:1.4}}
.comp-row{{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:8px}}
.comp-name{{font-size:10px;padding:2px 7px;border-radius:4px;background:#fef2f2;color:#991b1b;border:1px solid #fca5a5}}
.rp-present{{font-size:10px;padding:2px 7px;border-radius:4px;background:#f0fdf4;color:#166534;border:1px solid #86efac}}
.angle-box{{background:#f8fafc;border-radius:6px;padding:10px 12px;margin-bottom:10px;border-left:3px solid #2563eb}}
.angle-label{{font-size:10px;font-weight:600;color:#2563eb;margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em}}
.angle-text{{font-size:12px;color:#334155;line-height:1.6}}
.card-bottom{{display:flex;justify-content:flex-end}}
.action-btn{{font-size:11px;padding:5px 12px;border-radius:6px;border:1px solid #e2e8f0;background:white;color:#0f172a;cursor:pointer;font-family:'DM Sans',sans-serif}}
.action-btn:hover{{background:#f8fafc}}
.actioned-label{{font-size:11px;color:#94a3b8}}
.empty{{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:40px;text-align:center;color:#94a3b8;font-size:14px}}
.toast{{position:fixed;bottom:24px;right:24px;background:#0f172a;color:white;padding:10px 18px;border-radius:8px;font-size:13px;display:none;z-index:100}}
@media(max-width:640px){{.stats{{grid-template-columns:repeat(2,1fr)}}}}
</style>
</head>
<body>
<div class="header">
  <div style="max-width:960px;margin:0 auto">
    <h1>📡 Reddit LLM Thread Monitor</h1>
    <p>Threads being cited by AI models · Updated weekly · {datetime.date.today().strftime('%B %d, %Y')} · <a href="{REPORT_URL}" style="color:#60a5fa">{REPORT_URL}</a></p>
  </div>
</div>

<div class="container">
  <div class="week-bar">{history_btns}</div>

  <div class="stats">
    <div class="stat"><div class="stat-n red">{len(gaps)}</div><div class="stat-l">Competitor gaps</div></div>
    <div class="stat"><div class="stat-n blue">{len(new)}</div><div class="stat-l">New this week</div></div>
    <div class="stat"><div class="stat-n amber">{len(active)}</div><div class="stat-l">Active threads</div></div>
    <div class="stat"><div class="stat-n green">{len(actioned)}</div><div class="stat-l">Actioned</div></div>
  </div>

  <div class="filters">
    <span class="filter-lbl">Filter:</span>
    <button class="fpill active" onclick="filter('all',this)">All</button>
    <button class="fpill" onclick="filter('gap',this)">Competitor gaps</button>
    <button class="fpill" onclick="filter('new',this)">New this week</button>
    <button class="fpill" onclick="filter('bofu',this)">BOFU</button>
    <button class="fpill" onclick="filter('mofu',this)">MOFU</button>
    <button class="fpill" onclick="filter('tofu',this)">TOFU</button>
    <button class="fpill" onclick="filter('actioned',this)">Actioned</button>
  </div>

  <div id="main-feed">
    {"<div class='section-lbl'>Competitor gaps — act first</div><div class='thread-list'>" + gaps_html + "</div>" if gaps_html else ""}
    {"<div class='section-lbl'>New threads — comment opportunity</div><div class='thread-list'>" + comment_html + "</div>" if comment_html else ""}
    {"<div class='empty'>No active threads this week.</div>" if not gaps_html and not comment_html else ""}

    {"<div class='section-lbl'>Actioned this week</div><div class='thread-list'>" + actioned_html + "</div>" if actioned_html else ""}
  </div>
</div>

<div class="toast" id="toast">URL copied — paste into the Slack Canvas action log</div>

<script>
function copyUrl(url) {{
  navigator.clipboard.writeText(url).then(() => {{
    const t = document.getElementById('toast');
    t.style.display = 'block';
    setTimeout(() => t.style.display = 'none', 3000);
  }});
}}
function filter(type, btn) {{
  document.querySelectorAll('.fpill').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.thread-card').forEach(card => {{
    const isGap = card.querySelector('.gap-badge');
    const isNew = card.querySelector('.new-badge');
    const isActioned = card.querySelector('.actioned-label');
    const funnel = card.querySelector('.tofu-badge, .mofu-badge, .bofu-badge');
    const funnelType = funnel ? funnel.textContent.toLowerCase() : '';
    let show = true;
    if (type === 'gap') show = !!isGap;
    else if (type === 'new') show = !!isNew;
    else if (type === 'actioned') show = !!isActioned;
    else if (type === 'bofu') show = funnelType === 'bofu';
    else if (type === 'mofu') show = funnelType === 'mofu';
    else if (type === 'tofu') show = funnelType === 'tofu';
    card.style.display = show ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    log.info("=== Red Points Reddit LLM Monitor starting ===")

    current_monday, current_sunday = last_complete_week()
    prev_monday = current_monday - datetime.timedelta(weeks=1)
    prev_sunday = prev_monday + datetime.timedelta(days=6)

    week_start = week_key(current_monday)
    week_end   = current_sunday.strftime("%Y-%m-%d")
    prev_start = week_key(prev_monday)
    prev_end   = prev_sunday.strftime("%Y-%m-%d")

    log.info(f"Analysing week: {week_start} → {week_end}")

    history = load_history()

    # ── Step 1: Fetch all prompts ─────────────────────────────────────────────
    prompts = fetch_omnia_prompts()
    if not prompts:
        log.error("No prompts found — exiting")
        return

    # ── Step 2: Fetch Reddit citations for this week ──────────────────────────
    threads = fetch_all_reddit_threads(prompts, week_start, week_end)

    # ── Step 3: Net-new detection ─────────────────────────────────────────────
    threads = identify_new_threads(threads, history)

    # ── Step 4: Fetch actioned URLs from Slack Canvas ────────────────────────
    actioned_urls = fetch_actioned_urls()

    # ── Step 5: Competitive gap detection + priority scoring ──────────────────
    for thread in threads:
        comp_info = detect_competitors(thread)
        thread["comp_info"] = comp_info
        thread["priority_score"] = score_thread(thread, comp_info)

    # ── Step 6: Generate angles (only for active, uncapped set) ──────────────
    active_threads = sorted(
        [t for t in threads if t["url"] not in actioned_urls],
        key=lambda t: -t["priority_score"],
    )[:MAX_ACTIVE_THREADS]

    log.info(f"Generating angles for {len(active_threads)} active threads...")
    for t in active_threads:
        t["angle"] = generate_angle(t, t["comp_info"])

    # Ensure all threads have an angle key
    for t in threads:
        if "angle" not in t:
            t["angle"] = ""

    # ── Step 7: Save this week's data ────────────────────────────────────────
    save_week_data(week_start, {
        "week_start": week_start,
        "week_end": week_end,
        "threads": threads,
        "generated_at": datetime.datetime.utcnow().isoformat(),
    })

    history = load_history()

    # ── Step 8: Generate and save HTML dashboard ──────────────────────────────
    html = generate_html(threads, actioned_urls, history, week_start, week_end)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    log.info("index.html written for GitHub Pages")

    # ── Step 9: Send Slack alert ──────────────────────────────────────────────
    send_slack_alert(threads, actioned_urls, week_end)

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
