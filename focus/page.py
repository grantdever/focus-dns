"""The one page, in the author's house style: design tokens inlined, forced dark with a
LIGHT toggle, one accent, plain HTML forms, phone width first. One block per list (its sites and its
hours), the always-blocked categories, passes, and a light Insights section with one bar chart
(inline SVG on the --cat-1 token).

The only script is the LIGHT toggle; the server's CSP allows exactly that script by its hash
(SCRIPT_HASH) plus the Geist fonts from Google Fonts. Everything else is forms.
"""
from __future__ import annotations

import base64
import hashlib
from html import escape

from . import core
from . import schedule as sch
from .config import QUERY_CAP
from .report import passes_by_day, passes_this_week

SERVICE_NAMES = {"youtube": "YouTube", "twitter": "Twitter (twitter.com)"}

# The design tokens (colours, type, spacing), inlined. Swap these to restyle the page.
TOKENS = """:root{--bg:#0f0d14;--surface:#18151f;--line:#2b2636;--fg:#ecebf2;--muted:#a39db3;--wisteria:#a78bfa;--violet:#8b5cf6;--amethyst:#7c3aed;--iris:#9b7bff;--lilac:#c9b8ff;--lavender:#b9a6ff;--mist:#d9ccff;--indigo:#5b3fd6;--accent:var(--wisteria);--accent-ink:#120f1a;--accent-soft:#a78bfa24;--accent-press:var(--violet);--fill:var(--amethyst);--fill-ink:#ffffff;--fill-hover:var(--violet);--link-hover:var(--iris);--soft:#c9b8ff1f;--selection:var(--mist);--selection-ink:#120f1a;--second:#aeb657;--ok:#65c98c;--warn:#eebc4a;--crit:#f2716a;--cat-1:#9778ed;--cat-2:#02a998;--cat-3:#b58e18;--cat-4:#d65997;--cat-5:#8d9e35;--cat-6:#288dd4;--seq-1:#5b3fd6;--seq-2:#8b5cf6;--seq-3:#a78bfa;--seq-4:#b9a6ff;--seq-5:#d9ccff;--div-p3:#a78bfa;--div-p2:#856fc6;--div-p1:#665992;--div-mid:#3d3b47;--div-g1:#636837;--div-g2:#80873b;--div-g3:#aeb657;--f-text:"Geist",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;--f-mono:"Geist Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--s2:8px;--s3:12px;--s4:16px;--s5:24px;--s6:40px;--s7:64px;color-scheme:dark;}
:root.light{--bg:#f9f8fc;--surface:#ffffff;--line:#e2deec;--fg:#16131d;--muted:#5f586f;--accent:#5a3fd6;--accent-ink:#ffffff;--accent-soft:#5a3fd614;--accent-press:var(--amethyst);--fill:var(--amethyst);--fill-ink:#ffffff;--fill-hover:var(--violet);--link-hover:var(--accent);--soft:#c9b8ff40;--selection:var(--mist);--selection-ink:#16131d;--second:#636919;--ok:#0b7643;--warn:#886602;--crit:#ba2b2e;--cat-1:#5a3fd6;--cat-2:#039585;--cat-3:#a68003;--cat-4:#ba367c;--cat-5:#748410;--cat-6:#036eae;--seq-1:#a78bfa;--seq-2:#9171f5;--seq-3:#7c55f0;--seq-4:#6442dc;--seq-5:#5035bd;--div-p3:#5a3fd6;--div-p2:#9179d8;--div-p1:#b3a6e5;--div-mid:#e1e0eb;--div-g1:#b0b77f;--div-g2:#8c9250;--div-g3:#636919;color-scheme:light;}
::selection{background:var(--selection);color:var(--selection-ink);}"""

# Base rules (body, .page, h1/h2, .mono, .theme,
# :focus-visible, .choice buttons, inputs), then this page's few additions.
CSS = TOKENS + """
.theme{font:inherit;background:none;border:0;padding:0;color:var(--muted);cursor:pointer;text-decoration:underline;text-underline-offset:3px}
.theme:hover{color:var(--fg)}
*{box-sizing:border-box}
html{background:var(--bg)}
body{background:var(--bg);color:var(--fg);font-family:var(--f-text);font-size:16px;line-height:1.5;margin:0;-webkit-font-smoothing:antialiased}
.page{max-width:760px;margin:0 auto;padding-inline:16px;padding-block:var(--s6) var(--s7);display:grid;grid-template-columns:minmax(0,1fr);gap:var(--s7)}
.page>*{min-width:0}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.mono{font-family:var(--f-mono);font-size:12px;letter-spacing:.02em;color:var(--muted)}
h1{font-size:clamp(30px,5vw,42px);line-height:1.08;font-weight:700;letter-spacing:-.025em;margin:var(--s2) 0 0;text-wrap:balance}
h2{font-size:22px;font-weight:600;letter-spacing:-.015em;margin:0;text-wrap:balance}
p{margin:0;max-width:62ch}
section{display:grid;gap:var(--s4)}
form{margin:0}
.num{font-family:var(--f-mono);font-variant-numeric:tabular-nums}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}

/* State: the accent lands here only when there is no pass to take */
header{display:grid;gap:var(--s3)}
.now{display:grid;gap:4px}
.now.lead{border-left:3px solid var(--accent);padding:var(--s2) 0 var(--s2) var(--s4)}
.now.lead .mono{color:var(--accent)}
.then{color:var(--muted)}
.flash{border-left:3px solid var(--line);padding:var(--s2) 0 var(--s2) var(--s4);display:grid;gap:2px}
.flash .mono.crit{color:var(--crit)}

/* Buttons and fields (the template's .choice button and textarea, at 16px so iOS does not zoom) */
.btn{font:500 16px var(--f-text);padding:10px 16px;border:1px solid var(--line);background:var(--surface);color:var(--fg);cursor:pointer;border-radius:0}
.btn:hover:not(:disabled){border-color:var(--muted)}
.btn:disabled{color:var(--muted);cursor:default}
.btn.big{width:100%;padding:18px 12px;font-size:18px;font-weight:600}
.btn.fill{background:var(--fill);border-color:var(--fill);color:var(--fill-ink)}
.btn.fill:hover{background:var(--fill-hover);border-color:var(--fill-hover)}
.link{font:inherit;background:none;border:0;padding:6px 0 6px 12px;color:var(--muted);cursor:pointer;text-decoration:underline;text-underline-offset:3px}
.link:hover{color:var(--fg)}
input,select{font:16px/1.4 var(--f-text);padding:8px 10px;border:1px solid var(--line);background:var(--surface);color:var(--fg);border-radius:0;min-width:0}
input[type=time],input[type=number]{font-family:var(--f-mono);font-variant-numeric:tabular-nums}
label{color:var(--muted);font-size:15px}

/* Actions */
.acts{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:var(--s3)}
.act{display:grid;gap:6px;align-content:start}
.until{grid-column:1/-1;display:flex;flex-wrap:wrap;align-items:center;gap:var(--s2)}
.until input{width:auto;min-width:8rem}

/* Rows: a 1px line separates, nothing else */
.rows{list-style:none;margin:0;padding:0}
.rows li{display:flex;justify-content:space-between;align-items:center;gap:var(--s3);min-height:44px;padding-block:4px;border-bottom:1px solid var(--line)}
.rows li:last-child{border-bottom:0}
.rows li>span:first-child{overflow-wrap:anywhere}
.add{display:flex;gap:var(--s2)}
.add input{flex:1}
.pend{font-family:var(--f-mono);font-size:12px;letter-spacing:.02em;color:var(--warn);text-transform:uppercase}
.note{color:var(--muted);font-size:15px}

/* Hours */
.days{display:grid}
.day{display:grid;grid-template-columns:3rem minmax(0,1fr);gap:6px var(--s3);align-items:center;padding-block:var(--s3);border-bottom:1px solid var(--line)}
.day:last-child{border-bottom:0}
.day>.mono{font-size:13px;color:var(--fg)}
.day select{width:100%}
.times,.day .pendrow{grid-column:2;display:flex;flex-wrap:wrap;align-items:center;gap:var(--s2) var(--s3)}
.times{flex-direction:column;align-items:flex-start}
.times label{display:flex;align-items:center;gap:6px}
.times input{width:auto;min-width:0;padding-inline:8px}
.win .to{color:var(--muted);font-size:15px}
.win .link{padding-left:4px}
.day:not(:has(option[value=from]:checked)) .times{display:none}

/* Passes */
.week{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(7,minmax(0,1fr));border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.week li{display:grid;justify-items:center;gap:2px;padding-block:var(--s2)}
.week .num{font-size:18px}
.week .ahead .num{color:var(--muted)}
.week .today .mono,.week .today .num{color:var(--fg);font-weight:600}
.week .today .mono{text-decoration:underline;text-underline-offset:3px}
.lim{display:flex;flex-wrap:wrap;align-items:end;gap:var(--s3)}
.lim label{display:grid;gap:4px}
.lim input{width:6rem}

/* Lists */
.list{display:grid;gap:var(--s4)}
.list h2 .mono{margin-left:var(--s2)}
details>summary{cursor:pointer;color:var(--muted);font-size:15px;padding-block:var(--s2);list-style-position:outside}
details>summary:hover{color:var(--fg)}
details[open]>summary{margin-bottom:var(--s2)}
.win{display:flex;flex-wrap:wrap;align-items:center;gap:var(--s2)}
.newlist{display:flex;gap:var(--s2);flex-wrap:wrap}
.newlist input{flex:1;min-width:10rem}

/* Insights: one chart, recessive axes */
h3{font-size:16px;font-weight:600;margin:0}
.ins{display:grid;gap:var(--s5)}
.ins>div{display:grid;gap:var(--s3)}
.chart{position:relative;height:140px;margin-top:var(--s2)}
.chart svg{display:block;width:100%;height:100%;overflow:visible}
.chart .grid{stroke:var(--line);stroke-width:1;vector-effect:non-scaling-stroke}
.chart .bar{fill:var(--cat-1)}
.chart .hit{fill:transparent}
.chart:hover .bar{opacity:.55}
.chart .col:hover .bar{opacity:1}
.tick,.peak{position:absolute;font-family:var(--f-mono);font-size:11px;font-variant-numeric:tabular-nums;color:var(--muted);line-height:1}
.tick{left:0;transform:translateY(-120%)}
.peak{transform:translate(-50%,-4px);color:var(--fg)}
.xaxis{display:flex;justify-content:space-between}
table{border-collapse:collapse;width:100%;font-size:15px}
th{font-family:var(--f-mono);font-size:11px;font-weight:500;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);text-align:left;padding:0 var(--s3) var(--s2) 0;border-bottom:1px solid var(--line)}
td{padding:6px var(--s3) 6px 0;border-bottom:1px solid var(--line)}
td.num,th.num{text-align:right}
.scroll{overflow-x:auto;max-height:22rem;overflow-y:auto}

/* Lookups */
.bar{height:4px;background:var(--line)}
.bar div{height:100%;background:var(--muted)}
footer{padding-top:var(--s2)}
@media (prefers-reduced-motion: reduce){*{transition:none!important}}
"""

# The LIGHT toggle from the review template, unchanged in behaviour. Pinned in the CSP by its hash.
SCRIPT = ("(function(){const root=document.documentElement,b=document.getElementById(\"theme\");"
          "const apply=on=>{root.classList.toggle(\"light\",on);b.textContent=on?\"DARK\":\"LIGHT\";};"
          "let on=false;try{on=localStorage.getItem(\"theme\")===\"light\";}catch(e){}"
          "apply(on);"
          "b.onclick=()=>{on=!on;apply(on);try{localStorage.setItem(\"theme\",on?\"light\":\"dark\");}catch(e){}};})();")
SCRIPT_HASH = "'sha256-" + base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode() + "'"

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700'
         '&amp;family=Geist+Mono:wght@400;500&amp;display=swap">')


def _post(action: str, label: str, cls: str = "btn", disabled: bool = False, hidden: dict | None = None,
          form_id: str | None = None) -> str:
    h = "".join(f'<input type="hidden" name="{escape(k)}" value="{escape(v)}">' for k, v in (hidden or {}).items())
    fid = f' id="{escape(form_id)}"' if form_id else ""
    if label is None:  # a bare form that a button elsewhere submits through form="..."
        return f'<form method="post" action="{action}"{fid}>{h}</form>'
    dis = " disabled" if disabled else ""
    return f'<form method="post" action="{action}"{fid}>{h}<button class="{cls}"{dis}>{escape(label)}</button></form>'


def _undo_forms(st: dict) -> str:
    """One bare /undo form per queued change; Undo buttons inside other forms point here with form=."""
    return "".join(_post("/undo", None, hidden={"id": p["id"]}, form_id=_fid(p["id"])) for p in core.pending(st))


def _fid(pid: str) -> str:
    return "undo-" + "".join(c if c.isalnum() else "-" for c in pid)


def _undo_btn(pid: str) -> str:
    return f'<button class="link" form="{_fid(pid)}">Undo</button>'


def slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower())


def _hours_rows(st: dict, L: dict, more_day: str | None, ts: float) -> str:
    q = {p["day"]: p for p in core.pending(st)
         if p.get("id", "").startswith("hours:") and (p.get("list") or "").lower() == L["name"].lower()}
    rows = []
    for d in sch.DAYS:
        r = sch.normalize_rule(L["hours"][d])
        kind = "from" if r not in ("blocked", "open") or d == more_day else r
        pairs = sch.window_pairs(r) if kind == "from" else [("17:00", sch.MIDNIGHT)]
        if d == more_day and r == "blocked":
            pairs = []
        if d == more_day:
            pairs = pairs + [("", "")]
        opts = "".join(f'<option value="{v}"{" selected" if v == kind else ""}>{lab}</option>'
                       for v, lab in (("blocked", "Blocked all day"), ("open", "Open all day"), ("from", "Open during…")))
        name = d.capitalize()
        wins = []
        real = sum(1 for a, _ in pairs if a)
        for i, (a, b) in enumerate(pairs):
            b = "00:00" if b == sch.MIDNIGHT else b
            drop = (f'<button class="link" name="drop" value="{d}:{i}" aria-label="Remove {name} window {i + 1}">Remove</button>'
                    if real > 1 and a else "")
            wins.append(f'<div class="win"><input type="time" name="start_{d}_{i}" value="{a}" aria-label="{name} window {i + 1} opens at">'
                        f'<span class="to">to</span><input type="time" name="end_{d}_{i}" value="{b}" aria-label="{name} window {i + 1} closes at">{drop}</div>')
        plus = (f'<button class="link" name="plus" value="{d}" aria-label="Add a window on {name}">+ Window</button>'
                if len(pairs) < sch.MAX_WINDOWS else "")
        pend = ""
        if d in q:
            pend = (f'<p class="pendrow"><span class="pend">At {escape(core.pending_when(st, ts, q[d]))}: '
                    f'{escape(sch.describe_rule(q[d]["rule"]))}</span>{_undo_btn(q[d]["id"])}</p>')
        rows.append(
            f'<div class="day"><span class="mono">{sch.ABBR[d].upper()}</span>'
            f'<select name="rule_{d}" aria-label="{name}">{opts}</select>'
            f'<div class="times">{"".join(wins)}{plus}</div>{pend}</div>')
    return "".join(rows)


def _list_block(st: dict, L: dict, info: dict, ts: float, more: tuple | None) -> str:
    sl = slug(L["name"])
    locked = core.list_block_in_force(st, L, ts) and not core.is_empty(L)
    until = core.unlock_words(st, ts, L) if locked else "now"
    items = [f'<li><span>{escape(SERVICE_NAMES.get(sid, sid))}</span><span class="mono">SERVICE</span></li>'
             for sid in L["services"]]
    for d in L["domains"]:
        p = core.find_pending(st, f"remove:{d}")
        if p:
            right = f'<span><span class="pend">Removes at {escape(until)}</span>{_undo_btn(p["id"])}</span>'
        else:
            right = _post("/remove", "Remove", "link", hidden={"domain": d})
        items.append(f"<li><span>{escape(d)}</span>{right}</li>")
    if not items:
        items.append('<li><span class="note">No sites yet.</span></li>')
    ls = info["lists"][core.lists(st).index(L)]
    state = f'{"BLOCKED" if ls["blocked"] else "OPEN"} UNTIL {ls["until"]}'.upper()
    more_day = more[1] if more and more[0] == L["name"].lower() else None
    has_pend = any((p.get("list") or "").lower() == L["name"].lower() and p["id"].startswith("hours:")
                   for p in core.pending(st))
    is_open = " open" if (more_day or has_pend) else ""
    hours_note = ("Shorter or fewer windows apply now; longer or extra ones wait for this list's unlock. " if locked else "") + \
        f"Times are {escape(st['settings']['timezone'])}. An end of 00:00 means midnight."
    return (f'<section class="list" id="list-{sl}" aria-labelledby="h-{sl}">'
            f'<h2 id="h-{sl}">{escape(L["name"])}<span class="mono">{escape(state)}</span></h2>'
            f'<ul class="rows">{"".join(items)}</ul>'
            f'<form method="post" action="/add" class="add"><input type="hidden" name="list" value="{escape(L["name"])}">'
            f'<label class="sr" for="domain-{sl}">Domain to block on {escape(L["name"])}</label>'
            f'<input id="domain-{sl}" name="domain" placeholder="example.com" autocapitalize="off" autocomplete="off" '
            f'spellcheck="false" inputmode="url" required><button class="btn">Add</button></form>'
            + ('<p class="note">During this list\'s block, a removal waits for its unlock.</p>' if locked else "")
            + f'<details id="hours-{sl}"{is_open}><summary>Hours · {escape(sch.summarize_week(L["hours"]))}</summary>'
            f'<form method="post" action="/hours"><button class="sr" tabindex="-1" aria-hidden="true">Save hours</button>'
            f'<input type="hidden" name="list" value="{escape(L["name"])}">'
            f'<div class="days">{_hours_rows(st, L, more_day, ts)}</div>'
            f'<p style="margin-top:var(--s3)"><button class="btn">Save hours</button></p></form>'
            f'<p class="note">{hours_note}</p></details></section>')


def _always_block(st: dict, ts: float) -> str:
    cats = core.categories(st)
    if not cats:
        return ""
    rows = []
    for c in cats:
        name = escape(core.category_name(c["id"]))
        p = core.find_pending(st, f"category:{c['id']}")
        if not c["active"]:
            right = (f'<span><span class="mono">OFF</span>'
                     + _post("/category", "On", "link", hidden={"id": c["id"], "on": "1"}).replace("<form", '<form style="display:inline"') + "</span>")
        elif p:
            right = f'<span><span class="pend">Off at {escape(core.pending_when(st, ts, p))}</span>{_undo_btn(p["id"])}</span>'
        else:
            right = _post("/category", "Off", "link", hidden={"id": c["id"], "on": "0"})
        rows.append(f"<li><span>{name}</span>{right}</li>")
    note = "No hours, and a pass never opens these."
    if core.block_in_force(st, ts):
        note += " During a block, Off waits for the unlock."
    return (f'<section aria-labelledby="h-always"><h2 id="h-always">Always blocked</h2>'
            f'<ul class="rows">{"".join(rows)}</ul><p class="note">{note}</p></section>')


def _nice_max(m: int) -> int:
    if m <= 0:
        return 1
    k = 1
    while True:
        for f in (1, 2, 5):
            if f * k >= m:
                return f * k
        k *= 10


def _compact(n: int) -> str:
    return f"{n / 1000:g}K" if n >= 1000 else str(n)


def chart_svg(days: list[dict]) -> str:
    """Lookups per day as columns: one series (--cat-1), 2px gaps, rounded data-ends, square at the
    baseline, hairline grid, the peak labelled, a native tooltip per column. Labels are HTML so
    they keep their size while the SVG stretches to the column width."""
    n = len(days)
    if not n:
        return ""
    W, H = 300.0, 140.0
    top = _nice_max(max(d["queries"] for d in days))
    slot = W / n
    bw = min(24.0, slot - 2.0)
    cols, peak_i = [], max(range(n), key=lambda i: days[i]["queries"])
    for i, d in enumerate(days):
        x = i * slot + (slot - bw) / 2
        h = H * d["queries"] / top
        tip = f'{_date_words(d["date"])}: {d["queries"]:,} lookups, {d["blocked"]:,} blocked'
        bar = ""
        if h > 0:
            r = min(4.0, h, bw / 2)
            y = H - h
            bar = (f'<path class="bar" d="M{x:.2f},{H:.2f}V{y + r:.2f}Q{x:.2f},{y:.2f} {x + r:.2f},{y:.2f}'
                   f'H{x + bw - r:.2f}Q{x + bw:.2f},{y:.2f} {x + bw:.2f},{y + r:.2f}V{H:.2f}Z"/>')
        cols.append(f'<g class="col"><title>{escape(tip)}</title>'
                    f'<rect class="hit" x="{i * slot:.2f}" y="0" width="{slot:.2f}" height="{H:.0f}"/>{bar}</g>')
    grid = "".join(f'<line class="grid" x1="0" x2="{W:.0f}" y1="{y:.1f}" y2="{y:.1f}"/>' for y in (0.5, H / 2, H - 0.5))
    pk = days[peak_i]["queries"]
    peak = ""
    if pk > 0:
        left = (peak_i + 0.5) / n * 100
        bottom = pk / top * 100
        peak = f'<span class="peak" style="left:{left:.2f}%;bottom:{bottom:.1f}%">{pk:,}</span>'
    ticks = (f'<span class="tick" style="top:0">{_compact(top)}</span>'
             f'<span class="tick" style="top:50%">{_compact(top // 2) if top > 1 else ""}</span>')
    return (f'<div class="chart"><svg viewBox="0 0 {W:.0f} {H:.0f}" preserveAspectRatio="none" role="img" '
            f'aria-label="Lookups per day for the last {n} days, peak {pk:,}">{grid}{"".join(cols)}</svg>{ticks}{peak}</div>'
            f'<div class="xaxis mono"><span>{escape(_date_words(days[0]["date"]))}</span><span>TODAY</span></div>')


def _date_words(iso: str) -> str:
    try:
        y, m, d = map(int, iso.split("-"))
        import datetime as _dt
        return _dt.date(y, m, d).strftime("%a %b %-d")
    except (ValueError, AttributeError):
        return iso


def _rows(items: list[tuple[str, int]]) -> str:
    return "".join(f'<li><span>{escape(a)}</span><span class="num">{b:,}</span></li>' for a, b in items)


def insights_html(st: dict, ts: float, ins: dict | None) -> str:
    when = lambda t: core.when(st, t, ts)  # noqa: E731
    u = st.get("usage") or {}
    q = int(u.get("queries", 0))
    pct = min(100.0, q / QUERY_CAP * 100)
    usage = (f'<p><span class="num">{q:,}</span> of <span class="num">{QUERY_CAP:,}</span> lookups this month '
             f'(<span class="num">{pct:.1f}%</span>)</p><div class="bar"><div style="width:{pct:.1f}%"></div></div>'
             f'<p class="note">Updated {escape(when(u["at"]))}. Past 200,000 by the 20th: decide on a paid plan.</p>'
             if u else "<p>Lookups this month: not fetched yet.</p>")
    parts = [f"<div>{usage}</div>"]
    if not ins or not ins.get("ok"):
        parts.append('<div><h3>Lookups per day</h3><p class="note">No data yet.</p></div>')
        return "".join(parts)
    days = ins.get("days") or []
    if days and any(d["queries"] for d in days):
        table = "".join(f'<tr><td>{escape(_date_words(d["date"]))}</td><td class="num">{d["queries"]:,}</td>'
                        f'<td class="num">{d["blocked"]:,}</td></tr>' for d in reversed(days))
        parts.append(f'<div><h3>Lookups per day</h3><p class="mono">LAST {len(days)} DAYS · ALL DEVICES</p>'
                     f'{chart_svg(days)}<details><summary>As a table</summary><div class="scroll"><table>'
                     f'<thead><tr><th>Day</th><th class="num">Lookups</th><th class="num">Blocked</th></tr></thead>'
                     f'<tbody>{table}</tbody></table></div></details></div>')
    else:
        parts.append('<div><h3>Lookups per day</h3><p class="note">No lookups in the last 30 days.</p></div>')
    m = ins.get("month") or {}
    tot, blk = int(m.get("total", 0)), int(m.get("blocked", 0))
    share = (f'<p>Blocked this month: <span class="num">{blk / tot:.0%}</span> '
             f'(<span class="num">{blk:,}</span> of <span class="num">{tot:,}</span> lookups).</p>'
             if tot else "<p>Blocked this month: no lookups yet.</p>")
    top = ins.get("top_blocked") or []
    top_html = (f'<h3>Top blocked</h3><ol class="rows">{_rows([(t["domain"], t["queries"]) for t in top])}</ol>'
                if top else "<h3>Top blocked</h3><p class=\"note\">Nothing blocked yet.</p>")
    reasons = ins.get("reasons") or []
    by_rule = (f'<p class="note">By rule: ' + ", ".join(f'{escape(r["name"])} <span class="num">{r["queries"]:,}</span>'
                                                       for r in reasons) + ".</p>") if reasons else ""
    parts.append(f'<div>{share}</div><div>{top_html}{by_rule}</div>')
    wk = ins.get("week")
    if wk and (wk.get("devices") or wk.get("sites")):
        hm = lambda m: f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"  # noqa: E731
        dev_rows = "".join(f'<li><span>{escape(d["name"])}</span><span class="num">{hm(d["minutes"])}</span></li>'
                           for d in wk["devices"])
        site_rows = "".join(f'<li><span>{escape(x["site"])}</span><span class="num">{hm(x["minutes"])}</span></li>'
                            for x in wk["sites"])
        parts.append(f'<div><h3>Time online this week</h3><ul class="rows">{dev_rows}</ul>'
                     f'<h3>Top sites</h3><ol class="rows">{site_rows}</ol>'
                     f'<p class="note">Estimated from lookups since Monday, to within a few minutes a visit. '
                     f'Kept on this server only.</p></div>')
    devs = ins.get("devices") or []
    if devs:
        parts.append(f'<div><h3>By device</h3><ul class="rows">{_rows([(d["name"], d["queries"]) for d in devs])}</ul>'
                     f'<p class="note">Last 30 days. Updated {escape(when(ins.get("at")))}.</p></div>')
    return "".join(parts)


def render(st: dict, ts: float, flash: str | None = None, flash_err: bool = False,
           insights: dict | None = None, more: tuple | None = None) -> str:
    s = st["settings"]
    info = core.describe(st, ts)
    when = lambda t: core.when(st, t, ts)  # noqa: E731
    locked = info["locked"]
    until = core.unlock_words(st, ts) if locked else "now"

    # Pass: the primary action when one can be taken; otherwise the state line carries the accent.
    left = info["passes_left"]
    if info["pass_active"]:
        pass_label, pass_ok, pass_sub = "Pass running", False, f"OPEN UNTIL {when(st['pass_until'])}"
    elif left <= 0:
        pass_label, pass_ok, pass_sub = "Pass · 0 left", False, "MORE AT MIDNIGHT"
    elif not core.block_in_force(st, ts):
        pass_label, pass_ok, pass_sub = f"Pass · {s['pass_minutes']} min", False, "NOTHING BLOCKED NOW"
    else:
        pass_label, pass_ok, pass_sub = f"Pass · {s['pass_minutes']} min", True, f"{left} OF {s['passes_per_day']} LEFT TODAY"
    opens = [sch.next_sched_open(core.lsch(st, L), ts) for L in core.lists(st) if core.list_sched_blocked(st, L, ts)]
    opens = [o for o in opens if o]
    blk_sub = f"{core.BLOCK_NOW_MIN} MIN, OR TO {when(max(opens))}" if opens else f"{core.BLOCK_NOW_MIN} MIN"

    flash_html = ""
    if flash:
        tag = '<span class="mono crit">NOT DONE</span>' if flash_err else '<span class="mono">DONE</span>'
        flash_html = f'<div class="flash" role="status">{tag}<p>{escape(flash)}</p></div>'
    if info["pass_active"]:
        word = "PASS RUNNING"
    elif info["blocked_all"]:
        word = "BLOCKED"
    elif info["blocked"]:
        word = "PARTLY BLOCKED"
    else:
        word = "OPEN"
    now_html = (f'<div class="now{"" if pass_ok else " lead"}"><span class="mono">NOW · {word}</span>'
                f'<h1>{escape(info["headline"])}.</h1><p class="then">{escape(info["next"])}</p></div>')

    lists_html = "".join(_list_block(st, L, info, ts, more) for L in core.lists(st))
    newlist = ('<form method="post" action="/list" class="newlist"><label class="sr" for="newlist">New list name</label>'
               '<input id="newlist" name="name" placeholder="New list" maxlength="24" autocomplete="off" required>'
               '<button class="btn">Add list</button></form>') if len(core.lists(st)) < core.MAX_LISTS else ""

    # Passes
    week = passes_by_day(st, ts)
    today = sch.local(s, ts).date()
    cells = "".join(
        f'<li class="{"today" if d == today else ("ahead" if d > today else "")}">'
        f'<span class="mono">{d:%a}</span><span class="num">{n if d <= today else "·"}</span></li>' for d, n in week)
    lim_pend = ""
    for key, unit in (("passes", "passes a day"), ("minutes", "min a pass")):
        p = core.find_pending(st, key)
        if p:
            lim_pend += f'<p><span class="pend">At {escape(until)}: {p["value"]} {unit}</span>{_undo_btn(p["id"])}</p>'
    tzp = core.find_pending(st, "timezone")
    tz_pend = (f'<p><span class="pend">At {escape(until)}: timezone {escape(tzp["value"])}</span>{_undo_btn(tzp["id"])}</p>'
               if tzp else "")

    lr = st.get("last_reconcile") or {}
    sync = ("LAST SYNC WITH NEXTDNS: NEVER" if not lr else
            f"LAST SYNC WITH NEXTDNS: {escape(when(lr.get('at')))} · "
            + ("OK" if lr.get("ok") else f"FAILED ({escape(str(lr.get('error'))[:120])})"))

    stamp = sch.local(s, ts).strftime("%a %H:%M").upper()
    lim_note = ('<p class="note">Fewer or shorter passes apply now; more or longer wait until no list is blocked.</p>'
                if locked else "")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><meta name="referrer" content="no-referrer"><title>Focus</title>
{FONTS}<style>{CSS}</style></head><body><main class="page">
<header>
<div class="mono">FOCUS · {stamp} · <button type="button" class="theme" id="theme">LIGHT</button></div>
{now_html}
{flash_html}
</header>
<section class="acts" aria-label="Pass and block">
<div class="act">{_post("/pass", pass_label, "btn big fill" if pass_ok else "btn big", disabled=not pass_ok)}<p class="mono">{escape(pass_sub)}</p></div>
<div class="act">{_post("/block", "Block now", "btn big")}<p class="mono">{escape(blk_sub)}</p></div>
<form method="post" action="/block" class="until"><label for="until">Block until</label><input type="time" id="until" name="until" required><button class="btn">Block</button><span class="mono">TODAY · ALL LISTS</span></form>
</section>
{lists_html}
{newlist}
{_always_block(st, ts)}
<section aria-labelledby="h-passes"><h2 id="h-passes">Passes</h2><p>A pass opens every list at once. Passes used this week: <span class="num">{passes_this_week(st, ts)}</span></p>
<ol class="week" aria-label="This week, Monday to Sunday">{cells}</ol>
<form method="post" action="/limits" class="lim">
<label>Per day<input type="number" name="passes" min="0" max="{core.MAX_PASSES}" value="{s['passes_per_day']}"></label>
<label>Minutes<input type="number" name="minutes" min="1" max="{core.MAX_PASS_MIN}" value="{s['pass_minutes']}"></label>
<button class="btn">Save</button></form>{lim_pend}{lim_note}{tz_pend}</section>
<section class="ins" aria-labelledby="h-ins"><h2 id="h-ins">Insights</h2>{insights_html(st, ts, insights)}</section>
<footer class="mono">{sync}</footer>
{_undo_forms(st)}
</main><script>{SCRIPT}</script></body></html>
"""
