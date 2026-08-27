"""Annotation tool for the retention set (EVAL-PIPELINE, EVAL-ANNOTATION-FORMAT).

A self-contained HTML file per held-out document, not a hosted page: it embeds the document's
Markdown and the seed's classes, and the corpus never leaves the machine.

The offsets it produces index the parser's Markdown, character for character, because that is
what the mention layer anchors on and what `markdown_hash` validates. So the text is embedded
verbatim and selections are measured against it, rather than against a rendered view whose
whitespace would not correspond.

The field that justifies a bespoke format is `in_seed`, and the tool sets it structurally
rather than asking: choosing a class from the seed list sets it true, typing a name that is not
in the seed sets it false. That is the distinction the false-orphan metric rests on, and it is
too easy to get wrong if it is a checkbox.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, SKOS


@dataclass
class SeedClass:
    iri: str
    label: str
    gloss: str = ""


def seed_classes(graph: Graph) -> list[SeedClass]:
    classes = []
    for subject in graph.subjects(RDF.type, OWL.Class):
        if not isinstance(subject, URIRef):
            continue
        label = next((str(o) for o in graph.objects(subject, SKOS.prefLabel)), None)
        if not label:
            continue
        gloss = next(
            (str(o) for o in graph.objects(subject, SKOS.definition)
             if getattr(o, "language", None) == "en"),
            "",
        )
        classes.append(SeedClass(iri=str(subject), label=label, gloss=gloss))
    return sorted(classes, key=lambda item: item.label.lower())


def page_index(blocks: list[dict]) -> list[list[int]]:
    """[start, end, page] per block, so a selection's page comes from its offset."""
    return [
        [block["span_start"], block["span_end"], block["page"]]
        for block in blocks
        if block["span_start"] is not None
    ]


def build(
    *,
    doc_id: str,
    markdown: str,
    markdown_hash: str,
    classes: list[SeedClass],
    pages: list[list[int]],
    target: Path,
) -> Path:
    payload = {
        "doc_id": doc_id,
        "markdown_hash": markdown_hash,
        "classes": [{"iri": c.iri, "label": c.label, "gloss": c.gloss} for c in classes],
        "pages": pages,
    }
    html = _TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    html = html.replace("__TEXT__", _escape(markdown))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return target


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_TEMPLATE = r"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>Anotación · conjunto de retención</title>
<style>
:root { color-scheme: light dark; --line:#8884; }
* { box-sizing: border-box; }
body { font: 14px/1.6 system-ui, sans-serif; margin:0; height:100vh; display:flex;
       flex-direction:column; }
header { padding:10px 16px; border-bottom:1px solid var(--line); display:flex; gap:16px;
         align-items:center; flex-wrap:wrap; }
header h1 { font-size:14px; margin:0; font-weight:600; }
header .meta { font-size:12px; color:#888; font-family:ui-monospace,monospace; }
button { font:inherit; padding:5px 12px; border:1px solid var(--line); border-radius:6px;
         background:transparent; color:inherit; cursor:pointer; }
button.primary { background:#2563eb; color:#fff; border-color:#2563eb; }
button:disabled { opacity:.4; cursor:default; }
main { flex:1; display:grid; grid-template-columns: minmax(0,1fr) 340px; overflow:hidden; }
#text { overflow:auto; padding:20px 24px; white-space:pre-wrap; word-break:break-word;
        font-size:14px; font-family:ui-monospace,monospace; }
aside { border-left:1px solid var(--line); overflow:auto; padding:16px; }
mark { padding:1px 0; border-radius:2px; cursor:pointer; }
mark.in-seed { background:#22863a44; box-shadow:inset 0 -2px 0 #22863a; }
mark.out-seed { background:#b0850044; box-shadow:inset 0 -2px 0 #b08500; }
mark.no-class { background:#8884; box-shadow:inset 0 -2px 0 #888; }
.sel { font-size:13px; padding:8px 10px; border:1px solid var(--line); border-radius:6px;
       margin-bottom:12px; min-height:42px; }
.sel em { color:#888; font-style:normal; }
input[type=search], input[type=text] { width:100%; font:inherit; padding:6px 8px;
       border:1px solid var(--line); border-radius:6px; background:transparent; color:inherit; }
#classes { max-height:34vh; overflow:auto; margin:8px 0; border:1px solid var(--line);
           border-radius:6px; }
#classes div { padding:6px 9px; cursor:pointer; font-size:13px;
               border-bottom:1px solid var(--line); }
#classes div:last-child { border-bottom:0; }
#classes div:hover, #classes div.on { background:#2563eb22; }
#classes .gloss { display:block; color:#888; font-size:11px; }
h2 { font-size:12px; text-transform:uppercase; letter-spacing:.05em; color:#888;
     margin:16px 0 6px; }
#list { font-size:12px; }
#list div { padding:5px 7px; border-bottom:1px solid var(--line); display:flex; gap:6px;
            justify-content:space-between; }
#list b { font-weight:600; }
#list span.k { color:#888; }
#list button { padding:0 6px; font-size:11px; }
.counts { font-size:12px; color:#888; }
kbd { font:11px ui-monospace,monospace; border:1px solid var(--line); border-radius:3px;
      padding:0 4px; }
</style></head><body>
<header>
  <h1>Conjunto de retención</h1>
  <span class="meta" id="docid"></span>
  <span class="counts" id="counts"></span>
  <span style="flex:1"></span>
  <button id="export" class="primary">Exportar JSONL</button>
</header>
<main>
  <div id="text">__TEXT__</div>
  <aside>
    <div class="sel" id="sel"><em>Seleccioná texto para anotar.</em></div>
    <input type="search" id="filter" placeholder="Filtrar clases de la semilla…" disabled>
    <div id="classes"></div>
    <h2>Clase fuera de la semilla</h2>
    <input type="text" id="custom" placeholder="Nombre de clase nueva + Enter" disabled>
    <button id="noclass" disabled style="margin-top:8px;width:100%">
      Mención válida sin clase asignable
    </button>
    <h2>Anotaciones</h2>
    <div id="list"></div>
    <p class="counts" style="margin-top:16px">
      <kbd>Enter</kbd> asigna la primera clase filtrada. Se guarda solo en este navegador;
      exportá antes de cerrar.
    </p>
  </aside>
</main>
<script>
const DATA = __PAYLOAD__;
const textEl = document.getElementById("text");
const SOURCE = textEl.textContent;           // el Markdown exacto: los offsets indexan esto
const KEY = "onto-annot:" + DATA.doc_id;
let mentions = JSON.parse(localStorage.getItem(KEY) || "[]");
let pending = null;

document.getElementById("docid").textContent =
  DATA.doc_id + " · " + DATA.markdown_hash.slice(0, 19);

const pageFor = (offset) => {
  for (const [a, b, p] of DATA.pages) if (offset >= a && offset <= b) return p;
  let best = 1;
  for (const [a, , p] of DATA.pages) if (a <= offset) best = p;
  return best;
};

// Offset absoluto dentro de SOURCE, recorriendo los nodos de texto en orden. Hace falta
// porque al resaltar se parten los nodos y la posición local deja de servir.
const offsetOf = (node, nodeOffset) => {
  const walker = document.createTreeWalker(textEl, NodeFilter.SHOW_TEXT);
  let total = 0, current;
  while ((current = walker.nextNode())) {
    if (current === node) return total + nodeOffset;
    total += current.textContent.length;
  }
  return -1;
};

const save = () => { localStorage.setItem(KEY, JSON.stringify(mentions)); render(); };

function render() {
  const sorted = [...mentions].sort((x, y) => x.span[0] - y.span[0]);
  let html = "", cursor = 0;
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  for (const m of sorted) {
    if (m.span[0] < cursor) continue;                    // solapada: se ignora al pintar
    const cls = m.gold_class === null ? "no-class" : (m.in_seed ? "in-seed" : "out-seed");
    html += esc(SOURCE.slice(cursor, m.span[0]));
    html += `<mark class="${cls}" data-id="${m.id}" title="${esc(m.gold_class || "sin clase")}">`
          + esc(SOURCE.slice(m.span[0], m.span[1])) + "</mark>";
    cursor = m.span[1];
  }
  html += esc(SOURCE.slice(cursor));
  textEl.innerHTML = html;

  const inSeed = mentions.filter((m) => m.in_seed).length;
  document.getElementById("counts").textContent =
    `${mentions.length} menciones · ${inSeed} con clase de la semilla`;

  document.getElementById("list").innerHTML = sorted.map((m) =>
    `<div><span><b>${esc(m.gold_class || "—")}</b> `
    + `<span class="k">${esc(m.text.slice(0, 26))}</span></span>`
    + `<button data-del="${m.id}">✕</button></div>`).join("");
  for (const b of document.querySelectorAll("#list button[data-del]"))
    b.onclick = () => { mentions = mentions.filter((m) => m.id !== b.dataset.del); save(); };
  for (const mk of document.querySelectorAll("mark"))
    mk.onclick = () => { mentions = mentions.filter((m) => m.id !== mk.dataset.id); save(); };
}

document.addEventListener("mouseup", () => {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !textEl.contains(sel.anchorNode)) return;
  const a = offsetOf(sel.anchorNode, sel.anchorOffset);
  const b = offsetOf(sel.focusNode, sel.focusOffset);
  const [start, end] = a < b ? [a, b] : [b, a];
  if (start < 0 || end <= start) return;
  pending = { start, end, text: SOURCE.slice(start, end) };
  document.getElementById("sel").textContent = pending.text;
  for (const el of ["filter", "custom", "noclass"]) document.getElementById(el).disabled = false;
  document.getElementById("filter").focus();
});

function add(gold_class, in_seed) {
  if (!pending) return;
  mentions.push({
    id: "m" + (mentions.length + 1) + "_" + Date.now().toString(36),
    page: pageFor(pending.start), span: [pending.start, pending.end], text: pending.text,
    gold_class, in_seed, entity_id: null,
  });
  pending = null;
  document.getElementById("sel").innerHTML = "<em>Seleccioná texto para anotar.</em>";
  document.getElementById("filter").value = "";
  for (const el of ["filter", "custom", "noclass"]) document.getElementById(el).disabled = true;
  drawClasses("");
  save();
}

function drawClasses(query) {
  const q = query.toLowerCase();
  const hits = DATA.classes.filter((c) => c.label.toLowerCase().includes(q)
                                       || c.gloss.toLowerCase().includes(q));
  const box = document.getElementById("classes");
  box.innerHTML = hits.slice(0, 60).map((c, i) =>
    `<div data-iri="${c.iri}" class="${i === 0 && q ? "on" : ""}">${c.label}`
    + (c.gloss ? `<span class="gloss">${c.gloss.slice(0, 90)}</span>` : "") + "</div>").join("");
  for (const d of box.children)
    d.onclick = () => add(DATA.classes.find((c) => c.iri === d.dataset.iri).label, true);
  return hits;
}

document.getElementById("filter").oninput = (e) => drawClasses(e.target.value);
document.getElementById("filter").onkeydown = (e) => {
  if (e.key !== "Enter") return;
  const hits = drawClasses(e.target.value);
  if (hits.length) add(hits[0].label, true);
};
document.getElementById("custom").onkeydown = (e) => {
  if (e.key === "Enter" && e.target.value.trim()) {
    add(e.target.value.trim(), false);       // no está en la semilla -> in_seed false
    e.target.value = "";
  }
};
document.getElementById("noclass").onclick = () => add(null, false);

document.getElementById("export").onclick = () => {
  const doc = {
    doc_id: DATA.doc_id, markdown_hash: DATA.markdown_hash,
    mentions: [...mentions].sort((x, y) => x.span[0] - y.span[0]), relations: [],
  };
  const blob = new Blob([JSON.stringify(doc) + "\n"], { type: "application/x-ndjson" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = DATA.doc_id + ".jsonl";
  a.click();
};

drawClasses("");
render();
</script></body></html>
"""
