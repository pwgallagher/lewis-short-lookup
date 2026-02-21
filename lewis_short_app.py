#!/usr/bin/env python3
"""
Lewis & Short Latin Dictionary — local lookup app
==================================================
Run:   python3 lewis_short_app.py
Open:  http://localhost:5050

Search priority (per query):
  1. Prefix / headword matches   — instant, shown unlabelled
  2. Full-text matches           — "Found in:" section, ranked by occurrence count
  3. Fuzzy headword matches      — "Similar:" fallback when 1 & 2 both empty
"""

import re
import sys
import bisect
import difflib
import pickle
import unicodedata
import html as html_mod
from collections import Counter
from pathlib import Path
from flask import Flask, request, jsonify, Response

# ── Configuration ─────────────────────────────────────────────────────────────
DICT_FILE    = Path(__file__).parent / "lewis-short-smart-quotes.txt"
PICKLE_FILE  = Path(__file__).parent / "lewis-short-wordindex.pkl"
PORT         = 5050
MAX_PREFIX   = 25
MAX_FULLTEXT = 6
MAX_FUZZY    = 8
FUZZY_CUTOFF = 0.62

# ── Normalisation ──────────────────────────────────────────────────────────────
def normalise(s: str) -> str:
    """Strip combining diacritics (macrons, breves, …), remove hyphens, lowercase.
    Used for headword keys and for the fulltext query."""
    s = s.replace("-", "")
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()

HW_RE = re.compile(r"^([^\s,;:(]+)")

# ── Load dictionary ────────────────────────────────────────────────────────────
print(f"Loading {DICT_FILE.name} …", end="  ", flush=True, file=sys.stderr)
LINES = DICT_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
print(f"{len(LINES):,} lines.", file=sys.stderr)

# ── Headword index (prefix search) ────────────────────────────────────────────
print("Building headword index …", end="  ", flush=True, file=sys.stderr)
_pairs: list[tuple[str, str, int]] = []  # (norm_key, raw_headword, line_idx)
for i, line in enumerate(LINES):
    m = HW_RE.match(line.lstrip())
    if m:
        raw = m.group(1)
        _pairs.append((normalise(raw), raw, i))

_pairs.sort()
NORM_KEYS = [p[0] for p in _pairs]   # sorted normalised keys  (for bisect)
RAW_HEADS = [p[1] for p in _pairs]   # parallel raw headwords  (with macrons)
LINE_IDXS = [p[2] for p in _pairs]   # parallel line indices
print(f"{len(_pairs):,} entries.", file=sys.stderr)

# ── Word index (fulltext search) ───────────────────────────────────────────────
def _build_word_index() -> dict:
    """
    For every entry line, tokenise the diacritic-stripped text into alpha words
    and count their occurrences.  Returns:
        word -> list of (count, line_idx), sorted by count descending.
    Saved/loaded as a pickle so it is only built once (~4 s first run, <1 s after).
    """
    print("Building word index (first run — this takes ~4 s) …",
          end="  ", flush=True, file=sys.stderr)
    index: dict[str, list[tuple[int, int]]] = {}
    for i, line in enumerate(LINES):
        if not HW_RE.match(line.lstrip()):   # skip blank / non-entry lines
            continue
        nfd  = unicodedata.normalize("NFD", line)
        norm = "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()
        for word, cnt in Counter(re.findall(r"[a-z]+", norm)).items():
            if word not in index:
                index[word] = []
            index[word].append((cnt, i))
    for lst in index.values():
        lst.sort(reverse=True)
    print(f"{len(index):,} unique words.", file=sys.stderr)
    return index

def _load_word_index() -> dict:
    if PICKLE_FILE.exists():
        print(f"Loading word index from cache …", end="  ", flush=True, file=sys.stderr)
        with open(PICKLE_FILE, "rb") as f:
            idx = pickle.load(f)
        print(f"{len(idx):,} words.", file=sys.stderr)
        return idx
    idx = _build_word_index()
    print(f"Saving word index cache …", end="  ", flush=True, file=sys.stderr)
    with open(PICKLE_FILE, "wb") as f:
        pickle.dump(idx, f, protocol=pickle.HIGHEST_PROTOCOL)
    print("done.", file=sys.stderr)
    return idx

WORD_INDEX = _load_word_index()

# ── Search: prefix ─────────────────────────────────────────────────────────────
def prefix_search(q_norm: str, limit: int = MAX_PREFIX) -> list[dict]:
    lo  = bisect.bisect_left(NORM_KEYS, q_norm)
    out = []
    for i in range(lo, len(NORM_KEYS)):
        if not NORM_KEYS[i].startswith(q_norm):
            break
        out.append({"norm": NORM_KEYS[i], "raw": RAW_HEADS[i], "line": LINE_IDXS[i]})
        if len(out) >= limit:
            break
    return out

# ── Search: fulltext ───────────────────────────────────────────────────────────
def fulltext_search(q_norm: str, exclude_lines: set,
                    limit: int = MAX_FULLTEXT) -> list[dict]:
    """
    Look up q_norm in the pre-built word index.
    Returns up to `limit` entries (ranked by occurrence count) whose line index
    is not already in `exclude_lines` (so prefix hits are not duplicated).
    Each result dict carries a `count` field for display as a badge.
    """
    hits = WORD_INDEX.get(q_norm, [])
    out  = []
    for count, line_idx in hits:
        if line_idx in exclude_lines:
            continue
        m = HW_RE.match(LINES[line_idx].lstrip())
        if m:
            out.append({
                "norm":  normalise(m.group(1)),
                "raw":   m.group(1),
                "line":  line_idx,
                "count": count,
            })
        if len(out) >= limit:
            break
    return out

# ── Search: fuzzy ──────────────────────────────────────────────────────────────
def fuzzy_search(q_norm: str, limit: int = MAX_FUZZY) -> list[dict]:
    matches = difflib.get_close_matches(
        q_norm, NORM_KEYS, n=limit, cutoff=FUZZY_CUTOFF
    )
    out = []
    for m in matches:
        idx = bisect.bisect_left(NORM_KEYS, m)
        while idx < len(NORM_KEYS) and NORM_KEYS[idx] == m:
            out.append({"norm": NORM_KEYS[idx], "raw": RAW_HEADS[idx], "line": LINE_IDXS[idx]})
            idx += 1
    return out[:limit]

# ── Entry rendering ────────────────────────────────────────────────────────────
def render_entry(line_idx: int) -> str:
    """Return HTML for the full dictionary entry at line_idx."""
    line = LINES[line_idx].rstrip()
    m = HW_RE.match(line.lstrip())
    if not m:
        return f"<span>{html_mod.escape(line)}</span>"

    hw_html   = f'<span class="hw">{html_mod.escape(m.group(1))}</span>'
    rest_html = html_mod.escape(line[m.end():])

    # Italic for bracketed etymologies
    rest_html = re.sub(
        r"\[([^\]]+)\]",
        lambda mo: f"<em>[{mo.group(1)}]</em>",
        rest_html,
    )
    # Distinctive styling for common author abbreviations
    AUTHOR_RE = re.compile(
        r"\b(Cic|Verg|Hor|Ov|Liv|Tac|Plaut|Ter|Caes|Sall|Quint|Plin|"
        r"Juv|Luc|Mart|Suet|Varro|Lucr|Cat|Sen|Gell|Prop|Tib|Stat)\."
    )
    rest_html = AUTHOR_RE.sub(
        lambda mo: f'<cite>{mo.group(0)}</cite>', rest_html
    )

    return hw_html + rest_html

# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"prefix": [], "fulltext": [], "fuzzy": [],
                        "entry": None, "entry_line": None})

    q_norm = normalise(q)

    # 1. Prefix / headword matches
    prefix = prefix_search(q_norm)

    # 2. Fulltext matches (excluding lines already found by prefix)
    prefix_lines = {r["line"] for r in prefix}
    fulltext = fulltext_search(q_norm, exclude_lines=prefix_lines)

    # 3. Fuzzy fallback — only when both above are empty
    fuzzy = []
    if not prefix and not fulltext:
        fuzzy = fuzzy_search(q_norm)

    # Choose entry to auto-load: first prefix → first fulltext → first fuzzy
    first = (prefix or fulltext or fuzzy or [None])[0]
    entry_html  = render_entry(first["line"]) if first else None
    entry_line  = first["line"]               if first else None

    return jsonify({
        "prefix":   prefix,
        "fulltext": fulltext,
        "fuzzy":    fuzzy,
        "entry":    entry_html,
        "entry_line": entry_line,
    })

@app.route("/api/entry")
def api_entry():
    try:
        line = int(request.args.get("line", -1))
        if line < 0 or line >= len(LINES):
            raise ValueError
    except ValueError:
        return jsonify({"entry": "<em>Invalid line number.</em>"})
    return jsonify({"entry": render_entry(line)})

# ── HTML page ──────────────────────────────────────────────────────────────────
PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lewis &amp; Short — Latin Dictionary</title>
<style>
/* ── Reset & base ─────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 16px; scroll-behavior: smooth; }
body {
  font-family: Georgia, "Times New Roman", Times, serif;
  background: #faf8f2;
  color: #1c1208;
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
}

/* ── Header ───────────────────────────────────────────────────────── */
#header {
  flex-shrink: 0;
  background: #2b1a0d;
  color: #f5e8cc;
  padding: .7rem 1.6rem;
  display: flex;
  align-items: center;
  gap: 1.4rem;
}
#header h1 { font-size: 1.15rem; letter-spacing: .05em; white-space: nowrap; }
#header small { font-size: .78rem; opacity: .65; font-style: italic; white-space: nowrap; }
#header-sig {
  margin-left: auto;
  font-size: .72rem;
  font-style: italic;
  opacity: .6;
  white-space: nowrap;
  letter-spacing: .02em;
}
#search-wrap { flex: 1; max-width: 520px; }
#q {
  width: 100%;
  font-family: Georgia, serif;
  font-size: 1.05rem;
  padding: .38rem .7rem;
  border: 1.5px solid #8c6040;
  border-radius: 4px;
  background: #fffef8;
  color: #1c1208;
  outline: none;
}
#q:focus { border-color: #d4a060; box-shadow: 0 0 0 2px #4a3010; }
#q::placeholder { color: #b09070; }

/* ── Matches bar ──────────────────────────────────────────────────── */
#matches-bar {
  flex-shrink: 0;
  background: #ede6d4;
  border-bottom: 1px solid #d4c8a8;
  padding: .45rem 1.6rem;
  display: flex;
  flex-wrap: wrap;
  gap: .3rem;
  align-items: center;
  min-height: 2.6rem;
}

/* section divider inside the bar */
.bar-sep {
  width: 1px;
  height: 1.2em;
  background: #c0a880;
  margin: 0 .25rem;
  align-self: center;
  flex-shrink: 0;
}
.bar-label {
  font-family: sans-serif;
  font-size: .72rem;
  font-style: italic;
  color: #7a5030;
  align-self: center;
  white-space: nowrap;
  margin-right: .1rem;
}

/* headword / prefix buttons */
.mbtn {
  font-family: Georgia, serif;
  font-size: .9rem;
  padding: .18rem .55rem;
  border: 1px solid #b09070;
  border-radius: 3px;
  background: #fffef8;
  color: #2b1a0d;
  cursor: pointer;
  transition: background .12s, color .12s;
  display: inline-flex;
  align-items: baseline;
  gap: .25rem;
}
.mbtn:hover  { background: #e0cfa8; }
.mbtn.active { background: #2b1a0d; color: #f5e8cc; border-color: #1a0e06; }
.mbtn.active:hover { background: #3c2618; }

/* fulltext buttons — slightly different border colour to distinguish */
.mbtn-ft {
  border-color: #7a9060;
  color: #1a2808;
}
.mbtn-ft:hover  { background: #d8e4c0; }
.mbtn-ft.active { background: #2a380d; border-color: #1a2808; }
.mbtn-ft.active:hover { background: #3a4818; }

/* occurrence-count badge on fulltext buttons */
.ft-count {
  font-family: sans-serif;
  font-size: .65rem;
  font-style: normal;
  opacity: .7;
  letter-spacing: 0;
}
.mbtn.active .ft-count { opacity: .85; }

.fuzzy-tag, .no-match {
  font-family: sans-serif;
  font-size: .8rem;
  font-style: italic;
  color: #9a7050;
}

/* ── Entry area ───────────────────────────────────────────────────── */
#entry-area {
  flex: 1;
  overflow-y: auto;
  padding: 1.4rem 2.2rem 2.5rem;
}
#entry-area::-webkit-scrollbar { width: 8px; }
#entry-area::-webkit-scrollbar-thumb { background: #c8b890; border-radius: 4px; }
#entry-area::-webkit-scrollbar-track { background: #f0e8d4; }

#entry {
  font-family: Georgia, "Times New Roman", Times, serif;
  font-size: 1rem;
  line-height: 1.85;
  color: #1c1208;
  max-width: 860px;
}
#entry .hw {
  font-size: 1.4rem;
  font-weight: bold;
  color: #5a2800;
  letter-spacing: .03em;
}
#entry cite { font-style: normal; font-size: .88em; color: #5a4025; letter-spacing: .02em; }
#entry em   { font-style: italic; }

.placeholder {
  color: #b09070;
  font-style: italic;
  font-family: sans-serif;
  font-size: .9rem;
  padding-top: .5rem;
}
.spinner {
  display: none;
  font-family: sans-serif;
  font-size: .85rem;
  color: #9a7050;
  font-style: italic;
}

/* ── Footer ───────────────────────────────────────────────────────── */
#footer {
  flex-shrink: 0;
  background: #ede6d4;
  border-top: 1px solid #d4c8a8;
  padding: .28rem 1.6rem;
  font-family: sans-serif;
  font-size: .7rem;
  color: #9a7050;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
#copy-btn {
  font-family: sans-serif;
  font-size: .7rem;
  padding: .15rem .5rem;
  background: transparent;
  border: 1px solid #b09070;
  border-radius: 3px;
  color: #7a5030;
  cursor: pointer;
  display: none;
}
#copy-btn:hover { background: #e0cfa8; }
</style>
</head>
<body>

<div id="header">
  <h1>Lewis &amp;&nbsp;Short</h1>
  <small>A Latin Dictionary (1879)</small>
  <div id="search-wrap">
    <input id="q" type="text"
           placeholder="Latin headword, inflected form, or English word (e.g. texit, amor, exile) …"
           autocomplete="off" spellcheck="false" autocorrect="off" autocapitalize="none">
  </div>
  <span id="header-sig">Claudius fecit &nbsp;·&nbsp; Feb. MMXXVI</span>
</div>

<div id="matches-bar">
  <span class="placeholder">Type a word to begin.</span>
</div>

<div id="entry-area">
  <div class="spinner" id="spinner">Searching…</div>
  <div id="entry">
    <p class="placeholder">
      Results will appear here.<br>
      Headwords and inflected forms are both supported — diacritics ignored.<br>
      <em>texit</em> → finds <em>tĕgo</em> &nbsp;·&nbsp;
      <em>consulem</em> → finds <em>consul</em> &nbsp;·&nbsp;
      <em>amor</em> → finds <em>ămor</em> directly.
    </p>
  </div>
</div>

<div id="footer">
  <span>Lewis &amp; Short, <em>A Latin Dictionary</em> (Oxford, 1879). Original text public domain.
  Digital edition by <a href="https://josephholsten.com" target="_blank"
  style="color:#9a7050;text-decoration:underline dotted;">Joseph Holsten</a>
  (<a href="https://creativecommons.org/licenses/by-sa/3.0/" target="_blank"
  style="color:#9a7050;text-decoration:underline dotted;">CC BY-SA 3.0</a>).
  Prefix · full-text · fuzzy.</span>
  <button id="copy-btn" onclick="copyEntry()">Copy entry</button>
</div>

<script>
"use strict";
const qEl      = document.getElementById("q");
const matchBar = document.getElementById("matches-bar");
const entryEl  = document.getElementById("entry");
const spinner  = document.getElementById("spinner");
const copyBtn  = document.getElementById("copy-btn");

let debounce   = null;
let activeLine = null;

// ── Input handlers ────────────────────────────────────────────────────────────
qEl.addEventListener("input", () => {
  clearTimeout(debounce);
  debounce = setTimeout(doSearch, 230);
});
qEl.addEventListener("keydown", e => {
  if (e.key === "Enter")  { clearTimeout(debounce); doSearch(); }
  if (e.key === "Escape") { clearQ(); }
});

function clearQ() {
  qEl.value = "";
  matchBar.innerHTML = '<span class="placeholder">Type a word to begin.</span>';
  entryEl.innerHTML  = '<p class="placeholder">Results will appear here.</p>';
  copyBtn.style.display = "none";
  activeLine = null;
  qEl.focus();
}

// ── Search ────────────────────────────────────────────────────────────────────
async function doSearch() {
  const q = qEl.value.trim();
  if (q.length < 2) {
    matchBar.innerHTML = '<span class="placeholder">Keep typing…</span>';
    entryEl.innerHTML  = "";
    copyBtn.style.display = "none";
    return;
  }
  spinner.style.display = "block";
  try {
    const res  = await fetch("/api/search?q=" + encodeURIComponent(q));
    const data = await res.json();
    spinner.style.display = "none";
    renderResults(data);
  } catch (err) {
    spinner.style.display = "none";
    entryEl.innerHTML = "<em>Server error — is the app still running?</em>";
  }
}

// ── Render result buttons ─────────────────────────────────────────────────────
function makeBtn(r, isFirst, isFt) {
  const btn = document.createElement("button");
  btn.className = "mbtn" + (isFt ? " mbtn-ft" : "") + (isFirst ? " active" : "");
  btn.dataset.line = r.line;

  // headword text
  const nameSpan = document.createElement("span");
  nameSpan.textContent = r.raw;
  btn.appendChild(nameSpan);

  // occurrence-count badge for fulltext results
  if (isFt && r.count) {
    const badge = document.createElement("span");
    badge.className = "ft-count";
    badge.textContent = r.count + "\u00d7";   // "2×"
    btn.appendChild(badge);
  }

  btn.addEventListener("click", () => loadEntry(r.line, btn));
  return btn;
}

function renderResults(data) {
  matchBar.innerHTML = "";

  const hasPrefix   = data.prefix   && data.prefix.length   > 0;
  const hasFulltext = data.fulltext && data.fulltext.length  > 0;
  const hasFuzzy    = data.fuzzy    && data.fuzzy.length     > 0;

  if (!hasPrefix && !hasFulltext && !hasFuzzy) {
    matchBar.innerHTML = '<span class="no-match">No matches found.</span>';
    entryEl.innerHTML  = "";
    copyBtn.style.display = "none";
    return;
  }

  // Track which button gets the "active" (first auto-loaded) state
  let firstBtn = null;

  // ── Section 1: prefix / headword matches (unlabelled) ───────────────────
  if (hasPrefix) {
    data.prefix.forEach((r, i) => {
      const isFirst = (i === 0 && !firstBtn);
      const btn = makeBtn(r, isFirst, false);
      if (isFirst) firstBtn = btn;
      matchBar.appendChild(btn);
    });
  }

  // ── Section 2: full-text matches ─────────────────────────────────────────
  if (hasFulltext) {
    if (hasPrefix) {
      const sep = document.createElement("span");
      sep.className = "bar-sep";
      matchBar.appendChild(sep);
    }
    const lbl = document.createElement("span");
    lbl.className   = "bar-label";
    lbl.textContent = hasPrefix ? "Found in:" : "Found in:";
    matchBar.appendChild(lbl);

    data.fulltext.forEach((r) => {
      const isFirst = !firstBtn;
      const btn = makeBtn(r, isFirst, true);
      if (isFirst) firstBtn = btn;
      matchBar.appendChild(btn);
    });
  }

  // ── Section 3: fuzzy fallback ─────────────────────────────────────────────
  if (hasFuzzy) {
    const lbl = document.createElement("span");
    lbl.className   = "bar-label fuzzy-tag";
    lbl.textContent = "Similar:";
    matchBar.appendChild(lbl);

    data.fuzzy.forEach((r) => {
      const isFirst = !firstBtn;
      const btn = makeBtn(r, isFirst, false);
      if (isFirst) firstBtn = btn;
      matchBar.appendChild(btn);
    });
  }

  // Auto-load the first result's entry
  if (data.entry) {
    entryEl.innerHTML     = data.entry;
    activeLine            = data.entry_line;
    copyBtn.style.display = "block";
    document.getElementById("entry-area").scrollTop = 0;
  }
}

// ── Load entry on button click ────────────────────────────────────────────────
async function loadEntry(line, btn) {
  if (line === activeLine) return;
  document.querySelectorAll(".mbtn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  spinner.style.display = "block";
  try {
    const res  = await fetch("/api/entry?line=" + line);
    const data = await res.json();
    spinner.style.display = "none";
    entryEl.innerHTML     = data.entry;
    activeLine            = line;
    copyBtn.style.display = "block";
    document.getElementById("entry-area").scrollTop = 0;
  } catch (err) {
    spinner.style.display = "none";
  }
}

// ── Copy entry text ───────────────────────────────────────────────────────────
function copyEntry() {
  navigator.clipboard.writeText(entryEl.innerText).then(() => {
    copyBtn.textContent = "Copied!";
    setTimeout(() => { copyBtn.textContent = "Copy entry"; }, 1800);
  });
}

window.addEventListener("load", () => qEl.focus());
</script>
</body>
</html>
"""

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n  ✦ Lewis & Short lookup running at  http://localhost:{PORT}", file=sys.stderr)
    print(  "    Press Ctrl-C to quit.\n", file=sys.stderr)
    app.run(host="127.0.0.1", port=PORT, debug=False)
