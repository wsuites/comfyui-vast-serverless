#!/usr/bin/env python3
"""
Generates visor.html: a self-contained A/B viewer for comparing outputs.

No server needed: the file index is embedded in the HTML and images load by
relative path. Double click and it works.

    python scripts/visor.py
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from config import ROOT

OUT = ROOT / "output"

# stage (SaveImage prefix in the graph) -> what it is
STAGES = {
    "a": "scene (scenario only)",
    "b": "collage (raw paste)",
    "c": "fused (img2img)",
    "d": "mask",
    "e": "pose (skeleton)",
    "f": "detail (2nd pass)",
    "h": "upscale 2x",
}

# combo -> active levers
COMBOS = {
    "base": [], "v": ["v"], "l": ["l"], "u": ["u"], "b": ["b"],
    "vl": ["v", "l"], "vu": ["v", "u"], "lu": ["l", "u"], "vb": ["v", "b"],
    "vlu": ["v", "l", "u"], "vbu": ["v", "b", "u"], "vblu": ["v", "b", "l", "u"],
}
LEVERS = {
    "v": "vertical: the character is generated at 832x1216 (SDXL native ratio) "
         "instead of 1024 square",
    "l": "canvas 1536: the scene/composition goes from 1024 to 1536",
    "b": "bbox: crop to the real character silhouette before scaling",
    "u": "upscale: UltimateSDUpscale 2x on the final composite",
}

DIRS_AB = ["ab-resolution/ab_res12", "ab-resolution/ab_up4"]
DIRS_SPRITES = ["sprites/sprites_out", "sprites/sprites_pose2",
                "sprites/sprites_norm", "running/correr_out"]


def med(p: Path):
    try:
        with Image.open(p) as im:
            return list(im.size)
    except Exception:
        return None


def main() -> None:
    ab: dict[str, dict] = {}
    for d in DIRS_AB:
        dd = OUT / d
        if not dd.is_dir():
            continue
        for p in sorted(dd.glob("*.png")):
            trozos = p.stem.split("_", 1)
            if len(trozos) != 2:
                continue
            etapa, combo = trozos
            ab.setdefault(f"{etapa}|{combo}", {})[d] = {
                "src": f"{d}/{p.name}", "size": med(p)}

    sprites: dict[str, list] = {}
    for d in DIRS_SPRITES:
        dd = OUT / d
        if not dd.is_dir():
            continue
        fs = sorted(dd.glob("*.png"))
        if fs:
            sprites[d] = [{"src": f"{d}/{p.name}", "name": p.name, "size": med(p)}
                          for p in fs]

    data = {"ab": ab, "sprites": sprites, "stages": STAGES,
            "combos": COMBOS, "levers": LEVERS, "dirs": DIRS_AB}
    html = TEMPLATE.replace("/*DATA*/", json.dumps(data, ensure_ascii=False))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "visor.html").write_text(html, encoding="utf-8")
    print(f"visor.html: {len(ab)} A/B pairs, "
          f"{sum(len(v) for v in sprites.values())} sprites in {len(sprites)} sets")


TEMPLATE = r"""<!doctype html>
<meta charset="utf-8">
<title>comfy-vast viewer</title>
<style>
:root { --bg:#15161a; --pan:#1e2027; --tx:#d8dae0; --mut:#888e9c; --ac:#e0533f; }
* { box-sizing:border-box }
body { margin:0; background:var(--bg); color:var(--tx);
       font:13px/1.5 ui-monospace,Consolas,monospace; overflow:hidden }
header { display:flex; gap:6px; align-items:center; padding:8px 12px;
         background:var(--pan); border-bottom:1px solid #000; flex-wrap:wrap }
button, select { background:#2a2d36; color:var(--tx); border:1px solid #3a3e4a;
                 border-radius:4px; padding:4px 9px; font:inherit; cursor:pointer }
button.on { background:var(--ac); border-color:var(--ac); color:#fff }
.sep { width:1px; height:22px; background:#3a3e4a; margin:0 4px }
.mut { color:var(--mut) }
#canvas { position:relative; height:calc(100vh - 190px); overflow:hidden;
          background:#0d0e11; touch-action:none }
.pane { position:absolute; inset:0; overflow:hidden }
.pane img { position:absolute; transform-origin:0 0 }
#B { clip-path:inset(0 0 0 50%) }
#slider { position:absolute; top:0; bottom:0; width:2px; background:var(--ac);
          cursor:ew-resize; z-index:5 }
#slider::after { content:''; position:absolute; top:50%; left:-9px; width:20px;
                 height:20px; margin-top:-10px; border-radius:50%;
                 background:var(--ac) }
body.side #A { right:50%; border-right:1px solid #000 }
body.side #B { left:50%; clip-path:none }
body.side #slider { display:none }
.lbl { position:absolute; top:8px; padding:2px 8px; background:#000a;
       border-radius:3px; z-index:4; pointer-events:none; font-size:12px }
.lbl.a { left:8px } .lbl.b { right:8px }
footer { padding:6px 12px; background:var(--pan); border-top:1px solid #000 }
#strip { display:flex; gap:4px; overflow-x:auto; padding:4px 0 }
#strip figure { margin:0; text-align:center; cursor:pointer;
                border:2px solid transparent; border-radius:4px; padding:2px }
#strip figure.flip img { transform:scaleX(-1) }
#strip figure.sel { border-color:var(--ac) }
#strip img { height:110px; display:block; background:#fff }
#strip figcaption { font-size:11px; color:var(--mut) }
#anim { background:#fff; border-radius:4px }
body.dark #anim, body.dark #strip img { background:#111 }
body.checker #anim, body.checker #strip img {
  background-color:#8a8a8a;
  background-image:linear-gradient(45deg,#666 25%,transparent 25%,transparent 75%,#666 75%),
                   linear-gradient(45deg,#666 25%,transparent 25%,transparent 75%,#666 75%);
  background-size:16px 16px; background-position:0 0,8px 8px }
#help { position:fixed; right:12px; bottom:12px; max-width:560px; max-height:70vh;
        overflow:auto; padding:12px 16px; background:var(--pan);
        border:1px solid #3a3e4a; border-radius:6px; display:none; z-index:10 }
#help.show { display:block }
#help dt { color:var(--ac); margin-top:8px } #help dd { margin:0 0 0 14px }
.hidden { display:none !important }
</style>

<header>
  <button id="modeAB" class="on">A/B resolution</button>
  <button id="modeSP">sprites</button>
  <span class="sep"></span>

  <span id="ctlAB">
    <select id="stage"></select>
    <select id="cA"></select><span class="mut"> vs </span><select id="cB"></select>
    <select id="dirA"></select><select id="dirB"></select>
    <button id="view">curtain</button>
    <button id="fit">fit</button>
    <button id="one">1:1</button>
  </span>

  <span id="ctlSP" class="hidden">
    <select id="set"></select>
    <button id="play" class="on">pause</button>
    <input id="ms" type="range" min="40" max="500" step="10" value="140"
           style="vertical-align:middle">
    <span id="msv" class="mut">140ms / 7.1fps</span>
    <button id="bg">background</button>
    <button id="flipall">flip all</button>
    <button id="copy">copy --flip</button>
  </span>

  <span class="sep"></span>
  <button id="btnHelp">?</button>
</header>

<div id="canvas">
  <div class="pane" id="A"><img id="imgA"></div>
  <div class="pane" id="B"><img id="imgB"></div>
  <div id="slider" style="left:50%"></div>
  <span class="lbl a" id="etA"></span><span class="lbl b" id="etB"></span>
  <img id="anim" class="hidden" style="position:absolute;inset:0;margin:auto;
       max-width:100%;max-height:100%">
</div>

<footer>
  <div id="strip" class="hidden"></div>
  <span id="info" class="mut"></span>
</footer>

<div id="help">
  <b>Nomenclature</b>
  <p class="mut">Files are <code>stage_combo.png</code>: the <b>letter</b> is the
  pipeline stage, the <b>suffix</b> is the enabled levers.</p>
  <dl id="glossary"></dl>
  <p class="mut">Wheel = zoom (synced on both panels).
  Drag = pan. Left/right arrows = change the B combo.</p>
</div>

<script>
const D = /*DATA*/;
const $ = s => document.querySelector(s);

/* ---------- glossary ---------- */
{
  let h = '<dt>levers (suffix)</dt>';
  for (const [k, v] of Object.entries(D.levers)) h += `<dd><b>${k}</b> — ${v}</dd>`;
  h += '<dt>stages (letter)</dt>';
  for (const [k, v] of Object.entries(D.stages)) h += `<dd><b>${k}</b> — ${v}</dd>`;
  $('#glossary').innerHTML = h;
}
$('#btnHelp').onclick = () => $('#help').classList.toggle('show');

/* ---------- what is available ---------- */
const stages = [...new Set(Object.keys(D.ab).map(k => k.split('|')[0]))].sort();
const combos = Object.keys(D.combos);
const fill = (sel, arr, txt) => {
  sel.innerHTML = arr.map(v => `<option value="${v}">${txt(v)}</option>`).join('');
};
fill($('#stage'), stages, v => `${v} · ${D.stages[v] || '?'}`);
fill($('#cA'), combos, v => v);
fill($('#cB'), combos, v => v);
fill($('#dirA'), D.dirs, v => v);
fill($('#dirB'), D.dirs, v => v);
$('#stage').value = stages.includes('f') ? 'f' : stages[0];
$('#cA').value = 'base'; $('#cB').value = 'vblu';

/* ---------- synced zoom/pan ---------- */
let z = 1, ox = 0, oy = 0;
const apply = () => {
  for (const id of ['#imgA', '#imgB'])
    $(id).style.transform = `translate(${ox}px,${oy}px) scale(${z})`;
};
const fit = () => {
  const im = $('#imgA'), L = $('#canvas');
  if (!im.naturalWidth) return;
  const w = document.body.classList.contains('side') ? L.clientWidth / 2 : L.clientWidth;
  z = Math.min(w / im.naturalWidth, L.clientHeight / im.naturalHeight) * 0.96;
  ox = (w - im.naturalWidth * z) / 2;
  oy = (L.clientHeight - im.naturalHeight * z) / 2;
  apply();
};
$('#fit').onclick = fit;
$('#one').onclick = () => { z = 1; apply(); };

$('#canvas').addEventListener('wheel', e => {
  if (mode !== 'ab') return;
  e.preventDefault();
  const f = e.deltaY < 0 ? 1.12 : 1 / 1.12;
  const r = $('#canvas').getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  ox = mx - (mx - ox) * f; oy = my - (my - oy) * f; z *= f; apply();
}, { passive: false });

let drag = null;
$('#canvas').addEventListener('pointerdown', e => {
  if (mode !== 'ab' || e.target.id === 'slider') return;
  drag = { x: e.clientX - ox, y: e.clientY - oy };
  $('#canvas').setPointerCapture(e.pointerId);
});
$('#canvas').addEventListener('pointermove', e => {
  if (!drag) return;
  ox = e.clientX - drag.x; oy = e.clientY - drag.y; apply();
});
addEventListener('pointerup', () => drag = null);

/* ---------- curtain ---------- */
{
  let grabbing = false;
  const t = $('#slider');
  t.addEventListener('pointerdown', e => { grabbing = true; t.setPointerCapture(e.pointerId); });
  addEventListener('pointermove', e => {
    if (!grabbing) return;
    const r = $('#canvas').getBoundingClientRect();
    const p = Math.max(0, Math.min(100, (e.clientX - r.left) / r.width * 100));
    t.style.left = p + '%';
    $('#B').style.clipPath = `inset(0 0 0 ${p}%)`;
  });
  addEventListener('pointerup', () => grabbing = false);
}
$('#view').onclick = e => {
  document.body.classList.toggle('side');
  e.target.textContent = document.body.classList.contains('side')
    ? 'side by side' : 'curtain';
  fit();
};

/* ---------- paint A/B ---------- */
function paint() {
  const st = $('#stage').value;
  const set = (side, combo, dir) => {
    const e = D.ab[`${st}|${combo}`];
    const f = e && (e[dir] || Object.values(e)[0]);
    const im = $('#img' + side);
    im.src = f ? f.src : '';
    im.style.opacity = f ? 1 : 0;
    $('#et' + side).textContent = f
      ? `${side}: ${combo} [${D.combos[combo].join('') || '—'}] ${f.size.join('×')}`
      : `${side}: ${st}_${combo} — does not exist`;
    return f;
  };
  const a = set('A', $('#cA').value, $('#dirA').value);
  const b = set('B', $('#cB').value, $('#dirB').value);
  $('#info').textContent = (a && b && a.size.join() !== b.size.join())
    ? 'note: canvases of different sizes; the zoom equalizes them in file px, '
      + 'not in the real scale of the figure'
    : '';
  $('#imgA').onload = fit;
}
for (const id of ['#stage', '#cA', '#cB', '#dirA', '#dirB']) $(id).onchange = paint;
addEventListener('keydown', e => {
  if (mode !== 'ab') return;
  const s = $('#cB'), i = s.selectedIndex;
  if (e.key === 'ArrowRight') { s.selectedIndex = (i + 1) % s.length; paint(); }
  if (e.key === 'ArrowLeft') { s.selectedIndex = (i - 1 + s.length) % s.length; paint(); }
});

/* ---------- sprites ---------- */
const sets = Object.keys(D.sprites);
fill($('#set'), sets, v => `${v} (${D.sprites[v].length})`);
let frames = [], idx = 0, tim = null, flips = new Set();

function buildStrip() {
  frames = D.sprites[$('#set').value] || [];
  flips = new Set();
  $('#strip').innerHTML = frames.map((f, i) =>
    `<figure data-i="${i}"><img src="${f.src}"><figcaption>${i}</figcaption></figure>`
  ).join('');
  $('#strip').querySelectorAll('figure').forEach(fg => {
    fg.onclick = () => {
      const i = +fg.dataset.i;
      flips.has(i) ? flips.delete(i) : flips.add(i);
      fg.classList.toggle('flip');
      idx = i; paintFrame();
    };
  });
  idx = 0; paintFrame();
}
function paintFrame() {
  if (!frames.length) return;
  const f = frames[idx];
  $('#anim').src = f.src;
  $('#anim').style.transform = flips.has(idx) ? 'scaleX(-1)' : '';
  $('#strip').querySelectorAll('figure').forEach((fg, i) =>
    fg.classList.toggle('sel', i === idx));
  $('#info').textContent = `${f.name} ${f.size.join('×')} · flipped: [${
    [...flips].sort((a, b) => a - b).join(',') || '—'}]`;
}
function run() {
  clearInterval(tim);
  if (mode !== 'sp' || !$('#play').classList.contains('on')) return;
  tim = setInterval(() => { idx = (idx + 1) % frames.length; paintFrame(); },
                    +$('#ms').value);
}
$('#set').onchange = () => { buildStrip(); run(); };
$('#play').onclick = e => {
  e.target.classList.toggle('on');
  e.target.textContent = e.target.classList.contains('on') ? 'pause' : 'play';
  run();
};
$('#ms').oninput = e => {
  $('#msv').textContent = `${e.target.value}ms / ${(1000 / e.target.value).toFixed(1)}fps`;
  run();
};
$('#bg').onclick = () => {
  const b = document.body;
  if (b.classList.contains('dark')) {
    b.classList.remove('dark'); b.classList.add('checker');
  } else if (b.classList.contains('checker')) {
    b.classList.remove('checker');
  } else {
    b.classList.add('dark');
  }
};
$('#flipall').onclick = () => {
  frames.forEach((_, i) => flips.has(i) ? flips.delete(i) : flips.add(i));
  $('#strip').querySelectorAll('figure').forEach(fg => fg.classList.toggle('flip'));
  paintFrame();
};
$('#copy').onclick = () => {
  const t = `--flip ${[...flips].sort((a, b) => a - b).join(',')} --ms ${$('#ms').value}`;
  navigator.clipboard?.writeText(t);
  $('#info').textContent = 'copied: ' + t;
};

/* ---------- modes ---------- */
let mode = 'ab';
function change(m) {
  mode = m;
  const ab = m === 'ab';
  $('#modeAB').classList.toggle('on', ab);
  $('#modeSP').classList.toggle('on', !ab);
  $('#ctlAB').classList.toggle('hidden', !ab);
  $('#ctlSP').classList.toggle('hidden', ab);
  $('#strip').classList.toggle('hidden', ab);
  $('#anim').classList.toggle('hidden', ab);
  for (const id of ['#A', '#B', '#slider', '#etA', '#etB'])
    $(id).classList.toggle('hidden', !ab);
  if (ab) { clearInterval(tim); paint(); } else { buildStrip(); run(); }
}
$('#modeAB').onclick = () => change('ab');
$('#modeSP').onclick = () => change('sp');
paint();
</script>
"""

if __name__ == "__main__":
    main()