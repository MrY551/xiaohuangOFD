/* ==========================================================================
   OFD 场景渲染器 —— 把服务端给的绘制指令画到 Canvas2D 上。
   协议见 ofdlite/scene.py，单位毫米，原点左上，Y 轴向下。
   ========================================================================== */
'use strict';

const FONT_ALIASES = {
  '宋体': 'SimSun, Songti SC, Noto Serif CJK SC, Source Han Serif SC, serif',
  'simsun': 'SimSun, Songti SC, Noto Serif CJK SC, serif',
  '新宋体': 'NSimSun, SimSun, Noto Serif CJK SC, serif',
  '仿宋': 'FangSong, STFangsong, Noto Serif CJK SC, AR PL UKai CN, serif',
  'fangsong': 'FangSong, STFangsong, Noto Serif CJK SC, serif',
  '楷体': 'KaiTi, STKaiti, AR PL UKai CN, Noto Serif CJK SC, serif',
  'kaiti': 'KaiTi, STKaiti, AR PL UKai CN, serif',
  '黑体': 'SimHei, Heiti SC, Noto Sans CJK SC, Source Han Sans SC, sans-serif',
  'simhei': 'SimHei, Heiti SC, Noto Sans CJK SC, sans-serif',
  '微软雅黑': 'Microsoft YaHei, Noto Sans CJK SC, WenQuanYi Micro Hei, sans-serif',
  'microsoft yahei': 'Microsoft YaHei, Noto Sans CJK SC, sans-serif',
  'times new roman': 'Times New Roman, Liberation Serif, DejaVu Serif, serif',
  'arial': 'Arial, Liberation Sans, DejaVu Sans, sans-serif',
  'helvetica': 'Helvetica, Liberation Sans, DejaVu Sans, sans-serif',
  'courier': 'Courier New, Liberation Mono, DejaVu Sans Mono, monospace',
};

function fontStack(family) {
  if (!family) return 'sans-serif';
  const key = String(family).trim();
  const low = key.toLowerCase();
  if (FONT_ALIASES[key]) return FONT_ALIASES[key];
  if (FONT_ALIASES[low]) return FONT_ALIASES[low];
  const esc = key.replace(/"/g, '');
  return `"${esc}", Noto Sans CJK SC, Source Han Sans SC, WenQuanYi Zen Hei, sans-serif`;
}

function rgbaCss(c) {
  if (!c) return null;
  const r = Math.round(c[0] * 255);
  const g = Math.round(c[1] * 255);
  const b = Math.round(c[2] * 255);
  const a = c.length > 3 ? c[3] : 1;
  return `rgba(${r},${g},${b},${a})`;
}

class SceneRenderer {
  constructor() {
    this.scene = null;
    this.zoom = 3.78;          // 像素 / 毫米
    this.images = {};          // resKey -> HTMLImageElement
    this.missing = [];
  }

  /** 载入场景；返回 Promise，图片加载完再 resolve。 */
  load(scene, zoom) {
    this.scene = scene;
    if (zoom) this.zoom = zoom;
    this.missing = [];
    const jobs = [];
    const res = scene.resources || {};
    for (const key of Object.keys(res)) {
      if (this.images[key]) continue;
      const meta = res[key];
      if (!meta || !meta.data) { this.missing.push(key); continue; }
      jobs.push(new Promise((resolve) => {
        const img = new Image();
        img.onload = () => { this.images[key] = img; resolve(); };
        img.onerror = () => { this.missing.push(key); resolve(); };
        img.src = `data:${meta.mime || 'image/png'};base64,${meta.data}`;
      }));
    }
    return Promise.all(jobs);
  }

  /** 画布像素尺寸 */
  sizePx() {
    const s = this.scene ? this.scene.size : [210, 297];
    return [Math.ceil(s[0] * this.zoom), Math.ceil(s[1] * this.zoom)];
  }

  render(ctx, opts) {
    const o = opts || {};
    const Z = this.zoom;
    const [W, H] = this.sizePx();
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, W, H);
    ctx.scale(Z, Z);              // 之后全部用毫米坐标

    const ops = (this.scene && this.scene.ops) || [];
    const preview = o.preview || null;
    for (const op of ops) {
      const shift = (preview && op.id && preview.ids.has(op.id)) ? preview : null;
      if (shift) {
        ctx.save();
        ctx.translate(shift.dx, shift.dy);
      }
      try {
        switch (op.t) {
          case 'rect': this._rect(ctx, op); break;
          case 'path': this._path(ctx, op); break;
          case 'text': this._text(ctx, op); break;
          case 'image': this._image(ctx, op); break;
          default: break;
        }
      } catch (e) {
        // 单个指令失败不影响整页
        if (window.console) console.warn('op failed', op.t, e);
      } finally {
        if (shift) ctx.restore();
      }
    }
    ctx.restore();
    if (o.grid) this._grid(ctx);
  }

  _rect(ctx, op) {
    const fill = rgbaCss(op.fill);
    const stroke = rgbaCss(op.stroke);
    if (fill) {
      ctx.fillStyle = fill;
      ctx.fillRect(op.x, op.y, op.w, op.h);
    }
    if (stroke) {
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 0.1;
      ctx.strokeRect(op.x, op.y, op.w, op.h);
    }
  }

  _path(ctx, op) {
    const subs = op.subs || [];
    const closed = op.closed || [];
    if (!subs.length) return;
    ctx.beginPath();
    for (let i = 0; i < subs.length; i++) {
      const a = subs[i];
      if (a.length < 2) continue;
      ctx.moveTo(a[0], a[1]);
      for (let k = 2; k + 1 < a.length; k += 2) ctx.lineTo(a[k], a[k + 1]);
      if (closed[i]) ctx.closePath();
    }
    const fill = rgbaCss(op.fill);
    if (fill) {
      ctx.fillStyle = fill;
      ctx.fill(op.rule === 'evenodd' ? 'evenodd' : 'nonzero');
    }
    const stroke = rgbaCss(op.stroke);
    if (stroke) {
      ctx.strokeStyle = stroke;
      ctx.lineWidth = op.w || 0.2;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      if (op.dash && op.dash.length) ctx.setLineDash(op.dash);
      ctx.stroke();
      if (op.dash && op.dash.length) ctx.setLineDash([]);
    }
  }

  _text(ctx, op) {
    const txt = op.text || '';
    if (!txt) return;
    const size = op.size || 3;
    const weight = op.bold ? 'bold ' : '';
    const style = op.italic ? 'italic ' : '';
    ctx.font = `${style}${weight}${size}px ${fontStack(op.font)}`;
    ctx.fillStyle = rgbaCss(op.color) || '#000000';
    ctx.textBaseline = 'alphabetic';
    ctx.textAlign = 'left';
    if (op.spacing) ctx.letterSpacing = `${op.spacing}px`;
    const rot = op.rot || 0;
    const hscale = op.hscale || 1;
    if (Math.abs(rot) > 0.01 || Math.abs(hscale - 1) > 0.001) {
      ctx.save();
      ctx.translate(op.x, op.y);
      if (Math.abs(rot) > 0.01) ctx.rotate(rot * Math.PI / 180);
      if (Math.abs(hscale - 1) > 0.001) ctx.scale(hscale, 1);
      ctx.fillText(txt, 0, 0);
      ctx.restore();
    } else {
      ctx.fillText(txt, op.x, op.y);
    }
    if (op.spacing) ctx.letterSpacing = '0px';
  }

  _image(ctx, op) {
    const img = this.images[op.res];
    if (!img) return;
    const corners = op.corners || [];
    ctx.save();
    if (corners.length === 4) {
      // 用四角做仿射：左上 + 两个方向向量
      const [p0, p1, , p3] = corners;
      const a = (p1[0] - p0[0]) / (op.w || 1);
      const b = (p1[1] - p0[1]) / (op.w || 1);
      const c = (p3[0] - p0[0]) / (op.h || 1);
      const d = (p3[1] - p0[1]) / (op.h || 1);
      ctx.globalAlpha = op.alpha === undefined ? 1 : op.alpha;
      ctx.transform(a, b, c, d, p0[0], p0[1]);
      ctx.drawImage(img, 0, 0, op.w || 1, op.h || 1);
    } else {
      ctx.globalAlpha = op.alpha === undefined ? 1 : op.alpha;
      ctx.drawImage(img, op.x, op.y, op.w, op.h);
    }
    ctx.restore();
  }

  _grid(ctx) {
    const Z = this.zoom;
    const [W, H] = this.sizePx();
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.strokeStyle = 'rgba(0,0,0,0.06)';
    ctx.lineWidth = 1;
    const step = 10 * Z;
    for (let x = 0; x < W; x += step) {
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    }
    for (let y = 0; y < H; y += step) {
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    }
    ctx.restore();
  }

  /** 屏幕坐标 → 毫米坐标 */
  toMm(clientX, clientY, canvas) {
    const rect = canvas.getBoundingClientRect();
    const sx = canvas.width / rect.width;
    const sy = canvas.height / rect.height;
    const px = (clientX - rect.left) * sx;
    const py = (clientY - rect.top) * sy;
    return [px / this.zoom, py / this.zoom];
  }

  /** 命中测试：从上层到下层找包围盒命中的对象 */
  hit(scene, x, y) {
    const objs = (scene && scene.objects) || [];
    for (let i = objs.length - 1; i >= 0; i--) {
      const o = objs[i];
      if (pointInQuad(x, y, o.corners)) return o;
    }
    return null;
  }
}

function pointInQuad(x, y, quad) {
  if (!quad || quad.length !== 4) return false;
  let inside = false;
  for (let i = 0, j = 3; i < 4; j = i++) {
    const xi = quad[i][0], yi = quad[i][1];
    const xj = quad[j][0], yj = quad[j][1];
    const hit = ((yi > y) !== (yj > y)) &&
      (x < (xj - xi) * (y - yi) / ((yj - yi) || 1e-9) + xi);
    if (hit) inside = !inside;
  }
  return inside;
}

window.SceneRenderer = SceneRenderer;
window.fontStack = fontStack;
window.rgbaCss = rgbaCss;
window.pointInQuad = pointInQuad;
