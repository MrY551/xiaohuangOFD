/* ==========================================================================
   OFD 网页端主逻辑。与服务端 web/server.py 的接口一一对应。
   ========================================================================== */
'use strict';

const $ = (id) => document.getElementById(id);
const MM_PER_INCH = 25.4;
const BASE_PX_PER_MM = 96 / MM_PER_INCH;     // 96dpi 下 1mm = 3.7795px

const state = {
  doc: null,
  page: null,
  pageIndex: 0,
  zoom: 1.0,            // 1.0 = 96dpi 显示；范围 0.2 ~ 6
  selection: [],        // [{id, type, corners, label}]
  mode: 'select',       // select | place-text | place-rect
  meta: null,
  dragging: null,
  placing: null,
  fonts: {},
};

const renderer = new window.SceneRenderer();
let ctx = null;

/* ------------------------------------------------------------------ 工具 */

function toast(msg, isErr) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast' + (isErr ? ' err' : '');
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, isErr ? 5200 : 2600);
}

function setStatus(left, right) {
  if (left !== undefined) $('statusLeft').textContent = left;
  if (right !== undefined) $('statusRight').textContent = right;
}

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, opt);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok || !data || data.ok === false) {
    const msg = (data && data.error) || ('HTTP ' + res.status);
    throw new Error(msg);
  }
  return data;
}

function parseRanges(text, total) {
  if (!text || !text.trim()) return Array.from({ length: total }, (_, i) => i);
  const out = new Set();
  for (const part of text.split(',')) {
    const s = part.trim();
    if (!s) continue;
    const m = s.match(/^(\d+)\s*-\s*(\d+)$/);
    if (m) {
      const a = parseInt(m[1], 10), b = parseInt(m[2], 10);
      for (let i = Math.min(a, b); i <= Math.max(a, b); i++) {
        if (i >= 1 && i <= total) out.add(i - 1);
      }
    } else if (/^\d+$/.test(s)) {
      const i = parseInt(s, 10);
      if (i >= 1 && i <= total) out.add(i - 1);
    }
  }
  return Array.from(out).sort((a, b) => a - b);
}

function hexToRgba(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255, 1];
}

function rgbaToHex(rgba) {
  if (!rgba) return '#000000';
  const f = (v) => Math.round(Math.max(0, Math.min(1, v)) * 255).toString(16).padStart(2, '0');
  return '#' + f(rgba[0]) + f(rgba[1]) + f(rgba[2]);
}

/* ------------------------------------------------------------------ 启动 */

async function boot() {
  const cv = $('cv');
  ctx = cv.getContext('2d');
  bindEvents();
  try {
    state.meta = await api('/api/meta');
    state.fonts = state.meta.fonts || {};
    setStatus('就绪', `字体 ${state.meta.fonts.families} 族 · CUPS ${state.meta.cups ? '可用' : '不可用'}`);
  } catch (e) {
    setStatus('服务未就绪: ' + e.message, '');
  }
  // 服务端可能已经打开了文档（启动参数 --open）
  try {
    const d = await api('/api/doc');
    if (d.doc && d.doc.pages && d.doc.pages.length) {
      state.doc = d.doc;
      await renderThumbs();
      await loadPage(0);
      applyVPreferences(d.doc);
      updateFileInfo();
    }
  } catch (e) { /* 没有打开文档属正常 */ }
  updateToolbar();
}

/* ------------------------------------------------------------------ 文档 */

async function openLocalFile(file) {
  const buf = await file.arrayBuffer();
  const b64 = btoa(String.fromCharCode(...new Uint8Array(buf)));
  setStatus('正在打开 ' + file.name + ' …');
  try {
    const d = await api('/api/open', { data_b64: b64, name: file.name });
    state.doc = d.doc;
    state.selection = [];
    await renderThumbs();
    await loadPage(0);
    applyVPreferences(d.doc);
    toast('已打开 ' + file.name);
  } catch (e) {
    toast('打开失败：' + e.message, true);
    setStatus('打开失败：' + e.message);
  }
}

async function refreshDoc() {
  const d = await api('/api/doc');
  state.doc = d.doc;
}

async function loadPage(index) {
  if (!state.doc && !(await safeDoc())) return;
  const total = state.doc.pages.length;
  if (index < 0) index = 0;
  if (index >= total) index = total - 1;
  const d = await api('/api/page/' + index);
  state.page = d.page;
  state.pageIndex = index;
  state.selection = [];
  await renderer.load(d.page, BASE_PX_PER_MM * state.zoom);
  resizeCanvas();
  draw();
  renderProps();
  renderThumbsActive();
  $('emptystate').hidden = true;
  $('canvasHost').hidden = false;
  fitZoom();
  updateFileInfo();
  updateToolbar();
  renderOutlines(state.doc);
  renderBookmarks(state.doc);
  renderAttachments(state.doc);
}

/* ---------------------------------------------------------------- 动作热区 */

// 14 章：带 Actions 的图元要能点。这里在画布上叠一层细虚线框标出来，
// 点击时按 action.kind 分派：goto 跳页、uri 交给浏览器（新窗口）。
function collectActionRegions(scene) {
  const out = [];
  if (!scene || !scene.ops) return out;
  for (const op of scene.ops) {
    const acts = op.actions;
    if (!acts || !acts.length) continue;
    let x, y, w, h;
    if (op.corners && op.corners.length >= 2) {
      const xs = op.corners.map((c) => c[0]);
      const ys = op.corners.map((c) => c[1]);
      x = Math.min(...xs); y = Math.min(...ys);
      w = Math.max(...xs) - x; h = Math.max(...ys) - y;
    } else if (typeof op.x === 'number' && typeof op.w === 'number') {
      x = op.x; y = op.y; w = op.w; h = op.h;
    } else if (op.subs && op.subs.length) {
      // path 类型没有 x/w/h，要从子路径点集算包围盒
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const a of op.subs) {
        for (let k = 0; k + 1 < a.length; k += 2) {
          if (a[k] < x0) x0 = a[k];
          if (a[k] > x1) x1 = a[k];
          if (a[k + 1] < y0) y0 = a[k + 1];
          if (a[k + 1] > y1) y1 = a[k + 1];
        }
      }
      if (!isFinite(x0)) continue;
      x = x0; y = y0; w = x1 - x0; h = y1 - y0;
    } else {
      continue;
    }
    out.push({ x, y, w, h, actions: acts });
  }
  return out;
}

function drawActionRegions(ctx, regions, scale) {
  if (!regions.length) return;
  ctx.save();
  ctx.setLineDash([3, 3]);
  ctx.lineWidth = 1;
  ctx.strokeStyle = 'rgba(22,104,220,0.75)';
  for (const r of regions) {
    ctx.strokeRect(r.x * scale, r.y * scale, r.w * scale, r.h * scale);
  }
  ctx.restore();
}

// 点画布时先查热区，命中就执行动作；否则走原来的选中逻辑
function hitActionRegion(regions, xmm, ymm) {
  for (let i = regions.length - 1; i >= 0; i--) {
    const r = regions[i];
    if (xmm >= r.x && xmm <= r.x + r.w && ymm >= r.y && ymm <= r.y + r.h) return r;
  }
  return null;
}

// 表54 的 Dest 落地：XYZ 用缩放+定位，Fit/FitH/FitV/FitR 用对应的适配
function applyDest(detail) {
  if (!detail) return;
  const t = (detail.type || 'XYZ').toUpperCase();
  if (t === 'FITH') { fitWidthOnly(); }
  else if (t === 'FITV') { fitHeightOnly(); }
  else if (t === 'FITR') { fitRectOnly(detail.left, detail.top, detail.right, detail.bottom); }
  else if (t === 'FIT') { fitZoom(); }
  else {
    if (detail.zoom) setZoom(detail.zoom / 100);
  }
  if (t === 'XYZ' || t === 'FITH' || t === 'FITR') {
    scrollToMm(detail.left || 0, detail.top || 0);
  }
}

// 滚到页内某点（Left/Top 是目标区域左上角，单位 mm）
function scrollToMm(xmm, ymm) {
  const wrap = $('canvaswrap');
  const host = $('canvasHost');
  if (!wrap || !host) return;
  const Z = BASE_PX_PER_MM * state.zoom;
  const left = Math.max(0, xmm * Z - wrap.clientWidth / 3);
  const top = Math.max(0, ymm * Z - wrap.clientHeight / 3);
  if (host.offsetParent === wrap) {
    wrap.scrollLeft = left;
    wrap.scrollTop = top;
  }
}

function runActions(region) {
  for (const a of region.actions) {
    if (a.kind === 'goto') {
      const idx = pageIndexOfDest(state.doc, a.dest);
      if (idx >= 0) {
        loadPage(idx).then(() => applyDest(a.dest_detail));
        return true;
      }
    } else if (a.kind === 'uri' && a.uri) {
      const url = (a.base || '') + a.uri;
      // 附录 A 的 URI 动作可带 Target（目标窗口名）
      window.open(url, a.target || '_blank', 'noopener');
      return true;
    } else if (a.kind === 'gotoa' && a.attach_id) {
      // 表55：打开本文档内的附件
      window.open('/api/attachment/' + encodeURIComponent(a.attach_id),
        a.new_window === false ? '_self' : '_blank');
      return true;
    } else if (a.kind === 'sound' || a.kind === 'movie') {
      toast('该动作是' + (a.kind === 'sound' ? '音频' : '视频') +
        '播放（第14章），本工具不放音视频；资源 ID ' + (a.resource_id || '—'));
      return true;
    }
  }
  return false;
}

/* ---------------------------------------------------------------- 大纲 */

// 7.8 大纲：把树渲染成可点击的层级列表。
// 只处理 goto（跳页）动作；URI 动作显示成外链样式但不在此处跳转。
function pageIndexOfDest(doc, pageId) {
  if (!doc || !pageId) return -1;
  const pages = doc.pages || [];
  for (let i = 0; i < pages.length; i++) {
    if (String(pages[i].id) === String(pageId)) return i;
  }
  return -1;
}

function renderOutlines(doc) {
  const body = $('outlineBody');
  const title = $('outlineTitle');
  if (!body || !title) return;
  const tree = (doc && doc.outlines) || [];
  if (!tree.length) {
    title.hidden = true;
    body.hidden = true;
    body.innerHTML = '';
    return;
  }
  title.hidden = false;
  body.hidden = false;
  body.innerHTML = '';
  const walk = (nodes, depth) => {
    for (const n of nodes) {
      const row = document.createElement('div');
      row.className = 'outline-row';
      row.style.paddingLeft = (6 + depth * 13) + 'px';
      row.textContent = n.title || '(无标题)';
      const act = (n.actions || []).find((a) => a.kind === 'goto');
      if (act) {
        const idx = pageIndexOfDest(doc, act.dest);
        if (idx >= 0) {
          row.classList.add('clickable');
          row.title = '跳到第 ' + (idx + 1) + ' 页';
          row.addEventListener('click', () => loadPage(idx));
        }
      }
      body.appendChild(row);
      if (n.children && n.children.length) walk(n.children, depth + 1);
    }
  };
  walk(tree, 0);
}

async function safeDoc() {
  try { await refreshDoc(); return !!(state.doc && state.doc.pages.length); }
  catch (e) { return false; }
}

/* -------------------------------------------------- 表10 书签 / 表72 附件 */

// 表10：Bookmarks/Bookmark[@Name] + Dest（表54）。dest.page_id 是页面标识，
// 要换算成页序号才能跳；Zoom 有值就顺手设缩放。
function renderBookmarks(doc) {
  const body = $('bookmarkBody');
  const title = $('bookmarkTitle');
  if (!body || !title) return;
  const list = (doc && doc.bookmarks) || [];
  if (!list.length) {
    title.hidden = true; body.hidden = true; body.innerHTML = '';
    return;
  }
  title.hidden = false; body.hidden = false; body.innerHTML = '';
  for (const bm of list) {
    const row = document.createElement('div');
    row.className = 'outline-row';
    row.textContent = bm.name || '(未命名书签)';
    const dest = bm.dest || {};
    const idx = pageIndexOfDest(doc, dest.page_id);
    if (idx >= 0) {
      row.classList.add('clickable');
      row.title = '跳到第 ' + (idx + 1) + ' 页';
      row.addEventListener('click', async () => {
        await loadPage(idx);
        applyDest(dest);
      });
    }
    body.appendChild(row);
  }
}

// 表72/73：附件列表。点一行下载包内附件（/api/attachment/<ID>）。
function renderAttachments(doc) {
  const body = $('attachBody');
  const title = $('attachTitle');
  if (!body || !title) return;
  const list = (doc && doc.attachments) || [];
  if (!list.length) {
    title.hidden = true; body.hidden = true; body.innerHTML = '';
    return;
  }
  title.hidden = false; body.hidden = false; body.innerHTML = '';
  for (const at of list) {
    const row = document.createElement('div');
    row.className = 'outline-row clickable';
    const kb = at.size_kb ? ' · ' + at.size_kb + ' KB' : '';
    row.textContent = (at.name || at.id) + (at.format ? ' (' + at.format + ')' : '') + kb;
    row.title = '下载附件 ' + (at.name || at.id);
    row.addEventListener('click', () => {
      window.open('/api/attachment/' + encodeURIComponent(at.id), '_blank');
    });
    body.appendChild(row);
  }
}

// 表8/66-71：权限、签名、版本的只读摘要（放进「文档」面板）
function docExtrasHtml(d) {
  const rows = [];
  const perm = d.permissions || {};
  const bad = [];
  if (perm.Edit === false) bad.push('编辑');
  if (perm.Export === false) bad.push('导出');
  if (perm.Printable === false) bad.push('打印');
  if (perm.Annot === false) bad.push('标注');
  if (perm.Signature === false) bad.push('签名');
  if (perm.Watermark === false) bad.push('水印');
  if (perm.PrintScreen === false) bad.push('截屏');
  if (bad.length) {
    rows.push(`<div class="row"><label>权限</label><div style="color:#c0392b">禁止：${escapeHtml(bad.join('、'))}</div></div>`);
  }
  if (perm.Copies != null && perm.Copies !== '') {
    rows.push(`<div class="row"><label>打印份数</label><div>${escapeHtml(String(perm.Copies))}</div></div>`);
  }
  const sig = (d.signatures && d.signatures.items) || [];
  if (sig.length) {
    const txt = sig.map((s) => (s.type === 'Seal' ? '签章' : '签名') + '#' + s.id +
      (s.datetime ? ' ' + s.datetime : '')).join('、');
    rows.push(`<div class="row"><label>签名</label><div style="word-break:break-all">${escapeHtml(txt)}</div></div>`);
  }
  const ver = (d.versions && d.versions.items) || [];
  if (ver.length) {
    const txt = ver.map((v) => '#' + v.index + (v.current ? '(当前)' : '')).join('、');
    rows.push(`<div class="row"><label>版本</label><div>${escapeHtml(txt)}</div></div>`);
  }
  const tags = d.custom_tags || [];
  if (tags.length) {
    rows.push(`<div class="row"><label>标引</label><div>${tags.length} 组</div></div>`);
  }
  const exts = d.extensions || [];
  if (exts.length) {
    const names = exts.map((e) => e.app || '扩展').slice(0, 3).join('、');
    rows.push(`<div class="row"><label>扩展</label><div>${escapeHtml(names)}</div></div>`);
  }
  return rows.join('');
}

// 表9 视图首选项：打开文档时按文件自己的要求设初始缩放
function applyVPreferences(doc) {
  const pref = (doc && doc.vpreferences) || {};
  if (pref.HideToolbar) {
    const bar = document.querySelector('.toolbar');
    if (bar) bar.style.display = 'none';
  } else {
    const bar = document.querySelector('.toolbar');
    if (bar) bar.style.display = '';
  }
  if (pref.Zoom) {
    setZoom(pref.Zoom / 100);
  } else if (pref.ZoomMode) {
    // 表9：Default/FitHeight/FitWidth/FitRect 四种自动缩放
    const mode = String(pref.ZoomMode).toLowerCase();
    if (mode === 'fitwidth') fitWidthOnly();
    else if (mode === 'fitheight') fitHeightOnly();
    else if (mode === 'fitrect') fitZoom();
  }
  // 表9 PageMode 里点名要看的东西，直接把对应面板亮出来
  const pm = String(pref.PageMode || '').toLowerCase();
  if (pm === 'usebookmarks') {
    const t = $('bookmarkTitle');
    if (t) t.scrollIntoView({ block: 'nearest' });
  } else if (pm === 'useattatchs' || pm === 'useattachs') {
    const t = $('attachTitle');
    if (t) t.scrollIntoView({ block: 'nearest' });
  }
}

function updateFileInfo() {
  if (!state.doc) { $('fileinfo').textContent = '未打开文档'; return; }
  const name = (state.doc.path || '').split(/[\\/]/).pop() || '未命名';
  const t = state.doc.title ? ` · ${state.doc.title}` : '';
  $('fileinfo').textContent =
    `${name}${t} · ${state.doc.pages.length} 页 · ${state.doc.dirty ? '有未保存改动' : '已保存'}`;
}

/* ------------------------------------------------------------------ 绘制 */

function resizeCanvas() {
  const cv = $('cv');
  const [w, h] = renderer.sizePx();
  cv.width = Math.max(1, w);
  cv.height = Math.max(1, h);
  cv.style.width = w + 'px';
  cv.style.height = h + 'px';
}

function draw() {
  if (!state.page) return;
  renderer.zoom = BASE_PX_PER_MM * state.zoom;
  renderer.render(ctx, {
    grid: $('chkGrid').checked,
    preview: state.dragging
      ? { ids: new Set(state.selection.map((o) => o.id)),
          dx: state.dragging.dx, dy: state.dragging.dy }
      : null,
  });
  drawOverlay();
  state.actionRegions = collectActionRegions(state.page);
  if ($('chkActions') && $('chkActions').checked) {
    drawActionRegions(ctx, state.actionRegions, BASE_PX_PER_MM * state.zoom);
  }
}

function drawOverlay() {
  if (!state.selection.length || !state.page) return;
  const Z = BASE_PX_PER_MM * state.zoom;
  const dx = state.dragging ? state.dragging.dx : 0;
  const dy = state.dragging ? state.dragging.dy : 0;
  ctx.save();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.strokeStyle = '#0072c6';
  ctx.lineWidth = 1.5;
  ctx.setLineDash([5, 3]);
  for (const o of state.selection) {
    const c = o.corners || [];
    if (c.length !== 4) continue;
    ctx.beginPath();
    ctx.moveTo((c[0][0] + dx) * Z, (c[0][1] + dy) * Z);
    for (let i = 1; i < 4; i++) ctx.lineTo((c[i][0] + dx) * Z, (c[i][1] + dy) * Z);
    ctx.closePath();
    ctx.stroke();
  }
  ctx.setLineDash([]);
  ctx.restore();
}

/* ------------------------------------------------------------------ 交互 */

function bindEvents() {
  $('btnOpen').onclick = () => $('fileInput').click();
  $('fileInput').onchange = (e) => {
    const f = e.target.files[0];
    if (f) openLocalFile(f);
    e.target.value = '';
  };

  $('btnSave').onclick = async () => {
    if (!state.doc) return;
    try {
      const d = await api('/api/edit', { op: 'save' });
      state.doc = d.doc;
      updateFileInfo();
      // 保存重写了 XML，原数字签名失效——票据类误用要挡一道
      toast('已保存 ' + (state.doc.path || '') + '　·　原数字签名已失效，仅用于版式调整',
            true);
    } catch (e) { toast('保存失败：' + e.message, true); }
  };

  $('btnSaveAs').onclick = () => {
    if (!state.doc) return;
    const cur = state.doc.path || '';
    const guess = cur.replace(/\.ofd$/i, '') + '_修改.ofd';
    $('savePath').value = guess;
    showModal('dlgSaveAs');
  };

  $('saveAsOk').onclick = async () => {
    const path = $('savePath').value.trim();
    if (!path) { toast('请填路径', true); return; }
    try {
      const d = await api('/api/edit', { op: 'save_as', path });
      state.doc = d.doc;
      updateFileInfo();
      hideModal('dlgSaveAs');
      toast('已另存为 ' + path);
    } catch (e) { toast('另存失败：' + e.message, true); }
  };

  $('btnUndo').onclick = () => doEdit({ op: 'undo' }, true);
  $('btnRedo').onclick = () => doEdit({ op: 'redo' }, true);

  $('btnDelete').onclick = () => {
    if (!state.selection.length) { toast('先选中对象', true); return; }
    doEdit({ op: 'delete', page: state.pageIndex, ids: state.selection.map((o) => o.id) });
  };

  $('btnDup').onclick = () => {
    if (!state.selection.length) { toast('先选中对象', true); return; }
    doEdit({ op: 'duplicate', page: state.pageIndex, ids: state.selection.map((o) => o.id) });
  };

  $('btnText').onclick = () => {
    state.mode = state.mode === 'place-text' ? 'select' : 'place-text';
    $('cv').className = state.mode === 'place-text' ? 'placing' : '';
    setStatus(state.mode === 'place-text' ? '在页面上点一下，放置新文字' : '就绪');
  };

  $('btnRect').onclick = () => {
    state.mode = state.mode === 'place-rect' ? 'select' : 'place-rect';
    $('cv').className = state.mode === 'place-rect' ? 'placing' : '';
    setStatus(state.mode === 'place-rect' ? '在页面上拖一个方框' : '就绪');
  };

  $('btnImage').onclick = () => $('imgInput').click();
  $('imgInput').onchange = async (e) => {
    const f = e.target.files[0];
    e.target.value = '';
    if (!f) return;
    const buf = await f.arrayBuffer();
    state.pendingImage = {
      name: f.name,
      b64: btoa(String.fromCharCode(...new Uint8Array(buf))),
    };
    state.mode = 'place-image';
    $('cv').className = 'placing';
    setStatus('在页面上点一下，放置图片 ' + f.name);
  };

  $('btnFind').onclick = () => { $('findResult').textContent = ''; showModal('dlgFind'); };
  $('findOk').onclick = async () => {
    const find = $('findText').value;
    if (!find) { toast('要填查找内容', true); return; }
    try {
      const r = await api('/api/edit', { op: 'replace_all', find, repl: $('replText').value });
      $('findResult').textContent = `替换了 ${r.replaced} 处`;
      state.doc = r.doc;
      await loadPage(state.pageIndex);
    } catch (e) { toast('替换失败：' + e.message, true); }
  };

  $('btnPrint').onclick = openPrintDialog;
  $('btnExport').onclick = () => showModal('dlgExport');

  $('btnZoomIn').onclick = () => setZoom(state.zoom * 1.2);
  $('btnZoomOut').onclick = () => setZoom(state.zoom / 1.2);
  $('btnFit').onclick = fitZoom;
  $('chkGrid').onchange = draw;
  if ($('chkActions')) $('chkActions').onchange = draw;

  // Canvas 交互
  const cv = $('cv');
  cv.addEventListener('pointerdown', onPointerDown);
  window.addEventListener('pointermove', onPointerMove);
  window.addEventListener('pointerup', onPointerUp);
  cv.addEventListener('dblclick', onDoubleClick);
  cv.addEventListener('wheel', (e) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    setZoom(state.zoom * (e.deltaY < 0 ? 1.1 : 1 / 1.1));
  }, { passive: false });

  // 拖拽打开
  const wrap = $('canvaswrap');
  wrap.addEventListener('dragover', (e) => { e.preventDefault(); });
  wrap.addEventListener('drop', async (e) => {
    e.preventDefault();
    const f = e.dataTransfer.files[0];
    if (f) openLocalFile(f);
  });
  document.body.addEventListener('dragover', (e) => e.preventDefault());
  document.body.addEventListener('drop', (e) => {
    e.preventDefault();
    const f = e.dataTransfer.files[0];
    if (f && /\.ofd$/i.test(f.name)) openLocalFile(f);
  });

  // 键盘
  window.addEventListener('keydown', onKeyDown);

  // 弹窗关闭
  document.querySelectorAll('.modal').forEach((m) => {
    m.addEventListener('click', (e) => {
      if (e.target === m || e.target.hasAttribute('data-close')) hideModal(m.id);
    });
  });

  // 文本弹窗
  $('textOk').onclick = submitTextEdit;
}

function onPointerDown(e) {
  if (!state.page) return;
  const [x, y] = renderer.toMm(e.clientX, e.clientY, $('cv'));
  // 14 章：先看有没有动作热区命中，命中就执行动作而不是选中对象
  const region = hitActionRegion(state.actionRegions || [], x, y);
  if (region && runActions(region)) return;
  if (state.mode === 'place-text') {
    api('/api/edit', {
      op: 'add_text', page: state.pageIndex,
      x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10,
      text: '新文本', size: 5,
    }).then((d) => {
      state.doc = d.doc;
      state.mode = 'select';
      $('cv').className = '';
      return loadPage(state.pageIndex);
    }).catch((err) => toast('插入失败：' + err.message, true));
    return;
  }
  if (state.mode === 'place-rect') {
    state.placing = { x0: x, y0: y, x1: x, y1: y };
    return;
  }
  if (state.mode === 'place-image' && state.pendingImage) {
    const w = 40, h = 30;
    api('/api/edit', {
      op: 'add_image', page: state.pageIndex,
      x: Math.round((x - w / 2) * 10) / 10, y: Math.round((y - h / 2) * 10) / 10,
      w, h, data_b64: state.pendingImage.b64, name: state.pendingImage.name,
    }).then((d) => {
      state.doc = d.doc;
      state.pendingImage = null;
      state.mode = 'select';
      $('cv').className = '';
      return loadPage(state.pageIndex);
    }).catch((err) => toast('插入图片失败：' + err.message, true));
    return;
  }

  // 选择 / 拖动
  const hit = renderer.hit(state.page, x, y);
  if (!hit) {
    state.selection = [];
    draw();
    renderProps();
    return;
  }
  const already = state.selection.some((o) => o.id === hit.id);
  if (!already) {
    state.selection = e.shiftKey ? state.selection.concat([hit]) : [hit];
    draw();
    renderProps();
  }
  state.dragging = { x0: x, y0: y, dx: 0, dy: 0, moved: false };
}

function onPointerMove(e) {
  if (!state.page) return;
  const [x, y] = renderer.toMm(e.clientX, e.clientY, $('cv'));
  if (state.placing) {
    state.placing.x1 = x;
    state.placing.y1 = y;
    draw();
    drawPlacingRect();
    return;
  }
  if (!state.dragging) return;
  const dx = x - state.dragging.x0;
  const dy = y - state.dragging.y0;
  if (Math.abs(dx) > 0.15 || Math.abs(dy) > 0.15) state.dragging.moved = true;
  state.dragging.dx = dx;
  state.dragging.dy = dy;
  draw();
}

function onPointerUp() {
  if (state.placing) {
    const p = state.placing;
    state.placing = null;
    const x = Math.min(p.x0, p.x1), y = Math.min(p.y0, p.y1);
    const w = Math.abs(p.x1 - p.x0), h = Math.abs(p.y1 - p.y0);
    if (w > 1 && h > 1) {
      api('/api/edit', {
        op: 'add_rect', page: state.pageIndex,
        x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10,
        w: Math.round(w * 10) / 10, h: Math.round(h * 10) / 10,
        color: '#333333', width: 0.4,
      }).then((d) => {
        state.doc = d.doc;
        state.mode = 'select';
        $('cv').className = '';
        return loadPage(state.pageIndex);
      }).catch((err) => toast('插入方框失败：' + err.message, true));
    } else {
      draw();
    }
    return;
  }
  if (!state.dragging) return;
  const d = state.dragging;
  state.dragging = null;
  if (!d.moved || !state.selection.length) { draw(); return; }
  doEdit({
    op: 'move', page: state.pageIndex,
    ids: state.selection.map((o) => o.id),
    dx: Math.round(d.dx * 100) / 100, dy: Math.round(d.dy * 100) / 100,
  });
}

function drawPlacingRect() {
  const p = state.placing;
  if (!p) return;
  const Z = BASE_PX_PER_MM * state.zoom;
  ctx.save();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.strokeStyle = '#0072c6';
  ctx.setLineDash([4, 3]);
  ctx.strokeRect(p.x0 * Z, p.y0 * Z, (p.x1 - p.x0) * Z, (p.y1 - p.y0) * Z);
  ctx.restore();
}

function onDoubleClick(e) {
  if (!state.page) return;
  const [x, y] = renderer.toMm(e.clientX, e.clientY, $('cv'));
  const hit = renderer.hit(state.page, x, y);
  if (!hit) return;
  state.selection = [hit];
  draw();
  renderProps();
  const obj = (state.page.objects || []).find((o) => o.id === hit.id);
  if (obj && obj.type === '文本') openTextEditor(obj);
}

function openTextEditor(obj) {
  $('textInput').value = obj.text || '';
  $('textSize').value = (obj.size || 5).toFixed(1);
  $('textColor').value = rgbaToHex(obj.color);
  $('textBold').checked = !!obj.bold;
  const sel = $('textFont');
  sel.innerHTML = '<option value="">（保持当前字体）</option>';
  for (const [fid, name] of Object.entries(state.doc.fonts || {})) {
    const o = document.createElement('option');
    o.value = fid; o.textContent = `${name}（#${fid}）`;
    if (String(fid) === String(obj.font_id)) o.selected = true;
    sel.appendChild(o);
  }
  window._editingObj = obj;
  showModal('dlgText');
}

async function submitTextEdit() {
  const obj = window._editingObj;
  if (!obj) return;
  const text = $('textInput').value;
  const size = parseFloat($('textSize').value) || 5;
  const color = $('textColor').value;
  const bold = $('textBold').checked;
  const fontId = $('textFont').value;
  hideModal('dlgText');
  try {
    if (text !== (obj.text || '')) {
      await api('/api/edit', { op: 'set_text', page: state.pageIndex, id: obj.id, text });
    }
    await api('/api/edit', {
      op: 'set_text_style', page: state.pageIndex, id: obj.id,
      size, color, bold, font_id: fontId || undefined,
    });
    await refreshDoc();
    await loadPage(state.pageIndex);
    toast('文字已更新');
  } catch (e) { toast('修改失败：' + e.message, true); }
}

async function doEdit(payload, keepIndex) {
  try {
    const d = await api('/api/edit', payload);
    if (d.doc) state.doc = d.doc;
    await loadPage(keepIndex ? state.pageIndex : (d.page ? d.page.index : state.pageIndex));
    if (d.label) toast(d.label);
    updateFileInfo();
    updateToolbar();
  } catch (e) {
    toast('操作失败：' + e.message, true);
  }
}

function onKeyDown(e) {
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
    e.preventDefault();
    doEdit({ op: e.shiftKey ? 'redo' : 'undo' }, true);
    return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') {
    e.preventDefault();
    doEdit({ op: 'redo' }, true);
    return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    e.preventDefault();
    $('btnSave').click();
    return;
  }
  if (e.key === 'Delete' && state.selection.length) {
    $('btnDelete').click();
    return;
  }
  if (e.key === 'Escape') {
    state.mode = 'select';
    state.placing = null;
    $('cv').className = '';
    state.selection = [];
    draw();
    renderProps();
    return;
  }
  const step = e.shiftKey ? 1 : 0.5;
  const moves = {
    ArrowLeft: [-step, 0], ArrowRight: [step, 0],
    ArrowUp: [0, -step], ArrowDown: [0, step],
  };
  if (moves[e.key] && state.selection.length) {
    e.preventDefault();
    const [dx, dy] = moves[e.key];
    doEdit({
      op: 'move', page: state.pageIndex,
      ids: state.selection.map((o) => o.id), dx, dy,
    });
  }
}

/* ------------------------------------------------------------------ 缩放 */

function setZoom(z) {
  state.zoom = Math.max(0.2, Math.min(6, z));
  $('zoomLabel').textContent = Math.round(state.zoom * 100) + '%';
  if (!state.page) return;
  renderer.zoom = BASE_PX_PER_MM * state.zoom;
  resizeCanvas();
  draw();
}

function fitZoom() {
  if (!state.page) return;
  const wrap = $('canvaswrap');
  const availW = wrap.clientWidth - 64;
  const availH = wrap.clientHeight - 64;
  const w = state.page.size[0] * BASE_PX_PER_MM;
  const h = state.page.size[1] * BASE_PX_PER_MM;
  setZoom(Math.min(availW / w, availH / h));
}

// 表9 ZoomMode 与表54 FitH/FitV/FitR 都要求"适合某一边/某块区域"，
// 一律当整页适应会把 FitWidth 的版面压小
function fitWidthOnly() {
  if (!state.page) return;
  const wrap = $('canvaswrap');
  setZoom((wrap.clientWidth - 64) / (state.page.size[0] * BASE_PX_PER_MM));
}

function fitHeightOnly() {
  if (!state.page) return;
  const wrap = $('canvaswrap');
  setZoom((wrap.clientHeight - 64) / (state.page.size[1] * BASE_PX_PER_MM));
}

function fitRectOnly(left, top, right, bottom) {
  if (right == null || bottom == null || right <= left || bottom <= top) {
    fitZoom();
    return;
  }
  const wrap = $('canvaswrap');
  const rw = (right - left) * BASE_PX_PER_MM;
  const rh = (bottom - top) * BASE_PX_PER_MM;
  setZoom(Math.min((wrap.clientWidth - 64) / rw, (wrap.clientHeight - 64) / rh));
}

/* ------------------------------------------------------------------ 属性 */

function renderProps() {
  const body = $('propsBody');
  if (!state.selection.length) {
    body.innerHTML = '<p class="muted">没有选中对象（点画布上的内容）</p>';
    renderDocProps();
    return;
  }
  const o = state.selection[0];
  const x = Math.min(...o.corners.map((c) => c[0]));
  const y = Math.min(...o.corners.map((c) => c[1]));
  const x1 = Math.max(...o.corners.map((c) => c[0]));
  const y1 = Math.max(...o.corners.map((c) => c[1]));

  let html = `
    <div class="row"><label>类型</label><div>${o.type}</div></div>
    <div class="row"><label>编号</label><div>#${o.id}</div></div>
    <div class="row"><label>位置</label><div>${x.toFixed(1)}, ${y.toFixed(1)} mm</div></div>
    <div class="row"><label>尺寸</label><div>${(x1 - x).toFixed(1)} × ${(y1 - y).toFixed(1)} mm</div></div>`;

  if (o.type === '文本') {
    html += `
      <hr>
      <div class="row"><label>文字</label><div><input type="text" id="ppText" value="${escapeAttr(o.text || '')}"></div></div>
      <div class="row"><label>字号</label><div><input type="number" id="ppSize" step="0.5" min="0.5" value="${(o.size || 5).toFixed(1)}"></div></div>
      <div class="row"><label>颜色</label><div><input type="color" id="ppColor" value="${rgbaToHex(o.color)}"></div></div>
      <div class="row"><label>加粗</label><div><input type="checkbox" id="ppBold" ${o.bold ? 'checked' : ''}></div></div>
      <div class="row"><div style="grid-column:1/3"><button id="ppApply" class="primary" style="width:100%">应用文字属性</button></div></div>`;
  } else if (o.type === '图片') {
    html += `<hr><p class="muted">图片对象：可拖动、缩放、删除。替换图片请用「插入图片」重新放一张。</p>`;
  } else {
    html += `<hr>
      <div class="row"><label>描边</label><div><input type="color" id="ppStroke" value="${rgbaToHex(o.stroke)}"></div></div>
      <div class="row"><label>填充</label><div><input type="color" id="ppFillHex" value="${rgbaToHex(o.fill)}"></div></div>
      <div class="row"><div style="grid-column:1/3"><button id="ppApplyPath" class="primary" style="width:100%">应用颜色</button></div></div>`;
  }
  body.innerHTML = html;

  if (o.type === '文本') {
    $('ppApply').onclick = async () => {
      const text = $('ppText').value;
      try {
        if (text !== (o.text || '')) {
          await api('/api/edit', { op: 'set_text', page: state.pageIndex, id: o.id, text });
        }
        await api('/api/edit', {
          op: 'set_text_style', page: state.pageIndex, id: o.id,
          size: parseFloat($('ppSize').value) || 5,
          color: $('ppColor').value,
          bold: $('ppBold').checked,
        });
        await refreshDoc();
        await loadPage(state.pageIndex);
      } catch (e) { toast('应用失败：' + e.message, true); }
    };
  } else if (o.type !== '图片') {
    $('ppApplyPath').onclick = async () => {
      try {
        await api('/api/edit', {
          op: 'set_fill', page: state.pageIndex,
          ids: [o.id], color: $('ppFillHex').value,
        });
        await api('/api/edit', {
          op: 'set_stroke', page: state.pageIndex,
          ids: [o.id], color: $('ppStroke').value,
        });
        await refreshDoc();
        await loadPage(state.pageIndex);
      } catch (e) { toast('应用失败：' + e.message, true); }
    };
  }
  renderDocProps();
}

function renderDocProps() {
  const d = state.doc;
  if (!d) { $('docBody').innerHTML = '<p class="muted">未打开文档</p>'; return; }
  const p = d.pages[state.pageIndex] || {};
  $('docBody').innerHTML = `
    <div class="row"><label>标题</label><div>${escapeHtml(d.title || '—')}</div></div>
    <div class="row"><label>作者</label><div>${escapeHtml(d.author || '—')}</div></div>
    <div class="row"><label>页数</label><div>${d.pages.length}</div></div>
    <hr>
    <div class="row"><label>当前页</label><div>第 ${state.pageIndex + 1} 页 / ${p.objects || 0} 个对象</div></div>
    <div class="row"><label>页尺寸</label><div>${p.w || 0} × ${p.h || 0} mm</div></div>
    <div class="row"><label>旋转</label><div>${p.rotation || 0}°</div></div>
    <div class="row"><label>旋转页</label><div>
      <button id="pgRot" style="width:100%">顺时针 90°</button></div></div>
    <div class="row"><label>页操作</label><div style="display:flex;gap:6px">
      <button id="pgAdd" style="flex:1">插页</button>
      <button id="pgDel" style="flex:1">删页</button></div></div>
    ${docExtrasHtml(d)}`;
  if ($('pgRot')) {
    $('pgRot').onclick = () => doEdit({
      op: 'page_rotate', page: state.pageIndex,
      deg: ((p.rotation || 0) + 90) % 360,
    }, true);
  }
  if ($('pgAdd')) {
    $('pgAdd').onclick = () => doEdit({ op: 'page_insert', index: state.pageIndex + 1 }, true);
  }
  if ($('pgDel')) {
    $('pgDel').onclick = () => {
      if (d.pages.length <= 1) { toast('至少保留一页', true); return; }
      doEdit({ op: 'page_delete', index: state.pageIndex }, true);
    };
  }
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
function escapeAttr(s) { return escapeHtml(s); }

/* ------------------------------------------------------------------ 缩略图 */

async function renderThumbs() {
  const box = $('thumbs');
  box.innerHTML = '';
  if (!state.doc) return;
  for (let i = 0; i < state.doc.pages.length; i++) {
    const div = document.createElement('div');
    div.className = 'thumb';
    div.dataset.index = i;
    const img = document.createElement('img');
    img.src = `/api/thumb/${i}?dpi=40&t=${Date.now()}`;
    img.alt = '第 ' + (i + 1) + ' 页';
    img.loading = 'lazy';
    const cap = document.createElement('span');
    cap.textContent = '第 ' + (i + 1) + ' 页';
    div.appendChild(img);
    div.appendChild(cap);
    div.onclick = () => loadPage(i);
    box.appendChild(div);
  }
  renderThumbsActive();
}

function renderThumbsActive() {
  document.querySelectorAll('.thumb').forEach((el) => {
    el.classList.toggle('active', parseInt(el.dataset.index, 10) === state.pageIndex);
  });
}

/* ------------------------------------------------------------------ 打印 */

async function openPrintDialog() {
  if (!state.doc) { toast('先打开文档', true); return; }
  const sel = $('printerSel');
  sel.innerHTML = '<option value="">（系统默认）</option>';
  try {
    const info = await api('/api/printers');
    if (!info.available) {
      $('printHint').textContent = '系统里没有 CUPS（cups-client 未安装），只能用「先看预览」导出 PDF。';
    } else if (!info.printers.length) {
      $('printHint').textContent = 'CUPS 里没有配置打印机，请先在系统设置里添加。';
    } else {
      $('printHint').textContent = '共 ' + info.printers.length + ' 台打印机，默认：' + (info.default || '无');
    }
    for (const p of info.printers) {
      const o = document.createElement('option');
      o.value = p.name;
      o.textContent = p.name + (p.info ? ' — ' + p.info.slice(0, 40) : '');
      if (p.name === info.default) o.selected = true;
      sel.appendChild(o);
    }
  } catch (e) {
    $('printHint').textContent = '取打印机列表失败：' + e.message;
  }
  $('printPages').value = '';
  $('printCopies').value = '1';
  $('printNup').value = '1';
  $('printDuplex').checked = false;
  $('printBooklet').checked = false;
  $('printDpi').value = '200';
  showModal('dlgPrint');
}

async function doPrint(previewOnly) {
  if (!state.doc) return;
  const total = state.doc.pages.length;
  const pages = parseRanges($('printPages').value, total);
  if (!pages.length) { toast('页码范围无效', true); return; }
  const payload = {
    pages,
    copies: parseInt($('printCopies').value, 10) || 1,
    n_up: parseInt($('printNup').value, 10) || 1,
    paper: $('paperSel').value,
    duplex: $('printDuplex').checked,
    booklet: $('printBooklet').checked,
    dpi: parseInt($('printDpi').value, 10) || 200,
  };
  if (previewOnly) {
    try {
      const r = await api('/api/export', Object.assign({ format: 'pdf' }, payload));
      downloadFile(r.file);
      toast('已生成打印预览 PDF，可直接用阅读器打印');
    } catch (e) { toast('生成预览失败：' + e.message, true); }
    return;
  }
  payload.printer = $('printerSel').value || undefined;
  setStatus('正在提交打印任务 …');
  try {
    const r = await api('/api/print', payload);
    const res = r.result || {};
    if (res.ok) {
      toast('已提交：' + (res.stdout || '任务已进入队列'));
      setStatus('打印任务已提交');
      hideModal('dlgPrint');
    } else {
      toast('打印失败：' + (res.stderr || res.code || '未知原因'), true);
      setStatus('打印失败');
      if (res.pdf) {
        $('printHint').textContent = '已生成 PDF：' + res.pdf + '，可直接打这个文件。';
      }
    }
  } catch (e) {
    toast('打印失败：' + e.message, true);
    setStatus('打印失败');
  }
}

async function doExport() {
  if (!state.doc) { toast('先打开文档', true); return; }
  const fmt = $('exportFmt').value;
  const dpi = parseInt($('exportDpi').value, 10) || 200;
  const payload = { format: fmt, dpi };
  if (fmt === 'png' || fmt === 'svg') payload.pages = [state.pageIndex];
  hideModal('dlgExport');
  try {
    const r = await api('/api/export', payload);
    downloadFile(r.file);
    const f = r.file || {};
    toast(`已导出 ${f.name}（${(f.size / 1024).toFixed(0)} KB）`);
  } catch (e) { toast('导出失败：' + e.message, true); }
}

function downloadFile(info) {
  if (!info || !info.file) return;
  const url = '/api/download?file=' + encodeURIComponent(info.file);
  const a = document.createElement('a');
  a.href = url;
  a.download = info.name || 'export';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/* ------------------------------------------------------------------ 其它 */

function showModal(id) { $(id).hidden = false; }
function hideModal(id) { $(id).hidden = true; }

function updateToolbar() {
  const has = !!state.doc;
  ['btnSave', 'btnSaveAs', 'btnPrint', 'btnExport', 'btnFind',
   'btnText', 'btnImage', 'btnRect'].forEach((id) => { $(id).disabled = !has; });
  const d = state.doc || {};
  // 表8 权限声明：文件自己说不许做什么，就不给按钮留可点状态
  const perm = d.permissions || {};
  const deny = (id, allowed, why) => {
    if (!$(id)) return;
    if (has && !allowed) {
      $(id).disabled = true;
      $(id).title = '文档权限声明禁止' + why;
    } else {
      $(id).title = '';
    }
  };
  deny('btnSave', perm.Edit !== false, '编辑');
  deny('btnSaveAs', perm.Edit !== false, '编辑');
  deny('btnExport', perm.Export !== false, '导出');
  deny('btnPrint', perm.Printable !== false, '打印');
  $('btnUndo').disabled = !d.can_undo;
  $('btnRedo').disabled = !d.can_redo;
  $('btnUndo').title = d.undo_label ? '撤销 ' + d.undo_label : '撤销';
  $('btnRedo').title = d.redo_label ? '重做 ' + d.redo_label : '重做';
  $('btnDelete').disabled = !state.selection.length;
  $('btnDup').disabled = !state.selection.length;
}

// 打印按钮绑定（弹窗里的）
document.addEventListener('DOMContentLoaded', () => {
  const preview = $('printPreview');
  const ok = $('printOk');
  const exp = $('exportOk');
  if (preview) preview.onclick = () => doPrint(true);
  if (ok) ok.onclick = () => doPrint(false);
  if (exp) exp.onclick = () => doExport();
  boot();
});

/* 调试/自动化句柄：供 tools/cdp_web_test.py 用浏览器真实跑一遍交互。
   正常使用不依赖它，也不改变任何行为。 */
window.__OFD__ = {
  state, renderer, api, loadPage, doEdit, draw, renderProps,
  fitZoom, setZoom, openTextEditor,
  hit: (x, y) => renderer.hit(state.page, x, y),
  selectById: (id) => {
    const o = (state.page.objects || []).find((x) => String(x.id) === String(id));
    state.selection = o ? [o] : [];
    draw();
    renderProps();
    return o || null;
  },
  ready: () => !!(state.page && state.page.ops && state.page.ops.length),
};
