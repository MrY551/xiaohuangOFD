// fill_coverage 的 C 版基准实现 —— 与 ofdlite/raster.py 的算法逐步对应。
//
// 算法（与 Python 版一致，不多不少）：
//   对每一行 y，取 ss 条子扫描线 yc = y + (k + 0.5) / ss
//   找出与 yc 相交的边，算交点 x，按方向累加
//   nonzero：看累计量是否为 0；even-odd：看奇偶
//   得到若干 span，按「子像素覆盖率」累加到 acc[x]
//   最后 acc[x] / ss 归一化到 0~255
//
// 编译: gcc -O2 -o bench_c bench_c.c -lm
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>

typedef struct {
    double x0, y0, x1, y1;
    int dir;        // +1 向下、-1 向上
    double ymin, ymax;
} Edge;

typedef struct {
    double x;
    int dir;
} Cross;

static int cmp_cross(const void *a, const void *b) {
    double d = ((const Cross *)a)->x - ((const Cross *)b)->x;
    return (d < 0) ? -1 : (d > 0) ? 1 : 0;
}

// 返回 0 表示成功；cov 需由调用方分配到 W*H
int fill_coverage(const double *pts, int npts, int W, int H,
                  int evenodd, int ss, unsigned char *cov)
{
    if (npts < 3) return -1;

    Edge *edges = (Edge *)malloc(sizeof(Edge) * (size_t)npts);
    if (!edges) return -2;
    int ne = 0;
    for (int i = 0; i < npts; i++) {
        int j = (i + 1) % npts;             // 闭合
        double x0 = pts[i * 2], y0 = pts[i * 2 + 1];
        double x1 = pts[j * 2], y1 = pts[j * 2 + 1];
        if (y0 == y1) continue;             // 水平边不参与
        Edge e;
        e.x0 = x0; e.y0 = y0; e.x1 = x1; e.y1 = y1;
        e.ymin = (y0 < y1) ? y0 : y1;
        e.ymax = (y0 < y1) ? y1 : y0;
        e.dir = (y1 > y0) ? 1 : -1;
        edges[ne++] = e;
    }

    double *acc = (double *)calloc((size_t)W, sizeof(double));
    Cross *cr = (Cross *)malloc(sizeof(Cross) * (size_t)(ne + 1));
    if (!acc || !cr) { free(edges); free(acc); free(cr); return -3; }

    double inv = 1.0 / ss;

    for (int y = 0; y < H; y++) {
        int touched = 0;
        int lo = W, hi = -1;
        for (int k = 0; k < ss; k++) {
            double yc = (double)y + (k + 0.5) * inv;
            int nc = 0;
            for (int i = 0; i < ne; i++) {
                if (yc < edges[i].ymin || yc >= edges[i].ymax) continue;
                double t = (yc - edges[i].y0) / (edges[i].y1 - edges[i].y0);
                cr[nc].x = edges[i].x0 + t * (edges[i].x1 - edges[i].x0);
                cr[nc].dir = edges[i].dir;
                nc++;
            }
            if (nc < 2) continue;
            qsort(cr, (size_t)nc, sizeof(Cross), cmp_cross);

            // 按 nonzero / even-odd 扫出 span
            int wind = 0;
            for (int i = 0; i < nc - 1; i++) {
                wind += cr[i].dir;
                int inside = evenodd ? ((i + 1) & 1) : (wind != 0);
                if (!inside) continue;
                double x0 = cr[i].x, x1 = cr[i + 1].x;
                if (x1 <= 0.0 || x0 >= (double)W) continue;
                if (x0 < 0.0) x0 = 0.0;
                if (x1 > (double)W) x1 = (double)W;
                if (x1 <= x0) continue;
                int ix0 = (int)floor(x0);
                int ix1 = (int)floor(x1);
                if (ix1 >= W) ix1 = W - 1;
                if (ix0 == ix1) {
                    acc[ix0] += (x1 - x0) * inv;
                } else {
                    acc[ix0] += ((double)(ix0 + 1) - x0) * inv;
                    for (int x = ix0 + 1; x < ix1; x++) acc[x] += inv;
                    acc[ix1] += (x1 - (double)ix1) * inv;
                }
                touched = 1;
                if (ix0 < lo) lo = ix0;
                if (ix1 > hi) hi = ix1;
            }
        }
        if (!touched) continue;
        int base = y * W;
        for (int x = lo; x <= hi; x++) {
            double a = acc[x];
            if (a > 0.0) {
                if (a > 1.0) a = 1.0;
                int v = (int)(a * 255.0 + 0.5);
                if (v) cov[base + x] = (unsigned char)v;
                acc[x] = 0.0;
            }
        }
    }

    free(edges); free(acc); free(cr);
    return 0;
}

// ---------------------------------------------------------------- 基准
static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

int main(int argc, char **argv) {
    int W = 1240, H = 827;              // A4 @150dpi
    int repeat = (argc > 1) ? atoi(argv[1]) : 20;
    int ss = 4;

    // 造一个 A4 大小的圆（模拟真实页面里最常见的抗锯齿填充）
    int npts = 720;
    double *pts = (double *)malloc(sizeof(double) * 2 * (size_t)(npts + 1));
    double cx = W / 2.0, cy = H / 2.0, r = (H < W ? H : W) * 0.45;
    for (int i = 0; i < npts; i++) {
        double a = 2.0 * M_PI * i / npts;
        pts[i * 2] = cx + r * cos(a);
        pts[i * 2 + 1] = cy + r * sin(a);
    }

    unsigned char *cov = (unsigned char *)calloc((size_t)W * H, 1);
    if (!cov) return 1;

    // 预热
    fill_coverage(pts, npts, W, H, 0, ss, cov);

    double t0 = now_ms();
    for (int i = 0; i < repeat; i++) {
        memset(cov, 0, (size_t)W * H);
        fill_coverage(pts, npts, W, H, 0, ss, cov);
    }
    double t1 = now_ms();

    long nz = 0;
    for (int i = 0; i < W * H; i++) if (cov[i]) nz++;

    printf("C      %d 次  平均 %.3f ms/次  非零 %ld / %d\n",
           repeat, (t1 - t0) / repeat, nz, W * H);

    free(pts); free(cov);
    return 0;
}
