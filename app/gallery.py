"""The frame's own gallery, served at ``GET /`` (and ``/gallery``).

Shaped like a phone photo app on purpose: a tile grid, a tab bar at the bottom to
switch between everything and collections, and a full-screen sheet when you open a
photo. It exists because Home Assistant's built-in media browser is a file lister —
`local_source.py` never sets a `thumbnail`, and the only way to *play* from it is a
`media_player` — so the frame serves the picker itself. Home Assistant embeds this
page as a sidebar panel; it also works on its own from a phone.

Two things it must never do, both learned the hard way:

* **Loading the page must not touch the panel.** A browser restoring form state across
  a navigation fires `change` on its own, so no `<select>` may drive a panel-touching
  action without checking that the change came from a person.
* **Nothing here assumes an absolute origin.** Every URL is relative, so the page
  behaves the same on ``http://<ip>:8080`` and behind the Tailscale HTTPS proxy — which
  is also what keeps it out of mixed-content trouble inside an HTTPS Home Assistant.

No build step and no CDN: the Pi has no business fetching a framework to draw a grid,
and the frame should keep working when the internet doesn't.
"""

from __future__ import annotations

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Inky Frame</title>
<style>
  :root {
    --bg: #f6f6f4; --card: #fff; --fg: #16161a; --muted: #6b6b76;
    --line: #e2e2dd; --accent: #0a84ff; --danger: #d1344b; --tile: #e8e8e3;
    --bar: rgba(246,246,244,.86); --sheet: #fff;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f0f12; --card: #1b1b21; --fg: #ececf0; --muted: #9a9aa6;
      --line: #2b2b34; --accent: #4da3ff; --danger: #ff6b7f; --tile: #22222a;
      --bar: rgba(15,15,18,.86); --sheet: #17171c;
    }
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  /* Dragging an <img> hands the browser the image as a file; the page's own drop
     handler then treats it as an upload. Nothing here is meant to be dragged out.
     -webkit-touch-callout is the other half: without it iOS answers the press-and-hold
     compare gesture with its own "Save Image / Copy" sheet, over the thing you are
     trying to look at. */
  img { -webkit-user-drag: none; -webkit-user-select: none; user-select: none;
        -webkit-touch-callout: none; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    padding-bottom: calc(4.2rem + env(safe-area-inset-bottom));
  }
  button, select { font: inherit; color: var(--fg); cursor: pointer;
                   -webkit-appearance: none; appearance: none; }
  .btn { background: var(--card); border: 1px solid var(--line); border-radius: 9px;
         padding: .4rem .7rem; }
  .btn:hover { border-color: var(--accent); }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: #fff;
                 font-weight: 600; }
  .btn.danger { color: var(--danger); }
  /* A destructive confirmation is still the primary button — it goes red, not blue
     with red text on it. */
  .btn.primary.danger { background: var(--danger); border-color: var(--danger);
                        color: #fff; }
  .btn:disabled { opacity: .45; cursor: default; }

  /* ---- now-showing strip ------------------------------------------------ */
  header { position: sticky; top: 0; z-index: 4; backdrop-filter: blur(14px);
           background: var(--bar); border-bottom: 1px solid var(--line); }
  .now { display: flex; gap: .8rem; align-items: center;
         max-width: 1100px; margin: 0 auto; padding: .6rem 1rem; }
  .now img { width: 92px; flex: none; border-radius: 8px; background: var(--tile);
             border: 1px solid var(--line); aspect-ratio: 4/3; object-fit: contain; }
  .now .meta { min-width: 0; flex: 1; }
  .now h1 { font-size: .95rem; margin: 0 0 .1rem; font-weight: 650; }
  .now .sub { color: var(--muted); font-size: .8rem; overflow: hidden;
              text-overflow: ellipsis; white-space: nowrap; }
  .dot { display: inline-block; width: .5rem; height: .5rem; border-radius: 50%;
         background: #35c759; margin-right: .35rem; vertical-align: 1px; }
  .dot.busy { background: #ff9f0a; animation: pulse 1.1s ease-in-out infinite; }
  .dot.err { background: var(--danger); }
  @keyframes pulse { 50% { opacity: .25; } }
  .now .row { display: flex; gap: .3rem; margin-top: .4rem; flex-wrap: wrap; }

  /* ---- grid ------------------------------------------------------------- */
  main { max-width: 1100px; margin: 0 auto; padding: .5rem; }
  .grid { display: grid; gap: 3px;
          grid-template-columns: repeat(auto-fill, minmax(108px, 1fr)); }
  .cell { position: relative; border: 0; padding: 0; background: var(--tile);
          aspect-ratio: 1; overflow: hidden; border-radius: 3px; }
  .cell img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .cell.on { outline: 3px solid var(--accent); outline-offset: -3px; }
  .cell .pin { position: absolute; left: 4px; bottom: 4px; background: var(--accent);
               color: #fff; font-size: .6rem; font-weight: 700; letter-spacing: .04em;
               padding: .05rem .3rem; border-radius: 4px; }
  .cell .n { position: absolute; right: 4px; top: 4px; background: rgba(0,0,0,.55);
             color: #fff; font-size: .6rem; padding: .05rem .3rem; border-radius: 4px; }

  /* ---- collections ------------------------------------------------------ */
  .cols { display: grid; gap: .8rem;
          grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
  .col { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
         overflow: hidden; text-align: left; padding: 0; }
  .col .cover { display: grid; grid-template-columns: 1fr 1fr; gap: 1px;
                background: var(--line); aspect-ratio: 1; }
  .col .cover img, .col .cover div { width: 100%; height: 100%; object-fit: cover;
                                     background: var(--tile); }
  .col .lab { padding: .45rem .6rem; }
  .col .lab b { display: block; overflow: hidden; text-overflow: ellipsis;
                white-space: nowrap; }
  .col .lab span { color: var(--muted); font-size: .8rem; }

  /* ---- tab bar ---------------------------------------------------------- */
  footer { position: fixed; left: 0; right: 0; bottom: 0; z-index: 6;
           backdrop-filter: blur(14px); background: var(--bar);
           border-top: 1px solid var(--line);
           padding: .35rem .5rem calc(.35rem + env(safe-area-inset-bottom)); }
  .tabs { max-width: 1100px; margin: 0 auto; display: flex; align-items: center; }
  .tab { flex: 1; background: none; border: 0; color: var(--muted); font-size: .72rem;
         display: flex; flex-direction: column; align-items: center; gap: .12rem;
         padding: .25rem 0; }
  .tab.sel { color: var(--accent); }
  .tab .ic { line-height: 0; }
  .tab .ic svg { width: 1.3rem; height: 1.3rem; stroke: currentColor; fill: none;
                 stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }

  /* ---- sheet ------------------------------------------------------------ */
  #sheet { position: fixed; inset: 0; z-index: 10; background: var(--sheet);
           display: none; flex-direction: column; }
  #sheet.open { display: flex; }
  .sheet-top { display: flex; align-items: center; gap: .5rem; padding: .6rem 1rem;
               border-bottom: 1px solid var(--line); }
  .sheet-top .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
                  white-space: nowrap; font-weight: 600; }
  .stage { flex: 1; min-height: 0; display: flex; align-items: center;
           justify-content: center; padding: 1rem; background: var(--tile); }
  .stage img { max-width: 100%; max-height: 100%; object-fit: contain;
               border-radius: 6px; }
  .sheet-foot { border-top: 1px solid var(--line); padding: .6rem 1rem
                calc(.6rem + env(safe-area-inset-bottom)); display: grid; gap: .55rem; }
  /* Both grids, both min-width:0. The render strip scrolls sideways, but only while
     it is a GRID ITEM (whose automatic minimum size an overflow:auto box takes to 0);
     wrapped in a plain block it would hand its full min-content width upward and shove
     the Show button off the screen. */
  #foot-view, #foot-crop { display: grid; gap: .55rem; min-width: 0; }
  /* ...and an id selector outranks the UA's `[hidden] { display: none }`, so say it. */
  #foot-view[hidden], #foot-crop[hidden] { display: none; }
  .strip { display: flex; gap: .4rem; overflow-x: auto; padding-bottom: .15rem;
           min-width: 0; }
  .strip button { flex: none; border: 2px solid transparent; border-radius: 8px;
                  padding: 0; background: var(--tile); width: 62px; aspect-ratio: 4/3;
                  overflow: hidden; }
  .strip button.sel { border-color: var(--accent); }
  .strip img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .ctl { display: flex; gap: .4rem; flex-wrap: wrap; align-items: center; }
  .ctl label { display: inline-flex; align-items: center; gap: .3rem;
               color: var(--muted); font-size: .8rem; }
  .ctl select { background: var(--card); border: 1px solid var(--line);
                border-radius: 9px; padding: .35rem .5rem; max-width: 11rem; }
  .chips { display: flex; gap: .35rem; flex-wrap: wrap; }
  .chip { border: 1px solid var(--line); background: var(--card); border-radius: 999px;
          padding: .22rem .6rem; font-size: .8rem; }
  .chip.on { background: var(--accent); border-color: var(--accent); color: #fff; }

  #toast { position: fixed; left: 50%; bottom: 5rem; transform: translateX(-50%);
           background: var(--fg); color: var(--bg); padding: .5rem 1rem;
           border-radius: 999px; font-size: .85rem; opacity: 0; pointer-events: none;
           transition: opacity .2s; z-index: 20; }
  #toast.on { opacity: .95; }
  .empty { color: var(--muted); text-align: center; padding: 3rem 1rem; }
  /* ---- icon buttons ---------------------------------------------------- */
  .ib { display: inline-grid; place-items: center; width: 2.3rem; height: 2.3rem;
        border-radius: 50%; border: 1px solid var(--line); background: var(--card);
        color: var(--fg); padding: 0; flex: none; }
  .ib:hover { border-color: var(--accent); color: var(--accent); }
  .ib.on { background: var(--accent); border-color: var(--accent); color: #fff; }
  .ib.danger:hover { border-color: var(--danger); color: var(--danger); }
  .ib.wide { width: auto; border-radius: 999px; padding: 0 .9rem; gap: .4rem;
             grid-auto-flow: column; font-weight: 600; }
  .ib:disabled { opacity: .4; cursor: default; border-color: var(--line);
                 color: var(--muted); }
  .ib svg { width: 1.15rem; height: 1.15rem; stroke: currentColor; fill: none;
            stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }

  /* ---- crop editor ------------------------------------------------------ */
  .cropwrap { position: relative; width: 100%; height: 100%; overflow: hidden;
              touch-action: none; cursor: grab; }
  .cropwrap.grabbing { cursor: grabbing; }
  .rot { position: absolute; transform-origin: 0 0; will-change: transform; }
  .rot img { position: absolute; max-width: none; }
  .frame { position: absolute; border: 2px solid #fff; border-radius: 2px;
           box-shadow: 0 0 0 9999px rgba(0,0,0,.62),
                       inset 0 0 0 1px rgba(0,0,0,.35); pointer-events: none; }
  /* Rule-of-thirds, as two elements rather than pseudo-element margins: a PERCENTAGE
     MARGIN RESOLVES AGAINST THE WIDTH ON EVERY SIDE, so `margin: 33.33% 0` put the
     horizontal lines at 44%/56% of a 4:3 frame instead of at the thirds. Percentage
     top/bottom offsets resolve against the height, which is what this needs. */
  /* A white hairline vanishes on a pale photo and a dark one vanishes on a dark
     photo, so every guide is white with a dark halo under it. */
  .frame i { position: absolute; display: block; pointer-events: none;
             border: 0 solid rgba(255,255,255,.75);
             filter: drop-shadow(0 0 1px rgba(0,0,0,.65)); }
  .frame i.v { top: 0; bottom: 0; left: 33.3333%; right: 33.3333%;
               border-left-width: 1px; border-right-width: 1px; }
  .frame i.h { left: 0; right: 0; top: 33.3333%; bottom: 33.3333%;
               border-top-width: 1px; border-bottom-width: 1px; }
  /* A finer 9x9 while straightening: thirds are for composing, this is for lining a
     horizon up against something. */
  .frame u { position: absolute; inset: 0; opacity: 0; pointer-events: none;
             transition: opacity .15s;
             filter: drop-shadow(0 0 1px rgba(0,0,0,.6));
             background-image: linear-gradient(90deg, rgba(255,255,255,.55) 1px,
                                               transparent 1px),
                               linear-gradient(rgba(255,255,255,.55) 1px,
                                               transparent 1px);
             background-size: 11.1111% 100%, 100% 11.1111%; }
  body.rotating .frame u { opacity: 1; }
  .hint { position: absolute; left: 50%; bottom: .5rem; transform: translateX(-50%);
          background: rgba(0,0,0,.55); color: #fff; font-size: .72rem; max-width: 92%;
          padding: .15rem .55rem; border-radius: 999px; pointer-events: none;
          white-space: nowrap; transition: background .15s; }
  .hint.snap { background: var(--accent); font-weight: 600; }

  /* ---- straighten dial --------------------------------------------------- */
  /* A ruler that the photo hangs off: drag it and the ticks follow your finger, so
     the picture turns the other way. Detents every 45 deg (§ snapAngle). */
  .dial { position: relative; height: 2.5rem; overflow: hidden; touch-action: none;
          cursor: ew-resize; border: 1px solid var(--line); border-radius: 10px;
          background: var(--card);
          -webkit-user-select: none; user-select: none; }
  .dial .ticks { position: absolute; left: 50%; top: 0; bottom: 0; width: 0;
                 will-change: transform; }
  .dial .ticks i { position: absolute; top: 1.55rem; bottom: .35rem; width: 1px;
                   background: var(--muted); opacity: .5; }
  .dial .ticks i.maj { top: 1.1rem; opacity: 1; background: var(--fg); }
  .dial .ticks i.maj b { position: absolute; left: 50%; top: -1.05rem; font-size: .6rem;
                         font-weight: 500; color: var(--muted); line-height: 1;
                         transform: translateX(-50%); }
  .dial .needle { position: absolute; left: 50%; top: .2rem; bottom: .2rem; width: 2px;
                  margin-left: -1px; background: var(--accent); border-radius: 2px; }
  .deg { font-variant-numeric: tabular-nums; font-size: .82rem; color: var(--muted);
         min-width: 3.6rem; text-align: center; }
  .deg.snap { color: var(--accent); font-weight: 600; }

  /* ---- loading ----------------------------------------------------------- */
  .spin { width: 1.6rem; height: 1.6rem; border-radius: 50%;
          border: 2px solid var(--line); border-top-color: var(--accent);
          animation: spin .7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .veil { position: absolute; inset: 0; display: none; place-items: center;
          background: color-mix(in srgb, var(--tile) 62%, transparent); z-index: 3; }
  .veil.on { display: grid; }
  .skel { background: linear-gradient(90deg, var(--tile), var(--line), var(--tile));
          background-size: 200% 100%; animation: sweep 1.2s ease-in-out infinite; }
  @keyframes sweep { to { background-position: -200% 0; } }
  .bar { position: fixed; left: 0; top: 0; height: 2px; background: var(--accent);
         width: 0; transition: width .2s; z-index: 40; }

  /* ---- dither / background pickers --------------------------------------- */
  #dpick, #bgpick { position: fixed; inset: 0; z-index: 14; display: none;
           background: color-mix(in srgb, var(--bg) 70%, transparent); }
  #dpick.open, #bgpick.open { display: grid; place-items: end center; }
  #dpick .panel, #bgpick .panel { background: var(--sheet);
                  border-top-left-radius: 16px;
                  border-top-right-radius: 16px; width: min(560px, 100%);
                  max-height: 82vh; overflow: auto; padding: .8rem 1rem
                  calc(1rem + env(safe-area-inset-bottom)); }
  #dpick h3, #bgpick h3 { margin: .2rem 0 .7rem; font-size: 1rem; }
  .note { margin: 0 0 .8rem; color: var(--muted); font-size: .82rem; }
  /* A swatch is the control: naming a colour in a dropdown says less than showing it. */
  .sw { display: block; width: 1.15rem; height: 1.15rem; border-radius: 5px;
        border: 1px solid var(--line); box-shadow: inset 0 0 0 1px rgba(0,0,0,.12); }
  #bgopts { display: grid; grid-template-columns: repeat(3, 1fr); gap: .5rem; }
  .bgopt { display: grid; gap: .4rem; justify-items: center; padding: .7rem .4rem;
           border: 1px solid var(--line); border-radius: 12px; background: var(--card);
           color: var(--fg); font-size: .82rem; }
  .bgopt .sw { width: 2.2rem; height: 2.2rem; border-radius: 8px; }
  .bgopt.sel { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
  .dopt { display: grid; grid-template-columns: 1fr auto; gap: .1rem .6rem;
          width: 100%; text-align: left; background: var(--card); color: var(--fg);
          border: 1px solid var(--line); border-radius: 12px; padding: .6rem .8rem;
          margin-bottom: .4rem; }
  .dopt.sel { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
  .dopt b { font-weight: 620; }
  .dopt span { grid-column: 1 / -1; color: var(--muted); font-size: .82rem; }
  /* An inline <svg> with no width/height lays out at 300x150 and fills black. The tick
     on the selected row did exactly that and shoved the sheet apart. */
  .dopt svg { width: 1.15rem; height: 1.15rem; stroke: var(--accent); fill: none;
              stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round;
              align-self: center; }
  .dopt i { display: block; width: 1.15rem; }
  #dpick .head, #bgpick .head { display: flex; align-items: center; gap: .5rem;
                               margin: .2rem 0 .7rem; }
  #dpick .head h3, #bgpick .head h3 { margin: 0; font-size: 1rem; flex: 1; }

  /* ---- ask / confirm ----------------------------------------------------- */
  /* Not window.prompt(): Chrome ignores prompt/confirm inside a CROSS-ORIGIN IFRAME,
     which is exactly how Home Assistant embeds this page in the sidebar. Renaming and
     deleting silently did nothing there. */
  #ask { position: fixed; inset: 0; z-index: 18; display: none;
         background: color-mix(in srgb, var(--bg) 62%, transparent);
         place-items: center; padding: 1rem; }
  #ask.open { display: grid; }
  #ask form { background: var(--sheet); border: 1px solid var(--line);
              border-radius: 14px; padding: 1rem; width: min(24rem, 100%);
              display: grid; gap: .7rem; box-shadow: 0 18px 50px rgba(0,0,0,.25); }
  #ask h3 { margin: 0; font-size: 1rem; font-weight: 620; }
  #ask input { font: inherit; color: var(--fg); background: var(--card); width: 100%;
               border: 1px solid var(--line); border-radius: 9px; padding: .5rem .6rem; }
  #ask input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  #ask .row { display: flex; gap: .4rem; justify-content: flex-end; }

  #drop { position: fixed; inset: 0; z-index: 30; display: none; align-items: center;
          justify-content: center; font-weight: 600;
          background: color-mix(in srgb, var(--bg) 82%, transparent);
          outline: 3px dashed var(--accent); outline-offset: -1.2rem; }
  body.dragging #drop { display: flex; }
</style></head><body>

<header><div class="now">
  <img id="preview" alt="what is on the panel now">
  <div class="meta">
    <h1><span class="dot" id="dot"></span><span id="state">connecting…</span></h1>
    <div class="sub" id="sub">&nbsp;</div>
    <div class="row">
      <button class="ib" title="Previous photo" onclick="nav('prev')"
              data-icon="prev"></button>
      <button class="ib" title="Random photo" onclick="nav('random')"
              data-icon="shuffle"></button>
      <button class="ib" title="Next photo" onclick="nav('next')"
              data-icon="next"></button>
      <button class="ib" title="Blank the panel" onclick="clearPanel()"
              data-icon="eraser"></button>
    </div>
  </div>
</div></header>

<main>
  <input id="picker" type="file" accept="image/*" multiple hidden
         onchange="upload(this.files); this.value = '';">
  <div id="crumb" class="row" style="display:none;padding:.2rem .3rem .6rem">
    <button class="ib" title="Back to collections" onclick="openTab('collections')"
            data-icon="back"></button>
    <b id="crumb-name" style="align-self:center;flex:1"></b>
    <button class="ib" title="Rename collection" onclick="renameCollection()"
            data-icon="pencil"></button>
    <button class="ib danger" title="Delete collection" onclick="removeCollection()"
            data-icon="trash"></button>
  </div>
  <div class="grid" id="grid"></div>
  <div class="cols" id="cols" style="display:none"></div>
  <div class="empty" id="empty" hidden></div>
  <div class="row" style="justify-content:center;margin-top:1rem">
    <button class="btn" id="more" onclick="loadMore()" hidden>Load more</button>
  </div>
</main>

<footer><div class="tabs">
  <button class="tab sel" id="tab-all" onclick="openTab('all')">
    <span class="ic" data-icon="grid"></span>All</button>
  <button class="tab" id="tab-collections" onclick="openTab('collections')">
    <span class="ic" data-icon="folder"></span>Collections</button>
  <button class="tab" onclick="picker.click()">
    <span class="ic" data-icon="plus"></span>Add</button>
</div></footer>

<div id="sheet">
  <div class="sheet-top">
    <button class="ib" title="Close" onclick="closeSheet()" data-icon="close"></button>
    <span class="t" id="sheet-title"></span>
    <button class="ib" title="Rename this photo" onclick="renamePhoto()"
            data-icon="pencil"></button>
    <button class="ib danger" title="Delete this photo" onclick="deletePhoto()"
            data-icon="trash"></button>
  </div>

  <div class="stage" style="position:relative">
    <img id="stage-img" alt="" draggable="false" style="display:none">
    <div class="cropwrap" id="cropwrap" style="display:none">
      <div class="rot" id="rot"><img id="cropimg" alt="" draggable="false"></div>
      <div class="frame" id="frame"><i class="v"></i><i class="h"></i><u></u></div>
      <div class="hint" id="hint"></div>
    </div>
    <div class="veil" id="veil"><div class="spin"></div></div>
  </div>

  <div class="sheet-foot">
    <!-- Two toolbars, one per mode. Placement is a gesture now, so the only geometry
         control out here is the door into the editor. -->
    <div id="foot-view">
      <div class="strip" id="strip"></div>
      <div class="ctl">
        <button class="ib" id="b-crop" title="Crop, zoom and straighten"
                onclick="toggleCrop()" data-icon="crop"></button>
        <button class="ib" id="b-bg" title="Background colour" onclick="openBg()">
          <i class="sw" id="bg-sw"></i></button>
        <button class="ib wide" id="b-dither" title="Dithering algorithm"
                onclick="openDither()"><span data-icon="palette"></span>
          <span id="d-name">—</span></button>
        <button class="ib wide" id="show-btn" title="Put this on the panel"
                onclick="showCurrent()" style="margin-left:auto;background:var(--accent);
                border-color:var(--accent);color:#fff">
          <span data-icon="send"></span>Show</button>
      </div>
      <div class="chips" id="chips"></div>
    </div>
    <div id="foot-crop" hidden>
      <div class="dial" id="dial"><div class="ticks" id="ticks"></div>
        <div class="needle"></div></div>
      <div class="ctl">
        <button class="ib" id="b-undo" title="Undo the last change"
                onclick="undo()" data-icon="undo"></button>
        <button class="ib" title="Start over" onclick="resetCrop()"
                data-icon="reset"></button>
        <output class="deg" id="deg">0°</output>
        <button class="ib wide" title="Apply this crop" onclick="toggleCrop()"
                style="margin-left:auto;background:var(--accent);
                border-color:var(--accent);color:#fff">
          <span data-icon="check"></span>Done</button>
      </div>
    </div>
  </div>
</div>

<div id="dpick" onclick="if (event.target === this) closeDither()">
  <div class="panel">
    <div class="head"><h3>Dithering</h3>
      <button class="ib" title="Close" onclick="closeDither()" data-icon="close"></button>
    </div>
    <div id="dopts"></div>
  </div>
</div>

<div id="bgpick" onclick="if (event.target === this) closeBg()">
  <div class="panel">
    <div class="head"><h3>Background</h3>
      <button class="ib" title="Close" onclick="closeBg()" data-icon="close"></button>
    </div>
    <p class="note">What fills the frame where the photo doesn't reach — the margin a
      fitted photo leaves, and the corners straightening exposes. Only the panel's own
      six colours: any other would come out as a stipple rather than a flat block.</p>
    <div id="bgopts"></div>
  </div>
</div>

<div id="ask" onclick="if (event.target === this) askDone(null)">
  <form onsubmit="event.preventDefault(); askSubmit()">
    <h3 id="ask-title"></h3>
    <input id="ask-input" autocomplete="off" spellcheck="false">
    <div class="row">
      <button type="button" class="btn" onclick="askDone(null)">Cancel</button>
      <button type="submit" class="btn primary" id="ask-ok">OK</button>
    </div>
  </form>
</div>
<div class="bar" id="bar"></div>

<div id="drop">Drop photos to add them</div>
<div id="toast"></div>

<script>
const PAGE_SIZE = 60;
const $ = (id) => document.getElementById(id);

// Inline SVG, drawn here rather than fetched: the frame has to work with no internet.
const ICONS = {
  close: "M6 6l12 12M18 6L6 18",
  trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6",
  prev: "M15 5l-7 7 7 7", next: "M9 5l7 7-7 7",
  back: "M15 5l-7 7 7 7",
  shuffle: "M3 7h4l10 10h4M17 3l4 4-4 4M3 17h4l3-3M14 10l3-3h4M17 21l4-4",
  eraser: "M4 19h16M6 16l8-8 5 5-6 6H8z",
  plus: "M12 5v14M5 12h14",
  grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  folder: "M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z",
  pencil: "M4 20h4L20 8l-4-4L4 16zM14 6l4 4",
  crop: "M6 2v14a2 2 0 002 2h14M2 6h14a2 2 0 012 2v14",
  undo: "M4 10h10a5 5 0 010 10h-4M4 10l4-4M4 10l4 4",
  reset: "M20 12a8 8 0 11-2.6-5.9M20 3v5h-5",
  palette: "M12 3a9 9 0 100 18h2a2 2 0 002-2 2 2 0 012-2h1a2 2 0 002-2 9 9 0 00-9-12z"
           + "M7.5 11h.01M10 7.5h.01M14 7.5h.01M16.5 11h.01",
  send: "M4 12l16-8-6 16-2-6z",
  check: "M5 13l4 4L19 7",
};
const icon = (name) =>
  `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${ICONS[name] || ""}"/></svg>`;
function paintIcons(root) {
  (root || document).querySelectorAll("[data-icon]").forEach((el) => {
    if (el.dataset.painted) return;
    el.dataset.painted = "1";
    el.insertAdjacentHTML("afterbegin", icon(el.dataset.icon));
  });
}

// What each algorithm actually does to a photo, so the choice means something. Order
// is rough "smoothest first"; whatever the frame reports that isn't listed still shows.
const DITHER_INFO = {
  JARVIS_JUDICE_NINKE: ["Jarvis", "Spreads the error widest. Smoothest gradients and the softest look — the safe default for photographs."],
  STUCKI: ["Stucki", "Like Jarvis but crisper: a little more edge detail, slightly more visible grain."],
  BURKES: ["Burkes", "A faster Stucki. Slightly coarser grain, still smooth in skies and skin."],
  FLOYD_STEINBERG: ["Floyd–Steinberg", "The classic. Fine even grain, a good all-rounder with more bite than Jarvis."],
  SIERRA: ["Sierra", "Balanced grain with a touch more contrast than Floyd–Steinberg."],
  SIERRA_LITE: ["Sierra Lite", "Sparse and quick. Visible grain, punchy — good for graphic images."],
  ATKINSON: ["Atkinson", "Keeps only part of the error: bright, high contrast, crisp edges. Loses detail in deep shadow."],
  ORDERED: ["Ordered", "A fixed crosshatch pattern instead of noise. Retro and very even — a print-screen look."],
  NONE: ["None", "No dithering at all: flat blocks of the six colours. What you want for text, logos and QR codes."],
};
const ditherLabel = (name) => (DITHER_INFO[name] || [name])[0];

let tab = "all";            // all | collections
let collectionId = null;    // set while browsing inside one
let photos = [], total = 0, collections = [];
let current = {};           // {photo_id, render_key} — what is on the panel
let busy = false, timer = null;
let ditherOptions = [], bgOptions = [], prefsNow = {}, panelSize = [1600, 1200];
let sheet = null;           // the photo open in the sheet
let want = {};              // fit / rotate / dither chosen in the sheet
let selectedKey = null;

// A modal of our own, because window.prompt/confirm are IGNORED inside a
// cross-origin iframe — which is exactly how Home Assistant embeds this page in the
// sidebar, where renaming and deleting quietly did nothing. Resolves to the typed
// string, `true` for a bare confirmation, or null when dismissed.
let askResolve = null, askHasInput = false;
function ask({ title, value = null, ok = "OK", danger = false }) {
  if (askResolve) { const stale = askResolve; askResolve = null; stale(null); }
  askHasInput = value !== null;
  $("ask-title").textContent = title;
  const input = $("ask-input");
  input.hidden = !askHasInput;
  input.value = askHasInput ? value : "";
  $("ask-ok").textContent = ok;
  $("ask-ok").classList.toggle("danger", danger);
  $("ask").classList.add("open");
  if (askHasInput) setTimeout(() => { input.focus(); input.select(); }, 30);
  return new Promise((resolve) => { askResolve = resolve; });
}
const askSubmit = () => askDone(askHasInput ? $("ask-input").value.trim() : true);
function askDone(value) {
  $("ask").classList.remove("open");
  const resolve = askResolve;
  askResolve = null;
  if (resolve) resolve(value);
}
const confirmAsk = (title, ok) => ask({ title, ok, danger: true });

function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.add("on");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("on"), 2600);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return r.status === 204 ? null : r.json();
}

const when = (ts) => !ts ? "" : new Date(ts * 1000).toLocaleString(
  [], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
const titleOf = (p) => p.name || when(p.added_at);

function fillSelect(el, options, value) {
  if (el.dataset.opts !== JSON.stringify(options)) {
    el.dataset.opts = JSON.stringify(options);
    el.innerHTML = options.map((o) => `<option>${o}</option>`).join("");
  }
  el.value = value;
}

// -- panel state -----------------------------------------------------------
async function poll() {
  try {
    const s = await api("/status");
    const d = s.display, wasBusy = busy;
    busy = d.busy;
    current = { photo_id: s.library.current_id, render_key: s.library.current_render };
    ditherOptions = s.dither.available;
    bgOptions = (s.background && s.background.available) || bgOptions;
    prefsNow = s.prefs;
    panelSize = s.display.resolution || panelSize;

    $("dot").className = "dot" + (d.busy ? " busy" : (d.last_error ? " err" : ""));
    $("state").textContent = d.busy ? "Refreshing the panel…"
      : (d.last_error ? "Last refresh failed" : (s.library.current_name || "Idle"));
    $("sub").textContent = d.last_error || `${s.device.name} · ${s.library.count} photos · `
      + `${s.library.renders} renders · ${s.library.collections} `
      + `collection${s.library.collections === 1 ? "" : "s"}`;
    const v = Math.floor(d.preview_updated_at || d.last_shown_at || 0);
    $("preview").src = `/display/preview?view=true&v=${v}`;
    $("preview").style.aspectRatio = s.prefs.orientation === "portrait" ? "3/4" : "4/3";

    markCurrent();
    if (wasBusy && !busy) { reload(); }
  } catch (e) {
    $("dot").className = "dot err";
    $("state").textContent = "Frame unreachable";
    $("sub").textContent = String(e.message || e);
  }
  clearTimeout(timer);
  timer = setTimeout(poll, busy ? 2500 : 9000);
}

// -- grid ------------------------------------------------------------------
function cell(p) {
  const el = document.createElement("button");
  el.className = "cell";
  el.dataset.id = p.id;
  el.title = titleOf(p);
  el.innerHTML = `<img loading="lazy" draggable="false" alt=""
    src="${p.thumb}?v=${Math.floor(p.added_at)}">`
    + (p.renders.length ? `<span class="n">${p.renders.length}</span>` : "");
  el.onclick = () => openSheet(p.id);
  return el;
}

function markCurrent() {
  document.querySelectorAll(".cell").forEach((c) => {
    const on = c.dataset.id === current.photo_id;
    c.classList.toggle("on", on);
    const pin = c.querySelector(".pin");
    if (on && !pin) {
      const b = document.createElement("span");
      b.className = "pin"; b.textContent = "ON FRAME"; c.appendChild(b);
    } else if (!on && pin) { pin.remove(); }
  });
}

function renderGrid() {
  const grid = $("grid");
  grid.innerHTML = "";
  photos.forEach((p) => grid.appendChild(cell(p)));
  $("more").hidden = photos.length >= total;
  $("empty").hidden = total > 0;
  $("empty").textContent = collectionId
    ? "This collection is empty. Open a photo and tag it here."
    : "Nothing here yet — tap ＋ Add, or drop images anywhere on this page.";
  markCurrent();
}

function skeletons(n) {
  const grid = $("grid");
  grid.innerHTML = "";
  for (let i = 0; i < n; i++) {
    const d = document.createElement("div");
    d.className = "cell skel";
    grid.appendChild(d);
  }
  $("empty").hidden = true;
  $("more").hidden = true;
}

async function loadPage(offset) {
  if (offset === 0) skeletons(12);
  const q = new URLSearchParams({ limit: PAGE_SIZE, offset });
  if (collectionId) q.set("collection", collectionId);
  const d = await api(`/library?${q}`);
  total = d.total;
  current = d.current;
  photos = offset === 0 ? d.photos : photos.concat(d.photos);
  renderGrid();
}

const loadMore = () => loadPage(photos.length).catch((e) => toast(e.message));
const reload = () => (tab === "collections" && !collectionId ? loadCollections() : loadPage(0))
  .catch((e) => toast(e.message));

// -- collections -----------------------------------------------------------
async function loadCollections() {
  const d = await api("/collections");
  collections = d.collections;
  const box = $("cols");
  box.innerHTML = "";
  collections.forEach((c) => {
    const el = document.createElement("button");
    el.className = "col";
    // Four covers, like a folder of photos. Filled in below once we know its members.
    el.innerHTML = `<div class="cover"></div>
      <div class="lab"><b>${c.name}</b><span>${c.count} photo${c.count === 1 ? "" : "s"}</span></div>`;
    el.onclick = () => openCollection(c.id);
    box.appendChild(el);
    api(`/library?limit=4&collection=${c.id}`).then((page) => {
      // One photo fills the cover; two or more tile it, like a folder of photos.
      const cover = el.querySelector(".cover");
      cover.style.gridTemplateColumns = page.photos.length > 1 ? "1fr 1fr" : "1fr";
      cover.innerHTML = page.photos.map(
        (p) => `<img loading="lazy" src="${p.thumb}?v=${Math.floor(p.added_at)}" alt="">`
      ).join("") || "<div></div>";
    }).catch(() => {});
  });
  const add = document.createElement("button");
  add.className = "col";
  add.innerHTML = `<div class="cover" style="place-items:center;grid-template-columns:1fr;
    font-size:2rem;color:var(--muted)">＋</div><div class="lab"><b>New collection</b>
    <span>group photos</span></div>`;
  add.onclick = newCollection;
  box.appendChild(add);
  $("empty").hidden = true;
  $("more").hidden = true;
}

async function newCollection() {
  const name = await ask({ title: "Name for the new collection", value: "",
                           ok: "Create" });
  if (!name) return;
  try {
    await api("/collections", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    await loadCollections();
  } catch (e) { toast(e.message); }
}

async function renameCollection() {
  const c = collections.find((x) => x.id === collectionId);
  const name = await ask({ title: "Rename collection", value: c ? c.name : "",
                           ok: "Rename" });
  if (!name) return;
  try {
    await api(`/collections/${collectionId}`, { method: "PATCH",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    $("crumb-name").textContent = name;
    collections = await api("/collections").then((d) => d.collections);
  } catch (e) { toast(e.message); }
}

async function removeCollection() {
  if (!await confirmAsk("Delete this collection? The photos stay in the library.",
                        "Delete")) return;
  try {
    await api(`/collections/${collectionId}`, { method: "DELETE" });
    openTab("collections");
  } catch (e) { toast(e.message); }
}

function openCollection(id) {
  collectionId = id;
  const c = collections.find((x) => x.id === id);
  $("crumb-name").textContent = c ? c.name : "";
  $("crumb").style.display = "flex";
  $("cols").style.display = "none";
  $("grid").style.display = "";
  loadPage(0).catch((e) => toast(e.message));
}

function openTab(next) {
  tab = next;
  collectionId = null;
  $("crumb").style.display = "none";
  $("tab-all").classList.toggle("sel", next === "all");
  $("tab-collections").classList.toggle("sel", next === "collections");
  $("grid").style.display = next === "all" ? "" : "none";
  $("cols").style.display = next === "collections" ? "" : "none";
  reload();
}

// -- the photo sheet ------------------------------------------------------
// Two views over one photo. "view" shows the chosen render (press and hold to compare
// it against the original); "crop" shows the photo behind a fixed-aspect frame you
// drag, zoom and turn. The frame is the panel's shape, so what you see inside it is
// exactly what the panel will get.
let mode = "view";
let crop = null;                 // [x, y, w, h] in ROTATED photo pixels
let srcSize = [0, 0];            // the rotated photo's bounding box
let canvasSize = [1600, 1200];   // the panel's working canvas
let viewT = { k: 1, tx: 0, ty: 0 };
let frameBox = { x: 0, y: 0, w: 0, h: 0 };
let renderSeq = 0;

const setBusyUI = (on) => {
  $("veil").classList.toggle("on", on);
  $("show-btn").disabled = on;
  $("b-crop").disabled = on;
};

async function openSheet(id) {
  $("sheet").classList.add("open");
  setBusyUI(true);
  try {
    sheet = await api(`/library/${id}`);
  } catch (e) { closeSheet(); return toast(e.message); }
  $("sheet-title").textContent = titleOf(sheet);

  const last = sheet.renders[sheet.renders.length - 1];
  want = {
    rotate: last ? last.rotate : 0,
    dither: last ? last.dither : (prefsNow.dither || "JARVIS_JUDICE_NINKE"),
    background: (last && last.background) || prefsNow.background || "white",
  };
  crop = last && last.crop ? last.crop.slice() : null;
  selectedKey = last ? last.key : null;
  canvasSize = (prefsNow.orientation === "portrait")
    ? [panelSize[1], panelSize[0]] : panelSize.slice();
  srcSize = rotatedSize([sheet.width, sheet.height], want.rotate);
  mode = "view";
  history = [];
  paintSheet();
  setBusyUI(false);
  // Nothing rendered yet: make one, so you see the e-paper version rather than the
  // photo — that is the thing you are actually choosing.
  if (!sheet.renders.length) { rerender(); }
}

// The bounding box a rotated photo occupies — and the pixel space `crop` is written
// in, so this has to agree with the server's `rotated_size()` exactly. Off the quarter
// turns that means reproducing PIL's expand box, corners rounded outward.
function rotatedSize(size, r) {
  const [w, h] = size, a = round1(norm360(r));
  if (a % 90 === 0) { return (a % 180) ? [h, w] : [w, h]; }
  const rad = a * Math.PI / 180;
  const c = Math.abs(Math.cos(rad)), s = Math.abs(Math.sin(rad));
  const bw = w * c + h * s, bh = w * s + h * c;
  return [Math.ceil(w / 2 + bw / 2) - Math.floor(w / 2 - bw / 2),
          Math.ceil(h / 2 + bh / 2) - Math.floor(h / 2 - bh / 2)];
}

function closeSheet() {
  $("sheet").classList.remove("open");
  sheet = null; mode = "view";
}

function paintSheet() {
  $("d-name").textContent = ditherLabel(want.dither);
  paintSwatch($("bg-sw"), want.background);
  // What the frame is padded with has to be visible while you are choosing the crop,
  // or you are deciding blind whether a gap looks right.
  $("cropwrap").style.background = BG_COLOURS[want.background] || "#fff";
  $("b-crop").classList.toggle("on", mode === "crop");
  $("stage-img").style.display = mode === "view" ? "" : "none";
  $("cropwrap").style.display = mode === "crop" ? "" : "none";
  $("foot-view").hidden = mode === "crop";
  $("foot-crop").hidden = mode !== "crop";
  if (mode === "crop") paintCropBar();
  if (mode === "view") {
    $("stage-img").src = selectedKey
      ? `/library/${sheet.id}/renders/${selectedKey}?v=${selectedKey}`
      : `/library/${sheet.id}/original`;
  }
  paintStrip();
  paintChips();
}

function paintStrip() {
  const strip = $("strip");
  strip.innerHTML = "";
  sheet.renders.forEach((r) => {
    const wrap = document.createElement("div");
    wrap.style.cssText = "position:relative;flex:none";
    const b = document.createElement("button");
    b.className = r.key === selectedKey ? "sel" : "";
    b.title = `${r.fit} · ${degText(r.rotate)} · ${ditherLabel(r.dither)}`
      + ` · ${bgLabel(r.background || "white")} background`;
    b.innerHTML = `<img draggable="false" alt=""
      src="${r.thumb}?v=${Math.floor(r.created_at)}">`;
    b.onclick = () => selectRender(r);
    const del = document.createElement("button");
    del.className = "ib danger";
    del.style.cssText = "position:absolute;top:-6px;right:-6px;width:1.25rem;"
      + "height:1.25rem;background:var(--sheet)";
    del.title = "Delete this version";
    del.innerHTML = icon("close");
    del.onclick = (e) => { e.stopPropagation(); dropRender(r.key); };
    wrap.append(b, del);
    strip.appendChild(wrap);
  });
}

function paintChips() {
  const chips = $("chips");
  chips.innerHTML = "";
  collections.forEach((c) => {
    const b = document.createElement("button");
    b.className = "chip" + (sheet.collections.includes(c.id) ? " on" : "");
    b.textContent = c.name;
    b.onclick = () => toggleCollection(c.id);
    chips.appendChild(b);
  });
}

function selectRender(r) {
  selectedKey = r.key;
  want = { rotate: r.rotate, dither: r.dither, background: r.background || "white" };
  crop = r.crop ? r.crop.slice() : null;
  srcSize = rotatedSize([sheet.width, sheet.height], want.rotate);
  mode = "view";
  paintSheet();
}

async function dropRender(key) {
  try {
    const d = await api(`/library/${sheet.id}/renders/${key}`, { method: "DELETE" });
    sheet = d.photo;
    if (selectedKey === key) {
      const last = sheet.renders[sheet.renders.length - 1];
      selectedKey = last ? last.key : null;
      if (last) { selectRender(last); return; }
    }
    paintSheet();
    toast("Version deleted");
  } catch (e) { toast(e.message); }
}

// -- crop editor -----------------------------------------------------------
// Placement is a set of gestures, not a row of presets: drag to move, pinch or scroll
// to zoom, twist or work the dial to straighten. What used to be the cover/contain
// buttons are DETENTS in the zoom instead — the scale snaps as it passes "fills the
// frame" and "the whole photo", so both are still one gesture away and land exactly.
const PX_PER_DEG = 4;   // how wide a degree is on the dial
const SNAP_DEG = 2;     // capture window around every 45°
const SNAP_K = 0.025;   // ... around the two frame-fitting scales, as a fraction
const SNAP_PX = 6;      // ... and around the frame's edges and centre
let history = [];       // {rotate, k, tx, ty}, pushed at the START of each gesture
let snapNote = "";
// Where the drag really is, before any magnetism. Snapping writes to viewT ONLY —
// feed a snapped position back into the next delta and the photo sticks: every small
// move starts from inside the capture window and re-snaps, so it takes a single big
// jerk to escape. Keeping the raw position makes the magnet a pure function of where
// your finger is, and you leave it the moment you pass the window.
let rawT = null;

const norm360 = (a) => ((a % 360) + 360) % 360;
const signedDeg = (a) => { const v = norm360(a); return v > 180 ? v - 360 : v; };
const round1 = (a) => Math.round(a * 10) / 10;
const degText = (a) => `${round1(signedDeg(a))}°`;

function toggleCrop() {
  if (mode === "crop") { mode = "view"; paintSheet(); rerender(); return; }
  mode = "crop";
  history = [];
  snapNote = "";
  $("cropimg").src = `/library/${sheet.id}/original`;
  paintSheet();
  if (!layoutStage()) return;   // no size yet — the observer below finishes the job
  if (!crop) { crop = defaultCrop(srcSize, canvasSize, "cover"); }
  applyCrop();
}

// The stage has no size until the browser has laid the sheet out, and laying the crop
// frame out against a zero-sized box would set the scale to 0 and hide the photo.
// Watching the element is the only reliable moment to do it.
new ResizeObserver(() => {
  if (!sheet || mode !== "crop") return;
  const keep = crop && crop.slice();
  if (!layoutStage()) return;
  crop = keep || defaultCrop(srcSize, canvasSize, "cover");
  applyCrop();
}).observe($("cropwrap"));

// Where the frame and the photo SIT. Separate from applyCrop() because an angle change
// resizes the photo's box without meaning to throw the view away.
function layoutStage() {
  const wrap = $("cropwrap");
  const sw = wrap.clientWidth, sh = wrap.clientHeight;
  if (!sw || !sh) return false;
  // The frame is the panel's shape, at 78% of the stage — enough room around it to
  // see what you are cropping away.
  const aspect = canvasSize[0] / canvasSize[1];
  let fw = sw * 0.78, fh = fw / aspect;
  if (fh > sh * 0.78) { fh = sh * 0.78; fw = fh * aspect; }
  frameBox = { x: (sw - fw) / 2, y: (sh - fh) / 2, w: fw, h: fh };
  const f = $("frame");
  f.style.left = `${frameBox.x}px`; f.style.top = `${frameBox.y}px`;
  f.style.width = `${frameBox.w}px`; f.style.height = `${frameBox.h}px`;

  const [rw, rh] = srcSize;
  const rot = $("rot"), img = $("cropimg");
  rot.style.width = `${rw}px`; rot.style.height = `${rh}px`;
  // The wrapper is the photo's BOUNDING BOX; the photo sits centred inside it, turned
  // about its own middle — exactly what PIL's rotate(expand=True) produces. That keeps
  // every coordinate below in bounding-box pixels, which is what the API wants.
  img.style.width = `${sheet.width}px`; img.style.height = `${sheet.height}px`;
  img.style.left = `${(rw - sheet.width) / 2}px`;
  img.style.top = `${(rh - sheet.height) / 2}px`;
  img.style.transform = `rotate(${want.rotate}deg)`;
  return true;
}

function defaultCrop(size, canvas, fit) {
  const [iw, ih] = size, a = iw / ih, c = canvas[0] / canvas[1];
  let w, h;
  if (fit === "contain") { [w, h] = a > c ? [iw, iw / c] : [ih * c, ih]; }
  else { [w, h] = a > c ? [ih * c, ih] : [iw, iw / c]; }
  return [(iw - w) / 2, (ih - h) / 2, w, h];
}

function applyCrop() {
  if (!frameBox.w || !crop || !crop[2]) return;
  viewT.k = frameBox.w / crop[2];
  viewT.tx = frameBox.x - crop[0] * viewT.k;
  viewT.ty = frameBox.y - crop[1] * viewT.k;
  syncRaw(); paintTransform();
}

function paintTransform() {
  $("rot").style.transform =
    `translate(${viewT.tx}px, ${viewT.ty}px) scale(${viewT.k})`;
  crop = [
    (frameBox.x - viewT.tx) / viewT.k,
    (frameBox.y - viewT.ty) / viewT.k,
    frameBox.w / viewT.k,
    frameBox.h / viewT.k,
  ];
  paintHint();
}

function paintHint() {
  const note = document.body.classList.contains("rotating")
    ? degText(want.rotate) : snapNote;
  const h = $("hint");
  h.textContent = note || "drag to move · pinch or scroll to zoom · twist to straighten";
  h.classList.toggle("snap", !!note);
}

function clampView() {
  if (!frameBox.w) return;  // not laid out yet — clamping now would zero the scale
  // Keep the photo overlapping the frame, and the zoom sane in both directions.
  const minK = frameBox.w / (srcSize[0] * 6);
  const maxK = frameBox.w / 24;
  viewT.k = Math.min(maxK, Math.max(minK, viewT.k));
  const w = srcSize[0] * viewT.k, h = srcSize[1] * viewT.k;
  const slackX = frameBox.w * 0.9, slackY = frameBox.h * 0.9;
  viewT.tx = Math.min(frameBox.x + slackX, Math.max(frameBox.x + frameBox.w - w - slackX, viewT.tx));
  viewT.ty = Math.min(frameBox.y + slackY, Math.max(frameBox.y + frameBox.h - h - slackY, viewT.ty));
}

// -- rotation --------------------------------------------------------------
const frameCentre = () =>
  ({ x: frameBox.x + frameBox.w / 2, y: frameBox.y + frameBox.h / 2 });

// The photo turns inside its own bounding box, and `crop` is written in that box's
// pixels — so changing the angle moves every coordinate. These two convert between a
// point of the UNROTATED photo and where it currently sits on the stage, which is what
// lets a rotation keep whatever is under the frame's centre exactly where it is.
// CSS rotate() is clockwise with y down: [c, -s; s, c].
function photoPointAt(scr) {
  const th = want.rotate * Math.PI / 180, c = Math.cos(th), s = Math.sin(th);
  const ux = (scr.x - viewT.tx) / viewT.k - srcSize[0] / 2;
  const uy = (scr.y - viewT.ty) / viewT.k - srcSize[1] / 2;
  return { x: c * ux + s * uy + sheet.width / 2,
           y: -s * ux + c * uy + sheet.height / 2 };
}

function placePhotoPointAt(p, scr) {
  const th = want.rotate * Math.PI / 180, c = Math.cos(th), s = Math.sin(th);
  const dx = p.x - sheet.width / 2, dy = p.y - sheet.height / 2;
  viewT.tx = scr.x - viewT.k * (c * dx - s * dy + srcSize[0] / 2);
  viewT.ty = scr.y - viewT.k * (s * dx + c * dy + srcSize[1] / 2);
}

function setAngle(next) {
  const at = frameCentre();
  const anchor = photoPointAt(at);
  want.rotate = round1(norm360(next));
  srcSize = rotatedSize([sheet.width, sheet.height], want.rotate);
  layoutStage();
  placePhotoPointAt(anchor, at);
  clampView(); syncRaw(); paintTransform(); paintCropBar();
}

// Detents every 45°, so 0/45/90/… are the angles you get by aiming near them and the
// ones in between still need only a nudge past the window.
function snapAngle(a) {
  const sd = signedDeg(a), detent = Math.round(sd / 45) * 45;
  return Math.abs(sd - detent) <= SNAP_DEG ? detent : sd;
}

// The smallest scale at which the photo still covers the whole frame at this angle:
// turn the FRAME by -angle instead and require its bounding box to fit inside the
// photo. Conservative when tilted, which is the safe direction — it never leaves white.
function coverScale() {
  const th = want.rotate * Math.PI / 180;
  const c = Math.abs(Math.cos(th)), s = Math.abs(Math.sin(th));
  return Math.max((frameBox.w * c + frameBox.h * s) / sheet.width,
                  (frameBox.w * s + frameBox.h * c) / sheet.height);
}
const containScale = () => Math.min(frameBox.w / srcSize[0], frameBox.h / srcSize[1]);

function snapScale(k) {
  for (const [target, note] of [[coverScale(), "fills the frame"],
                                [containScale(), "the whole photo"]]) {
    if (target > 0 && Math.abs(k - target) / target < SNAP_K) {
      snapNote = note;
      return target;
    }
  }
  return k;
}

// Magnetism against the frame: its edges and its centre, per axis. Tilted, the photo's
// "edges" are its bounding box's — an approximation while straightening, and exact
// again at every quarter turn, which is when edge alignment actually matters.
function snapTranslate() {
  if (!frameBox.w) return;
  const w = srcSize[0] * viewT.k, h = srcSize[1] * viewT.k;
  const pull = (lo, size, flo, fsize) => {
    let best = 0;
    for (const d of [flo - lo, flo + fsize - (lo + size),
                     flo + fsize / 2 - (lo + size / 2)]) {
      if (Math.abs(d) <= SNAP_PX && (!best || Math.abs(d) < Math.abs(best))) best = d;
    }
    return best;
  };
  const dx = pull(viewT.tx, w, frameBox.x, frameBox.w);
  const dy = pull(viewT.ty, h, frameBox.y, frameBox.h);
  viewT.tx += dx; viewT.ty += dy;
  if ((dx || dy) && !snapNote) snapNote = "aligned to the frame";
}

function zoomTo(k) {
  const factor = k / viewT.k;
  if (!isFinite(factor) || factor <= 0) return;
  const c = frameCentre();
  viewT.tx = c.x - (c.x - viewT.tx) * factor;
  viewT.ty = c.y - (c.y - viewT.ty) * factor;
  viewT.k = k;
  clampView(); syncRaw(); paintTransform();
}

// Anything that moves the photo other than a drag — a zoom, a rotation, an undo —
// redefines where the drag would be resuming from.
function syncRaw() {
  if (rawT) { rawT.tx = viewT.tx; rawT.ty = viewT.ty; }
}

// -- undo ------------------------------------------------------------------
// One entry per GESTURE, not per pointer event: a drag that moved the photo across the
// stage is one thing you did, and undoing it a pixel at a time would be useless.
function pushHistory() {
  history.push({ rotate: want.rotate, k: viewT.k, tx: viewT.tx, ty: viewT.ty });
  if (history.length > 60) history.shift();
  paintCropBar();
}

function undo() {
  const step = history.pop();
  if (!step) return;
  want.rotate = step.rotate;
  srcSize = rotatedSize([sheet.width, sheet.height], want.rotate);
  layoutStage();
  viewT = { k: step.k, tx: step.tx, ty: step.ty };
  snapNote = "";
  syncRaw(); paintTransform(); paintCropBar();
}

function resetCrop() {
  pushHistory();
  want.rotate = 0;
  srcSize = rotatedSize([sheet.width, sheet.height], 0);
  layoutStage();
  crop = defaultCrop(srcSize, canvasSize, "cover");
  snapNote = "";
  applyCrop(); paintCropBar();
}

// -- the dial --------------------------------------------------------------
function buildDial() {
  const ticks = $("ticks");
  if (ticks.childElementCount) return;
  let html = "";
  for (let a = -180; a <= 180; a += 5) {
    const major = a % 45 === 0;
    html += `<i class="${major ? "maj" : ""}" style="left:${a * PX_PER_DEG}px">`
      + (major ? `<b>${a}</b>` : "") + "</i>";
  }
  ticks.innerHTML = html;
}

function paintCropBar() {
  const a = signedDeg(want.rotate);
  const onDetent = Math.abs(a - Math.round(a / 45) * 45) < 0.05;
  $("deg").textContent = degText(want.rotate);
  $("deg").classList.toggle("snap", onDetent);
  $("ticks").style.transform = `translateX(${-a * PX_PER_DEG}px)`;
  $("b-undo").disabled = !history.length;
}

(function straighten() {
  const el = $("dial");
  let start = null;
  el.addEventListener("pointerdown", (e) => {
    el.setPointerCapture(e.pointerId);
    pushHistory();
    start = { x: e.clientX, a: signedDeg(want.rotate) };
    document.body.classList.add("rotating");
    paintHint();
  });
  el.addEventListener("pointermove", (e) => {
    if (!start) return;
    // The ruler follows your finger, so the picture turns the other way. Stops at ±180
    // rather than wrapping, which would make the ticks jump mid-drag.
    const raw = Math.max(-180, Math.min(180,
      start.a - (e.clientX - start.x) / PX_PER_DEG));
    setAngle(snapAngle(raw));
  });
  const end = () => {
    if (!start) return;
    start = null;
    document.body.classList.remove("rotating");
    paintHint();
  };
  ["pointerup", "pointercancel"].forEach((ev) => el.addEventListener(ev, end));
})();

// -- drag, pinch, twist ----------------------------------------------------
(function gestures() {
  const wrap = $("cropwrap");
  const points = new Map();
  let base = null, wheelAt = 0;
  const pair = () => [...points.values()];
  const dist = () => { const [a, b] = pair(); return Math.hypot(a.x - b.x, a.y - b.y); };
  const twist = () => {
    const [a, b] = pair();
    return Math.atan2(b.y - a.y, b.x - a.x) * 180 / Math.PI;
  };

  wrap.addEventListener("pointerdown", (e) => {
    wrap.setPointerCapture(e.pointerId);
    if (!points.size) { pushHistory(); rawT = { tx: viewT.tx, ty: viewT.ty }; }
    points.set(e.pointerId, { x: e.clientX, y: e.clientY });
    wrap.classList.add("grabbing");
    if (points.size === 2) {
      // Both deltas are measured from the moment the second finger landed, so a snap
      // never sticks: the next move recomputes from the raw gesture, not from where
      // the snap put it.
      base = { d: dist(), k: viewT.k, a: twist(), rot: signedDeg(want.rotate) };
      document.body.classList.add("rotating");
    }
  });
  wrap.addEventListener("pointermove", (e) => {
    if (!points.has(e.pointerId)) return;
    const prev = points.get(e.pointerId);
    points.set(e.pointerId, { x: e.clientX, y: e.clientY });
    snapNote = "";
    if (points.size === 2 && base) {
      zoomTo(snapScale(base.k * (dist() / base.d)));
      setAngle(snapAngle(base.rot + (twist() - base.a)));
    } else {
      rawT.tx += e.clientX - prev.x;
      rawT.ty += e.clientY - prev.y;
      viewT.tx = rawT.tx; viewT.ty = rawT.ty;
      clampView(); syncRaw();   // the clamp is real movement; the snap below is not
      snapTranslate(); paintTransform();
    }
  });
  const up = (e) => {
    points.delete(e.pointerId);
    if (points.size < 2) { base = null; document.body.classList.remove("rotating"); }
    if (!points.size) {
      wrap.classList.remove("grabbing"); rawT = null; snapNote = ""; paintHint();
    }
  };
  wrap.addEventListener("pointerup", up);
  wrap.addEventListener("pointercancel", up);
  wrap.addEventListener("wheel", (e) => {
    e.preventDefault();
    const now = performance.now();
    if (now - wheelAt > 500) pushHistory();   // one entry per burst, not per notch
    wheelAt = now;
    snapNote = "";
    zoomTo(snapScale(viewT.k * Math.exp(-e.deltaY / 400)));
  }, { passive: false });
})();

// -- press and hold to compare with the source -----------------------------
// Against the SAME framing, not the raw photo: `renders/<key>/source` is the identical
// crop, angle, canvas and background with the dithering left off, so the only thing
// that changes under your finger is the thing you are judging. (It used to swap in
// `/original`, which jumped to a completely different picture.)
(function compare() {
  const img = $("stage-img");
  let hold = null, showingSource = false;
  const rendered = () => selectedKey
    ? `/library/${sheet.id}/renders/${selectedKey}?v=${selectedKey}`
    : `/library/${sheet.id}/original`;
  const source = () => selectedKey
    ? `/library/${sheet.id}/renders/${selectedKey}/source`
    : `/library/${sheet.id}/original`;
  const show = (on) => {
    if (!sheet || on === showingSource) return;
    showingSource = on;
    img.src = on ? source() : rendered();
  };
  img.addEventListener("pointerdown", (e) => {
    if (mode !== "view") return;
    e.preventDefault();
    hold = setTimeout(() => show(true), 140);
  });
  const release = () => { clearTimeout(hold); show(false); };
  ["pointerup", "pointercancel", "pointerleave"].forEach(
    (ev) => img.addEventListener(ev, release));
})();

// -- rendering -------------------------------------------------------------
async function rerender() {
  if (!sheet) return;
  const seq = ++renderSeq;
  const q = new URLSearchParams({
    rotate: want.rotate, dither: want.dither, background: want.background,
    show: "false",
  });
  if (crop) { q.set("crop", crop.map((v) => Math.round(v)).join(",")); }
  setBusyUI(true);
  try {
    const d = await api(`/library/${sheet.id}/render?${q}`, { method: "POST" });
    if (seq !== renderSeq) return;   // a newer adjustment already won
    sheet = d.photo;
    want = { rotate: d.rotate, dither: d.dither, background: d.background || "white" };
    crop = d.crop ? d.crop.slice() : crop;
    canvasSize = d.canvas || canvasSize;
    srcSize = d.source || srcSize;
    selectedKey = d.render ? d.render.key : null;
    mode = "view";
    paintSheet();
  } catch (e) { toast(e.message); }
  if (seq === renderSeq) setBusyUI(false);
}

async function showCurrent() {
  if (!sheet) return;
  setBusyUI(true);
  try {
    if (!selectedKey) { await rerender(); }
    const q = selectedKey ? `?render=${selectedKey}` : "";
    const d = await api(`/display/library/${sheet.id}${q}`, { method: "POST" });
    current = { photo_id: sheet.id, render_key: d.render ? d.render.key : null };
    busy = true; markCurrent(); poll();
    toast(`Showing ${titleOf(sheet)}`);
    closeSheet();
  } catch (e) { toast(e.message); setBusyUI(false); }
}

// -- background picker -----------------------------------------------------
// Only the panel's six colours: one of them dithers to a FLAT block, anything else to
// a stipple of the six — which is not a background, it's noise.
const BG_COLOURS = {
  white: "#ffffff", black: "#000000", red: "#ff0000",
  green: "#00d000", blue: "#0000ff", yellow: "#ffe000",
};
const bgLabel = (name) => name.charAt(0).toUpperCase() + name.slice(1);

function paintSwatch(el, name) {
  if (!el) return;
  el.style.background = BG_COLOURS[name] || "#fff";
}

function openBg() {
  const box = $("bgopts");
  box.innerHTML = "";
  const names = (bgOptions.length ? bgOptions : Object.keys(BG_COLOURS));
  names.forEach((name) => {
    const b = document.createElement("button");
    b.className = "bgopt" + (name === want.background ? " sel" : "");
    b.innerHTML = `<i class="sw"></i><span>${bgLabel(name)}</span>`;
    paintSwatch(b.querySelector(".sw"), name);
    b.onclick = () => {
      closeBg();
      if (name === want.background) return;
      want.background = name;
      // Repaint the stage straight away; the render follows.
      $("cropwrap").style.background = BG_COLOURS[name] || "#fff";
      paintSwatch($("bg-sw"), name);
      if (mode !== "crop") rerender();
    };
    box.appendChild(b);
  });
  $("bgpick").classList.add("open");
}
const closeBg = () => $("bgpick").classList.remove("open");

// -- dither picker ---------------------------------------------------------
function openDither() {
  const box = $("dopts");
  box.innerHTML = "";
  const order = Object.keys(DITHER_INFO).filter((k) => ditherOptions.includes(k))
    .concat(ditherOptions.filter((k) => !DITHER_INFO[k]));
  order.forEach((name) => {
    const [label, blurb] = DITHER_INFO[name] || [name, ""];
    const b = document.createElement("button");
    b.className = "dopt" + (name === want.dither ? " sel" : "");
    b.innerHTML = `<b>${label}</b>${name === want.dither ? icon("check") : "<i></i>"}`
      + `<span>${blurb}</span>`;
    b.onclick = () => {
      closeDither();
      if (name === want.dither) return;
      want.dither = name;
      rerender();
    };
    box.appendChild(b);
  });
  paintIcons(box);
  $("dpick").classList.add("open");
}
const closeDither = () => $("dpick").classList.remove("open");

async function toggleCollection(id) {
  const next = sheet.collections.includes(id)
    ? sheet.collections.filter((c) => c !== id)
    : sheet.collections.concat([id]);
  try {
    sheet = await api(`/library/${sheet.id}`, { method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ collections: next }) });
    paintChips();
    collections = await api("/collections").then((d) => d.collections);
  } catch (e) { toast(e.message); }
}

async function renamePhoto() {
  if (!sheet) return;
  const name = await ask({ title: "Rename photo", value: sheet.name || "",
                           ok: "Rename" });
  if (name === null) return;
  try {
    sheet = await api(`/library/${sheet.id}`, { method: "PATCH",
      headers: { "Content-Type": "application/json" },
      // "" clears the label, which falls back to the date the photo arrived.
      body: JSON.stringify({ name: name || null }) });
    $("sheet-title").textContent = titleOf(sheet);
    const cell = document.querySelector(`.cell[data-id="${sheet.id}"]`);
    if (cell) cell.title = titleOf(sheet);
    const listed = photos.find((p) => p.id === sheet.id);
    if (listed) listed.name = sheet.name;
    poll();
  } catch (e) { toast(e.message); }
}

async function deletePhoto() {
  if (!sheet) return;
  if (!await confirmAsk(`Delete "${titleOf(sheet)}"? Every version of it goes too.`,
                        "Delete")) return;
  try {
    await api(`/library/${sheet.id}`, { method: "DELETE" });
    closeSheet(); toast("Deleted"); await reload();
  } catch (e) { toast(e.message); }
}

// -- panel actions ---------------------------------------------------------
async function nav(direction) {
  try {
    const r = await api(`/display/nav?direction=${direction}`, { method: "POST" });
    current = { photo_id: r.photo.id, render_key: r.render ? r.render.key : null };
    busy = true; markCurrent(); poll();
  } catch (e) { toast(e.message); }
}

async function clearPanel() {
  if (!await confirmAsk("Blank the panel to white?", "Clear")) return;
  try { await api("/display/clear", { method: "POST" }); busy = true; poll(); }
  catch (e) { toast(e.message); }
}

// -- upload ----------------------------------------------------------------
// Uploading only FILES the photos: no render, no panel refresh. A selection can be
// dozens of images and a refresh is ~30 s, so choosing what to show is a separate,
// deliberate act in the sheet.
async function upload(files) {
  const list = [...files].filter(
    (f) => f.type.startsWith("image/") || /\.(jpe?g|png|webp|heic|heif)$/i.test(f.name));
  if (!list.length) { toast("No images in that selection"); return; }
  let added = 0, failed = 0;
  const bar = $("bar");
  bar.style.width = "0";
  for (const [i, file] of list.entries()) {
    bar.style.width = `${Math.round((i / list.length) * 100)}%`;
    const q = new URLSearchParams();
    // The filename makes a decent label, unless it is the "image.jpg" placeholder iOS
    // hands out from the photo picker.
    const stem = file.name.replace(/\.[^.]+$/, "").trim();
    if (stem && !/^(image|photo|img|unknown|untitled)$/i.test(stem)) q.set("name", stem);
    if (collectionId) q.set("collection", collectionId);
    toast(list.length > 1 ? `Uploading ${i + 1} of ${list.length}…` : `Uploading ${file.name}…`);
    try {
      await api(`/library?${q}`, { method: "POST", body: file,
        headers: file.type ? { "Content-Type": file.type } : {} });
      added++;
    } catch (e) { failed++; toast(`${file.name}: ${e.message}`); }
  }
  bar.style.width = "100%";
  setTimeout(() => { bar.style.width = "0"; }, 400);
  if (added) {
    toast(`Added ${added} photo${added > 1 ? "s" : ""}${failed ? `, ${failed} failed` : ""}`);
    if (tab !== "all" && !collectionId) { openTab("all"); } else { await reload(); }
    poll();
  }
}

let dragDepth = 0;
document.addEventListener("dragenter", (e) => {
  if (![...e.dataTransfer.types].includes("Files")) return;
  dragDepth++; document.body.classList.add("dragging");
});
document.addEventListener("dragover", (e) => {
  if ([...e.dataTransfer.types].includes("Files")) e.preventDefault();
});
document.addEventListener("dragleave", () => {
  if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragging"); }
});
document.addEventListener("drop", (e) => {
  if (![...e.dataTransfer.types].includes("Files")) return;
  e.preventDefault();
  if (sheet) { dragDepth = 0; document.body.classList.remove("dragging"); return; }
  dragDepth = 0; document.body.classList.remove("dragging");
  upload(e.dataTransfer.files);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("ask").classList.contains("open")) { askDone(null); return; }
  if (e.key === "z" && (e.metaKey || e.ctrlKey) && mode === "crop") {
    e.preventDefault(); undo(); return;
  }
  if (e.key !== "Escape") return;
  if ($("bgpick").classList.contains("open")) { closeBg(); }
  else if ($("dpick").classList.contains("open")) { closeDither(); }
  else if (mode === "crop") { toggleCrop(); }
  else if (sheet) { closeSheet(); }
});
// The other half of the iOS press-and-hold fix: -webkit-touch-callout stops the share
// sheet, this stops the desktop/Android context menu on the same gesture.
$("sheet").addEventListener("contextmenu", (e) => e.preventDefault());
// The frame is sized from the stage, so a device rotation or a resized window has to
// redo it — keeping the rectangle you chose, which is in photo pixels either way.
addEventListener("resize", () => {
  if (!sheet || mode !== "crop") return;
  const keep = crop && crop.slice();
  if (!layoutStage()) return;
  crop = keep || defaultCrop(srcSize, canvasSize, "cover");
  applyCrop();
});

paintIcons();
buildDial();
// Status first: it carries the panel size, the dither list and the prefs the sheet
// needs before anything can be opened.
poll()
  .then(() => api("/collections"))
  .then((d) => { collections = d.collections; })
  .catch(() => {})
  .then(reload);
</script>
</body></html>
"""
