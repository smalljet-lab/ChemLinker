/** The panel's only stylesheet, set as the textContent of one <style> in the shadow root. */

export const PANEL_CSS = `
:host { all: initial; }
.root {
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 13px;
  line-height: 1.4;
  color: #1f2328;
  -webkit-font-smoothing: antialiased;
}
*, *::before, *::after { box-sizing: border-box; }
[hidden] { display: none !important; }
button, input, select, textarea { font: inherit; color: inherit; }
:focus-visible { outline: 3px solid #0b5cad; outline-offset: 2px; }

.launcher {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 44px; height: 44px; padding: 0 14px;
  border-radius: 22px; border: 1px solid #0a4f94;
  background: #0b5cad; color: #ffffff;
  font-weight: 700; font-size: 15px; letter-spacing: 0.04em;
  cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,0.25);
}
.launcher:hover { background: #0a4f94; }

.panel {
  width: 380px; max-width: calc(100vw - 32px);
  max-height: calc(100vh - 32px);
  overflow-y: auto; overscroll-behavior: contain;
  background: #ffffff; border: 1px solid #c4c9d0; border-radius: 8px;
  box-shadow: 0 6px 24px rgba(0,0,0,0.25);
  padding: 12px;
}
.panel-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.panel-head h2 { margin: 0; font-size: 15px; font-weight: 700; }
.icon-btn {
  min-width: 28px; height: 28px; border-radius: 4px; border: 1px solid transparent;
  background: transparent; cursor: pointer; font-size: 18px; line-height: 1; color: #1f2328;
}
.icon-btn:hover { background: #eef1f4; }

h3 { margin: 0 0 6px; font-size: 13px; font-weight: 700; }
h4 { margin: 8px 0 4px; font-size: 12px; font-weight: 700; color: #3d444d; }
p { margin: 4px 0; }
.small { font-size: 11px; color: #4b535d; }
.mono { font-family: ui-monospace, "SFMono-Regular", Consolas, "Liberation Mono", monospace; overflow-wrap: anywhere; word-break: break-all; }
.section { margin-top: 10px; padding-top: 10px; border-top: 1px solid #e3e6ea; }

.btn {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 5px 10px; border-radius: 4px; border: 1px solid #8c959f;
  background: #f6f8fa; color: #1f2328; cursor: pointer;
}
.btn:hover:not(:disabled) { background: #eaeef2; }
.btn.primary { background: #1a7f37; border-color: #116329; color: #ffffff; font-weight: 600; }
.btn.primary:hover:not(:disabled) { background: #116329; }
.btn:disabled { opacity: 0.55; cursor: not-allowed; }
.row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 6px; }

label.field { display: block; margin-top: 8px; font-weight: 600; font-size: 12px; }
label.field .hint { font-weight: 400; color: #4b535d; }
input[type="text"], input[type="number"], textarea, select {
  display: block; width: 100%; margin-top: 3px; padding: 5px 6px;
  border: 1px solid #8c959f; border-radius: 4px; background: #ffffff;
}
textarea { min-height: 84px; resize: vertical; white-space: pre; overflow: auto; }
.fields-inline { display: grid; grid-template-columns: 1fr 1fr 80px; gap: 6px; }
.dropzone {
  position: relative;
  margin-top: 6px; padding: 8px; border: 2px dashed #8c959f; border-radius: 6px;
  text-align: center; color: #3d444d; background: #f6f8fa;
}
.dropzone.over { border-color: #0b5cad; background: #ddf4ff; }
.file-label { color: #0b5cad; text-decoration: underline; cursor: pointer; }
.file-input { position: absolute; width: 1px; height: 1px; opacity: 0; overflow: hidden; }
.file-input:focus-visible + .file-label { outline: 3px solid #0b5cad; outline-offset: 2px; }

.banner { margin-top: 6px; padding: 6px 8px; border-radius: 4px; border: 1px solid; overflow-wrap: anywhere; }
.banner.warn { background: #fff8c5; border-color: #9a6700; color: #3b2300; }
.banner.info { background: #ddf4ff; border-color: #54aeff; color: #0a3069; }
.banner.ok { background: #dafbe1; border-color: #4ac26b; color: #0f3d1d; }
.banner.error { background: #ffebe9; border-color: #cf222e; color: #5c0b10; }
.banner ul { margin: 4px 0 0; padding-left: 18px; }
.banner strong { display: block; }

.card { margin-top: 8px; padding: 8px; border: 1px solid #c4c9d0; border-radius: 6px; background: #ffffff; }
.card.skipped { background: #f6f8fa; }
.card-head { display: flex; align-items: center; gap: 8px; }
.card-title { min-width: 0; }
.card-title h3 { margin: 0; }
.analysis.stale { opacity: 0.5; filter: grayscale(1); }
.stale-note { margin-top: 6px; font-weight: 600; color: #6e4b00; }
.struct { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
.thumb { width: 100%; height: auto; aspect-ratio: 3 / 2; object-fit: contain; border: 1px solid #e3e6ea; border-radius: 4px; background: #ffffff; flex: none; }
.thumb.small { width: 96px; height: 64px; aspect-ratio: auto; }
dl.facts { margin: 0; display: grid; grid-template-columns: auto 1fr; gap: 2px 8px; min-width: 0; }
dl.facts dt { font-weight: 600; color: #3d444d; }
dl.facts dd { margin: 0; min-width: 0; }
.status { margin-top: 6px; font-weight: 600; }
.id-box { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
.big-id { font-size: 20px; font-weight: 700; user-select: all; }
ul.list { margin: 4px 0 0; padding-left: 18px; }
ul.list li { margin: 2px 0; }
.variant { display: flex; gap: 6px; align-items: center; list-style: none; margin-left: -18px; }
.retired { color: #8c1a1a; font-weight: 600; }
.ignored { margin-top: 8px; }
.check { display: flex; gap: 6px; align-items: flex-start; margin-top: 4px; font-weight: 400; }
.check input { margin-top: 2px; flex: none; }
.confirm { margin-top: 8px; padding: 8px; border: 2px solid #9a6700; border-radius: 6px; background: #fff8c5; color: #3b2300; }
.blockers { margin: 4px 0 0; padding-left: 18px; color: #4b535d; font-size: 12px; }
.code { font-size: 11px; color: #4b535d; }
a { color: #0b5cad; }
.sr-status { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.status-line { margin-top: 8px; font-size: 12px; color: #3d444d; min-height: 1em; }

.panel-head { align-items: flex-start; }
.panel-title { min-width: 0; }
.experiment { margin: 2px 0 0; font-weight: 600; color: #3d444d; overflow-wrap: anywhere; }
.badge {
  display: inline-block; min-width: 18px; padding: 0 5px; border-radius: 9px;
  background: #ffffff; color: #0b5cad; font-size: 12px; line-height: 18px; text-align: center;
}
.badge.done { color: #1a7f37; }
.busy { color: #3d444d; font-style: italic; margin: 2px 0 0; }
.summary { margin: 4px 0 0; color: #3d444d; }
.banner.compact { padding: 3px 6px; font-size: 12px; }
.disclosure {
  display: flex; align-items: center; gap: 6px; width: 100%; padding: 4px 0;
  border: 0; background: transparent; cursor: pointer; color: #3d444d; font-weight: 600; text-align: left;
}
.disclosure::before { content: "+"; display: inline-block; width: 1em; text-align: center; }
.disclosure[aria-expanded="true"]::before { content: "\\2212"; }
.advanced-toggle { padding-top: 6px; }
.advanced-card { margin-top: 6px; padding-top: 6px; border-top: 1px dashed #d0d7de; }
.manual { margin-top: 8px; }

.also { margin: 4px 0 0; overflow-wrap: anywhere; }
.search { padding-top: 6px; margin-top: 8px; }
.search label.field { margin-top: 0; }
.search-row { display: flex; gap: 6px; align-items: center; margin-top: 3px; }
.search-row input[type="text"] { flex: 1 1 auto; min-width: 0; margin-top: 0; }
.small-btn { padding: 2px 8px; font-size: 12px; }
.found { margin-top: 6px; }
.found h3 { margin: 6px 0 4px; }
.found-code { font-weight: 600; }
ul.found-batches { margin: 2px 0 4px; padding-left: 16px; font-size: 12px; }
.retired-batch { color: #6e7781; }
.other-variants { margin-top: 6px; }

.nav-row { margin-top: 6px; }
.nav-feedback:empty { display: none; }
.details-toggle { margin-top: 6px; }
.details-block { margin-top: 4px; }
dl.details dd { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 6px; }
dl.details dd > .mono { flex: 1 1 100%; }
.copy { display: inline-flex; align-items: baseline; gap: 4px; }
.copy-feedback:empty { display: none; }
.name-row { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; min-width: 0; }
.name-row.editing { display: block; }
.name-row .name-text { flex: 1 1 100%; font-weight: 600; overflow-wrap: anywhere; }
.name-row label.field { margin-top: 0; }
.name-row > p, .name-row > .banner { flex: 1 1 100%; margin: 2px 0 0; }
.name-row .name-source { font-style: italic; }
.row.inline { margin-top: 0; }
.problem { color: #8c1a1a; font-weight: 600; }
.yield-line { margin: 2px 0 0; }
.yield-note { margin: 4px 0 0; color: #6e4b00; }
.found-yield { font-weight: 400; color: #3d444d; }
`;
