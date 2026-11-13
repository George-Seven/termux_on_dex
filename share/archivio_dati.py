"""archivio_dati.py — quanto spazio occupa cosa, per Impostazioni → Archiviazione (share/tod-settings.py). Senza grafica.

13 novembre 2026, Pietro: "ho 120 GB di roba su Termux, che cosa abbiamo combinato?" (era un registro di prova da
62 GB in $PREFIX/tmp) → "fammi un'app tipo macOS che mi fa vedere la memoria occupata", poi "lo volevo nelle
impostazioni": come macOS, Impostazioni → Generali → Archiviazione.

Misura con `du` (molto più veloce di un giro in Python): tre misure in parallelo (casa di Termux, $PREFIX, il container)
e poi le categorie per sottrazione, così niente è contato due volte. "Android e altre app" è quello che resta dello
spazio occupato sul telefono (statvfs): foto, app del telefono, sistema.
"""
import json
import os
import subprocess
import threading
import time

CASA = os.environ.get("TOD_TERMUX_HOME") or os.environ.get("HOME", "/data/data/com.termux/files/home")
PREFIX = os.environ.get("PREFIX") or os.path.join(os.path.dirname(CASA), "usr")


def rootfs_predefinito():
    for r in (os.path.join(PREFIX, "var/lib/proot-distro/containers/debian/rootfs"),
              os.path.join(PREFIX, "var/lib/proot-distro/installed-rootfs/debian")):
        if os.path.isdir(r):
            return r
    return os.path.join(PREFIX, "var/lib/proot-distro/containers/debian/rootfs")


ROOTFS = os.environ.get("TOD_CX_ROOTFS") or rootfs_predefinito()
CACHE = os.path.join(CASA, ".cache/tod/archivio.json")

# cartelle della casa che sono lavoro di sviluppo (sorgenti, compilazioni, strumenti): si possono rifare
SVILUPPO = ("src", "lkl", ".cargo", ".rustup", ".npm", ".cpan", "go", ".gradle", ".m2", "tod-istantanee")
# le categorie, nell'ordine della barra (come macOS: prima le grandi "di sistema", poi i dati), con il colore
CATEGORIE = [
    ("android", "Android e altre app", "Android and other apps", "#8e8e93", "phone"),
    ("termux", "Sistema del desktop", "Desktop system", "#5e5ce6", "applications-system"),
    ("debian", "App Linux (Debian)", "Linux apps (Debian)", "#ff9f0a", "package-x-generic"),
    ("store", "App dallo Store", "Apps from the Store", "#0a84ff", "system-software-install"),
    ("dati", "Dati delle app", "App data", "#30b0c7", "folder-documents"),
    ("documenti", "Documenti e file", "Documents and files", "#34c759", "folder"),
    ("sviluppo", "Sviluppo e prove", "Development and tests", "#bf5af2", "applications-engineering"),
    ("cache", "Cache", "Cache", "#ffcc00", "edit-clear"),
    ("temporanei", "File temporanei", "Temporary files", "#ff453a", "document-open-recent"),
    ("cestino", "Cestino", "Trash", "#a2845e", "user-trash"),
]
GRANDE_TEMPORANEO = 50 * 1000 * 1000   # un file temporaneo da qui in su diventa un consiglio


def du(percorso, profondita, comando="du"):
    """{cartella: byte} per tutte le cartelle fino a quella profondità (du -x: si resta su quel disco)."""
    if not os.path.isdir(percorso):
        return {}
    try:
        r = subprocess.run([comando, "-x", "-k", "-d", str(profondita), percorso], capture_output=True, text=True,
                           errors="replace")
    except OSError:
        return {}
    return leggi_du(r.stdout)


def leggi_du(testo):
    out = {}
    for riga in testo.splitlines():
        kb, _, p = riga.partition("\t")
        if kb.strip().isdigit() and p:
            out[p.rstrip("/") or "/"] = int(kb) * 1024
    return out


def figli(m, p):
    """le cartelle subito dentro p, dalla misura m, dalla più grande"""
    p = p.rstrip("/")
    return sorted(((q, b) for q, b in m.items() if os.path.dirname(q) == p), key=lambda x: -x[1])


def disco(percorso=CASA):
    """(totale, usato, libero) in byte del disco dei dati del telefono"""
    try:
        s = os.statvfs(percorso)
    except OSError:
        return 0, 0, 0
    tot = s.f_blocks * s.f_frsize
    libero = s.f_bavail * s.f_frsize
    return tot, tot - s.f_bfree * s.f_frsize, libero


def case_del_container(rootfs=ROOTFS):
    h = os.path.join(rootfs, "home")
    try:
        return sorted(os.path.join(h, n) for n in os.listdir(h) if os.path.isdir(os.path.join(h, n))
                      and not os.path.islink(os.path.join(h, n)))
    except OSError:
        return []


def calcola(a, b, c, d=None, spazio=(0, 0, 0), casa=CASA, prefix=PREFIX, rootfs=ROOTFS, altri=None, case_cx=None):
    """le categorie dalle misure: a = casa (du -d3), b = $PREFIX (du -d3), c = container (du -d3),
    d = cestino (du -d1 della sua cartella files), altri = {cartella accanto a casa e usr: byte}.
    Restituisce {chiave: {"byte": n, "parti": [(percorso, byte), ...]}} più "totale", "usato", "libero"."""
    d = d or {}
    g = lambda m, p: m.get(p.rstrip("/"), 0)   # noqa: E731
    j = lambda *x: os.path.join(*x)            # noqa: E731
    case_cx = case_del_container(rootfs) if case_cx is None else case_cx
    cat = {k: {"byte": 0, "parti": []} for k, *_ in CATEGORIE}

    def metti(k, percorso, byte):
        if byte > 0:
            cat[k]["byte"] += byte
            cat[k]["parti"].append((percorso, byte))

    # --- casa di Termux
    cestino = j(casa, ".local/share/Trash")
    store = [j(casa, ".local/share/tod-store"), j(casa, ".local/share/flatpak")]
    metti("cestino", cestino, g(a, cestino))
    for s in store:
        metti("store", s, g(a, s))
    for q, n in figli(a, j(casa, ".cache")):
        metti("cache", q, n)
    resto_cache = g(a, j(casa, ".cache")) - sum(n for _q, n in figli(a, j(casa, ".cache")))
    metti("cache", j(casa, ".cache"), resto_cache)
    for s in SVILUPPO:
        sotto = figli(a, j(casa, s))
        for q, n in sotto:
            metti("sviluppo", q, n)
        metti("sviluppo", j(casa, s), g(a, j(casa, s)) - sum(n for _q, n in sotto))
    for q, n in figli(a, j(casa, ".config")):
        metti("dati", q, n)
    metti("dati", j(casa, ".config"), g(a, j(casa, ".config")) - sum(n for _q, n in figli(a, j(casa, ".config"))))
    fuori = {cestino, *store}
    locale = j(casa, ".local")
    for q, n in figli(a, locale):
        if q == j(locale, "share"):
            for qq, nn in figli(a, q):
                if qq not in fuori:
                    metti("dati", qq, nn)
            metti("dati", q, g(a, q) - sum(nn for _qq, nn in figli(a, q)))
        else:
            metti("dati", q, n)
    metti("dati", locale, g(a, locale) - sum(n for _q, n in figli(a, locale)))
    gia = {j(casa, ".cache"), j(casa, ".config"), locale, *(j(casa, s) for s in SVILUPPO)}
    for q, n in figli(a, casa):
        if q not in gia:
            metti("documenti", q, n)
    metti("documenti", casa, g(a, casa) - sum(n for _q, n in figli(a, casa)))   # i file sciolti nella casa
    for q, n in (altri or {}).items():
        metti("documenti", q, n)

    # --- $PREFIX: sistema del desktop, file temporanei; il container a parte
    tmp = j(prefix, "tmp")
    pd = j(prefix, "var/lib/proot-distro")
    for q, n in figli(b, tmp):
        metti("temporanei", q, n)
    metti("temporanei", tmp, g(b, tmp) - sum(n for _q, n in figli(b, tmp)))   # i file sciolti (il registro da 62 GB)
    for q, n in figli(b, prefix):
        if q == tmp:
            continue
        if q == j(prefix, "var"):
            n -= g(b, pd)
        metti("termux", q, n)
    metti("termux", prefix, g(b, prefix) - sum(n for _q, n in figli(b, prefix)))

    # --- il container: le app Debian; la casa dell'utente dentro sono dati (e cache), /tmp temporanei
    cx_tmp = j(rootfs, "tmp")
    for h in case_cx:
        hc = j(h, ".cache")
        metti("cache", hc, g(c, hc))
        for q, n in figli(c, h):
            if q != hc:
                metti("dati", q, n)
        metti("dati", h, g(c, h) - sum(n for _q, n in figli(c, h)))
    metti("temporanei", cx_tmp, g(c, cx_tmp))
    togli = {cx_tmp, j(rootfs, "home")}
    for q, n in figli(c, rootfs):
        if q not in togli:
            metti("debian", q, n)
    metti("debian", j(rootfs, "home"), g(c, j(rootfs, "home")) - sum(g(c, h) for h in case_cx))
    metti("debian", rootfs, g(c, rootfs) - sum(n for _q, n in figli(c, rootfs)))
    metti("debian", pd, g(b, pd) - g(c, rootfs))   # altri container o resti di proot-distro

    # il Cestino in dettaglio: i file buttati, non la cartella interna
    file_cestino = [(q, n) for q, n in figli(d, j(cestino, "files"))]
    if file_cestino:
        cat["cestino"]["parti"] = file_cestino + [(j(cestino, "info"), max(0, g(a, cestino) - sum(n for _q, n in file_cestino)))]

    tot, usato, libero = spazio
    nostro = sum(v["byte"] for v in cat.values())
    if usato:
        metti("android", "", max(0, usato - nostro))
    for v in cat.values():
        v["parti"] = sorted((p for p in v["parti"] if p[1] > 0), key=lambda x: -x[1])
    return {"categorie": cat, "totale": tot, "usato": usato, "libero": libero, "nostro": nostro}


def aperti(proc="/proc"):
    """i file aperti da qualche processo (per non proporre di cancellare un registro ancora in uso)"""
    out = set()
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return out
    for p in pids:
        fd = os.path.join(proc, p, "fd")
        try:
            for f in os.listdir(fd):
                try:
                    out.add(os.readlink(os.path.join(fd, f)))
                except OSError:
                    pass
        except OSError:
            pass
    return out


def temporanei_grandi(cartelle, soglia=GRANDE_TEMPORANEO, in_uso=None):
    """[(percorso, byte, aperto)] dei file sciolti grandi nelle cartelle dei temporanei, dal più grande"""
    in_uso = aperti() if in_uso is None else in_uso
    out = []
    for c in cartelle:
        try:
            voci = list(os.scandir(c))
        except OSError:
            continue
        for e in voci:
            try:
                if e.is_file(follow_symlinks=False):
                    n = e.stat(follow_symlinks=False).st_blocks * 512
                    if n >= soglia:
                        out.append((e.path, n, e.path in in_uso))
            except OSError:
                pass
    return sorted(out, key=lambda x: -x[1])


def in_esecuzione(proc="/proc"):
    """il testo delle righe di comando dei processi vivi (per non pulire la cache di un'app aperta)"""
    out = []
    try:
        for p in os.listdir(proc):
            if p.isdigit():
                try:
                    out.append(open(os.path.join(proc, p, "cmdline"), "rb").read().replace(b"\0", b" ").decode(
                        errors="replace").lower())
                except OSError:
                    pass
    except OSError:
        pass
    return out


def cache_pulibili(dati, comandi=None):
    """le cartelle della cache di Termux che si possono svuotare: tutte tranne la nostra (tod: elenchi che costa
    rifare) e quelle di un programma aperto (chromium mentre Chromium gira)"""
    comandi = in_esecuzione() if comandi is None else comandi
    cache = os.path.join(CASA, ".cache")
    out = []
    for p, n in dati["categorie"]["cache"]["parti"]:
        if os.path.dirname(p) != cache:
            continue
        nome = os.path.basename(p)
        if nome == "tod" or any(nome.lower() in c for c in comandi):
            continue
        out.append((p, n))
    return out


def consigli(dati, temporanei=None, pulibili=None):
    """come "Consigli" di macOS: [(chiave, byte, dettaglio)] solo quando servono"""
    c = dati["categorie"]
    out = []
    temporanei = [] if temporanei is None else temporanei
    liberi = [t for t in temporanei if not t[2]]
    if liberi:
        out.append(("temporanei", sum(t[1] for t in liberi), liberi))
    if c["cestino"]["byte"] >= 50 * 1000 * 1000:
        out.append(("cestino", c["cestino"]["byte"], None))
    pulibili = [] if pulibili is None else pulibili
    tot = sum(n for _p, n in pulibili)
    if tot >= 100 * 1000 * 1000:
        out.append(("cache", tot, pulibili))
    return out


def analizza(avanzamento=None):
    """tutte le misure (in parallelo) → il risultato di calcola, salvato in CACHE"""
    misure = {}

    def misura(k, *arg):
        misure[k] = du(*arg)
        if avanzamento:
            avanzamento(len(misure))
    fili = [threading.Thread(target=misura, args=("a", CASA, 3)),
            threading.Thread(target=misura, args=("b", PREFIX, 3)),
            threading.Thread(target=misura, args=("c", ROOTFS, 3)),
            threading.Thread(target=misura, args=("d", os.path.join(CASA, ".local/share/Trash/files"), 1))]
    for f in fili:
        f.start()
    for f in fili:
        f.join()
    base = os.path.dirname(CASA)
    altri = {}
    try:
        for n in os.listdir(base):
            p = os.path.join(base, n)
            if p not in (CASA, PREFIX) and os.path.isdir(p) and not os.path.islink(p):
                altri[p] = du(p, 0).get(p, 0)
    except OSError:
        pass
    r = calcola(misure["a"], misure["b"], misure["c"], misure["d"], disco(), altri=altri)
    r["quando"] = time.time()
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE + ".nuovo", "w") as f:
            json.dump(r, f)
        os.replace(CACHE + ".nuovo", CACHE)
    except OSError:
        pass
    return r


def ultima():
    """l'ultima analisi salvata (si mostra subito all'apertura, mentre se ne fa una nuova)"""
    try:
        with open(CACHE) as f:
            r = json.load(f)
        for v in r["categorie"].values():
            v["parti"] = [tuple(p) for p in v["parti"]]
        return r
    except (OSError, ValueError, KeyError):
        return None


def leggibile(p):
    """un percorso da mostrare: ~ per la casa di Termux, «Debian:» per il container, «Termux:» per $PREFIX"""
    if p.startswith(ROOTFS):
        return "Debian: " + (p[len(ROOTFS):] or "/")
    if p.startswith(CASA):
        return "~" + p[len(CASA):]
    if p.startswith(PREFIX):
        return "Termux: /usr" + p[len(PREFIX):]
    return p


def dimensione(n, it=True):
    """come macOS e Android: unità decimali (1 GB = 1000 MB), virgola in italiano"""
    for u, s in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= s:
            v = n / s
            t = f"{v:.1f}" if v < 100 else f"{v:.0f}"
            if t.endswith(".0"):
                t = t[:-2]
            return (t.replace(".", ",") if it else t) + " " + u
    return f"{int(n)} byte" if it else f"{int(n)} bytes"
