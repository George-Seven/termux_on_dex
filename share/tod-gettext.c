/*
 * tod-gettext.c - traduzioni per i programmi nativi di Termux (Termux On DeX)
 *
 * La libc di Android (Bionic) non supporta le localizzazioni: setlocale() rifiuta
 * "it_IT.UTF-8" e gettext non traduce. Questa libreria, caricata con LD_PRELOAD,
 * intercetta le funzioni di traduzione (gettext e famiglia, e quelle di GLib usate
 * da GTK e XFCE) e legge direttamente i cataloghi .mo.
 *
 *   TOD_GETTEXT_LANG   lingua, es. "it"            (senza: nessuna traduzione)
 *   TOD_GETTEXT_DIR    cartella dei cataloghi       <dir>/<lingua>/LC_MESSAGES/<dominio>.mo
 *
 * Idea ripresa dal lavoro di hansm629 (termux-desktop, issue #214), riscritta da zero.
 * Compilazione: clang -shared -fPIC -O2 -o tod-gettext.so tod-gettext.c -ldl
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <dlfcn.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>

/* ---------------------------------------------------------------- catalogo .mo */
typedef struct Entry { const char *key; const char *val; uint32_t vlen; struct Entry *next; } Entry;
typedef struct Catalog { Entry **buckets; uint32_t nbuckets; char *blob; int plural_gt1; int nplurals; } Catalog;
typedef struct Domain { char *name; Catalog *cat; struct Domain *next; } Domain;

static Domain *g_domains = NULL;
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static char g_default_domain[128] = "messages";
static int g_domain_set = 0;          /* textdomain() chiamata dal programma? */
static char g_prog_domain[128] = "";

/* Il dominio predefinito. In Termux libxfce4util è compilata senza NLS, quindi
 * xfce_textdomain() non chiama textdomain() e il dominio resta "messages": le voci
 * dei menu tradotte da libxfce4ui con g_dgettext(NULL, ...) non si trovavano. Se il
 * programma non l'ha impostato, il dominio è il nome del programma (thunar,
 * mousepad, xfce4-terminal...), che è anche il nome del suo catalogo. */
static const char *default_domain(void) {
    if (g_domain_set) return g_default_domain;
    if (!g_prog_domain[0]) {
#if defined(__BIONIC__) || defined(__ANDROID__)
        const char *n = getprogname();
#else
        extern char *program_invocation_short_name;
        const char *n = program_invocation_short_name;
#endif
        size_t i = 0;
        for (; n && n[i] && i < sizeof(g_prog_domain) - 1; i++)
            g_prog_domain[i] = (n[i] >= 'A' && n[i] <= 'Z') ? (char)(n[i] + 32) : n[i];
        g_prog_domain[i] = '\0';
        if (!g_prog_domain[0]) snprintf(g_prog_domain, sizeof(g_prog_domain), "messages");
    }
    return g_prog_domain;
}

static uint32_t hash_str(const char *s) {
    uint32_t h = 2166136261u;
    while (*s) { h ^= (unsigned char)*s++; h *= 16777619u; }
    return h;
}

static uint32_t rd32(const char *p, int swap) {
    uint32_t v;
    memcpy(&v, p, 4);
    if (swap) v = (v >> 24) | ((v >> 8) & 0xFF00) | ((v << 8) & 0xFF0000) | (v << 24);
    return v;
}

static void parse_plural_header(Catalog *c, const char *header) {
    /* basta riconoscere le regole comuni: "n != 1" (it, en, de, es...), "n > 1" (fr, pt-BR), nplurals=1 */
    c->nplurals = 2;
    c->plural_gt1 = 0;
    const char *p = header ? strstr(header, "Plural-Forms:") : NULL;
    if (!p) return;
    const char *np = strstr(p, "nplurals=");
    if (np) {
        int v = atoi(np + 9);
        if (v >= 1 && v <= 6) c->nplurals = v;
    }
    const char *pl = strstr(p, "plural=");
    if (!pl) return;
    char expr[64];
    size_t k = 0;
    for (pl += 7; *pl && *pl != ';' && *pl != '\n' && k < sizeof(expr) - 1; pl++)
        if (*pl != ' ' && *pl != '(' && *pl != ')') expr[k++] = *pl;
    expr[k] = '\0';
    if (strcmp(expr, "n>1") == 0) c->plural_gt1 = 1;
}

static Catalog *load_mo(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long size = ftell(f);
    if (size < 28 || size > 64L * 1024 * 1024) { fclose(f); return NULL; }
    rewind(f);
    char *b = malloc((size_t)size + 1);
    if (!b) { fclose(f); return NULL; }
    if (fread(b, 1, (size_t)size, f) != (size_t)size) { fclose(f); free(b); return NULL; }
    fclose(f);
    b[size] = '\0';

    uint32_t magic = rd32(b, 0);
    int swap;
    if (magic == 0x950412de) swap = 0;
    else if (magic == 0xde120495) swap = 1;
    else { free(b); return NULL; }

    uint32_t n = rd32(b + 8, swap), oo = rd32(b + 12, swap), ot = rd32(b + 16, swap);
    if (n > 1000000 || (uint64_t)oo + 8ull * n > (uint64_t)size || (uint64_t)ot + 8ull * n > (uint64_t)size) {
        free(b);
        return NULL;
    }
    Catalog *c = calloc(1, sizeof(Catalog));
    if (!c) { free(b); return NULL; }
    c->nbuckets = n * 2 + 1;
    c->buckets = calloc(c->nbuckets, sizeof(Entry *));
    c->blob = b;
    c->nplurals = 2;
    if (!c->buckets) { free(c); free(b); return NULL; }

    for (uint32_t i = 0; i < n; i++) {
        uint32_t olen = rd32(b + oo + i * 8, swap), ooff = rd32(b + oo + i * 8 + 4, swap);
        uint32_t tlen = rd32(b + ot + i * 8, swap), toff = rd32(b + ot + i * 8 + 4, swap);
        if ((uint64_t)ooff + olen >= (uint64_t)size || (uint64_t)toff + tlen >= (uint64_t)size) continue;
        const char *key = b + ooff, *val = b + toff;
        if (olen == 0) { parse_plural_header(c, val); continue; }   /* intestazione */
        if (tlen == 0) continue;                                    /* non tradotto */
        Entry *e = malloc(sizeof(Entry));
        if (!e) continue;
        e->key = key;   /* per i plurali la chiave è il singolare (fino al primo \0) */
        e->val = val;
        e->vlen = tlen;
        uint32_t h = hash_str(key) % c->nbuckets;
        e->next = c->buckets[h];
        c->buckets[h] = e;
    }
    return c;
}

static const Entry *cat_find(const Catalog *c, const char *key) {
    if (!c || !key) return NULL;
    for (const Entry *e = c->buckets[hash_str(key) % c->nbuckets]; e; e = e->next)
        if (strcmp(e->key, key) == 0) return e;
    return NULL;
}

static Catalog *domain_catalog(const char *domain) {
    const char *lang = getenv("TOD_GETTEXT_LANG");
    if (!lang || !*lang || !domain || !*domain) return NULL;
    pthread_mutex_lock(&g_lock);
    for (Domain *d = g_domains; d; d = d->next)
        if (strcmp(d->name, domain) == 0) { pthread_mutex_unlock(&g_lock); return d->cat; }
    const char *dir = getenv("TOD_GETTEXT_DIR");
    if (!dir || !*dir) dir = "/data/data/com.termux/files/usr/share/tod-locale";
    char path[1024];
    snprintf(path, sizeof(path), "%s/%s/LC_MESSAGES/%s.mo", dir, lang, domain);
    Catalog *c = load_mo(path);
    Domain *nd = calloc(1, sizeof(Domain));   /* anche i domini senza catalogo restano in cache */
    if (nd) {
        nd->name = strdup(domain);
        nd->cat = c;
        nd->next = g_domains;
        g_domains = nd;
    }
    pthread_mutex_unlock(&g_lock);
    return c;
}

/* Voci di menu di Thunar e della scrivania che aprivano la finestra del nome: ora si scrive sul nome (come macOS,
 * tod_create_inline e tod_inline_rename), quindi niente puntini; in italiano "Nuova cartella" come macOS (Thunar
 * diceva "Creare cartella…"). Con TOD_RINOMINA_CLASSICA / TOD_NUOVO_CLASSICO restano quelle di prima. */
static const char *tod_voce_menu(const char *dom, const char *msgid, const char *t) {
    static char buf[4][160];
    if (!dom || (strcasecmp(dom, "thunar") != 0 && strcasecmp(dom, "xfdesktop") != 0)) return t;
    int folder = !strcmp(msgid, "Create _Folder..."), ren = !strcmp(msgid, "_Rename...");
    if ((!folder && !ren) || getenv("TOD_RINOMINA_CLASSICA") || (folder && getenv("TOD_NUOVO_CLASSICO"))) return t;
    const char *lang = getenv("TOD_GETTEXT_LANG");
    if (folder && lang && !strcmp(lang, "it")) return "Nuova carte_lla";
    char *b = buf[(strcasecmp(dom, "thunar") == 0) * 2 + ren];
    if (!*b) {
        snprintf(b, sizeof buf[0], "%s", t ? t : msgid);
        size_t n = strlen(b);
        if (n >= 3 && !strcmp(b + n - 3, "...")) b[n - 3] = '\0';
        else if (n >= 3 && !strcmp(b + n - 3, "\xe2\x80\xa6")) b[n - 3] = '\0';   /* … */
    }
    return b;
}

static const char *translate(const char *domain, const char *msgid) {
    if (!msgid || !*msgid) return NULL;
    const char *dom = domain ? domain : default_domain();
    const Entry *e = cat_find(domain_catalog(dom), msgid);
    return tod_voce_menu(dom, msgid, e ? e->val : NULL);
}

static const char *translate_plural(const char *domain, const char *msgid, unsigned long n) {
    if (!msgid || !*msgid) return NULL;
    Catalog *c = domain_catalog(domain ? domain : default_domain());
    const Entry *e = cat_find(c, msgid);
    if (!e) return NULL;
    unsigned idx = c->nplurals <= 1 ? 0 : (c->plural_gt1 ? (n > 1) : (n != 1));
    const char *p = e->val, *end = e->val + e->vlen;
    for (unsigned i = 0; i < idx; i++) {
        const char *z = memchr(p, '\0', (size_t)(end - p));
        if (!z || z + 1 >= end) break;   /* meno forme del previsto: resta sull'ultima */
        p = z + 1;
    }
    return p;
}

static const char *translate_ctx(const char *domain, const char *ctx, const char *msgid) {
    if (!ctx || !msgid) return NULL;
    size_t a = strlen(ctx), b = strlen(msgid);
    char stackbuf[512], *key = (a + b + 2 <= sizeof(stackbuf)) ? stackbuf : malloc(a + b + 2);
    if (!key) return NULL;
    memcpy(key, ctx, a);
    key[a] = '\004';
    memcpy(key + a + 1, msgid, b + 1);
    const char *t = translate(domain, key);
    if (key != stackbuf) free(key);
    return t;
}

/* -------------------------------------------------- funzioni originali (RTLD_NEXT) */
/* RTLD_NEXT basta nei programmi collegati a GTK. In quelli che caricano GTK dopo (Python con gi)
 * su Android dà NULL: allora si cerca nelle librerie già caricate, saltando questa. Senza, la
 * funzione originale non girava mai (es. g_object_add_weak_pointer: puntatori deboli mai
 * registrati, poi memoria liberata usata → la barra e lo Start in crash). */
static const char *const TOD_LIBRERIE[] = {"libgobject-2.0.so.0", "libglib-2.0.so.0", "libgio-2.0.so.0",
                                           "libgtk-3.so.0", "libX11.so", "libXext.so", "libintl.so", "libc.so", NULL};

static void *tod_next(const char *name) {
    void *f = dlsym(RTLD_NEXT, name);
    if (f) return f;
    Dl_info mia, sua;
    if (!dladdr((void *)tod_next, &mia)) return NULL;
    for (int i = 0; TOD_LIBRERIE[i]; i++) {
        void *h = dlopen(TOD_LIBRERIE[i], RTLD_NOW | RTLD_NOLOAD);
        if (!h) continue;
        f = dlsym(h, name);
        dlclose(h);
        if (f && dladdr(f, &sua) && sua.dli_fbase != mia.dli_fbase) return f;
    }
    return NULL;
}

#define REAL(ret, name, ...) \
    static ret (*real_##name)(__VA_ARGS__) = NULL; \
    static ret (*get_##name(void))(__VA_ARGS__) { \
        if (!real_##name) real_##name = (ret (*)(__VA_ARGS__))tod_next(#name); \
        return real_##name; }

REAL(char *, gettext, const char *)
REAL(char *, dgettext, const char *, const char *)
REAL(char *, dcgettext, const char *, const char *, int)
REAL(char *, ngettext, const char *, const char *, unsigned long)
REAL(char *, dngettext, const char *, const char *, const char *, unsigned long)
REAL(char *, dcngettext, const char *, const char *, const char *, unsigned long, int)
REAL(char *, textdomain, const char *)
REAL(const char *, g_dgettext, const char *, const char *)
REAL(const char *, g_dcgettext, const char *, const char *, int)
REAL(const char *, g_dngettext, const char *, const char *, const char *, unsigned long)
REAL(const char *, g_dpgettext, const char *, const char *, size_t)
REAL(const char *, g_dpgettext2, const char *, const char *, const char *)

/* ------------------------------------------------------------ funzioni intercettate */
char *textdomain(const char *domainname) {
    if (domainname && *domainname) {
        pthread_mutex_lock(&g_lock);
        snprintf(g_default_domain, sizeof(g_default_domain), "%s", domainname);
        g_domain_set = 1;
        pthread_mutex_unlock(&g_lock);
    }
    if (get_textdomain()) return real_textdomain(domainname);
    return g_default_domain;
}

char *gettext(const char *msgid) {
    const char *t = translate(NULL, msgid);
    if (t) return (char *)t;
    return get_gettext() ? real_gettext(msgid) : (char *)msgid;
}

char *dgettext(const char *domain, const char *msgid) {
    const char *t = translate(domain, msgid);
    if (t) return (char *)t;
    return get_dgettext() ? real_dgettext(domain, msgid) : (char *)msgid;
}

char *dcgettext(const char *domain, const char *msgid, int category) {
    const char *t = translate(domain, msgid);
    if (t) return (char *)t;
    return get_dcgettext() ? real_dcgettext(domain, msgid, category) : (char *)msgid;
}

char *ngettext(const char *msgid, const char *plural, unsigned long n) {
    const char *t = translate_plural(NULL, msgid, n);
    if (t) return (char *)t;
    return get_ngettext() ? real_ngettext(msgid, plural, n) : (char *)(n == 1 ? msgid : plural);
}

char *dngettext(const char *domain, const char *msgid, const char *plural, unsigned long n) {
    const char *t = translate_plural(domain, msgid, n);
    if (t) return (char *)t;
    return get_dngettext() ? real_dngettext(domain, msgid, plural, n) : (char *)(n == 1 ? msgid : plural);
}

char *dcngettext(const char *domain, const char *msgid, const char *plural, unsigned long n, int category) {
    const char *t = translate_plural(domain, msgid, n);
    if (t) return (char *)t;
    return get_dcngettext() ? real_dcngettext(domain, msgid, plural, n, category) : (char *)(n == 1 ? msgid : plural);
}

/* GLib: g_dgettext e compagni non chiamano gettext se pensano che la lingua sia
 * l'inglese (e con Bionic lo pensano sempre), quindi vanno intercettati a parte. */
const char *g_dgettext(const char *domain, const char *msgid) {
    const char *t = translate(domain, msgid);
    if (t) return t;
    return get_g_dgettext() ? real_g_dgettext(domain, msgid) : msgid;
}

const char *g_dcgettext(const char *domain, const char *msgid, int category) {
    const char *t = translate(domain, msgid);
    if (t) return t;
    return get_g_dcgettext() ? real_g_dcgettext(domain, msgid, category) : msgid;
}

const char *g_dngettext(const char *domain, const char *msgid, const char *plural, unsigned long n) {
    const char *t = translate_plural(domain, msgid, n);
    if (t) return t;
    return get_g_dngettext() ? real_g_dngettext(domain, msgid, plural, n) : (n == 1 ? msgid : plural);
}

/* "contesto|testo" oppure "contesto\004testo", msgidoffset = inizio del testo */
const char *g_dpgettext(const char *domain, const char *msgctxtid, size_t msgidoffset) {
    if (msgctxtid) {
        size_t off = msgidoffset;
        if (off == 0) {
            const char *sep = strchr(msgctxtid, '\004');
            if (!sep) sep = strchr(msgctxtid, '|');
            if (sep) off = (size_t)(sep - msgctxtid) + 1;
        }
        if (off > 0 && off <= strlen(msgctxtid)) {
            char ctx[256];
            size_t clen = off - 1 < sizeof(ctx) - 1 ? off - 1 : sizeof(ctx) - 1;
            memcpy(ctx, msgctxtid, clen);
            ctx[clen] = '\0';
            const char *t = translate_ctx(domain, ctx, msgctxtid + off);
            if (t) return t;
        }
    }
    return get_g_dpgettext() ? real_g_dpgettext(domain, msgctxtid, msgidoffset)
                             : (msgctxtid ? msgctxtid + msgidoffset : msgctxtid);
}

const char *g_dpgettext2(const char *domain, const char *context, const char *msgid) {
    const char *t = translate_ctx(domain, context, msgid);
    if (t) return t;
    return get_g_dpgettext2() ? real_g_dpgettext2(domain, context, msgid) : msgid;
}

/* ------------------------------------------------------------ nome utente
 * Su Android l'utente si chiama u0_a715 e la sua home è quella di Termux. Nella
 * sessione grafica TOD_USER, TOD_USER_HOME e TOD_USER_FULLNAME danno un utente vero
 * (es. "pietro", .../files/pietro, "Pietro"): lo vedono terminale, menu e whoami. */
#include <errno.h>
#include <pwd.h>
#include <unistd.h>
/* il pwd.h di Termux ridefinisce queste funzioni come "polyfill" statici: qui servono quelle vere */
#undef getpwnam
#undef getpwuid
#undef getpwuid_r

REAL(struct passwd *, getpwuid, uid_t)
REAL(struct passwd *, getpwnam, const char *)
REAL(int, getpwuid_r, uid_t, struct passwd *, char *, size_t, struct passwd **)
REAL(int, getpwnam_r, const char *, struct passwd *, char *, size_t, struct passwd **)

static const char *tod_user(void) {
    const char *u = getenv("TOD_USER");
    return (u && *u) ? u : NULL;
}

static const char *env_or(const char *name, const char *dflt) {
    const char *v = getenv(name);
    return (v && *v) ? v : dflt;
}

/* copia l'utente vero (shell, uid...) cambiando nome, home e nome completo */
static int fill_user(const struct passwd *src, struct passwd *dst, char *buf, size_t len) {
    const char *u = tod_user();
    const char *strs[4] = { u, env_or("TOD_USER_FULLNAME", u), env_or("TOD_USER_HOME", src ? src->pw_dir : "/"),
                            src && src->pw_shell ? src->pw_shell : "/system/bin/sh" };
    char *out[4];
    size_t used = 0;
    for (int i = 0; i < 4; i++) {
        size_t n = strlen(strs[i]) + 1;
        if (used + n > len) return ERANGE;
        memcpy(buf + used, strs[i], n);
        out[i] = buf + used;
        used += n;
    }
    if (src) *dst = *src; else memset(dst, 0, sizeof(*dst));
    dst->pw_uid = getuid();
    dst->pw_gid = getgid();
    dst->pw_name = out[0];
    dst->pw_gecos = out[1];
    dst->pw_dir = out[2];
    dst->pw_shell = out[3];
    if (!src) dst->pw_passwd = out[0] + strlen(out[0]);   /* stringa vuota */
    return 0;
}

static int is_us(uid_t uid) { return tod_user() && uid == getuid(); }
static int is_our_name(const char *n) {
    if (!tod_user() || !n) return 0;
    if (strcmp(n, tod_user()) == 0) return 1;
    struct passwd *r = get_getpwuid() ? real_getpwuid(getuid()) : NULL;   /* "u0_a715" */
    return r && r->pw_name && strcmp(n, r->pw_name) == 0;
}

static struct passwd g_pw;
static char g_pwbuf[1024];

struct passwd *getpwuid(uid_t uid) {
    struct passwd *r = get_getpwuid() ? real_getpwuid(uid) : NULL;
    if (!is_us(uid)) return r;
    return fill_user(r, &g_pw, g_pwbuf, sizeof(g_pwbuf)) == 0 ? &g_pw : r;
}

struct passwd *getpwnam(const char *name) {
    if (is_our_name(name)) return getpwuid(getuid());
    return get_getpwnam() ? real_getpwnam(name) : NULL;
}

int getpwuid_r(uid_t uid, struct passwd *pwd, char *buf, size_t len, struct passwd **result) {
    int rc = get_getpwuid_r() ? real_getpwuid_r(uid, pwd, buf, len, result) : ENOENT;
    if (!is_us(uid)) return rc;
    struct passwd *r = get_getpwuid() ? real_getpwuid(uid) : NULL;
    struct passwd copy;
    if (r) copy = *r;
    rc = fill_user(r ? &copy : NULL, pwd, buf, len);
    *result = rc == 0 ? pwd : NULL;
    return rc;
}

int getpwnam_r(const char *name, struct passwd *pwd, char *buf, size_t len, struct passwd **result) {
    if (is_our_name(name)) return getpwuid_r(getuid(), pwd, buf, len, result);
    return get_getpwnam_r() ? real_getpwnam_r(name, pwd, buf, len, result) : ENOENT;
}

char *getlogin(void) {
    const char *u = tod_user();
    return (char *)(u ? u : "");
}

/* ------------------------------------------------------------ cartelle di sistema
 * Sopra la home (/data/data, /data, /storage/emulated...) Android nega la lettura: il
 * file manager mostrava "Permesso negato". Con TOD_FS_MAP, una cartella che non si
 * può leggere viene elencata da TOD_FS_MAP/<percorso>, che contiene solo la strada
 * verso le cartelle dell'utente (es. /data/data → com.termux). Il resto (stat,
 * apertura dei file) resta quello vero. */
#include <dirent.h>
#include <fcntl.h>
#include <limits.h>
#include <sys/stat.h>

REAL(DIR *, opendir, const char *)
REAL(int, access, const char *, int)
REAL(int, faccessat, int, const char *, int, int)

/* percorso assoluto e pulito ("." "..", "//", "/" in fondo). Nel Terminale "cd /storage" e poi "ls" apre ".":
 * "Permesso negato" (9 novembre), mentre "ls /storage" ed Esplora file, che usano percorsi interi, andavano */
static int fs_absolute(const char *path, char *out, size_t len) {
    char tmp[PATH_MAX], cwd[PATH_MAX];
    if (path[0] == '/') {
        if (strlen(path) >= sizeof(tmp)) return 0;
        strcpy(tmp, path);
    } else {
        if (!getcwd(cwd, sizeof(cwd))) return 0;
        if ((size_t)snprintf(tmp, sizeof(tmp), "%s/%s", cwd, path) >= sizeof(tmp)) return 0;
    }
    size_t o = 0;
    char *save = NULL;
    out[0] = '\0';
    for (char *c = strtok_r(tmp, "/", &save); c; c = strtok_r(NULL, "/", &save)) {
        if (!strcmp(c, ".")) continue;
        if (!strcmp(c, "..")) {   /* su di uno */
            while (o > 0 && out[o - 1] != '/') o--;
            if (o > 0) o--;
            out[o] = '\0';
            continue;
        }
        size_t n = strlen(c);
        if (o + n + 2 > len) return 0;
        out[o++] = '/';
        memcpy(out + o, c, n);
        o += n;
        out[o] = '\0';
    }
    if (o == 0) {
        if (len < 2) return 0;
        strcpy(out, "/");
    }
    return 1;
}

static int fs_mapped(const char *path, char *out, size_t len) {
    const char *m = getenv("TOD_FS_MAP");
    char abs[PATH_MAX];
    if (!m || !*m || !path || !*path || !fs_absolute(path, abs, sizeof(abs))) return 0;
    path = abs;
    size_t n = strlen(path);
    if (n == 1) n = 0;                          /* "/" → la radice della mappa */
    if ((size_t)snprintf(out, len, "%s%.*s", m, (int)n, path) >= len) return 0;
    struct stat st;
    return stat(out, &st) == 0 && S_ISDIR(st.st_mode);
}

DIR *opendir(const char *name) {
    if (!get_opendir()) { errno = ENOSYS; return NULL; }
    DIR *d = real_opendir(name);
    if (d || (errno != EACCES && errno != EPERM && errno != ENOENT)) return d;
    int e = errno;
    char p[PATH_MAX];
    struct stat st;
    /* /storage/emulated: stat va, ma elencarla dà "non esiste" (non "permesso negato"): da Esplora file
     * "emulated" non si apriva. ENOENT vale solo per una cartella che esiste davvero */
    if (e == ENOENT && (stat(name, &st) != 0 || !S_ISDIR(st.st_mode))) { errno = e; return NULL; }
    if (fs_mapped(name, p, sizeof(p)) && (d = real_opendir(p))) return d;
    errno = e;
    return NULL;
}

static int fs_access_fix(int rc, const char *path) {
    char p[PATH_MAX];
    if (rc == 0 || (errno != EACCES && errno != EPERM)) return rc;
    int e = errno;
    if (fs_mapped(path, p, sizeof(p))) return 0;
    errno = e;
    return rc;
}

int access(const char *path, int mode) {
    if (!get_access()) { errno = ENOSYS; return -1; }
    return fs_access_fix(real_access(path, mode), path);
}

int faccessat(int dirfd, const char *path, int mode, int flags) {
    if (!get_faccessat()) { errno = ENOSYS; return -1; }
    int rc = real_faccessat(dirfd, path, mode, flags);
    const char *p = path;   /* glibc dichiara path nonnull: il controllo sul parametro sarebbe un errore */
    return (p && (p[0] == '/' || dirfd == AT_FDCWD)) ? fs_access_fix(rc, path) : rc;   /* relativo: dalla cartella corrente */
}

/* ------------------------------------------------- rinomina "in linea" (Thunar e scrivania)
 * Thunar (e la scrivania, che per rinominare chiama Thunar via D-Bus) apre un pannello con
 * titolo, icona, "Inserisci il nuovo nome:" e due pulsanti. Pietro: "è bruttissimo, non si può
 * fare come su Windows o su Mac?". Qui, solo in Thunar e solo per quella finestra (riconosciuta
 * dalla forma: un'immagine, un testo e una casella), la si trasforma prima che compaia: niente
 * cornice, niente testo né pulsanti, solo la casella col nome, dove si trova il puntatore.
 * Invio o un clic altrove rinominano (come Windows e macOS), Esc annulla.
 * Niente intestazioni di GTK: le funzioni si prendono con dlsym, i tipi sono puntatori.
 * TOD_RINOMINA_CLASSICA=1 rimette il pannello di Thunar. */
typedef unsigned long tod_gtype;
typedef struct TodList { void *data; struct TodList *next, *prev; } TodList;
typedef struct { tod_gtype type; const char *type_name; unsigned class_size, instance_size; } TodTypeQuery;

#define GTKFN(ret, name, ...) \
    static ret (*f_##name)(__VA_ARGS__); \
    if (!f_##name) f_##name = (ret (*)(__VA_ARGS__))dlsym(RTLD_DEFAULT, #name); \
    if (!f_##name) return 0;

static int tod_ok_response = -5;

static const char *tod_type_name(void *obj) {
    GTKFN(const char *, g_type_name, tod_gtype)
    if (!obj) return "";
    return f_g_type_name(**(tod_gtype **)obj);
}

static int tod_is_container(void *w) {
    GTKFN(tod_gtype, gtk_container_get_type, void)
    GTKFN(int, g_type_check_instance_is_a, void *, tod_gtype)
    return f_g_type_check_instance_is_a(w, f_gtk_container_get_type());
}

/* conta (e se hide, nasconde) immagini ed etichette; restituisce la casella di testo */
static void *tod_scan(void *w, int *images, int *labels, int *entries, int hide) {
    GTKFN(TodList *, gtk_container_get_children, void *)
    GTKFN(void, g_list_free, TodList *)
    GTKFN(void, gtk_widget_hide, void *)
    GTKFN(void, gtk_widget_set_no_show_all, void *, int)
    void *entry = NULL;
    const char *t = tod_type_name(w);
    if (!strcmp(t, "GtkEntry")) { (*entries)++; return w; }
    if (!strcmp(t, "GtkImage") || !strcmp(t, "GtkLabel")) {
        if (!strcmp(t, "GtkImage")) (*images)++; else (*labels)++;
        if (hide) { f_gtk_widget_hide(w); f_gtk_widget_set_no_show_all(w, 1); }
        return NULL;
    }
    if (!tod_is_container(w)) return NULL;
    TodList *kids = f_gtk_container_get_children(w);
    for (TodList *l = kids; l; l = l->next) {
        void *e = tod_scan(l->data, images, labels, entries, hide);
        if (e && !entry) entry = e;
    }
    f_g_list_free(kids);
    return entry;
}

/* il primo widget del tipo dato (nome esatto) dentro w, w compreso */
static void *tod_find_type(void *w, const char *type) {
    GTKFN(TodList *, gtk_container_get_children, void *)
    GTKFN(void, g_list_free, TodList *)
    if (!w) return NULL;
    if (!strcmp(tod_type_name(w), type)) return w;
    if (!tod_is_container(w)) return NULL;
    void *found = NULL;
    TodList *kids = f_gtk_container_get_children(w);
    for (TodList *l = kids; l && !found; l = l->next) found = tod_find_type(l->data, type);
    f_g_list_free(kids);
    return found;
}

/* --- aspetto del nome in modifica, come macOS (8 novembre) ---
 * Campo bianco (scuro col tema scuro) con angoli tondi e l'anello nel colore d'accento del sistema, testo scelto
 * evidenziato nell'accento chiaro. Vale per la casella di Thunar (classe tod-rinomina) e per il campo della
 * scrivania (finestra senza fondo + riquadro + testo). Priorità sopra gtk.css dell'utente. */
static int tod_rename_css(void *screen) {
    GTKFN(void *, gtk_css_provider_new, void)
    GTKFN(int, gtk_css_provider_load_from_data, void *, const char *, long, void *)
    GTKFN(void, gtk_style_context_add_provider_for_screen, void *, void *, unsigned)
    static void *done_for;
    if (!screen || done_for == screen) return 0;
    static const char css[] =
        "window.tod-rinomina-finestra, window.tod-rinomina-finestra.background {"
        "  background-color: transparent; background-image: none; border: none; box-shadow: none; }\n"
        ".tod-rinomina-campo {"
        "  background-color: @theme_base_color; border-radius: 5px; margin: 4px; padding: 1px 4px;"
        "  border: 1px solid alpha(@theme_selected_bg_color, 0.85);"
        "  box-shadow: 0 0 0 3px alpha(@theme_selected_bg_color, 0.35), 0 1px 3px rgba(0, 0, 0, 0.25); }\n"
        "textview.tod-rinomina-testo, textview.tod-rinomina-testo text {"
        "  background-color: transparent; color: @theme_text_color; }\n"
        "textview.tod-rinomina-testo text selection, textview.tod-rinomina-testo text selection:focus {"
        "  background-color: alpha(@theme_selected_bg_color, 0.35); color: @theme_text_color; }\n"
        "entry.tod-rinomina, iconview entry.tod-rinomina, treeview entry.tod-rinomina {"
        "  min-height: 0; margin: 0; padding: 1px 4px; border-radius: 5px; background-image: none;"
        "  background-color: @theme_base_color; color: @theme_text_color;"
        "  border: 1px solid alpha(@theme_selected_bg_color, 0.85);"
        "  box-shadow: 0 0 0 3px alpha(@theme_selected_bg_color, 0.35); }\n"
        "entry.tod-rinomina selection, entry.tod-rinomina selection:focus {"
        "  background-color: alpha(@theme_selected_bg_color, 0.35); color: @theme_text_color; }\n";
    void *p = f_gtk_css_provider_new();
    f_gtk_css_provider_load_from_data(p, css, -1, NULL);
    f_gtk_style_context_add_provider_for_screen(screen, p, 900 /* sopra GTK_STYLE_PROVIDER_PRIORITY_USER */);
    done_for = screen;
    return 1;
}

/* --- l'etichetta vera sparisce mentre si scrive il nome (8 novembre) ---
 * Pietro: "hai fatto un'etichetta sopra il reale". Sotto il campo di modifica restava disegnato il nome vecchio
 * (sulla scrivania tutto, in Thunar le righe che il campo a una riga non copre). Qui si sostituisce, nella classe
 * del disegnatore di quel testo (GtkCellRendererClass->render: struttura fissa di GTK 3, si controlla la misura),
 * il disegno con uno che salta solo l'etichetta con quel testo in quella vista; tutto il resto passa all'originale. */
typedef void (*TodRenderFn)(void *cell, void *cr, void *widget, const void *bg, const void *area, int flags);
static struct { void *klass; TodRenderFn orig; } tod_render_saved[6];
static int tod_render_n;
static void *tod_hide_widget;
static char *tod_hide_text;

static TodRenderFn tod_render_orig(void *klass) {
    GTKFN(void *, g_type_class_peek_parent, void *)
    for (void *k = klass; k; k = f_g_type_class_peek_parent(k))
        for (int i = 0; i < tod_render_n; i++)
            if (tod_render_saved[i].klass == k) return tod_render_saved[i].orig;
    return NULL;
}

static void tod_render_hook(void *cell, void *cr, void *widget, const void *bg, const void *area, int flags) {
    static void (*get)(void *, const char *, ...);
    static void (*gfree)(void *);
    if (!get) get = (void (*)(void *, const char *, ...))dlsym(RTLD_DEFAULT, "g_object_get");
    if (!gfree) gfree = (void (*)(void *))dlsym(RTLD_DEFAULT, "g_free");
    if (tod_hide_text && widget == tod_hide_widget && get && gfree) {
        char *t = NULL;
        get(cell, "text", &t, NULL);
        int same = t && !strcmp(t, tod_hide_text);
        gfree(t);
        if (same) return;
    }
    TodRenderFn orig = tod_render_orig(*(void **)cell);   /* la classe è il primo campo dell'istanza */
    if (orig) orig(cell, cr, widget, bg, area, flags);
}

/* klass: classe del disegnatore delle etichette; text NULL = si torna a disegnare tutto */
static int tod_hide_label(void *klass, void *widget, const char *text) {
    GTKFN(tod_gtype, gtk_cell_renderer_get_type, void)
    GTKFN(tod_gtype, g_object_get_type, void)
    GTKFN(void, g_type_query, tod_gtype, TodTypeQuery *)
    GTKFN(void, gtk_widget_queue_draw, void *)
    if (text && klass) {
        TodTypeQuery obj = {0}, cr = {0};
        f_g_type_query(f_g_object_get_type(), &obj);
        f_g_type_query(f_gtk_cell_renderer_get_type(), &cr);
        if (!obj.class_size || cr.class_size != obj.class_size + 16 * sizeof(void *)) {
            fprintf(stderr, "tod-rinomina: GtkCellRendererClass inattesa (%u), l'etichetta resta\n", cr.class_size);
            return 0;
        }
        TodRenderFn *slot = (TodRenderFn *)((char *)klass + obj.class_size + 7 * sizeof(void *));   /* ->render */
        if (*slot != tod_render_hook) {
            if (tod_render_n >= (int)(sizeof tod_render_saved / sizeof tod_render_saved[0]) || !*slot) return 0;
            tod_render_saved[tod_render_n].klass = klass;
            tod_render_saved[tod_render_n].orig = *slot;
            tod_render_n++;
            *slot = tod_render_hook;
        }
    }
    free(tod_hide_text);
    tod_hide_text = text ? strdup(text) : NULL;
    tod_hide_widget = text ? widget : NULL;
    if (widget) f_gtk_widget_queue_draw(widget);
    return 1;
}

static int tod_commit(void *dialog) {
    GTKFN(void, gtk_dialog_response, void *, int)
    GTKFN(int, gtk_widget_get_visible, void *)
    if (f_gtk_widget_get_visible(dialog)) f_gtk_dialog_response(dialog, tod_ok_response);
    return 0;
}

static int tod_on_focus_out(void *dialog, void *event, void *data) {
    (void)event; (void)data;
    return tod_commit(dialog);
}

static void tod_on_activate(void *entry, void *dialog) { (void)entry; tod_commit(dialog); }

static int tod_compact_rename(void *dialog) {
    GTKFN(const char *, g_get_prgname, void)
    GTKFN(void *, gtk_dialog_get_content_area, void *)
    GTKFN(void *, gtk_dialog_get_action_area, void *)
    GTKFN(TodList *, gtk_container_get_children, void *)
    GTKFN(void, g_list_free, TodList *)
    GTKFN(int, gtk_dialog_get_response_for_widget, void *, void *)
    GTKFN(void, gtk_widget_hide, void *)
    GTKFN(void, gtk_widget_set_no_show_all, void *, int)
    GTKFN(void, gtk_window_set_decorated, void *, int)
    GTKFN(void, gtk_window_set_position, void *, int)
    GTKFN(void, gtk_container_set_border_width, void *, unsigned)
    GTKFN(void, gtk_entry_set_width_chars, void *, int)
    GTKFN(void *, gtk_widget_get_style_context, void *)
    GTKFN(void, gtk_style_context_add_class, void *, const char *)
    GTKFN(unsigned long, g_signal_connect_data, void *, const char *, void *, void *, void *, int)
    const char *prg = f_g_get_prgname();
    if (!prg || strcasecmp(prg, "thunar") != 0 || getenv("TOD_RINOMINA_CLASSICA")) return 0;
    void *content = f_gtk_dialog_get_content_area(dialog);
    int images = 0, labels = 0, entries = 0;
    void *entry = tod_scan(content, &images, &labels, &entries, 0);
    if (!entry || entries != 1 || images < 1 || labels < 1) return 0;   /* non è la finestra di rinomina */
    tod_scan(content, &images, &labels, &entries, 1);
    void *actions = f_gtk_dialog_get_action_area(dialog);
    if (actions) {
        TodList *btns = f_gtk_container_get_children(actions);
        for (TodList *l = btns; l; l = l->next) {
            int r = f_gtk_dialog_get_response_for_widget(dialog, l->data);
            if (r != -6 && r != -4 && r != -1) tod_ok_response = r;   /* non Annulla/chiusura */
        }
        f_g_list_free(btns);
        f_gtk_widget_hide(actions);
        f_gtk_widget_set_no_show_all(actions, 1);
    }
    f_gtk_window_set_decorated(dialog, 0);
    f_gtk_window_set_position(dialog, 2 /* GTK_WIN_POS_MOUSE */);
    f_gtk_container_set_border_width(dialog, 0);
    f_gtk_entry_set_width_chars(entry, 30);
    f_gtk_style_context_add_class(f_gtk_widget_get_style_context(dialog), "tod-rename");
    f_g_signal_connect_data(entry, "activate", (void *)tod_on_activate, dialog, NULL, 0);
    f_g_signal_connect_data(dialog, "focus-out-event", (void *)tod_on_focus_out, NULL, NULL, 0);
    return 1;
}

/* --- rinomina VERA sul nome (Pietro: "come Windows o Mac, che rinomini direttamente il nome") ---
 * Nella finestra di Thunar la vista (icone: ExoIconView; dettagli: GtkTreeView) sa modificare il
 * testo di un elemento: si rende modificabile la colonna del nome, si avvia la modifica sul file
 * selezionato e si aspetta (ciclo annidato). Il nome scritto va nella casella della finestra di
 * rinomina, che non viene mai mostrata, e si risponde "Rinomina": Thunar fa il resto come sempre
 * (controlli, errori, Annulla). Se qualcosa non torna, si ripiega sulla casella compatta. */
typedef struct { void *loop; int started, done, fired; char *text; void *view; int icons, sel_s, sel_e; } TodEdit;

/* fix: campo e vista (con un riferimento ciascuno); se il campo non ha ancora un posto, la vista si risistema
 * subito con la sua stessa misura: exo_icon_view_size_allocate assegna lo spazio ai campi in modifica */
static int tod_edit_fix_alloc(void **fix) {
    GTKFN(int, gtk_widget_get_allocated_width, void *)
    GTKFN(void, gtk_widget_get_allocation, void *, int *)
    GTKFN(void, gtk_widget_get_preferred_size, void *, void *, void *)
    GTKFN(void, gtk_widget_queue_allocate, void *)
    GTKFN(void, gtk_widget_size_allocate, void *, int *)
    GTKFN(void *, gtk_widget_get_parent, void *)
    GTKFN(void, g_object_unref, void *)
    void *entry = fix[0], *view = fix[1];
    if (f_gtk_widget_get_parent(entry) == view && f_gtk_widget_get_allocated_width(entry) <= 1) {
        int a[4] = {0};   /* GtkAllocation: x, y, larghezza, altezza */
        f_gtk_widget_get_allocation(view, a);
        f_gtk_widget_get_preferred_size(view, NULL, NULL);   /* GTK vuole la misura chiesta prima di assegnarla */
        f_gtk_widget_queue_allocate(view);
        f_gtk_widget_size_allocate(view, a);
        fprintf(stderr, "tod-rinomina: campo senza posto, vista risistemata (%d px)\n", f_gtk_widget_get_allocated_width(entry));
    }
    f_g_object_unref(entry);
    f_g_object_unref(view);
    free(fix);
    return 0;
}

static int /* il valore si ignora */ tod_edit_started(void *cell, void *editable, const char *path, TodEdit *e) {
    (void)path;
    GTKFN(const char *, gtk_entry_get_text, void *)
    GTKFN(void, gtk_editable_select_region, void *, int, int)
    GTKFN(void *, gtk_widget_get_style_context, void *)
    GTKFN(void, gtk_style_context_add_class, void *, const char *)
    GTKFN(void, gtk_entry_set_alignment, void *, float)
    GTKFN(void *, gtk_widget_get_screen, void *)
    e->started = 1;
    tod_rename_css(f_gtk_widget_get_screen(editable));
    f_gtk_style_context_add_class(f_gtk_widget_get_style_context(editable), "tod-rinomina");
    if (e->icons) f_gtk_entry_set_alignment(editable, 0.5f);   /* sotto l'icona, centrato come l'etichetta */
    const char *t = f_gtk_entry_get_text(editable);
    if (t) tod_hide_label(*(void **)cell, e->view, t);   /* sotto il campo niente nome vecchio */
    /* ExoIconView dà al campo il suo spazio solo nella propria size_allocate; con un elemento appena creato (senza
     * che nel frattempo si sia aperta una finestra) GTK non la richiamava più e il campo restava di 1x1 pixel,
     * invisibile anche se si poteva scrivere (8 novembre). Appena il campo è dentro la vista si controlla. */
    GTKFN(void *, g_object_ref, void *)
    GTKFN(unsigned, g_idle_add, void *, void *)
    void **fix = malloc(2 * sizeof *fix);
    fix[0] = f_g_object_ref(editable);
    fix[1] = f_g_object_ref(e->view);
    f_g_idle_add((void *)tod_edit_fix_alloc, fix);
    /* la stessa scelta della finestra di Thunar: cartelle per intero, file senza estensione */
    if (e->sel_e > e->sel_s) f_gtk_editable_select_region(editable, e->sel_s, e->sel_e);
    else {
        GTKFN(long, g_utf8_pointer_to_offset, const char *, const char *)
        const char *dot = t ? strrchr(t, '.') : NULL;
        if (t && dot && dot != t) f_gtk_editable_select_region(editable, 0, (int)f_g_utf8_pointer_to_offset(t, dot));
    }
    return 0;
}

static int /* il valore si ignora */ tod_edit_done(void *cell, const char *path, const char *text, TodEdit *e) {
    (void)cell; (void)path;
    GTKFN(void, g_main_loop_quit, void *)
    free(e->text);
    e->text = text ? strdup(text) : NULL;
    e->done = 1;
    f_g_main_loop_quit(e->loop);
    return 0;
}

static int /* il valore si ignora */ tod_edit_canceled(void *cell, TodEdit *e) {
    (void)cell;
    GTKFN(void, g_main_loop_quit, void *)
    e->done = 1;
    f_g_main_loop_quit(e->loop);
    return 0;
}

static int tod_edit_watchdog(TodEdit *e) {   /* la modifica non è partita: si esce e si ripiega */
    GTKFN(void, g_main_loop_quit, void *)
    e->fired = 1;
    if (!e->started) f_g_main_loop_quit(e->loop);
    return 0;
}

/* Thunar crea la finestra di rinomina senza genitore (le passa solo lo schermo): si cerca la
 * finestra di Thunar attiva; se non risulta attiva ma ce n'è una sola visibile, è quella */
static void *tod_active_window(void *dialog) {
    GTKFN(TodList *, gtk_window_list_toplevels, void)
    GTKFN(int, gtk_window_is_active, void *)
    GTKFN(int, gtk_window_has_toplevel_focus, void *)
    GTKFN(int, gtk_widget_get_visible, void *)
    GTKFN(void, g_list_free, TodList *)
    void *active = NULL, *only = NULL;
    int visible = 0;
    TodList *all = f_gtk_window_list_toplevels();
    for (TodList *l = all; l; l = l->next) {
        if (l->data == dialog || !strstr(tod_type_name(l->data), "ThunarWindow")) continue;
        int act = f_gtk_window_is_active(l->data), foc = f_gtk_window_has_toplevel_focus(l->data);
        int vis = f_gtk_widget_get_visible(l->data);
        fprintf(stderr, "tod-rinomina: finestra %s attiva=%d fuoco=%d visibile=%d\n", tod_type_name(l->data), act, foc, vis);
        if ((act || foc) && !active) active = l->data;
        if (vis) { visible++; only = l->data; }
    }
    f_g_list_free(all);
    return active ? active : visible == 1 ? only : NULL;
}

/* La finestra "Nuova cartella"/"Nuovo documento" di Thunar e della scrivania: casella del nome di libxfce4ui
 * (XfceFilenameInput) e l'icona del TIPO di file (GIcon). Quella di rinomina di Thunar ha l'icona del file
 * disegnata (cairo surface): così le due non si confondono. */
static void *tod_create_entry(void *dialog) {
    GTKFN(void *, gtk_dialog_get_content_area, void *)
    GTKFN(int, gtk_image_get_storage_type, void *)
    void *content = f_gtk_dialog_get_content_area(dialog);
    void *input = tod_find_type(content, "XfceFilenameInput");
    void *image = tod_find_type(content, "GtkImage");
    if (!input || !image || f_gtk_image_get_storage_type(image) != 6 /* GTK_IMAGE_GICON */) return NULL;
    return tod_find_type(input, "GtkEntry");
}

#define TOD_WHY(msg) do { fprintf(stderr, "tod-rinomina: sul nome no, %s\n", msg); return 0; } while (0)

static int tod_inline_rename(void *dialog, int *response) {
    GTKFN(const char *, g_get_prgname, void)
    GTKFN(void *, gtk_window_get_transient_for, void *)
    GTKFN(void *, gtk_window_get_focus, void *)
    GTKFN(tod_gtype, exo_icon_view_get_type, void)
    GTKFN(tod_gtype, gtk_tree_view_get_type, void)
    GTKFN(int, g_type_check_instance_is_a, void *, tod_gtype)
    GTKFN(TodList *, exo_icon_view_get_selected_items, void *)
    GTKFN(void, exo_icon_view_set_cursor, void *, void *, void *, int)
    GTKFN(void *, gtk_tree_view_get_selection, void *)
    GTKFN(TodList *, gtk_tree_selection_get_selected_rows, void *, void **)
    GTKFN(void *, gtk_tree_view_get_column, void *, int)
    GTKFN(void, gtk_tree_view_set_cursor_on_cell, void *, void *, void *, void *, int)
    GTKFN(TodList *, gtk_cell_layout_get_cells, void *)
    GTKFN(void, g_list_free, TodList *)
    GTKFN(void, gtk_tree_path_free, void *)
    GTKFN(void, g_object_set, void *, const char *, ...)
    GTKFN(unsigned long, g_signal_connect_data, void *, const char *, void *, void *, void *, int)
    GTKFN(void, g_signal_handler_disconnect, void *, unsigned long)
    GTKFN(void *, g_main_loop_new, void *, int)
    GTKFN(void, g_main_loop_run, void *)
    GTKFN(void, g_main_loop_unref, void *)
    GTKFN(unsigned, g_timeout_add, unsigned, void *, void *)
    GTKFN(int, g_source_remove, unsigned)
    GTKFN(void, gtk_entry_set_text, void *, const char *)
    GTKFN(const char *, gtk_entry_get_text, void *)
    GTKFN(tod_gtype, gtk_cell_renderer_text_get_type, void)
    GTKFN(tod_gtype, gtk_cell_renderer_get_type, void)
    GTKFN(tod_gtype, gtk_scrolled_window_get_type, void)
    GTKFN(tod_gtype, g_type_from_name, const char *)
    GTKFN(void, g_type_query, tod_gtype, TodTypeQuery *)
    GTKFN(void *, gtk_widget_get_parent, void *)
    GTKFN(void *, gtk_widget_get_toplevel, void *)
    GTKFN(void *, gtk_dialog_get_content_area, void *)
    GTKFN(void *, gtk_dialog_get_action_area, void *)
    GTKFN(int, gtk_dialog_get_response_for_widget, void *, void *)
    GTKFN(TodList *, gtk_container_get_children, void *)
    GTKFN(int, gtk_editable_get_selection_bounds, void *, int *, int *)
    const char *prg = f_g_get_prgname();
    if (!prg || strcasecmp(prg, "thunar") != 0 || getenv("TOD_RINOMINA_CLASSICA")) return 0;
    if (tod_create_entry(dialog)) return 0;   /* "Nuova cartella" non è una rinomina (prima la scambiava) */
    int images = 0, labels = 0, entries = 0;
    void *entry = tod_scan(f_gtk_dialog_get_content_area(dialog), &images, &labels, &entries, 0);
    if (!entry || entries != 1 || images < 1 || labels < 1) return 0;
    void *parent = f_gtk_window_get_transient_for(dialog);
    if (!parent) parent = tod_active_window(dialog);
    if (!parent) TOD_WHY("nessuna finestra di Thunar trovata");
    void *view = f_gtk_window_get_focus(parent);
    if (!view) TOD_WHY("nessun elemento con il fuoco");
    int icons = f_g_type_check_instance_is_a(view, f_exo_icon_view_get_type());
    int tree = !icons && f_g_type_check_instance_is_a(view, f_gtk_tree_view_get_type());
    if (!icons && !tree) {
        fprintf(stderr, "tod-rinomina: sul nome no, il fuoco è su %s (finestra %s)\n", tod_type_name(view),
                tod_type_name(f_gtk_widget_get_toplevel(view)));
        return 0;
    }
    void *path = NULL, *column = NULL, *cell = NULL;
    TodList *sel = icons ? f_exo_icon_view_get_selected_items(view)
                         : f_gtk_tree_selection_get_selected_rows(f_gtk_tree_view_get_selection(view), NULL);
    if (!sel || sel->next) {   /* nessuno o più di un file selezionato */
        for (TodList *l = sel; l; l = l->next) f_gtk_tree_path_free(l->data);
        f_g_list_free(sel);
        TOD_WHY("selezione non di un solo file");
    }
    path = sel->data;
    f_g_list_free(sel);
    /* ExoIconView non elenca le sue celle: il campo del nome si legge da ThunarStandardView
     * (thunar-standard-view.h 4.20: dopo GtkScrolledWindow, preferences, model, icon_factory,
     * icon_renderer, name_renderer). Ogni puntatore si verifica sul tipo prima di usarlo. */
    tod_gtype tsv = f_g_type_from_name("ThunarStandardView");
    void *sv = view;
    while (sv && !(tsv && f_g_type_check_instance_is_a(sv, tsv))) sv = f_gtk_widget_get_parent(sv);
    if (sv) {
        TodTypeQuery q = {0};
        f_g_type_query(f_gtk_scrolled_window_get_type(), &q);
        void **fields = (void **)((char *)sv + q.instance_size);
        if (q.instance_size && fields[3] && fields[4] &&
            f_g_type_check_instance_is_a(fields[3], f_gtk_cell_renderer_get_type()) &&
            f_g_type_check_instance_is_a(fields[4], f_gtk_cell_renderer_text_get_type()))
            cell = fields[4];
    }
    if (tree) {   /* nei dettagli: la colonna che contiene il nome */
        for (int i = 0; !column; i++) {
            void *c = f_gtk_tree_view_get_column(view, i);
            if (!c) break;
            TodList *cells = f_gtk_cell_layout_get_cells(c);
            for (TodList *l = cells; l; l = l->next) {
                if (!cell && f_g_type_check_instance_is_a(l->data, f_gtk_cell_renderer_text_get_type())) cell = l->data;
                if (l->data == cell) column = c;
            }
            f_g_list_free(cells);
        }
        if (!column) cell = NULL;
    }
    fprintf(stderr, "tod-rinomina: cella %s\n", cell ? tod_type_name(cell) : "(nessuna)");
    if (!cell) { f_gtk_tree_path_free(path); TOD_WHY("nessuna cella di testo"); }
    /* la risposta "Rinomina" della finestra (mai mostrata) */
    void *actions = f_gtk_dialog_get_action_area(dialog);
    if (actions) {
        TodList *btns = f_gtk_container_get_children(actions);
        for (TodList *l = btns; l; l = l->next) {
            int r = f_gtk_dialog_get_response_for_widget(dialog, l->data);
            if (r != -6 && r != -4 && r != -1) tod_ok_response = r;
        }
        f_g_list_free(btns);
    }
    int sel_s = 0, sel_e = 0;
    f_gtk_editable_get_selection_bounds(entry, &sel_s, &sel_e);   /* quella che Thunar ha preparato */
    TodEdit e = { f_g_main_loop_new(NULL, 0), 0, 0, 0, NULL, view, icons, sel_s, sel_e };
    f_g_object_set(cell, "editable", 1, NULL);
    unsigned long h1 = f_g_signal_connect_data(cell, "editing-started", (void *)tod_edit_started, &e, NULL, 0);
    unsigned long h2 = f_g_signal_connect_data(cell, "edited", (void *)tod_edit_done, &e, NULL, 0);
    unsigned long h3 = f_g_signal_connect_data(cell, "editing-canceled", (void *)tod_edit_canceled, &e, NULL, 0);
    if (icons) f_exo_icon_view_set_cursor(view, path, cell, 1);
    else f_gtk_tree_view_set_cursor_on_cell(view, path, column, cell, 1);
    unsigned wd = f_g_timeout_add(1500, (void *)tod_edit_watchdog, &e);
    if (!e.done) f_g_main_loop_run(e.loop);
    if (!e.fired) f_g_source_remove(wd);
    f_g_signal_handler_disconnect(cell, h1);
    f_g_signal_handler_disconnect(cell, h2);
    f_g_signal_handler_disconnect(cell, h3);
    f_g_object_set(cell, "editable", 0, NULL);   /* niente modifica col doppio clic */
    f_g_main_loop_unref(e.loop);
    tod_hide_label(NULL, view, NULL);   /* il nome (nuovo) torna a disegnarsi */
    f_gtk_tree_path_free(path);
    if (!e.started) { free(e.text); TOD_WHY("la modifica sul nome non è partita"); }   /* casella compatta */
    const char *old = f_gtk_entry_get_text(entry);
    if (e.text && *e.text && (!old || strcmp(old, e.text) != 0)) {
        f_gtk_entry_set_text(entry, e.text);
        *response = tod_ok_response;
    } else {
        *response = -6;   /* invariato o vuoto: come Annulla */
    }
    free(e.text);
    return 1;
}

static int tod_create_inline(void *dialog, int *response);

int gtk_dialog_run(void *dialog) {
    static int (*real)(void *);
    if (!real) real = (int (*)(void *))tod_next("gtk_dialog_run");
    int response;
    if (tod_create_inline(dialog, &response)) return response;   /* Nuova cartella: niente finestra, come macOS */
    if (tod_inline_rename(dialog, &response)) {   /* la finestra non si vede mai */
        fprintf(stderr, "tod-rinomina: sul nome (risposta %d)\n", response);
        return response;
    }
    if (tod_compact_rename(dialog)) fprintf(stderr, "tod-rinomina: casella compatta\n");
    return real ? real(dialog) : -4;
}

/* --- rinomina sul nome anche sulla SCRIVANIA ---
 * xfdesktop non sa modificare le etichette: per rinominare chiede a Thunar (D-Bus RenameFile), che apre la sua
 * finestra. Qui si intercetta quella chiamata dentro xfdesktop e il nome diventa modificabile al suo posto, come su
 * macOS. 8 novembre (Pietro: "hai fatto un'etichetta sopra il reale"): l'etichetta vera non si disegna più mentre si
 * scrive (tod_hide_label) e al suo posto c'è un campo con lo stesso carattere, largo al massimo quanto l'etichetta,
 * che va a capo come lei e cresce o si stringe mentre si scrive, centrato sotto l'icona; aspetto in tod_rename_css.
 * Invio, Tab o clic altrove: rinomina; Esc: annulla. Misure dalla griglia di xfdesktop 4.20 (xfdesktop-icon-view.c:
 * etichetta a cell-padding*2 + icona dall'alto della casella, testo largo cell-text-width-proportion*icona, margine
 * interno label-radius). Se qualcosa non torna (lanciatori senza Name, più file, vista non trovata) si lascia fare a
 * Thunar come prima. */
typedef struct {
    void *win, *text, *file, *view, *font;
    char *old, *key;
    int closing, maxw, cx, top;
} TodDesk;
typedef struct { uint32_t domain; int code; char *message; } TodError;

/* campo: margine della finestra 4 (anello d'accento) + bordo 1 + spazio interno 4 a destra e a sinistra, 1 sopra e sotto */
#define TOD_DESK_PADX 9
#define TOD_DESK_PADY 6

static int tod_desk_close(TodDesk *d) {
    GTKFN(void, gtk_widget_destroy, void *)
    GTKFN(void, g_object_unref, void *)
    GTKFN(void, pango_font_description_free, void *)
    if (d->closing) return 0;
    d->closing = 1;
    tod_hide_label(NULL, d->view, NULL);   /* torna l'etichetta (col nome nuovo, se rinominato) */
    f_gtk_widget_destroy(d->win);
    f_g_object_unref(d->file);
    f_g_object_unref(d->view);
    if (d->font) f_pango_font_description_free(d->font);
    free(d->old);
    free(d->key);
    free(d);
    return 0;
}

/* lanciatore: si cambia il nome mostrato (Name o la sua traduzione in uso), non il file; poi si
 * rinnova il sigillo di fiducia, altrimenti Thunar e la scrivania lo mostrerebbero come sconosciuto */
static int tod_desk_launcher_name(void *file, char **key, char **name) {
    GTKFN(char *, g_file_get_path, void *)
    GTKFN(void *, g_key_file_new, void)
    GTKFN(int, g_key_file_load_from_file, void *, const char *, int, void *)
    GTKFN(char *, g_key_file_get_locale_for_key, void *, const char *, const char *, const char *)
    GTKFN(char *, g_key_file_get_locale_string, void *, const char *, const char *, const char *, void *)
    GTKFN(void, g_key_file_free, void *)
    GTKFN(void, g_free, void *)
    char *path = f_g_file_get_path(file);
    void *kf = f_g_key_file_new();
    int ok = path && f_g_key_file_load_from_file(kf, path, 1 /* KEEP_COMMENTS */, NULL);
    char *n = ok ? f_g_key_file_get_locale_string(kf, "Desktop Entry", "Name", NULL, NULL) : NULL;
    if (n) {
        char *loc = f_g_key_file_get_locale_for_key(kf, "Desktop Entry", "Name", NULL);
        size_t len = loc ? strlen(loc) + 8 : 5;
        *key = malloc(len);
        if (loc) snprintf(*key, len, "Name[%s]", loc); else strcpy(*key, "Name");
        *name = strdup(n);
        f_g_free(loc);
        f_g_free(n);
    }
    f_g_key_file_free(kf);
    f_g_free(path);
    return n != NULL;
}

static int tod_desk_launcher_save(TodDesk *d, const char *text, TodError **err) {
    GTKFN(char *, g_file_get_path, void *)
    GTKFN(void *, g_key_file_new, void)
    GTKFN(int, g_key_file_load_from_file, void *, const char *, int, TodError **)
    GTKFN(void, g_key_file_set_string, void *, const char *, const char *, const char *)
    GTKFN(int, g_key_file_save_to_file, void *, const char *, TodError **)
    GTKFN(void, g_key_file_free, void *)
    GTKFN(int, g_file_get_contents, const char *, char **, size_t *, TodError **)
    GTKFN(char *, g_compute_checksum_for_data, int, const void *, size_t)
    GTKFN(void *, g_file_query_info, void *, const char *, int, void *, void *)
    GTKFN(const char *, g_file_info_get_attribute_string, void *, const char *)
    GTKFN(int, g_file_set_attribute_string, void *, const char *, const char *, int, void *, void *)
    GTKFN(void, g_object_unref, void *)
    GTKFN(void, g_free, void *)
    const char *attr = "metadata::xfce-exe-checksum";
    void *info = f_g_file_query_info(d->file, attr, 0, NULL, NULL);
    int trusted = info && f_g_file_info_get_attribute_string(info, attr);
    if (info) f_g_object_unref(info);
    char *path = f_g_file_get_path(d->file);
    void *kf = f_g_key_file_new();
    int ok = path && f_g_key_file_load_from_file(kf, path, 1, err);
    if (ok) {
        f_g_key_file_set_string(kf, "Desktop Entry", d->key, text);
        ok = f_g_key_file_save_to_file(kf, path, err);
    }
    char *data = NULL;
    size_t len = 0;
    if (ok && trusted && f_g_file_get_contents(path, &data, &len, NULL)) {
        char *sum = f_g_compute_checksum_for_data(2 /* SHA256 */, data, len);
        if (sum) f_g_file_set_attribute_string(d->file, attr, sum, 0, NULL, NULL);
        f_g_free(sum);
        f_g_free(data);
    }
    f_g_key_file_free(kf);
    f_g_free(path);
    return ok;
}

/* il testo scritto, su una riga sola (un a capo incollato diventa uno spazio); da liberare con free */
static char *tod_desk_text(TodDesk *d) {
    GTKFN(void *, gtk_text_view_get_buffer, void *)
    GTKFN(void, gtk_text_buffer_get_bounds, void *, void *, void *)
    GTKFN(char *, gtk_text_buffer_get_text, void *, const void *, const void *, int)
    GTKFN(void, g_free, void *)
    long s[16], e[16];   /* GtkTextIter: 80 byte */
    void *buf = f_gtk_text_view_get_buffer(d->text);
    f_gtk_text_buffer_get_bounds(buf, s, e);
    char *g = f_gtk_text_buffer_get_text(buf, s, e, 0);
    char *t = strdup(g ? g : "");
    f_g_free(g);
    for (char *p = t; *p; p++) if (*p == '\n' || *p == '\r' || *p == '\t') *p = ' ';
    return t;
}

static int tod_desk_commit(TodDesk *d, int keep_on_error) {
    GTKFN(void *, g_file_set_display_name, void *, const char *, void *, TodError **)
    GTKFN(void, g_object_unref, void *)
    GTKFN(void, g_error_free, TodError *)
    GTKFN(void, gtk_widget_error_bell, void *)
    if (d->closing) return 0;
    char *t = tod_desk_text(d);
    if (*t && strcmp(t, d->old) != 0) {
        TodError *err = NULL;
        void *nf = d->key ? (tod_desk_launcher_save(d, t, &err) ? d : NULL)
                          : f_g_file_set_display_name(d->file, t, NULL, &err);
        if (nf && !d->key) f_g_object_unref(nf);
        if (!nf) {
            fprintf(stderr, "tod-rinomina: scrivania, errore: %s\n", err && err->message ? err->message : "?");
            if (err) f_g_error_free(err);
            if (keep_on_error) { free(t); f_gtk_widget_error_bell(d->text); return 0; }   /* si corregge il nome */
        }
    }
    free(t);
    return tod_desk_close(d);
}

static int tod_desk_focus_out(void *w, void *ev, TodDesk *d) { (void)w; (void)ev; tod_desk_commit(d, 0); return 0; }
static int tod_desk_key(void *w, void *ev, TodDesk *d) {
    (void)w;
    GTKFN(int, gdk_event_get_keyval, void *, unsigned *)
    unsigned kv = 0;
    if (!f_gdk_event_get_keyval(ev, &kv)) return 0;
    if (kv == 0xff1b /* Esc */) { tod_desk_close(d); return 1; }
    if (kv == 0xff0d || kv == 0xff8d || kv == 0xfe34 /* Invio */ || kv == 0xff09 /* Tab */) {
        tod_desk_commit(d, 1);
        return 1;
    }
    return 0;
}

/* misura il testo come lo disegnerebbe l'etichetta e adatta campo e finestra, centrati sotto l'icona */
static int tod_desk_fit(void *buffer, TodDesk *d) {
    (void)buffer;
    GTKFN(void *, gtk_widget_create_pango_layout, void *, const char *)
    GTKFN(void, pango_layout_set_font_description, void *, void *)
    GTKFN(void, pango_layout_set_width, void *, int)
    GTKFN(void, pango_layout_set_wrap, void *, int)
    GTKFN(void, pango_layout_set_alignment, void *, int)
    GTKFN(void, pango_layout_get_pixel_size, void *, int *, int *)
    GTKFN(void, pango_layout_get_pixel_extents, void *, int *, int *)
    GTKFN(void, g_object_unref, void *)
    GTKFN(void, gtk_widget_set_size_request, void *, int, int)
    GTKFN(void, gtk_window_resize, void *, int, int)
    GTKFN(void, gtk_window_move, void *, int, int)
    if (d->closing) return 0;
    char *t = tod_desk_text(d);
    void *layout = f_gtk_widget_create_pango_layout(d->text, *t ? t : " ");
    free(t);
    if (d->font) f_pango_layout_set_font_description(layout, d->font);
    int w = 0, h = 0;
    f_pango_layout_get_pixel_size(layout, &w, &h);   /* su una riga: la larghezza naturale */
    w += 3;   /* il cursore in fondo */
    if (w > d->maxw) w = d->maxw;
    if (w < 12) w = 12;
    f_pango_layout_set_width(layout, w * 1024 /* PANGO_SCALE */);
    f_pango_layout_set_wrap(layout, 2 /* PANGO_WRAP_WORD_CHAR, come GTK_WRAP_WORD_CHAR */);
    f_pango_layout_set_alignment(layout, 1 /* PANGO_ALIGN_CENTER */);
    int lg[4] = {0};   /* PangoRectangle logico: x, y, larghezza, altezza */
    f_pango_layout_get_pixel_extents(layout, NULL, lg);
    h = lg[3];
    if (lg[2] + 3 < w) w = lg[2] + 3;   /* andando a capo le righe sono più corte: il campo si stringe come macOS */
    f_g_object_unref(layout);
    f_gtk_widget_set_size_request(d->text, w, h);
    f_gtk_window_resize(d->win, 1, 1);   /* la finestra si stringe con il testo */
    f_gtk_window_move(d->win, d->cx - (w + 2 * TOD_DESK_PADX) / 2, d->top - TOD_DESK_PADY);
    return 0;
}

/* un solo elemento selezionato, che è anche quello attivo: la sua casella nella griglia */
static void *tod_desk_view(void *w, int *row, int *col) {
    GTKFN(TodList *, gtk_container_get_children, void *)
    GTKFN(void, g_list_free, TodList *)
    GTKFN(int, xfdesktop_icon_view_get_cursor, void *, void *, int *, int *)
    GTKFN(TodList *, xfdesktop_icon_view_get_selected_items, void *)
    GTKFN(void *, xfdesktop_icon_view_get_model, void *)
    GTKFN(void *, gtk_tree_model_get_path, void *, void *)
    GTKFN(int, gtk_tree_path_compare, void *, void *)
    GTKFN(void, gtk_tree_path_free, void *)
    if (!strcmp(tod_type_name(w), "XfdesktopIconView")) {
        long iter[4] = {0};   /* GtkTreeIter: stamp + 3 puntatori */
        if (!f_xfdesktop_icon_view_get_cursor(w, iter, row, col)) return NULL;
        TodList *sel = f_xfdesktop_icon_view_get_selected_items(w);
        int ok = sel && !sel->next;
        if (ok) {
            void *cur = f_gtk_tree_model_get_path(f_xfdesktop_icon_view_get_model(w), iter);
            ok = cur && f_gtk_tree_path_compare(cur, sel->data) == 0;
            if (cur) f_gtk_tree_path_free(cur);
        }
        for (TodList *l = sel; l; l = l->next) f_gtk_tree_path_free(l->data);
        f_g_list_free(sel);
        return ok ? w : NULL;
    }
    if (!tod_is_container(w)) return NULL;
    void *found = NULL;
    TodList *kids = f_gtk_container_get_children(w);
    for (TodList *l = kids; l && !found; l = l->next) found = tod_desk_view(l->data, row, col);
    f_g_list_free(kids);
    return found;
}

/* il nome dell'elemento nella casella (row, col) di view diventa modificabile; uri: il file */
static int tod_desk_edit(void *view, int row, int col, const char *uri) {
    GTKFN(int, xfdesktop_icon_view_slot_coords_to_widget_coords, void *, int, int, int *, int *)
    GTKFN(int, xfdesktop_icon_view_get_icon_size, void *)
    GTKFN(void, gtk_widget_style_get, void *, const char *, ...)
    GTKFN(void, g_object_get, void *, const char *, ...)
    GTKFN(void *, g_object_ref, void *)
    GTKFN(void *, gtk_widget_get_window, void *)
    GTKFN(int, gdk_window_get_origin, void *, int *, int *)
    GTKFN(void *, gtk_widget_get_screen, void *)
    GTKFN(void *, gdk_screen_get_rgba_visual, void *)
    GTKFN(void, gtk_widget_set_visual, void *, void *)
    GTKFN(void *, gtk_widget_get_pango_context, void *)
    GTKFN(void *, pango_context_get_font_description, void *)
    GTKFN(void *, pango_font_description_copy, void *)
    GTKFN(void, pango_font_description_set_size, void *, int)
    GTKFN(void, gtk_widget_override_font, void *, void *)
    GTKFN(tod_gtype, g_type_from_name, const char *)
    GTKFN(void *, g_type_class_ref, tod_gtype)
    GTKFN(void *, g_file_new_for_uri, const char *)
    GTKFN(char *, g_file_get_basename, void *)
    GTKFN(int, g_file_query_file_type, void *, int, void *)
    GTKFN(void, g_free, void *)
    GTKFN(long, g_utf8_pointer_to_offset, const char *, const char *)
    GTKFN(void *, gtk_window_new, int)
    GTKFN(void *, gtk_box_new, int, int)
    GTKFN(void *, gtk_text_view_new, void)
    GTKFN(void *, gtk_text_view_get_buffer, void *)
    GTKFN(void, gtk_text_view_set_wrap_mode, void *, int)
    GTKFN(void, gtk_text_view_set_justification, void *, int)
    GTKFN(void, gtk_text_view_set_accepts_tab, void *, int)
    GTKFN(void, gtk_text_buffer_set_text, void *, const char *, int)
    GTKFN(void, gtk_text_buffer_get_iter_at_offset, void *, void *, int)
    GTKFN(void, gtk_text_buffer_select_range, void *, const void *, const void *)
    GTKFN(void, gtk_container_add, void *, void *)
    GTKFN(void, gtk_window_set_decorated, void *, int)
    GTKFN(void, gtk_window_set_skip_taskbar_hint, void *, int)
    GTKFN(void, gtk_window_set_skip_pager_hint, void *, int)
    GTKFN(void, gtk_window_set_keep_above, void *, int)
    GTKFN(void, gtk_widget_show_all, void *)
    GTKFN(void, gtk_window_present_with_time, void *, unsigned)
    GTKFN(void, gtk_widget_grab_focus, void *)
    GTKFN(void *, gtk_widget_get_style_context, void *)
    GTKFN(void, gtk_style_context_add_class, void *, const char *)
    GTKFN(unsigned long, g_signal_connect_data, void *, const char *, void *, void *, void *, int)
    int wx, wy, ox, oy;
    if (!f_xfdesktop_icon_view_slot_coords_to_widget_coords(view, row, col, &wx, &wy)) {
        fprintf(stderr, "tod-rinomina: scrivania, icona non trovata\n");
        return 0;
    }
    if (!f_gtk_widget_get_window(view) || !f_gdk_window_get_origin(f_gtk_widget_get_window(view), &ox, &oy)) { ox = 0; oy = 0; }
    int pad = 6, icon = f_xfdesktop_icon_view_get_icon_size(view);
    double prop = 1.75, radius = 4, pts = 0;
    int pts_set = 0;
    f_gtk_widget_style_get(view, "cell-padding", &pad, "cell-text-width-proportion", &prop, "label-radius", &radius, NULL);
    f_g_object_get(view, "icon-font-size", &pts, "icon-font-size-set", &pts_set, NULL);
    int textw = (int)(prop * icon);   /* TEXT_WIDTH: l'etichetta va a capo qui */
    void *file = f_g_file_new_for_uri(uri);
    size_t n = strlen(uri);
    int launcher = n > 8 && !strcmp(uri + n - 8, ".desktop");   /* lanciatore: si rinomina il nome mostrato */
    TodDesk *d = calloc(1, sizeof *d);
    d->file = file;
    d->view = f_g_object_ref(view);
    if (!launcher || !tod_desk_launcher_name(file, &d->key, &d->old)) {
        char *base = f_g_file_get_basename(file);
        d->old = strdup(base ? base : "");
        f_g_free(base);
    }
    d->maxw = textw;
    d->cx = ox + wx + (textw + 2 * pad) / 2;          /* centro della casella (SLOT_SIZE = testo + 2 padding) */
    d->top = oy + wy + 2 * pad + icon + (int)radius;  /* prima riga del testo dell'etichetta */
    /* il carattere dell'etichetta: quello della vista, con la misura scelta nelle impostazioni della scrivania */
    d->font = f_pango_font_description_copy(f_pango_context_get_font_description(f_gtk_widget_get_pango_context(view)));
    if (d->font && pts_set && pts > 0) f_pango_font_description_set_size(d->font, (int)(pts * 1024));
    d->win = f_gtk_window_new(0);
    void *screen = f_gtk_widget_get_screen(d->win);
    void *rgba = f_gdk_screen_get_rgba_visual(screen);
    if (rgba) f_gtk_widget_set_visual(d->win, rgba);   /* angoli tondi e anello trasparenti attorno al campo */
    tod_rename_css(screen);
    void *box = f_gtk_box_new(0, 0);
    d->text = f_gtk_text_view_new();
    f_gtk_style_context_add_class(f_gtk_widget_get_style_context(d->win), "tod-rinomina-finestra");
    f_gtk_style_context_add_class(f_gtk_widget_get_style_context(box), "tod-rinomina-campo");
    f_gtk_style_context_add_class(f_gtk_widget_get_style_context(d->text), "tod-rinomina-testo");
    if (d->font) f_gtk_widget_override_font(d->text, d->font);
    f_gtk_text_view_set_wrap_mode(d->text, 3 /* GTK_WRAP_WORD_CHAR */);
    f_gtk_text_view_set_justification(d->text, 2 /* GTK_JUSTIFY_CENTER */);
    f_gtk_text_view_set_accepts_tab(d->text, 0);
    void *buf = f_gtk_text_view_get_buffer(d->text);
    f_gtk_text_buffer_set_text(buf, d->old, -1);
    f_gtk_container_add(box, d->text);
    f_gtk_container_add(d->win, box);
    f_gtk_window_set_decorated(d->win, 0);
    f_gtk_window_set_skip_taskbar_hint(d->win, 1);
    f_gtk_window_set_skip_pager_hint(d->win, 1);
    f_gtk_window_set_keep_above(d->win, 1);
    tod_desk_fit(buf, d);
    f_g_signal_connect_data(buf, "changed", (void *)tod_desk_fit, d, NULL, 0);
    f_g_signal_connect_data(d->win, "focus-out-event", (void *)tod_desk_focus_out, d, NULL, 0);
    f_g_signal_connect_data(d->text, "key-press-event", (void *)tod_desk_key, d, NULL, 0);
    /* sotto il campo l'etichetta vera non si disegna più (la classe del suo disegnatore, di xfdesktop) */
    tod_gtype lt = f_g_type_from_name("XfdesktopCellRendererIconLabel");
    if (!lt || !tod_hide_label(f_g_type_class_ref(lt), view, d->old))
        fprintf(stderr, "tod-rinomina: scrivania, l'etichetta sotto il campo resta\n");
    f_gtk_widget_show_all(d->win);
    f_gtk_window_present_with_time(d->win, 0);
    f_gtk_widget_grab_focus(d->text);
    /* come macOS: le cartelle e i lanciatori per intero, i file senza l'estensione */
    const char *dot = d->key || f_g_file_query_file_type(file, 0, NULL) == 2 /* G_FILE_TYPE_DIRECTORY */
                      ? NULL : strrchr(d->old, '.');
    long s[16], e[16];
    f_gtk_text_buffer_get_iter_at_offset(buf, s, 0);
    f_gtk_text_buffer_get_iter_at_offset(buf, e, dot && dot != d->old ? (int)f_g_utf8_pointer_to_offset(d->old, dot) : -1);
    f_gtk_text_buffer_select_range(buf, e, s);   /* cursore in fondo alla parte scelta, come GtkEntry */
    fprintf(stderr, "tod-rinomina: scrivania, sul nome\n");
    return 1;
}

static int tod_desk_rename(const char *uri) {
    GTKFN(TodList *, gtk_window_list_toplevels, void)
    GTKFN(void, g_list_free, TodList *)
    if (!uri || strncmp(uri, "file://", 7) != 0) return 0;
    int row = -1, col = -1;
    void *view = NULL;
    TodList *tops = f_gtk_window_list_toplevels();
    for (TodList *l = tops; l && !view; l = l->next) view = tod_desk_view(l->data, &row, &col);
    f_g_list_free(tops);
    if (!view) {
        fprintf(stderr, "tod-rinomina: scrivania, icona non trovata\n");
        return 0;
    }
    return tod_desk_edit(view, row, col, uri);
}

/* --- Nuova cartella / Nuovo documento come macOS (8 novembre) ---
 * Pietro: "senza finestra che si apre con la casella del nome, LO VOGLIO IDENTICO A MACOS". Thunar e la scrivania
 * chiedono il nome con una finestra già riempita con un nome libero ("Nuova cartella", "Nuova cartella 1"...,
 * "Nuovo file vuoto", il nome del modello). Qui quella finestra non si mostra: si risponde subito "Crea" con quel
 * nome; quando l'elemento nuovo compare (Thunar lo seleziona da sé; sulla scrivania sta nel punto del clic) si
 * seleziona e parte la rinomina sul nome, come su macOS. Thunar: con la sua azione "Rinomina" (scorciatoia F2 o
 * quella scelta dall'utente), così il resto passa da tod_inline_rename. Se la vista non si trova, la finestra si
 * mostra come prima; se l'elemento non compare entro 4 s, resta col nome proposto. */
typedef struct { void *win, *view; char *name; int ticks, desk; } TodNew;

/* scrivania: l'elemento con quel nome, quando ha la sua casella nella griglia */
static int tod_new_desk(TodNew *n) {
    GTKFN(void *, xfdesktop_icon_view_get_model, void *)
    GTKFN(void, xfdesktop_icon_view_unselect_all, void *)
    GTKFN(void, xfdesktop_icon_view_select_item, void *, void *)
    GTKFN(void, g_object_get, void *, const char *, ...)
    GTKFN(int, gtk_tree_model_get_iter_first, void *, void *)
    GTKFN(int, gtk_tree_model_iter_next, void *, void *)
    GTKFN(void, gtk_tree_model_get, void *, void *, ...)
    GTKFN(const char *, g_get_user_special_dir, int)
    GTKFN(char *, g_build_filename, const char *, ...)
    GTKFN(char *, g_filename_to_uri, const char *, const char *, void *)
    GTKFN(void, g_free, void *)
    int tc = -1, rc = -1, cc = -1;
    void *model = f_xfdesktop_icon_view_get_model(n->view);
    f_g_object_get(n->view, "text-column", &tc, "row-column", &rc, "col-column", &cc, NULL);
    if (!model || tc < 0 || rc < 0 || cc < 0) return 1;   /* niente da fare: resta così */
    long it[4] = {0};
    for (int ok = f_gtk_tree_model_get_iter_first(model, it); ok; ok = f_gtk_tree_model_iter_next(model, it)) {
        char *t = NULL;
        int r = -1, c = -1;
        f_gtk_tree_model_get(model, it, tc, &t, rc, &r, cc, &c, -1);
        int same = t && !strcmp(t, n->name);
        f_g_free(t);
        if (!same) continue;
        if (r < 0 || c < 0) return 0;   /* c'è ma non è ancora al suo posto */
        f_xfdesktop_icon_view_unselect_all(n->view);
        f_xfdesktop_icon_view_select_item(n->view, it);
        const char *desk = f_g_get_user_special_dir(0 /* G_USER_DIRECTORY_DESKTOP, come xfdesktop */);
        char *path = desk ? f_g_build_filename(desk, n->name, NULL) : NULL;
        char *uri = path ? f_g_filename_to_uri(path, NULL, NULL) : NULL;
        if (uri) tod_desk_edit(n->view, r, c, uri);
        f_g_free(uri);
        f_g_free(path);
        return 1;
    }
    return 0;
}

/* Thunar: il file nuovo è l'unico selezionato (un testo della sua riga è il nome) → la sua azione Rinomina */
static int tod_new_thunar(TodNew *n) {
    GTKFN(void *, gtk_window_get_focus, void *)
    GTKFN(tod_gtype, exo_icon_view_get_type, void)
    GTKFN(tod_gtype, gtk_tree_view_get_type, void)
    GTKFN(int, g_type_check_instance_is_a, void *, tod_gtype)
    GTKFN(TodList *, exo_icon_view_get_selected_items, void *)
    GTKFN(void *, exo_icon_view_get_model, void *)
    GTKFN(void *, gtk_tree_view_get_selection, void *)
    GTKFN(TodList *, gtk_tree_selection_get_selected_rows, void *, void **)
    GTKFN(int, gtk_tree_model_get_iter, void *, void *, void *)
    GTKFN(int, gtk_tree_model_get_n_columns, void *)
    GTKFN(tod_gtype, gtk_tree_model_get_column_type, void *, int)
    GTKFN(void, gtk_tree_model_get, void *, void *, ...)
    GTKFN(void, gtk_tree_path_free, void *)
    GTKFN(void, g_list_free, TodList *)
    GTKFN(void, g_free, void *)
    GTKFN(int, gtk_accel_map_lookup_entry, const char *, unsigned *)
    GTKFN(int, gtk_accel_groups_activate, void *, unsigned, int)
    void *view = f_gtk_window_get_focus(n->win);
    if (!view) return 0;
    int icons = f_g_type_check_instance_is_a(view, f_exo_icon_view_get_type());
    int tree = !icons && f_g_type_check_instance_is_a(view, f_gtk_tree_view_get_type());
    if (!icons && !tree) return 0;
    void *model = NULL;
    TodList *sel = icons ? f_exo_icon_view_get_selected_items(view)
                         : f_gtk_tree_selection_get_selected_rows(f_gtk_tree_view_get_selection(view), &model);
    if (icons) model = f_exo_icon_view_get_model(view);
    int found = 0;
    long it[4] = {0};
    if (sel && !sel->next && model && f_gtk_tree_model_get_iter(model, it, sel->data)) {
        int cols = f_gtk_tree_model_get_n_columns(model);
        for (int c = 0; c < cols && !found; c++) {
            if (f_gtk_tree_model_get_column_type(model, c) != 64 /* G_TYPE_STRING */) continue;
            char *t = NULL;
            f_gtk_tree_model_get(model, it, c, &t, -1);
            found = t && !strcmp(t, n->name);
            f_g_free(t);
        }
    }
    for (TodList *l = sel; l; l = l->next) f_gtk_tree_path_free(l->data);
    f_g_list_free(sel);
    if (!found) return 0;
    unsigned key[3] = {0xffbf /* F2 */, 0, 0};   /* GtkAccelKey: tasto, modificatori, flag */
    if (!f_gtk_accel_map_lookup_entry("<Actions>/ThunarStandardView/rename", key) || !key[0]) {
        key[0] = 0xffbf;
        key[1] = 0;
    }
    fprintf(stderr, "tod-nuovo: %s selezionato, rinomina sul nome\n", n->name);
    if (!f_gtk_accel_groups_activate(n->win, key[0], (int)key[1]))
        fprintf(stderr, "tod-nuovo: l'azione Rinomina di Thunar non è partita\n");
    return 1;
}

static int tod_new_tick(TodNew *n) {
    GTKFN(void, g_object_unref, void *)
    int done = ++n->ticks > 80;   /* 4 s */
    if (done) fprintf(stderr, "tod-nuovo: %s non è comparso, resta col nome proposto\n", n->name);
    else done = n->desk ? tod_new_desk(n) : tod_new_thunar(n);
    if (!done) return 1;   /* G_SOURCE_CONTINUE */
    f_g_object_unref(n->win);
    if (n->view) f_g_object_unref(n->view);
    free(n->name);
    free(n);
    return 0;
}

/* la vista delle icone della scrivania dentro w (o in una qualunque finestra di xfdesktop se w è NULL) */
static void *tod_desk_iconview(void *w) {
    GTKFN(TodList *, gtk_window_list_toplevels, void)
    GTKFN(void, g_list_free, TodList *)
    if (w) return tod_find_type(w, "XfdesktopIconView");
    void *found = NULL;
    TodList *tops = f_gtk_window_list_toplevels();
    for (TodList *l = tops; l && !found; l = l->next) found = tod_find_type(l->data, "XfdesktopIconView");
    f_g_list_free(tops);
    return found;
}

static int tod_create_inline(void *dialog, int *response) {
    GTKFN(const char *, g_get_prgname, void)
    GTKFN(void *, gtk_window_get_transient_for, void *)
    GTKFN(void *, gtk_window_get_focus, void *)
    GTKFN(tod_gtype, exo_icon_view_get_type, void)
    GTKFN(tod_gtype, gtk_tree_view_get_type, void)
    GTKFN(int, g_type_check_instance_is_a, void *, tod_gtype)
    GTKFN(const char *, gtk_entry_get_text, void *)
    GTKFN(void *, gtk_dialog_get_action_area, void *)
    GTKFN(TodList *, gtk_container_get_children, void *)
    GTKFN(void, g_list_free, TodList *)
    GTKFN(int, gtk_dialog_get_response_for_widget, void *, void *)
    GTKFN(int, gtk_widget_is_sensitive, void *)
    GTKFN(void *, gtk_widget_get_toplevel, void *)
    GTKFN(void *, g_object_ref, void *)
    GTKFN(unsigned, g_timeout_add, unsigned, void *, void *)
    const char *prg = f_g_get_prgname();
    if (!prg || getenv("TOD_RINOMINA_CLASSICA") || getenv("TOD_NUOVO_CLASSICO")) return 0;
    int desk = !strcmp(prg, "xfdesktop");
    if (!desk && strcasecmp(prg, "thunar") != 0) return 0;
    void *entry = tod_create_entry(dialog);
    const char *name = entry ? f_gtk_entry_get_text(entry) : NULL;
    if (!name || !*name) return 0;
    /* "Crea": il pulsante che non è Annulla, attivo solo se il nome proposto va bene */
    int ok = 0, okr = 0;
    void *actions = f_gtk_dialog_get_action_area(dialog);
    TodList *btns = actions ? f_gtk_container_get_children(actions) : NULL;
    for (TodList *l = btns; l; l = l->next) {
        int r = f_gtk_dialog_get_response_for_widget(dialog, l->data);
        if (r != -6 && r != -4 && r != -1 && r != 0) { okr = r; ok = f_gtk_widget_is_sensitive(l->data); }
    }
    f_g_list_free(btns);
    if (!okr || !ok) return 0;
    void *win = NULL, *view = NULL;
    if (desk) {
        view = tod_desk_iconview(f_gtk_window_get_transient_for(dialog));
        if (!view) view = tod_desk_iconview(NULL);
        if (view) win = f_gtk_widget_get_toplevel(view);
    } else {
        win = f_gtk_window_get_transient_for(dialog);
        if (!win) win = tod_active_window(dialog);
        void *focus = win ? f_gtk_window_get_focus(win) : NULL;
        if (!focus || !(f_g_type_check_instance_is_a(focus, f_exo_icon_view_get_type()) ||
                        f_g_type_check_instance_is_a(focus, f_gtk_tree_view_get_type()))) win = NULL;
    }
    if (!win) {
        fprintf(stderr, "tod-nuovo: vista non trovata, la finestra del nome resta\n");
        return 0;
    }
    TodNew *n = calloc(1, sizeof *n);
    n->win = f_g_object_ref(win);
    n->view = view ? f_g_object_ref(view) : NULL;
    n->name = strdup(name);
    n->desk = desk;
    f_g_timeout_add(50, (void *)tod_new_tick, n);
    fprintf(stderr, "tod-nuovo: %s \"%s\" senza finestra\n", desk ? "scrivania" : "Thunar", name);
    *response = okr;
    return 1;
}

void g_dbus_proxy_call(void *proxy, const char *method, void *params, int flags, int timeout,
                       void *cancellable, void *callback, void *user_data) {
    static void (*real)(void *, const char *, void *, int, int, void *, void *, void *);
    if (!real) real = (void (*)(void *, const char *, void *, int, int, void *, void *, void *))tod_next("g_dbus_proxy_call");
    static const char *(*prgname)(void);
    if (!prgname) prgname = (const char *(*)(void))dlsym(RTLD_DEFAULT, "g_get_prgname");
    const char *prg = prgname ? prgname() : NULL;
    if (method && params && prg && !strcmp(method, "RenameFile") && !strcmp(prg, "xfdesktop") &&
        !getenv("TOD_RINOMINA_CLASSICA")) {
        void *(*ref_sink)(void *) = (void *(*)(void *))dlsym(RTLD_DEFAULT, "g_variant_ref_sink");
        void (*unref)(void *) = (void (*)(void *))dlsym(RTLD_DEFAULT, "g_variant_unref");
        void (*child)(void *, size_t, const char *, ...) = (void (*)(void *, size_t, const char *, ...))dlsym(RTLD_DEFAULT, "g_variant_get_child");
        void (*gfree)(void *) = (void (*)(void *))dlsym(RTLD_DEFAULT, "g_free");
        if (ref_sink && unref && child && gfree) {
            ref_sink(params);
            char *uri = NULL;
            child(params, 0, "s", &uri);
            int done = tod_desk_rename(uri);
            gfree(uri);
            /* rinominato qui: la richiamata di xfdesktop non serve; altrimenti a Thunar come prima */
            if (!done && real) real(proxy, method, params, flags, timeout, cancellable, callback, user_data);
            unref(params);
            return;
        }
    }
    if (real) real(proxy, method, params, flags, timeout, cancellable, callback, user_data);
}

/* --- xfwm4: ridimensionamento senza tremolii per le app lente (15 ottobre) ---
 * Durante il ridimensionamento xfwm4 manda _NET_WM_SYNC_REQUEST e aspetta che l'app confermi il
 * ridisegno (contatore XSync) prima di cambiare di nuovo misura. Qui le app non confermano mai:
 * provato con un'app Electron (contatore semplice: resta a 0 anche dopo 2 s) e con una finestra GTK
 * (contatore esteso fermo); il server invece consegna gli allarmi. Dopo 500 ms xfwm4 smette di
 * aspettare per sempre e ridimensiona a ogni movimento del mouse (~100 volte al secondo, misurato):
 * le app che disegnano con la GPU o dal container restano indietro → tremolii e bande nere.
 * Qui, solo dentro xfwm4, la conferma la diamo noi dopo TOD_XSYNC_MS (40 ms): al massimo ~25 cambi
 * di misura al secondo, e a ogni passo l'app ha il tempo di ridisegnare. Se l'app conferma da sé
 * prima, il nostro valore non è più alto del suo e non si scrive. TOD_XSYNC_CLASSICO=1 lo spegne. */
typedef struct { void *dpy; unsigned long counter; long long value; } TodSyncAck;
typedef struct { int hi; unsigned lo; } TodSyncValue;

static int tod_is_xfwm(void) {
    static int known = -1;
    if (known < 0) {
#if defined(__BIONIC__) || defined(__ANDROID__)
        const char *n = getprogname();
#else
        extern char *program_invocation_short_name;
        const char *n = program_invocation_short_name;
#endif
        known = n && strcmp(n, "xfwm4") == 0;
    }
    return known;
}

static void tod_sync_set(TodSyncAck *a) {
    static int (*query)(void *, unsigned long, TodSyncValue *);
    static int (*set)(void *, unsigned long, TodSyncValue);
    static int (*flush)(void *);
    if (!query) query = (int (*)(void *, unsigned long, TodSyncValue *))dlsym(RTLD_DEFAULT, "XSyncQueryCounter");
    if (!set) set = (int (*)(void *, unsigned long, TodSyncValue))dlsym(RTLD_DEFAULT, "XSyncSetCounter");
    if (!flush) flush = (int (*)(void *))dlsym(RTLD_DEFAULT, "XFlush");
    TodSyncValue now = {0, 0};
    if (query && set && query(a->dpy, a->counter, &now)) {
        long long cur = ((long long)now.hi << 32) | now.lo;
        if (cur < a->value) {   /* l'app non ha confermato: lo facciamo noi */
            TodSyncValue v = { (int)(a->value >> 32), (unsigned)(a->value & 0xffffffffu) };
            set(a->dpy, a->counter, v);
            if (flush) flush(a->dpy);
        }
    }
}

/* La conferma si dà quando l'app ha davvero disegnato: un secondo collegamento al server osserva
 * (estensione DAMAGE) la finestra dell'app; il primo disegno arrivato almeno TOD_XSYNC_MIN ms (100) dopo
 * la richiesta fa scattare la conferma.
 * Se l'app non disegna entro TOD_XSYNC_MS (150 ms), si conferma lo stesso. */
#define TOD_PENDING 8
static struct { unsigned long win, damage; long long t0; int drawn; TodSyncAck ack; } tod_pend[TOD_PENDING];
static void *tod_ddpy;
static int tod_damage_base = -1;
static unsigned tod_poll_id;

static long long tod_now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int tod_env_ms(const char *name, int def) {
    const char *v = getenv(name);
    return v && atoi(v) > 0 ? atoi(v) : def;
}

static int tod_sync_poll(void *unused) {
    (void)unused;
    static int (*pending)(void *);
    static int (*next)(void *, long *);
    static int (*subtract)(void *, unsigned long, unsigned long, unsigned long);
    static int (*destroy)(void *, unsigned long);
    static int (*flush)(void *);
    if (!pending) pending = (int (*)(void *))dlsym(RTLD_DEFAULT, "XPending");
    if (!next) next = (int (*)(void *, long *))dlsym(RTLD_DEFAULT, "XNextEvent");
    if (!subtract) subtract = (int (*)(void *, unsigned long, unsigned long, unsigned long))dlsym(RTLD_DEFAULT, "XDamageSubtract");
    if (!destroy) destroy = (int (*)(void *, unsigned long))dlsym(RTLD_DEFAULT, "XDamageDestroy");
    if (!flush) flush = (int (*)(void *))dlsym(RTLD_DEFAULT, "XFlush");
    long long now = tod_now_ms();
    /* Electron segnala un disegno ogni ~17 ms anche quando non ha ancora ridisegnato alla misura
     * nuova (misurato il 15 ottobre): confermare al primo disegno riportava i cambi a ~60/s, peggio.
     * Quindi almeno 100 ms (per Pietro "molto meglio"), poi al primo disegno, al più 150 ms. */
    int min_ms = tod_env_ms("TOD_XSYNC_MIN", 100), max_ms = tod_env_ms("TOD_XSYNC_MS", 150);
    long ev[24];
    while (pending && next && pending(tod_ddpy) > 0) {
        next(tod_ddpy, ev);
        if ((int)ev[0] != tod_damage_base) continue;   /* XDamageNotify: drawable, damage ai campi 4 e 5 */
        for (int i = 0; i < TOD_PENDING; i++)
            if (tod_pend[i].win && (unsigned long)ev[5] == tod_pend[i].damage) {
                if (now - tod_pend[i].t0 >= min_ms) tod_pend[i].drawn = 1;
                if (subtract) subtract(tod_ddpy, tod_pend[i].damage, 0, 0);   /* riarma l'avviso */
            }
    }
    int left = 0;
    for (int i = 0; i < TOD_PENDING; i++) {
        if (!tod_pend[i].win) continue;
        if (tod_pend[i].drawn || now - tod_pend[i].t0 >= max_ms) {
            tod_sync_set(&tod_pend[i].ack);
            if (destroy) destroy(tod_ddpy, tod_pend[i].damage);
            tod_pend[i].win = 0;
        } else left++;
    }
    if (flush) flush(tod_ddpy);
    if (!left) tod_poll_id = 0;
    return left > 0;   /* G_SOURCE_CONTINUE finché c'è qualcosa da confermare */
}

static int tod_sync_watch(void *dpy, unsigned long w, unsigned long counter, long long value) {
    static void *(*opendpy)(const char *);
    static int (*query)(void *, int *, int *);
    static unsigned long (*create)(void *, unsigned long, int);
    static unsigned (*add)(int, unsigned, void *, void *, void *);
    if (!opendpy) opendpy = (void *(*)(const char *))dlsym(RTLD_DEFAULT, "XOpenDisplay");
    if (!query) query = (int (*)(void *, int *, int *))dlsym(RTLD_DEFAULT, "XDamageQueryExtension");
    if (!create) create = (unsigned long (*)(void *, unsigned long, int))dlsym(RTLD_DEFAULT, "XDamageCreate");
    if (!add) add = (unsigned (*)(int, unsigned, void *, void *, void *))dlsym(RTLD_DEFAULT, "g_timeout_add_full");
    if (!opendpy || !query || !create || !add) return 0;
    if (!tod_ddpy) {
        int eb = 0, er = 0;
        tod_ddpy = opendpy(NULL);
        if (!tod_ddpy || !query(tod_ddpy, &eb, &er)) return 0;
        tod_damage_base = eb;   /* XDamageNotify = base + 0 */
    }
    int slot = -1;
    for (int i = 0; i < TOD_PENDING; i++) {
        if (tod_pend[i].win == w) { slot = i; break; }   /* nuova richiesta per la stessa finestra */
        if (!tod_pend[i].win && slot < 0) slot = i;
    }
    if (slot < 0) return 0;
    if (tod_pend[slot].win != w) {
        tod_pend[slot].damage = create(tod_ddpy, w, 3 /* XDamageReportNonEmpty */);
        if (!tod_pend[slot].damage) return 0;
    }
    tod_pend[slot].win = w;
    tod_pend[slot].t0 = tod_now_ms();
    tod_pend[slot].drawn = 0;
    tod_pend[slot].ack.dpy = dpy;
    tod_pend[slot].ack.counter = counter;
    tod_pend[slot].ack.value = value;
    if (!tod_poll_id) tod_poll_id = add(0, 4, (void *)tod_sync_poll, NULL, NULL);
    return 1;
}

static unsigned long tod_sync_counter(void *dpy, unsigned long w, int extended) {
    static unsigned long (*intern)(void *, const char *, int);
    static int (*getprop)(void *, unsigned long, unsigned long, long, long, int, unsigned long, unsigned long *,
                          int *, unsigned long *, unsigned long *, unsigned char **);
    static int (*xfree)(void *);
    if (!intern) intern = (unsigned long (*)(void *, const char *, int))dlsym(RTLD_DEFAULT, "XInternAtom");
    if (!getprop) getprop = (int (*)(void *, unsigned long, unsigned long, long, long, int, unsigned long, unsigned long *,
                                     int *, unsigned long *, unsigned long *, unsigned char **))dlsym(RTLD_DEFAULT, "XGetWindowProperty");
    if (!xfree) xfree = (int (*)(void *))dlsym(RTLD_DEFAULT, "XFree");
    if (!intern || !getprop || !xfree) return 0;
    unsigned long type = 0, n = 0, after = 0, counter = 0;
    int fmt = 0;
    unsigned char *data = NULL;
    if (getprop(dpy, w, intern(dpy, "_NET_WM_SYNC_REQUEST_COUNTER", 0), 0, 2, 0, 6 /* XA_CARDINAL */, &type, &fmt,
                &n, &after, &data) == 0 && data) {
        unsigned long *v = (unsigned long *)data;
        if (fmt == 32 && n >= 1) counter = extended && n >= 2 ? v[1] : v[0];
        xfree(data);
    }
    return counter;
}

typedef struct { int type; unsigned long serial; int send_event; void *display; unsigned long window;
                 unsigned long message_type; int format; long l[5]; } TodClientMessage;

int XSendEvent(void *dpy, unsigned long w, int propagate, long mask, void *event) {
    static int (*real)(void *, unsigned long, int, long, void *);
    if (!real) real = (int (*)(void *, unsigned long, int, long, void *))tod_next("XSendEvent");
    int r = real ? real(dpy, w, propagate, mask, event) : 0;
    if (event && tod_is_xfwm() && !getenv("TOD_XSYNC_CLASSICO")) {
        TodClientMessage *ev = event;
        static unsigned long sync_atom;
        if (!sync_atom) {
            unsigned long (*intern)(void *, const char *, int) =
                (unsigned long (*)(void *, const char *, int))dlsym(RTLD_DEFAULT, "XInternAtom");
            if (intern) sync_atom = intern(dpy, "_NET_WM_SYNC_REQUEST", 0);
        }
        if (getenv("TOD_XSYNC_DEBUG") && ev->type == 33)
            fprintf(stderr, "tod-xsync: messaggio a 0x%lx l0 %ld l4 %ld\n", w, ev->l[0], ev->l[4]);
        if (ev->type == 33 /* ClientMessage */ && ev->format == 32 && sync_atom && (unsigned long)ev->l[0] == sync_atom) {
            unsigned long counter = tod_sync_counter(dpy, w, ev->l[4] != 0);
            long long value = (long long)(unsigned long)(unsigned)ev->l[2] | ((long long)ev->l[3] << 32);
            if (counter && tod_sync_watch(dpy, w, counter, value)) {
                static int said;
                if (!said++) fprintf(stderr, "tod-xsync: conferma del ridisegno quando l'app disegna (finestra 0x%lx)\n", w);
            }
        }
    }
    return r;
}

/* diagnosi (TOD_XSYNC_DEBUG=1 nell'ambiente di xfwm4): gli allarmi SYNC che crea */
unsigned long XSyncCreateAlarm(void *dpy, unsigned long mask, void *attrs) {
    static unsigned long (*real)(void *, unsigned long, void *);
    if (!real) real = (unsigned long (*)(void *, unsigned long, void *))tod_next("XSyncCreateAlarm");
    unsigned long a = real ? real(dpy, mask, attrs) : 0;
    if (getenv("TOD_XSYNC_DEBUG")) fprintf(stderr, "tod-xsync: allarme creato %lu\n", a);
    return a;
}

/* ---- Dischi USB in Esplora file: niente doppioni sotto "Rete" ----
 * Ogni partizione della macchina dei dischi USB è montata da gvfs come sftp://<disco>/ e il volume
 * monitor di tod (share/usb/tod-volumi.py) la nasconde dietro il suo volume ("shadow mount"). Ma GIO
 * lo fa un attimo dopo l'avvio del programma: Thunar e i dialoghi Apri/Salva intanto la mettono
 * sotto "Rete" ("/ su emuelec") e non ricontrollano. Qui un montaggio sftp con la radice "/" su un
 * nome dell'elenco dei dischi ($TMPDIR/tod-usb/alias) è nascosto fin da subito. */
static int tod_disco_usb(const char *uri) {
    if (!uri || strncmp(uri, "sftp://", 7)) return 0;
    const char *host = uri + 7, *slash = strchr(host, '/');
    if (!slash || slash[1]) return 0;   /* solo la radice: le voci dei volumi restano */
    size_t n = (size_t)(slash - host);
    const char *at = memchr(host, '@', n);
    if (at) { n -= (size_t)(at + 1 - host); host = at + 1; }
    char path[512];
    const char *tmp = getenv("TMPDIR");
    snprintf(path, sizeof path, "%s/tod-usb/alias", tmp ? tmp : "/data/data/com.termux/files/usr/tmp");
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    char riga[256];
    int trovato = 0;
    while (!trovato && fgets(riga, sizeof riga, f)) {
        riga[strcspn(riga, "\r\n")] = 0;
        if (strlen(riga) == n && !strncasecmp(riga, host, n)) trovato = 1;
    }
    fclose(f);
    return trovato;
}

int g_mount_is_shadowed(void *mount) {
    static int (*real)(void *);
    if (!real) real = (int (*)(void *))tod_next("g_mount_is_shadowed");
    if (real && real(mount)) return 1;
    void *(*get_root)(void *) = (void *(*)(void *))dlsym(RTLD_DEFAULT, "g_mount_get_root");
    char *(*get_uri)(void *) = (char *(*)(void *))dlsym(RTLD_DEFAULT, "g_file_get_uri");
    void (*unref)(void *) = (void (*)(void *))dlsym(RTLD_DEFAULT, "g_object_unref");
    void (*gfree)(void *) = (void (*)(void *))dlsym(RTLD_DEFAULT, "g_free");
    if (!mount || !get_root || !get_uri || !unref || !gfree) return 0;
    /* il montaggio del volume (tod-volumi.py) ha la stessa radice ma resta visibile */
    const char *tipo = tod_type_name(mount);
    if (tipo && !strcmp(tipo, "GProxyShadowMount")) return 0;
    void *root = get_root(mount);
    if (!root) return 0;
    char *uri = get_uri(root);
    int nascosto = tod_disco_usb(uri);
    gfree(uri);
    unref(root);
    return nascosto;
}

/* La radice di un disco sftp per gvfs si chiama "/ on emuelec" (o "/ su emuelec"): nella barra del
 * percorso e nei titoli diventa il nome della partizione ("EMUELEC"), preso da $TMPDIR/tod-usb/volumi
 * (prima colonna; il nome del disco è quello in minuscolo con i trattini, come alias_of di tod-usb). */
static const char *tod_nome_disco(const char *host, size_t n) {
    char path[512];
    const char *tmp = getenv("TMPDIR");
    snprintf(path, sizeof path, "%s/tod-usb/volumi", tmp ? tmp : "/data/data/com.termux/files/usr/tmp");
    FILE *f = fopen(path, "r");
    if (!f) return NULL;
    static char nome[256];
    char riga[1024];
    const char *trovato = NULL;
    while (!trovato && fgets(riga, sizeof riga, f)) {
        size_t k = strcspn(riga, "\t\r\n");
        if (k == 0 || k != n || k >= sizeof nome) continue;
        size_t i;
        for (i = 0; i < k; i++) {
            char c = riga[i];
            c = c == ' ' ? '-' : c == '/' ? '_' : (char)tolower((unsigned char)c);
            if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-')) c = '-';   /* accenti */
            if (c != tolower((unsigned char)host[i])) break;
        }
        if (i == k) { memcpy(nome, riga, k); nome[k] = 0; trovato = nome; }
    }
    fclose(f);
    if (!trovato) return NULL;
    const char *(*intern)(const char *) = (const char *(*)(const char *))dlsym(RTLD_DEFAULT, "g_intern_string");
    return intern ? intern(trovato) : NULL;
}

const char *g_file_info_get_display_name(void *info) {
    static const char *(*real)(void *);
    if (!real) real = (const char *(*)(void *))tod_next("g_file_info_get_display_name");
    const char *nome = real ? real(info) : NULL;
    /* "/ on <disco>": "/", una parola (on, su, auf...), il nome del disco senza spazi */
    if (!nome || nome[0] != '/' || nome[1] != ' ') return nome;
    const char *host = strrchr(nome, ' ');
    if (!host || host == nome + 1 || !host[1] || strchr(nome + 2, ' ') != host) return nome;
    host++;
    const char *vero = tod_nome_disco(host, strlen(host));
    return vero ? vero : nome;
}

/* ---- Thunar: l'intestazione "Dispositivi" dopo aver riaperto la finestra ----
 * Chiusa l'ultima finestra, Thunar butta l'elenco della barra laterale (ThunarShortcutsModel) e lo
 * rifà alla riapertura: calcola le intestazioni subito, ma i dischi con la radice remota (i nostri
 * sftp) arrivano dopo, in modo asincrono, e l'intestazione resta nascosta. Quando l'elenco nasce
 * (registra il suo puntatore debole) gli si fa ricalcolare le intestazioni dopo 1, 3 e 6 secondi,
 * reimpostando "hidden-bookmarks" allo stesso valore (il suo set_property le ricalcola). */
static int tod_ricalcolo_attivo;

static int tod_ricalcola_intestazioni(void *model) {   /* ogni giro ha il suo riferimento */
    void *(*find)(void *, const char *) = (void *(*)(void *, const char *))dlsym(RTLD_DEFAULT, "g_object_class_find_property");
    const void *(*def)(void *) = (const void *(*)(void *))dlsym(RTLD_DEFAULT, "g_param_spec_get_default_value");
    void *(*init)(void *, unsigned long) = (void *(*)(void *, unsigned long))dlsym(RTLD_DEFAULT, "g_value_init");
    void (*get)(void *, const char *, void *) = (void (*)(void *, const char *, void *))dlsym(RTLD_DEFAULT, "g_object_get_property");
    void (*set)(void *, const char *, void *) = (void (*)(void *, const char *, void *))dlsym(RTLD_DEFAULT, "g_object_set_property");
    void (*unset)(void *) = (void (*)(void *))dlsym(RTLD_DEFAULT, "g_value_unset");
    void (*unref)(void *) = (void (*)(void *))dlsym(RTLD_DEFAULT, "g_object_unref");
    if (find && def && init && get && set && unset) {
        void *pspec = find(*(void **)model, "hidden-bookmarks");   /* la classe è il primo campo */
        const void *d = pspec ? def(pspec) : NULL;
        if (d) {
            unsigned long valore[3] = {0, 0, 0};   /* GValue: tipo + 2 parole */
            init(valore, *(const unsigned long *)d);
            get(model, "hidden-bookmarks", valore);
            tod_ricalcolo_attivo = 1;   /* righe inserite da qui non chiedono un altro giro */
            set(model, "hidden-bookmarks", valore);
            tod_ricalcolo_attivo = 0;
            unset(valore);
        }
    }
    if (unref) unref(model);
    return 0;   /* G_SOURCE_REMOVE */
}

static void tod_riga_inserita(void *model, void *path, void *iter, void *dati) {
    (void)path; (void)iter; (void)dati;
    if (tod_ricalcolo_attivo) return;
    /* non dentro il segnale (l'elenco sta cambiando): appena Thunar torna al ciclo, prima di disegnare */
    void *(*ref)(void *) = (void *(*)(void *))dlsym(RTLD_DEFAULT, "g_object_ref");
    unsigned (*idle)(int, int (*)(void *), void *, void *) =
        (unsigned (*)(int, int (*)(void *), void *, void *))dlsym(RTLD_DEFAULT, "g_idle_add_full");
    if (!ref || !idle) return;
    ref(model);
    idle(-100, tod_ricalcola_intestazioni, model, NULL);   /* G_PRIORITY_HIGH: prima del ridisegno */
}

void g_object_add_weak_pointer(void *object, void **location) {
    static void (*real)(void *, void **);
    if (!real) real = (void (*)(void *, void **))tod_next("g_object_add_weak_pointer");
    if (real) real(object, location);
    const char *tipo = object ? tod_type_name(object) : NULL;
    if (!tipo || strcmp(tipo, "ThunarShortcutsModel")) return;
    void *(*ref)(void *) = (void *(*)(void *))dlsym(RTLD_DEFAULT, "g_object_ref");
    unsigned (*timeout)(unsigned, int (*)(void *), void *) =
        (unsigned (*)(unsigned, int (*)(void *), void *))dlsym(RTLD_DEFAULT, "g_timeout_add");
    if (!ref || !timeout) return;
    /* a ogni riga inserita (il disco arrivato in ritardo) si ricalcola prima del disegno successivo;
     * i giri a tempo restano per sicurezza */
    unsigned long (*connect)(void *, const char *, void *, void *, void *, int) =
        (unsigned long (*)(void *, const char *, void *, void *, void *, int))dlsym(RTLD_DEFAULT, "g_signal_connect_data");
    if (connect) connect(object, "row-inserted", (void *)tod_riga_inserita, NULL, NULL, 0);
    ref(object); ref(object);   /* uno per giro: l'elenco resta vivo fino all'ultimo */
    timeout(1000, tod_ricalcola_intestazioni, object);
    timeout(4000, tod_ricalcola_intestazioni, object);
}

/* ------------------------------------------- cartelle del telefono: cestino e spostamenti
 * Download, Documenti, Immagini... della home sono le cartelle del telefono (/storage/emulated/0, un
 * FUSE di Android). GIO non ha un cestino lì ("Trashing on system internal mounts is not supported"):
 * Esplora file cancellava per sempre. E le vede su un altro disco: trascinare copiava invece di spostare.
 * Qui: g_file_trash sposta nel Cestino del desktop (con .trashinfo: si ripristina al suo posto), e in
 * Esplora file e scrivania i file del telefono sono "cestinabili" e sullo stesso disco della home. */
#include <sys/wait.h>   /* niente spawn.h: in Termux lo porta solo libandroid-spawn, che un Termux nuovo non ha
                          (1° novembre, S20+: modulo non compilato, desktop in inglese) */
#include <time.h>
extern char **environ;

/* il percorso è nel telefono? Si risolve la cartella che lo contiene, non il file: cestinare il
 * collegamento ~/Telefono non deve diventare "cestina tutto il telefono" */
static int tod_telefono(const char *path) {
    if (!path || path[0] != '/') return 0;
    const char *home = getenv("HOME");
    size_t hl = home ? strlen(home) : 0;
    if (strncmp(path, "/storage/", 9) && !(hl && !strncmp(path, home, hl))) return 0;   /* veloce: il resto no */
    const char *radice = getenv("TOD_PHONE_STORAGE");
    if (!radice || !*radice) radice = "/storage/emulated/0";
    char dir[PATH_MAX], vero[PATH_MAX];
    const char *barra = strrchr(path, '/');
    size_t n = (size_t)(barra - path);
    if (n == 0 || n >= sizeof dir) return 0;
    memcpy(dir, path, n);
    dir[n] = 0;
    if (!realpath(dir, vero)) return 0;
    size_t rl = strlen(radice);
    if (!strncmp(vero, radice, rl) && (vero[rl] == 0 || vero[rl] == '/')) return 1;
    return !strncmp(vero, "/storage/self/primary", 21) && (vero[21] == 0 || vero[21] == '/');
}

static void tod_errore(void **error, int codice, const char *testo) {
    void (*set)(void **, uint32_t, int, const char *) =
        (void (*)(void **, uint32_t, int, const char *))dlsym(RTLD_DEFAULT, "g_set_error_literal");
    uint32_t (*quark)(void) = (uint32_t (*)(void))dlsym(RTLD_DEFAULT, "g_io_error_quark");
    if (set && quark && error) set(error, quark(), codice, testo);
}

static int tod_mv(const char *da, const char *a) {   /* mv: anche cartelle, tra dischi diversi */
    char mv[PATH_MAX];
    const char *pre = getenv("PREFIX");
    snprintf(mv, sizeof mv, "%s/bin/mv", pre && *pre ? pre : "/data/data/com.termux/files/usr");
    char *argv[] = {"mv", "-f", "-T", "--", (char *)da, (char *)a, NULL};   /* -T: mai "dentro" a una cartella */
    int st;
    pid_t pid = fork();   /* fork + execve: ci sono su ogni Android (posix_spawn solo da Android 9) */
    if (pid < 0) return -1;
    if (pid == 0) {
        execve(mv, argv, environ);
        _exit(127);
    }
    while (waitpid(pid, &st, 0) < 0)
        if (errno != EINTR) return -1;
    return WIFEXITED(st) && WEXITSTATUS(st) == 0 ? 0 : -1;
}

int g_file_trash(void *file, void *cancellable, void **error) {
    static int (*real)(void *, void *, void **);
    if (!real) real = (int (*)(void *, void *, void **))tod_next("g_file_trash");
    char *(*get_path)(void *) = (char *(*)(void *))dlsym(RTLD_DEFAULT, "g_file_get_path");
    char *(*escape)(const char *, const char *, int) =
        (char *(*)(const char *, const char *, int))dlsym(RTLD_DEFAULT, "g_uri_escape_string");
    void (*gfree)(void *) = (void (*)(void *))dlsym(RTLD_DEFAULT, "g_free");
    char *path = get_path && file ? get_path(file) : NULL;
    if (!path || !escape || !gfree || !tod_telefono(path) || getenv("TOD_CESTINO_CLASSICO")) {
        if (gfree && path) gfree(path);
        if (!real) { tod_errore(error, 15 /* G_IO_ERROR_NOT_SUPPORTED */, "g_file_trash"); return 0; }
        return real(file, cancellable, error);
    }
    /* il Cestino del desktop (quello che apre Esplora file): $XDG_DATA_HOME/Trash */
    char cestino[PATH_MAX], info[PATH_MAX], dest[PATH_MAX], nome[NAME_MAX + 16];
    const char *xdg = getenv("XDG_DATA_HOME"), *home = getenv("HOME");
    if (xdg && *xdg) snprintf(cestino, sizeof cestino, "%s/Trash", xdg);
    else snprintf(cestino, sizeof cestino, "%s/.local/share/Trash", home ? home : "");
    snprintf(info, sizeof info, "%s/files", cestino);
    mkdir(cestino, 0700); mkdir(info, 0700);
    snprintf(info, sizeof info, "%s/info", cestino);
    mkdir(info, 0700);
    const char *base = strrchr(path, '/') + 1;
    int fd = -1;
    for (int i = 1; i < 1000 && fd < 0; i++) {
        if (i == 1) snprintf(nome, sizeof nome, "%s", base);
        else snprintf(nome, sizeof nome, "%s.%d", base, i);
        snprintf(info, sizeof info, "%s/info/%s.trashinfo", cestino, nome);
        snprintf(dest, sizeof dest, "%s/files/%s", cestino, nome);
        struct stat st;
        if (lstat(dest, &st) == 0) continue;
        fd = open(info, O_WRONLY | O_CREAT | O_EXCL, 0600);
        if (fd < 0 && errno != EEXIST) break;
    }
    if (fd < 0) {
        tod_errore(error, 0 /* G_IO_ERROR_FAILED */, "Impossibile scrivere nel Cestino");
        gfree(path);
        return 0;
    }
    char *esc = escape(path, "/", 1);
    char quando[32];
    time_t t = time(NULL);
    struct tm tm;
    strftime(quando, sizeof quando, "%Y-%m-%dT%H:%M:%S", localtime_r(&t, &tm));
    dprintf(fd, "[Trash Info]\nPath=%s\nDeletionDate=%s\n", esc ? esc : path, quando);
    close(fd);
    if (esc) gfree(esc);
    int ok = tod_mv(path, dest) == 0;
    if (!ok) {
        unlink(info);
        tod_errore(error, 0, "Impossibile spostare nel Cestino");
    }
    gfree(path);
    return ok;
}

/* Esplora file e scrivania: i file del telefono "cestinabili" (sennò il tasto Canc elimina per sempre)
 * e con il disco della home (sennò trascinarli dalla home o verso la home copia invece di spostare) */
static int tod_file_manager(void) {
    static int si = -1;
    if (si < 0) {
        const char *(*prg)(void) = (const char *(*)(void))dlsym(RTLD_DEFAULT, "g_get_prgname");
        const char *n = prg ? prg() : NULL;
        if (!n) return 0;   /* ancora presto: si riprova */
        si = !strcasecmp(n, "thunar") || !strcasecmp(n, "xfdesktop");
    }
    return si && !getenv("TOD_CESTINO_CLASSICO");
}

static void tod_ritocca(void *info, int telefono) {
    if (!info || !telefono) return;
    int (*has)(void *, const char *) = (int (*)(void *, const char *))dlsym(RTLD_DEFAULT, "g_file_info_has_attribute");
    void (*set_b)(void *, const char *, int) =
        (void (*)(void *, const char *, int))dlsym(RTLD_DEFAULT, "g_file_info_set_attribute_boolean");
    void (*set_s)(void *, const char *, const char *) =
        (void (*)(void *, const char *, const char *))dlsym(RTLD_DEFAULT, "g_file_info_set_attribute_string");
    if (!has || !set_b || !set_s) return;
    if (has(info, "access::can-trash")) set_b(info, "access::can-trash", 1);
    static char disco[32];
    if (!disco[0]) {
        struct stat st;
        const char *home = getenv("HOME");
        if (!home || stat(home, &st) != 0) return;
        snprintf(disco, sizeof disco, "l%llu", (unsigned long long)st.st_dev);   /* come GLib */
    }
    if (has(info, "id::filesystem")) set_s(info, "id::filesystem", disco);
}

static int tod_telefono_file(void *file) {
    char *(*get_path)(void *) = (char *(*)(void *))dlsym(RTLD_DEFAULT, "g_file_get_path");
    void (*gfree)(void *) = (void (*)(void *))dlsym(RTLD_DEFAULT, "g_free");
    if (!get_path || !gfree || !file) return 0;
    char *p = get_path(file);
    int r = tod_telefono(p);
    gfree(p);
    return r;
}

static int tod_telefono_cartella(void *enumeratore) {   /* i figli di una cartella del telefono */
    void *(*cont)(void *) = (void *(*)(void *))dlsym(RTLD_DEFAULT, "g_file_enumerator_get_container");
    char *(*get_path)(void *) = (char *(*)(void *))dlsym(RTLD_DEFAULT, "g_file_get_path");
    void (*gfree)(void *) = (void (*)(void *))dlsym(RTLD_DEFAULT, "g_free");
    if (!cont || !get_path || !gfree) return 0;
    char *p = get_path(cont(enumeratore));
    if (!p) return 0;
    size_t n = strlen(p);
    char *figlio = malloc(n + 3);
    int r = 0;
    if (figlio) {   /* tod_telefono guarda la cartella che contiene: qui è la cartella stessa */
        snprintf(figlio, n + 3, "%s/x", p);
        r = tod_telefono(figlio);
        free(figlio);
    }
    gfree(p);
    return r;
}

/* "Esplora la rete" di Esplora file (network://: il gvfs di Termux, senza Samba, la lascia vuota) apre la Rete di
 * tod, i PC e i NAS in /media/rete (share/rete, tod-fs.so). 8 novembre, Pietro: "su Esplora rete non c'è niente".
 * Solo se /media/rete c'è (tod-fs.so caricato): sennò resta quella di gvfs */
void *g_file_new_for_uri(const char *uri) {
    static void *(*real)(const char *), *(*per_percorso)(const char *);
    if (!real) real = (void *(*)(const char *))tod_next("g_file_new_for_uri");
    if (!real) return NULL;
    if (uri && !strncmp(uri, "network:", 8) && (!uri[8] || !strcmp(uri + 8, "//") || !strcmp(uri + 8, "///")) &&
        tod_file_manager() && access("/media/rete", F_OK) == 0) {
        if (!per_percorso) per_percorso = (void *(*)(const char *))tod_next("g_file_new_for_path");
        if (per_percorso) return per_percorso("/media/rete");
    }
    return real(uri);
}

void *g_file_query_info(void *file, const char *attributes, int flags, void *cancellable, void **error) {
    static void *(*real)(void *, const char *, int, void *, void **);
    if (!real) real = (void *(*)(void *, const char *, int, void *, void **))tod_next("g_file_query_info");
    if (!real) return NULL;
    void *info = real(file, attributes, flags, cancellable, error);
    if (info && tod_file_manager()) tod_ritocca(info, tod_telefono_file(file));
    return info;
}

void *g_file_query_info_finish(void *file, void *res, void **error) {
    static void *(*real)(void *, void *, void **);
    if (!real) real = (void *(*)(void *, void *, void **))tod_next("g_file_query_info_finish");
    if (!real) return NULL;
    void *info = real(file, res, error);
    if (info && tod_file_manager()) tod_ritocca(info, tod_telefono_file(file));
    return info;
}

void *g_file_enumerator_next_file(void *e, void *cancellable, void **error) {
    static void *(*real)(void *, void *, void **);
    if (!real) real = (void *(*)(void *, void *, void **))tod_next("g_file_enumerator_next_file");
    if (!real) return NULL;
    void *info = real(e, cancellable, error);
    if (info && tod_file_manager()) tod_ritocca(info, tod_telefono_cartella(e));
    return info;
}

TodList *g_file_enumerator_next_files_finish(void *e, void *res, void **error) {
    static TodList *(*real)(void *, void *, void **);
    if (!real) real = (TodList *(*)(void *, void *, void **))tod_next("g_file_enumerator_next_files_finish");
    if (!real) return NULL;
    TodList *l = real(e, res, error);
    if (l && tod_file_manager() && tod_telefono_cartella(e))
        for (TodList *i = l; i; i = i->next) tod_ritocca(i->data, 1);
    return l;
}

/* cartelle tra telefono e home (o dal Cestino al telefono): GIO non sposta cartelle tra dischi diversi
 * ("Can't recursively copy directory", G_IO_ERROR_WOULD_RECURSE): ripristinare una cartella cestinata
 * falliva. Solo in quel caso, e mai sopra qualcosa che c'è già, la sposta mv */
static int tod_dal_cestino(const char *uri, char *vero, size_t lv, char *info, size_t li) {
    if (!uri || strncmp(uri, "trash:///", 9) || !uri[9] || strchr(uri + 9, '/')) return 0;
    char *(*unescape)(const char *, const char *) =
        (char *(*)(const char *, const char *))dlsym(RTLD_DEFAULT, "g_uri_unescape_string");
    void (*gfree)(void *) = (void (*)(void *))dlsym(RTLD_DEFAULT, "g_free");
    if (!unescape || !gfree) return 0;
    char *nome = unescape(uri + 9, NULL);
    if (!nome) return 0;
    const char *xdg = getenv("XDG_DATA_HOME"), *home = getenv("HOME");
    char cestino[PATH_MAX];
    if (xdg && *xdg) snprintf(cestino, sizeof cestino, "%s/Trash", xdg);
    else snprintf(cestino, sizeof cestino, "%s/.local/share/Trash", home ? home : "");
    snprintf(vero, lv, "%s/files/%s", cestino, nome);
    snprintf(info, li, "%s/info/%s.trashinfo", cestino, nome);
    gfree(nome);
    struct stat st;
    return lstat(vero, &st) == 0;
}

int g_file_move(void *src, void *dst, int flags, void *cancellable, void *progress, void *dati, void **error) {
    static int (*real)(void *, void *, int, void *, void *, void *, void **);
    if (!real) real = (int (*)(void *, void *, int, void *, void *, void *, void **))tod_next("g_file_move");
    if (!real) { tod_errore(error, 15, "g_file_move"); return 0; }
    void *err = NULL;
    if (real(src, dst, flags, cancellable, progress, dati, &err)) return 1;
    int (*matches)(void *, uint32_t, int) = (int (*)(void *, uint32_t, int))dlsym(RTLD_DEFAULT, "g_error_matches");
    uint32_t (*quark)(void) = (uint32_t (*)(void))dlsym(RTLD_DEFAULT, "g_io_error_quark");
    void (*efree)(void *) = (void (*)(void *))dlsym(RTLD_DEFAULT, "g_error_free");
    char *(*get_path)(void *) = (char *(*)(void *))dlsym(RTLD_DEFAULT, "g_file_get_path");
    char *(*get_uri)(void *) = (char *(*)(void *))dlsym(RTLD_DEFAULT, "g_file_get_uri");
    void (*gfree)(void *) = (void (*)(void *))dlsym(RTLD_DEFAULT, "g_free");
    int riprova = err && matches && quark && efree && get_path && get_uri && gfree && matches(err, quark(), 25)
                  && !getenv("TOD_CESTINO_CLASSICO");
    char *da = riprova ? get_path(src) : NULL, *a = riprova ? get_path(dst) : NULL, *uri = NULL;
    char vero[PATH_MAX], info[PATH_MAX];
    int dal_cestino = 0, ok = 0;
    info[0] = 0;
    if (riprova && !da && (uri = get_uri(src)) && tod_dal_cestino(uri, vero, sizeof vero, info, sizeof info))
        dal_cestino = 1;
    const char *sorgente = dal_cestino ? vero : da;
    struct stat st;
    if (sorgente && a && (dal_cestino || tod_telefono(sorgente) || tod_telefono(a)) && lstat(a, &st) != 0
        && errno == ENOENT && tod_mv(sorgente, a) == 0) {
        ok = 1;
        if (dal_cestino) unlink(info);
    }
    if (da) gfree(da);
    if (a) gfree(a);
    if (uri) gfree(uri);
    if (ok) { efree(err); return 1; }
    if (error) *error = err; else if (err && efree) efree(err);
    return 0;
}

/* scrivania: niente "Elimina" (per sempre) nel tasto destro, resta "Sposta nel cestino" (Pietro).
 * xfdesktop crea la voce con xfce_gtk_image_menu_item_new (senza menu) e la aggiunge da sé con
 * gtk_menu_shell_append: la voce si segna alla creazione e all'aggiunta si salta. Resta viva (xfdesktop
 * può ancora toccarla), solo fuori dal menu */
static int tod_scrivania(void) {
    static int si = -1;
    if (si < 0) {
        const char *(*prg)(void) = (const char *(*)(void))dlsym(RTLD_DEFAULT, "g_get_prgname");
        const char *n = prg ? prg() : NULL;
        if (!n) return 0;
        si = !strcasecmp(n, "xfdesktop") && !getenv("TOD_CESTINO_CLASSICO");
    }
    return si;
}

void *xfce_gtk_image_menu_item_new(const char *label, const char *tooltip, const char *accel_path, void *callback,
                                   void *callback_param, void *image, void *menu) {
    static void *(*real)(const char *, const char *, const char *, void *, void *, void *, void *);
    if (!real) real = (void *(*)(const char *, const char *, const char *, void *, void *, void *, void *))
                      tod_next("xfce_gtk_image_menu_item_new");
    if (!real) return NULL;
    int via = accel_path && !strcmp(accel_path, "<Actions>/XfdesktopFileIconManager/delete") && tod_scrivania();
    void *w = real(label, tooltip, accel_path, callback, callback_param, image, via ? NULL : menu);
    if (via && w) {
        void (*set_data)(void *, const char *, void *) = (void (*)(void *, const char *, void *))dlsym(RTLD_DEFAULT, "g_object_set_data");
        void (*no_show)(void *, int) = (void (*)(void *, int))dlsym(RTLD_DEFAULT, "gtk_widget_set_no_show_all");
        if (set_data) set_data(w, "tod-fuori-dal-menu", (void *)1);
        if (no_show) no_show(w, 1);
    }
    return w;
}

void gtk_menu_shell_append(void *shell, void *child) {
    static void (*real)(void *, void *);
    if (!real) real = (void (*)(void *, void *))tod_next("gtk_menu_shell_append");
    if (!real) return;
    if (child && tod_scrivania()) {
        void *(*get_data)(void *, const char *) = (void *(*)(void *, const char *))dlsym(RTLD_DEFAULT, "g_object_get_data");
        if (get_data && get_data(child, "tod-fuori-dal-menu")) return;
    }
    real(shell, child);
}

/* --- Anteprima con la barra spaziatrice, come Quick Look di macOS (10 novembre) ----------------------------------
 * Pietro: "se da un file dal desktop o da Thunar premo la barra spaziatrice come sul mac li vedo in anteprima".
 * In Thunar e in xfdesktop ogni finestra principale, appena mappata, riceve un gestore dei tasti (aggancio al segnale
 * "map", messo prima di g_application_run). Lo spazio senza modificatori, con il fuoco sulla vista dei file (non su
 * una casella di testo né su un nome in rinomina), lancia `tod-anteprima` (share/bin, primo nel PATH della sessione;
 * la finestra è share/media/tod-anteprima.py) con i file scelti, e alla vista non arriva. Con niente di scelto lo
 * spazio fa quello di sempre. Thunar: la proprietà "selected-files" della vista (ThunarStandardView) e
 * thunarx_file_info_get_location; scrivania: xfdesktop_icon_view_get_selected_items e il file di ogni icona.
 * TOD_ANTEPRIMA_SPENTA=1 lo spegne. */
#define TOD_AP_MAX 500       /* file scelti */
#define TOD_AP_TUTTI 20000   /* file della vista (le frecce con un file solo scelto) */

typedef struct { tod_gtype g_type; long long dati[2]; } TodValue;   /* GValue */

static int tod_ap_chi(void) {   /* 1 = Esplora file (Thunar), 2 = scrivania (xfdesktop), 0 = un altro programma */
    static int chi = -1;
    if (chi < 0) {
        const char *n = getprogname();
        chi = !n || getenv("TOD_ANTEPRIMA_SPENTA") ? 0 : !strcasecmp(n, "thunar") ? 1 : !strcasecmp(n, "xfdesktop") ? 2 : 0;
    }
    return chi;
}

static int tod_ap_e_un(void *obj, const char *tipo) {   /* obj è di quel tipo (classe, sottoclasse o interfaccia) */
    GTKFN(tod_gtype, g_type_from_name, const char *)
    GTKFN(int, g_type_check_instance_is_a, void *, tod_gtype)
    tod_gtype t = obj ? f_g_type_from_name(tipo) : 0;
    return t && f_g_type_check_instance_is_a(obj, t);
}

/* Esplora file: i file scelti nella vista che contiene il widget col fuoco */
static int tod_ap_thunar(void *vista, char **file, int max) {
    GTKFN(void *, gtk_widget_get_parent, void *)
    GTKFN(void *, g_object_class_find_property, void *, const char *)
    GTKFN(void *, g_value_init, TodValue *, tod_gtype)
    GTKFN(void, g_object_get_property, void *, const char *, TodValue *)
    GTKFN(void *, g_value_get_boxed, const TodValue *)
    GTKFN(void *, g_value_get_pointer, const TodValue *)
    GTKFN(void, g_value_unset, TodValue *)
    GTKFN(tod_gtype, g_type_fundamental, tod_gtype)
    GTKFN(void *, thunarx_file_info_get_location, void *)
    GTKFN(char *, g_file_get_path, void *)
    GTKFN(void, g_object_unref, void *)
    void *sv = vista;
    while (sv && !tod_ap_e_un(sv, "ThunarStandardView")) sv = f_gtk_widget_get_parent(sv);
    if (!sv) return 0;   /* la barra laterale o un'altra vista ad albero: non i file della cartella */
    /* GParamSpec: istanza (classe), nome, opzioni, tipo del valore */
    struct { void *g_class; const char *name; int flags; tod_gtype value_type; } *ps =
        f_g_object_class_find_property(*(void **)sv, "selected-files");
    if (!ps) return 0;
    TodValue v = {0};
    f_g_value_init(&v, ps->value_type);
    f_g_object_get_property(sv, "selected-files", &v);   /* una lista di ThunarxFileInfo (copia, la libera unset) */
    tod_gtype fond = f_g_type_fundamental(ps->value_type);
    TodList *sel = fond == (18 << 2) ? f_g_value_get_boxed(&v) : fond == (17 << 2) ? f_g_value_get_pointer(&v) : NULL;
    int n = 0;
    for (TodList *l = sel; l && n < max; l = l->next) {
        void *gf = f_thunarx_file_info_get_location(l->data);
        if (!gf) continue;
        char *p = f_g_file_get_path(gf);   /* NULL fuori dai file locali (Cestino, ricerche...) */
        f_g_object_unref(gf);
        if (p) file[n++] = p;
    }
    f_g_value_unset(&v);
    return n;
}

/* Esplora file, un file solo scelto: tutti quelli della vista, nell'ordine in cui si vedono (nome, data... anche al
 * contrario; i nascosti solo se si vedono), e il posto di quello scelto: le frecce dell'anteprima li sfogliano così */
static int tod_ap_thunar_vista(void *vista, const char *scelto, char **file, int max, int *posto) {
    GTKFN(void *, exo_icon_view_get_model, void *)
    GTKFN(void *, gtk_tree_view_get_model, void *)
    GTKFN(int, gtk_tree_model_get_n_columns, void *)
    GTKFN(tod_gtype, gtk_tree_model_get_column_type, void *, int)
    GTKFN(int, gtk_tree_model_get_iter_first, void *, void *)
    GTKFN(int, gtk_tree_model_iter_next, void *, void *)
    GTKFN(void, gtk_tree_model_get, void *, void *, ...)
    GTKFN(const char *, g_type_name, tod_gtype)
    GTKFN(void *, thunarx_file_info_get_location, void *)
    GTKFN(char *, g_file_get_path, void *)
    GTKFN(void, g_object_unref, void *)
    void *model = tod_ap_e_un(vista, "ExoIconView") ? f_exo_icon_view_get_model(vista) : f_gtk_tree_view_get_model(vista);
    int col = -1, nc = model ? f_gtk_tree_model_get_n_columns(model) : 0;
    for (int c = 0; c < nc && col < 0; c++) {
        const char *tn = f_g_type_name(f_gtk_tree_model_get_column_type(model, c));
        if (tn && !strcmp(tn, "ThunarFile")) col = c;
    }
    long iter[4] = {0};
    int n = 0;
    *posto = -1;
    if (col < 0 || !f_gtk_tree_model_get_iter_first(model, iter)) return 0;
    do {
        void *f = NULL;
        f_gtk_tree_model_get(model, iter, col, &f, -1);   /* un riferimento in più */
        if (!f) continue;
        void *gf = f_thunarx_file_info_get_location(f);
        f_g_object_unref(f);
        char *p = gf ? f_g_file_get_path(gf) : NULL;
        if (gf) f_g_object_unref(gf);
        if (!p) continue;
        if (*posto < 0 && !strcmp(p, scelto)) *posto = n;
        file[n++] = p;
    } while (n < max && f_gtk_tree_model_iter_next(model, iter));
    return n;
}

/* scrivania: le icone scelte; il modello è il filtro di xfdesktop 4.20 (o il modello stesso) */
static int tod_ap_scrivania(void *vista, char **file, int max) {
    GTKFN(TodList *, xfdesktop_icon_view_get_selected_items, void *)
    GTKFN(void *, xfdesktop_icon_view_get_model, void *)
    GTKFN(int, gtk_tree_model_get_iter, void *, void *, void *)
    GTKFN(void *, xfdesktop_file_icon_model_filter_get_icon, void *, void *)
    GTKFN(void *, xfdesktop_file_icon_model_get_icon, void *, void *)
    GTKFN(void *, xfdesktop_file_icon_peek_file, void *)
    GTKFN(char *, g_file_get_path, void *)
    GTKFN(void, gtk_tree_path_free, void *)
    GTKFN(void, g_list_free, TodList *)
    void *model = f_xfdesktop_icon_view_get_model(vista);
    int filtro = tod_ap_e_un(model, "XfdesktopFileIconModelFilter");
    if (!filtro && !tod_ap_e_un(model, "XfdesktopFileIconModel")) return 0;
    TodList *sel = f_xfdesktop_icon_view_get_selected_items(vista);
    int n = 0;
    for (TodList *l = sel; l; l = l->next) {
        long iter[4] = {0};   /* GtkTreeIter */
        if (n < max && f_gtk_tree_model_get_iter(model, iter, l->data)) {
            void *icona = filtro ? f_xfdesktop_file_icon_model_filter_get_icon(model, iter)
                                 : f_xfdesktop_file_icon_model_get_icon(model, iter);   /* senza riferimento in più */
            void *gf = tod_ap_e_un(icona, "XfdesktopFileIcon") ? f_xfdesktop_file_icon_peek_file(icona) : NULL;
            char *p = gf ? f_g_file_get_path(gf) : NULL;   /* il Cestino (trash:///) non ha un percorso */
            if (p) file[n++] = p;
        }
        f_gtk_tree_path_free(l->data);
    }
    f_g_list_free(sel);
    return n;
}

static int tod_ap_tasto(void *finestra, void *ev, void *dati) {
    (void)dati;
    GTKFN(int, gdk_event_get_keyval, void *, unsigned *)
    GTKFN(int, gdk_event_get_state, void *, unsigned *)
    GTKFN(void *, gtk_window_get_focus, void *)
    GTKFN(int, g_spawn_async, const char *, char **, char **, int, void *, void *, int *, void **)
    GTKFN(void, g_free, void *)
    unsigned kv = 0, st = 0;
    if (!f_gdk_event_get_keyval(ev, &kv) || kv != 0x20) return 0;   /* GDK_KEY_space */
    f_gdk_event_get_state(ev, &st);
    if (st & (1u | 4u | 8u | (1u << 26) | (1u << 28))) return 0;   /* Maiusc, Ctrl, Alt, Super, Meta */
    void *fuoco = f_gtk_window_get_focus(finestra);
    if (!fuoco || tod_ap_e_un(fuoco, "GtkEditable") || tod_ap_e_un(fuoco, "GtkTextView")) return 0;
    char **file = calloc(TOD_AP_TUTTI + 4, sizeof *file), num[16];
    int n = 0, da = 1;   /* file[0] il comando, poi i file (o "--indice", il numero e tutta la vista) */
    if (!file) return 0;
    if (tod_ap_chi() == 2 && tod_ap_e_un(fuoco, "XfdesktopIconView"))
        n = tod_ap_scrivania(fuoco, file + 1, TOD_AP_MAX);
    else if (tod_ap_chi() == 1 && (tod_ap_e_un(fuoco, "ExoIconView") || tod_ap_e_un(fuoco, "GtkTreeView")))
        n = tod_ap_thunar(fuoco, file + 1, TOD_AP_MAX);
    if (n == 1 && tod_ap_chi() == 1) {
        int posto = -1, m = tod_ap_thunar_vista(fuoco, file[1], file + 3, TOD_AP_TUTTI, &posto);
        if (m > 1 && posto >= 0) {
            f_g_free(file[1]);
            snprintf(num, sizeof num, "%d", posto);
            file[1] = "--indice";
            file[2] = num;
            da = 3;
            n = m;
        } else {
            for (int i = 0; i < m; i++) f_g_free(file[3 + i]);
            file[3] = NULL;
        }
    }
    if (n > 0) {
        file[0] = "tod-anteprima";
        file[da + n] = NULL;
        struct { unsigned dominio; int codice; char *testo; } *errore = NULL;   /* GError */
        int ok = f_g_spawn_async(NULL, file, NULL, (1 << 2) | (1 << 3), NULL, NULL, NULL, (void **)&errore);   /* PATH */
        fprintf(stderr, "tod-anteprima: %d file%s%s\n", n, ok ? "" : ", non partita: ", !ok && errore ? errore->testo : "");
        for (int i = da; i < da + n; i++) f_g_free(file[i]);
    }
    free(file);
    return n > 0;
}

static int tod_ap_mappa(void *suggerimento, unsigned n, const TodValue *valori, void *dati) {
    (void)suggerimento; (void)dati;
    GTKFN(void *, g_value_get_object, const TodValue *)
    GTKFN(void *, g_object_get_data, void *, const char *)
    GTKFN(void, g_object_set_data, void *, const char *, void *)
    GTKFN(int, gtk_window_get_window_type, void *)
    GTKFN(unsigned long, g_signal_connect_data, void *, const char *, void *, void *, void *, int)
    void *w = n ? f_g_value_get_object(valori) : NULL;
    if (tod_ap_e_un(w, "GtkWindow") && f_gtk_window_get_window_type(w) == 0 && !f_g_object_get_data(w, "tod-anteprima")) {
        f_g_object_set_data(w, "tod-anteprima", (void *)1);
        f_g_signal_connect_data(w, "key-press-event", (void *)tod_ap_tasto, NULL, NULL, 0);
    }
    return 1;   /* l'aggancio resta */
}

static int tod_ap_aggancia(void) {
    GTKFN(tod_gtype, gtk_widget_get_type, void)
    GTKFN(void *, g_type_class_ref, tod_gtype)
    GTKFN(unsigned, g_signal_lookup, const char *, tod_gtype)
    GTKFN(unsigned long, g_signal_add_emission_hook, unsigned, unsigned, void *, void *, void *)
    static int fatto;
    if (fatto || !tod_ap_chi()) return 0;
    fatto = 1;
    tod_gtype t = f_gtk_widget_get_type();
    f_g_type_class_ref(t);   /* i segnali di GtkWidget esistono solo con la classe pronta */
    unsigned id = f_g_signal_lookup("map", t);
    if (id) f_g_signal_add_emission_hook(id, 0, (void *)tod_ap_mappa, NULL, NULL);
    fprintf(stderr, "tod-anteprima: barra spaziatrice pronta\n");
    return 1;
}

int g_application_run(void *app, int argc, char **argv) {
    static int (*real)(void *, int, char **);
    if (!real) real = (int (*)(void *, int, char **))tod_next("g_application_run");
    if (!real) return 1;
    tod_ap_aggancia();
    return real(app, argc, argv);
}
