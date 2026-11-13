/* tod-va-chip — l'aiutante in Termux del driver VA-API "tod" (12-13 novembre 2026, lib/videochip.sh: avviato
 * dall'hook di sessione, uno per sessione).
 *
 * Il driver gira nel container (glibc) e non può usare MediaCodec; questo processo sì (bionic, ffmpeg di Termux con
 * h264_mediacodec & co., come share/media/tod-chip.c). Ascolta sul socket astratto "@tod-va-chip"; per ogni
 * connessione (= un decodificatore di Chromium) un processo figlio:
 *   APRI       carico = nome del decodificatore ffmpeg + '\0' + extradata (SPS/PPS in Annex-B)
 *   PACCHETTO  carico = un fotogramma Annex-B; con il messaggio arriva (SCM_RIGHTS) il dma-buf della superficie VA
 *              dove scrivere il risultato (passi e scostamento del piano UV nell'intestazione)
 *   SVUOTA     dopo un salto: si butta tutto quello che è in volo
 * Risposta per ogni fotogramma: FATTO + gettone (il driver aspetta quello in vaSyncSurface).
 * Compilazione (Termux): clang -O2 -o tod-va-chip tod-va-chip.c $(pkg-config --cflags --libs libavcodec libavutil)
 */
#include <libavcodec/avcodec.h>
#include <libavutil/pixfmt.h>
#include <dlfcn.h>
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

enum { APRI = 1, PACCHETTO = 2, SVUOTA = 3, CHIUDI = 4, FATTO = 10, SCARTATO = 11 };
struct msg { uint32_t tipo, len, token, w, h, p0, p1, off1, dim; };
struct risposta { uint32_t tipo, token; };

#define MAXA 64
struct attesa { int usato, fd; uint32_t token, w, h, p0, p1, off1, dim; };
static struct attesa att[MAXA];
static AVCodecContext *ctx;
static AVPacket *pkt;
static AVFrame *fr;
static int sock;
static unsigned consegnati;

#define L(...) do { fprintf(stderr, "tod-va-chip[%d]: ", getpid()); fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } while (0)

static void fili_binder(void) {
    void *b = dlopen("libbinder_ndk.so", RTLD_NOW | RTLD_GLOBAL);
    if (!b) return;
    void (*massimo)(uint32_t) = (void (*)(uint32_t))dlsym(b, "ABinderProcess_setThreadPoolMaxThreadCount");
    void (*avvia)(void) = (void (*)(void))dlsym(b, "ABinderProcess_startThreadPool");
    if (massimo) massimo(4);
    if (avvia) avvia();
}

static int leggi_tutto(int fd, void *p, size_t n) {
    uint8_t *q = p;
    while (n) {
        ssize_t r = read(fd, q, n);
        if (r <= 0) { if (r < 0 && errno == EINTR) continue; return -1; }
        q += r, n -= (size_t)r;
    }
    return 0;
}
static void rispondi(uint32_t tipo, uint32_t token) {
    struct risposta r = {tipo, token};
    if (write(sock, &r, sizeof r) != sizeof r) L("risposta non inviata");
}
static void libera(struct attesa *a) {
    if (a->fd >= 0) close(a->fd);
    a->usato = 0, a->fd = -1;
}

struct dma_buf_sync { uint64_t flags; };
/* il fotogramma del chip → la superficie VA (NV12 con i passi del driver) */
static void scrivi(struct attesa *a, AVFrame *f) {
    uint8_t *m = mmap(NULL, a->dim, PROT_READ | PROT_WRITE, MAP_SHARED, a->fd, 0);
    if (m == MAP_FAILED) { L("mmap della superficie fallita: %s", strerror(errno)); return; }
    struct dma_buf_sync s = {2 /* WRITE */};
    ioctl(a->fd, 0x40086200, &s);
    int w = f->width < (int)a->w ? f->width : (int)a->w, h = f->height < (int)a->h ? f->height : (int)a->h;
    for (int y = 0; y < h; y++) memcpy(m + (size_t)y * a->p0, f->data[0] + (size_t)y * f->linesize[0], (size_t)w);
    uint8_t *uv = m + a->off1;
    if (f->format == AV_PIX_FMT_NV12) {
        for (int y = 0; y < (h + 1) / 2; y++)
            memcpy(uv + (size_t)y * a->p1, f->data[1] + (size_t)y * f->linesize[1], (size_t)((w + 1) & ~1));
    } else if (f->format == AV_PIX_FMT_YUV420P) {   /* I420 → NV12 */
        for (int y = 0; y < (h + 1) / 2; y++) {
            uint8_t *o = uv + (size_t)y * a->p1;
            const uint8_t *u = f->data[1] + (size_t)y * f->linesize[1], *v = f->data[2] + (size_t)y * f->linesize[2];
            for (int x = 0; x < (w + 1) / 2; x++) o[2 * x] = u[x], o[2 * x + 1] = v[x];
        }
    } else {
        static int detto;
        if (!detto++) L("formato dei fotogrammi %d non gestito", f->format);
    }
    s.flags = 2 | 4 /* WRITE | END */;
    ioctl(a->fd, 0x40086200, &s);
    munmap(m, a->dim);
}

static void ricevi(void) {
    if (!ctx) return;
    for (;;) {
        int r = avcodec_receive_frame(ctx, fr);
        if (r == AVERROR(EAGAIN) || r == AVERROR_EOF) return;
        if (r < 0) { L("receive_frame: %d", r); return; }
        uint32_t token = (uint32_t)(fr->pts / 33333);
        struct attesa *a = NULL;
        for (int i = 0; i < MAXA; i++) if (att[i].usato && att[i].token == token) { a = &att[i]; break; }
        if (a) {
            scrivi(a, fr);
            libera(a);
            rispondi(FATTO, token);
            if (consegnati++ < 3 || consegnati % 300 == 0)
                L("fotogramma %u (%dx%d formato %d) consegnato, totale %u", token, fr->width, fr->height, fr->format, consegnati);
        } else L("fotogramma con gettone %u senza superficie", token);
        av_frame_unref(fr);
    }
}

static void apri(const struct msg *h, uint8_t *dati) {
    const char *nome = (const char *)dati;
    size_t nl = strnlen(nome, h->len);
    const AVCodec *c = avcodec_find_decoder_by_name(nome);
    if (!c) { L("decodificatore %s assente", nome); return; }
    if (ctx) avcodec_free_context(&ctx);
    ctx = avcodec_alloc_context3(c);
    ctx->width = (int)h->w, ctx->height = (int)h->h;
    size_t ne = h->len > nl + 1 ? h->len - nl - 1 : 0;
    if (ne) {
        ctx->extradata = av_mallocz(ne + AV_INPUT_BUFFER_PADDING_SIZE);
        memcpy(ctx->extradata, dati + nl + 1, ne);
        ctx->extradata_size = (int)ne;
    }
    ctx->pkt_timebase = (AVRational){1, 1000000};
    int r = avcodec_open2(ctx, c, NULL);
    L("apertura di %s %ux%u, extradata %zu byte: %d", nome, h->w, h->h, ne, r);
    if (r < 0) avcodec_free_context(&ctx);
}

static void pacchetto(const struct msg *h, uint8_t *dati, int fd) {
    struct attesa *a = NULL;
    for (int i = 0; i < MAXA; i++) if (!att[i].usato) { a = &att[i]; break; }
    if (!ctx || !a || fd < 0) {
        if (fd >= 0) close(fd);
        rispondi(SCARTATO, h->token);
        return;
    }
    *a = (struct attesa){1, fd, h->token, h->w, h->h, h->p0, h->p1, h->off1, h->dim};
    av_packet_unref(pkt);
    if (av_new_packet(pkt, (int)h->len) < 0) return;
    memcpy(pkt->data, dati, h->len);
    pkt->pts = pkt->dts = (int64_t)h->token * 33333;
    for (int giri = 0; giri < 500; giri++) {
        int r = avcodec_send_packet(ctx, pkt);
        if (r == AVERROR(EAGAIN)) { ricevi(); usleep(1000); continue; }
        if (r < 0) { L("pacchetto %u rifiutato: %d", h->token, r); libera(a); rispondi(SCARTATO, h->token); }
        break;
    }
    ricevi();
}

static void svuota(void) {
    if (ctx) avcodec_flush_buffers(ctx);
    for (int i = 0; i < MAXA; i++)
        if (att[i].usato) rispondi(SCARTATO, att[i].token), libera(&att[i]);
}

static void servi(int s) {
    sock = s;
    fili_binder();
    pkt = av_packet_alloc();
    fr = av_frame_alloc();
    for (int i = 0; i < MAXA; i++) att[i].fd = -1;
    for (;;) {
        int in_volo = 0;
        for (int i = 0; i < MAXA; i++) in_volo += att[i].usato;
        struct pollfd p = {s, POLLIN, 0};
        int r = poll(&p, 1, in_volo ? 3 : 200);
        if (r < 0 && errno == EINTR) continue;
        if (r == 0) { ricevi(); continue; }
        struct msg h;
        char cbuf[CMSG_SPACE(sizeof(int))];
        struct iovec io = {&h, sizeof h};
        struct msghdr mh = {.msg_iov = &io, .msg_iovlen = 1, .msg_control = cbuf, .msg_controllen = sizeof cbuf};
        ssize_t n = recvmsg(s, &mh, MSG_WAITALL);
        if (n <= 0) break;
        if (n != sizeof h) { L("intestazione corta (%zd)", n); break; }
        int fd = -1;
        for (struct cmsghdr *c = CMSG_FIRSTHDR(&mh); c; c = CMSG_NXTHDR(&mh, c))
            if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_RIGHTS) memcpy(&fd, CMSG_DATA(c), sizeof fd);
        uint8_t *dati = h.len ? malloc(h.len + 1) : NULL;
        if (h.len && (!dati || leggi_tutto(s, dati, h.len) < 0)) break;
        if (dati) dati[h.len] = 0;
        switch (h.tipo) {
        case APRI: apri(&h, dati); break;
        case PACCHETTO: pacchetto(&h, dati, fd); fd = -1; break;
        case SVUOTA: svuota(); break;
        case CHIUDI: free(dati); goto fine;
        default: L("messaggio %u sconosciuto", h.tipo);
        }
        if (fd >= 0) close(fd);
        free(dati);
    }
fine:
    L("fine della connessione (%u fotogrammi)", consegnati);
    if (ctx) avcodec_free_context(&ctx);
    _exit(0);
}

int main(void) {
    signal(SIGCHLD, SIG_IGN);   /* i figli si raccolgono da soli */
    int l = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    struct sockaddr_un a = {.sun_family = AF_UNIX};
    const char nome[] = "tod-va-chip";
    memcpy(a.sun_path + 1, nome, sizeof nome - 1);   /* astratto: lo vedono anche i processi del container */
    socklen_t al = (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 1 + sizeof nome - 1);
    if (bind(l, (struct sockaddr *)&a, al) < 0 || listen(l, 8) < 0) {
        L("socket @%s occupato: %s", nome, strerror(errno));
        return 1;
    }
    L("in ascolto su @%s", nome);
    for (;;) {
        int c = accept(l, NULL, NULL);
        if (c < 0) { if (errno == EINTR) continue; break; }
        if (fork() == 0) { close(l); servi(c); }
        close(c);
    }
    return 0;
}
