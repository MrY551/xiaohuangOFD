// 可被 Python ctypes 调用的光栅化内核（演示用最小实现）。
//
// 编译: gcc -O3 -shared -fPIC -o libraster_c.so raster_c.c -lm
// 调用: ctypes.CDLL("libraster_c.so").ofd_fill_coverage(...)
//
// 为了不依赖 Python 的对象模型，坐标用 double* 传、覆盖率用 unsigned char* 返回，
// 内存在 C 侧用 malloc 分配，由 Python 侧调 free 释放（或复用同一块缓冲）。
#include <stdlib.h>
#include <string.h>
#include <math.h>

typedef struct {
    double x0, y0, x1, y1;
    int dir;
    double ymin, ymax;
} Edge;

typedef struct { double x; int dir; } Cross;

static int cmp_cross(const void *a, const void *b) {
    double d = ((const Cross *)a)->x - ((const Cross *)b)->x;
    return (d < 0) ? -1 : (d > 0) ? 1 : 0;
}

// 单子路径版本。pts 是扁平坐标 [x0,y0,x1,y1,...]，npts 是点数。
// 返回覆盖率缓冲（W*H 字节），由调用方 free()。
unsigned char *ofd_fill_coverage(const double *pts, int npts,
                                 int W, int H, int evenodd, int ss)
{
    if (npts < 3 || W <= 0 || H <= 0) return NULL;

    unsigned char *out = (unsigned char *)calloc((size_t)W * H, 1);
    if (!out) return NULL;

    Edge *edges = (Edge *)malloc(sizeof(Edge) * (size_t)npts);
    Cross *cr = (Cross *)malloc(sizeof(Cross) * (size_t)(npts + 1));
    double *acc = (double *)calloc((size_t)W, sizeof(double));
    if (!edges || !cr || !acc) { free(out); free(edges); free(cr); free(acc); return NULL; }

    int ne = 0;
    for (int i = 0; i < npts; i++) {
        int j = (i + 1) % npts;
        double x0 = pts[i * 2], y0 = pts[i * 2 + 1];
        double x1 = pts[j * 2], y1 = pts[j * 2 + 1];
        if (y0 == y1) continue;
        edges[ne].x0 = x0; edges[ne].y0 = y0;
        edges[ne].x1 = x1; edges[ne].y1 = y1;
        edges[ne].ymin = (y0 < y1) ? y0 : y1;
        edges[ne].ymax = (y0 < y1) ? y1 : y0;
        edges[ne].dir = (y1 > y0) ? 1 : -1;
        ne++;
    }

    const double inv = 1.0 / ss;
    for (int y = 0; y < H; y++) {
        int touched = 0, lo = W, hi = -1;
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
                int ix0 = (int)floor(x0), ix1 = (int)floor(x1);
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
                if (v) out[base + x] = (unsigned char)v;
                acc[x] = 0.0;
            }
        }
    }

    free(edges); free(cr); free(acc);
    return out;
}

// 零拷贝版本：调用方给输出缓冲，避免把 100 万字节再复制一遍。
// 这才是给 Python 用的正确接口 —— 用 bytearray 的地址直接写。
void ofd_fill_coverage_into(const double *pts, int npts,
                            int W, int H, int evenodd, int ss,
                            unsigned char *out)
{
    if (!out || npts < 3 || W <= 0 || H <= 0) return;
    memset(out, 0, (size_t)W * H);

    Edge *edges = (Edge *)malloc(sizeof(Edge) * (size_t)npts);
    Cross *cr = (Cross *)malloc(sizeof(Cross) * (size_t)(npts + 1));
    double *acc = (double *)calloc((size_t)W, sizeof(double));
    if (!edges || !cr || !acc) { free(edges); free(cr); free(acc); return; }

    int ne = 0;
    for (int i = 0; i < npts; i++) {
        int j = (i + 1) % npts;
        double x0 = pts[i * 2], y0 = pts[i * 2 + 1];
        double x1 = pts[j * 2], y1 = pts[j * 2 + 1];
        if (y0 == y1) continue;
        edges[ne].x0 = x0; edges[ne].y0 = y0;
        edges[ne].x1 = x1; edges[ne].y1 = y1;
        edges[ne].ymin = (y0 < y1) ? y0 : y1;
        edges[ne].ymax = (y0 < y1) ? y1 : y0;
        edges[ne].dir = (y1 > y0) ? 1 : -1;
        ne++;
    }

    const double inv = 1.0 / ss;
    for (int y = 0; y < H; y++) {
        int touched = 0, lo = W, hi = -1;
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
                int ix0 = (int)floor(x0), ix1 = (int)floor(x1);
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
                if (v) out[base + x] = (unsigned char)v;
                acc[x] = 0.0;
            }
        }
    }
    free(edges); free(cr); free(acc);
}

// 多子路径版本：subs 里每个元素是 "点数 后跟 npts*2 个坐标" 的扁平数组。
// 这里只做串联（组内取并集），够演示用。
unsigned char *ofd_fill_coverage_multi(const int *counts, const double *flat,
                                       int nsubs, int W, int H,
                                       int evenodd, int ss)
{
    if (nsubs < 1) return NULL;
    unsigned char *out = (unsigned char *)calloc((size_t)W * H, 1);
    if (!out) return NULL;
    int off = 0;
    for (int s = 0; s < nsubs; s++) {
        unsigned char *one = ofd_fill_coverage(flat + off, counts[s], W, H, evenodd, ss);
        if (one) {
            for (long i = 0; i < (long)W * H; i++) {
                if (one[i] > out[i]) out[i] = one[i];   // 并集取大
            }
            free(one);
        }
        off += counts[s] * 2;
    }
    return out;
}
