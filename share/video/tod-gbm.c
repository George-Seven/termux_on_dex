// tod-gbm.so — una libgbm minima per Chromium/Electron su Android (12 novembre 2026, lib/videochip.sh). Chromium alloca i
// fotogrammi del video con GBM sul nodo di rendering; il nostro nodo è finto, quindi la memoria la prendiamo qui da
// /dev/dma_heap/system: un solo blocco per buffer, lineare (modificatore 0), righe allineate a 256 byte, piani uno
// dopo l'altro. La GPU del container (zink/turnip) importa questi blocchi con EGL_EXT_image_dma_buf_import (provato).
// Va in LD_PRELOAD prima di libgbm.so.1, solo per le app Chromium/Electron (diretto_run, tod-run).
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>

void *calloc(unsigned long, unsigned long);
void free(void *);
char *getenv(const char *);
int open(const char *, int, ...);
int close(int);
int dup(int);
int fcntl(int, int, ...);
int ioctl(int, unsigned long, ...);
long lseek(int, long, int);
long write(int, const void *, unsigned long);
void *mmap(void *, size_t, int, int, int, long);
int munmap(void *, size_t);
int vsnprintf(char *, unsigned long, const char *, va_list);

static void reg(const char *f, ...) {
    static int fd = -2;
    if (fd == -2) {
        const char *p = getenv("TOD_VA_LOG");
        fd = p ? open(p, 01 | 0100 | 02000, 0600) : -1;
    }
    if (fd < 0) return;
    char b[300];
    va_list a;
    va_start(a, f);
    int n = vsnprintf(b, sizeof b - 1, f, a);
    va_end(a);
    if (n < 0) return;
    if (n > (int)sizeof b - 2) n = sizeof b - 2;
    b[n++] = '\n';
    write(fd, b, n);
}

#define FOURCC(a, b, c, d) ((uint32_t)(a) | (uint32_t)(b) << 8 | (uint32_t)(c) << 16 | (uint32_t)(d) << 24)

// per ogni formato: numero di piani, byte per pixel di ogni piano, divisori orizzontale/verticale dei piani colore
struct formato { uint32_t cc; int piani; int bpp[3]; int sx, sy; };
static const struct formato FORMATI[] = {
    {FOURCC('N', 'V', '1', '2'), 2, {1, 2}, 2, 2},
    {FOURCC('N', 'V', '2', '1'), 2, {1, 2}, 2, 2},
    {FOURCC('P', '0', '1', '0'), 2, {2, 4}, 2, 2},
    {FOURCC('Y', 'U', '1', '2'), 3, {1, 1, 1}, 2, 2},
    {FOURCC('Y', 'V', '1', '2'), 3, {1, 1, 1}, 2, 2},
    {FOURCC('A', 'R', '2', '4'), 1, {4}, 1, 1},
    {FOURCC('X', 'R', '2', '4'), 1, {4}, 1, 1},
    {FOURCC('A', 'B', '2', '4'), 1, {4}, 1, 1},
    {FOURCC('X', 'B', '2', '4'), 1, {4}, 1, 1},
    {FOURCC('A', 'R', '3', '0'), 1, {4}, 1, 1},
    {FOURCC('X', 'R', '3', '0'), 1, {4}, 1, 1},
    {FOURCC('A', 'B', '3', '0'), 1, {4}, 1, 1},
    {FOURCC('X', 'B', '3', '0'), 1, {4}, 1, 1},
    {FOURCC('A', 'B', '4', 'H'), 1, {8}, 1, 1},
    {FOURCC('R', 'G', '1', '6'), 1, {2}, 1, 1},
    {FOURCC('R', '8', ' ', ' '), 1, {1}, 1, 1},
    {FOURCC('G', 'R', '8', '8'), 1, {2}, 1, 1},
    {FOURCC('R', '1', '6', ' '), 1, {2}, 1, 1},
    {FOURCC('G', 'R', '3', '2'), 1, {4}, 1, 1},
};
static const struct formato *formato(uint32_t cc) {
    for (unsigned i = 0; i < sizeof FORMATI / sizeof *FORMATI; i++)
        if (FORMATI[i].cc == cc) return &FORMATI[i];
    return 0;
}

struct gbm_device { int fd; };
struct gbm_bo {
    struct gbm_device *dev;
    uint32_t w, h, cc;
    int piani, fd[4];
    uint32_t stride[4], offset[4];
    size_t dim;
    void *mappa;
    void *dati;
    void (*distruggi_dati)(struct gbm_bo *, void *);
};
union gbm_bo_handle { void *ptr; int32_t s32; uint32_t u32; int64_t s64; uint64_t u64; };

#define ALLINEA(x, a) (((x) + (a) - 1) & ~((a) - 1))

struct gbm_device *gbm_create_device(int fd) {
    struct gbm_device *d = calloc(1, sizeof *d);
    if (d) d->fd = fd;
    reg("gbm: dispositivo sul descrittore %d", fd);
    return d;
}
void gbm_device_destroy(struct gbm_device *d) { free(d); }
int gbm_device_get_fd(struct gbm_device *d) { return d->fd; }
const char *gbm_device_get_backend_name(struct gbm_device *d) { return "tod"; }
int gbm_device_is_format_supported(struct gbm_device *d, uint32_t cc, uint32_t uso) { return formato(cc) != 0; }
int gbm_device_get_format_modifier_plane_count(struct gbm_device *d, uint32_t cc, uint64_t mod) {
    const struct formato *f = formato(cc);
    return f && mod == 0 ? f->piani : -1;
}

struct dma_heap_allocation_data { uint64_t len; uint32_t fd, fd_flags; uint64_t heap_flags; };
int getpid(void);
static int heap = -2;
static int heap_fd(void) { return heap; }
static int dmabuf(size_t len) {
    if (heap < 0) heap = open("/dev/dma_heap/system", 02000000 /* O_CLOEXEC */);
    if (heap < 0) return -1;
    struct dma_heap_allocation_data d = {len, 0, 02 | 02000000 /* O_RDWR | O_CLOEXEC */, 0};
    if (ioctl(heap, 0xc0184800ul /* DMA_HEAP_IOCTL_ALLOC */, &d) < 0) {
        // Chromium chiude descrittori altrui (e il numero viene riusato): si riapre e si riprova una volta
        heap = open("/dev/dma_heap/system", 02000000);
        if (heap < 0 || ioctl(heap, 0xc0184800ul, &d) < 0) return -1;
    }
    return (int)d.fd;
}

struct gbm_bo *gbm_bo_create(struct gbm_device *dev, uint32_t w, uint32_t h, uint32_t cc, uint32_t uso) {
    const struct formato *f = formato(cc);
    if (!f || !w || !h) {
        reg("gbm: creazione %ux%u %.4s rifiutata", w, h, (char *)&cc);
        return 0;
    }
    struct gbm_bo *bo = calloc(1, sizeof *bo);
    bo->dev = dev, bo->w = w, bo->h = h, bo->cc = cc, bo->piani = f->piani;
    size_t pos = 0;
    for (int i = 0; i < f->piani; i++) {
        uint32_t pw = i ? (w + f->sx - 1) / f->sx : w, ph = i ? (h + f->sy - 1) / f->sy : h;
        if (f->piani == 2 && i) pw = (w + 1) / 2;   // NV12/P010: un campione UV ogni due pixel, bpp già doppi
        bo->stride[i] = ALLINEA(pw * f->bpp[i], 256);
        bo->offset[i] = pos;
        pos += (size_t)bo->stride[i] * ALLINEA(ph, 16);
    }
    bo->dim = ALLINEA(pos, 4096);
    int fd = dmabuf(bo->dim);
    if (fd < 0) {
        reg("gbm: dma_heap rifiuta %zu byte (processo %d, heap %d)", bo->dim, getpid(), heap_fd());
        free(bo);
        return 0;
    }
    for (int i = 0; i < f->piani; i++) bo->fd[i] = i ? dup(fd) : fd;
    for (int i = f->piani; i < 4; i++) bo->fd[i] = -1;
    reg("gbm: creato %ux%u %.4s uso %#x, %d piani, passo %u, %zu byte, fd %d", w, h, (char *)&cc, uso, f->piani,
        bo->stride[0], bo->dim, fd);
    return bo;
}
struct gbm_bo *gbm_bo_create_with_modifiers(struct gbm_device *dev, uint32_t w, uint32_t h, uint32_t cc,
                                            const uint64_t *mods, unsigned n) {
    for (unsigned i = 0; i < n; i++)
        if (mods[i] == 0) return gbm_bo_create(dev, w, h, cc, 0);
    if (n) {
        reg("gbm: nessun modificatore lineare tra %u richiesti", n);
        return 0;
    }
    return gbm_bo_create(dev, w, h, cc, 0);
}
struct gbm_bo *gbm_bo_create_with_modifiers2(struct gbm_device *dev, uint32_t w, uint32_t h, uint32_t cc,
                                             const uint64_t *mods, unsigned n, uint32_t uso) {
    return gbm_bo_create_with_modifiers(dev, w, h, cc, mods, n);
}

struct gbm_import_fd_data { int fd; uint32_t width, height, stride, format; };
struct gbm_import_fd_modifier_data {
    uint32_t width, height, format, num_fds;
    int fds[4], strides[4], offsets[4];
    uint64_t modifier;
};
struct gbm_bo *gbm_bo_import(struct gbm_device *dev, uint32_t tipo, void *buf, uint32_t uso) {
    struct gbm_bo *bo = calloc(1, sizeof *bo);
    bo->dev = dev;
    for (int i = 0; i < 4; i++) bo->fd[i] = -1;
    if (tipo == 0x5503) {   // GBM_BO_IMPORT_FD
        struct gbm_import_fd_data *d = buf;
        bo->w = d->width, bo->h = d->height, bo->cc = d->format, bo->piani = 1;
        bo->fd[0] = fcntl(d->fd, 1030 /* F_DUPFD_CLOEXEC */, 0), bo->stride[0] = d->stride;
    } else if (tipo == 0x5504) {   // GBM_BO_IMPORT_FD_MODIFIER
        struct gbm_import_fd_modifier_data *d = buf;
        const struct formato *f = formato(d->format);
        bo->w = d->width, bo->h = d->height, bo->cc = d->format;
        bo->piani = f ? f->piani : (int)d->num_fds;
        for (int i = 0; i < bo->piani && i < 4; i++) {
            int src = d->fds[i < (int)d->num_fds ? i : 0];
            bo->fd[i] = fcntl(src, 1030, 0), bo->stride[i] = d->strides[i], bo->offset[i] = d->offsets[i];
        }
        if (d->modifier != 0 && d->modifier != 0x00ffffffffffffffull)
            reg("gbm: importato con modificatore %#llx (non lineare)", (unsigned long long)d->modifier);
    } else {
        reg("gbm: importazione di tipo %#x non gestita", tipo);
        free(bo);
        return 0;
    }
    long fine = lseek(bo->fd[0], 0, 2);
    lseek(bo->fd[0], 0, 0);
    bo->dim = fine > 0 ? (size_t)fine : 0;
    reg("gbm: importato %ux%u %.4s, %d piani, fd %d, %zu byte", bo->w, bo->h, (char *)&bo->cc, bo->piani, bo->fd[0],
        bo->dim);
    return bo;
}

void gbm_bo_destroy(struct gbm_bo *bo) {
    if (!bo) return;
    if (bo->distruggi_dati) bo->distruggi_dati(bo, bo->dati);
    if (bo->mappa) munmap(bo->mappa, bo->dim);
    for (int i = 0; i < 4; i++) if (bo->fd[i] >= 0) close(bo->fd[i]);
    free(bo);
}
struct gbm_device *gbm_bo_get_device(struct gbm_bo *bo) { return bo->dev; }
uint32_t gbm_bo_get_width(struct gbm_bo *bo) { return bo->w; }
uint32_t gbm_bo_get_height(struct gbm_bo *bo) { return bo->h; }
uint32_t gbm_bo_get_format(struct gbm_bo *bo) { return bo->cc; }
uint32_t gbm_bo_get_bpp(struct gbm_bo *bo) { const struct formato *f = formato(bo->cc); return f ? f->bpp[0] * 8 : 0; }
uint64_t gbm_bo_get_modifier(struct gbm_bo *bo) { return 0; }
int gbm_bo_get_plane_count(struct gbm_bo *bo) { return bo->piani; }
uint32_t gbm_bo_get_stride(struct gbm_bo *bo) { return bo->stride[0]; }
uint32_t gbm_bo_get_stride_for_plane(struct gbm_bo *bo, int p) { return p >= 0 && p < 4 ? bo->stride[p] : 0; }
uint32_t gbm_bo_get_offset(struct gbm_bo *bo, int p) { return p >= 0 && p < 4 ? bo->offset[p] : 0; }
int gbm_bo_get_fd(struct gbm_bo *bo) { return fcntl(bo->fd[0], 1030, 0); }
int gbm_bo_get_fd_for_plane(struct gbm_bo *bo, int p) {
    return p >= 0 && p < bo->piani ? fcntl(bo->fd[p], 1030, 0) : -1;
}
// le "maniglie" GEM non esistono: un numero stabile e diverso per ogni buffer basta a Chromium per distinguerli
union gbm_bo_handle gbm_bo_get_handle(struct gbm_bo *bo) {
    union gbm_bo_handle h;
    h.u64 = (uint64_t)(bo->fd[0] + 1);
    return h;
}
union gbm_bo_handle gbm_bo_get_handle_for_plane(struct gbm_bo *bo, int p) { return gbm_bo_get_handle(bo); }
void gbm_bo_set_user_data(struct gbm_bo *bo, void *d, void (*fn)(struct gbm_bo *, void *)) {
    bo->dati = d, bo->distruggi_dati = fn;
}
void *gbm_bo_get_user_data(struct gbm_bo *bo) { return bo->dati; }

// map/unmap: tutto il blocco, con DMA_BUF_IOCTL_SYNC attorno perché processore e GPU vedano le stesse cose
struct dma_buf_sync { uint64_t flags; };
static void sincronizza(int fd, uint64_t fl) {
    struct dma_buf_sync s = {fl};
    ioctl(fd, 0x40086200ul /* DMA_BUF_IOCTL_SYNC */, &s);
}
void *gbm_bo_map(struct gbm_bo *bo, uint32_t x, uint32_t y, uint32_t w, uint32_t h, uint32_t fl, uint32_t *stride,
                 void **map_data) {
    if (!bo->mappa) {
        void *m = mmap(0, bo->dim, 3, 1, bo->fd[0], 0);
        if (m == (void *)-1) {
            reg("gbm: mmap di %zu byte fallita", bo->dim);
            return 0;
        }
        bo->mappa = m;
    }
    sincronizza(bo->fd[0], 3 /* READ|WRITE, START */);
    const struct formato *f = formato(bo->cc);
    *stride = bo->stride[0];
    *map_data = bo;
    return (uint8_t *)bo->mappa + bo->offset[0] + (size_t)y * bo->stride[0] + (size_t)x * (f ? f->bpp[0] : 4);
}
void gbm_bo_unmap(struct gbm_bo *bo, void *map_data) { sincronizza(bo->fd[0], 3 | 4 /* END */); }
