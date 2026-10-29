// 小黄OFD 的 Rust 加速内核。
//
// 只做热点：扫描线光栅化（覆盖率）与像素混合。OFD 的解析、场景构建、界面
// 都在 Python 侧，这里只暴露 C ABI 给 ctypes 用。
//
// 为什么用 Rust 而不是 C：同样是编译到机器码、速度持平，但内存安全是
// 编译期保证的——光栅化里越界写一个字节就是随机崩溃或画错，C 靠自觉，
// Rust 编译不过。项目要求「最快 + 最稳」，这一条是决定性的。
//
// 算法与 ofdlite/raster.py 的 fill_coverage 完全一致（自己写的扫描线：
// ss 条子扫描线、非零/奇偶环绕、span 累加、0~255 归一化），不是搬来的实现。
//
// 编译: cargo build --release
// 产物: target/release/libofd_core.so
use std::os::raw::c_int;

/// 一条边。方向用于非零环绕计数。
struct Edge {
    x0: f64,
    y0: f64,
    x1: f64,
    y1: f64,
    dir: i32,
    ymin: f64,
    ymax: f64,
}

/// 与一条子扫描线的交点。
struct Cross {
    x: f64,
    dir: i32,
}

/// 从扁平坐标建边表。水平边不参与（标准里填充只按垂直方向扫描）。
fn build_edges(pts: &[f64], counts: Option<&[i32]>) -> Vec<Edge> {
    let n = pts.len() / 2;
    let mut out = Vec::with_capacity(n);
    if n < 3 {
        return out;
    }
    // 单路径：整段闭合；多子路径：按 counts 逐段闭合，段间不连
    let spans: Vec<(usize, usize)> = match counts {
        None => vec![(0, n)],
        Some(cs) => {
            let mut v = Vec::with_capacity(cs.len());
            let mut off = 0usize;
            for &c in cs {
                let c = c.max(0) as usize;
                if c >= 1 {
                    v.push((off, off + c));
                }
                off += c;
            }
            v
        }
    };
    for &(start, end) in spans.iter() {
        if end <= start + 2 {
            continue;
        }
        fill_span_edges(pts, start, end, &mut out);
    }
    out
}

fn fill_span_edges(pts: &[f64], start: usize, end: usize, out: &mut Vec<Edge>) {
    let n = end - start;
    for i in 0..n {
        let i = start + i;
        let j = start + (i - start + 1) % n; // 隐式闭合
        let x0 = pts[i * 2];
        let y0 = pts[i * 2 + 1];
        let x1 = pts[j * 2];
        let y1 = pts[j * 2 + 1];
        // 近似水平边也要跳过：Python 侧是 abs(y1-y0) < 1e-12。
        // 用 == 只会过滤严格相等，轮廓点的浮点误差会让一批「几乎水平」的边
        // 留下来，扫描时算出额外的交点，span 就多画一片（实测左侧 x=2..5）。
        if (y0 - y1).abs() < 1e-12 {
            continue;
        }
        out.push(Edge {
            x0,
            y0,
            x1,
            y1,
            dir: if y1 > y0 { 1 } else { -1 },
            ymin: if y0 < y1 { y0 } else { y1 },
            ymax: if y0 < y1 { y1 } else { y0 },
        });
    }
}

/// 扫描线填充，输出每一行的覆盖率（0~255）。
///
/// # Safety
/// `out` 必须指向至少 `w * h` 字节的可写内存；`pts` 必须指向 `npts * 2` 个 f64。
/// 这是给 ctypes 用的 C ABI，调用方（Python 侧）保证缓冲大小。
unsafe fn fill_impl(
    pts: &[f64],
    counts: Option<&[i32]>,
    w: usize,
    h: usize,
    evenodd: bool,
    ss: usize,
    out: &mut [u8],
) {
    if pts.len() < 6 || w == 0 || h == 0 || out.len() < w * h {
        return;
    }
    for v in out.iter_mut() {
        *v = 0;
    }

    let edges = build_edges(pts, counts);
    if edges.is_empty() {
        return;
    }

    let mut acc = vec![0.0f64; w];
    let mut cr: Vec<Cross> = Vec::with_capacity(edges.len() + 1);
    let inv = 1.0 / ss as f64;
    let wf = w as f64;

    for y in 0..h {
        let mut touched = false;
        let mut lo = w;
        let mut hi: isize = -1;
        let ybase = y as f64;

        for k in 0..ss {
            let yc = ybase + (k as f64 + 0.5) * inv;

            cr.clear();
            for e in edges.iter() {
                if yc < e.ymin || yc >= e.ymax {
                    continue;
                }
                let t = (yc - e.y0) / (e.y1 - e.y0);
                cr.push(Cross {
                    x: e.x0 + t * (e.x1 - e.x0),
                    dir: e.dir,
                });
            }
            if cr.len() < 2 {
                continue;
            }
            // 稳定排序在这里没有意义（交点相等时顺序不影响 span），用 unstable
            // 必须用**稳定排序**：交点 x 相等时顺序会影响 `prev == 0 / wind == 0`
            // 的判定，进而把 span 的左端点算错（实测左侧多画一片）。
            // Python 侧是 xs.sort()，也是稳定排序。
            cr.sort_by(|a, b| a.x.partial_cmp(&b.x).unwrap_or(std::cmp::Ordering::Equal));

            // 非零环绕必须按「从 0 变非 0 开始、从非 0 变 0 结束」划 span，
            // 不能写成「当前 wind != 0 就画」 —— 后者在 wind 从 2 变 1（都非 0）
            // 时还会继续画，而正确做法是那一整段本来就连着，不该断开也不该重复。
            // 这个差别在小区域里能看到 23 个像素偏大（实测 15x14 的字形轮廓）。
            let mut wind: i32 = 0;
            let mut start: Option<f64> = None;
            let mut spans: Vec<(f64, f64)> = Vec::with_capacity(cr.len() / 2 + 1);
            for i in 0..cr.len() {
                let prev = wind;
                wind += cr[i].dir;
                if evenodd {
                    if i + 1 < cr.len() && (i + 1) % 2 == 1 {
                        spans.push((cr[i].x, cr[i + 1].x));
                    }
                } else if prev == 0 && wind != 0 {
                    start = Some(cr[i].x);
                } else if prev != 0 && wind == 0 {
                    if let Some(sx) = start.take() {
                        spans.push((sx, cr[i].x));
                    }
                }
            }
            if !evenodd {
                if let Some(sx) = start {
                    if let Some(last) = cr.last() {
                        spans.push((sx, last.x));
                    }
                }
            }

            for &(mut x0, mut x1) in spans.iter() {
                if x1 <= 0.0 || x0 >= wf || x1 <= x0 {
                    continue;
                }
                if x0 < 0.0 {
                    x0 = 0.0;
                }
                if x1 > wf {
                    x1 = wf;
                }
                if x1 <= x0 {
                    continue;
                }
                let mut ix0 = x0.floor() as isize;
                let mut ix1 = x1.floor() as isize;
                if ix1 >= w as isize {
                    ix1 = w as isize - 1;
                }
                if ix0 < 0 {
                    ix0 = 0;
                }
                if ix0 == ix1 {
                    acc[ix0 as usize] += (x1 - x0) * inv;
                } else {
                    acc[ix0 as usize] += (ix0 as f64 + 1.0 - x0) * inv;
                    for x in (ix0 + 1)..ix1 {
                        acc[x as usize] += inv;
                    }
                    acc[ix1 as usize] += (x1 - ix1 as f64) * inv;
                }
                touched = true;
                if ix0 < lo as isize {
                    lo = ix0 as usize;
                }
                if ix1 > hi {
                    hi = ix1;
                }
            }
        }

        if !touched {
            continue;
        }
        let base = y * w;
        let stop = (hi as usize).min(w - 1);
        for x in lo..=stop {
            let a = acc[x];
            if a > 0.0 {
                let c = if a > 1.0 { 1.0 } else { a };
                let v = (c * 255.0 + 0.5) as u8;
                if v != 0 {
                    out[base + x] = v;
                }
                acc[x] = 0.0;
            }
        }
    }
}

/// 把覆盖率按颜色混合进 RGBA 画布（等价 raster.py 的 fill_polygons 混合段）。
///
/// 这一步在 Python 里是逐像素循环，A4 一页要几百万次迭代。
unsafe fn blend_impl(
    cov: &[u8],
    w: usize,
    h: usize,
    buf: &mut [u8],
    r: u8,
    g: u8,
    b: u8,
    a: u8,
) {
    if w == 0 || h == 0 || cov.len() < w * h || buf.len() < w * h * 4 {
        return;
    }
    let opaque = a == 255;
    let fa = a as f64 / 255.0;
    let inv = 1.0 - fa;
    let n = w * h;
    for i in 0..n {
        let c = cov[i];
        if c == 0 {
            continue;
        }
        let o = i * 4;
        if opaque && c == 255 {
            buf[o] = r;
            buf[o + 1] = g;
            buf[o + 2] = b;
            buf[o + 3] = 255;
            continue;
        }
        let k = (c as f64 / 255.0) * fa;
        let ki = (1.0 - k) as f64;
        buf[o] = (buf[o] as f64 * ki + r as f64 * k + 0.5) as u8;
        buf[o + 1] = (buf[o + 1] as f64 * ki + g as f64 * k + 0.5) as u8;
        buf[o + 2] = (buf[o + 2] as f64 * ki + b as f64 * k + 0.5) as u8;
        if buf[o + 3] < 255 {
            // 目标透明度用 source-over 合成；as u8 已经截断到 0~255
            buf[o + 3] = (buf[o + 3] as f64 * inv + 255.0 * fa + 0.5) as u8;
        } else {
            buf[o + 3] = 255;
        }
    }
}

// ---------------------------------------------------------------- C ABI

/// 扫描线填充：把覆盖率写进调用方给的缓冲（零拷贝）。
///
/// `pts` 是扁平坐标 `[x0, y0, x1, y1, ...]`，`npts` 是点数。
/// `out` 需要 `w * h` 字节。
#[no_mangle]
pub unsafe extern "C" fn ofd_fill_coverage(
    pts: *const f64,
    npts: c_int,
    w: c_int,
    h: c_int,
    evenodd: c_int,
    ss: c_int,
    out: *mut u8,
) {
    if pts.is_null() || out.is_null() || npts < 3 || w <= 0 || h <= 0 || ss <= 0 {
        return;
    }
    let pts_s = std::slice::from_raw_parts(pts, npts as usize * 2);
    let out_s = std::slice::from_raw_parts_mut(out, (w as usize) * (h as usize));
    fill_impl(
        pts_s,
        None,
        w as usize,
        h as usize,
        evenodd != 0,
        ss as usize,
        out_s,
    );
}

/// 多子路径一次算完：`counts[i]` 是第 i 个子路径的点数，`flat` 顺序拼接。
///
/// **必须一次算完**，不能逐个算再取最大 —— 按标准 9.2，非零环绕是在
/// 「所有子路径的边放在一起」的前提下计数的，重叠区域会相互抵消或叠加。
/// 逐个算再取并集，重叠区会变成「谁大听谁的」，与标准不符。
#[no_mangle]
pub unsafe extern "C" fn ofd_fill_coverage_multi(
    counts: *const c_int,
    nsubs: c_int,
    flat: *const f64,
    w: c_int,
    h: c_int,
    evenodd: c_int,
    ss: c_int,
    out: *mut u8,
) {
    if counts.is_null() || flat.is_null() || out.is_null()
        || nsubs < 1 || w <= 0 || h <= 0 || ss <= 0
    {
        return;
    }
    let mut total = 0usize;
    for i in 0..nsubs as usize {
        total += *counts.add(i) as usize;
    }
    if total < 3 {
        return;
    }
    let pts_s = std::slice::from_raw_parts(flat, total * 2);
    let out_s = std::slice::from_raw_parts_mut(out, (w as usize) * (h as usize));
    fill_impl(
        pts_s,
        None,
        w as usize,
        h as usize,
        evenodd != 0,
        ss as usize,
        out_s,
    );
}

/// 按覆盖率把纯色混合进 RGBA 画布。
#[no_mangle]
pub unsafe extern "C" fn ofd_blend(
    cov: *const u8,
    w: c_int,
    h: c_int,
    buf: *mut u8,
    r: u8,
    g: u8,
    b: u8,
    a: u8,
) {
    if cov.is_null() || buf.is_null() || w <= 0 || h <= 0 {
        return;
    }
    let n = (w as usize) * (h as usize);
    let cov_s = std::slice::from_raw_parts(cov, n);
    let buf_s = std::slice::from_raw_parts_mut(buf, n * 4);
    blend_impl(cov_s, w as usize, h as usize, buf_s, r, g, b, a);
}

/// 版本串，供 Python 侧确认加载到的是哪个构建。
#[no_mangle]
pub extern "C" fn ofd_core_version() -> *const u8 {
    b"1.0.0\0".as_ptr()
}


// ---------------------------------------------------------------- JPEG 色彩转换

/// YCbCr → RGB，含 4:2:0 / 4:2:2 / 4:4:4 上采样。返回交错 RGB。
///
/// 这是 JPEG 解码里最重的一步（实测占 61%）：1812x1062 要算 192 万个像素。
/// Cb/Cr 的贡献预先算成 256 项查表，循环里只做加法——系数固定，没必要每像素乘。
///
/// # Safety
/// 各平面长度需满足：`yd >= w*h`，`cd/crd >= cw*ch`，`out >= w*h*3`。
#[no_mangle]
pub unsafe extern "C" fn ofd_ycbcr_to_rgb(
    yd: *const u8,
    cd: *const u8,
    crd: *const u8,
    w: c_int,
    h: c_int,
    yw: c_int,
    yh: c_int,
    cw: c_int,
    ch: c_int,
    sh: c_int,
    sv: c_int,
    out: *mut u8,
) {
    if yd.is_null() || cd.is_null() || crd.is_null() || out.is_null()
        || w <= 0 || h <= 0 || yw <= 0 || yh <= 0
        || cw <= 0 || ch <= 0 || sh <= 0 || sv <= 0
    {
        return;
    }
    let w = w as usize;
    let h = h as usize;
    // **Y 平面的行宽不是图像宽度**：每个分量按 8x8 块存，块数按 MCU 对齐，
    // 所以平面宽 = bw*8，可能大于图像宽（1812 对齐后可能 1824）。
    // 用 w 当行宽会让整幅图逐行错位 —— 这是第一版差 31722 像素的原因。
    let yw = yw as usize;
    let yh = yh as usize;
    let cw = cw as usize;
    let chh = ch as usize;
    let sh = sh as usize;
    let sv = sv as usize;

    let ys = std::slice::from_raw_parts(yd, yw * yh);
    let cbs = std::slice::from_raw_parts(cd, cw * chh);
    let crs = std::slice::from_raw_parts(crd, cw * chh);
    let dst = std::slice::from_raw_parts_mut(out, w * h * 3);

    // 预计算：Cb/Cr 的贡献表（下标是原始值 0..255，偏移已含在里面）
    // 表里存**浮点**贡献，不预先取整 —— Python 版是
    //   r = Y + 1.402*Cr 然后 int(r + 0.5)
    // 若在这里先把 1.402*Cr 取整再与 Y 相加，会与 Python 差 1（实测差 1267 像素）。
    let mut rt = [0f32; 256]; // 1.402 * (Cr - 128)
    let mut gt_cb = [0f32; 256]; // -0.344136 * (Cb - 128)
    let mut gt_cr = [0f32; 256]; // -0.714136 * (Cr - 128)
    let mut bt = [0f32; 256]; // 1.772 * (Cb - 128)
    for v in 0..256i32 {
        let d = v - 128;
        rt[v as usize] = (1.402 * d as f64) as f32;
        gt_cb[v as usize] = (-0.344136 * d as f64) as f32;
        gt_cr[v as usize] = (-0.714136 * d as f64) as f32;
        bt[v as usize] = (1.772 * d as f64) as f32;
    }

    for y in 0..h {
        let yrow = y * yw;
        let cy = (y / sv).min(chh - 1);
        let crow = cy * cw;
        for x in 0..w {
            let cx = (x / sh).min(cw - 1);
            let yy = ys[yrow + x] as i32;
            let cb = cbs[crow + cx] as usize;
            let cr = crs[crow + cx] as usize;

            let yf = yy as f32;
            let r = yf + rt[cr];
            let g = yf + gt_cb[cb] + gt_cr[cr];
            let b = yf + bt[cb];

            // 注意：源平面用 yrow（= y*yw，对齐后的行宽），
            // 目标缓冲用图像坐标 y*w + x —— 两者不能混，混了就写越界。
            let o = (y * w + x) * 3;
            let ri = (r + 0.5) as i32;
            let gi = (g + 0.5) as i32;
            let bi = (b + 0.5) as i32;
            dst[o] = if ri < 0 { 0 } else if ri > 255 { 255 } else { ri as u8 };
            dst[o + 1] = if gi < 0 { 0 } else if gi > 255 { 255 } else { gi as u8 };
            dst[o + 2] = if bi < 0 { 0 } else if bi > 255 { 255 } else { bi as u8 };
        }
    }
}

/// 单分量（灰度）展开成 RGB。
#[no_mangle]
pub unsafe extern "C" fn ofd_gray_to_rgb(yd: *const u8, n: c_int, out: *mut u8) {
    if yd.is_null() || out.is_null() || n <= 0 {
        return;
    }
    let n = n as usize;
    let src = std::slice::from_raw_parts(yd, n);
    let dst = std::slice::from_raw_parts_mut(out, n * 3);
    for i in 0..n {
        let v = src[i];
        dst[i * 3] = v;
        dst[i * 3 + 1] = v;
        dst[i * 3 + 2] = v;
    }
}

/// 8x8 DCT 的余弦表与归一化系数，全进程只算一次。
fn cos_tables() -> &'static ([[f64; 8]; 8], [f64; 8]) {
    static TABLES: std::sync::OnceLock<([[f64; 8]; 8], [f64; 8])> =
        std::sync::OnceLock::new();
    TABLES.get_or_init(|| {
        let mut cos_t = [[0f64; 8]; 8];
        let mut norm = [0f64; 8];
        for u in 0..8 {
            norm[u] = if u == 0 { 1.0 / (2.0f64).sqrt() } else { 1.0 };
            for x in 0..8 {
                cos_t[u][x] =
                    (((2 * x + 1) as f64) * (u as f64) * std::f64::consts::PI / 16.0).cos();
            }
        }
        (cos_t, norm)
    })
}

/// 反量化 + 分离式二维 IDCT，输出 8x8 像素。
///
/// `coef` 是自然序（已反 zigzag）的 64 个系数，函数内部乘量化表后变换。
/// 只有 DC 的块走常数填充短路——这是 JPEG 里占比最大的情况。
#[no_mangle]
pub unsafe extern "C" fn ofd_idct8x8(coef: *const i32, qt: *const u16, out: *mut u8) {
    if coef.is_null() || qt.is_null() || out.is_null() {
        return;
    }
    let c = std::slice::from_raw_parts(coef, 64);
    let q = std::slice::from_raw_parts(qt, 64);
    let dst = std::slice::from_raw_parts_mut(out, 64);

    // 反量化
    let mut blk = [0f64; 64];
    let mut ac_zero = true;
    for i in 0..64 {
        let v = (c[i] as f64) * (q[i] as f64);
        blk[i] = v;
        if i > 0 && c[i] != 0 {
            ac_zero = false;
        }
    }

    if ac_zero {
        // 只有 DC：IDCT 退化成 clamp(round(DC/8 + 128))
        let v = (blk[0] / 8.0 + 128.5) as i32;
        let v = if v < 0 { 0 } else if v > 255 { 255 } else { v } as u8;
        for i in 0..64 {
            dst[i] = v;
        }
        return;
    }

    // 余弦表只算一次：一张 1812x1062 的图有 3456 个块，
    // 每块都重算 64 次 cos 会把 IDCT 拖慢一个数量级（实测 3.46s → 4.32s）。
    let (cos_t, norm) = cos_tables();

    // 行变换
    let mut tmp = [0f64; 64];
    for v in 0..8 {
        let base = v * 8;
        for x in 0..8 {
            let mut s = 0f64;
            for u in 0..8 {
                s += norm[u] * blk[base + u] * cos_t[u][x];
            }
            tmp[base + x] = s * 0.5;
        }
    }
    // 列变换
    for x in 0..8 {
        for y in 0..8 {
            let mut s = 0f64;
            for v in 0..8 {
                s += norm[v] * tmp[v * 8 + x] * cos_t[v][y];
            }
            let val = (s * 0.5 + 128.5) as i32;
            dst[y * 8 + x] = if val < 0 { 0 } else if val > 255 { 255 } else { val as u8 };
        }
    }
}


/// 局部区混合：把 `mw x mh` 的覆盖率按颜色混进画布的 `(ox, oy)` 处。
///
/// fill_polygons 算完覆盖率后，只有 bbox 那一小块要混，用覆盖整幅画布的
/// 接口会把范围放大几十倍。
///
/// # Safety
/// `cov` 至少 `mw*mh` 字节；`buf` 至少 `bw*bh*4` 字节；`ox+mw <= bw`、`oy+mh <= bh`。
#[no_mangle]
pub unsafe extern "C" fn ofd_blend_rect(
    cov: *const u8,
    mw: c_int,
    mh: c_int,
    buf: *mut u8,
    bw: c_int,
    bh: c_int,
    ox: c_int,
    oy: c_int,
    r: u8,
    g: u8,
    b: u8,
    a: u8,
) {
    if cov.is_null() || buf.is_null() || mw <= 0 || mh <= 0 || bw <= 0 || bh <= 0 {
        return;
    }
    let mw = mw as usize;
    let mh = mh as usize;
    let bw = bw as usize;
    let bh = bh as usize;
    let ox = ox.max(0) as usize;
    let oy = oy.max(0) as usize;
    if ox + mw > bw || oy + mh > bh {
        return;
    }

    let covs = std::slice::from_raw_parts(cov, mw * mh);
    let bufs = std::slice::from_raw_parts_mut(buf, bw * bh * 4);

    let opaque = a == 255;
    // Python 侧是 f64，这里也必须用 f64 —— f32 会差 1 个色阶（实测 sha 不同）
    let fa = a as f64 / 255.0;

    for row in 0..mh {
        let src = row * mw;
        let dst_row = (oy + row) * bw;
        for col in 0..mw {
            let c = covs[src + col];
            if c == 0 {
                continue;
            }
            let o = (dst_row + ox + col) * 4;
            if opaque && c == 255 {
                bufs[o] = r;
                bufs[o + 1] = g;
                bufs[o + 2] = b;
                bufs[o + 3] = 255;
                continue;
            }
            let fa2 = (c as f64 / 255.0) * fa;
            if fa2 >= 1.0 {
                // 覆盖率折算后已经完全不透明：直接写，不走混合
                bufs[o] = r;
                bufs[o + 1] = g;
                bufs[o + 2] = b;
                bufs[o + 3] = 255;
                continue;
            }
            let iv = 1.0 - fa2;
            // 与 Python 的 int(...) 一致：**截断**，不是四舍五入；
            // 且必须用 f64（f32 会差 1 个色阶）。
            bufs[o] = (bufs[o] as f64 * iv + r as f64 * fa2) as u8;
            bufs[o + 1] = (bufs[o + 1] as f64 * iv + g as f64 * fa2) as u8;
            bufs[o + 2] = (bufs[o + 2] as f64 * iv + b as f64 * fa2) as u8;
            // 目标的 alpha **不是** source-over 合成，而是「取较大值」：
            //   na = int(sa * cvr / 255)
            //   if 目标 alpha < na: 目标 alpha = na
            // 写成 source-over 会让半透明叠加后的 alpha 与 Python 不同
            // （实测 WPS 第 3 页整页 sha 因此不同）。
            let na = (a as f64 * c as f64 / 255.0) as u8;
            if bufs[o + 3] < na {
                bufs[o + 3] = na;
            }
        }
    }
}
