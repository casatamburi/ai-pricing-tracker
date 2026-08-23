#!/usr/bin/env python3
"""Weekly agent. Steps:
1. Ask Claude (with web search) for AI model releases / price changes in last 7 days.
2. Claude returns STRICT JSON: only raw facts (name, prices, context, license, cluster).
3. Python merges into models.json — NEW models appended, EXISTING prices updated.
4. render.py regenerates index.html deterministically (blended + bars in Python).
5. If anything changed, commit is left to the GH Action; email is sent either way.

The LLM never computes blended cost or bar widths. It only extracts facts.
Qualitative notes it adds are marked flagged=true -> 'auto · verify' badge.
"""
import json, os, smtplib, ssl, subprocess, sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import urllib.request

API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PW = os.environ["GMAIL_APP_PASSWORD"]
MAIL_TO = os.environ.get("MAIL_TO", GMAIL_USER)
PAGES_URL = os.environ.get("PAGES_URL", "(set PAGES_URL secret)")

MODEL = "claude-opus-4-8"

EXTRACTION_PROMPT = """You are a data extractor for an AI token-pricing tracker. Today is the date of this run.

Using web search, find AI LLM developments from the LAST 7 DAYS ONLY:
- NEW models released (any major lab: Anthropic, OpenAI, Google, Meta, NVIDIA, Mistral, DeepSeek, Alibaba/Qwen, Moonshot/Kimi, xAI, etc.)
- PRICE CHANGES to existing models in the current list below.

CURRENT LIST (name -> input/output USD per 1M tokens):
{current_list}

Return STRICT JSON, no markdown, no prose. Schema:
{{
  "found_changes": true/false,
  "new_models": [
    {{
      "cluster": "western" | "usopen" | "chinese",
      "name": "string",
      "vendor": "lab · date · license if open",
      "badges": [{{"label":"Closed|Open|CN","type":"closed|oai|open|chinese|nvidia"}}],
      "architecture": "Dense|MoE|...",
      "active_params": "e.g. 49B / 1.6T or —",
      "context": "e.g. 1M",
      "input_per_m": number (USD per 1M input tokens, US-hosted rate; for CN open models use US-hosted price not CN-direct),
      "output_per_m": number,
      "aa_blended": number or null (Artificial Analysis blended if known, else null),
      "aa_index": "string or —",
      "notes": "one short factual sentence",
      "flagged": true if notes contains any qualitative/estimated claim
    }}
  ],
  "price_updates": [
    {{"name": "exact name from current list", "input_per_m": number, "output_per_m": number, "source": "url"}}
  ],
  "summary": "2-3 sentence plain-text summary of what changed this week, or 'No material changes.'"
}}

RULES:
- Only include items with a credible source from the last 7 days.
- Prices must be US-hosted / first-party Western rates. Never CN-direct subsidised prices for the main entry.
- If unsure about a number, omit the model rather than guess.
- Do NOT compute any blended cost — that is done downstream.
"""


def call_claude(current_list):
    body = {
        "model": MODEL,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": EXTRACTION_PROMPT.format(current_list=current_list)}],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read())
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    text = text.replace("```json", "").replace("```", "").strip()
    # grab first {...} block defensively
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def merge(store, result):
    changed = False
    by_name = {m["name"]: m for m in store["models"]}

    for upd in result.get("price_updates", []):
        m = by_name.get(upd["name"])
        if m and (m["input_per_m"] != upd["input_per_m"] or m["output_per_m"] != upd["output_per_m"]):
            m["input_per_m"] = upd["input_per_m"]
            m["output_per_m"] = upd["output_per_m"]
            changed = True

    for nm in result.get("new_models", []):
        if nm["name"] in by_name:
            continue
        nm.setdefault("aa_blended", 0.0)
        if nm.get("aa_blended") is None:
            nm["aa_blended"] = 0.0
        nm.setdefault("aa_index", "—")
        nm.setdefault("flagged", True)  # new entries default to verify
        store["models"].append(nm)
        changed = True

    return changed


def send_mail(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = MAIL_TO
    msg.attach(MIMEText(html_body, "html"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(GMAIL_USER, GMAIL_APP_PW)
        s.sendmail(GMAIL_USER, [MAIL_TO], msg.as_string())


def main():
    store = json.load(open("models.json", encoding="utf-8"))
    current_list = "\n".join(
        f"- {m['name']}: {m['input_per_m']}/{m['output_per_m']}"
        for m in store["models"] if m["cluster"] != "reference"
    )

    try:
        result = call_claude(current_list)
    except Exception as e:
        send_mail("⚠ Token pricing agent — ERROR",
                  f"<p>Weekly run failed:</p><pre>{e}</pre>")
        sys.exit(1)

    changed = merge(store, result)

    from datetime import date
    store["meta"]["last_updated"] = date.today().isoformat()
    json.dump(store, open("models.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    subprocess.run([sys.executable, "render.py"], check=True)

    summary = result.get("summary", "")
    new_rows = "".join(
        f"<li><b>{m['name']}</b> — {m.get('vendor','')} · in ${m['input_per_m']}/out ${m['output_per_m']} "
        f"<span style='color:#b45309'>(auto · verify)</span></li>"
        for m in result.get("new_models", [])
    ) or "<li>none</li>"
    upd_rows = "".join(
        f"<li><b>{u['name']}</b> → in ${u['input_per_m']}/out ${u['output_per_m']}</li>"
        for u in result.get("price_updates", [])
    ) or "<li>none</li>"

    status = "UPDATED" if changed else "no changes"
    body = f"""
    <div style="font-family:-apple-system,sans-serif;font-size:14px;color:#0f172a">
      <h2 style="margin:0 0 4px">Agentic Token Pricing — weekly check ({status})</h2>
      <p style="color:#475569;margin:0 0 16px">{summary}</p>
      <p><b>Live page:</b> <a href="{PAGES_URL}">{PAGES_URL}</a></p>
      <h3 style="margin:16px 0 4px">New models</h3><ul>{new_rows}</ul>
      <h3 style="margin:16px 0 4px">Price updates</h3><ul>{upd_rows}</ul>
      <p style="color:#94a3b8;font-size:12px;margin-top:20px">
        Blended costs &amp; bars computed deterministically in Python. Items tagged
        <i>auto · verify</i> are LLM-extracted qualitative claims — confirm before relying.
      </p>
    </div>"""

    send_mail(f"Token pricing — {status} — {store['meta']['last_updated']}", body)
    print(f"Done. changed={changed}")


if __name__ == "__main__":
    main()
