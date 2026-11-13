// tod-va-shim.so — un dispositivo DRM finto per Chromium/Electron (12-13 novembre 2026, lib/videochip.sh). Chromium cerca
// la scheda grafica e apre il nodo di rendering: su Android /dev/dri non c'è, e il video accelerato si spegne prima di
// caricare un driver VA-API. Qui: un nodo finto /dev/dri/renderD128 (un memfd), /sys della scheda da una cartella finta
// (TOD_VA_FINTO), DRM_IOCTL_VERSION che risponde "tod"; libva con LIBVA_DRIVER_NAME=tod carica poi il nostro driver.
// In più DRI3 Open: Termux:X11 la rifiuta, e senza Chromium non usa la GBM (tod-gbm.so) per i fotogrammi.
// In LD_PRELOAD (dopo tod-gbm.so) solo per le app riconosciute come Chromium/Electron; nei processi che non sono quell'app
// (shell, strumenti, aiutanti) si toglie dalla variabile, così i loro figli partono puliti.
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>

void *dlsym(void *, const char *);
#define RTLD_NEXT ((void *)-1l)
int strcmp(const char *, const char *);
void *calloc(unsigned long, unsigned long);
void free(void *);
char *getenv(const char *);
int memfd_create(const char *, unsigned);
long write(int, const void *, unsigned long);
int vsnprintf(char *, unsigned long, const char *, va_list);
int open(const char *, int, ...);
void *memcpy(void *, const void *, unsigned long);

#define NODO "/dev/dri/renderD128"

static void reg(const char *f, ...) {
    static int fd = -2;
    if (fd == -2) {
        const char *p = getenv("TOD_VA_LOG");
        fd = p ? ((int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "open"))(p, 01 | 0100 | 02000, 0600) : -1;
    }
    if (fd < 0) return;
    char b[300];
    va_list a;
    va_start(a, f);
    int n = vsnprintf(b, sizeof b - 1, f, a);
    va_end(a);
    if (n > (int)sizeof b - 2) n = sizeof b - 2;
    b[n++] = '\n';
    write(fd, b, n);
}

// --- libdrm: le strutture come in xf86drm.h ------------------------------------------------------------------------
typedef struct { char fullname[512]; } PlatBus;
typedef struct { char **compatible; } PlatDev;
typedef struct {
    char **nodes;
    int available_nodes;
    int bustype;
    union { void *pci; void *usb; PlatBus *platform; void *host1x; } businfo;
    union { void *pci; void *usb; PlatDev *platform; void *host1x; } deviceinfo;
} drmDevice;
typedef struct {
    int version_major, version_minor, version_patchlevel;
    int name_len; char *name;
    int date_len; char *date;
    int desc_len; char *desc;
} drmVersion;
#define DRM_NODE_RENDER 2
#define DRM_BUS_PLATFORM 2

static char *nodi[3] = {0, 0, NODO};
static PlatBus bus = {"tod-chip"};
static char *compat[] = {"termuxondex,chip", 0};
static PlatDev dev = {compat};

static drmDevice *nuovo(void) {
    drmDevice *d = calloc(1, sizeof *d);
    d->nodes = nodi;
    d->available_nodes = 1 << DRM_NODE_RENDER;
    d->bustype = DRM_BUS_PLATFORM;
    d->businfo.platform = &bus;
    d->deviceinfo.platform = &dev;
    return d;
}

int drmGetDevices2(uint32_t flags, drmDevice *devs[], int max) {
    reg("shim: drmGetDevices2 (max %d)", max);
    if (devs && max > 0) devs[0] = nuovo();
    return 1;
}
int drmGetDevices(drmDevice *devs[], int max) { return drmGetDevices2(0, devs, max); }
int drmGetDevice2(int fd, uint32_t flags, drmDevice **d) { reg("shim: drmGetDevice2 fd %d", fd); *d = nuovo(); return 0; }
int drmGetDevice(int fd, drmDevice **d) { return drmGetDevice2(fd, 0, d); }
void drmFreeDevice(drmDevice **d) { if (d && *d) free(*d), *d = 0; }
void drmFreeDevices(drmDevice *devs[], int n) { for (int i = 0; i < n; i++) if (devs[i]) free(devs[i]), devs[i] = 0; }

static drmVersion versione = {1, 0, 0, 3, "tod", 8, "20261112", 33, "Termux On DeX - chip del telefono"};
drmVersion *drmGetVersion(int fd) { reg("shim: drmGetVersion fd %d", fd); return &versione; }
void drmFreeVersion(drmVersion *v) {}

// --- la libdrm incorporata in Chromium/Electron non passa da qui: legge /dev/dri e /sys/dev/char/226:128 con le
// funzioni della libc. Quei percorsi si mandano a una scheda finta fatta di file veri (TOD_VA_FINTO, costruita da
// finto.sh); stat del nodo dice "dispositivo a caratteri 226:128" ------------------------------------------------
int snprintf(char *, unsigned long, const char *, ...);
int strncmp(const char *, const char *, unsigned long);
unsigned long strlen(const char *);
static const char *FINTO;
static const char *finto(const char *p, char *b, unsigned long n) {
    if (!p) return p;
    if (!FINTO) FINTO = getenv("TOD_VA_FINTO");
    if (!FINTO) return p;
    if (!strncmp(p, "/dev/dri", 8) || !strncmp(p, "/sys/dev/char/226:128", 21) || !strncmp(p, "/sys/class/drm", 14)) {
        snprintf(b, n, "%s%s", FINTO, p);
        static int volte;
        if (volte++ < 60) reg("shim: percorso %s", p);
        return b;
    }
    return p;
}
static int e_nodo(const char *p) { return p && !strcmp(p, NODO); }
static void come_nodo(void *st) {   // struct stat di aarch64: st_mode a 16, st_rdev a 32
    *(unsigned *)((char *)st + 16) = 0020000 | 0666;
    *(unsigned long *)((char *)st + 32) = (226ul << 8) | 128;
}
#define STAT_FN(nome, tipo_p, chiama)                                                                 \
    int nome tipo_p {                                                                                 \
        static int (*vero)();                                                                         \
        if (!vero) vero = (int (*)())dlsym(RTLD_NEXT, #nome);                                         \
        char b[600];                                                                                  \
        int r = chiama;                                                                               \
        if (r == 0 && e_nodo(p)) come_nodo(st), reg("shim: stat del nodo (%s)", #nome);              \
        return r;                                                                                     \
    }
STAT_FN(stat, (const char *p, void *st), vero(finto(p, b, sizeof b), st))
STAT_FN(stat64, (const char *p, void *st), vero(finto(p, b, sizeof b), st))
STAT_FN(lstat, (const char *p, void *st), vero(finto(p, b, sizeof b), st))
STAT_FN(lstat64, (const char *p, void *st), vero(finto(p, b, sizeof b), st))
STAT_FN(__xstat, (int v, const char *p, void *st), vero(v, finto(p, b, sizeof b), st))
STAT_FN(__xstat64, (int v, const char *p, void *st), vero(v, finto(p, b, sizeof b), st))
STAT_FN(__lxstat, (int v, const char *p, void *st), vero(v, finto(p, b, sizeof b), st))
STAT_FN(__lxstat64, (int v, const char *p, void *st), vero(v, finto(p, b, sizeof b), st))
STAT_FN(fstatat, (int d, const char *p, void *st, int fl), vero(d, finto(p, b, sizeof b), st, fl))
STAT_FN(fstatat64, (int d, const char *p, void *st, int fl), vero(d, finto(p, b, sizeof b), st, fl))
int statx(int d, const char *p, int fl, unsigned mask, void *sx) {
    static int (*vero)(int, const char *, int, unsigned, void *);
    if (!vero) vero = (int (*)(int, const char *, int, unsigned, void *))dlsym(RTLD_NEXT, "statx");
    char b[600];
    int r = vero(d, finto(p, b, sizeof b), fl, mask, sx);
    if (r == 0 && e_nodo(p)) {   // struct statx: stx_mode (u16) a 0x1c, stx_rdev_major/minor a 0x80/0x84
        *(unsigned short *)((char *)sx + 0x1c) = 0020000 | 0666;
        *(unsigned *)((char *)sx + 0x80) = 226, *(unsigned *)((char *)sx + 0x84) = 128;
        reg("shim: statx del nodo");
    }
    return r;
}
void *opendir(const char *p) {
    static void *(*vero)(const char *);
    if (!vero) vero = (void *(*)(const char *))dlsym(RTLD_NEXT, "opendir");
    char b[600];
    const char *q = finto(p, b, sizeof b);
    if (q != p) reg("shim: opendir %s -> %s", p, q);
    return vero(q);
}
long readlink(const char *p, char *buf, unsigned long n) {
    static long (*vero)(const char *, char *, unsigned long);
    if (!vero) vero = (long (*)(const char *, char *, unsigned long))dlsym(RTLD_NEXT, "readlink");
    char b[600];
    return vero(finto(p, b, sizeof b), buf, n);
}
char *realpath(const char *p, char *out) {
    static char *(*vero)(const char *, char *);
    if (!vero) vero = (char *(*)(const char *, char *))dlsym(RTLD_NEXT, "realpath");
    char b[600];
    return vero(finto(p, b, sizeof b), out);
}
int access(const char *p, int m) {
    static int (*vero)(const char *, int);
    if (!vero) vero = (int (*)(const char *, int))dlsym(RTLD_NEXT, "access");
    char b[600];
    return vero(finto(p, b, sizeof b), m);
}
void *fopen(const char *p, const char *m) {
    static void *(*vero)(const char *, const char *);
    if (!vero) vero = (void *(*)(const char *, const char *))dlsym(RTLD_NEXT, "fopen");
    char b[600];
    return vero(finto(p, b, sizeof b), m);
}
void *fopen64(const char *p, const char *m) {
    static void *(*vero)(const char *, const char *);
    if (!vero) vero = (void *(*)(const char *, const char *))dlsym(RTLD_NEXT, "fopen64");
    char b[600];
    return vero(finto(p, b, sizeof b), m);
}
// fuori dalle app Electron (aiutanti, shell, strumenti…) lo shim e la GBM non servono: si tolgono da LD_PRELOAD così
// i figli di quei processi partono puliti (in questo processo restano caricati, ma non fanno niente)
int setenv(const char *, const char *, int);
long read(int, void *, unsigned long);
int close(int);
char *strstr(const char *, const char *);
static int e_chromium(void) {
    static char c[4096];
    int fd = ((int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "open"))("/proc/self/cmdline", 0);
    if (fd < 0) return 1;
    long n = read(fd, c, sizeof c - 1);
    close(fd);
    if (n <= 0) return 1;
    for (long i = 0; i < n; i++) if (!c[i]) c[i] = ' ';
    c[n] = 0;
    // il programma che tod ha riconosciuto come Chromium/Electron (diretto_run, tod-run: TOD_VA_PROG = suo percorso nel
    // container; senza proot la riga di comando ha davanti la radice del container, quindi basta che lo contenga)
    const char *prog = getenv("TOD_VA_PROG");
    return (prog && *prog && strstr(c, prog)) || strstr(c, "--type=gpu-process");
}
static void togli_da_ld_preload(void) {
    const char *v = getenv("LD_PRELOAD");
    if (!v) return;
    char o[2048];
    unsigned long k = 0;
    while (*v && k < sizeof o - 2) {
        while (*v == ' ' || *v == ':') v++;
        const char *e = v;
        while (*e && *e != ' ' && *e != ':') e++;
        unsigned long n = (unsigned long)(e - v);
        int nostra = 0;
        for (const char *p = v; p + 6 <= e; p++) if (!strncmp(p, "tod-va", 6) || !strncmp(p, "tod-gbm", 7)) nostra = 1;
        if (n && !nostra && k + n + 1 < sizeof o) { if (k) o[k++] = ' '; memcpy(o + k, v, n), k += n; }
        v = e;
    }
    o[k] = 0;
    setenv("LD_PRELOAD", o, 1);
}
__attribute__((constructor)) static void caricato(void) {
    if (!e_chromium()) { togli_da_ld_preload(); return; }
    reg("shim: caricato");
}
static int nodo(const char *);
// Chromium è compilato con _FORTIFY_SOURCE: apre con __open64_2 e simili, non con open
int __open_2(const char *p, int fl) {
    int fd = nodo(p);
    if (fd >= 0) return fd;
    static int (*vero)(const char *, int);
    if (!vero) vero = (int (*)(const char *, int))dlsym(RTLD_NEXT, "__open_2");
    char b[600];
    return vero(finto(p, b, sizeof b), fl);
}
int __open64_2(const char *p, int fl) {
    int fd = nodo(p);
    if (fd >= 0) return fd;
    static int (*vero)(const char *, int);
    if (!vero) vero = (int (*)(const char *, int))dlsym(RTLD_NEXT, "__open64_2");
    char b[600];
    return vero(finto(p, b, sizeof b), fl);
}
int __openat_2(int d, const char *p, int fl) {
    int fd = nodo(p);
    if (fd >= 0) return fd;
    static int (*vero)(int, const char *, int);
    if (!vero) vero = (int (*)(int, const char *, int))dlsym(RTLD_NEXT, "__openat_2");
    char b[600];
    return vero(d, finto(p, b, sizeof b), fl);
}
int __openat64_2(int d, const char *p, int fl) {
    int fd = nodo(p);
    if (fd >= 0) return fd;
    static int (*vero)(int, const char *, int);
    if (!vero) vero = (int (*)(int, const char *, int))dlsym(RTLD_NEXT, "__openat64_2");
    char b[600];
    return vero(d, finto(p, b, sizeof b), fl);
}

// --- il file del nodo: un descrittore che esiste davvero -----------------------------------------------------------
static int mie[16];   // i descrittori dei nodi finti aperti
static int e_mio(int fd) { for (int i = 0; i < 16; i++) if (mie[i] == fd && fd > 0) return 1; return 0; }
static int nodo(const char *p) {
    if (!p || strcmp(p, NODO)) return -1;
    int fd = memfd_create("tod-renderD128", 1 /* MFD_CLOEXEC */);
    for (int i = 0; i < 16; i++) if (mie[i] <= 0) { mie[i] = fd; break; }
    reg("shim: apertura di %s -> %d", p, fd);
    return fd;
}

// DRM_IOCTL_VERSION (_IOWR('d', 0, struct drm_version) = 0xc0406400 su aarch64): Chromium vuole il nome del driver
struct drm_version { int maj, min, patch; unsigned long name_len; char *name; unsigned long date_len; char *date;
                     unsigned long desc_len; char *desc; };
static void copia(char *dst, unsigned long *len, const char *src) {
    unsigned long n = strlen(src);
    if (dst && *len) memcpy(dst, src, *len < n ? *len : n);
    *len = n;
}
int ioctl(int fd, unsigned long req, ...) {
    va_list a;
    va_start(a, req);
    void *arg = va_arg(a, void *);
    va_end(a);
    if (e_mio(fd)) {
        if (req == 0xc0406400ul) {
            struct drm_version *v = arg;
            v->maj = 1, v->min = 0, v->patch = 0;
            copia(v->name, &v->name_len, "tod");
            copia(v->date, &v->date_len, "20261112");
            copia(v->desc, &v->desc_len, "Termux On DeX - chip del telefono");
            reg("shim: DRM_IOCTL_VERSION su %d", fd);
            return 0;
        }
        reg("shim: ioctl %#lx sul nodo finto: non gestita", req);
        return -1;
    }
    static int (*vero)(int, unsigned long, ...);
    if (!vero) vero = (int (*)(int, unsigned long, ...))dlsym(RTLD_NEXT, "ioctl");
    return vero(fd, req, arg);
}
// fstat del nodo aperto: dispositivo a caratteri 226:128 (libva e libdrm guardano il tipo di nodo dal descrittore)
#define FSTAT_FN(nome, tipo_p, chiama, fd)                                         \
    int nome tipo_p {                                                              \
        static int (*vero)();                                                      \
        if (!vero) vero = (int (*)())dlsym(RTLD_NEXT, #nome);                      \
        int r = chiama;                                                            \
        if (r == 0 && e_mio(fd)) come_nodo(st);                                    \
        return r;                                                                  \
    }
FSTAT_FN(fstat, (int f, void *st), vero(f, st), f)
FSTAT_FN(fstat64, (int f, void *st), vero(f, st), f)
FSTAT_FN(__fxstat, (int v, int f, void *st), vero(v, f, st), f)
FSTAT_FN(__fxstat64, (int v, int f, void *st), vero(v, f, st), f)
int open(const char *p, int fl, ...) {
    int fd = nodo(p);
    if (fd >= 0) return fd;
    va_list a;
    va_start(a, fl);
    int m = va_arg(a, int);
    va_end(a);
    static int (*vero)(const char *, int, ...);
    if (!vero) vero = (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "open");
    char b[600];
    return vero(finto(p, b, sizeof b), fl, m);
}
int open64(const char *p, int fl, ...) {
    int fd = nodo(p);
    if (fd >= 0) return fd;
    va_list a;
    va_start(a, fl);
    int m = va_arg(a, int);
    va_end(a);
    static int (*vero)(const char *, int, ...);
    if (!vero) vero = (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "open64");
    char b[600];
    return vero(finto(p, b, sizeof b), fl, m);
}
int openat(int dir, const char *p, int fl, ...) {
    int fd = nodo(p);
    if (fd >= 0) return fd;
    va_list a;
    va_start(a, fl);
    int m = va_arg(a, int);
    va_end(a);
    static int (*vero)(int, const char *, int, ...);
    if (!vero) vero = (int (*)(int, const char *, int, ...))dlsym(RTLD_NEXT, "openat");
    char b[600];
    return vero(dir, finto(p, b, sizeof b), fl, m);
}
int openat64(int dir, const char *p, int fl, ...) {
    int fd = nodo(p);
    if (fd >= 0) return fd;
    va_list a;
    va_start(a, fl);
    int m = va_arg(a, int);
    va_end(a);
    static int (*vero)(int, const char *, int, ...);
    if (!vero) vero = (int (*)(int, const char *, int, ...))dlsym(RTLD_NEXT, "openat64");
    char b[600];
    return vero(dir, finto(p, b, sizeof b), fl, m);
}

#ifndef SENZA_XCB
// --- DRI3 Open: Termux:X11 ha DRI3 ma rifiuta l'Open (BadMatch). Chromium ci prende il descrittore per creare la sua
// GBM (e da lì "supports_native_pixmaps"): rispondiamo noi con il nodo finto, che arriva alla nostra tod-gbm.so.
// La richiesta si riconosce senza il codice dell'estensione: sottocodice 1, lunghezza 3, risposta con descrittori.
struct iovec { void *base; unsigned long len; };
typedef struct { unsigned count; void *ext; uint8_t opcode, isvoid; } xcb_protocol_request_t;
typedef struct { uint8_t response_type, error_code; uint16_t sequence; uint32_t resource_id; uint16_t minor; uint8_t major, pad0;
                 uint32_t pad[5]; uint32_t full_sequence; } xcb_generic_error_t;
void *malloc(unsigned long);
void *dlopen(const char *, int);
static void *xcb_vero(const char *n) {   // RTLD_NEXT non vede una libxcb aperta con dlopen locale
    void *f = dlsym(RTLD_NEXT, n);
    if (!f) {
        void *l = dlopen("libxcb.so.1", 2 | 4 /* RTLD_NOW | RTLD_NOLOAD */);
        f = l ? dlsym(l, n) : 0;
    }
    return f;
}
static unsigned dri3_seq;
static int dri3_attesa;
unsigned xcb_send_request(void *c, int fl, void *vv, const void *rr) {
    struct iovec *v = vv;
    static unsigned (*vero)(void *, int, void *, const void *);
    if (!vero) vero = (unsigned (*)(void *, int, void *, const void *))xcb_vero("xcb_send_request");
    // l'intestazione va letta PRIMA dell'invio: libxcb consuma i vettori (azzera le lunghezze)
    const uint8_t *h = v && v[0].len >= 4 ? v[0].base : 0;
    int e_open = h && (fl & 2 /* RAW: solo Chromium, non Mesa */) && (fl & 8 /* REPLY_FDS */) && h[1] == 1 &&
                 (h[2] | h[3] << 8) == 3;
    uint8_t codice = h ? h[0] : 0;
    unsigned seq = vero(c, fl, vv, rr);
    if (e_open) {
        dri3_seq = seq, dri3_attesa = 1;
        reg("shim: DRI3 Open di Chromium (codice %u), sequenza %u", codice, seq);
    }
    return seq;
}
static void *finta_open(unsigned seq) {
    uint8_t *r = malloc(32 + 4);
    for (int i = 0; i < 32; i++) r[i] = 0;
    r[0] = 1, r[1] = 1, r[2] = seq & 255, r[3] = (seq >> 8) & 255;   // risposta, 1 descrittore, sequenza, lunghezza 0
    int fd = nodo(NODO);
    memcpy(r + 32, &fd, 4);
    reg("shim: DRI3 Open: risposta finta con il nodo %d", fd);
    dri3_attesa = 0;
    return r;
}
void *xcb_wait_for_reply(void *c, unsigned seq, xcb_generic_error_t **e) {
    static void *(*vero)(void *, unsigned, xcb_generic_error_t **);
    if (!vero) vero = (void *(*)(void *, unsigned, xcb_generic_error_t **))xcb_vero("xcb_wait_for_reply");
    void *r = vero(c, seq, e);
    if (dri3_attesa && seq == dri3_seq && !r) {
        if (e && *e) reg("shim: DRI3 Open: il server risponde errore %d", (*e)->error_code), free(*e), *e = 0;
        return finta_open(seq);
    }
    return r;
}
int xcb_poll_for_reply(void *c, unsigned seq, void **reply, xcb_generic_error_t **e) {
    static int (*vero)(void *, unsigned, void **, xcb_generic_error_t **);
    if (!vero) vero = (int (*)(void *, unsigned, void **, xcb_generic_error_t **))xcb_vero("xcb_poll_for_reply");
    int fatto = vero(c, seq, reply, e);
    if (fatto && dri3_attesa && seq == dri3_seq && !*reply) {
        if (e && *e) reg("shim: DRI3 Open: il server risponde errore %d", (*e)->error_code), free(*e), *e = 0;
        *reply = finta_open(seq);
    }
    return fatto;
}
#endif
