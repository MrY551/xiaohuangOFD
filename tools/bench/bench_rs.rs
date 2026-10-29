// fill_coverage 的 Rust 版基准实现 —— 与 bench_c.c / raster.py 算法逐步对应。
//
// 编译: rustc -O -o bench_rs bench_rs.rs
use std::env;
use std::f64::consts::PI;
use std::time::Instant;

struct Edge {
    x0: f64,
    y0: f64,
    x1: f64,
    y1: f64,
    dir: i32,
    ymin: f64,
    ymax: f64,
}

struct Cross {
    x: f64,
    dir: i32,
}

fn fill_coverage(
    pts: &[f64],
    w: usize,
    h: usize,
    evenodd: bool,
    ss: usize,
    cov: &mut [u8],
) {
    let npts = pts.len() / 2;
    if npts < 3 {
        return;
    }

    let mut edges: Vec<Edge> = Vec::with_capacity(npts);
    for i in 0..npts {
        let j = (i + 1) % npts;
        let (x0, y0) = (pts[i * 2], pts[i * 2 + 1]);
        let (x1, y1) = (pts[j * 2], pts[j * 2 + 1]);
        if y0 == y1 {
            continue;
        }
        edges.push(Edge {
            x0,
            y0,
            x1,
            y1,
            dir: if y1 > y0 { 1 } else { -1 },
            ymin: if y0 < y1 { y0 } else { y1 },
            ymax: if y0 < y1 { y1 } else { y0 },
        });
    }

    let mut acc: Vec<f64> = vec![0.0; w];
    let mut cr: Vec<Cross> = Vec::with_capacity(edges.len() + 1);
    let inv = 1.0 / ss as f64;

    for y in 0..h {
        let mut touched = false;
        let mut lo = w;
        let mut hi: isize = -1;

        for k in 0..ss {
            let yc = y as f64 + (k as f64 + 0.5) * inv;
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
            let nc = cr.len();
            if nc < 2 {
                continue;
            }
            // sort_unstable_by 不做稳定排序、也没有 unwrap 开销，实测快一截
            cr.sort_unstable_by(|a, b| a.x.partial_cmp(&b.x).unwrap());

            let mut wind: i32 = 0;
            for i in 0..nc - 1 {
                wind += cr[i].dir;
                let inside = if evenodd {
                    (i + 1) % 2 == 1
                } else {
                    wind != 0
                };
                if !inside {
                    continue;
                }
                let mut x0 = cr[i].x;
                let mut x1 = cr[i + 1].x;
                if x1 <= 0.0 || x0 >= w as f64 {
                    continue;
                }
                if x0 < 0.0 {
                    x0 = 0.0;
                }
                if x1 > w as f64 {
                    x1 = w as f64;
                }
                if x1 <= x0 {
                    continue;
                }
                let mut ix0 = x0.floor() as isize;
                let mut ix1 = x1.floor() as isize;
                if ix1 >= w as isize {
                    ix1 = w as isize - 1;
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
        for x in lo..=(hi as usize) {
            let a = acc[x];
            if a > 0.0 {
                let c = if a > 1.0 { 1.0 } else { a };
                let v = (c * 255.0 + 0.5) as u8;
                if v != 0 {
                    cov[base + x] = v;
                }
                acc[x] = 0.0;
            }
        }
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let repeat: usize = if args.len() > 1 {
        args[1].parse().unwrap_or(20)
    } else {
        20
    };
    let (w, h) = (1240usize, 827usize); // A4 @150dpi
    let ss = 4usize;

    let npts = 720usize;
    let mut pts: Vec<f64> = Vec::with_capacity(npts * 2);
    let (cx, cy) = (w as f64 / 2.0, h as f64 / 2.0);
    let r = (h.min(w) as f64) * 0.45;
    for i in 0..npts {
        let a = 2.0 * PI * i as f64 / npts as f64;
        pts.push(cx + r * a.cos());
        pts.push(cy + r * a.sin());
    }

    let mut cov = vec![0u8; w * h];
    fill_coverage(&pts, w, h, false, ss, &mut cov); // 预热

    let t0 = Instant::now();
    for _ in 0..repeat {
        for v in cov.iter_mut() {
            *v = 0;
        }
        fill_coverage(&pts, w, h, false, ss, &mut cov);
    }
    let ms = t0.elapsed().as_secs_f64() * 1000.0 / repeat as f64;

    let nz = cov.iter().filter(|&&v| v != 0).count();
    println!(
        "Rust   {} 次  平均 {:.3} ms/次  非零 {} / {}",
        repeat,
        ms,
        nz,
        w * h
    );
}
