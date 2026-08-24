#!/usr/bin/env python3
"""Weekly agent. Two LLM passes, then deterministic merge.

PASS 1 - NEWS (last 7 days): new model releases / price changes.
PASS 2 - AUDIT (no time window): re-verify every listed price against current
         reality, catch renames/supersessions, and resolve pending markers.
         This is what catches changes older than 7 days, which pass 1 is
         structurally blind to.

Both passes only ever return RAW FACTS. Blended cost and bar widths are
computed later, in render.py, in deterministic Python — never by the LLM.

Everything is automatic: there is no human gate. Safety comes from Python-side
validation (price bounds, mandatory source URL) plus a full price_history audit
trail, not from a person confirming each row. Anything the validator rejects is
reported in the email instead of being silently applied.
"""
import json, os, smtplib, ssl, subprocess, sys
from datetime import date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import urllib.request

API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PW = os.environ["GMAIL_APP_PASSWORD"]
MAIL_TO = os.environ.get("MAIL_TO", GMAIL_USER)
PAGES_URL = os.environ.get("PAGES_URL", "(set PAGES_URL secret)")

MODEL = "claude-opus-4-8"

# --- deterministic guardrails (these replace the human "verify" step) -------
PRICE_MIN, PRICE_MAX = 0.001, 500.0   # USD per 1M tokens, plausible envelope


def valid_price(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and PRICE_MIN <= float(v) <= PRICE_MAX)


def valid_source(s):
    return isinstance(s, str) and s.startswith(("http://", "https://"))


NEWS_PROMPT = """You are a data extractor for an AI token-pricing tracker. Today is the date of this run.

Using web search, find AI LLM developments from the LAST 7 DAYS ONLY:
- NEW models released (any major lab: Anthropic, OpenAI, Google, Meta, NVIDIA, Mistral, DeepSeek, Alibaba/Qwen, Moonshot/Kimi, xAI, etc.)
- PRICE CHANGES to existing models in the current list below.

CURRENT LIST (name [cluster] -> input/output USD per 1M tokens):
{current_list}

Cluster meanings: western/usopen/chinese = US-hosted first-party or Western
infra rates. reference = the model's own China-direct API (CN servers, RMB
tariff converted to USD) — a DIFFERENT price from the US-hosted row.

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
      "aa_blended": number or null,
      "aa_index": "string or —",
      "notes": "one short factual sentence",
      "flagged": true if notes contains any qualitative/estimated claim,
      "source": "url"
    }}
  ],
  "price_updates": [
    {{"name": "exact name from current list", "input_per_m": number, "output_per_m": number, "source": "url"}}
  ],
  "summary": "2-3 sentence plain-text summary of what changed this week, or 'No material changes.'"
}}

RULES:
- Only include items with a credible source from the last 7 days.
- Every price_update and new_model MUST carry a real source URL. No URL = omit it.
- For a 'reference' cluster row, quote the China-direct tariff (convert RMB->USD and say the rate in notes). For all other clusters quote US-hosted / first-party Western rates.
- If unsure about a number, omit the model rather than guess.
- Do NOT compute any blended cost — that is done downstream in Python.
"""

AUDIT_PROMPT = """You are auditing an AI token-pricing tracker for staleness. Today is the date of this run.
There is NO time window for this task: a price that changed months ago and was
never picked up is exactly what you are here to find.

Using web search, verify EVERY row below against the vendor's current public
pricing. Report only rows where reality differs from what is listed, plus any
model that has been renamed or superseded.

CURRENT ROWS (name [cluster] -> listed input/output USD per 1M tokens · notes):
{audit_list}

Cluster meanings: western/usopen/chinese = US-hosted / first-party Western
rates. reference = the model's own China-direct API (CN servers; RMB tariff
converted to USD). These are DIFFERENT prices for the same model — audit each
against the right tariff, and never copy a US-hosted price onto a CN-direct row
or vice versa.

Several notes contain pending markers such as "t.b.v.", "n.v.", "pricing t.b.v.",
"post-hike n.v.", or have a missing Intel Index. Resolve those if the number is
now published.

Return STRICT JSON, no markdown, no prose. Schema:
{{
  "price_updates": [
    {{"name": "exact name from the list", "input_per_m": number, "output_per_m": number,
      "source": "url", "reason": "what changed and when, one short sentence"}}
  ],
  "supersessions": [
    {{"old_name": "exact name from the list", "new_name": "the replacing model",
      "note": "one short factual sentence", "source": "url"}}
  ],
  "resolved_pending": [
    {{"name": "exact name from the list", "notes": "rewritten note with the pending marker resolved",
      "intel_v41": number or null, "aa_index": "string or null", "source": "url"}}
  ],
  "audit_summary": "2-3 sentence plain-text summary of the audit, or 'All listed prices verified current.'"
}}

RULES:
- Report a price_update ONLY if you have a credible current source contradicting the listed number. Silence means 'still correct'.
- Every entry MUST carry a real source URL. No URL = omit the entry.
- Prices are USD per 1M tokens. For peak/off-peak tariffs report the PEAK price and say so in the reason.
- If a vendor lists RMB, convert to USD and state the rate used in the reason.
- If you cannot confirm a number, omit it. Never guess, never interpolate.
- Do NOT compute any blended cost — that is done downstream in Python.
"""


def call_claude(prompt, max_tokens=4000):
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
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
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in model reply: {text[:400]}")
    return json.loads(text[start:end + 1])


def apply_price_update(m, upd, today, applied, rejected):
    """Validate, then apply one price update. Records an audit-trail entry."""
    name = upd.get("name", "?")
    if not (valid_price(upd.get("input_per_m")) and valid_price(upd.get("output_per_m"))):
        rejected.append((name, f"price outside ${PRICE_MIN}-${PRICE_MAX}/M or not numeric: "
                               f"in={upd.get('input_per_m')!r} out={upd.get('output_per_m')!r}"))
        return False
    if not valid_source(upd.get("source")):
        rejected.append((name, f"missing/invalid source URL: {upd.get('source')!r}"))
        return False

    new_in, new_out = float(upd["input_per_m"]), float(upd["output_per_m"])
    if m["input_per_m"] == new_in and m["output_per_m"] == new_out:
        return False   # already correct — not a change

    old_in, old_out = m["input_per_m"], m["output_per_m"]
    m.setdefault("price_history", []).append({
        "date": today,
        "from": {"input_per_m": old_in, "output_per_m": old_out},
        "to": {"input_per_m": new_in, "output_per_m": new_out},
        "source": upd["source"],
        "reason": upd.get("reason", ""),
    })
    m["input_per_m"], m["output_per_m"] = new_in, new_out
    applied.append({
        "name": name, "old_in": old_in, "old_out": old_out,
        "new_in": new_in, "new_out": new_out,
        "source": upd["source"], "reason": upd.get("reason", ""),
    })
    return True


def merge(store, news, audit, today):
    """Deterministic merge. Returns (changed, report)."""
    changed = False
    report = {"applied": [], "rejected": [], "new": [], "superseded": [], "resolved": []}
    by_name = {m["name"]: m for m in store["models"]}

    # --- price updates: news first, then audit (audit runs second so it wins)
    for src_list in (news.get("price_updates") or [], audit.get("price_updates") or []):
        for upd in src_list:
            m = by_name.get(upd.get("name"))
            if not m:
                report["rejected"].append((upd.get("name", "?"), "name not in current list"))
                continue
            if apply_price_update(m, upd, today, report["applied"], report["rejected"]):
                changed = True

    # --- new models
    for nm in news.get("new_models") or []:
        name = nm.get("name")
        if not name or name in by_name:
            continue
        if not (valid_price(nm.get("input_per_m")) and valid_price(nm.get("output_per_m"))):
            report["rejected"].append((name, "new model with implausible/missing prices"))
            continue
        if not valid_source(nm.get("source")):
            report["rejected"].append((name, "new model without source URL"))
            continue
        nm.pop("source", None)
        if nm.get("aa_blended") is None:
            nm["aa_blended"] = 0.0
        nm.setdefault("aa_blended", 0.0)
        nm.setdefault("aa_index", "—")
        nm.setdefault("flagged", True)      # provenance marker, not a to-do
        nm.setdefault("intel_v41", None)
        store["models"].append(nm)
        by_name[name] = nm
        report["new"].append(nm)
        changed = True

    # --- supersessions: non-destructive. The old row is kept and marked, never
    #     deleted, so history stays intact and a wrong call stays reversible.
    for sup in audit.get("supersessions") or []:
        old = by_name.get(sup.get("old_name"))
        if not old or not valid_source(sup.get("source")):
            continue
        if old.get("superseded_by") == sup.get("new_name"):
            continue
        old["superseded_by"] = sup.get("new_name", "")
        old["deprecated"] = True
        note = sup.get("note", "")
        if note and note not in old.get("notes", ""):
            old["notes"] = (old.get("notes", "") + " " + note).strip()
        report["superseded"].append(sup)
        changed = True

    # --- resolved pending markers (qualitative -> stays flagged as auto-sourced)
    for res in audit.get("resolved_pending") or []:
        m = by_name.get(res.get("name"))
        if not m or not valid_source(res.get("source")):
            continue
        touched = False
        if res.get("notes"):
            m["notes"] = res["notes"]
            touched = True
        iv = res.get("intel_v41")
        if isinstance(iv, (int, float)) and not isinstance(iv, bool):
            m["intel_v41"] = iv
            touched = True
        if isinstance(res.get("aa_index"), str) and res["aa_index"].strip():
            m["aa_index"] = res["aa_index"]
            touched = True
        if touched:
            m["flagged"] = True
            report["resolved"].append(res)
            changed = True

    return changed, report


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


def build_email(news, audit, report, changed):
    def ul(items, empty="<li>none</li>"):
        return "".join(items) or empty

    new_rows = ul([
        f"<li><b>{m['name']}</b> — {m.get('vendor','')} · in ${m['input_per_m']}/out ${m['output_per_m']}"
        f" <span style='color:#b45309'>(auto-sourced)</span></li>"
        for m in report["new"]
    ])
    upd_rows = ul([
        f"<li><b>{u['name']}</b>: in ${u['old_in']} → <b>${u['new_in']}</b>, "
        f"out ${u['old_out']} → <b>${u['new_out']}</b>"
        f"{' — ' + u['reason'] if u['reason'] else ''} "
        f"<a href=\"{u['source']}\">source</a></li>"
        for u in report["applied"]
    ])
    sup_rows = ul([
        f"<li><b>{s.get('old_name')}</b> → superseded by <b>{s.get('new_name')}</b> · "
        f"<a href=\"{s.get('source','')}\">source</a> (old row kept, marked deprecated)</li>"
        for s in report["superseded"]
    ])
    res_rows = ul([
        f"<li><b>{r.get('name')}</b> — pending item resolved · "
        f"<a href=\"{r.get('source','')}\">source</a></li>"
        for r in report["resolved"]
    ])
    rej_rows = ul(
        [f"<li><b>{n}</b> — {why}</li>" for n, why in report["rejected"]],
        empty="<li>none — every proposed change passed validation</li>")

    status = "UPDATED" if changed else "no changes"
    body = f"""
    <div style="font-family:-apple-system,sans-serif;font-size:14px;color:#0f172a">
      <h2 style="margin:0 0 4px">Agentic Token Pricing — weekly check ({status})</h2>
      <p style="color:#475569;margin:0 0 4px"><b>News (7d):</b> {news.get('summary','—')}</p>
      <p style="color:#475569;margin:0 0 16px"><b>Audit (all rows):</b> {audit.get('audit_summary','—')}</p>
      <p><b>Live page:</b> <a href="{PAGES_URL}">{PAGES_URL}</a></p>
      <h3 style="margin:16px 0 4px">New models</h3><ul>{new_rows}</ul>
      <h3 style="margin:16px 0 4px">Price updates applied</h3><ul>{upd_rows}</ul>
      <h3 style="margin:16px 0 4px">Supersessions</h3><ul>{sup_rows}</ul>
      <h3 style="margin:16px 0 4px">Pending items resolved</h3><ul>{res_rows}</ul>
      <h3 style="margin:16px 0 4px">Rejected by validation</h3><ul>{rej_rows}</ul>
      <p style="color:#94a3b8;font-size:12px;margin-top:20px">
        Fully automatic — no manual confirmation step. Blended costs &amp; bars are
        computed deterministically in Python; the model only supplies raw facts, each
        required to carry a source URL and to sit inside a plausible price envelope
        (${PRICE_MIN}–${PRICE_MAX}/M). Every price change is appended to
        <code>price_history</code> in models.json, so a bad value stays traceable and
        revertible. Rejected entries above were dropped, not applied.
      </p>
    </div>"""
    return status, body


def main():
    with open("models.json", encoding="utf-8") as f:
        store = json.load(f)

    # Both passes see EVERY row, reference cluster included — otherwise CN-direct
    # prices could never be corrected (they were invisible to the old prompt).
    current_list = "\n".join(
        f"- {m['name']} [{m['cluster']}]: {m['input_per_m']}/{m['output_per_m']}"
        for m in store["models"]
    )
    audit_list = "\n".join(
        f"- {m['name']} [{m['cluster']}]: {m['input_per_m']}/{m['output_per_m']}"
        f" · intel_v41={m.get('intel_v41')} · notes: {m.get('notes','')}"
        for m in store["models"]
    )

    news, audit, errors = {}, {}, []
    try:
        news = call_claude(NEWS_PROMPT.format(current_list=current_list))
    except Exception as e:
        errors.append(f"NEWS pass failed: {e}")
    try:
        audit = call_claude(AUDIT_PROMPT.format(audit_list=audit_list), max_tokens=6000)
    except Exception as e:
        errors.append(f"AUDIT pass failed: {e}")

    if len(errors) == 2:
        send_mail("⚠ Token pricing agent — ERROR",
                  "<p>Both passes failed:</p><pre>" + "\n".join(errors) + "</pre>")
        sys.exit(1)

    today = date.today().isoformat()
    changed, report = merge(store, news, audit, today)

    store["meta"]["last_updated"] = today
    with open("models.json", "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    subprocess.run([sys.executable, "render.py"], check=True)

    status, body = build_email(news, audit, report, changed)
    if errors:
        body += "<p style='color:#b91c1c'><b>Partial failure:</b><br>" + "<br>".join(errors) + "</p>"
        status += " (partial)"
    send_mail(f"Token pricing — {status} — {today}", body)

    print(f"Done. changed={changed} applied={len(report['applied'])} "
          f"new={len(report['new'])} superseded={len(report['superseded'])} "
          f"resolved={len(report['resolved'])} rejected={len(report['rejected'])}")


if __name__ == "__main__":
    main()
