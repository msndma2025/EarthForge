"""Builds the documentation as ONE self-contained HTML file with search.

The Markdown pages in docs/ (in the order of mkdocs.yml) are the source, so
re-run this after changing the docs:

    python tools/build_docs_html.py                     # -> documentation.html
    python tools/build_docs_html.py --fragment out.html # body-only version, for hosting

The output works offline (double-click it): images are embedded, search runs
in the page, and the API reference is generated from the docstrings.
"""

import argparse
import base64
import datetime
import html
import importlib
import inspect
import io
import json
import re
import sys
from pathlib import Path

import markdown
import yaml
from PIL import Image as PILImage

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
sys.path.insert(0, str(ROOT))

EXTENSIONS = [
    "tables", "toc", "admonition", "attr_list", "md_in_html",
    "pymdownx.superfences", "pymdownx.tabbed", "pymdownx.details",
    "pymdownx.highlight", "pymdownx.inlinehilite", "pymdownx.snippets",
]
EXTENSION_CONFIGS = {
    "toc": {"permalink": False},
    "pymdownx.highlight": {"use_pygments": True, "css_class": "highlight", "guess_lang": False},
    "pymdownx.tabbed": {"alternate_style": True},
    "pymdownx.snippets": {"base_path": [str(ROOT)], "check_paths": True},
}


# ---------------------------------------------------------------------------
#  Pages
# ---------------------------------------------------------------------------

def nav_pages(nav, group=None):
    """Flatten the mkdocs.yml nav into (title, file, group) tuples."""
    for item in nav:
        (title, value), = item.items()
        if isinstance(value, list):
            yield from nav_pages(value, title)
        else:
            yield title, value, group


def page_id(filename):
    stem = Path(filename).stem
    return "home" if stem == "index" else slug(stem)


def slug(text):
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")


def strip_tags(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


# ---------------------------------------------------------------------------
#  API reference (replaces mkdocstrings' "::: module.Name" lines)
# ---------------------------------------------------------------------------

def _doc(obj):
    text = inspect.getdoc(obj) or ""
    return f'<pre class="docstring">{html.escape(text)}</pre>' if text else ""


def _signature(func, name, drop_self):
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return name + "(...)"
    params = list(sig.parameters.values())
    if drop_self and params and params[0].name in ("self", "cls"):
        sig = sig.replace(parameters=params[1:])
    return f"{name}{sig}"


def api_html(dotted):
    module_name, _, name = dotted.rpartition(".")
    obj = getattr(importlib.import_module(module_name), name)
    anchor = slug(name)
    if not inspect.isclass(obj):
        sig = _signature(obj, f"{module_name.split('.')[-1]}.{name}", drop_self=False)
        return (f'<h3 id="{anchor}">{html.escape(name)}()</h3>'
                f'<div class="api-sig"><code>{html.escape(sig)}</code></div>{_doc(obj)}')

    bases = [b.__name__ for b in obj.__bases__ if b is not object]
    head = f"class {name}" + (f"({', '.join(bases)})" if bases else "")
    parts = [f'<h3 id="{anchor}">{html.escape(name)}</h3>',
             f'<div class="api-sig"><code>{html.escape(head)}</code></div>', _doc(obj)]
    if issubclass(obj, BaseException):
        return "".join(parts)
    seen = {}
    for member, attr in vars(obj).items():
        if member.startswith("_"):
            continue
        member_anchor = f"{anchor}-{slug(member)}"
        if isinstance(attr, property):
            parts.append(f'<h4 id="{member_anchor}">{html.escape(member)} <span class="kind">property</span></h4>')
            parts.append(_doc(attr))
            continue
        func = attr.__func__ if isinstance(attr, (staticmethod, classmethod)) else attr
        if not callable(func):
            continue
        original = getattr(func, "__wrapped__", func)
        sig = _signature(func, member, drop_self=not isinstance(attr, staticmethod))
        parts.append(f'<h4 id="{member_anchor}">{html.escape(member)}()</h4>')
        parts.append(f'<div class="api-sig"><code>{html.escape(sig)}</code></div>')
        if original in seen:
            parts.append(f'<p>Same as <code>{html.escape(seen[original])}()</code>.</p>')
        else:
            seen[original] = member
            parts.append(_doc(func))
    return "".join(parts)


# ---------------------------------------------------------------------------
#  Converting one page
# ---------------------------------------------------------------------------

def embed_image(src):
    """images/x.png -> a small JPEG data URI."""
    path = DOCS / src
    with PILImage.open(path) as im:
        im = im.convert("RGBA")
        flat = PILImage.new("RGB", im.size, (255, 255, 255))
        flat.paste(im, mask=im.split()[3])
        flat.thumbnail((1100, 1100))
        buf = io.BytesIO()
        flat.save(buf, "JPEG", quality=82, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def convert_page(pid, filename, file_to_id):
    text = (DOCS / filename).read_text(encoding="utf-8")

    api_blocks = []

    def api_placeholder(match):
        api_blocks.append(api_html(match.group(1).strip()))
        return f"\nAPIBLOCK{len(api_blocks) - 1}\n"
    text = re.sub(r"^:::\s*(\S+)\s*$", api_placeholder, text, flags=re.M)

    md = markdown.Markdown(extensions=EXTENSIONS, extension_configs=EXTENSION_CONFIGS)
    body = md.convert(text)
    for i, block in enumerate(api_blocks):
        body = body.replace(f"<p>APIBLOCK{i}</p>", block)

    # heading ids -> "<page>--<anchor>", so every heading has a unique, linkable id
    body = re.sub(r'<(h[1-4]) id="([^"]+)"',
                  lambda m: f'<{m.group(1)} id="{pid}--{slug(m.group(2))}"', body)
    body = body.replace('id="__tabbed_', f'id="{pid}_tabbed_').replace('for="__tabbed_', f'for="{pid}_tabbed_')
    body = body.replace('name="__tabbed_', f'name="{pid}_tabbed_')

    def fix_href(match):
        url = match.group(1)
        if url.startswith(("http://", "https://")):
            return f'href="{url}" target="_blank" rel="noopener"'
        if url.startswith("#"):
            return f'href="#{pid}--{slug(url[1:])}"'
        m = re.match(r"^([\w./-]+\.md)(?:#(.*))?$", url)
        if m and Path(m.group(1)).name in file_to_id:
            target = file_to_id[Path(m.group(1)).name]
            return f'href="#{target}--{slug(m.group(2))}"' if m.group(2) else f'href="#{target}"'
        return match.group(0)
    body = re.sub(r'href="([^"]*)"', fix_href, body)

    body = re.sub(r'src="(images/[^"]+)"', lambda m: f'src="{embed_image(m.group(1))}" loading="lazy"', body)
    body = body.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")
    return body


def search_entries(pid, page_title, body):
    """Split a page at its headings into searchable sections."""
    entries = []
    for part in re.split(r'(?=<h[1-4][^>]*\sid=")', body):
        m = re.match(r'<h([1-4])[^>]*\sid="([^"]+)"[^>]*>(.*?)</h\1>', part, re.S)
        if m:
            anchor, title = m.group(2), strip_tags(m.group(3))
            rest = part[m.end():]
        else:
            anchor, title, rest = pid, page_title, part
        text = strip_tags(rest)
        if title or text:
            entries.append({"p": pid, "a": anchor, "t": title, "pt": page_title, "x": text[:5000]})
    return entries


# ---------------------------------------------------------------------------
#  Build
# ---------------------------------------------------------------------------

def build():
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    pages = list(nav_pages(config["nav"]))
    file_to_id = {Path(f).name: page_id(f) for _, f, _ in pages}

    sections, nav, index = [], [], []
    for i, (title, filename, group) in enumerate(pages):
        pid = page_id(filename)
        body = convert_page(pid, filename, file_to_id)
        headings = [{"id": a, "title": strip_tags(t)}
                    for a, t in re.findall(r'<h2[^>]*\sid="([^"]+)"[^>]*>(.*?)</h2>', body, re.S)]
        nav.append({"id": pid, "title": title, "group": group, "headings": headings})
        index.extend(search_entries(pid, title, body))

        pager = []
        if i > 0:
            ptitle, pfile, _ = pages[i - 1]
            pager.append(f'<a class="prev" href="#{page_id(pfile)}"><span>Previous</span>{html.escape(ptitle)}</a>')
        if i < len(pages) - 1:
            ntitle, nfile, _ = pages[i + 1]
            pager.append(f'<a class="next" href="#{page_id(nfile)}"><span>Next</span>{html.escape(ntitle)}</a>')
        eyebrow = f'<p class="eyebrow">{html.escape(group)}</p>' if group else ""
        sections.append(f'<section class="doc" data-page="{pid}" hidden>{eyebrow}{body}'
                        f'<nav class="pager">{"".join(pager)}</nav></section>')

    import pyearthforge
    data = {"pages": nav, "index": index}
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    meta = (f"v{pyearthforge.__version__} · built {datetime.date.today().isoformat()} · "
            f"{len(index)} searchable sections")
    return (TEMPLATE.replace("__CONTENT__", "\n".join(sections))
                    .replace("__DATA__", data_json)
                    .replace("__META__", html.escape(meta)))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(ROOT / "documentation.html"),
                        help="standalone HTML file (default: documentation.html)")
    parser.add_argument("--fragment", help="also write a body-only version (no <html>/<head>) to this path")
    args = parser.parse_args()

    page = build()
    standalone = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
                  '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                  '</head>\n<body>\n' + page + '\n</body>\n</html>\n')
    Path(args.out).write_text(standalone, encoding="utf-8")
    print(f"Wrote {Path(args.out).resolve()}  ({len(standalone) / 1e6:.2f} MB)")
    if args.fragment:
        Path(args.fragment).write_text(page, encoding="utf-8")
        print(f"Wrote {Path(args.fragment).resolve()}  (fragment)")


# ---------------------------------------------------------------------------
#  Page template: styles, layout and the search/navigation script
# ---------------------------------------------------------------------------

TEMPLATE = r"""<title>EarthForge Documentation</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&family=Space+Grotesk:wght@500;600;700&display=swap">
<style>
/* Layout: sticky top bar (name + search), sticky sidebar navigation beside one
   reading column; one doc page is shown at a time and search results are their own view. */
:root {
  --bg: #f6f8f7;
  --panel: #edf2ef;
  --fg: #16201c;
  --muted: #586862;
  --line: #d6e0db;
  --accent: #0a7558;
  --accent-soft: #dcefe7;
  --mark: #ffe28f;
  --code-bg: #eaf0ed;
  --note: #22609f;  --note-bg: #e6eff9;
  --warn: #8f5a00;  --warn-bg: #fbf0da;
  --tip: #0a7558;   --tip-bg: #e0f2ea;
  --syn-kw: #8b3ab0; --syn-str: #a04a12; --syn-num: #0b6a99;
  --syn-com: #6a7a73; --syn-fn: #1d6b47; --syn-op: #4f5d57;
  --font-body: "Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --font-head: "Space Grotesk", "Inter", system-ui, sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, "Cascadia Code", Consolas, monospace;
  --topbar-h: 56px;
  --sidebar-w: 260px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0e1412; --panel: #131b18; --fg: #e1eae6; --muted: #93a49c; --line: #24302b;
    --accent: #47c79c; --accent-soft: #16302a; --mark: #6e5711; --code-bg: #161f1b;
    --note: #86b8f2; --note-bg: #15233a; --warn: #e8b85e; --warn-bg: #2d2412; --tip: #47c79c; --tip-bg: #13281f;
    --syn-kw: #d4a0f2; --syn-str: #f0a874; --syn-num: #74c6f2; --syn-com: #7e8f88; --syn-fn: #85dcb4; --syn-op: #a8b5af;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0e1412; --panel: #131b18; --fg: #e1eae6; --muted: #93a49c; --line: #24302b;
  --accent: #47c79c; --accent-soft: #16302a; --mark: #6e5711; --code-bg: #161f1b;
  --note: #86b8f2; --note-bg: #15233a; --warn: #e8b85e; --warn-bg: #2d2412; --tip: #47c79c; --tip-bg: #13281f;
  --syn-kw: #d4a0f2; --syn-str: #f0a874; --syn-num: #74c6f2; --syn-com: #7e8f88; --syn-fn: #85dcb4; --syn-op: #a8b5af;
  color-scheme: dark;
}

* { box-sizing: border-box; }
html { scroll-padding-top: calc(var(--topbar-h) + env(safe-area-inset-top, 0px) + 16px); }
body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.65 var(--font-body); }
body::before { content: ""; position: fixed; inset: 0 0 auto; height: env(safe-area-inset-top, 0px); z-index: 30; background: var(--panel); }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* ---- top bar ---- */
.topbar { position: sticky; top: env(safe-area-inset-top, 0px); z-index: 20; background: var(--panel); border-bottom: 1px solid var(--line); }
.topbar-inner {
  height: var(--topbar-h); display: flex; align-items: center; gap: 14px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
}
.brand { display: flex; flex-direction: column; gap: 4px; color: var(--fg); text-decoration: none; flex: none; }
.brand-name { font: 700 19px/1 var(--font-head); letter-spacing: -0.01em; }
.brand-name small { font: 500 12px/1 var(--font-body); color: var(--muted); margin-left: 6px; letter-spacing: 0; }
/* the built-in "ndvi" palette, as a colorbar */
.brand-strip { height: 4px; border-radius: 2px; background: linear-gradient(90deg, #ce7e45, #f1b555, #fcd163, #99b718, #66a000, #3e8601, #056201, #023b01); }
.search { position: relative; flex: 1; max-width: 460px; margin-left: auto; }
.search input {
  width: 100%; height: 38px; padding: 0 44px 0 36px; border: 1px solid var(--line); border-radius: 8px;
  background: var(--bg); color: var(--fg); font: 14px var(--font-body);
}
.search input::placeholder { color: var(--muted); }
.search input:focus { outline: 2px solid var(--accent); outline-offset: 0; border-color: transparent; }
.search svg { position: absolute; left: 11px; top: 50%; translate: 0 -50%; width: 16px; height: 16px; color: var(--muted); pointer-events: none; }
.search kbd {
  position: absolute; right: 9px; top: 50%; translate: 0 -50%; font: 500 11px/1 var(--font-mono); color: var(--muted);
  border: 1px solid var(--line); border-radius: 4px; padding: 3px 6px; background: var(--panel);
}
.menu-btn {
  display: none; flex: none; width: 38px; height: 38px; align-items: center; justify-content: center;
  border: 1px solid var(--line); border-radius: 8px; background: var(--bg); color: var(--fg); cursor: pointer;
}
.menu-btn svg { width: 18px; height: 18px; }

/* ---- layout ---- */
.layout {
  display: grid; grid-template-columns: var(--sidebar-w) minmax(0, 1fr);
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
}
.sidebar {
  position: sticky; top: calc(var(--topbar-h) + env(safe-area-inset-top, 0px)); align-self: start;
  max-height: calc(100vh - var(--topbar-h) - env(safe-area-inset-top, 0px)); overflow-y: auto;
  padding: 20px 14px 28px 0; border-right: 1px solid var(--line);
  display: flex; flex-direction: column; gap: 18px;
}
.nav { display: flex; flex-direction: column; gap: 1px; }
.nav-group { font: 600 11px/1 var(--font-body); text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); padding: 16px 10px 6px; }
.nav-group:first-child { padding-top: 0; }
.nav a { display: block; padding: 5px 10px; border-radius: 6px; color: var(--fg); text-decoration: none; font-size: 14px; line-height: 1.4; }
.nav a:hover { background: var(--accent-soft); }
.nav a.active { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.nav .sub { display: flex; flex-direction: column; gap: 1px; padding: 2px 0 6px; }
.nav .sub a { font-size: 13px; color: var(--muted); padding: 3px 10px 3px 22px; }
.nav .sub a:hover { color: var(--fg); }
.sidebar-meta { font: 12px/1.5 var(--font-body); color: var(--muted); padding: 0 10px; font-variant-numeric: tabular-nums; }

main { min-width: 0; padding: 32px 0 72px clamp(16px, 4vw, 56px); }

/* ---- doc pages ---- */
.doc { max-width: 780px; }
.doc > * { min-width: 0; }
.eyebrow { font: 600 12px/1 var(--font-body); text-transform: uppercase; letter-spacing: 0.08em; color: var(--accent); margin: 0 0 10px; }
.doc h1 { font: 700 2.15rem/1.15 var(--font-head); letter-spacing: -0.02em; margin: 0 0 0.6em; text-wrap: balance; }
.doc h2 { font: 600 1.45rem/1.25 var(--font-head); letter-spacing: -0.01em; margin: 2.1em 0 0.6em; padding-top: 0.9em; border-top: 1px solid var(--line); text-wrap: balance; }
.doc h3 { font: 600 1.15rem/1.3 var(--font-head); margin: 1.8em 0 0.5em; text-wrap: balance; }
.doc h4 { font: 600 1rem/1.35 var(--font-body); margin: 1.6em 0 0.4em; }
.doc h4 .kind { font: 500 12px/1 var(--font-body); color: var(--muted); margin-left: 6px; }
.doc p, .doc li { max-width: 70ch; }
.doc a { color: var(--accent); text-underline-offset: 2px; }
.doc hr { border: none; border-top: 1px solid var(--line); margin: 2em 0; }
.doc ul, .doc ol { padding-left: 1.4em; }
.doc li + li { margin-top: 0.25em; }
.doc code { font: 0.86em var(--font-mono); background: var(--code-bg); padding: 0.12em 0.36em; border-radius: 4px; overflow-wrap: anywhere; }
.doc img { max-width: 100%; height: auto; border-radius: 6px; border: 1px solid var(--line); vertical-align: top; }
.doc .grid { display: flex; flex-wrap: wrap; gap: 8px; }
.doc .grid img { width: calc(33.333% - 6px) !important; min-width: 0; }

.highlight { position: relative; margin: 1.1em 0; background: var(--code-bg); border: 1px solid var(--line); border-radius: 8px; }
.highlight pre { margin: 0; padding: 14px 16px; overflow-x: auto; font: 13px/1.6 var(--font-mono); }
.highlight code { background: none; padding: 0; font-size: inherit; overflow-wrap: normal; }
.copy-btn {
  position: absolute; top: 6px; right: 6px; font: 500 11px/1 var(--font-body); padding: 6px 8px; border-radius: 5px;
  border: 1px solid var(--line); background: var(--panel); color: var(--muted); cursor: pointer; opacity: 0; transition: opacity 0.15s;
}
.highlight:hover .copy-btn, .copy-btn:focus-visible { opacity: 1; }
@media (hover: none) { .copy-btn { opacity: 0.85; } }
.highlight .k, .highlight .kn, .highlight .kc, .highlight .kd, .highlight .kr, .highlight .ow { color: var(--syn-kw); }
.highlight .s, .highlight .s1, .highlight .s2, .highlight .sa, .highlight .sd, .highlight .se, .highlight .si, .highlight .sb { color: var(--syn-str); }
.highlight .m, .highlight .mi, .highlight .mf, .highlight .mh, .highlight .il { color: var(--syn-num); }
.highlight .c, .highlight .c1, .highlight .cm, .highlight .ch { color: var(--syn-com); font-style: italic; }
.highlight .nf, .highlight .nb, .highlight .fm, .highlight .nc, .highlight .bp { color: var(--syn-fn); }
.highlight .o, .highlight .p { color: var(--syn-op); }
.highlight .gp, .highlight .go { color: var(--muted); }

.table-wrap { overflow-x: auto; margin: 1.1em 0; border: 1px solid var(--line); border-radius: 8px; }
.table-wrap table { border-collapse: collapse; width: 100%; font-size: 14px; }
.table-wrap th, .table-wrap td { padding: 8px 12px; text-align: left; vertical-align: top; border-bottom: 1px solid var(--line); }
.table-wrap th { background: var(--panel); font-weight: 600; white-space: nowrap; }
.table-wrap tr:last-child td { border-bottom: none; }
.table-wrap td code { white-space: nowrap; overflow-wrap: normal; }

.admonition { margin: 1.3em 0; padding: 12px 16px; border-radius: 8px; border: 1px solid var(--line); background: var(--note-bg); }
.admonition > :last-child { margin-bottom: 0; }
.admonition-title { margin: 0 0 6px; font-weight: 600; color: var(--note); }
.admonition.warning, .admonition.danger { background: var(--warn-bg); }
.admonition.warning .admonition-title, .admonition.danger .admonition-title { color: var(--warn); }
.admonition.tip, .admonition.success { background: var(--tip-bg); }
.admonition.tip .admonition-title, .admonition.success .admonition-title { color: var(--tip); }

.tabbed-set { display: flex; flex-direction: column; margin: 1.1em 0; }
.tabbed-set > input { position: absolute; opacity: 0; pointer-events: none; }
.tabbed-labels { display: flex; flex-wrap: wrap; gap: 2px; border-bottom: 1px solid var(--line); }
.tabbed-labels > label { padding: 7px 12px; font-size: 13px; font-weight: 500; color: var(--muted); cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -1px; }
.tabbed-content > .tabbed-block { display: none; }
.tabbed-content > .tabbed-block > .highlight:first-child { margin-top: 10px; }
.tabbed-set > input:nth-child(1):checked ~ .tabbed-labels > label:nth-child(1),
.tabbed-set > input:nth-child(2):checked ~ .tabbed-labels > label:nth-child(2),
.tabbed-set > input:nth-child(3):checked ~ .tabbed-labels > label:nth-child(3),
.tabbed-set > input:nth-child(4):checked ~ .tabbed-labels > label:nth-child(4),
.tabbed-set > input:nth-child(5):checked ~ .tabbed-labels > label:nth-child(5),
.tabbed-set > input:nth-child(6):checked ~ .tabbed-labels > label:nth-child(6) { color: var(--accent); border-bottom-color: var(--accent); }
.tabbed-set > input:nth-child(1):checked ~ .tabbed-content > .tabbed-block:nth-child(1),
.tabbed-set > input:nth-child(2):checked ~ .tabbed-content > .tabbed-block:nth-child(2),
.tabbed-set > input:nth-child(3):checked ~ .tabbed-content > .tabbed-block:nth-child(3),
.tabbed-set > input:nth-child(4):checked ~ .tabbed-content > .tabbed-block:nth-child(4),
.tabbed-set > input:nth-child(5):checked ~ .tabbed-content > .tabbed-block:nth-child(5),
.tabbed-set > input:nth-child(6):checked ~ .tabbed-content > .tabbed-block:nth-child(6) { display: block; }
.tabbed-set > input:focus-visible ~ .tabbed-labels { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }

.api-sig { margin: 0.4em 0 0.6em; overflow-x: auto; }
.api-sig code { display: inline-block; white-space: pre; overflow-wrap: normal; font-size: 13px; padding: 6px 10px; border: 1px solid var(--line); }
pre.docstring {
  margin: 0 0 1em; padding: 12px 14px; overflow-x: auto; white-space: pre-wrap;
  font: 12.5px/1.6 var(--font-mono); background: var(--code-bg); border-radius: 8px; border: 1px solid var(--line);
}

.pager { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 3.5em; padding-top: 1.4em; border-top: 1px solid var(--line); }
.pager a {
  flex: 1 1 220px; display: flex; flex-direction: column; gap: 2px; padding: 12px 14px; border: 1px solid var(--line);
  border-radius: 8px; text-decoration: none; color: var(--fg); font-weight: 600;
}
.pager a span { font: 500 12px/1.2 var(--font-body); color: var(--muted); }
.pager a:hover { border-color: var(--accent); }
.pager .next { text-align: right; }

/* ---- search view ---- */
.search-summary { color: var(--muted); }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 0.6em 0 0; padding: 0; list-style: none; }
.chips button {
  font: 500 13px/1 var(--font-mono); padding: 8px 10px; border-radius: 6px; border: 1px solid var(--line);
  background: var(--panel); color: var(--fg); cursor: pointer;
}
.chips button:hover { border-color: var(--accent); color: var(--accent); }
.results { list-style: none; padding: 0; margin: 1.2em 0 0; display: flex; flex-direction: column; gap: 4px; }
.results a {
  display: flex; flex-direction: column; gap: 4px; padding: 12px 14px; border-radius: 8px;
  text-decoration: none; color: var(--fg); border: 1px solid transparent;
}
.results a:hover, .results a.selected { background: var(--panel); border-color: var(--line); }
.result-path { font: 500 12px/1.3 var(--font-body); color: var(--muted); }
.result-title { font: 600 15px/1.35 var(--font-head); color: var(--accent); }
.result-snippet { font-size: 13.5px; line-height: 1.55; color: var(--fg); overflow-wrap: anywhere; }
mark { background: var(--mark); color: inherit; border-radius: 2px; padding: 0 1px; }

/* ---- small screens ---- */
.scrim { display: none; }
@media (max-width: 900px) {
  .menu-btn { display: inline-flex; }
  .layout { grid-template-columns: minmax(0, 1fr); }
  .sidebar {
    position: fixed; z-index: 15; top: calc(var(--topbar-h) + env(safe-area-inset-top, 0px)); bottom: 0; left: 0;
    width: min(300px, 86vw); max-height: none; background: var(--panel);
    padding: 18px 14px calc(28px + env(safe-area-inset-bottom, 0px)) max(16px, env(safe-area-inset-left, 0px));
    transform: translateX(-102%); transition: transform 0.2s ease; border-right: 1px solid var(--line);
  }
  .sidebar.open { transform: none; }
  .scrim.open { display: block; position: fixed; inset: 0; z-index: 14; background: var(--fg); opacity: 0.25; }
  main { padding-left: 0; padding-top: 24px; }
  .search kbd { display: none; }
  .search input { padding-right: 12px; }
  .brand-name small { display: none; }
  .doc h1 { font-size: 1.75rem; }
  .doc .grid img { width: 100% !important; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; scroll-behavior: auto !important; } }
</style>

<header class="topbar">
  <div class="topbar-inner">
    <button class="menu-btn" id="menu-btn" type="button" aria-label="Show navigation" aria-expanded="false" aria-controls="sidebar">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
    </button>
    <a class="brand" href="#home">
      <span class="brand-name">EarthForge<small>documentation</small></span>
      <span class="brand-strip" aria-hidden="true"></span>
    </a>
    <div class="search" role="search">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
      <input id="q" type="search" placeholder="Search the docs" autocomplete="off" spellcheck="false" aria-label="Search the documentation">
      <kbd aria-hidden="true">/</kbd>
    </div>
  </div>
</header>

<div class="layout">
  <aside class="sidebar" id="sidebar" aria-label="Documentation">
    <nav class="nav" id="nav"></nav>
    <p class="sidebar-meta">__META__</p>
  </aside>
  <div class="scrim" id="scrim"></div>
  <main id="main">
    <section class="doc" id="view-search" hidden>
      <p class="eyebrow">Search</p>
      <h1 id="search-heading">Search the documentation</h1>
      <p class="search-summary" id="search-summary"></p>
      <ul class="chips" id="chips"></ul>
      <ol class="results" id="results"></ol>
    </section>
__CONTENT__
  </main>
</div>

<script type="application/json" id="docs-data">__DATA__</script>
<script>
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("docs-data").textContent);
  var pages = DATA.pages, index = DATA.index;
  var byId = {};
  pages.forEach(function (p) { byId[p.id] = p; });

  var nav = document.getElementById("nav");
  var input = document.getElementById("q");
  var sidebar = document.getElementById("sidebar");
  var scrim = document.getElementById("scrim");
  var menuBtn = document.getElementById("menu-btn");
  var searchView = document.getElementById("view-search");
  var resultsEl = document.getElementById("results");
  var summaryEl = document.getElementById("search-summary");
  var headingEl = document.getElementById("search-heading");
  var chipsEl = document.getElementById("chips");
  var lastPage = "#home";
  var selected = 0;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---- sidebar navigation ----
  var html = "", group = null;
  pages.forEach(function (p) {
    if (p.group !== group) {
      if (p.group) html += '<div class="nav-group">' + esc(p.group) + "</div>";
      group = p.group;
    }
    html += '<a href="#' + p.id + '" data-page="' + p.id + '">' + esc(p.title) + "</a>";
    if (p.headings.length) {
      html += '<div class="sub" data-sub="' + p.id + '" hidden>' + p.headings.map(function (h) {
        return '<a href="#' + h.id + '">' + esc(h.title) + "</a>";
      }).join("") + "</div>";
    }
  });
  nav.innerHTML = html;

  function setActive(pid) {
    nav.querySelectorAll("a[data-page]").forEach(function (a) {
      var on = a.getAttribute("data-page") === pid;
      a.classList.toggle("active", on);
      if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    nav.querySelectorAll(".sub").forEach(function (s) { s.hidden = s.getAttribute("data-sub") !== pid; });
  }

  function openMenu(open) {
    sidebar.classList.toggle("open", open);
    scrim.classList.toggle("open", open);
    menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
  }
  menuBtn.addEventListener("click", function () { openMenu(!sidebar.classList.contains("open")); });
  scrim.addEventListener("click", function () { openMenu(false); });

  // ---- routing: #page or #page--section, plus #search ----
  function route() {
    var h = decodeURIComponent(location.hash.slice(1)) || "home";
    openMenu(false);
    var docs = document.querySelectorAll("section.doc[data-page]");
    if (h === "search") {
      docs.forEach(function (s) { s.hidden = true; });
      searchView.hidden = false;
      setActive(null);
      renderResults();
      window.scrollTo(0, 0);
      return;
    }
    var cut = h.indexOf("--");
    var pid = cut >= 0 ? h.slice(0, cut) : h;
    if (!byId[pid]) pid = "home";
    lastPage = "#" + h;
    searchView.hidden = true;
    docs.forEach(function (s) { s.hidden = s.getAttribute("data-page") !== pid; });
    setActive(pid);
    var target = cut >= 0 ? document.getElementById(h) : null;
    if (target) target.scrollIntoView(); else window.scrollTo(0, 0);
  }
  window.addEventListener("hashchange", route);

  // ---- search ----
  function terms(q) { return q.toLowerCase().split(/\s+/).filter(Boolean); }
  function count(hay, w) {
    var n = 0, i = hay.indexOf(w);
    while (i !== -1 && n < 6) { n++; i = hay.indexOf(w, i + w.length); }
    return n;
  }
  // guide pages first; reference pages (errors, API, changelog) rank a little lower
  var PAGE_BIAS = { errors: -6, api: -4, changelog: -10 };
  function search(q) {
    var ts = terms(q);
    if (!ts.length) return [];
    var out = [];
    index.forEach(function (e) {
      var t = e.t.toLowerCase(), x = e.x.toLowerCase(), pt = e.pt.toLowerCase(), score = PAGE_BIAS[e.p] || 0;
      for (var i = 0; i < ts.length; i++) {
        var w = ts[i], inT = t.indexOf(w) !== -1, nX = count(x, w), inP = pt.indexOf(w) !== -1;
        if (!inT && !nX && !inP) return;
        score += (inT ? 14 : 0) + nX + (inP ? 3 : 0);
      }
      out.push({ e: e, score: score });
    });
    out.sort(function (a, b) { return b.score - a.score; });
    return out.slice(0, 60);
  }
  function highlight(text, ts) {
    var safe = esc(text);
    ts.forEach(function (w) {
      var pattern = new RegExp("(" + esc(w).replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi");
      safe = safe.replace(pattern, "<mark>$1</mark>");
    });
    return safe;
  }
  function snippet(text, ts) {
    var lower = text.toLowerCase(), at = -1;
    ts.forEach(function (w) { var i = lower.indexOf(w); if (i !== -1 && (at === -1 || i < at)) at = i; });
    if (at === -1) return text.slice(0, 180) + (text.length > 180 ? "…" : "");
    var start = Math.max(0, at - 70), end = Math.min(text.length, at + 170);
    return (start > 0 ? "…" : "") + text.slice(start, end).trim() + (end < text.length ? "…" : "");
  }

  var SUGGESTIONS = ["clip", "palette", "normalizedDifference", "where", "remap", "save", "vector.Open", "expression", "NoData", "memory"];
  chipsEl.innerHTML = SUGGESTIONS.map(function (s) {
    return '<li><button type="button" data-q="' + esc(s) + '">' + esc(s) + "</button></li>";
  }).join("");
  chipsEl.addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-q]");
    if (!b) return;
    input.value = b.getAttribute("data-q");
    if (location.hash !== "#search") location.hash = "search"; else renderResults();
    input.focus();
  });

  function renderResults() {
    var q = input.value.trim(), ts = terms(q);
    chipsEl.hidden = ts.length > 0;
    if (!ts.length) {
      headingEl.textContent = "Search the documentation";
      summaryEl.textContent = "Type in the search box to find methods, parameters, error messages and examples. Try one of these:";
      resultsEl.innerHTML = "";
      return;
    }
    var hits = search(q);
    selected = 0;
    headingEl.textContent = "Results for “" + q + "”";
    summaryEl.textContent = hits.length
      ? hits.length + (hits.length === 60 ? "+" : "") + " matching section" + (hits.length === 1 ? "" : "s") + ". Press Enter to open the first one."
      : "No sections match all of these words. Check the spelling, or search for fewer words.";
    resultsEl.innerHTML = hits.map(function (h, i) {
      var e = h.e, path = e.pt + (e.t && e.t !== e.pt ? " › " + e.t : "");
      return '<li><a href="#' + e.a + '"' + (i === 0 ? ' class="selected"' : "") + ">" +
        '<span class="result-path">' + esc(e.pt) + "</span>" +
        '<span class="result-title">' + highlight(e.t || e.pt, ts) + "</span>" +
        '<span class="result-snippet">' + highlight(snippet(e.x, ts), ts) + "</span></a></li>";
    }).join("");
  }

  input.addEventListener("input", function () {
    if (location.hash !== "#search") location.hash = "search"; else renderResults();
  });
  input.addEventListener("keydown", function (ev) {
    var links = resultsEl.querySelectorAll("a");
    if (ev.key === "Enter" && links.length) {
      ev.preventDefault();
      location.hash = links[selected].getAttribute("href").slice(1);
      input.blur();
    } else if ((ev.key === "ArrowDown" || ev.key === "ArrowUp") && links.length) {
      ev.preventDefault();
      links[selected].classList.remove("selected");
      selected = (selected + (ev.key === "ArrowDown" ? 1 : links.length - 1)) % links.length;
      links[selected].classList.add("selected");
      links[selected].scrollIntoView({ block: "nearest" });
    } else if (ev.key === "Escape") {
      input.value = "";
      input.blur();
      if (location.hash === "#search") location.hash = lastPage.slice(1);
    }
  });
  document.addEventListener("keydown", function (ev) {
    var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (ev.key === "/" && !typing) { ev.preventDefault(); input.focus(); input.select(); }
  });

  // ---- copy buttons on code blocks ----
  document.querySelectorAll(".highlight").forEach(function (block) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.textContent = "Copy";
    btn.addEventListener("click", function () {
      var code = block.querySelector("code") || block.querySelector("pre");
      var text = code.innerText;
      function done() { btn.textContent = "Copied"; setTimeout(function () { btn.textContent = "Copy"; }, 1400); }
      function fallback() {
        var range = document.createRange();
        range.selectNodeContents(code);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        btn.textContent = "Selected: press Ctrl+C";
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, fallback);
      } else {
        fallback();
      }
    });
    block.appendChild(btn);
  });

  route();
})();
</script>
"""


if __name__ == "__main__":
    main()
