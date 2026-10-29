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

function fontStack(family, fixed) {
  // 表44 FixedWidth 只用于"匹配替代字型"：原名照用，兜底时优先等宽
  const tail = fixed
    ? 'Liberation Mono, DejaVu Sans Mono, Courier New, monospace'
    : 'Noto Sans CJK SC, Source Han Sans SC, WenQuanYi Zen Hei, sans-serif';
  if (!family) return fixed ? 'monospace' : 'sans-serif';
  const key = String(family).trim();
  const low = key.toLowerCase();
  if (FONT_ALIASES[key]) return FONT_ALIASES[key];
  if (FONT_ALIASES[low]) return FONT_ALIASES[low];
  const esc = key.replace(/"/g, '');
  return `"${esc}", ${tail}`;
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
      const clip = op.clip || null;
      if (shift || clip) {
        ctx.save();
      }
      if (shift) ctx.translate(shift.dx, shift.dy);
      if (clip) this._clip(ctx, clip);
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
        if (shift || clip) ctx.restore();
      }
    }
    ctx.restore();
    if (o.grid) this._grid(ctx);
  }

  // 8.4 / 表34：组间交集、组内并集。
  // 一个 path 里追加多条子路径再 clip() → 并集（nonzero 默认）；
  // 连续多次 clip() 在 Canvas 里天然是交集。
  _clip(ctx, groups) {
    for (const g of groups) {
      ctx.beginPath();
      for (const pts of g) {
        if (pts.length < 6) continue;
        ctx.moveTo(pts[0], pts[1]);
        for (let k = 2; k < pts.length - 1; k += 2) {
          ctx.lineTo(pts[k], pts[k + 1]);
        }
        ctx.closePath();
      }
      ctx.clip();
    }
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

  // 8.3.4：把 scene 的渐变描述变成 Canvas 渐变。
  // Canvas 原生没有 Spread（只 pad），所以 Repeat / Reflect 手工按 MapUnit 分段：
  // 在对象 bbox 覆盖的轴投影区间内，一个周期一段，Reflect 的奇数周期把颜色顺序反转。
  _gradient(ctx, shd, bbox) {
    const [sx, sy] = shd.start;
    const [ex, ey] = shd.end;
    const segs = shd.segments || [];
    if (!segs.length) return null;
    const mt = (shd.map || 'direct').toLowerCase();
    const unit = shd.unit;

    const makeLinear = (x0, y0, x1, y1, reverse) => {
      const g = ctx.createLinearGradient(x0, y0, x1, y1);
      const list = reverse ? segs.slice().reverse() : segs;
      for (const [pos, c] of list) {
        const p = reverse ? 1 - pos : pos;
        g.addColorStop(Math.max(0, Math.min(1, p)), rgbaCss(c));
      }
      return g;
    };

    // Qt / Canvas 都没有三角形顶点色插值的原生能力。
    // 不假装支持：按控制点平均色填纯色，并在控制台报一次，
    // 导出（Pillow 后端）是准确的，屏幕上会明显不同。
    if (shd.kind === 'gouraud' || shd.kind === 'lattice') {
      const pts = shd.points || [];
      if (!pts.length) return null;
      let r = 0, g = 0, b = 0, a = 0;
      for (const pt of pts) { r += pt[2][0]; g += pt[2][1]; b += pt[2][2]; a += pt[2][3]; }
      const n = pts.length;
      if (window.console) {
        console.warn('高洛德渐变在 Canvas 端暂用平均色降级（导出 PDF/图片是准确的）');
      }
      return 'rgba(' + Math.round(r / n * 255) + ',' + Math.round(g / n * 255) +
             ',' + Math.round(b / n * 255) + ',' + (a / n) + ')';
    }

    if (mt === 'direct' || !unit) {
      return makeLinear(sx, sy, ex, ey, false);
    }

    // 分段：先求 bbox 在轴上的投影参数范围
    const ax = ex - sx, ay = ey - sy;
    const L2 = ax * ax + ay * ay;
    if (L2 < 1e-12) return makeLinear(sx, sy, ex, ey, false);
    const corners = [[bbox[0], bbox[1]], [bbox[2], bbox[1]],
                     [bbox[2], bbox[3]], [bbox[0], bbox[3]]];
    let tmin = Infinity, tmax = -Infinity;
    for (const [px, py] of corners) {
      const t = ((px - sx) * ax + (py - sy) * ay) / L2;   // 0..1 相对整轴
      tmin = Math.min(tmin, t); tmax = Math.max(tmax, t);
    }
    const L = Math.sqrt(L2);
    // 段的长度就是 MapUnit（mm）。t 是相对整轴的比例，局部坐标 = t * L，
    // 所以段号 = 局部坐标 / unit。早期写成 t/(L/unit) 再乘 L，量纲就错了。
    const k0 = Math.floor((tmin * L) / unit) - 1;
    const k1 = Math.ceil((tmax * L) / unit) + 1;

    // 返回一个"按段循环画"的函数。
    //
    // 做法：先把坐标系搬到轴的局部系（原点 StartPoint、x 轴沿轴方向），
    // 于是"段"就退化成 x 区间 [k*unit, (k+1)*unit] 的矩形，用 rect clip 即可，
    // 不必自己算法线、也不会在斜轴时算歪。调用方负责先 clip 好对象形状。
    const Lax = Math.sqrt(L2) || 1;
    return { segmented: true, paint: (drawFn) => {
      const ang = Math.atan2(ay, ax);
      ctx.save();
      ctx.translate(sx, sy);
      ctx.rotate(ang);
      const far = (Lax + (bbox[2] - bbox[0]) + (bbox[3] - bbox[1])) * 2 + 100;
      for (let k = k0; k <= k1; k++) {
        const xa = k * unit;                 // 段的起点（局部 x，mm）
        const xb = (k + 1) * unit;           // 段的终点
        const reverse = (mt === 'reflect') && (((k % 2) + 2) % 2 === 1);
        const g = ctx.createLinearGradient(xa, 0, xb, 0);
        const list = reverse ? segs.slice().reverse() : segs;
        for (const pair of list) {
          const pos = pair[0], c = pair[1];
          const q = reverse ? 1 - pos : pos;
          g.addColorStop(Math.max(0, Math.min(1, q)), rgbaCss(c));
        }
        ctx.save();
        ctx.beginPath();
        ctx.rect(xa, -far, xb - xa, far * 2);
        ctx.clip();
        ctx.fillStyle = g;
        ctx.fillRect(xa, -far, xb - xa, far * 2);
        ctx.restore();
      }
      ctx.restore();
    }};
  }

  // 8.3.3 底纹：把 CellContent 画到离屏 canvas，再 createPattern 平铺。
  // 镜像（ReflectMethod）canvas pattern 做不到，按不镜像平铺并在控制台报一次。
  _patternStyle(ctx, shd) {
    const pt = shd.pattern || {};
    const ops = shd.cell_ops || [];
    if (!pt.w || !pt.h || !ops.length) return null;
    if (pt.reflect && pt.reflect !== 'normal' && window.console) {
      console.warn('底纹 ReflectMethod=' + pt.reflect +
                   ' 在 Canvas 端按不镜像平铺（导出 PDF/图片是准确的）');
    }
    // 用 1 CSS px = 1mm 的离屏画布，pattern 的 transform 再按 zoom 缩放
    const cw = Math.max(1, Math.ceil(pt.w));
    const chh = Math.max(1, Math.ceil(pt.h));
    const off = document.createElement('canvas');
    off.width = cw;
    off.height = chh;
    const oc = off.getContext('2d');
    // 离屏画布按 1px = 1mm 建，cell 里的指令坐标就是 mm，直接画
    const sub = this;
    for (const o of ops) {
      try {
        if (o.t === 'path') sub._path(oc, o);
        else if (o.t === 'rect') sub._rect(oc, o);
        else if (o.t === 'text') sub._text(oc, o);
      } catch (e) {
        if (window.console) console.warn('底纹单元绘制失败', e);
        break;
      }
    }
    // 主画布已 scale(zoom)，坐标单位就是 mm；离屏画布也是 1px = 1mm，
    // 两边单位一致，pattern 默认 1:1 映射即可，不要再加 transform。
    return ctx.createPattern(off, 'repeat');
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
    const rawFill = op.fill;
    if (rawFill && typeof rawFill === 'object' && rawFill.kind === 'pattern') {
      const pat = this._patternStyle(ctx, rawFill);
      if (pat) { ctx.fillStyle = pat; ctx.fill(op.rule === 'evenodd' ? 'evenodd' : 'nonzero'); }
    } else if (rawFill && typeof rawFill === 'object' && !Array.isArray(rawFill)) {
      // 渐变填充
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const a of subs) {
        for (let k = 0; k + 1 < a.length; k += 2) {
          if (a[k] < x0) x0 = a[k];
          if (a[k] > x1) x1 = a[k];
          if (a[k + 1] < y0) y0 = a[k + 1];
          if (a[k + 1] > y1) y1 = a[k + 1];
        }
      }
      if (x1 > x0 && y1 > y0) {
        const bb = [x0, y0, x1, y1];
        const rule = op.rule === 'evenodd' ? 'evenodd' : 'nonzero';
        const gd = this._gradient(ctx, rawFill, [x0, y0, x1, y1]);
        if (gd && gd.segmented) {
          ctx.save();
          // 先把这次填充限制在对象形状里（nonzero / evenodd 都在这里生效）
          ctx.beginPath();
          for (let i = 0; i < subs.length; i++) {
            const a = subs[i];
            if (a.length < 2) continue;
            ctx.moveTo(a[0], a[1]);
            for (let k = 2; k + 1 < a.length; k += 2) ctx.lineTo(a[k], a[k + 1]);
            if (closed[i]) ctx.closePath();
          }
          ctx.clip(rule);
          gd.paint(null);
          ctx.restore();
        } else if (gd) {
          ctx.fillStyle = gd;
          ctx.fill(rule);
        }
      }
    } else {
      const fill = rgbaCss(rawFill);
      if (fill) {
        ctx.fillStyle = fill;
        ctx.fill(op.rule === 'evenodd' ? 'evenodd' : 'nonzero');
      }
    }
    const stroke = rgbaCss(op.stroke);
    if (stroke) {
      ctx.strokeStyle = stroke;
      ctx.lineWidth = op.w || 0.2;
      // 8.2.2 / 8.2.5：按标准取值
      ctx.lineJoin = (op.join || 'miter').toLowerCase();
      ctx.lineCap = (op.cap || 'butt').toLowerCase();
      // 8.2.3：只在 Miter 时生效；canvas 的 miterLimit 默认是 10，
      // 与 OFD 正文的 3.528 不同，必须显式设，否则锐角尖端会被放长
      if (ctx.lineJoin === 'miter' && op.miter) ctx.miterLimit = op.miter;
      if (op.dash && op.dash.length) {
        ctx.setLineDash(op.dash);
        // 8.2.4：DashOffset 只在 DashPattern 出现时有效
        ctx.lineDashOffset = op.dash_offset || 0;
      }
      ctx.stroke();
      if (op.dash && op.dash.length) {
        ctx.setLineDash([]);
        ctx.lineDashOffset = 0;
      }
    }
  }

  _text(ctx, op) {
    const txt = op.text || '';
    if (!txt) return;
    const size = op.size || 3;
    const weight = op.bold ? 'bold ' : '';
    const style = op.italic ? 'italic ' : '';
    ctx.font = `${style}${weight}${size}px ${fontStack(op.font, op.fixed_width)}`;
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
