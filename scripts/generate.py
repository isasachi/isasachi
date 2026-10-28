#!/usr/bin/env python3
"""Genera los SVG estilo terminal del perfil y las tarjetas de proyectos del README.

  GITHUB_TOKEN=xxx python scripts/generate.py          # datos reales (lo usa el workflow)
  python scripts/generate.py --fixture data.json --avatar foto.png   # sin red, para probar
  GITHUB_TOKEN=xxx python scripts/generate.py --dump data.json       # guarda los datos crudos
"""
from __future__ import annotations

import argparse
import colorsys
import datetime as dt
import io
import json
import math
import os
import random
import re
from html import escape
from pathlib import Path

import requests
import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
README = ROOT / "README.md"
API = "https://api.github.com/graphql"
FONT = "'JetBrains Mono','Fira Code','SF Mono',Menlo,Consolas,'DejaVu Sans Mono','Liberation Mono',monospace"
CW = 0.62  # ancho de carácter / tamaño de fuente (margen para fuentes anchas)

THEMES = {
    "dark": dict(bg="#0d1117", bar="#161b22", border="#30363d", text="#c9d1d9", muted="#6e7681",
                 dots="#3d444d", key="#ff7b72", head="#56d4dd", accent="#d2a8ff", ok="#7ee787",
                 warn="#e3b341", mx=["#145c2a", "#1c8a3c", "#24b14c", "#3ee06a", "#8dffa8"], mxg="#d4ffdd",
                 lv=["#21262d", "#0e4429", "#006d32", "#26a641", "#39d353"]),
    "light": dict(bg="#ffffff", bar="#f6f8fa", border="#d0d7de", text="#1f2328", muted="#656d76",
                  dots="#c4cbd2", key="#cf222e", head="#0a7a8a", accent="#8250df", ok="#1a7f37",
                  warn="#9a6700", mx=["#6fbf86", "#3f9e5c", "#1f8240", "#126b30", "#0a4f21"], mxg="#0a3d18",
                  lv=["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"]),
}

MONTHS_ES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


# ───────────────────────────── GitHub API ─────────────────────────────

def gql(token: str, query: str, variables: dict | None = None) -> dict:
    r = requests.post(API, json={"query": query, "variables": variables or {}},
                      headers={"Authorization": f"bearer {token}"}, timeout=60)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        # Errores parciales (p. ej. un repo borrado) no detienen todo
        print("⚠ GraphQL:", "; ".join(e.get("message", "") for e in body["errors"]))
    return body.get("data") or {}


LANG_FRAGMENT = "languages(first: 12, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name color } } }"


def fetch(cfg: dict, token: str) -> dict:
    login = cfg["login"]
    u = gql(token, """
      query($login: String!) {
        user(login: $login) {
          id login name createdAt avatarUrl(size: 256) location company twitterUsername
          followers { totalCount }
          repositoriesContributedTo(first: 1, includeUserRepositories: false,
            contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY]) { totalCount }
          contributionsCollection {
            contributionYears
            contributionCalendar { totalContributions
              weeks { contributionDays { date contributionCount contributionLevel } } }
          }
        }
      }""", {"login": login})["user"]

    # Repos propios (paginado)
    repos, cursor = [], None
    while True:
        page = gql(token, f"""
          query($login: String!, $after: String) {{
            user(login: $login) {{
              repositories(first: 100, after: $after, ownerAffiliations: OWNER, privacy: PUBLIC) {{
                totalCount pageInfo {{ hasNextPage endCursor }}
                nodes {{ nameWithOwner isFork stargazerCount {LANG_FRAGMENT} }}
              }}
            }}
          }}""", {"login": login, "after": cursor})["user"]["repositories"]
        repos += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    # Commits totales: suma por año
    years = u["contributionsCollection"]["contributionYears"]
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = lambda y: min(f"{y}-12-31T23:59:59Z", now)  # la API no acepta fechas futuras
    parts = [f'y{y}: contributionsCollection(from: "{y}-01-01T00:00:00Z", to: "{end(y)}") '
             "{ totalCommitContributions restrictedContributionsCount }" for y in years]
    yd = gql(token, f'query($login: String!) {{ user(login: $login) {{ {" ".join(parts)} }} }}',
             {"login": login})["user"] if parts else {}
    commits = sum(v["totalCommitContributions"] for v in yd.values())

    # Proyectos destacados (propios + equipo), con tus commits en cada uno
    project_cfg = cfg.get("featured", [])
    aliases = []
    for i, p in enumerate(project_cfg):
        owner, name = p["repo"].split("/")
        aliases.append(f"""p{i}: repository(owner: "{owner}", name: "{name}") {{
          nameWithOwner description homepageUrl stargazerCount pushedAt
          primaryLanguage {{ name color }} {LANG_FRAGMENT}
          defaultBranchRef {{ target {{ ... on Commit {{ history(author: {{id: $id}}) {{ totalCount }} }} }} }}
        }}""")
    pd = gql(token, f'query($id: ID!) {{ {" ".join(aliases)} }}', {"id": u["id"]}) if aliases else {}
    projects = {}
    for i, p in enumerate(project_cfg):
        r = pd.get(f"p{i}")
        if not r:
            continue
        hist = (((r.get("defaultBranchRef") or {}).get("target") or {}).get("history") or {})
        projects[p["repo"].lower()] = {
            "description": r["description"], "homepage": r["homepageUrl"], "stars": r["stargazerCount"],
            "pushedAt": r["pushedAt"], "language": (r["primaryLanguage"] or {}).get("name"),
            "color": (r["primaryLanguage"] or {}).get("color"), "myCommits": hist.get("totalCount", 0),
            "languages": [{"name": e["node"]["name"], "color": e["node"]["color"], "size": e["size"]}
                          for e in r["languages"]["edges"]],
        }

    days = [d for w in u["contributionsCollection"]["contributionCalendar"]["weeks"] for d in w["contributionDays"]]
    return {
        "login": u["login"], "name": u["name"], "createdAt": u["createdAt"], "avatarUrl": u["avatarUrl"],
        "location": u["location"], "company": u["company"], "twitter": u["twitterUsername"],
        "followers": u["followers"]["totalCount"], "contributedTo": u["repositoriesContributedTo"]["totalCount"],
        "commits": commits,
        "repos": [{"name": r["nameWithOwner"], "fork": r["isFork"], "stars": r["stargazerCount"],
                   "languages": [{"name": e["node"]["name"], "color": e["node"]["color"], "size": e["size"]}
                                 for e in r["languages"]["edges"]]} for r in repos],
        "calendar": {
            "total": u["contributionsCollection"]["contributionCalendar"]["totalContributions"],
            "days": [{"date": d["date"], "count": d["contributionCount"], "level": d["contributionLevel"]} for d in days],
        },
        "projects": projects,
    }


# ───────────────────────────── cálculos ─────────────────────────────

def uptime(created: str, now: dt.date) -> str:
    c = dt.date.fromisoformat(created[:10])
    y, m, d = now.year - c.year, now.month - c.month, now.day - c.day
    if d < 0:
        m -= 1
        prev_month = (now.replace(day=1) - dt.timedelta(days=1))
        d += prev_month.day
    if m < 0:
        y -= 1
        m += 12
    plural = lambda n, s, p: f"{n} {s if n == 1 else p}"
    return f"{plural(y, 'año', 'años')}, {plural(m, 'mes', 'meses')}, {plural(d, 'día', 'días')}"


def top_languages(data: dict, cfg: dict) -> list[dict]:
    lc = cfg.get("languages", {})
    exclude = {x.lower() for x in lc.get("exclude", [])}
    ignore = {x.lower() for x in lc.get("ignore_repos", [])}
    seen, sizes, counts, colors = set(), {}, {}, {}
    sources = [(r["name"], r["languages"]) for r in data["repos"] if not r["fork"]]
    sources += [(k, v["languages"]) for k, v in data["projects"].items()]
    for name, langs in sources:
        if name.lower() in ignore or name.lower() in seen:
            continue
        seen.add(name.lower())
        for l in langs:
            if l["name"].lower() in exclude:
                continue
            sizes[l["name"]] = sizes.get(l["name"], 0) + l["size"]
            counts[l["name"]] = counts.get(l["name"], 0) + 1
            colors[l["name"]] = l["color"] or "#8b949e"
    # Mismo criterio que github-readme-stats con size_weight=0.5, count_weight=0.5
    score = {k: math.sqrt(sizes[k]) * math.sqrt(counts[k]) for k in sizes}
    total = sum(score.values()) or 1
    ranked = sorted(score, key=score.get, reverse=True)[: lc.get("top", 8)]
    return [{"name": k, "pct": 100 * score[k] / total, "color": colors[k]} for k in ranked]


def streaks(days: list[dict], today: dt.date) -> tuple[int, int]:
    longest = run = 0
    for d in days:
        run = run + 1 if d["count"] > 0 else 0
        longest = max(longest, run)
    current = 0
    for d in reversed(days):
        day = dt.date.fromisoformat(d["date"])
        if day > today:
            continue
        if d["count"] > 0:
            current += 1
        elif day == today:  # hoy aún no cuenta como racha rota
            continue
        else:
            break
    return current, longest


# ───────────────────────────── avatar ASCII ─────────────────────────────

def load_avatar(data: dict, path: str | None) -> Image.Image:
    if path:
        return Image.open(path)
    r = requests.get(data["avatarUrl"], timeout=60)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content))


def ascii_grid(img: Image.Image, rows: int, ramp: str, aspect: float):
    """Devuelve una grilla [(char, (r,g,b))] recortada al sujeto."""
    img = img.convert("RGBA")
    base = Image.new("RGBA", img.size, (255, 255, 255, 255))
    base.alpha_composite(img)
    rgb = base.convert("RGB")
    w, h = rgb.size
    px = rgb.load()
    border = [px[x, y] for x in range(w) for y in (0, h - 1)] + [px[x, y] for y in range(h) for x in (0, w - 1)]
    bg = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))
    flat_bg = max(abs(c[i] - bg[i]) for c in border for i in range(3)) < 60

    dark_bg = sum(bg) < 382
    is_subject = (lambda c: math.dist(c, bg) > 45) if flat_bg else (lambda c: True)

    if flat_bg:  # fondo liso (avatar ilustrado / pixel art): recortar al sujeto
        xs = [x for x in range(w) for y in range(h) if math.dist(px[x, y], bg) > 40]
        ys = [y for y in range(h) for x in range(w) if math.dist(px[x, y], bg) > 40]
        if xs:
            pad = max(1, w // 64)
            rgb = rgb.crop((max(0, min(xs) - pad), max(0, min(ys) - pad),
                            min(w, max(xs) + pad + 1), min(h, max(ys) + pad + 1)))
    cw, ch = rgb.size
    cols = max(8, round(rows * (cw / ch) / aspect))
    shape = rgb.resize((cols, rows), Image.BOX).load()      # color medio → máscara y color
    detail = rgb.resize((cols, rows), Image.LANCZOS).load()  # más nítido → densidad del carácter
    top = len(ramp) - 1
    grid = []
    for y in range(rows):
        row = []
        for x in range(cols):
            c, d = shape[x, y], detail[x, y]
            if not is_subject(c):
                row.append((" ", c))
                continue
            lum = (0.299 * d[0] + 0.587 * d[1] + 0.114 * d[2]) / 255
            ink = lum if dark_bg else 1 - lum  # más oscuro (o más claro en fondo oscuro) = más denso
            idx = max(1, min(top, int(ink * top * 1.25 + 1)))
            row.append((ramp[idx], c))
        grid.append(row)
    return grid


def tune(c, theme: str) -> str:
    """Ajusta el color del píxel para que se lea sobre el fondo del tema."""
    h, l, s = colorsys.rgb_to_hls(*(v / 255 for v in c))
    if theme == "dark":
        if s < 0.18:  # negros/grises → pizarra visible sobre el fondo oscuro
            h, l, s = 0.6, 0.36 + 0.42 * l, 0.14
        else:
            l = max(l, 0.45)
    else:
        l = min(l, 0.45)
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    q = lambda v: min(255, int(round(v * 255 / 20)) * 20)  # cuantizar para agrupar tspans
    return f"#{q(r):02x}{q(g):02x}{q(b):02x}"


def avatar_rows_svg(grid, theme, t, x0, y0, fs, lh, color, ramp=" .:-=+*#%@", chars=None, shift=None) -> str:
    """color: "matrix" (verdes por densidad), True (color del píxel) o un color fijo."""
    out = []
    for y, row in enumerate(grid):
        dx = (shift or {}).get(y, 0)
        spans, cur, buf = [], None, ""
        for x, (ch, c) in enumerate(row):
            if chars:
                ch = chars[y][x]
            if color == "matrix":
                i = ramp.find(ch)
                fill = t["mx"][min(4, max(0, (i - 1) * 5 // max(1, len(ramp) - 1)))]
            elif color is True:
                fill = tune(c, theme)
            else:
                fill = color
            if ch == " ":
                fill = cur
            if fill != cur and buf:
                spans.append(f'<tspan fill="{cur}">{escape(buf)}</tspan>')
                buf = ""
            cur = fill if fill else cur
            buf += ch
        if buf.strip():
            spans.append(f'<tspan fill="{cur}">{escape(buf.rstrip())}</tspan>')
        if spans:
            out.append(f'<text x="{x0 + dx * fs * 0.6:.1f}" y="{y0 + y * lh:.1f}">{"".join(spans)}</text>')
    return "\n".join(out)


# ───────────────────────────── piezas SVG ─────────────────────────────

def window(w, h, title, t, body, extra_css="") -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" font-family="{FONT}" role="img" aria-label="{escape(title)}">
<style>
text{{white-space:pre;font-variant-ligatures:none}}
.k{{fill:{t['key']}}}.d{{fill:{t['dots']}}}.v{{fill:{t['text']}}}.h{{fill:{t['head']}}}.m{{fill:{t['muted']}}}.a{{fill:{t['accent']}}}.ok{{fill:{t['ok']}}}
.in{{opacity:0;animation:in .4s ease-out forwards}}
@keyframes in{{from{{opacity:0;transform:translateX(-6px)}}to{{opacity:1;transform:none}}}}
.cur{{animation:blink 1.1s steps(1) infinite}}
@keyframes blink{{50%{{opacity:0}}}}
{extra_css}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important}}.in,.fin{{opacity:1!important}}.fr{{opacity:0!important}}}}
</style>
<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="10" fill="{t['bg']}" stroke="{t['border']}"/>
<path d="M0.5 10.5a10 10 0 0 1 10-10h{w - 21}a10 10 0 0 1 10 10v20h-{w - 1}z" fill="{t['bar']}"/>
<line x1="0.5" y1="30.5" x2="{w - 0.5}" y2="30.5" stroke="{t['border']}"/>
<circle cx="18" cy="16" r="5.5" fill="#ff5f57"/><circle cx="36" cy="16" r="5.5" fill="#febc2e"/><circle cx="54" cy="16" r="5.5" fill="#28c840"/>
<text x="{w / 2}" y="20" text-anchor="middle" font-size="12" class="m">{escape(title)}</text>
{body}
</svg>
"""


def leader(key: str, value: str, width: int, bullet=". ") -> str:
    """'. Key: ........ value' con tspans de color."""
    value = str(value)
    left = f"{bullet}{key}:"
    n = max(2, width - len(left) - len(value) - 2)
    return (f'<tspan class="k">{escape(left)}</tspan><tspan class="d"> {"." * n} </tspan>'
            f'<tspan class="v">{escape(value)}</tspan>')


def section(title: str, width: int) -> str:
    line = "─" * max(2, width - len(title) - 3)
    return f'<tspan class="d">─ </tspan><tspan class="h">{escape(title)}</tspan><tspan class="d"> {line}</tspan>'


def card_svg(data, cfg, langs, theme, grid, today, calendar_stats) -> str:
    t = THEMES[theme]
    c = cfg["card"]
    av = cfg.get("avatar", {})
    fs_a, lh_a = 10, 11.2
    cols = len(grid[0]) if grid else 0
    ax, ay = 26, 58
    av_w = cols * fs_a * CW
    fs, lh = 14, 21
    W = 62  # columnas del panel derecho
    px = ax + av_w + 34
    width = int(px + W * fs * CW + 26)

    cur, longest = calendar_stats
    rows = [
        f'<tspan class="h">{escape(c.get("host") or data["login"] + "@github")}</tspan>'
        f'<tspan class="d"> {"─" * (W - len(c.get("host") or data["login"]) - 9)}</tspan>',
        leader("Uptime", uptime(data["createdAt"], today), W),
        leader("Role", c.get("role", ""), W),
        leader("Location", c.get("location") or data.get("location") or "—", W),
        leader("Company", c.get("company") or (data.get("company") or "—").lstrip("@"), W),
        leader("Focus", c.get("focus", ""), W),
        "",
        section("Languages", W),
        leader("Programming", ", ".join(
            [l["name"] for l in langs if l["name"] not in c.get("markup", ["HTML", "CSS", "SCSS"])][:4]), W),
        leader("Frameworks", c.get("frameworks", ""), W),
        "",
        section("Contact", W),
        *[leader(k, v, W) for k, v in (c.get("contact") or {}).items()],
        "",
        section("GitHub Stats", W),
    ]
    half = (W - 3) // 2
    two = lambda k1, v1, k2, v2: leader(k1, v1, half) + '<tspan class="d"> │ </tspan>' + leader(k2, v2, half)
    total_stars = sum(r["stars"] for r in data["repos"] if not r["fork"])
    own = sum(1 for r in data["repos"] if not r["fork"])
    rows += [
        two("Repos", f"{own} (+{data['contributedTo']} org)", "Stars", total_stars),
        two("Commits", f"{data['commits']:,}", "Followers", data["followers"]),
        two("Contribs 12m", f"{data['calendar']['total']:,}", "Streak", f"{cur}d (max {longest}d)"),
    ]

    body = []
    for i, r in enumerate(rows):
        if r:
            body.append(f'<text class="in" style="animation-delay:{0.55 + i * 0.07:.2f}s" x="{px:.1f}" '
                        f'y="{62 + i * lh}" font-size="{fs}">{r}</text>')
    y_prompt = 62 + len(rows) * lh + 18
    body.append(
        f'<text class="in" style="animation-delay:{0.6 + len(rows) * 0.07:.2f}s" x="{px:.1f}" y="{y_prompt}" font-size="{fs}">'
        f'<tspan class="ok">{escape(data["login"])}@github</tspan><tspan class="m">:</tspan><tspan class="a">~</tspan>'
        f'<tspan class="m">$ </tspan><tspan class="cur v">▋</tspan></text>')
    body.append(f'<text x="{width - 18}" y="{y_prompt}" font-size="10" text-anchor="end" class="m">'
                f'actualizado {today.isoformat()}</text>')

    # Avatar animado: 2 cuadros de "decodificación", cuadro final y un glitch cada 7 s
    color = av.get("color", True)
    ramp = av.get("ramp", " .:-=+*#%@")
    rnd = random.Random(42)
    noisy = lambda: [[(rnd.choice(ramp[2:]) if ch != " " else " ") for ch, _ in row] for row in grid]
    glitch_shift = {y: rnd.choice([-3, -2, 2, 3]) for y in rnd.sample(range(len(grid)), k=max(3, len(grid) // 6))}
    matrix = color == "matrix"
    a_kw = dict(theme=theme, t=t, x0=ax, y0=ay, fs=fs_a, lh=lh_a, color=color, ramp=ramp)
    glow = matrix and theme == "dark"  # brillo de fósforo, solo en oscuro
    avatar = ("""<defs><filter id="glow" x="-10%" y="-10%" width="120%" height="120%">
<feGaussianBlur stdDeviation="1.3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
</filter></defs>""" if glow else "") + f"""<g font-size="{fs_a}" style="letter-spacing:0"{' filter="url(#glow)"' if glow else ''}>
<g class="fr f1">{avatar_rows_svg(grid, chars=noisy(), **a_kw)}</g>
<g class="fr f2">{avatar_rows_svg(grid, chars=noisy(), **a_kw)}</g>
<g class="fr f3">{avatar_rows_svg(grid, chars=noisy(), **a_kw)}</g>
<g class="fin">{avatar_rows_svg(grid, **a_kw)}</g>
<g class="fr gl">{avatar_rows_svg(grid, shift=glitch_shift, **{**a_kw, 'color': t["mxg"] if matrix else t["head"]})}</g>
</g>
<rect class="scan" x="{ax - 6}" y="{ay - 14}" width="{av_w + 12}" height="18" fill="{t['ok'] if matrix else t['head']}" opacity="0"/>"""
    av_h = len(grid) * lh_a
    css = f"""
.fr{{opacity:0}}.fin{{opacity:0;animation:on .01s .45s forwards,flick 7s steps(1) 2.5s infinite}}
.f1{{animation:blip .15s steps(1) 0s}}.f2{{animation:blip .15s steps(1) .15s}}.f3{{animation:blip .15s steps(1) .3s}}
.gl{{animation:gl 7s steps(1) 2.5s infinite}}
@keyframes on{{to{{opacity:1}}}}@keyframes blip{{0%,100%{{opacity:1}}}}
@keyframes flick{{0%,93.9%{{opacity:1}}94%,96.9%{{opacity:0}}97%,100%{{opacity:1}}}}
@keyframes gl{{0%,93.9%{{opacity:0}}94%,96.9%{{opacity:1}}97%,100%{{opacity:0}}}}
.scan{{animation:scan 5s linear 1s infinite}}
@keyframes scan{{0%{{opacity:0;transform:translateY(0)}}5%{{opacity:.10}}85%{{opacity:.10}}100%{{opacity:0;transform:translateY({av_h:.0f}px)}}}}
"""
    height = int(max(y_prompt + 26, ay + av_h + 20))
    title = f"{data['login']}@github: ~ — neofetch"
    return window(width, height, title, t, avatar + "\n" + "\n".join(body), css)


def languages_svg(langs, theme, width=430, rows_target=None) -> str:
    t = THEMES[theme]
    fs, lh = 13, 24
    name_w = max(len(l["name"]) for l in langs) if langs else 8
    bar_cells = int((width - 40) / (fs * CW)) - name_w - 9
    rows = [f'<text class="in" x="20" y="56" font-size="{fs}"><tspan class="ok">❯</tspan>'
            f'<tspan class="v"> linguist --top {len(langs)}</tspan></text>']
    for i, l in enumerate(langs):
        y = 86 + i * lh
        filled = max(1, round(bar_cells * l["pct"] / max(langs[0]["pct"], 1)))
        d = 0.3 + i * 0.08
        rows.append(
            f'<text class="in" style="animation-delay:{d:.2f}s" x="20" y="{y}" font-size="{fs}">'
            f'<tspan class="v">{escape(l["name"].ljust(name_w))} </tspan>'
            f'<tspan fill="{l["color"]}" class="bar" style="animation-delay:{d + .1:.2f}s">{"█" * filled}</tspan>'
            f'<tspan class="d">{"░" * (bar_cells - filled)}</tspan>'
            f'<tspan class="m"> {l["pct"]:5.1f}%</tspan></text>')
    h = max(86 + len(langs) * lh, rows_target or 0)
    return window(width, h, "~/stats — top languages", t, "\n".join(rows))


def stack_svg(cfg, theme, width=430, rows_target=None) -> str:
    t = THEMES[theme]
    fs, lh = 13, 24
    max_chars = int((width - 40) / (fs * CW))
    lines = [f'<tspan class="ok">❯</tspan><tspan class="v"> cat ~/.config/stack.yml</tspan>']
    stack = cfg.get("stack", {})
    kw = max((len(k) for k in stack), default=6) + 1
    for k, items in stack.items():
        prefix = f"{k}:".ljust(kw + 1)
        cur, first = "", True
        for it in items:
            piece = it if not cur else f", {it}"
            if len(prefix) + len(cur) + len(piece) + 2 > max_chars:
                lines.append((prefix if first else " " * len(prefix), cur + ","))
                cur, first = it, False
            else:
                cur += piece
        lines.append((prefix if first else " " * len(prefix), cur))
    body = []
    for i, ln in enumerate(lines):
        y = 56 + i * lh + (6 if i else 0)
        if isinstance(ln, tuple):
            body.append(f'<text class="in" style="animation-delay:{0.3 + i * 0.07:.2f}s" x="20" y="{y}" font-size="{fs}">'
                        f'<tspan class="h">{escape(ln[0])}</tspan><tspan class="v">{escape(ln[1])}</tspan></text>')
        else:
            body.append(f'<text class="in" x="20" y="{y}" font-size="{fs}">{ln}</text>')
    h = 56 + len(lines) * lh + 6
    if rows_target:
        h = max(h, rows_target)
    return window(width, h, "~/.config — stack", t, "\n".join(body))


def contrib_svg(data, theme, today, calendar_stats) -> str:
    t = THEMES[theme]
    days = data["calendar"]["days"]
    levels = {"NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2, "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4}
    glyph = ["·", "▪", "▪", "■", "■"]
    cx, cy, gx, gy = 58, 92, 14.2, 15
    first = dt.date.fromisoformat(days[0]["date"]) if days else today
    offset = (first.weekday() + 1) % 7  # GitHub: semanas empiezan en domingo
    cells, months, last_m, last_col = [], [], None, -9
    for i, d in enumerate(days):
        idx = i + offset
        col, row = idx // 7, idx % 7
        date = dt.date.fromisoformat(d["date"])
        if date.month != last_m and row <= 1:
            if months and col - last_col < 3:  # mes anterior casi vacío: gana el nuevo
                months.pop()
            last_col = col
            months.append(f'<text x="{cx + col * gx}" y="{cy - 16}" font-size="11" class="m">{MONTHS_ES[date.month - 1]}</text>')
        if date.month != last_m and row <= 1:
            last_m = date.month
        lv = levels.get(d["level"], 0)
        cells.append(f'<text x="{cx + col * gx:.1f}" y="{cy + row * gy}" fill="{t["lv"][lv]}" '
                     f'class="c" style="animation-delay:{0.4 + col * 0.018:.2f}s">{glyph[lv]}</text>')
    ncols = (len(days) + offset + 6) // 7
    width = int(cx + ncols * gx + 24)
    wd = "".join(f'<text x="{cx - 12}" y="{cy + r * gy}" font-size="11" text-anchor="end" class="m">{n}</text>'
                 for r, n in ((1, "Lun"), (3, "Mié"), (5, "Vie")))
    cur, longest = calendar_stats
    y_stats = cy + 7 * gy + 22
    legend_x = width - 24 - 5 * 14 - 70
    legend = (f'<text x="{legend_x}" y="{y_stats}" font-size="11" class="m">menos</text>' +
              "".join(f'<text x="{legend_x + 40 + i * 14}" y="{y_stats}" font-size="13" fill="{t["lv"][i]}">{glyph[i]}</text>'
                      for i in range(5)) +
              f'<text x="{legend_x + 40 + 5 * 14 + 4}" y="{y_stats}" font-size="11" class="m">más</text>')
    stats = (f'<text class="in" style="animation-delay:1.2s" x="22" y="{y_stats}" font-size="12">'
             f'<tspan class="k">{data["calendar"]["total"]:,}</tspan><tspan class="m"> contribuciones en 12 meses   </tspan>'
             f'<tspan class="k">{cur}d</tspan><tspan class="m"> racha actual   </tspan>'
             f'<tspan class="k">{longest}d</tspan><tspan class="m"> racha más larga</tspan></text>')
    head = (f'<text class="in" x="22" y="54" font-size="13"><tspan class="ok">❯</tspan>'
            f'<tspan class="v"> git log --author={escape(data["login"])} --since=1.year --graph</tspan></text>')
    css = ".c{font-size:13px;opacity:0;animation:pop .35s ease-out forwards}@keyframes pop{from{opacity:0}to{opacity:1}}"
    body = head + "\n" + "".join(months) + wd + "\n" + "\n".join(cells) + "\n" + stats + legend
    return window(width, y_stats + 22, "~/activity — contributions", t, body, css)


# ───────────────────────────── README ─────────────────────────────

def ago(iso: str | None, today: dt.date) -> str:
    if not iso:
        return "—"
    days = (today - dt.date.fromisoformat(iso[:10])).days
    if days < 1:
        return "hoy"
    if days < 31:
        return f"hace {days} d"
    if days < 365:
        return f"hace {days // 30} m"
    return f"hace {days // 365} a"


def wrap(text: str, width: int) -> list[str]:
    lines, cur = [], ""
    for word in (text or "").split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return lines + ([cur] if cur else [])


def slug(p: dict) -> str:
    base = p.get("slug") or p.get("title") or p["repo"].split("/")[1]
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")


FW, FS = 440, 13  # ancho de la tarjeta y tamaño de fuente


def featured_layout(p: dict, login: str) -> dict:
    n = int((FW - 40) / (FS * CW))
    team = p["repo"].split("/")[0].lower() != login.lower()
    return {
        "desc": wrap(p.get("description", ""), n)[:3],
        "role": wrap(p.get("contribution") or ("Autor: diseño y desarrollo." if not team else ""), n - 2)[:3],
        "stack": wrap(p.get("stack", ""), n - 7)[:2],
        "team": team,
    }


def featured_height(lay: dict) -> int:
    return 128 + 19 * (len(lay["desc"]) + len(lay["role"]) + len(lay["stack"])) + 16 + 52


def featured_svg(p: dict, info: dict, lay: dict, theme: str, today: dt.date, H: int) -> str:
    t = THEMES[theme]
    owner = p["repo"].split("/")[0]
    badge = f"equipo · @{owner}" if lay["team"] else "proyecto propio"
    body = [
        f'<text class="in" x="20" y="56" font-size="12"><tspan class="ok">❯</tspan>'
        f'<tspan class="m"> cat README.md</tspan></text>',
        f'<text class="in" style="animation-delay:.15s" x="20" y="88" font-size="19" font-weight="700" fill="{t["ok"]}">'
        f'{escape(p.get("title") or p["repo"].split("/")[1])}</text>',
        f'<text class="in" style="animation-delay:.2s" x="{FW - 20}" y="86" font-size="11" text-anchor="end" '
        f'fill="{t["accent"] if lay["team"] else t["head"]}">● {escape(badge)}</text>',
    ]
    y, i = 116, 0
    for ln in lay["desc"]:
        body.append(f'<text class="in v" style="animation-delay:{.3 + i * .06:.2f}s" x="20" y="{y}" font-size="{FS}">{escape(ln)}</text>')
        y, i = y + 19, i + 1
    y += 8
    for j, ln in enumerate(lay["role"]):
        lead = '<tspan class="k">› </tspan>' if j == 0 else '<tspan>  </tspan>'
        body.append(f'<text class="in" style="animation-delay:{.3 + i * .06:.2f}s" x="20" y="{y}" font-size="{FS}">'
                    f'{lead}<tspan class="v">{escape(ln)}</tspan></text>')
        y, i = y + 19, i + 1
    y += 8
    for j, ln in enumerate(lay["stack"]):
        lead = "stack: " if j == 0 else "       "
        body.append(f'<text class="in" style="animation-delay:{.3 + i * .06:.2f}s" x="20" y="{y}" font-size="{FS}">'
                    f'<tspan class="m">{lead}</tspan><tspan class="h">{escape(ln)}</tspan></text>')
        y, i = y + 19, i + 1

    # pie: barra de lenguajes + actividad
    langs = info.get("languages") or []
    total = sum(l["size"] for l in langs) or 1
    bar_y, x = H - 46, 20.0
    segs = []
    for l in langs:
        w = (FW - 40) * l["size"] / total
        if w < 0.5:
            continue
        segs.append(f'<rect x="{x:.1f}" y="{bar_y}" width="{w:.1f}" height="6" fill="{l["color"] or t["muted"]}"/>')
        x += w
    if not segs:
        segs.append(f'<rect x="20" y="{bar_y}" width="{FW - 40}" height="6" fill="{t["dots"]}"/>')
    commits = info.get("myCommits", 0)
    meta = f'{commits} commit{"s" if commits != 1 else ""} míos · {ago(info.get("pushedAt"), today)}'
    if info.get("stars"):
        meta = f'★ {info["stars"]} · ' + meta
    room = int((FW - 40) / (11 * CW)) - len(meta) - 3  # que leyenda y actividad no se pisen
    parts = [f'{l["name"]} {100 * l["size"] / total:.0f}%' for l in langs[:3]]
    while parts and len(" · ".join(parts)) > room:
        parts.pop()
    legend = " · ".join(parts)
    body += [
        f'<line x1="20" y1="{bar_y - 14}" x2="{FW - 20}" y2="{bar_y - 14}" stroke="{t["border"]}" stroke-dasharray="2 4"/>',
        f'<clipPath id="bc"><rect x="20" y="{bar_y}" width="{FW - 40}" height="6" rx="3"/></clipPath>',
        f'<g clip-path="url(#bc)"><g class="bar">{"".join(segs)}</g></g>',
        f'<text x="20" y="{H - 18}" font-size="11" class="m">{escape(legend)}</text>',
        f'<text x="{FW - 20}" y="{H - 18}" font-size="11" text-anchor="end" class="m">{escape(meta)}</text>',
    ]
    css = (".bar{transform-box:fill-box;transform-origin:left;animation:grow .9s cubic-bezier(.2,.8,.2,1) .5s both}"
           "@keyframes grow{from{transform:scaleX(0)}}")
    return window(FW, H, f"~/proyectos/{slug(p)}", t, "\n".join(body), css)


def featured_md(cfg, data) -> str:
    items = cfg.get("featured", [])
    cards = []
    for p in items:
        sl = slug(p)
        title = escape(p.get("title") or p["repo"])
        cards.append(
            f'<a href="https://github.com/{p["repo"]}"><picture>'
            f'<source media="(prefers-color-scheme: dark)" srcset="assets/featured-{sl}-dark.svg">'
            f'<source media="(prefers-color-scheme: light)" srcset="assets/featured-{sl}-light.svg">'
            f'<img alt="{title}" src="assets/featured-{sl}-dark.svg" width="49%"></picture></a>')
    rows = ["<p>\n" + "\n".join(cards[i:i + 2]) + "\n</p>" for i in range(0, len(cards), 2)]
    demos = []
    for p in items:
        info = data["projects"].get(p["repo"].lower(), {})
        demo = p.get("demo") or info.get("homepage")
        if demo:
            url = demo if demo.startswith("http") else "https://" + demo
            demos.append(f'<a href="{url}">{escape(p.get("title") or p["repo"])}</a>')
    if demos:
        rows.append(f'<p align="center"><sub><code>❯ open --demo</code>&nbsp; {" · ".join(demos)}</sub></p>')
    return "\n".join(rows)


def inject(text: str, tag: str, content: str) -> str:
    pat = re.compile(rf"(<!--{tag}:start-->)(.*?)(<!--{tag}:end-->)", re.S)
    return pat.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(3)}", text)


# ───────────────────────────── main ─────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "profile.yml"))
    ap.add_argument("--fixture", help="JSON con datos ya descargados (modo sin red)")
    ap.add_argument("--avatar", help="ruta a una imagen local en vez de la de GitHub")
    ap.add_argument("--dump", help="guardar los datos descargados en este JSON")
    ap.add_argument("--today", help="fecha fija AAAA-MM-DD (para pruebas)")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    if args.fixture:
        data = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    else:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise SystemExit("Falta GITHUB_TOKEN")
        data = fetch(cfg, token)
    if args.dump:
        Path(args.dump).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    lima = dt.timezone(dt.timedelta(hours=-5))
    today = dt.date.fromisoformat(args.today) if args.today else dt.datetime.now(lima).date()
    langs = top_languages(data, cfg)
    cal = streaks(data["calendar"]["days"], today)

    av = cfg.get("avatar", {})
    grid = ascii_grid(load_avatar(data, args.avatar), av.get("rows", 34), av.get("ramp", " .:-=+*#%@"),
                      aspect=CW * 10 / 11.2)

    ASSETS.mkdir(exist_ok=True)
    hgt = lambda svg: int(re.search(r'height="(\d+)"', svg).group(1))
    pair_h = max(hgt(languages_svg(langs, "dark")), hgt(stack_svg(cfg, "dark")))  # paneles lado a lado parejos
    for theme in THEMES:
        files = {
            "card": card_svg(data, cfg, langs, theme, grid, today, cal),
            "languages": languages_svg(langs, theme, rows_target=pair_h),
            "stack": stack_svg(cfg, theme, rows_target=pair_h),
            "contributions": contrib_svg(data, theme, today, cal),
        }
        for name, svg in files.items():
            (ASSETS / f"{name}-{theme}.svg").write_text(svg, encoding="utf-8")

    # Tarjetas de proyectos destacados (todas del mismo alto)
    feats = cfg.get("featured", [])
    lays = [featured_layout(p, data["login"]) for p in feats]
    fh = max((featured_height(l) for l in lays), default=0)
    keep = set()
    for p, lay in zip(feats, lays):
        info = data["projects"].get(p["repo"].lower(), {})
        for theme in THEMES:
            name = f"featured-{slug(p)}-{theme}.svg"
            keep.add(name)
            (ASSETS / name).write_text(featured_svg(p, info, lay, theme, today, fh), encoding="utf-8")
    for old in ASSETS.glob("featured-*.svg"):  # limpiar tarjetas de proyectos que ya no están
        if old.name not in keep:
            old.unlink()

    from xml.dom import minidom
    for f in ASSETS.glob("*.svg"):  # fallar aquí y no con una imagen rota en el perfil
        minidom.parse(str(f))

    if README.exists():
        text = inject(README.read_text(encoding="utf-8"), "featured", featured_md(cfg, data))
        README.write_text(text, encoding="utf-8")
    print(f"✓ {len(langs)} lenguajes · {data['commits']} commits · avatar {len(grid[0])}x{len(grid)} · {today}")


if __name__ == "__main__":
    main()
