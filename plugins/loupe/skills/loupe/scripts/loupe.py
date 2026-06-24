#!/usr/bin/env python3
"""
loupe - an interactive PR-review surface for coding agents.

The agent produces a review.json (PR metadata + raw unified diff + findings).
loupe parses the diff, renders a self-contained review UI, opens it in the
browser, and then *blocks* (long-poll style) until you curate the review and
send it back. The curated feedback is printed to stdout as JSON for the agent
to act on (post to GitHub, revise, etc.). Status goes to stderr so stdout stays
clean.

Zero third-party dependencies. Python 3.8+.

Usage:
    python3 loupe.py review.json [--port 7842] [--no-open] [--timeout 0]

Input JSON schema:
    {
      "pr": {"number", "title", "repo", "author", "branch", "base", "url"},
      "summary": "agent's one-paragraph overview",
      "diff": "<raw unified diff, e.g. from `gh pr diff <n>`>",
      "findings": [
        {
          "id": "f1",
          "file": "path/to/file.go",
          "line": 23,            # line number the finding anchors to
          "side": "new",         # "new" | "old"  (default "new")
          "severity": "high",    # critical|high|medium|low|nit|praise
          "title": "short headline",
          "body": "the review comment",
          "suggestion": "optional replacement code"
        }
      ]
    }

Output JSON (stdout):
    {
      "status": "submitted",
      "verdict": "request_changes",     # approve|comment|request_changes
      "notes": "overall reviewer notes",
      "findings": [
        {"id": "f1", "decision": "accept", "body": "..."},   # accept|edit|dismiss
        ...
      ],
      "added_comments": [
        {"file": "...", "line": 31, "side": "new", "body": "..."}
      ]
    }
"""

import argparse
import json
import re
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RESULT = {"value": None}
DONE = threading.Event()

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


# --------------------------------------------------------------------------- #
# Unified diff parser
# --------------------------------------------------------------------------- #
def parse_diff(diff_text):
    """Parse a unified diff into a list of file objects with hunks and lines."""
    files = []
    cur = None
    hunk = None

    def new_file(path):
        return {
            "path": path or "?",
            "old_path": path or "?",
            "status": "modified",
            "additions": 0,
            "deletions": 0,
            "binary": False,
            "hunks": [],
        }

    for raw in (diff_text or "").split("\n"):
        if raw.startswith("diff --git"):
            parts = raw.split(" ")
            path = None
            if len(parts) >= 4:
                b = parts[3]
                path = b[2:] if b.startswith("b/") else b
            cur = new_file(path)
            files.append(cur)
            hunk = None
            continue
        if raw.startswith("new file mode"):
            if cur:
                cur["status"] = "added"
            continue
        if raw.startswith("deleted file mode"):
            if cur:
                cur["status"] = "deleted"
            continue
        if raw.startswith("rename from "):
            if cur:
                cur["status"] = "renamed"
                cur["old_path"] = raw[len("rename from "):].strip()
            continue
        if raw.startswith("rename to "):
            if cur:
                cur["path"] = raw[len("rename to "):].strip()
            continue
        if raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
            if cur:
                cur["binary"] = True
            continue
        if raw.startswith("--- "):
            continue
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            if p.startswith("b/"):
                p = p[2:]
            if p != "/dev/null":
                if cur is None:
                    cur = new_file(p)
                    files.append(cur)
                elif cur["path"] in (None, "?"):
                    cur["path"] = p
            continue
        m = HUNK_RE.match(raw)
        if m:
            if cur is None:
                cur = new_file("?")
                files.append(cur)
            hunk = {
                "header": raw,
                "context": (m.group(5) or "").strip(),
                "lines": [],
                "_old": int(m.group(1)),
                "_new": int(m.group(3)),
            }
            cur["hunks"].append(hunk)
            continue
        if hunk is not None:
            if raw.startswith("+"):
                hunk["lines"].append(
                    {"type": "add", "old": None, "new": hunk["_new"], "text": raw[1:]}
                )
                hunk["_new"] += 1
                cur["additions"] += 1
            elif raw.startswith("-"):
                hunk["lines"].append(
                    {"type": "del", "old": hunk["_old"], "new": None, "text": raw[1:]}
                )
                hunk["_old"] += 1
                cur["deletions"] += 1
            elif raw.startswith("\\"):
                continue  # "\ No newline at end of file"
            elif raw.startswith(" ") or raw == "":
                hunk["lines"].append(
                    {
                        "type": "context",
                        "old": hunk["_old"],
                        "new": hunk["_new"],
                        "text": raw[1:] if raw else "",
                    }
                )
                hunk["_old"] += 1
                hunk["_new"] += 1

    for f in files:
        for h in f["hunks"]:
            h.pop("_old", None)
            h.pop("_new", None)
    return files


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
def build_html(review, demo=False):
    front = {
        "pr": review.get("pr", {}) or {},
        "summary": review.get("summary", "") or "",
        "files": review["_files"],
        "findings": review.get("findings", []) or [],
        "structure": review.get("structure") or None,
        "diagrams": review.get("diagrams") or None,
    }
    data_json = json.dumps(front)
    # keep the JSON from terminating the <script> early
    data_json = data_json.replace("</", "<\\/").replace("\u2028", "\\u2028").replace(
        "\u2029", "\\u2029"
    )
    html = TEMPLATE.replace("__LOUPE_DATA__", data_json)
    return html.replace("__LOUPE_DEMO__", "true" if demo else "false")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>loupe · review</title>
<style>
:root{
  --bg:#f6f6f8; --surface:#fff; --surface-2:#fbfbfd; --text:#1b1b1f; --muted:#6b6b76;
  --border:#e5e5ea; --border-strong:#d4d4da; --accent:#5b5bd6; --accent-weak:#ececfb;
  --add-bg:rgba(33,150,83,.10); --add-num:#1a7f43; --del-bg:rgba(208,48,40,.10); --del-num:#b42318;
  --sev-critical:#b42318; --sev-high:#d9480f; --sev-medium:#b07800; --sev-low:#2f6f9f;
  --sev-nit:#76767f; --sev-praise:#1f8f5b;
  --bar:#1b1b22; --bar-text:#f3f3f6; --bar-muted:#a6a6b2;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --ui:-apple-system,system-ui,"Segoe UI",Roboto,Inter,sans-serif;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0e1014; --surface:#15171d; --surface-2:#1a1d24; --text:#e7e7ec; --muted:#9a9aa6;
    --border:#262932; --border-strong:#333744; --accent:#8b8bf0; --accent-weak:#23233a;
    --add-bg:rgba(46,160,67,.15); --add-num:#56d364; --del-bg:rgba(248,81,73,.15); --del-num:#f08a82;
    --sev-medium:#d9a521; --sev-low:#6cb3e0; --bar:#080a0e; --bar-text:#f3f3f6; --bar-muted:#8d8d99;
  }
}
*{box-sizing:border-box}
html,body{margin:0}
body{
  background:var(--bg); color:var(--text); font-family:var(--ui);
  font-size:14px; line-height:1.55; -webkit-font-smoothing:antialiased;
  padding-bottom:124px;
}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px}

/* top bar */
header.top{
  position:sticky;top:0;z-index:30;background:var(--surface);
  border-bottom:1px solid var(--border);
}
.top .wrap{padding-top:14px;padding-bottom:14px;display:flex;gap:16px;align-items:flex-start}
.top h1{font-size:17px;font-weight:650;letter-spacing:-.01em;margin:0 0 4px}
.crumbs{font-size:12.5px;color:var(--muted);display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.crumbs .sep{opacity:.5}
.branch{font-family:var(--mono);font-size:11.5px;background:var(--surface-2);border:1px solid var(--border);
  border-radius:5px;padding:1px 6px}
.legend{margin-left:auto;display:flex;gap:10px;flex-wrap:wrap;font-size:11px;color:var(--muted);padding-top:2px}
.legend .dot{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px;vertical-align:middle}

/* layout */
.cols{display:flex;gap:24px;align-items:flex-start}
.main{flex:1;min-width:0;padding-top:20px}
nav.files{width:232px;flex:none;position:sticky;top:74px;padding-top:20px;max-height:calc(100vh - 96px);overflow:auto}
nav.files .nh{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin:0 0 8px;font-weight:600}
nav.files a.f{display:flex;gap:8px;align-items:baseline;padding:5px 8px;border-radius:6px;color:var(--text);font-size:12.5px}
nav.files a.f:hover{background:var(--surface-2);text-decoration:none}
nav.files a.f .fp{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono);font-size:11.5px}
nav.files a.f .cnt{font-size:10px;background:var(--accent-weak);color:var(--accent);border-radius:9px;padding:0 6px;font-weight:600}
.stat-add{color:var(--add-num);font-variant-numeric:tabular-nums}
.stat-del{color:var(--del-num);font-variant-numeric:tabular-nums}

/* summary */
.summary{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px;margin-bottom:22px}
.summary .eyebrow{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);font-weight:700;margin-bottom:6px}
.summary p{margin:0;white-space:pre-wrap}

/* file card */
.file{background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:22px;overflow:hidden}
.file > .fhead{display:flex;gap:10px;align-items:center;padding:11px 14px;border-bottom:1px solid var(--border);
  background:var(--surface-2);position:sticky;top:74px;z-index:10}
.file .fhead .path{font-family:var(--mono);font-size:12.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.file .fhead .tag{font-size:10px;text-transform:uppercase;letter-spacing:.05em;border-radius:5px;padding:1px 6px;font-weight:700}
.tag.added{background:var(--add-bg);color:var(--add-num)}
.tag.deleted{background:var(--del-bg);color:var(--del-num)}
.tag.renamed,.tag.modified{background:var(--accent-weak);color:var(--accent)}
.file .fhead .nums{margin-left:auto;font-family:var(--mono);font-size:11.5px}

/* diff */
.hunk-hd{font-family:var(--mono);font-size:11.5px;color:var(--muted);background:var(--surface-2);
  padding:4px 14px;border-top:1px solid var(--border);border-bottom:1px solid var(--border)}
.diff{font-family:var(--mono);font-size:12.5px;line-height:1.5}
.ln{display:grid;grid-template-columns:46px 46px 22px 1fr;align-items:stretch;position:relative}
.ln .g{color:var(--muted);opacity:.7;text-align:right;padding:0 8px;user-select:none;
  font-variant-numeric:tabular-nums;border-right:1px solid var(--border)}
.ln .plus{opacity:0;cursor:pointer;color:var(--accent);text-align:center;font-weight:700;user-select:none}
.ln:hover .plus{opacity:1}
.ln .code{padding:0 10px;white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere}
.ln.add{background:var(--add-bg)}
.ln.add .code::before{content:"+";color:var(--add-num);margin-right:2px}
.ln.del{background:var(--del-bg)}
.ln.del .code::before{content:"\2212";color:var(--del-num);margin-right:2px}
.ln.context .code::before{content:" ";margin-right:2px}
.anchor:empty{display:none}
.anchor{padding:6px 14px 6px 50px;display:flex;flex-direction:column;gap:8px}

/* finding / comment cards */
.card{border:1px solid var(--border-strong);border-left-width:3px;border-radius:8px;background:var(--surface);
  padding:10px 12px;font-family:var(--ui);font-size:13px}
.card.dismissed{opacity:.5}
.card.dismissed .ctitle, .card.dismissed .cbody{text-decoration:line-through;text-decoration-thickness:1px}
.card .crow{display:flex;gap:8px;align-items:center;margin-bottom:5px;flex-wrap:wrap}
.pill{font-size:10px;text-transform:uppercase;letter-spacing:.04em;font-weight:700;border-radius:5px;padding:1px 7px;color:#fff}
.ctitle{font-weight:650;font-size:13px}
.edited-tag,.you-tag{font-size:10px;font-weight:700;color:var(--muted);border:1px solid var(--border-strong);
  border-radius:5px;padding:0 5px;text-transform:uppercase;letter-spacing:.04em}
.cbody{white-space:pre-wrap;margin:2px 0}
.csugg{font-family:var(--mono);font-size:11.5px;background:var(--surface-2);border:1px solid var(--border);
  border-radius:6px;padding:8px 10px;margin-top:7px;white-space:pre-wrap;overflow-x:auto}
.cactions{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
.cbody-edit{width:100%;font-family:var(--ui);font-size:13px;border:1px solid var(--accent);border-radius:6px;
  padding:7px 9px;background:var(--surface);color:var(--text);resize:vertical;min-height:64px}

/* severity rail colors */
.sev-critical{border-left-color:var(--sev-critical)} .pill.sev-critical{background:var(--sev-critical)}
.sev-high{border-left-color:var(--sev-high)} .pill.sev-high{background:var(--sev-high)}
.sev-medium{border-left-color:var(--sev-medium)} .pill.sev-medium{background:var(--sev-medium)}
.sev-low{border-left-color:var(--sev-low)} .pill.sev-low{background:var(--sev-low)}
.sev-nit{border-left-color:var(--sev-nit)} .pill.sev-nit{background:var(--sev-nit)}
.sev-praise{border-left-color:var(--sev-praise)} .pill.sev-praise{background:var(--sev-praise)}
.card.you{border-left-color:var(--accent)}

/* buttons */
button{font-family:var(--ui);font-size:12px;font-weight:600;border-radius:6px;cursor:pointer;
  padding:5px 11px;border:1px solid var(--border-strong);background:var(--surface);color:var(--text)}
button:hover{border-color:var(--accent);color:var(--accent)}
button.ghost{background:transparent}
button.danger:hover{border-color:var(--sev-critical);color:var(--sev-critical)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.primary:hover{filter:brightness(1.06);color:#fff}
button.is-on{background:var(--accent-weak);border-color:var(--accent);color:var(--accent)}

/* composer */
.composer{border:1px solid var(--accent);border-radius:8px;padding:9px;background:var(--surface);display:flex;flex-direction:column;gap:7px}
.composer textarea{width:100%;font-family:var(--ui);font-size:13px;border:1px solid var(--border-strong);
  border-radius:6px;padding:7px 9px;background:var(--surface-2);color:var(--text);resize:vertical;min-height:60px}
.composer .crow2{display:flex;gap:6px;justify-content:flex-end}

/* review bar */
.reviewbar{position:fixed;left:0;right:0;bottom:0;z-index:40;background:var(--bar);color:var(--bar-text);
  border-top:1px solid #000}
.reviewbar .wrap{display:flex;gap:16px;align-items:center;padding:12px 20px}
.seg{display:inline-flex;border:1px solid #3a3a46;border-radius:8px;overflow:hidden}
.seg button{border:0;border-radius:0;background:transparent;color:var(--bar-muted);padding:7px 13px;font-weight:600}
.seg button + button{border-left:1px solid #3a3a46}
.seg button.on{color:#fff}
.seg button[data-v="approve"].on{background:rgba(33,150,83,.30);color:#7ee2a8}
.seg button[data-v="comment"].on{background:rgba(91,91,214,.34);color:#c7c7ff}
.seg button[data-v="request_changes"].on{background:rgba(208,48,40,.30);color:#f3a59e}
.tally{font-size:12px;color:var(--bar-muted);display:flex;gap:12px;flex-wrap:wrap}
.tally b{color:var(--bar-text);font-variant-numeric:tabular-nums}
.notes-toggle{margin-left:auto}
.bar-notes{display:none;background:var(--bar)}
.bar-notes.show{display:block}
.bar-notes .wrap{padding:0 20px 14px}
.bar-notes textarea{width:100%;font-family:var(--ui);font-size:13px;border:1px solid #3a3a46;border-radius:8px;
  padding:9px 11px;background:#000;color:var(--bar-text);resize:vertical;min-height:54px}
.send{margin-left:0}
.reviewbar button.send{padding:8px 18px;font-size:13px}

/* sent overlay */
.overlay{position:fixed;inset:0;z-index:100;background:rgba(10,10,14,.72);display:none;
  align-items:center;justify-content:center;backdrop-filter:blur(2px)}
.overlay.show{display:flex}
.overlay .box{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:28px 32px;
  text-align:center;max-width:380px}
.overlay .check{font-size:34px;line-height:1}
.overlay h2{margin:10px 0 6px;font-size:18px}
.overlay p{margin:0;color:var(--muted);font-size:13px}
.empty{color:var(--muted);font-size:13px;padding:8px 0}

/* ---- structure / UML meta view ---- */
/* ---- overview (mermaid) diagrams ---- */
.diagrams{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px;margin-bottom:22px}
.diagrams .shead{display:flex;align-items:baseline;gap:12px;margin-bottom:10px;flex-wrap:wrap}
.dg-card{margin:10px 0;padding:12px;border:1px solid var(--border);border-radius:8px;background:var(--surface-2);overflow:auto}
.dg-title{font-size:12.5px;font-weight:600;margin-bottom:8px}
.dg-render{text-align:center}
.dg-render svg{max-width:100%;height:auto}
.dg-src{font-family:var(--mono);font-size:12px;white-space:pre;overflow:auto;color:var(--muted);margin:0;text-align:left}
.dg-err{font-size:11px;color:var(--sev-medium);margin-top:6px}
.structure{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px;margin-bottom:22px}
.structure .shead{display:flex;align-items:center;gap:12px;margin-bottom:6px;flex-wrap:wrap}
.structure .eyebrow{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);font-weight:700}
.structure .stitle{font-size:13px;font-weight:600}
.uml-legend{margin-left:auto;display:flex;gap:12px;flex-wrap:wrap;font-size:11px;color:var(--muted)}
.uml-legend .dot{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:middle}
.uml-stage{position:relative;margin-top:14px}
.uml-edges{position:absolute;inset:0;pointer-events:none;z-index:1;overflow:visible}
.uml-cards{position:relative;z-index:2;display:flex;flex-wrap:wrap;gap:26px;align-items:flex-start}

.uml-card{min-width:218px;max-width:320px;border:1px solid var(--border-strong);border-radius:8px;overflow:hidden;
  background:var(--surface);box-shadow:0 1px 2px rgba(0,0,0,.05)}
.uml-card .uhead{padding:9px 12px;border-bottom:1px solid var(--border);background:var(--surface-2);
  display:flex;align-items:center;gap:8px}
.uml-card .stereo{display:block;font-size:10px;color:var(--muted);font-family:var(--mono);line-height:1.2}
.uml-card .cname{font-weight:700;font-size:14px;font-family:var(--mono);cursor:pointer}
.uml-card .cname:hover{color:var(--accent)}
.uml-card .ubadge{margin-left:auto;font-size:9.5px;font-weight:800;text-transform:uppercase;letter-spacing:.05em;
  border-radius:4px;padding:1px 6px;color:#fff;white-space:nowrap}
.uml-comp{padding:6px 0;font-family:var(--mono);font-size:11.5px}
.uml-comp + .uml-comp{border-top:1px solid var(--border)}
.uml-comp .mrow{display:grid;grid-template-columns:16px 1fr;gap:2px;padding:2px 12px 2px 6px;align-items:baseline}
.uml-comp .mrow .gut{text-align:center;font-weight:800;opacity:.85}
.uml-comp .mrow .msig{white-space:pre-wrap;word-break:break-word}
.uml-comp .none{color:var(--muted);font-style:italic;padding:2px 12px;font-size:11px}
.uml-card .uactions{padding:6px 8px;border-top:1px dashed var(--border);display:flex;gap:6px}

/* change states (shared by card accents and member rows) */
.chg-added{--c:var(--sev-praise)} .chg-modified{--c:var(--sev-medium)}
.chg-removed{--c:var(--sev-critical)} .chg-unchanged{--c:var(--muted)}
.uml-card.chg-added,.uml-card.chg-modified,.uml-card.chg-removed{border-color:var(--c);border-left:3px solid var(--c)}
.uml-card.chg-removed{border-style:dashed;opacity:.62}
.uml-card .ubadge.chg-added{background:var(--sev-praise)}
.uml-card .ubadge.chg-modified{background:var(--sev-medium)}
.uml-card .ubadge.chg-removed{background:var(--sev-critical)}
.mrow.chg-added{color:var(--sev-praise)} .mrow.chg-added .gut::before{content:"+"}
.mrow.chg-modified{color:var(--sev-medium)} .mrow.chg-modified .gut::before{content:"~"}
.mrow.chg-removed{color:var(--sev-critical);text-decoration:line-through;text-decoration-thickness:1px}
.mrow.chg-removed .gut::before{content:"\2212"}
.mrow.chg-unchanged .gut::before{content:""}

.uml-empty{color:var(--muted);font-size:12.5px}
@media (max-width:720px){ .uml-edges{display:none} }
#uml-edges line{stroke:var(--muted);stroke-width:1.4}
#uml-edges text{fill:var(--muted);font-family:var(--ui);font-size:10px;paint-order:stroke;stroke:var(--surface);stroke-width:3px}
#uml-edges .mk-stroke{stroke:var(--muted);stroke-width:1.2}
#uml-edges .mk-fill-surface{fill:var(--surface)}
#uml-edges .mk-fill-muted{fill:var(--muted)}
#uml-edges .mk-fill-none{fill:none}

</style>
</head>
<body>
<header class="top">
  <div class="wrap">
    <div style="flex:1;min-width:0">
      <h1 id="pr-title">Pull request</h1>
      <div class="crumbs" id="pr-crumbs"></div>
    </div>
    <div class="legend" id="legend"></div>
  </div>
</header>

<div class="wrap">
  <div class="cols">
    <nav class="files">
      <p class="nh">Files</p>
      <div id="file-nav"></div>
    </nav>
    <div class="main">
      <div class="summary" id="summary-box" style="display:none">
        <div class="eyebrow">Agent summary</div>
        <p id="summary-text"></p>
      </div>
      <section class="diagrams" id="diagrams-box" style="display:none">
        <div class="shead">
          <span class="eyebrow">Overview diagrams</span>
          <span class="stitle">the change at a glance</span>
        </div>
        <div id="dg-list"></div>
      </section>
      <section class="structure" id="structure-box" style="display:none">
        <div class="shead">
          <span class="eyebrow">Structure changes</span>
          <span class="stitle" id="structure-title"></span>
          <div class="uml-legend">
            <span><span class="dot" style="background:var(--sev-praise)"></span>Added</span>
            <span><span class="dot" style="background:var(--sev-medium)"></span>Modified</span>
            <span><span class="dot" style="background:var(--sev-critical)"></span>Removed</span>
          </div>
        </div>
        <div class="uml-stage">
          <svg class="uml-edges" id="uml-edges"></svg>
          <div class="uml-cards" id="uml-cards"></div>
        </div>
      </section>
      <div id="files"></div>
    </div>
  </div>
</div>

<div class="bar-notes" id="bar-notes">
  <div class="wrap"><textarea id="notes" placeholder="Overall notes for the agent (optional) — e.g. what to fix before merge, what to ignore."></textarea></div>
</div>
<div class="reviewbar">
  <div class="wrap">
    <div class="seg" id="verdict">
      <button data-v="approve">Approve</button>
      <button data-v="comment" class="on">Comment</button>
      <button data-v="request_changes">Request changes</button>
    </div>
    <div class="tally" id="tally"></div>
    <button class="ghost notes-toggle" id="notes-toggle" style="color:var(--bar-muted);border-color:#3a3a46">Notes</button>
    <button class="primary send" id="send">Send review to agent</button>
  </div>
</div>

<div class="overlay" id="overlay">
  <div class="box">
    <div class="check">✓</div>
    <h2>Review sent</h2>
    <p>Your curated review is back with the agent. You can close this tab.</p>
  </div>
</div>

<script>
const DATA = __LOUPE_DATA__;
const DEMO = __LOUPE_DEMO__;
const SEV = ["critical","high","medium","low","nit","praise"];
const SEV_LABEL = {critical:"Critical",high:"High",medium:"Medium",low:"Low",nit:"Nit",praise:"Praise"};

// per-finding state + human-added comments
const fstate = {};   // id -> {decision:'include'|'dismiss', body:editedOrNull}
const added = [];    // {file, line, side, body, _id}
const structureComments = []; // {class, file, line, body, _id}
let verdict = "comment";
let uid = 0;

function el(tag, props, ...kids){
  const e = document.createElement(tag);
  if(props) for(const k in props){
    if(k==="class") e.className=props[k];
    else if(k==="text") e.textContent=props[k];
    else if(k.startsWith("on")) e.addEventListener(k.slice(2).toLowerCase(), props[k]);
    else if(k==="style") e.setAttribute("style",props[k]);
    else if(props[k]!==null&&props[k]!==undefined) e.setAttribute(k,props[k]);
  }
  for(const kid of kids){ if(kid==null) continue; e.append(kid.nodeType?kid:document.createTextNode(kid)); }
  return e;
}
const sevClass = s => "sev-" + (SEV.includes(s)?s:"low");
const akey = (side,line) => side+":"+line;
const slug = p => "f-"+p.replace(/[^a-z0-9]/gi,"-");

// ---- header ----
function renderHeader(){
  const pr = DATA.pr||{};
  document.getElementById("pr-title").textContent = pr.title || "Pull request";
  const c = document.getElementById("pr-crumbs"); c.innerHTML="";
  const bits = [];
  if(pr.repo) bits.push(el("span",{text:pr.repo}));
  if(pr.number) bits.push(el("span",{class:"sep"},"·"), el("span",{text:"#"+pr.number}));
  if(pr.branch){
    bits.push(el("span",{class:"sep"},"·"),
      el("span",{class:"branch",text:pr.branch}),
      el("span",{class:"sep"},"→"),
      el("span",{class:"branch",text:pr.base||"base"}));
  }
  if(pr.author) bits.push(el("span",{class:"sep"},"·"), el("span",{text:"@"+pr.author}));
  if(pr.url) bits.push(el("span",{class:"sep"},"·"), el("a",{href:pr.url,target:"_blank",text:"open on GitHub ↗"}));
  bits.forEach(b=>c.append(b));

  const counts={};
  (DATA.findings||[]).forEach(f=>{const s=SEV.includes(f.severity)?f.severity:"low";counts[s]=(counts[s]||0)+1;});
  const lg=document.getElementById("legend"); lg.innerHTML="";
  SEV.filter(s=>counts[s]).forEach(s=>{
    lg.append(el("span",{},
      el("span",{class:"dot",style:"background:var(--sev-"+s+")"}),
      SEV_LABEL[s]+" "+counts[s]));
  });
}

// ---- file nav ----
function renderNav(){
  const nav=document.getElementById("file-nav"); nav.innerHTML="";
  const st=DATA.structure;
  if(st && Array.isArray(st.classes) && st.classes.length){
    const cc=st.classes.filter(c=>(c.change||"unchanged")!=="unchanged").length;
    const a=el("a",{class:"f",href:"#structure-box",style:"font-weight:600"},
      el("span",{class:"fp",text:"◇ Structure"}),
      el("span",{class:"cnt",text:String(cc||st.classes.length)}));
    nav.append(a);
  }
  (DATA.files||[]).forEach(f=>{
    const fc=(DATA.findings||[]).filter(x=>x.file===f.path).length;
    const a=el("a",{class:"f",href:"#"+slug(f.path)},
      el("span",{class:"fp",text:f.path}),
      el("span",{class:"stat-add",text:"+"+f.additions}),
      el("span",{class:"stat-del",text:"−"+f.deletions}));
    if(fc) a.append(el("span",{class:"cnt",text:String(fc)}));
    nav.append(a);
  });
  if(!(DATA.files||[]).length) nav.append(el("div",{class:"empty",text:"No file changes in diff."}));
}

// ---- summary ----
function renderSummary(){
  if(DATA.summary && DATA.summary.trim()){
    document.getElementById("summary-box").style.display="";
    document.getElementById("summary-text").textContent=DATA.summary;
  }
}

// ---- structure / UML meta view ----
const CHG = {added:"chg-added",modified:"chg-modified",removed:"chg-removed",unchanged:"chg-unchanged"};
const CHG_LABEL = {added:"New",modified:"Modified",removed:"Removed"};
const cardById = {};   // class name -> card element

function chgClass(c){ return CHG[c] || CHG.unchanged; }

// ---- overview (mermaid) diagrams ----
async function renderDiagrams(){
  const list = DATA.diagrams;
  if(!Array.isArray(list) || !list.length) return;
  document.getElementById("diagrams-box").style.display="";
  const root = document.getElementById("dg-list"); root.innerHTML="";
  let mermaid = null;
  try{
    const mod = await import("https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs");
    mermaid = mod.default;
    const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme:dark)").matches;
    mermaid.initialize({startOnLoad:false, theme:dark?"dark":"default", securityLevel:"loose", flowchart:{useMaxWidth:true}});
  }catch(e){ mermaid = null; } // offline / CDN blocked → fall back to source
  for(let i=0;i<list.length;i++){
    const d = list[i] || {};
    const card = el("div",{class:"dg-card"});
    if(d.title) card.append(el("div",{class:"dg-title",text:d.title}));
    const holder = el("div",{class:"dg-render"});
    card.append(holder); root.append(card);
    const code = String(d.mermaid||"").trim();
    if(mermaid && code){
      try{
        const {svg} = await mermaid.render("dg-svg-"+i+"-"+(d.id||""), code);
        holder.innerHTML = svg;
      }catch(err){
        holder.append(el("pre",{class:"dg-src",text:code}));
        holder.append(el("div",{class:"dg-err",text:"diagram failed to render — showing Mermaid source"}));
      }
    }else{
      holder.append(el("pre",{class:"dg-src",text:code}));
      if(!mermaid) holder.append(el("div",{class:"dg-err",text:"Mermaid CDN unavailable — showing source (paste into mermaid.live)"}));
    }
  }
}

function renderStructure(){
  const st = DATA.structure;
  if(!st || !Array.isArray(st.classes) || !st.classes.length) return;
  document.getElementById("structure-box").style.display="";
  if(st.diagram_title) document.getElementById("structure-title").textContent=st.diagram_title;

  const wrap = document.getElementById("uml-cards"); wrap.innerHTML="";
  st.classes.forEach(cls=>{
    const change = cls.change || "unchanged";
    const card = el("div",{class:"uml-card "+chgClass(change)});
    // header
    const head = el("div",{class:"uhead"});
    const nameWrap = el("div",{},
      cls.stereotype ? el("span",{class:"stereo",text:"«"+cls.stereotype+"»"}) : null,
      el("span",{class:"cname",text:cls.name||"?",title:"Jump to diff"}));
    head.append(nameWrap);
    if(CHG_LABEL[change]) head.append(el("span",{class:"ubadge "+chgClass(change),text:CHG_LABEL[change]}));
    card.append(head);
    // jump-to-diff on class name
    const cn = card.querySelector(".cname");
    cn.addEventListener("click",()=>jumpToClass(cls));
    // attributes compartment
    card.append(memberComp(cls.attributes, "No fields"));
    // methods compartment
    card.append(memberComp(cls.methods, "No methods"));
    // comment affordance
    const acts = el("div",{class:"uactions"},
      el("button",{class:"ghost",style:"font-size:11px;padding:3px 9px",
        onClick:()=>openClassComposer(cls, card),text:"+ Comment"}));
    card.append(acts);

    wrap.append(card);
    cardById[cls.name] = card;
  });

  drawRelations();
}

function memberComp(items, emptyLabel){
  const comp = el("div",{class:"uml-comp"});
  if(!Array.isArray(items) || !items.length){
    comp.append(el("div",{class:"none",text:emptyLabel}));
    return comp;
  }
  items.forEach(m=>{
    const c = (typeof m==="string") ? {name:m} : m;
    const change = c.change || "unchanged";
    comp.append(el("div",{class:"mrow "+chgClass(change)},
      el("span",{class:"gut"}),
      el("span",{class:"msig",text:c.name||""})));
  });
  return comp;
}

const REL = {
  extends:    {dash:false, end:"tri",   start:null,   label:"extends"},
  implements: {dash:true,  end:"tri",   start:null,   label:"implements"},
  uses:       {dash:true,  end:"arrow", start:null,   label:"uses"},
  depends:    {dash:true,  end:"arrow", start:null,   label:"depends"},
  composes:   {dash:false, end:"arrow", start:"diamondF", label:""},
  aggregates: {dash:false, end:"arrow", start:"diamondO", label:""}
};

function borderPoint(r, tx, ty){
  const cx=r.left+r.width/2, cy=r.top+r.height/2;
  const dx=tx-cx, dy=ty-cy;
  if(dx===0&&dy===0) return {x:cx,y:cy};
  const hw=r.width/2+2, hh=r.height/2+2;
  const scale = 1/Math.max(Math.abs(dx)/hw, Math.abs(dy)/hh);
  return {x:cx+dx*scale, y:cy+dy*scale};
}

function drawRelations(){
  const st = DATA.structure;
  const svg = document.getElementById("uml-edges");
  if(!svg || !st || !Array.isArray(st.relations)) { if(svg) svg.innerHTML=""; return; }
  const stage = svg.parentElement;
  const sb = stage.getBoundingClientRect();
  svg.setAttribute("width", sb.width);
  svg.setAttribute("height", sb.height);
  svg.innerHTML = EDGE_DEFS;

  st.relations.forEach(rel=>{
    const a = cardById[rel.from], b = cardById[rel.to];
    if(!a || !b) return;
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    const A = {left:ra.left-sb.left, top:ra.top-sb.top, width:ra.width, height:ra.height};
    const B = {left:rb.left-sb.left, top:rb.top-sb.top, width:rb.width, height:rb.height};
    const acx=A.left+A.width/2, acy=A.top+A.height/2, bcx=B.left+B.width/2, bcy=B.top+B.height/2;
    const p1 = borderPoint(A, bcx, bcy);
    const p2 = borderPoint(B, acx, acy);
    const spec = REL[rel.type] || REL.uses;
    const line = document.createElementNS("http://www.w3.org/2000/svg","line");
    line.setAttribute("x1",p1.x); line.setAttribute("y1",p1.y);
    line.setAttribute("x2",p2.x); line.setAttribute("y2",p2.y);
    if(spec.dash) line.setAttribute("stroke-dasharray","5 4");
    if(spec.end) line.setAttribute("marker-end","url(#"+spec.end+")");
    if(spec.start) line.setAttribute("marker-start","url(#"+spec.start+")");
    svg.appendChild(line);
    const lbl = (rel.label!=null ? rel.label : spec.label);
    if(lbl){
      const mx=(p1.x+p2.x)/2, my=(p1.y+p2.y)/2;
      const t = document.createElementNS("http://www.w3.org/2000/svg","text");
      t.setAttribute("x",mx); t.setAttribute("y",my-3);
      t.setAttribute("text-anchor","middle");
      t.textContent=lbl;
      svg.appendChild(t);
    }
  });
}

const EDGE_DEFS = ''
  + '<defs>'
  + '<marker id="tri" markerWidth="13" markerHeight="13" refX="11" refY="5" orient="auto">'
  + '<path class="mk-fill-surface mk-stroke" d="M1,1 L11,5 L1,9 Z"/></marker>'
  + '<marker id="arrow" markerWidth="11" markerHeight="11" refX="8" refY="4" orient="auto">'
  + '<path class="mk-fill-none mk-stroke" d="M1,1 L8,4 L1,7"/></marker>'
  + '<marker id="diamondF" markerWidth="16" markerHeight="11" refX="1" refY="5" orient="auto">'
  + '<path class="mk-fill-muted mk-stroke" d="M1,5 L7,1 L13,5 L7,9 Z"/></marker>'
  + '<marker id="diamondO" markerWidth="16" markerHeight="11" refX="1" refY="5" orient="auto">'
  + '<path class="mk-fill-surface mk-stroke" d="M1,5 L7,1 L13,5 L7,9 Z"/></marker>'
  + '</defs>';

function jumpToClass(cls){
  if(cls.file && cls.line!=null){
    const a = anchorFor(cls.file, "new", cls.line);
    if(a){
      const row = a.previousElementSibling;
      (row||a).scrollIntoView({behavior:"smooth",block:"center"});
      if(row){ row.style.transition="background .2s"; const orig=row.style.background;
        row.style.background="var(--accent-weak)"; setTimeout(()=>row.style.background=orig,1100); }
      return;
    }
  }
  // fall back to the file header
  const fileEl = cls.file ? document.getElementById(slug(cls.file)) : null;
  if(fileEl) fileEl.scrollIntoView({behavior:"smooth",block:"start"});
}

function openClassComposer(cls, card){
  let comp = card.querySelector(".class-composer");
  if(comp){ comp.querySelector("textarea").focus(); return; }
  const ta = el("textarea",{placeholder:"Architecture note on "+cls.name+" for the agent…"});
  comp = el("div",{class:"composer class-composer",style:"margin:0 8px 8px"}, ta,
    el("div",{class:"crow2"},
      el("button",{class:"ghost",onClick:()=>comp.remove(),text:"Cancel"}),
      el("button",{class:"primary",onClick:()=>{
        const v=ta.value.trim();
        if(v) structureComments.push({class:cls.name,file:cls.file||null,line:(cls.line!=null?cls.line:null),body:v,_id:++uid});
        comp.remove(); renderClassComments(card, cls);
      },text:"Add note"})));
  card.append(comp); ta.focus();
}

function renderClassComments(card, cls){
  card.querySelectorAll(".class-note").forEach(n=>n.remove());
  structureComments.filter(c=>c.class===cls.name).forEach(c=>{
    const note = el("div",{class:"card you class-note",style:"margin:0 8px 8px;font-size:12px"},
      el("div",{class:"crow"},
        el("span",{class:"pill",style:"background:var(--accent)",text:"You"}),
        el("span",{class:"ctitle",text:"on "+cls.name})),
      el("div",{class:"cbody",text:c.body}),
      el("div",{class:"cactions"},
        el("button",{class:"ghost danger",onClick:()=>{
          const i=structureComments.indexOf(c); if(i>=0) structureComments.splice(i,1);
          renderClassComments(card, cls); updateTally();
        },text:"Remove"})));
    card.append(note);
  });
  updateTally();
  drawRelations();
}

// ---- diff + anchors ----
const anchorMap = {};   // "file||side:line" -> anchor element
function renderFiles(){
  const root=document.getElementById("files"); root.innerHTML="";
  for(const k in anchorMap) delete anchorMap[k];
  (DATA.files||[]).forEach(f=>{
    const file=el("div",{class:"file",id:slug(f.path)});
    file.append(el("div",{class:"fhead"},
      el("span",{class:"tag "+(f.status||"modified"),text:f.status||"modified"}),
      el("span",{class:"path",text:f.path}),
      el("span",{class:"nums"},
        el("span",{class:"stat-add",text:"+"+f.additions})," ",
        el("span",{class:"stat-del",text:"−"+f.deletions}))));
    if(f.binary){ file.append(el("div",{class:"empty",style:"padding:14px",text:"Binary file — not shown."})); root.append(file); return; }
    (f.hunks||[]).forEach(h=>{
      file.append(el("div",{class:"hunk-hd",text:h.header}));
      const diff=el("div",{class:"diff"});
      (h.lines||[]).forEach(L=>{
        const row=el("div",{class:"ln "+L.type});
        row.append(el("span",{class:"g",text:L.old==null?"":String(L.old)}));
        row.append(el("span",{class:"g",text:L.new==null?"":String(L.new)}));
        const side = L.type==="del" ? "old" : "new";
        const lineNo = side==="old" ? L.old : L.new;
        const plus = el("span",{class:"plus",title:"Add a comment",text:"+"});
        if(lineNo!=null) plus.addEventListener("click",()=>openComposer(f.path,lineNo,side,anchor));
        row.append(plus);
        row.append(el("span",{class:"code",text:L.text}));
        diff.append(row);
        const anchor=el("div",{class:"anchor"});
        if(lineNo!=null) anchorMap[f.path+"||"+akey(side,lineNo)] = anchor;
        diff.append(anchor);
      });
      file.append(diff);
    });
    root.append(file);
  });
  renderAnchors();
}

function anchorFor(file, side, line){
  return anchorMap[file+"||"+akey(side,line)] || null;
}

function renderAnchors(){
  document.querySelectorAll(".anchor").forEach(a=>a.innerHTML="");
  (DATA.findings||[]).forEach(f=>{
    const side=f.side==="old"?"old":"new";
    const a=anchorFor(f.file, side, f.line);
    if(a) a.append(findingCard(f));
  });
  added.forEach(c=>{
    const a=anchorFor(c.file, c.side, c.line);
    if(a) a.append(addedCard(c));
  });
  updateTally();
}

function findingCard(f){
  const st = fstate[f.id] || (fstate[f.id]={decision:"include",body:null});
  const dismissed = st.decision==="dismiss";
  const body = st.body!=null ? st.body : (f.body||"");
  const card=el("div",{class:"card "+sevClass(f.severity)+(dismissed?" dismissed":"")});
  const row=el("div",{class:"crow"},
    el("span",{class:"pill "+sevClass(f.severity),text:SEV_LABEL[f.severity]||f.severity||"note"}),
    el("span",{class:"ctitle",text:f.title||"Comment"}));
  if(st.body!=null && !dismissed) row.append(el("span",{class:"edited-tag",text:"edited"}));
  card.append(row);
  card.append(el("div",{class:"cbody",text:body}));
  if(f.suggestion) card.append(el("div",{class:"csugg",text:f.suggestion}));

  const acts=el("div",{class:"cactions"});
  if(dismissed){
    acts.append(el("button",{class:"ghost",onClick:()=>{st.decision="include";renderAnchors();},text:"Restore"}));
  }else{
    acts.append(el("button",{class:"ghost",onClick:()=>editFinding(card,f,st),text:"Edit"}));
    acts.append(el("button",{class:"ghost danger",onClick:()=>{st.decision="dismiss";renderAnchors();},text:"Dismiss"}));
  }
  card.append(acts);
  return card;
}

function editFinding(card,f,st){
  const cur = st.body!=null ? st.body : (f.body||"");
  const ta=el("textarea",{class:"cbody-edit"}); ta.value=cur;
  const bodyEl=card.querySelector(".cbody");
  bodyEl.replaceWith(ta); ta.focus();
  const acts=card.querySelector(".cactions"); acts.innerHTML="";
  acts.append(
    el("button",{class:"primary",onClick:()=>{
      const v=ta.value.trim();
      st.body = (v===(f.body||"").trim() || v==="") ? (v===""?"":v) : v;
      if(v==="") { st.body=null; }      // empty -> revert to original
      else st.body=v;
      renderAnchors();
    },text:"Save"}),
    el("button",{class:"ghost",onClick:()=>renderAnchors(),text:"Cancel"}));
}

function addedCard(c){
  const card=el("div",{class:"card you"});
  card.append(el("div",{class:"crow"},
    el("span",{class:"pill",style:"background:var(--accent)",text:"You"}),
    el("span",{class:"ctitle",text:"Comment on line "+c.line})));
  card.append(el("div",{class:"cbody",text:c.body}));
  card.append(el("div",{class:"cactions"},
    el("button",{class:"ghost",onClick:()=>editAdded(card,c),text:"Edit"}),
    el("button",{class:"ghost danger",onClick:()=>{
      const i=added.indexOf(c); if(i>=0) added.splice(i,1); renderAnchors();
    },text:"Remove"})));
  return card;
}
function editAdded(card,c){
  const ta=el("textarea",{class:"cbody-edit"}); ta.value=c.body;
  card.querySelector(".cbody").replaceWith(ta); ta.focus();
  const acts=card.querySelector(".cactions"); acts.innerHTML="";
  acts.append(
    el("button",{class:"primary",onClick:()=>{const v=ta.value.trim(); if(v){c.body=v;} renderAnchors();},text:"Save"}),
    el("button",{class:"ghost",onClick:()=>renderAnchors(),text:"Cancel"}));
}

// composer for new human comments
let openC=null;
function openComposer(file,line,side,anchor){
  if(openC){ openC.remove(); openC=null; }
  const a=anchorFor(file,side,line); if(!a) return;
  const ta=el("textarea",{placeholder:"Leave a comment for the agent on this line…"});
  const box=el("div",{class:"composer"}, ta,
    el("div",{class:"crow2"},
      el("button",{class:"ghost",onClick:()=>{box.remove();openC=null;},text:"Cancel"}),
      el("button",{class:"primary",onClick:()=>{
        const v=ta.value.trim();
        if(v){ added.push({file,line,side,body:v,_id:++uid}); }
        box.remove(); openC=null; renderAnchors();
      },text:"Comment"})));
  a.append(box); openC=box; ta.focus();
}

// ---- review bar ----
function updateTally(){
  const findings=DATA.findings||[];
  let kept=0,edited=0,dismissed=0;
  findings.forEach(f=>{
    const st=fstate[f.id]||{decision:"include"};
    if(st.decision==="dismiss") dismissed++;
    else { kept++; if(st.body!=null) edited++; }
  });
  const t=document.getElementById("tally"); t.innerHTML="";
  const mine = added.length + structureComments.length;
  t.append(el("span",{},el("b",{text:String(kept+mine)})," comments"));
  if(edited) t.append(el("span",{},el("b",{text:String(edited)})," edited"));
  if(mine) t.append(el("span",{},el("b",{text:String(mine)})," yours"));
  if(dismissed) t.append(el("span",{},el("b",{text:String(dismissed)})," dismissed"));
}

function setVerdict(v){
  verdict=v;
  document.querySelectorAll("#verdict button").forEach(b=>b.classList.toggle("on",b.dataset.v===v));
}

function collect(){
  const findings=(DATA.findings||[]).map(f=>{
    const st=fstate[f.id]||{decision:"include",body:null};
    if(st.decision==="dismiss") return {id:f.id,decision:"dismiss"};
    if(st.body!=null) return {id:f.id,decision:"edit",body:st.body};
    return {id:f.id,decision:"accept",body:f.body||""};
  });
  return {
    status:"submitted",
    verdict,
    notes:document.getElementById("notes").value.trim(),
    findings,
    added_comments:added.map(c=>({file:c.file,line:c.line,side:c.side,body:c.body})),
    structure_comments:structureComments.map(c=>({class:c.class,file:c.file,line:c.line,body:c.body}))
  };
}

async function send(){
  const payload=collect();
  if(DEMO){
    const box=document.querySelector("#overlay .box");
    box.innerHTML="";
    box.append(el("div",{class:"check",text:"⤴"}));
    box.append(el("h2",{text:"This is what gets sent back"}));
    box.append(el("p",{text:"In the real tool this JSON is returned to the agent on stdout. (Demo — nothing was sent.)"}));
    const pre=el("pre",{style:"text-align:left;font-family:var(--mono);font-size:11px;max-height:320px;overflow:auto;background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:10px;margin-top:12px;white-space:pre-wrap"});
    pre.textContent=JSON.stringify(payload,null,2);
    box.append(pre);
    box.append(el("button",{class:"ghost",style:"margin-top:12px",onClick:()=>document.getElementById("overlay").classList.remove("show"),text:"Close"}));
    document.getElementById("overlay").classList.add("show");
    return;
  }
  const btn=document.getElementById("send"); btn.disabled=true; btn.textContent="Sending…";
  try{
    await fetch("/api/submit",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    document.getElementById("overlay").classList.add("show");
  }catch(e){
    btn.disabled=false; btn.textContent="Send review to agent";
    alert("Could not reach loupe. Is the CLI still running?\n\n"+e);
  }
}

// ---- init ----
renderHeader(); renderNav(); renderSummary(); renderDiagrams(); renderStructure(); renderFiles();
document.querySelectorAll("#verdict button").forEach(b=>b.addEventListener("click",()=>setVerdict(b.dataset.v)));
document.getElementById("notes-toggle").addEventListener("click",()=>document.getElementById("bar-notes").classList.toggle("show"));
document.getElementById("send").addEventListener("click",send);
window.addEventListener("keydown",e=>{ if((e.metaKey||e.ctrlKey)&&e.key==="Enter") send(); });
let _redraw; window.addEventListener("resize",()=>{ clearTimeout(_redraw); _redraw=setTimeout(drawRelations,120); });
if(window.ResizeObserver){ const ro=new ResizeObserver(()=>drawRelations()); const s=document.querySelector(".uml-stage"); if(s) ro.observe(s); }
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    html = ""

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, Handler.html, "text/html; charset=utf-8")
        elif self.path == "/api/health":
            self._send(200, json.dumps({"ok": True}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path in ("/api/submit", "/api/cancel"):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except Exception as e:
                self._send(400, json.dumps({"error": "bad json: %s" % e}))
                return
            if self.path == "/api/cancel":
                payload = {"status": "cancelled"}
            RESULT["value"] = payload
            self._send(200, json.dumps({"ok": True}))
            DONE.set()
        else:
            self._send(404, json.dumps({"error": "not found"}))


def find_open_port(start):
    for p in range(start, start + 60):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def main():
    ap = argparse.ArgumentParser(description="Interactive PR-review surface for coding agents.")
    ap.add_argument("review", help="path to review JSON (PR metadata + diff + findings)")
    ap.add_argument("--port", type=int, default=7842, help="preferred port (default 7842)")
    ap.add_argument("--no-open", action="store_true", help="do not auto-open the browser")
    ap.add_argument("--timeout", type=int, default=0, help="seconds to wait for feedback; 0 = forever")
    args = ap.parse_args()

    try:
        with open(args.review, "r", encoding="utf-8") as f:
            review = json.load(f)
    except FileNotFoundError:
        print(json.dumps({"status": "error", "error": "review file not found: %s" % args.review}))
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(json.dumps({"status": "error", "error": "invalid review JSON: %s" % e}))
        sys.exit(1)

    review["_files"] = parse_diff(review.get("diff", ""))
    Handler.html = build_html(review)

    port = find_open_port(args.port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % port

    nf = len(review["_files"])
    nfind = len(review.get("findings", []) or [])
    print("[loupe] serving review UI: %s" % url, file=sys.stderr)
    print("[loupe] %d file(s), %d finding(s) — waiting for you to send the review back…"
          % (nf, nfind), file=sys.stderr)
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        got = DONE.wait(timeout=None if args.timeout == 0 else args.timeout)
    except KeyboardInterrupt:
        got = False
        print("\n[loupe] interrupted", file=sys.stderr)

    httpd.shutdown()

    if not got:
        print(json.dumps({"status": "timeout"}))
        sys.exit(2)
    result = RESULT["value"] or {}
    if result.get("status") == "cancelled":
        print(json.dumps({"status": "cancelled"}))
        sys.exit(3)
    print(json.dumps(result, indent=2))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
