// tod-nolink.so — Android vieta i collegamenti fisici alle app (SELinux: linkat → EACCES).
// OSTree (Flatpak) li usa in due modi: file temporanei O_TMPFILE poi "collegati" al nome finale, e
// checkout per collegamento. Qui: O_TMPFILE → EOPNOTSUPP (libglnx ripiega su file temporaneo + rename)
// e un linkat rifiutato → EXDEV (OSTree ripiega sulla copia).
typedef unsigned long size_t;
#define RTLD_NEXT ((void *)-1l)
#define TMPFILE 020000000
#define EACCES 13
#define EPERM 1
#define EXDEV 18
#define EOPNOTSUPP 95
void *dlsym(void *, const char *);
int *__errno_location(void);
typedef unsigned int mode_t;

static int no_tmp(int flags) { if ((flags & TMPFILE) == TMPFILE) { *__errno_location() = EOPNOTSUPP; return 1; } return 0; }

#define OPEN(nome) \
  int nome(const char *p, int f, mode_t m) { static int (*r)(const char *, int, mode_t); \
    if (no_tmp(f)) return -1; if (!r) r = dlsym(RTLD_NEXT, #nome); return r(p, f, m); }
#define OPENAT(nome) \
  int nome(int d, const char *p, int f, mode_t m) { static int (*r)(int, const char *, int, mode_t); \
    if (no_tmp(f)) return -1; if (!r) r = dlsym(RTLD_NEXT, #nome); return r(d, p, f, m); }
OPEN(open) OPEN(open64) OPENAT(openat) OPENAT(openat64)
int __open_2(const char *p, int f) { return open(p, f, 0); }
int __open64_2(const char *p, int f) { return open64(p, f, 0); }
int __openat_2(int d, const char *p, int f) { return openat(d, p, f, 0); }
int __openat64_2(int d, const char *p, int f) { return openat64(d, p, f, 0); }

static int fisso(int r) { if (r < 0 && (*__errno_location() == EACCES || *__errno_location() == EPERM)) *__errno_location() = EXDEV; return r; }
int linkat(int a, const char *b, int c, const char *d, int e) {
  static int (*r)(int, const char *, int, const char *, int);
  if (!r) r = dlsym(RTLD_NEXT, "linkat");
  return fisso(r(a, b, c, d, e));
}
int link(const char *a, const char *b) {
  static int (*r)(const char *, const char *);
  if (!r) r = dlsym(RTLD_NEXT, "link");
  return fisso(r(a, b));
}
