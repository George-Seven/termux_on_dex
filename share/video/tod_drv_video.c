// tod_drv_video.c — driver VA-API "tod": il video delle app Chromium/Electron del container (VS Code,
// Chromium di Debian…) decodificato dal chip del telefono (12-13 novembre 2026; lib/videochip.sh).
//
// Chromium chiede al driver di decodificare H.264 e passa i dati già scomposti (parametri dell'immagine, matrici,
// slice). Il driver ricostruisce SPS e PPS bit per bit, rimette insieme il fotogramma in Annex-B e lo manda
// all'aiutante in Termux (share/video/tod-va-chip.c, socket @tod-va-chip) con il dma-buf della superficie: il chip
// (MediaCodec) decodifica e l'aiutante scrive l'NV12 direttamente lì; vaSyncSurface aspetta la risposta. Le superfici
// sono memoria /dev/dma_heap/system, che la GPU (ANGLE su Vulkan) mostra senza copie. C'è anche la conversione (VPP)
// NV12 → NV12/ARGB, che Chromium chiede quando non può mostrare l'NV12 direttamente.
// Senza l'aiutante il driver non parte: Chromium torna da solo alla decodifica col processore (mai un video finto).
// Compilato in Termux con clang per il glibc del container, senza le sue intestazioni (videochip_compila); quelle di
// libva vengono dal pacchetto libva di Termux. Registro solo con TOD_VA_LOG; TOD_VA_DIPINGI=1 (prove) dipinge un motivo.
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <va/va_backend.h>
#include <va/va_drmcommon.h>
#include <va/va_backend_vpp.h>
#include <va/va_vpp.h>

int open(const char *, int, ...);
long write(int, const void *, unsigned long);
int vsnprintf(char *, unsigned long, const char *, va_list);
char *getenv(const char *);
void *calloc(unsigned long, unsigned long);
void free(void *);
void *memcpy(void *, const void *, unsigned long);
int getpid(void);
int close(int);
int dup(int);
int fcntl(int, int, ...);
int ioctl(int, unsigned long, ...);
long lseek(int, long, int);
void *mmap(void *, size_t, int, int, int, long);
int munmap(void *, size_t);
void *memset(void *, int, unsigned long);
int memcmp(const void *, const void *, unsigned long);
void *malloc(unsigned long);

static int reg = -2;
static void scrivi(const char *f, ...) {
    if (reg == -2) {
        const char *p = getenv("TOD_VA_LOG");
        reg = p ? open(p, 01 | 0100 | 02000, 0600) : -1;   // O_WRONLY|O_CREAT|O_APPEND
    }
    if (reg < 0) return;
    char b[512];
    int n = 0;
    va_list a;
    va_start(a, f);
    n = vsnprintf(b, sizeof b, f, a);
    va_end(a);
    if (n > (int)sizeof b - 2) n = sizeof b - 2;
    b[n++] = '\n';
    write(reg, b, n);
}
#define L(...) scrivi(__VA_ARGS__)

// --- oggetti: un vettore semplice per tipo ------------------------------------------------------------------------
#define MAXO 4096
typedef struct { int usato; VABufferType tipo; unsigned size, num; void *dati; } Buf;
static Buf bufs[MAXO];
typedef struct { int usato; VAProfile prof; VAEntrypoint ep; } Conf;
static Conf confs[64];
// una superficie = un blocco dma-buf NV12 (nostro o portato da Chromium), con i due piani Y e UV
typedef struct {
    int usato, fd, nostro;
    unsigned w, h, fmt, fourcc, dim, pitch[2], offset[2];
    uint8_t *mappa;
    int attesa;             // il chip sta decodificando qui: vaSyncSurface aspetta il gettone
    unsigned gettone, ctx;
} Surf;
static Surf surfs[MAXO];
static unsigned prossimo_ctx = 1, prossima_img = 1;
typedef struct {
    int usato; VAConfigID conf; VASurfaceID bersaglio; VABufferID pipeline;
    VABufferID pp, iq, sp[128], sd[128];
    int nsp, nsd;
    int sock, aperto, rotto, cadute;   // la connessione con l'aiutante tod-va-chip (Termux)
    unsigned gettone;
    uint8_t sps[96], pps[640];
    int nsps, npps, l0, l1;
} Ctx;
static Ctx ctxs[64];
enum { APRI = 1, PACCHETTO = 2, SVUOTA = 3, CHIUDI = 4, FATTO = 10, SCARTATO = 11 };
struct msg { uint32_t tipo, len, token, w, h, p0, p1, off1, dim; };
static int manda(int s, const struct msg *h, const void *dati, int fd);


#define ALLINEA(x, a) (((x) + (a) - 1) & ~((a) - 1))
struct dma_heap_allocation_data { uint64_t len; uint32_t fd, fd_flags; uint64_t heap_flags; };
static int dmabuf(size_t len) {
    static int heap = -2;
    if (heap == -2) heap = open("/dev/dma_heap/system", 02000000);
    if (heap < 0) return -1;
    struct dma_heap_allocation_data d = {len, 0, 02 | 02000000, 0};
    if (ioctl(heap, 0xc0184800ul, &d) < 0) return -1;
    return (int)d.fd;
}
struct dma_buf_sync { uint64_t flags; };
static void sync_buf(int fd, uint64_t fl) { struct dma_buf_sync x = {fl}; ioctl(fd, 0x40086200ul, &x); }
static uint8_t *mappa(Surf *s) {
    if (!s->mappa && s->fd >= 0) {
        void *m = mmap(0, s->dim, 3, 1, s->fd, 0);
        if (m == (void *)-1) { L("mmap della superficie fallita (fd %d, %u byte)", s->fd, s->dim); return 0; }
        s->mappa = m;
    }
    return s->mappa;
}
static void libera(Surf *s) {
    if (s->mappa) munmap(s->mappa, s->dim);
    if (s->fd >= 0) close(s->fd);
    *s = (Surf){0};
}

static const VAProfile PROFILI[] = {VAProfileNone, VAProfileH264ConstrainedBaseline, VAProfileH264Main, VAProfileH264High,
                                    VAProfileVP9Profile0};   // HEVC no: VA non passa gli insiemi di riferimento della SPS   // HEVC e VP9: non ancora col chip (il driver li dipingerebbe soltanto)

static VAStatus t_Terminate(VADriverContextP c) { L("Terminate"); return VA_STATUS_SUCCESS; }
static VAStatus t_QueryConfigProfiles(VADriverContextP c, VAProfile *l, int *n) {
    *n = sizeof PROFILI / sizeof PROFILI[0];
    memcpy(l, PROFILI, sizeof PROFILI);
    L("QueryConfigProfiles -> %d", *n);
    return VA_STATUS_SUCCESS;
}
static VAStatus t_QueryConfigEntrypoints(VADriverContextP c, VAProfile p, VAEntrypoint *l, int *n) {
    l[0] = p == VAProfileNone ? VAEntrypointVideoProc : VAEntrypointVLD;
    *n = 1;
    L("QueryConfigEntrypoints profilo %d -> %s", p, p == VAProfileNone ? "VideoProc" : "VLD");
    return VA_STATUS_SUCCESS;
}
static VAStatus t_GetConfigAttributes(VADriverContextP c, VAProfile p, VAEntrypoint e, VAConfigAttrib *a, int n) {
    for (int i = 0; i < n; i++) {
        switch (a[i].type) {
        case VAConfigAttribRTFormat: a[i].value = VA_RT_FORMAT_YUV420 | (e == VAEntrypointVideoProc ? VA_RT_FORMAT_RGB32 : 0); break;
        case VAConfigAttribMaxPictureWidth: a[i].value = 4096; break;
        case VAConfigAttribMaxPictureHeight: a[i].value = 4096; break;
        default: a[i].value = VA_ATTRIB_NOT_SUPPORTED;
        }
        L("GetConfigAttributes profilo %d attributo %d -> %u", p, a[i].type, a[i].value);
    }
    return VA_STATUS_SUCCESS;
}
static VAStatus t_CreateConfig(VADriverContextP c, VAProfile p, VAEntrypoint e, VAConfigAttrib *a, int n, VAConfigID *id) {
    for (int i = 1; i < 64; i++)
        if (!confs[i].usato) {
            confs[i].usato = 1, confs[i].prof = p, confs[i].ep = e, *id = i;
            L("CreateConfig profilo %d entrypoint %d attributi %d -> %d", p, e, n, i);
            return VA_STATUS_SUCCESS;
        }
    return VA_STATUS_ERROR_ALLOCATION_FAILED;
}
static VAStatus t_DestroyConfig(VADriverContextP c, VAConfigID id) { if (id < 64) confs[id].usato = 0; L("DestroyConfig %d", id); return VA_STATUS_SUCCESS; }
static VAStatus t_QueryConfigAttributes(VADriverContextP c, VAConfigID id, VAProfile *p, VAEntrypoint *e, VAConfigAttrib *a, int *n) {
    *p = id < 64 ? confs[id].prof : VAProfileNone;
    *e = id < 64 ? confs[id].ep : VAEntrypointVLD;
    a[0].type = VAConfigAttribRTFormat, a[0].value = VA_RT_FORMAT_YUV420;
    *n = 1;
    L("QueryConfigAttributes %d", id);
    return VA_STATUS_SUCCESS;
}
static VAStatus t_CreateSurfaces2(VADriverContextP c, unsigned fmt, unsigned w, unsigned h, VASurfaceID *s, unsigned n,
                                  VASurfaceAttrib *at, unsigned na) {
    int tipo_mem = VA_SURFACE_ATTRIB_MEM_TYPE_VA;
    void *esterno = NULL;
    unsigned fourcc = fmt & VA_RT_FORMAT_RGB32 ? VA_FOURCC_ARGB : VA_FOURCC_NV12;
    for (unsigned i = 0; i < na; i++) {
        if (at[i].type == VASurfaceAttribMemoryType) tipo_mem = at[i].value.value.i;
        else if (at[i].type == VASurfaceAttribExternalBufferDescriptor) esterno = at[i].value.value.p;
        else if (at[i].type == VASurfaceAttribPixelFormat) fourcc = at[i].value.value.i;
        else L("   attributo superficie tipo %d valore %d", at[i].type, at[i].value.value.i);
    }
    L("CreateSurfaces2 formato %#x %.4s %ux%u n=%u memoria %#x%s", fmt, (char *)&fourcc, w, h, n, tipo_mem,
      esterno ? " (buffer portato)" : "");
    unsigned k = 0;
    for (unsigned i = 1; i < MAXO && k < n; i++) {
        if (surfs[i].usato) continue;
        Surf *v = &surfs[i];
        *v = (Surf){1, -1, 0, w, h, fmt, fourcc};
        if (esterno && tipo_mem == VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2) {
            VADRMPRIMESurfaceDescriptor *d = esterno;
            v->fd = fcntl(d->objects[0].fd, 1030, 0), v->dim = d->objects[0].size, v->fourcc = d->fourcc;
            unsigned p = 0;
            for (unsigned l = 0; l < d->num_layers && p < 2; l++)
                for (unsigned q = 0; q < d->layers[l].num_planes && p < 2; q++, p++)
                    v->pitch[p] = d->layers[l].pitch[q], v->offset[p] = d->layers[l].offset[q];
            if (!v->dim) { long e = lseek(v->fd, 0, 2); v->dim = e > 0 ? (unsigned)e : 0; }
            if (k == 0) L("   portata: fd %d %u byte, %u oggetti %u strati, passo %u/%u, piano UV a %u, modificatore %#llx",
                          v->fd, v->dim, d->num_objects, d->num_layers, v->pitch[0], v->pitch[1], v->offset[1],
                          (unsigned long long)d->objects[0].drm_format_modifier);
        } else if (esterno && tipo_mem == VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME) {
            VASurfaceAttribExternalBuffers *d = esterno;
            v->fd = fcntl((int)d->buffers[k < d->num_buffers ? k : 0], 1030, 0), v->dim = d->data_size;
            v->pitch[0] = d->pitches[0], v->pitch[1] = d->pitches[1], v->offset[0] = d->offsets[0], v->offset[1] = d->offsets[1];
        } else {
            v->nostro = 1;
            if (fmt & VA_RT_FORMAT_RGB32) {
                v->pitch[0] = ALLINEA(w * 4, 256);
                v->dim = ALLINEA(v->pitch[0] * ALLINEA(h, 16), 4096);
                v->fd = dmabuf(v->dim);
                if (v->fd < 0) { L("   dma_heap rifiuta %u byte", v->dim); v->usato = 0; break; }
                s[k++] = i;
                continue;
            }
            v->pitch[0] = v->pitch[1] = ALLINEA(w, 256);
            v->offset[1] = v->pitch[0] * ALLINEA(h, 16);
            v->dim = ALLINEA(v->offset[1] + v->pitch[1] * ALLINEA((h + 1) / 2, 16), 4096);
            v->fd = dmabuf(v->dim);
            if (v->fd < 0) { L("   dma_heap rifiuta %u byte", v->dim); v->usato = 0; break; }
        }
        s[k++] = i;
    }
    return k == n ? VA_STATUS_SUCCESS : VA_STATUS_ERROR_ALLOCATION_FAILED;
}
static VAStatus t_CreateSurfaces(VADriverContextP c, int w, int h, int fmt, int n, VASurfaceID *s) {
    return t_CreateSurfaces2(c, fmt, w, h, s, n, NULL, 0);
}
static VAStatus t_DestroySurfaces(VADriverContextP c, VASurfaceID *s, int n) {
    for (int i = 0; i < n; i++) if (s[i] < MAXO && surfs[s[i]].usato) libera(&surfs[s[i]]);
    L("DestroySurfaces n=%d", n);
    return VA_STATUS_SUCCESS;
}
static VAStatus t_CreateContext(VADriverContextP c, VAConfigID conf, int w, int h, int fl, VASurfaceID *s, int n, VAContextID *ctx) {
    *ctx = prossimo_ctx < 64 ? prossimo_ctx++ : 63;
    ctxs[*ctx] = (Ctx){.usato = 1, .conf = conf, .sock = -1};
    L("CreateContext config %d %dx%d superfici %d -> %d", conf, w, h, n, *ctx);
    return VA_STATUS_SUCCESS;
}
static VAStatus t_DestroyContext(VADriverContextP c, VAContextID ctx) {
    L("DestroyContext %d", ctx);
    if (ctx < 64 && ctxs[ctx].sock >= 0) {
        struct msg h = {CHIUDI, 0, 0, 0, 0, 0, 0, 0, 0};
        manda(ctxs[ctx].sock, &h, 0, -1);
        close(ctxs[ctx].sock);
        ctxs[ctx].sock = -1;
    }
    if (ctx < 64) ctxs[ctx].usato = 0;
    return VA_STATUS_SUCCESS;
}
static VAStatus t_CreateBuffer(VADriverContextP c, VAContextID ctx, VABufferType t, unsigned size, unsigned num, void *d, VABufferID *id) {
    for (unsigned i = 1; i < MAXO; i++)
        if (!bufs[i].usato) {
            bufs[i] = (Buf){1, t, size, num, calloc(num ? num : 1, size ? size : 1)};
            if (d && bufs[i].dati) memcpy(bufs[i].dati, d, (unsigned long)size * num);
            *id = i;
            if (t != VASliceDataBufferType && t != VASliceParameterBufferType)
                L("CreateBuffer tipo %d dim %u n %u -> %u", t, size, num, i);
            return VA_STATUS_SUCCESS;
        }
    return VA_STATUS_ERROR_ALLOCATION_FAILED;
}
static VAStatus t_BufferSetNumElements(VADriverContextP c, VABufferID id, unsigned n) { if (id < MAXO) bufs[id].num = n; return VA_STATUS_SUCCESS; }
static VAStatus t_MapBuffer(VADriverContextP c, VABufferID id, void **p) { *p = id < MAXO ? bufs[id].dati : NULL; return *p ? VA_STATUS_SUCCESS : VA_STATUS_ERROR_INVALID_BUFFER; }
static VAStatus t_UnmapBuffer(VADriverContextP c, VABufferID id) { return VA_STATUS_SUCCESS; }
static VAStatus t_DestroyBuffer(VADriverContextP c, VABufferID id) {
    if (id < MAXO && bufs[id].usato) free(bufs[id].dati), bufs[id].usato = 0;
    return VA_STATUS_SUCCESS;
}
static unsigned fotogrammi;
// per ora niente chip: un motivo che scorre, per vedere se il fotogramma arriva sullo schermo
static void dipingi(Surf *v, unsigned t) {
    uint8_t *m = mappa(v);
    if (!m) return;
    sync_buf(v->fd, 2);
    for (unsigned y = 0; y < v->h; y++) {
        uint8_t *r = m + v->offset[0] + (size_t)y * v->pitch[0];
        for (unsigned x = 0; x < v->w; x++) r[x] = (uint8_t)(((x + t * 4) ^ y) & 0xff) / 2 + 40;
    }
    for (unsigned y = 0; y < (v->h + 1) / 2; y++) {
        uint8_t *r = m + v->offset[1] + (size_t)y * v->pitch[1];
        for (unsigned x = 0; x < (v->w + 1) / 2; x++) r[2 * x] = (uint8_t)(64 + y), r[2 * x + 1] = (uint8_t)(200 - x / 4);
    }
    sync_buf(v->fd, 2 | 4);
}
static int e_rgb(unsigned cc) {
    return cc == VA_FOURCC_ARGB || cc == VA_FOURCC_XRGB || cc == VA_FOURCC_ABGR || cc == VA_FOURCC_XBGR ||
           cc == VA_FOURCC_BGRA || cc == VA_FOURCC_BGRX || cc == VA_FOURCC_RGBA || cc == VA_FOURCC_RGBX;
}
static inline uint8_t sat(int v) { return v < 0 ? 0 : v > 255 ? 255 : (uint8_t)v; }
static void converti(Surf *a, Surf *b) {
    uint8_t *ma = mappa(a), *mb = mappa(b);
    if (!ma || !mb) return;
    unsigned w = a->w < b->w ? a->w : b->w, h = a->h < b->h ? a->h : b->h;
    sync_buf(a->fd, 1), sync_buf(b->fd, 2);
    if (e_rgb(b->fourcc)) {   // NV12 -> 32 bit (BT.601 ridotto); ARGB/XRGB = B,G,R,A in memoria, ABGR/XBGR = R,G,B,A
        int scambia = b->fourcc == VA_FOURCC_ABGR || b->fourcc == VA_FOURCC_XBGR || b->fourcc == VA_FOURCC_RGBA ||
                      b->fourcc == VA_FOURCC_RGBX;
        for (unsigned y = 0; y < h; y++) {
            const uint8_t *py = ma + a->offset[0] + (size_t)y * a->pitch[0];
            const uint8_t *pc = ma + a->offset[1] + (size_t)(y / 2) * a->pitch[1];
            uint8_t *o = mb + b->offset[0] + (size_t)y * b->pitch[0];
            for (unsigned x = 0; x < w; x++) {
                int Y = (py[x] - 16) * 298, U = pc[x & ~1u] - 128, V = pc[(x & ~1u) + 1] - 128;
                uint8_t r = sat((Y + 409 * V + 128) >> 8), g = sat((Y - 100 * U - 208 * V + 128) >> 8),
                        bl = sat((Y + 516 * U + 128) >> 8);
                o[4 * x] = scambia ? r : bl, o[4 * x + 1] = g, o[4 * x + 2] = scambia ? bl : r, o[4 * x + 3] = 255;
            }
        }
        sync_buf(a->fd, 1 | 4), sync_buf(b->fd, 2 | 4);
        return;
    }
    for (unsigned y = 0; y < h; y++) memcpy(mb + b->offset[0] + (size_t)y * b->pitch[0], ma + a->offset[0] + (size_t)y * a->pitch[0], w);
    for (unsigned y = 0; y < (h + 1) / 2; y++)
        memcpy(mb + b->offset[1] + (size_t)y * b->pitch[1], ma + a->offset[1] + (size_t)y * a->pitch[1], (w + 1) & ~1u);
    sync_buf(a->fd, 1 | 4), sync_buf(b->fd, 2 | 4);
}
// ===================== ponte verso il chip (aiutante tod-va-chip in Termux, socket astratto) =====================
int socket(int, int, int);
int connect(int, const void *, unsigned);
int setsockopt(int, int, int, const void *, unsigned);
long sendmsg(int, const void *, int);
long send(int, const void *, unsigned long, int);
long read(int, void *, unsigned long);
struct pollfd { int fd; short events, revents; };
int poll(struct pollfd *, unsigned long, int);
struct iov { const void *b; unsigned long l; };
struct mhdr { void *name; unsigned namelen; struct iov *iov; unsigned long iovlen; void *ctl; unsigned long ctllen; int flags; };

static int collega(void) {
    int s = socket(1 /* AF_UNIX */, 1 | 02000000 /* SOCK_STREAM | SOCK_CLOEXEC */, 0);
    if (s < 0) return -1;
    struct { uint16_t fam; char p[108]; } a = {1, {0}};
    memcpy(a.p + 1, "tod-va-chip", 11);
    if (connect(s, &a, 2 + 1 + 11) < 0) { close(s); return -1; }
    // un aiutante incantato non deve fermare Chromium: un invio che non passa in 2 s = ponte rotto per questo video
    struct { long sec, usec; } limite = {2, 0};
    setsockopt(s, 1 /* SOL_SOCKET */, 21 /* SO_SNDTIMEO */, &limite, sizeof limite);
    return s;
}
static int manda(int s, const struct msg *h, const void *dati, int fd) {
    struct iov io[2] = {{h, sizeof *h}, {dati, h->len}};
    struct { unsigned long len; int level, type, fd, pad; } cm = {20, 1 /* SOL_SOCKET */, 1 /* SCM_RIGHTS */, fd, 0};
    struct mhdr m = {0, 0, io, h->len ? 2 : 1, fd >= 0 ? &cm : 0, fd >= 0 ? 24 : 0, 0};
    long n = sendmsg(s, &m, 0x4000 /* MSG_NOSIGNAL */);
    if (n < 0) return -1;
    unsigned long tot = sizeof *h + h->len;
    while ((unsigned long)n < tot) {   // il resto del carico, se sendmsg non l'ha preso tutto
        unsigned long fatto = (unsigned long)n - sizeof *h;
        long r = send(s, (const uint8_t *)dati + fatto, h->len - fatto, 0x4000);
        if (r <= 0) return -1;
        n += r;
    }
    return 0;
}
// apertura del decodificatore nell'aiutante, aspettando la sua risposta (al più 3 s): un chip che rifiuta (troppi video
// insieme, formato non suo) = ponte rotto per questo video, e Chromium avrà un errore invece di fotogrammi vuoti
static int apri_chip(Ctx *x, const void *dati, unsigned n, unsigned w, unsigned h) {
    struct msg m = {APRI, n, 0, w, h, 0, 0, 0, 0};
    if (manda(x->sock, &m, dati, -1) < 0) return -1;
    struct pollfd p = {x->sock, 1, 0};
    uint32_t r[2];
    if (poll(&p, 1, 3000) <= 0 || read(x->sock, r, 8) != 8 || r[0] != FATTO || r[1] != 0) {
        L("ponte: il chip non apre il decodificatore");
        return -1;
    }
    return 0;
}

// l'aiutante è caduto a metà video (il processo del chip è morto): ci si ricollega e si riapre il chip, al più 3 volte
// per video; il fotogramma in corso si salta (qualche fotogramma sporco fino al prossimo completo, ma il video continua).
// Alla quarta il ponte si arrende e Chromium ha un errore (13 novembre: prima il video si fermava alla prima caduta)
static void perso(Ctx *x, unsigned cid, const char *perche) {
    if (x->sock >= 0) close(x->sock);
    x->sock = -1, x->aperto = 0, x->nsps = x->npps = 0;
    if (++x->cadute > 3) x->rotto = 1;
    for (unsigned i = 1; i < MAXO; i++)   // le superfici in attesa non avranno mai risposta
        if (surfs[i].usato && surfs[i].attesa && surfs[i].ctx == cid) surfs[i].attesa = 0;
    L("ponte: %s (caduta %d, %s)", perche, x->cadute, x->rotto ? "il ponte si arrende" : "si ricollega");
}

// legge le risposte dell'aiutante (FATTO/SCARTATO + gettone) e libera le superfici; attesa in ms (0 = non aspetta)
static void risposte(Ctx *x, unsigned cid, int ms) {
    if (x->sock < 0) return;
    struct pollfd p = {x->sock, 1 /* POLLIN */, 0};
    while (poll(&p, 1, ms) > 0) {
        uint32_t r[2];
        if (read(x->sock, r, 8) != 8) { perso(x, cid, "l'aiutante ha chiuso"); return; }
        for (unsigned i = 1; i < MAXO; i++)
            if (surfs[i].usato && surfs[i].attesa && surfs[i].ctx == cid && surfs[i].gettone == r[1]) { surfs[i].attesa = 0; break; }
        ms = 0;
    }
}

typedef struct { uint8_t b[640]; int n; } Bits;
static void bit(Bits *w, int v) { if (w->n < 640 * 8) { if (v) w->b[w->n >> 3] |= (uint8_t)(0x80 >> (w->n & 7)); w->n++; } }
static void bits(Bits *w, unsigned v, int k) { for (int i = k - 1; i >= 0; i--) bit(w, (v >> i) & 1); }
static void ue(Bits *w, unsigned v) { v++; int k = 0; for (unsigned t = v; t > 1; t >>= 1) k++; bits(w, 0, k); bits(w, v, k + 1); }
static void se(Bits *w, int v) { ue(w, v > 0 ? 2u * (unsigned)v - 1 : 2u * (unsigned)(-v)); }
static void chiudi_rbsp(Bits *w) { bit(w, 1); while (w->n & 7) bit(w, 0); }
// NAL in Annex-B: 00 00 00 01, intestazione, RBSP con i byte di prevenzione 03
static int nal(uint8_t *o, uint8_t testa, const Bits *w) {
    int k = 0, zeri = 0;
    o[k++] = 0, o[k++] = 0, o[k++] = 0, o[k++] = 1, o[k++] = testa;
    for (int i = 0; i < w->n / 8; i++) {
        if (zeri >= 2 && w->b[i] <= 3) o[k++] = 3, zeri = 0;
        o[k++] = w->b[i];
        zeri = w->b[i] ? 0 : zeri + 1;
    }
    return k;
}
static int sps_h264(uint8_t *o, const VAPictureParameterBufferH264 *p, VAProfile prof) {
    Bits w = {{0}, 0};
    int alto = prof == VAProfileH264High;
    bits(&w, alto ? 100 : prof == VAProfileH264Main ? 77 : 66, 8);
    bits(&w, prof == VAProfileH264ConstrainedBaseline ? 0xC0 : prof == VAProfileH264Main ? 0x40 : 0, 8);   // constraint_set
    bits(&w, 51, 8);   // livello 5.1: basta fino al 4K
    ue(&w, 0);
    if (alto) {
        ue(&w, p->seq_fields.bits.chroma_format_idc);
        if (p->seq_fields.bits.chroma_format_idc == 3) bit(&w, 0);
        ue(&w, p->bit_depth_luma_minus8), ue(&w, p->bit_depth_chroma_minus8);
        bit(&w, 0), bit(&w, 0);   // qpprime_y_zero_transform_bypass, seq_scaling_matrix_present (le matrici vanno nella PPS)
    }
    ue(&w, p->seq_fields.bits.log2_max_frame_num_minus4);
    ue(&w, p->seq_fields.bits.pic_order_cnt_type);
    if (p->seq_fields.bits.pic_order_cnt_type == 0) ue(&w, p->seq_fields.bits.log2_max_pic_order_cnt_lsb_minus4);
    else if (p->seq_fields.bits.pic_order_cnt_type == 1) {   // i cicli non arrivano da VA: approssimazione
        bit(&w, p->seq_fields.bits.delta_pic_order_always_zero_flag);
        se(&w, 0), se(&w, 0), ue(&w, 0);
    }
    ue(&w, p->num_ref_frames);
    bit(&w, p->seq_fields.bits.gaps_in_frame_num_value_allowed_flag);
    ue(&w, p->picture_width_in_mbs_minus1);
    unsigned hm = p->picture_height_in_mbs_minus1;
    ue(&w, p->seq_fields.bits.frame_mbs_only_flag ? hm : (hm + 1) / 2 - 1);
    bit(&w, p->seq_fields.bits.frame_mbs_only_flag);
    if (!p->seq_fields.bits.frame_mbs_only_flag) bit(&w, p->seq_fields.bits.mb_adaptive_frame_field_flag);
    bit(&w, p->seq_fields.bits.direct_8x8_inference_flag);
    bit(&w, 0);   // niente ritaglio: la superficie è grande quanto i macroblocchi
    bit(&w, 1);   // VUI: solo bitstream_restriction, per non far trattenere fotogrammi al chip
    bits(&w, 0, 5), bits(&w, 0, 2);   // aspetto, overscan, segnale, croma, tempi; hrd nal e vcl
    bit(&w, 0);   // pic_struct
    bit(&w, 1);   // bitstream_restriction
    bit(&w, 1), ue(&w, 2), ue(&w, 1), ue(&w, 16), ue(&w, 16);
    ue(&w, 0);    // max_num_reorder_frames: ogni fotogramma ha la sua superficie, l'ordine lo tiene Chromium
    ue(&w, p->num_ref_frames ? p->num_ref_frames : 1);
    chiudi_rbsp(&w);
    return nal(o, 0x67, &w);
}
static void lista(Bits *w, const uint8_t *l, int n) {
    int ultimo = 8;
    for (int j = 0; j < n; j++) {
        int d = l[j] - ultimo;
        d = ((d + 128) & 255) - 128;
        se(w, d);
        ultimo = l[j];
    }
}
static int pps_h264(uint8_t *o, const VAPictureParameterBufferH264 *p, const VAIQMatrixBufferH264 *q, int l0, int l1, VAProfile prof) {
    Bits w = {{0}, 0};
    ue(&w, 0), ue(&w, 0);
    bit(&w, p->pic_fields.bits.entropy_coding_mode_flag);
    bit(&w, p->pic_fields.bits.pic_order_present_flag);
    ue(&w, 0);   // niente FMO
    ue(&w, (unsigned)l0), ue(&w, (unsigned)l1);
    bit(&w, p->pic_fields.bits.weighted_pred_flag);
    bits(&w, p->pic_fields.bits.weighted_bipred_idc, 2);
    se(&w, p->pic_init_qp_minus26), se(&w, p->pic_init_qs_minus26), se(&w, p->chroma_qp_index_offset);
    bit(&w, p->pic_fields.bits.deblocking_filter_control_present_flag);
    bit(&w, p->pic_fields.bits.constrained_intra_pred_flag);
    bit(&w, p->pic_fields.bits.redundant_pic_cnt_present_flag);
    int t8 = p->pic_fields.bits.transform_8x8_mode_flag, piatte = 1;
    if (q) {
        for (int i = 0; i < 6; i++) for (int j = 0; j < 16; j++) piatte &= q->ScalingList4x4[i][j] == 16;
        if (t8) for (int i = 0; i < 2; i++) for (int j = 0; j < 64; j++) piatte &= q->ScalingList8x8[i][j] == 16;
    }
    if (prof == VAProfileH264High && (t8 || !piatte || p->second_chroma_qp_index_offset != p->chroma_qp_index_offset)) {
        bit(&w, t8);
        bit(&w, !piatte);
        if (!piatte) {
            for (int i = 0; i < 6; i++) bit(&w, 1), lista(&w, q->ScalingList4x4[i], 16);
            if (t8) for (int i = 0; i < 2; i++) bit(&w, 1), lista(&w, q->ScalingList8x8[i], 64);
        }
        se(&w, p->second_chroma_qp_index_offset);
    }
    chiudi_rbsp(&w);
    return nal(o, 0x68, &w);
}
static int e_h264(VAProfile p) { return p == VAProfileH264ConstrainedBaseline || p == VAProfileH264Main || p == VAProfileH264High; }
static int e_vp9(VAProfile p) { return p == VAProfileVP9Profile0; }

// VP9: Chromium passa il fotogramma compresso intero (intestazioni comprese), che al chip va così com'è. I fotogrammi
// nascosti (show_frame = 0, gli "alt-ref" di libvpx) il chip li decodifica come riferimento ma non li restituisce:
// per quelli niente superficie da aspettare (sennò ognuno costerebbe l'attesa intera in vaSyncSurface)
static int ponte_vp9(VAContextID cid, Ctx *x, Surf *v) {
    if (x->rotto || !x->pp || !x->nsd || !x->nsp) return -1;
    const VADecPictureParameterBufferVP9 *pp = bufs[x->pp].dati;
    if (x->sock < 0) {
        x->sock = collega();
        if (x->sock < 0) { L("ponte: aiutante tod-va-chip non raggiungibile"); x->rotto = 1; return -1; }
    }
    if (!x->aperto) {
        // nome + '\0', niente extradata
        if (apri_chip(x, "vp9_mediacodec", 15, pp->frame_width, pp->frame_height) < 0) { x->rotto = 1; return -1; }
        x->aperto = 1;
        L("ponte: aperto vp9 %ux%u", pp->frame_width, pp->frame_height);
    }
    const VASliceParameterBufferVP9 *sl = bufs[x->sp[0]].dati;
    const uint8_t *d = (const uint8_t *)bufs[x->sd[0]].dati + sl->slice_data_offset;
    unsigned n = sl->slice_data_size;
    int mostra = pp->pic_fields.bits.show_frame;
    static int mostrati;
    if (mostrati < 3) { mostrati++; L("ponte: vp9 %u byte, inizio %02x, mostra %d, intestazione %u byte", n, d[0], mostra, pp->frame_header_length_in_bytes); }
    struct msg h = {PACCHETTO, n, ++x->gettone, v->w, v->h, v->pitch[0], v->pitch[1], v->offset[1], mostra ? v->dim : 0};
    if (mostra) v->attesa = 1, v->gettone = h.token, v->ctx = cid;
    if (manda(x->sock, &h, d, mostra ? v->fd : -1) < 0) {
        perso(x, cid, "invio fallito");
        return x->rotto ? -1 : 0;   // il fotogramma si salta
    }
    risposte(x, cid, 0);
    return 0;
}

// un'immagine H.264 decodificata da Chromium → un fotogramma Annex-B per il chip. 0 = inviato, -1 = niente ponte
static int ponte_h264(VAContextID cid, Ctx *x, Surf *v) {
    if (x->rotto || !x->pp || !x->nsd) return -1;
    VAProfile prof = confs[x->conf].prof;
    const VAPictureParameterBufferH264 *pp = bufs[x->pp].dati;
    const VAIQMatrixBufferH264 *iq = x->iq ? bufs[x->iq].dati : 0;
    // la PPS porta i num_ref_idx "predefiniti": quelli attivi della prima slice P/B (se la slice li ridichiara non conta)
    for (int i = 0; i < x->nsp; i++) {
        const VASliceParameterBufferH264 *sl = bufs[x->sp[i]].dati;
        int t = sl->slice_type % 5;
        if (t == 0 || t == 3 || t == 1) { x->l0 = sl->num_ref_idx_l0_active_minus1; if (t == 1) x->l1 = sl->num_ref_idx_l1_active_minus1; break; }
    }
    uint8_t sps[96], pps[640];
    int ns = sps_h264(sps, pp, prof), np = pps_h264(pps, pp, iq, x->l0, x->l1, prof);
    int nuove = ns != x->nsps || np != x->npps || memcmp(sps, x->sps, (unsigned long)ns) || memcmp(pps, x->pps, (unsigned long)np);
    if (x->sock < 0) {
        x->sock = collega();
        if (x->sock < 0) { L("ponte: aiutante tod-va-chip non raggiungibile, il driver dipinge"); x->rotto = 1; return -1; }
    }
    if (!x->aperto) {
        uint8_t ap[64 + 96 + 640];
        const char *nome = "h264_mediacodec";
        unsigned k = 16;
        memcpy(ap, nome, k);
        memcpy(ap + k, sps, (unsigned long)ns), k += (unsigned)ns;
        memcpy(ap + k, pps, (unsigned long)np), k += (unsigned)np;
        if (apri_chip(x, ap, k, v->w, v->h) < 0) { x->rotto = 1; return -1; }
        x->aperto = 1;
        L("ponte: aperto h264 %ux%u profilo %d, SPS %d byte, PPS %d byte", v->w, v->h, prof, ns, np);
    }
    unsigned long tot = (nuove ? (unsigned long)(ns + np) : 0);
    for (int i = 0; i < x->nsd; i++) tot += bufs[x->sd[i]].size * (bufs[x->sd[i]].num ? bufs[x->sd[i]].num : 1) + 4ul * 8;
    uint8_t *au = malloc(tot + 16);
    if (!au) return -1;
    unsigned long k = 0;
    if (nuove) {
        memcpy(au, sps, (unsigned long)ns), k += (unsigned long)ns;
        memcpy(au + k, pps, (unsigned long)np), k += (unsigned long)np;
        memcpy(x->sps, sps, (unsigned long)ns), memcpy(x->pps, pps, (unsigned long)np), x->nsps = ns, x->npps = np;
    }
    // coppie (parametri, dati) nell'ordine di arrivo; un buffer di parametri può descrivere più slice dello stesso dato
    static int mostrati;
    for (int i = 0; i < x->nsp && i < x->nsd; i++) {
        const Buf *bp = &bufs[x->sp[i]], *bd = &bufs[x->sd[i]];
        const VASliceParameterBufferH264 *sl = bp->dati;
        for (unsigned e = 0; e < (bp->num ? bp->num : 1); e++) {
            const uint8_t *d = (const uint8_t *)bd->dati + sl[e].slice_data_offset;
            unsigned n = sl[e].slice_data_size;
            if (mostrati < 4) { mostrati++; L("ponte: slice tipo %d, %u byte, inizio %02x %02x %02x %02x, bit_offset %u", sl[e].slice_type, n, d[0], d[1], d[2], d[3], sl[e].slice_data_bit_offset); }
            if (!(n > 3 && d[0] == 0 && d[1] == 0 && (d[2] == 1 || (d[2] == 0 && d[3] == 1)))) au[k++] = 0, au[k++] = 0, au[k++] = 0, au[k++] = 1;
            memcpy(au + k, d, n), k += n;
        }
    }
    struct msg h = {PACCHETTO, (uint32_t)k, ++x->gettone, v->w, v->h, v->pitch[0], v->pitch[1], v->offset[1], v->dim};
    v->attesa = 1, v->gettone = h.token, v->ctx = cid;
    int r = manda(x->sock, &h, au, v->fd);
    free(au);
    if (r < 0) {
        perso(x, cid, "invio fallito");
        return x->rotto ? -1 : 0;   // il fotogramma si salta
    }
    risposte(x, cid, 0);
    return 0;
}
// vaSyncSurface: aspetta che il chip abbia scritto la superficie (al massimo 300 ms: poi si mostra quel che c'è)
static unsigned scaduti;
static void aspetta(VASurfaceID s) {
    Surf *v = &surfs[s];
    if (!v->attesa || v->ctx >= 64) return;
    Ctx *x = &ctxs[v->ctx];
    for (int t = 0; v->attesa && x->sock >= 0 && t < 300; t += 10) risposte(x, v->ctx, 10);
    if (v->attesa) { v->attesa = 0; if (scaduti++ < 5 || scaduti % 100 == 0) L("ponte: superficie %u non pronta in 300 ms (%u volte)", s, scaduti); }
}
// =========================================================================================================

static VAStatus t_BeginPicture(VADriverContextP c, VAContextID ctx, VASurfaceID s) {
    if (fotogrammi < 5) L("BeginPicture contesto %d superficie %d", ctx, s);
    if (ctx < 64) ctxs[ctx].bersaglio = s, ctxs[ctx].pipeline = 0, ctxs[ctx].pp = ctxs[ctx].iq = 0, ctxs[ctx].nsp = ctxs[ctx].nsd = 0;
    return VA_STATUS_SUCCESS;
}
static VAStatus t_RenderPicture(VADriverContextP c, VAContextID ctx, VABufferID *b, int n) {
    for (int i = 0; i < n; i++) {
        if (b[i] >= MAXO) continue;
        if (ctx < 64) {
            Ctx *x = &ctxs[ctx];
            switch ((int)bufs[b[i]].tipo) {
            case VAProcPipelineParameterBufferType: x->pipeline = b[i]; break;
            case VAPictureParameterBufferType: x->pp = b[i]; break;
            case VAIQMatrixBufferType: x->iq = b[i]; break;
            case VASliceParameterBufferType: if (x->nsp < 128) x->sp[x->nsp++] = b[i]; break;
            case VASliceDataBufferType: if (x->nsd < 128) x->sd[x->nsd++] = b[i]; break;
            }
        }
        if (fotogrammi < 5) L("   RenderPicture buffer tipo %d dim %u n %u", bufs[b[i]].tipo, bufs[b[i]].size, bufs[b[i]].num);
    }
    return VA_STATUS_SUCCESS;
}
static unsigned conversioni;
static VAStatus t_EndPicture(VADriverContextP c, VAContextID ctx) {
    if (ctx >= 64) return VA_STATUS_ERROR_INVALID_CONTEXT;
    Ctx *x = &ctxs[ctx];
    VASurfaceID t = x->bersaglio;
    if (t >= MAXO || !surfs[t].usato) return VA_STATUS_ERROR_INVALID_SURFACE;
    if (x->pipeline) {   // conversione: sorgente -> bersaglio
        VAProcPipelineParameterBuffer *p = bufs[x->pipeline].dati;
        if (p && p->surface < MAXO && surfs[p->surface].usato) converti(&surfs[p->surface], &surfs[t]);
        if (conversioni++ < 3 || conversioni % 100 == 0)
            L("EndPicture conversione %u -> %u (%.4s -> %.4s), conversioni %u", p ? p->surface : 0, t,
              p ? (char *)&surfs[p->surface].fourcc : "????", (char *)&surfs[t].fourcc, conversioni);
        return VA_STATUS_SUCCESS;
    }
    VAProfile prof = confs[x->conf].prof;
    int fatto = e_h264(prof) ? ponte_h264(ctx, x, &surfs[t]) == 0 : e_vp9(prof) ? ponte_vp9(ctx, x, &surfs[t]) == 0 : 0;
    if (!fatto) {
        if (!getenv("TOD_VA_DIPINGI")) return VA_STATUS_ERROR_DECODING_ERROR;   // mai un video finto
        dipingi(&surfs[t], fotogrammi);
    }
    if (fotogrammi++ < 5 || fotogrammi % 100 == 0) L("EndPicture (fotogrammi %u)", fotogrammi);
    return VA_STATUS_SUCCESS;
}
static VAStatus t_SyncSurface(VADriverContextP c, VASurfaceID s) { if (s < MAXO) aspetta(s); return VA_STATUS_SUCCESS; }
static VAStatus t_QuerySurfaceStatus(VADriverContextP c, VASurfaceID s, VASurfaceStatus *st) {
    if (s < MAXO && surfs[s].attesa && surfs[s].ctx < 64) risposte(&ctxs[surfs[s].ctx], surfs[s].ctx, 0);
    *st = s < MAXO && surfs[s].attesa ? VASurfaceRendering : VASurfaceReady;
    return VA_STATUS_SUCCESS;
}
static VAStatus t_QueryImageFormats(VADriverContextP c, VAImageFormat *l, int *n) {
    l[0] = (VAImageFormat){.fourcc = VA_FOURCC_NV12, .byte_order = VA_LSB_FIRST, .bits_per_pixel = 12};
    *n = 1;
    L("QueryImageFormats -> NV12");
    return VA_STATUS_SUCCESS;
}
static VAStatus t_CreateImage(VADriverContextP c, VAImageFormat *f, int w, int h, VAImage *im) {
    VABufferID b;
    unsigned size = (unsigned)(w * h * 3 / 2);
    t_CreateBuffer(c, 0, VAImageBufferType, size, 1, NULL, &b);
    *im = (VAImage){.image_id = prossima_img++, .format = *f, .buf = b, .width = w, .height = h, .data_size = size,
                    .num_planes = 2, .pitches = {w, w}, .offsets = {0, (unsigned)(w * h)}};
    L("CreateImage %c%c%c%c %dx%d", f->fourcc & 255, (f->fourcc >> 8) & 255, (f->fourcc >> 16) & 255, f->fourcc >> 24, w, h);
    return VA_STATUS_SUCCESS;
}
static VAStatus t_DeriveImage(VADriverContextP c, VASurfaceID s, VAImage *im) { L("DeriveImage %d", s); return VA_STATUS_ERROR_OPERATION_FAILED; }
static VAStatus t_DestroyImage(VADriverContextP c, VAImageID im) { return VA_STATUS_SUCCESS; }
static VAStatus t_GetImage(VADriverContextP c, VASurfaceID s, int x, int y, unsigned w, unsigned h, VAImageID im) {
    static int volte;
    if (volte++ < 3) L("GetImage superficie %d %ux%u", s, w, h);
    return VA_STATUS_SUCCESS;
}
static VAStatus t_PutImage(VADriverContextP c, VASurfaceID s, VAImageID im, int sx, int sy, unsigned sw, unsigned sh, int dx, int dy, unsigned dw, unsigned dh) { L("PutImage"); return VA_STATUS_SUCCESS; }
static VAStatus t_QuerySurfaceAttributes(VADriverContextP c, VAConfigID conf, VASurfaceAttrib *a, unsigned *n) {
    L("QuerySurfaceAttributes config %d (spazio %u)", conf, a ? *n : 0);
    int vpp = conf < 64 && confs[conf].ep == VAEntrypointVideoProc;
    static const unsigned DEC[] = {VA_FOURCC_NV12, VA_FOURCC_P010};
    static const unsigned VPP[] = {VA_FOURCC_NV12, VA_FOURCC_P010, VA_FOURCC_ARGB, VA_FOURCC_XRGB, VA_FOURCC_ABGR, VA_FOURCC_XBGR};
    const unsigned *fm = vpp ? VPP : DEC;
    unsigned nf = vpp ? 6 : 2;
    VASurfaceAttrib v[16];
    unsigned k = 0;
    for (unsigned i = 0; i < nf; i++)
        v[k++] = (VASurfaceAttrib){.type = VASurfaceAttribPixelFormat, .flags = VA_SURFACE_ATTRIB_GETTABLE | VA_SURFACE_ATTRIB_SETTABLE,
                                   .value = {.type = VAGenericValueTypeInteger, .value.i = (int)fm[i]}};
    const int lim[][2] = {{VASurfaceAttribMinWidth, 16}, {VASurfaceAttribMinHeight, 16}, {VASurfaceAttribMaxWidth, 4096}, {VASurfaceAttribMaxHeight, 4096}};
    for (unsigned i = 0; i < 4; i++)
        v[k++] = (VASurfaceAttrib){.type = lim[i][0], .flags = VA_SURFACE_ATTRIB_GETTABLE, .value = {.type = VAGenericValueTypeInteger, .value.i = lim[i][1]}};
    v[k++] = (VASurfaceAttrib){.type = VASurfaceAttribMemoryType, .flags = VA_SURFACE_ATTRIB_GETTABLE | VA_SURFACE_ATTRIB_SETTABLE,
                               .value = {.type = VAGenericValueTypeInteger, .value.i = VA_SURFACE_ATTRIB_MEM_TYPE_VA | VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME | VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2}};
    if (!a) { *n = k; return VA_STATUS_SUCCESS; }
    if (*n < k) { *n = k; return VA_STATUS_ERROR_MAX_NUM_EXCEEDED; }
    memcpy(a, v, k * sizeof *v);
    *n = k;
    return VA_STATUS_SUCCESS;
}
static VAStatus t_ExportSurfaceHandle(VADriverContextP c, VASurfaceID s, uint32_t tipo, uint32_t fl, void *d) {
    static int volte;
    if (volte++ < 3) L("ExportSurfaceHandle superficie %d tipo %#x flag %#x", s, tipo, fl);
    if (tipo != VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2) return VA_STATUS_ERROR_UNSUPPORTED_MEMORY_TYPE;
    if (s >= MAXO || !surfs[s].usato) return VA_STATUS_ERROR_INVALID_SURFACE;
    Surf *v = &surfs[s];
    VADRMPRIMESurfaceDescriptor *o = d;
    memset(o, 0, sizeof *o);
    o->fourcc = v->fourcc, o->width = v->w, o->height = v->h, o->num_objects = 1;
    o->objects[0].fd = fcntl(v->fd, 1030, 0), o->objects[0].size = v->dim, o->objects[0].drm_format_modifier = 0;
    if (e_rgb(v->fourcc)) {
        uint32_t drm = v->fourcc == VA_FOURCC_ARGB ? 0x34325241 : v->fourcc == VA_FOURCC_XRGB ? 0x34325258
                     : v->fourcc == VA_FOURCC_ABGR ? 0x34324241 : 0x34324258;   // AR24 XR24 AB24 XB24
        o->num_layers = 1;
        o->layers[0] = (typeof(o->layers[0])){drm, 1, {0}, {v->offset[0]}, {v->pitch[0]}};
    } else if (fl & VA_EXPORT_SURFACE_SEPARATE_LAYERS) {
        o->num_layers = 2;
        o->layers[0] = (typeof(o->layers[0])){0x20203852 /* R8 */, 1, {0}, {v->offset[0]}, {v->pitch[0]}};
        o->layers[1] = (typeof(o->layers[1])){0x38385247 /* GR88 */, 1, {0}, {v->offset[1]}, {v->pitch[1]}};
    } else {
        o->num_layers = 1;
        o->layers[0] = (typeof(o->layers[0])){0x3231564e /* NV12 */, 2, {0, 0}, {v->offset[0], v->offset[1]}, {v->pitch[0], v->pitch[1]}};
    }
    return VA_STATUS_SUCCESS;
}
// conversione: nessun filtro, solo copia e cambio di formato
static VAStatus t_QueryVideoProcFilters(VADriverContextP c, VAContextID x, VAProcFilterType *f, unsigned *n) { *n = 0; L("QueryVideoProcFilters"); return VA_STATUS_SUCCESS; }
static VAStatus t_QueryVideoProcFilterCaps(VADriverContextP c, VAContextID x, VAProcFilterType t, void *caps, unsigned *n) { *n = 0; return VA_STATUS_SUCCESS; }
static VAStatus t_QueryVideoProcPipelineCaps(VADriverContextP c, VAContextID x, VABufferID *f, unsigned nf, VAProcPipelineCaps *p) {
    L("QueryVideoProcPipelineCaps");
    p->pipeline_flags = 0, p->filter_flags = 0, p->num_forward_references = 0, p->num_backward_references = 0;
    p->rotation_flags = 1 << VA_ROTATION_NONE, p->blend_flags = 0, p->mirror_flags = 0;
    p->num_input_color_standards = p->num_output_color_standards = 0;
    p->num_input_pixel_formats = p->num_output_pixel_formats = 0;
    p->max_input_width = p->max_output_width = 4096, p->max_input_height = p->max_output_height = 4096;
    p->min_input_width = p->min_output_width = 16, p->min_input_height = p->min_output_height = 16;
    return VA_STATUS_SUCCESS;
}
static VAStatus t_QueryDisplayAttributes(VADriverContextP c, VADisplayAttribute *a, int *n) { *n = 0; return VA_STATUS_SUCCESS; }
static VAStatus t_GetDisplayAttributes(VADriverContextP c, VADisplayAttribute *a, int n) { return VA_STATUS_ERROR_UNIMPLEMENTED; }
static VAStatus t_SetDisplayAttributes(VADriverContextP c, VADisplayAttribute *a, int n) { return VA_STATUS_ERROR_UNIMPLEMENTED; }
static VAStatus t_QuerySubpictureFormats(VADriverContextP c, VAImageFormat *l, unsigned *fl, unsigned *n) { *n = 0; return VA_STATUS_SUCCESS; }
static VAStatus t_PutSurface(VADriverContextP c, VASurfaceID s, void *dr, short sx, short sy, unsigned short sw, unsigned short sh,
                             short dx, short dy, unsigned short dw, unsigned short dh, VARectangle *cr, unsigned ncr, unsigned fl) {
    static int volte;
    if (volte++ < 3) L("PutSurface (X11) superficie %d %ux%u", s, dw, dh);
    return VA_STATUS_SUCCESS;
}

// obbligatorie per libva anche se non servono (sottotitoli sovrapposti, tavolozze): "non disponibili"
static VAStatus t_SetImagePalette(VADriverContextP c, VAImageID im, unsigned char *p) { return VA_STATUS_ERROR_UNIMPLEMENTED; }
static VAStatus t_CreateSubpicture(VADriverContextP c, VAImageID im, VASubpictureID *s) { return VA_STATUS_ERROR_UNIMPLEMENTED; }
static VAStatus t_DestroySubpicture(VADriverContextP c, VASubpictureID s) { return VA_STATUS_ERROR_UNIMPLEMENTED; }
static VAStatus t_SetSubpictureImage(VADriverContextP c, VASubpictureID s, VAImageID im) { return VA_STATUS_ERROR_UNIMPLEMENTED; }
static VAStatus t_SetSubpictureChromakey(VADriverContextP c, VASubpictureID s, unsigned a, unsigned b, unsigned m) { return VA_STATUS_ERROR_UNIMPLEMENTED; }
static VAStatus t_SetSubpictureGlobalAlpha(VADriverContextP c, VASubpictureID s, float a) { return VA_STATUS_ERROR_UNIMPLEMENTED; }
static VAStatus t_AssociateSubpicture(VADriverContextP c, VASubpictureID s, VASurfaceID *t, int n, short a, short b, unsigned short w, unsigned short h,
                                      short x, short y, unsigned short dw, unsigned short dh, unsigned fl) { return VA_STATUS_ERROR_UNIMPLEMENTED; }
static VAStatus t_DeassociateSubpicture(VADriverContextP c, VASubpictureID s, VASurfaceID *t, int n) { return VA_STATUS_ERROR_UNIMPLEMENTED; }

VAStatus tod_init(VADriverContextP c) {
    L("--- driver caricato: pid %d, display tipo %#x", getpid(), c->display_type);
    // senza l'aiutante del chip niente driver: Chromium decodifica col processore come prima
    int prova = collega();
    if (prova < 0 && !getenv("TOD_VA_DIPINGI")) {
        L("aiutante tod-va-chip non raggiungibile: il driver non parte");
        return VA_STATUS_ERROR_UNKNOWN;
    }
    if (prova >= 0) close(prova);
    struct VADriverVTable *v = c->vtable;
    c->version_major = VA_MAJOR_VERSION, c->version_minor = VA_MINOR_VERSION;
    c->max_profiles = 9, c->max_entrypoints = 4, c->max_attributes = 32, c->max_image_formats = 4;
    c->max_subpic_formats = 1, c->max_display_attributes = 1;
    c->str_vendor = "Termux On DeX - chip del telefono";
    v->vaTerminate = t_Terminate;
    v->vaQueryConfigProfiles = t_QueryConfigProfiles;
    v->vaQueryConfigEntrypoints = t_QueryConfigEntrypoints;
    v->vaGetConfigAttributes = t_GetConfigAttributes;
    v->vaCreateConfig = t_CreateConfig;
    v->vaDestroyConfig = t_DestroyConfig;
    v->vaQueryConfigAttributes = t_QueryConfigAttributes;
    v->vaCreateSurfaces = t_CreateSurfaces;
    v->vaCreateSurfaces2 = t_CreateSurfaces2;
    v->vaDestroySurfaces = t_DestroySurfaces;
    v->vaCreateContext = t_CreateContext;
    v->vaDestroyContext = t_DestroyContext;
    v->vaCreateBuffer = t_CreateBuffer;
    v->vaBufferSetNumElements = t_BufferSetNumElements;
    v->vaMapBuffer = t_MapBuffer;
    v->vaUnmapBuffer = t_UnmapBuffer;
    v->vaDestroyBuffer = t_DestroyBuffer;
    v->vaBeginPicture = t_BeginPicture;
    v->vaRenderPicture = t_RenderPicture;
    v->vaEndPicture = t_EndPicture;
    v->vaSyncSurface = t_SyncSurface;
    v->vaQuerySurfaceStatus = t_QuerySurfaceStatus;
    v->vaQueryImageFormats = t_QueryImageFormats;
    v->vaCreateImage = t_CreateImage;
    v->vaDeriveImage = t_DeriveImage;
    v->vaDestroyImage = t_DestroyImage;
    v->vaGetImage = t_GetImage;
    v->vaPutImage = t_PutImage;
    v->vaQuerySurfaceAttributes = t_QuerySurfaceAttributes;
    v->vaExportSurfaceHandle = t_ExportSurfaceHandle;
    v->vaQueryDisplayAttributes = t_QueryDisplayAttributes;
    v->vaGetDisplayAttributes = t_GetDisplayAttributes;
    v->vaSetDisplayAttributes = t_SetDisplayAttributes;
    v->vaQuerySubpictureFormats = t_QuerySubpictureFormats;
    v->vaPutSurface = t_PutSurface;
    v->vaSetImagePalette = t_SetImagePalette;
    v->vaCreateSubpicture = t_CreateSubpicture;
    v->vaDestroySubpicture = t_DestroySubpicture;
    v->vaSetSubpictureImage = t_SetSubpictureImage;
    v->vaSetSubpictureChromakey = t_SetSubpictureChromakey;
    v->vaSetSubpictureGlobalAlpha = t_SetSubpictureGlobalAlpha;
    v->vaAssociateSubpicture = t_AssociateSubpicture;
    v->vaDeassociateSubpicture = t_DeassociateSubpicture;
    struct VADriverVTableVPP *p = c->vtable_vpp;
    if (p) {
        p->version = VA_DRIVER_VTABLE_VPP_VERSION;
        p->vaQueryVideoProcFilters = t_QueryVideoProcFilters;
        p->vaQueryVideoProcFilterCaps = t_QueryVideoProcFilterCaps;
        p->vaQueryVideoProcPipelineCaps = t_QueryVideoProcPipelineCaps;
    }
    return VA_STATUS_SUCCESS;
}

// libva cerca __vaDriverInit_<maggiore>_<minore> dalla sua versione in giù: il driver si compila con le intestazioni di
// Termux (libva 2.24 → 1_24) ma nel container può esserci una libva più vecchia (Debian 13: 2.22 → 1_22). La tabella delle
// funzioni cresce solo in coda (spazi riservati), quindi lo stesso avvio va bene per tutte: lo si esporta con ogni nome.
#define AVVIO(m) VAStatus __vaDriverInit_1_##m(VADriverContextP c) __attribute__((alias("tod_init")));
AVVIO(0) AVVIO(1) AVVIO(2) AVVIO(3) AVVIO(4) AVVIO(5) AVVIO(6) AVVIO(7) AVVIO(8) AVVIO(9) AVVIO(10) AVVIO(11) AVVIO(12)
AVVIO(13) AVVIO(14) AVVIO(15) AVVIO(16) AVVIO(17) AVVIO(18) AVVIO(19) AVVIO(20) AVVIO(21) AVVIO(22) AVVIO(23) AVVIO(24)
AVVIO(25) AVVIO(26) AVVIO(27) AVVIO(28) AVVIO(29) AVVIO(30) AVVIO(31) AVVIO(32) AVVIO(33) AVVIO(34) AVVIO(35) AVVIO(36)
AVVIO(37) AVVIO(38) AVVIO(39) AVVIO(40)
