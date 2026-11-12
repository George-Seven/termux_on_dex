/* tod-smb: le cartelle condivise di un PC (o di un NAS) come le partizioni di tod-lkl. Un piccolo server SFTP su
 * RUN/lkl-<alias>.sock, che tod-fs.so raggiunge da /media/rete/<PC>; sotto non c'è LKL ma Samba (libsmbclient):
 * share/usb/lkl/sftp.c si include così com'è, e le chiamate lkl_sys_* che usa sono fatte qui. La radice "/" sono
 * le cartelle condivise del PC (senza quelle nascoste, NOME$); "/Documenti/x" è smb://PC/Documenti/x.
 *
 *   tod-smb RUN ALIAS INDIRIZZO[:PORTA] NOME       (lo lancia share/rete/tod-rete.py)
 *
 * Un processo per collegamento (fork): libsmbclient non regge più fili. Le credenziali si chiedono al servizio
 * (RUN/servizio.sock): "AUTH\t<nome>\t<pc>\t<cartella>\t<motivo>\n" → "OK\t<dominio>\t<utente>\t<password>\n" (campi
 * con %XX, dominio "-" se non c'è) o "NO\n" (da ospite). Il motivo: "primo" (la prima volta: il servizio dà quelle
 * che sa, o niente, e si prova da ospite), "ospite" (la cartella non accetta ospiti), "rifiutate" (utente e password
 * non sono passati). Libsmbclient chiede le credenziali a ogni operazione (per trovare la connessione già aperta):
 * la risposta si ricorda qui, per cartella, e si richiede solo dopo un rifiuto. La finestra la mostra il servizio. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <libsmbclient.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/time.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

/* ---------- quello che sftp.c si aspetta da LKL ---------- */
#define LKL_ENOENT ENOENT
#define LKL_ENOTDIR ENOTDIR
#define LKL_EACCES EACCES
#define LKL_EPERM EPERM
#define LKL_EROFS EROFS
#define LKL_EOPNOTSUPP EOPNOTSUPP
#define LKL_EINVAL EINVAL
#define LKL_EEXIST EEXIST
#define LKL_ENOSPC ENOSPC
#define LKL_O_RDONLY O_RDONLY
#define LKL_O_WRONLY O_WRONLY
#define LKL_O_RDWR O_RDWR
#define LKL_O_APPEND O_APPEND
#define LKL_O_CREAT O_CREAT
#define LKL_O_TRUNC O_TRUNC
#define LKL_O_EXCL O_EXCL
#define LKL_O_DIRECTORY O_DIRECTORY
#define LKL_AT_FDCWD AT_FDCWD
#define LKL_RENAME_NOREPLACE 1
#define LKL_S_ISDIR S_ISDIR
#define LKL_S_ISLNK S_ISLNK

struct lkl_stat { uint64_t st_size; uint32_t st_mode; long lkl_st_atime, lkl_st_mtime; };
struct lkl_linux_dirent64 { uint64_t d_ino; int64_t d_off; unsigned short d_reclen; unsigned char d_type; char d_name[]; };
struct __lkl__kernel_timespec { long tv_sec, tv_nsec; };
struct lkl_statfs { uint64_t f_bsize, f_frsize, f_blocks, f_bfree, f_bavail, f_files, f_ffree, f_namelen; };

static char radice[512];   /* smb://INDIRIZZO[:PORTA] */
static size_t lrad;
static char nome_pc[256], run_dir[512];

/* percorso di sftp.c (radice + /cartella/...) → indirizzo per libsmbclient: "%" e "?" nei nomi vanno scritti %XX */
static void url(const char *p, char *out, size_t n) {
    size_t l = snprintf(out, n, "%s", radice);
    for (const char *c = p + (strncmp(p, radice, lrad) ? 0 : lrad); *c && l + 4 < n; c++) {
        if (*c == '%' || *c == '?' || *c == '#') l += snprintf(out + l, n - l, "%%%02X", (unsigned char)*c);
        else out[l++] = *c;
    }
    out[l] = 0;
    if (l == lrad && l + 1 < n) { out[l++] = '/'; out[l] = 0; }   /* la radice del PC: le sue cartelle condivise */
}
static int e_radice(const char *p) { return !strcmp(p, radice) || !strcmp(p + lrad, "/"); }
static int e_cartella_condivisa(const char *p) {   /* /Documenti: una sola parte dopo la radice */
    const char *q = p + lrad;
    return *q == '/' && q[1] && !strchr(q + 1, '/');
}

/* ---------- credenziali: le chiede il servizio (e la finestra della password) ---------- */
#define MAXC 32
static struct cred { char cartella[256], wg[128], un[256], pw[256], motivo[16]; int ospite, data; } creds[MAXC];

static struct cred *cred_di(const char *shr, int crea) {
    for (int i = 0; i < MAXC; i++) if (creds[i].cartella[0] && !strcasecmp(creds[i].cartella, shr)) return &creds[i];
    if (!crea) return NULL;
    for (int i = 0; i < MAXC; i++)
        if (!creds[i].cartella[0]) { memset(&creds[i], 0, sizeof creds[i]); snprintf(creds[i].cartella, sizeof creds[i].cartella, "%s", shr); return &creds[i]; }
    return NULL;
}

static void decodifica(const char *in, char *out, int n) {   /* %XX → byte */
    int l = 0;
    for (; *in && l + 1 < n; in++) {
        if (*in == '%' && in[1] && in[2]) { char h[3] = {in[1], in[2], 0}; out[l++] = (char)strtol(h, NULL, 16); in += 2; }
        else out[l++] = *in;
    }
    out[l] = 0;
}

static void chiedi_al_servizio(const char *srv, struct cred *c) {
    char p[600], richiesta[1024], risposta[2048];
    snprintf(p, sizeof p, "%s/servizio.sock", run_dir);
    c->wg[0] = c->un[0] = c->pw[0] = 0;
    c->ospite = 1;
    c->data = 1;
    int s = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    struct sockaddr_un sa = {.sun_family = AF_UNIX};
    snprintf(sa.sun_path, sizeof sa.sun_path, "%s", p);
    if (s < 0 || connect(s, (struct sockaddr *)&sa, sizeof sa)) { if (s >= 0) close(s); return; }
    int k = snprintf(richiesta, sizeof richiesta, "AUTH\t%s\t%s\t%s\t%s\n", nome_pc, srv, c->cartella,
                     c->motivo[0] ? c->motivo : "primo");
    size_t l = 0;
    if (write(s, richiesta, k) == k) {
        struct pollfd pf = {s, POLLIN, 0};
        while (l + 1 < sizeof risposta && poll(&pf, 1, 300000) > 0) {   /* la finestra aspetta l'utente: fino a 5 minuti */
            ssize_t r = read(s, risposta + l, sizeof risposta - 1 - l);
            if (r <= 0) break;
            l += r;
            if (memchr(risposta, '\n', l)) break;
        }
    }
    close(s);
    risposta[l] = 0;
    risposta[strcspn(risposta, "\n")] = 0;
    if (strncmp(risposta, "OK\t", 3)) return;
    char *campi[3] = {0}, *sp = NULL, *t = risposta + 3;
    for (int i = 0; i < 3; i++, t = NULL) campi[i] = strtok_r(t, "\t", &sp);
    if (!campi[1]) return;
    if (campi[0] && strcmp(campi[0], "-")) decodifica(campi[0], c->wg, sizeof c->wg);
    decodifica(campi[1], c->un, sizeof c->un);
    if (campi[2]) decodifica(campi[2], c->pw, sizeof c->pw);
    c->ospite = 0;
}

static void credenziali(const char *srv, const char *shr, char *wg, int wglen, char *un, int unlen, char *pw, int pwlen) {
    struct cred *c = cred_di(shr, 1);
    if (!c) { un[0] = pw[0] = 0; return; }
    if (!c->data) chiedi_al_servizio(srv, c);
    if (c->wg[0]) snprintf(wg, wglen, "%s", c->wg);
    snprintf(un, unlen, "%s", c->un);
    snprintf(pw, pwlen, "%s", c->pw);
}

/* la cartella condivisa di un percorso ("IPC$" per la radice del PC: l'elenco delle cartelle) */
static void cartella_di(const char *p, char *out, size_t n) {
    const char *q = p + lrad;
    while (*q == '/') q++;
    size_t l = strcspn(q, "/");
    if (!l) snprintf(out, n, "IPC$");
    else snprintf(out, n, "%.*s", (int)(l < n - 1 ? l : n - 1), q);
}

/* un rifiuto (permesso negato) dopo un accesso da ospite, o sulla cartella condivisa stessa con utente e password: si
 * dimentica la risposta e l'operazione si ripete una volta, chiedendo di nuovo (al servizio, con il motivo). Un rifiuto
 * su un file o una cartella qualunque, con utente e password, è un permesso vero: niente da richiedere */
static int da_ripetere(const char *p) {
    char shr[256];
    cartella_di(p, shr, sizeof shr);
    struct cred *c = cred_di(shr, 0);
    if (!c || !c->data) return 0;
    if (c->ospite) snprintf(c->motivo, sizeof c->motivo, "ospite");
    else if (e_radice(p) || e_cartella_condivisa(p)) snprintf(c->motivo, sizeof c->motivo, "rifiutate");
    else return 0;
    c->data = 0;
    return 1;
}
#define RIPROVA(p, espr) ({ long _r = (espr); if ((_r == -EACCES || _r == -EPERM) && da_ripetere(p)) _r = (espr); _r; })

/* ---------- handle aperti (libsmbclient dà numeri suoi, per file e cartelle) ---------- */
#define MAXA 256
static struct aperto { int usato, fd, dir; char percorso[1100]; } aperti[MAXA];
static struct aperto *aperto_di(int fd) {
    for (int i = 0; i < MAXA; i++) if (aperti[i].usato && aperti[i].fd == fd) return &aperti[i];
    return NULL;
}

/* gli attributi dell'ultimo elenco letto: sftp.c fa lstat su ogni voce, e senza questi sarebbe un viaggio in rete
 * per ogni file */
#define MAXV 512
static struct voce { char nome[256]; struct lkl_stat st; } lotto[MAXV];
static int nlotto;
static char lotto_dir[1100];
static void lotto_via(void) { nlotto = 0; lotto_dir[0] = 0; }

static void da_stat(const struct stat *x, struct lkl_stat *st) {
    st->st_size = x->st_size; st->st_mode = x->st_mode;
    st->lkl_st_atime = x->st_atime; st->lkl_st_mtime = x->st_mtime;
}
static void finta_cartella(struct lkl_stat *st) { st->st_size = 0; st->st_mode = S_IFDIR | 0755; st->lkl_st_atime = st->lkl_st_mtime = time(NULL); }

static const char *lkl_strerror(long e) { return strerror(e < 0 ? -e : e); }

static long lkl_sys_stat(const char *p, struct lkl_stat *st) {
    if (e_radice(p)) { finta_cartella(st); return 0; }
    const char *b = strrchr(p, '/');
    if (b && nlotto && (size_t)(b - p) == strlen(lotto_dir) && !strncmp(p, lotto_dir, b - p))
        for (int i = 0; i < nlotto; i++) if (!strcmp(lotto[i].nome, b + 1)) { *st = lotto[i].st; return 0; }
    char u[2200]; struct stat x;
    url(p, u, sizeof u);
    long r = RIPROVA(p, smbc_stat(u, &x) ? -errno : 0);
    if (r && e_cartella_condivisa(p)) {   /* alcuni server non danno lo stato della cartella condivisa: si apre */
        int d = smbc_opendir(u);
        if (d >= 0) { smbc_closedir(d); finta_cartella(st); return 0; }
    }
    if (!r) da_stat(&x, st);
    return r;
}
#define lkl_sys_lstat lkl_sys_stat

static long lkl_sys_fstat(int fd, struct lkl_stat *st) {
    struct stat x;
    if (smbc_fstat(fd, &x)) return -errno;
    da_stat(&x, st);
    return 0;
}

static long apri(const char *u, int flag, int modo) {
    int fd = flag & O_DIRECTORY ? smbc_opendir(u) : smbc_open(u, flag & ~O_DIRECTORY, modo);
    return fd < 0 ? -errno : fd;
}

static long lkl_sys_open(const char *p, int flag, int modo) {
    char u[2200];
    struct aperto *a = NULL;
    for (int i = 0; i < MAXA && !a; i++) if (!aperti[i].usato) a = &aperti[i];
    if (!a) return -EMFILE;
    url(p, u, sizeof u);
    long fd = RIPROVA(p, apri(u, flag, modo));   /* la seconda volta solo se la prima è stata rifiutata: niente aperto */
    if (fd < 0) return fd;
    *a = (struct aperto){1, (int)fd, !!(flag & O_DIRECTORY), ""};
    snprintf(a->percorso, sizeof a->percorso, "%s", p);
    if (flag & (O_WRONLY | O_RDWR)) lotto_via();
    return fd;
}

static long lkl_sys_close(int fd) {
    struct aperto *a = aperto_di(fd);
    if (!a) return -EBADF;
    a->usato = 0;
    return (a->dir ? smbc_closedir(fd) : smbc_close(fd)) ? -errno : 0;
}

/* le voci della cartella (getdents64 di Linux), con gli attributi messi da parte per lkl_sys_stat. Nella radice:
 * solo le cartelle condivise vere (niente stampanti, IPC$, C$...) */
static long lkl_sys_getdents64(int fd, struct lkl_linux_dirent64 *buf, unsigned n) {
    struct aperto *a = aperto_di(fd);
    if (!a || !a->dir) return -EBADF;
    nlotto = 0;
    snprintf(lotto_dir, sizeof lotto_dir, "%s", a->percorso);
    unsigned l = 0;
    int radice_pc = e_radice(a->percorso);
    while (nlotto < MAXV) {
        off_t dove = smbc_telldir(fd);
        const char *nome = NULL;
        struct lkl_stat st;
        if (radice_pc) {
            struct smbc_dirent *de = smbc_readdir(fd);
            if (!de) break;
            if (de->smbc_type != SMBC_FILE_SHARE || !de->name[0] || de->name[strlen(de->name) - 1] == '$') continue;
            nome = de->name;
            finta_cartella(&st);
        } else {
            struct stat x;
            const struct libsmb_file_info *fi = smbc_readdirplus2(fd, &x);
            if (!fi) break;
            nome = fi->name;
            if (!strcmp(nome, ".") || !strcmp(nome, "..")) continue;
            da_stat(&x, &st);
        }
        size_t ln = strlen(nome);
        unsigned rec = (sizeof(struct lkl_linux_dirent64) + ln + 1 + 7) & ~7u;
        if (l + rec > n || ln >= sizeof lotto[0].nome) {   /* non ci sta: la prossima volta */
            if (l + rec > n) { smbc_lseekdir(fd, dove); break; }
            continue;
        }
        struct lkl_linux_dirent64 *d = (struct lkl_linux_dirent64 *)((char *)buf + l);
        d->d_ino = nlotto + 1; d->d_off = 0; d->d_reclen = rec;
        d->d_type = S_ISDIR(st.st_mode) ? 4 : 8;
        memcpy(d->d_name, nome, ln + 1);
        l += rec;
        snprintf(lotto[nlotto].nome, sizeof lotto[nlotto].nome, "%s", nome);
        lotto[nlotto++].st = st;
    }
    return l;
}

static long lkl_sys_pread64(int fd, char *b, unsigned long n, long long off) {
    if (smbc_lseek(fd, off, SEEK_SET) < 0) return -errno;
    ssize_t r = smbc_read(fd, b, n);
    return r < 0 ? -errno : r;
}

static long lkl_sys_pwrite64(int fd, const char *b, unsigned long n, long long off) {
    if (smbc_lseek(fd, off, SEEK_SET) < 0) return -errno;
    ssize_t r = smbc_write(fd, b, n);
    lotto_via();
    return r < 0 ? -errno : r;
}

static long lkl_sys_ftruncate(int fd, long long n) { lotto_via(); return smbc_ftruncate(fd, n) ? -errno : 0; }
static long lkl_sys_truncate(const char *p, long long n) {
    char u[2200]; url(p, u, sizeof u);
    long fd = RIPROVA(p, apri(u, O_WRONLY, 0));
    if (fd < 0) return fd;
    long r = smbc_ftruncate(fd, n) ? -errno : 0;
    smbc_close(fd);
    lotto_via();
    return r;
}
static long lkl_sys_fchmod(int fd, int m) { (void)fd; (void)m; return -EOPNOTSUPP; }   /* SMB non ha i permessi di Linux */
static long lkl_sys_chmod(const char *p, int m) { (void)p; (void)m; return -EOPNOTSUPP; }

static long lkl_sys_utimensat(int dd, const char *p, const struct __lkl__kernel_timespec *t, int f) {
    (void)f;
    if (!p) { struct aperto *a = aperto_di(dd); if (!a) return -EBADF; p = a->percorso; }
    char u[2200]; url(p, u, sizeof u);
    struct timeval tv[2] = {{t[0].tv_sec, 0}, {t[1].tv_sec, 0}};
    lotto_via();
    return RIPROVA(p, smbc_utimes(u, tv) ? -errno : 0);
}

static long lkl_sys_unlink(const char *p) { char u[2200]; url(p, u, sizeof u); lotto_via(); return RIPROVA(p, smbc_unlink(u) ? -errno : 0); }
static long lkl_sys_rmdir(const char *p) { char u[2200]; url(p, u, sizeof u); lotto_via(); return RIPROVA(p, smbc_rmdir(u) ? -errno : 0); }
static long lkl_sys_mkdir(const char *p, int m) { char u[2200]; url(p, u, sizeof u); lotto_via(); return RIPROVA(p, smbc_mkdir(u, m) ? -errno : 0); }

static long lkl_sys_rename(const char *p, const char *q) {   /* sostituendo: SMB rifiuta se la destinazione c'è */
    char u[2200], v[2200];
    url(p, u, sizeof u); url(q, v, sizeof v);
    lotto_via();
    if (!RIPROVA(p, smbc_rename(u, v) ? -errno : 0)) return 0;
    if (errno != EEXIST) return -errno;
    if (smbc_unlink(v)) return -errno;
    return smbc_rename(u, v) ? -errno : 0;
}
static long lkl_sys_renameat2(int d1, const char *p, int d2, const char *q, int f) {
    (void)d1; (void)d2;
    if (f & LKL_RENAME_NOREPLACE) {
        char u[2200], v[2200]; struct lkl_stat x;
        if (!lkl_sys_stat(q, &x)) return -EEXIST;   /* (con RIPROVA: da ospite non si vedrebbe la destinazione) */
        url(p, u, sizeof u); url(q, v, sizeof v);
        lotto_via();
        return RIPROVA(p, smbc_rename(u, v) ? -errno : 0);
    }
    return lkl_sys_rename(p, q);
}
static long lkl_sys_symlink(const char *a, const char *q) { (void)a; (void)q; return -EOPNOTSUPP; }
static long lkl_sys_readlink(const char *p, char *b, unsigned long n) { (void)p; (void)b; (void)n; return -EINVAL; }
static long lkl_sys_fsync(int fd) { (void)fd; return 0; }   /* la chiusura del file su SMB scrive tutto */

static long lkl_sys_statfs(const char *p, struct lkl_statfs *sf) {
    struct statvfs v;
    char u[2200];
    memset(sf, 0, sizeof *sf);
    sf->f_namelen = 255;
    url(p, u, sizeof u);
    if (e_radice(p) || RIPROVA(p, smbc_statvfs(u, &v) ? -errno : 0)) {   /* il PC intero, o un server che non lo dice: niente spazio da mostrare */
        sf->f_bsize = sf->f_frsize = 4096;
        return 0;
    }
    sf->f_bsize = v.f_bsize; sf->f_frsize = v.f_frsize; sf->f_blocks = v.f_blocks; sf->f_bfree = v.f_bfree;
    sf->f_bavail = v.f_bavail; sf->f_files = v.f_files; sf->f_ffree = v.f_ffree;
    return 0;
}

/* il contatore delle modifiche di sftp.c (tod-fs lo chiede per sapere se rileggere le cartelle che Esplora file
 * guarda): condiviso fra i processi dei collegamenti, e mosso anche ogni 5 s dal processo principale, perché i file
 * li cambia anche chi sta sul PC */
static uint64_t *cambi_condiviso;
#define cambi (*cambi_condiviso)
#include "sftp.c"
#undef cambi

static int leggi_riga(int fd, char *b, size_t n) {
    size_t l = 0;
    while (l + 1 < n) {
        char c;
        if (read(fd, &c, 1) != 1) return -1;
        if (c == '\n') break;
        b[l++] = c;
    }
    b[l] = 0;
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 5) { fprintf(stderr, "uso: tod-smb RUN ALIAS INDIRIZZO[:PORTA] NOME\n"); return 2; }
    snprintf(run_dir, sizeof run_dir, "%s", argv[1]);
    lrad = snprintf(radice, sizeof radice, "smb://%s", argv[3]);
    snprintf(nome_pc, sizeof nome_pc, "%s", argv[4]);
    cambi_condiviso = mmap(NULL, sizeof *cambi_condiviso, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    if (cambi_condiviso == MAP_FAILED) return 1;
    char p[700];
    snprintf(p, sizeof p, "%s/lkl-%s.sock", run_dir, argv[2]);
    int ls = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    struct sockaddr_un sa = {.sun_family = AF_UNIX};
    snprintf(sa.sun_path, sizeof sa.sun_path, "%s", p);
    unlink(p);
    if (ls < 0 || bind(ls, (struct sockaddr *)&sa, sizeof sa) || listen(ls, 16)) { perror(p); return 1; }
    chmod(p, 0600);
    signal(SIGCHLD, SIG_IGN);   /* i figli finiti non restano zombie */
    signal(SIGPIPE, SIG_IGN);
    fprintf(stderr, "tod-smb: %s (%s) su %s\n", nome_pc, radice, p);
    for (;;) {
        struct pollfd pf = {ls, POLLIN, 0};
        int r = poll(&pf, 1, 5000);
        if (r == 0) { __atomic_add_fetch(cambi_condiviso, 1, __ATOMIC_RELAXED); continue; }
        if (r < 0) continue;
        int c = accept4(ls, NULL, NULL, SOCK_CLOEXEC);
        if (c < 0) continue;
        pid_t f = fork();
        if (f == 0) {
            close(ls);
            char riga[300];
            if (leggi_riga(c, riga, sizeof riga) || strncmp(riga, "VOL ", 4)) _exit(1);
            /* un contesto di Samba per il collegamento; le funzioni smbc_* qui sopra lavorano su questo */
            SMBCCTX *ctx = smbc_new_context();
            if (!ctx) _exit(1);
            smbc_setFunctionAuthData(ctx, credenziali);
            smbc_setOptionUseKerberos(ctx, 0);
            /* un PC spento o fuori rete: errore dopo 10 s, non dopo i 20 di Samba (ogni operazione di Esplora file
             * aspetterebbe tanto). Il servizio lo toglie dopo 40 s, o subito se il telefono cambia rete */
            smbc_setTimeout(ctx, 10000);
            /* TOD_SMB_DEBUG=N: il registro di Samba in smb.log (3: perché un PC rifiuta utente e password, con il codice
             * NT_STATUS_... che dà il server). 11 novembre: il Mac di Pietro rifiuta anche la password giusta */
            const char *dbg = getenv("TOD_SMB_DEBUG");
            if (dbg && *dbg) { smbc_setDebug(ctx, atoi(dbg)); smbc_setOptionDebugToStderr(ctx, 1); }
            if (!smbc_init_context(ctx)) _exit(1);
            smbc_set_context(ctx);
            sftp_sessione(c, radice, 0);
            _exit(0);
        }
        close(c);
    }
}
