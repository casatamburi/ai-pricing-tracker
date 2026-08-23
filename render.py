#!/usr/bin/env python3
"""Render models.json -> index.html. Blended cost and bar widths are computed
here in deterministic Python, never by the LLM. Style is a fixed template."""
import json, html, sys

CLUSTER_HEADERS = {
    "western":   ("sec-western", "① Western Closed Source"),
    "usopen":    ("sec-usopen",  "② US Open-Weight — US Infrastructure"),
    "chinese":   ("sec-chinese", "③ Chinese Open-Weight — US-Hosted (DeepInfra / Fireworks / OpenRouter)"),
    "reference": ("sec-ref",     "Reference Only — Chinese Direct API (subsidised CN servers · AA default · not for EU/US production)"),
}
CLUSTER_ORDER = ["western", "usopen", "chinese", "reference"]

# blended cost color ramp by rank within priced (non-reference) models
RAMP = ["c1", "c2", "c3", "c4", "c5"]
BAR_COLORS = ["#f87171", "#fb923c", "#facc15", "#a3e635", "#4ade80"]


def blended(m, b):
    """Your exact formula, per 1M total tokens."""
    cache_miss = b["cache_miss_input_tokens"] / 1_000_000 * m["input_per_m"]
    cached = b["cached_input_tokens"] / 1_000_000 * m["input_per_m"] * b["cache_hit_fraction_of_input"]
    out = b["output_tokens"] / 1_000_000 * m["output_per_m"]
    return cache_miss + cached + out


def fmt(v):
    return f"~${v:,.2f}"


def badge_html(badges):
    out = ""
    for bd in badges:
        out += f' <span class="badge {html.escape(bd["type"])}">{html.escape(bd["label"])}</span>'
    return out


def render(data):
    b = data["meta"]["blend"]
    models = data["models"]

    # compute blended for all
    for m in models:
        m["_blended"] = blended(m, b)

    # bar scale: linear on max blended among priced models
    priced = [m for m in models if m["cluster"] != "reference"]
    max_blend = max((m["_blended"] for m in priced), default=1.0)
    MAX_BAR_PX = 68

    rows = []
    for cluster in CLUSTER_ORDER:
        cms = [m for m in models if m["cluster"] == cluster]
        if not cms:
            continue
        cls, label = CLUSTER_HEADERS[cluster]
        rows.append(f'<tr class="section-header {cls}"><td colspan="11">{html.escape(label)}</td></tr>')
        for m in cms:
            dimmed = " dimmed" if cluster == "reference" else ""
            bar_px = max(2, round(m["_blended"] / max_blend * MAX_BAR_PX))
            # bar color by blended bucket
            frac = m["_blended"] / max_blend
            bar_color = BAR_COLORS[min(len(BAR_COLORS) - 1, int(frac * len(BAR_COLORS)))]
            bar = "" if cluster == "reference" else (
                f'<div class="bar-wrap"><div class="bar" style="width:{bar_px}px;background:{bar_color}"></div></div>'
            )
            warn = ""
            if m.get("aa_blended_warn"):
                warn = f' <span class="warn">{html.escape(m["aa_blended_warn"])}</span>'
            is_est = m.get("estimate", False)
            flag = ' <span class="badge warn">auto · verify</span>' if m.get("flagged") else ""
            if is_est:
                flag = ' <span class="badge est-badge">PRELIMINARY ESTIMATE</span>' + flag
            hl_cls = "hl dimmed" if cluster == "reference" else "hl"
            aa_idx = m["aa_index"]
            iv = m.get("intel_v41")
            if iv is None:
                aa_idx_html = '<span style="color:#475569">n.p.</span>'
            else:
                ibar = max(2, round(iv / 60 * 40))
                icolor = "#4ade80" if iv >= 55 else "#a3e635" if iv >= 48 else "#facc15" if iv >= 42 else "#fb923c" if iv >= 34 else "#f87171"
                aa_idx_html = (f'<div style="display:flex;align-items:center;gap:6px">'
                               f'<span class="aa-idx">{iv}</span>'
                               f'<div class="bar" style="width:{ibar}px;background:{icolor}"></div></div>')
            tr_cls = (dimmed.strip() + (" estimate" if is_est else "")).strip()
            est_pfx = "est. ~" if is_est else ""
            blended_val = fmt(m["_blended"])
            if is_est and m.get("blended_range"):
                lo, hi = m["blended_range"]
                blended_val = f"~${lo:.2f}–{hi:.2f}"
            aa_val = "—" if is_est and not m.get("aa_blended") else fmt(m["aa_blended"])
            rows.append(f'''    <tr class="{tr_cls}">
      <td>
        <div class="model-name">{html.escape(m["name"])}{badge_html(m["badges"])}</div>
        <div class="model-sub">{html.escape(m["vendor"])}</div>
      </td>
      <td>{html.escape(m["architecture"])}</td><td>{html.escape(m["active_params"])}</td><td>{html.escape(m["context"])}</td>
      <td class="right">{est_pfx}${m["input_per_m"]:.2f}</td><td class="right">{est_pfx}${m["output_per_m"]:.2f}</td>
      <td class="{hl_cls}"><span class="hl-val">{blended_val}</span></td>
      <td class="right">{aa_val}{warn}</td>
      <td>{bar}</td>
      <td>{aa_idx_html}</td>
      <td>{html.escape(m["notes"])}{flag}</td>
    </tr>''')

    meta = data["meta"]
    bk = meta["blend"]
    cin = bk["cached_input_tokens"] // 1000
    cmin = bk["cache_miss_input_tokens"] // 1000
    out_k = bk["output_tokens"] // 1000
    hit = int(bk["cache_hit_fraction_of_input"] * 100)

    logo_html = ""
    try:
        with open("logo.b64", encoding="utf-8") as lf:
            b64 = lf.read().strip()
        logo_html = (f'<div class="logo-chip">'
                     f'<img src="data:image/png;base64,{b64}" alt="Merloni Holding"></div>')
    except FileNotFoundError:
        pass

    return TEMPLATE.format(
        title=html.escape(meta["title"]),
        subtitle=html.escape(meta["subtitle"]),
        updated=html.escape(meta["last_updated"]),
        cin=cin, cmin=cmin, out_k=out_k, hit=hit,
        logo=logo_html,
        rows="\n".join(rows),
    )


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0f1117; color: #e2e8f0; padding: 32px 28px; font-size: 13px;
  }}
  h1 {{ font-size: 16px; font-weight: 600; color: #f8fafc; margin-bottom: 4px; letter-spacing: -0.3px; }}
  .subtitle {{ color: #64748b; font-size: 11.5px; margin-bottom: 14px; }}
  .assumptions {{ color: #94a3b8; font-size: 11px; margin-bottom: 10px; line-height: 1.8; border-left: 2px solid #2d3748; padding-left: 10px; }}
  .assumptions b {{ color: #cbd5e1; }}
  .aa-note {{ background: #1a1f2e; border: 1px solid #2d3748; border-radius: 4px; padding: 8px 12px; font-size: 11px; color: #94a3b8; margin-bottom: 22px; line-height: 1.7; }}
  .aa-note b {{ color: #fbbf24; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th {{ background: #161b27; color: #64748b; font-weight: 500; text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px; padding: 9px 12px; text-align: left; border-bottom: 1px solid #2d3748; white-space: nowrap; }}
  th.right, td.right {{ text-align: right; }}
  th.hl {{ background: #2a2200; color: #fde68a; font-weight: 700; text-align: right; border-bottom: 2px solid #f59e0b; }}
  td {{ padding: 9px 12px; border-bottom: 1px solid #1a2030; vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #13181f; }}
  td.hl {{ background: #1c1600; text-align: right; border-left: 1px solid #3a2e00; border-right: 1px solid #3a2e00; }}
  tr:hover td.hl {{ background: #221a00; }}
  .section-header td {{ background: #0d1117; color: #475569; font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.9px; padding: 6px 12px; border-bottom: 1px solid #2d3748; border-top: 1px solid #2d3748; }}
  .sec-western td {{ color: #60a5fa; }}
  .sec-usopen  td {{ color: #4ade80; }}
  .sec-chinese td {{ color: #f87171; }}
  .sec-ref     td {{ color: #475569; }}
  .model-name {{ font-weight: 500; color: #e2e8f0; white-space: nowrap; }}
  .model-sub  {{ color: #4a5568; font-size: 10px; margin-top: 2px; }}
  .badge {{ display: inline-block; font-size: 9px; font-weight: 600; padding: 1px 5px; border-radius: 3px; text-transform: uppercase; letter-spacing: 0.3px; margin-left: 5px; vertical-align: middle; }}
  .closed  {{ background: #1e3a5f; color: #60a5fa; }}
  .oai     {{ background: #1a3020; color: #34d399; }}
  .open    {{ background: #14432a; color: #4ade80; }}
  .chinese {{ background: #3b1f1f; color: #f87171; }}
  .nvidia  {{ background: #1a2e1a; color: #76c442; }}
  .warn    {{ background: #3b2e10; color: #fbbf24; font-size: 8px; }}
  .hl-val {{ color: #fde68a; font-weight: 700; }}
  .aa-idx {{ display: inline-block; font-size: 11px; font-weight: 600; color: #e2e8f0; background: #1e2533; border-radius: 3px; padding: 1px 6px; white-space: nowrap; }}
  .bar-cell {{ width: 68px; }}
  .bar-wrap {{ display: flex; align-items: center; }}
  .bar {{ height: 5px; border-radius: 3px; min-width: 2px; }}
  .dimmed {{ opacity: 0.42; }}
  .page-header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }}
  .logo-chip {{ background: #ffffff; border-radius: 6px; padding: 8px 14px; flex-shrink: 0; }}
  .logo-chip img {{ display: block; height: 26px; width: auto; }}
  tr.estimate td {{ background: #2a1215; border-bottom: 1px solid #7f1d1d; }}
  tr.estimate:hover td {{ background: #331519; }}
  tr.estimate td.hl {{ background: #331014; border-left: 1px solid #7f1d1d; border-right: 1px solid #7f1d1d; }}
  tr.estimate .hl-val {{ color: #fca5a5; }}
  .est-badge {{ background: #7f1d1d; color: #fecaca; font-size: 8px; }}
  .note {{ font-size: 10.5px; color: #475569; margin-top: 18px; line-height: 1.8; border-top: 1px solid #1a2030; padding-top: 14px; }}
  .note b {{ color: #64748b; }}
</style>
</head>
<body>
<div class="page-header">
  <div>
    <h1>{title}</h1>
    <p class="subtitle">{subtitle} · Auto-updated {updated}</p>
  </div>
  {logo}
</div>
<div class="assumptions">
  <b>Our blended agentic basis:</b> {cin}K cached input + {cmin}K cache-miss input + {out_k}K output per 1M total tokens.<br>
  Cache hit = {hit}% of list input price &nbsp;|&nbsp; Reflects agentic loop: large system prompt / codebase reused across turns, ~40% output by volume.
</div>
<div class="aa-note">
  <b>⚠ Artificial Analysis methodology differences:</b>&nbsp;
  (1) AA blends at 7:2:1 cache-hit/input/output — almost no output weight, making their figures 3–9× lower than ours for output-heavy models.
  (2) AA uses first-party CN-direct API prices for DeepSeek, Qwen, and Kimi — not US-hosted rates.
</div>
<table>
  <thead>
    <tr>
      <th style="width:22%">Model</th>
      <th>Architecture</th>
      <th>Active params</th>
      <th>Context</th>
      <th class="right">Input $/M</th>
      <th class="right">Output $/M</th>
      <th class="hl">Our blended {cin}K in / {out_k}K out Cost</th>
      <th class="right">AA blended<br>7:2:1</th>
      <th class="bar-cell"></th>
      <th>Intel Index v4.1.1</th>
      <th>Notes</th>
    </tr>
  </thead>
  <tbody>
{rows}
  </tbody>
</table>
<div class="note">
  <b>Intel Index v4.1:</b> Artificial Analysis Intelligence Index v4.1 (9 evals incl. GDPval-AA v2, Terminal-Bench v2.1, GPQA Diamond, HLE), verified Jul 16 2026. Single consistent scale across all models — vendor-reported benchmark scores are NOT comparable across labs. n.p. = not yet published on v4.1.<br>
  <b>Blended formula:</b> ({cmin}K cache-miss input × $/M) + ({cin}K cached × {hit}% of $/M) + ({out_k}K output × $/M), per 1M total tokens — computed deterministically.<br>
  <b>auto · verify</b> badge = qualitative claim added by the weekly agent; confirm before relying on it.
</div>
<div class="note" style="border-top:none;padding-top:6px">Prepared by <b style="color:#94a3b8">Merloni Holding</b> — internal research. Not investment advice.</div>
</body>
</html>
"""


if __name__ == "__main__":
    with open("models.json", encoding="utf-8") as f:
        data = json.load(f)
    out = render(data)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(out)
    print(f"Rendered index.html ({len(out)} bytes), {len(data['models'])} models")
