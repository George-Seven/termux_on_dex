#!/usr/bin/env python3
"""Store di Termux On DeX: le app di Flathub (aarch64) avviate SENZA la sandbox di Flatpak (lib/store.sh).

  tod-store.py prepara <id>                      radice, estensioni e collegamenti (dopo installa/aggiorna)
  tod-store.py avvia <id> [--proot] [--cmd PROG] [argomenti...]
  tod-store.py menu <id> | togli-menu <id>       voci dello Start (Exec = tod store avvia <id>)
  tod-store.py elenco                            app installate: id<TAB>nome<TAB>versione

Flatpak (di Debian, estratto nella cartella dello Store) scarica e sistema le app: su Android funziona
senza root né proot (lib/store.sh). L'avvio invece è nostro: bubblewrap vuole i namespace utente, che
Android vieta alle app, e "flatpak run" si ferma prima ancora di chiamarlo. Flatpak monta il runtime su
/usr, l'app su /app e le estensioni (Mesa, traduzioni, codec) dentro di loro; qui si fa con collegamenti
simbolici e una radice per app, che fakechroot fa vedere all'app (come le app di Debian senza proot,
lib/diretto.sh), oppure proot -r se senza proot non parte.

Variabili da lib/store.sh: TOD_STORE_DIR, TOD_STORE_ROOTFS (container), TOD_STORE_DIRETTO (tod-diretto.so),
TOD_STORE_FC (libfakechroot), TOD_STORE_CORREGGI (correggi.py), TOD_STORE_CASA, TOD_STORE_MENU,
TOD_STORE_TOD (comando tod), TOD_STORE_LANG, TOD_STORE_TZ, TOD_STORE_PROOT (1 = sempre proot).
"""
import configparser
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import time

ARCH = "aarch64"
VERSIONE = "1"   # cambia → le radici si rifanno
FUORI = "/proc:/dev:/sys:/data:/storage:/sdcard:/apex:/system:/vendor:/odm:/product"
ENTRO = 15       # secondi: un crash prima di questo vuol dire "senza proot non va"

S = os.environ.get("TOD_STORE_DIR") or os.path.expanduser("~/.local/share/tod-store")
FP = os.path.join(S, "flatpak")


def leggi(meta):
    c = configparser.ConfigParser(interpolation=None, strict=False)
    c.optionxform = str
    c.read(meta)
    return c


def deploy(kind, ref):
    """org.x/aarch64/51 → cartella vera di .../active/files (o None)."""
    p = os.path.join(FP, kind, ref, "active", "files")
    return os.path.realpath(p) if os.path.isdir(p) else None


def app_ref(appid):
    """id → (ref app, files veri, metadata): il ramo installato (di solito stable)."""
    for r in sorted(glob.glob(os.path.join(FP, "app", appid, ARCH, "*"))):
        d = deploy("app", f"{appid}/{ARCH}/{os.path.basename(r)}")
        if d:
            return f"{appid}/{ARCH}/{os.path.basename(r)}", d, leggi(os.path.join(d, "..", "metadata"))
    sys.exit(f"{appid}: non installata")


# --- prepara: estensioni, collegamenti, radice ----------------------------------------------------------

def collega(dove, verso):
    """punto di montaggio → collegamento relativo all'estensione (una cartella vuota si toglie)."""
    if os.path.islink(dove):
        os.unlink(dove)
    elif os.path.isdir(dove):
        if any(fs for _, _, fs in os.walk(dove)):   # c'è già qualcosa: si aggiungono le voci che mancano
            for n in os.listdir(verso):
                if not os.path.lexists(os.path.join(dove, n)):
                    os.symlink(os.path.relpath(os.path.join(verso, n), dove), os.path.join(dove, n))
            return
        shutil.rmtree(dove)
    os.makedirs(os.path.dirname(dove), exist_ok=True)
    os.symlink(os.path.relpath(verso, os.path.dirname(dove)), dove)


def estensioni(meta, files, ramo_app):
    """Le [Extension X] installate → collegamenti dentro files. Ritorna [(deploy, cartelle add-ld-path)]."""
    fatte = []
    base = os.path.join(FP, "runtime")
    if not os.path.isdir(base):
        return fatte
    for s in meta.sections():
        if not s.startswith("Extension "):
            continue
        nome, e = s.split(" ", 1)[1], meta[s]
        cart = e.get("directory")
        if not cart:
            continue
        rami = [v for v in (e.get("versions") or e.get("version") or "").split(";") if v] or [ramo_app]
        sotto = e.get("subdirectories") == "true"
        for n in sorted(os.listdir(base)):
            if n != nome and not (sotto and n.startswith(nome + ".")):
                continue
            for r in rami:
                d = deploy("runtime", f"{n}/{ARCH}/{r}")
                if not d:
                    continue
                dove = os.path.join(files, cart)
                if sotto:
                    dove = os.path.join(dove, n[len(nome) + 1:] + e.get("subdirectory-suffix", ""))
                collega(dove, d)
                ld = [os.path.join(d, x) for x in (e.get("add-ld-path") or "").split(":") if x]
                fatte.append((d, ld))
                break   # il primo ramo installato, come flatpak
    return fatte


def relativi(files, mappa):
    """collegamenti assoluti (/usr/share/licenses/...) → relativi: senza proot il kernel li seguirebbe
    nel vero Android."""
    n = 0
    for d, ds, fs in os.walk(files):
        for f in fs + ds:
            p = os.path.join(d, f)
            if not os.path.islink(p):
                continue
            t = os.readlink(p)
            if not t.startswith("/"):
                continue
            for pre, vero in mappa:
                if t == pre or t.startswith(pre + "/"):
                    os.unlink(p)
                    os.symlink(os.path.relpath(vero + t[len(pre):], d), p)
                    n += 1
                    break
    return n


def runpath_assoluti(*cartelle):
    """RUNPATH/RPATH assoluti degli ELF (es. /usr/lib/aarch64-linux-gnu/gvfs): il caricatore li apre da sé,
    senza fakechroot, quindi vanno anche in LD_LIBRARY_PATH col percorso vero."""
    trovate = set()
    for base in cartelle:
        for d, _, fs in os.walk(base):
            for f in fs:
                p = os.path.join(d, f)
                if os.path.islink(p):
                    continue
                try:
                    with open(p, "rb") as h:
                        e = h.read(64)
                        if len(e) < 64 or e[:4] != b"\x7fELF" or e[4] != 2:
                            continue
                        shoff, = struct.unpack_from("<Q", e, 0x28)
                        shentsize, shnum = struct.unpack_from("<HH", e, 0x3A)
                        if not shoff or not shnum:
                            continue
                        h.seek(shoff)
                        sh = h.read(shentsize * shnum)
                        sez = [struct.unpack_from("<IIQQQQIIQQ", sh, i * shentsize) for i in range(shnum)]
                        for s in sez:
                            if s[1] != 6:   # SHT_DYNAMIC
                                continue
                            h.seek(s[4])
                            dyn = h.read(s[5])
                            st = sez[s[6]]
                            h.seek(st[4])
                            strtab = h.read(st[5])
                            for i in range(0, len(dyn) - 15, 16):
                                tag, val = struct.unpack_from("<qQ", dyn, i)
                                if tag in (15, 29):   # DT_RPATH, DT_RUNPATH
                                    for x in strtab[val:strtab.index(b"\0", val)].decode().split(":"):
                                        if x.startswith("/"):
                                            trovate.add(x.rstrip("/"))
                except (OSError, ValueError, IndexError, struct.error):
                    continue
    return sorted(trovate)


def radice(rad, rd, ad):
    os.makedirs(rad, exist_ok=True)
    for n in ("tmp", "run/host", "var/tmp", "var/cache", "var/data", "home", "proc", "dev", "sys", "etc"):
        os.makedirs(os.path.join(rad, n), exist_ok=True)
    # usr e app: percorso vero ASSOLUTO (senza proot il kernel lo segue com'è; in proot la cartella vera
    # c'è allo stesso percorso). Dentro runtime e app invece tutto relativo.
    for n, v in (("usr", rd), ("app", ad), ("bin", "usr/bin"), ("sbin", "usr/sbin"), ("lib", "usr/lib")):
        p = os.path.join(rad, n)
        if os.path.islink(p):
            os.unlink(p)
        elif os.path.isdir(p):
            shutil.rmtree(p)
        os.symlink(v, p)
    nostri = {"passwd", "group", "hosts", "resolv.conf", "machine-id", "localtime", "mtab", "ld.so.cache"}
    for n in os.listdir(os.path.join(rd, "etc")):
        p = os.path.join(rad, "etc", n)
        if n in nostri:
            continue
        if os.path.lexists(p):
            os.unlink(p)
        os.symlink("../usr/etc/" + n, p)
    casa = os.environ.get("TOD_STORE_CASA") or os.path.expanduser("~")
    utente = os.environ.get("TOD_STORE_UTENTE", "dex")
    uid, gid = os.getuid(), os.getgid()

    def scrivi(n, t):
        with open(os.path.join(rad, "etc", n), "w") as f:
            f.write(t)
    scrivi("passwd", f"root:x:0:0:root:/root:/bin/sh\n{utente}:x:{uid}:{gid}:{utente}:{casa}:/bin/bash\n"
                     "nobody:x:65534:65534:nobody:/:/sbin/nologin\n")
    scrivi("group", f"root:x:0:\n{utente}:x:{gid}:\nnobody:x:65534:\n")
    scrivi("hosts", "127.0.0.1 localhost\n::1 localhost\n")
    dns = [x for x in (subprocess.run(["getprop", f"net.dns{i}"], capture_output=True, text=True).stdout.strip()
                       if shutil.which("getprop") else "" for i in (1, 2)) if x]
    scrivi("resolv.conf", "".join(f"nameserver {x}\n" for x in dns + ["1.1.1.1", "8.8.8.8"]))
    if not os.path.exists(os.path.join(rad, "etc", "machine-id")):
        scrivi("machine-id", os.urandom(16).hex() + "\n")
    for n, v in (("localtime", "../usr/share/zoneinfo/" + (os.environ.get("TOD_STORE_TZ") or "UTC")),
                 ("mtab", "../proc/self/mounts")):
        p = os.path.join(rad, "etc", n)
        if os.path.lexists(p):
            os.unlink(p)
        os.symlink(v, p)
    # icone e temi del desktop (Termux) dove flatpak mette quelli dell'host: XDG_DATA_DIRS ha /run/host/share
    pre = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    for n in ("icons", "themes"):
        p = os.path.join(rad, "run", "host", "share", n)
        if os.path.isdir(os.path.join(pre, "share", n)) and not os.path.lexists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            os.symlink(os.path.join(pre, "share", n), p)


def firma(rd, ad):
    return f"{VERSIONE}\n{rd}\n{ad}\n"


def prepara(appid, zitto=False):
    t0 = time.time()
    ref, ad, meta_app = app_ref(appid)
    rref = meta_app["Application"]["runtime"]
    rd = deploy("runtime", rref)
    if not rd:
        sys.exit(f"{appid}: manca il runtime {rref}")
    meta_rt = leggi(os.path.join(rd, "..", "metadata"))
    ext = estensioni(meta_rt, rd, rref.split("/")[-1]) + estensioni(meta_app, ad, ref.split("/")[-1])
    mappa = [("/usr", rd), ("/app", ad), ("/etc", rd + "/etc"), ("/bin", rd + "/bin"), ("/lib", rd + "/lib"),
             ("/sbin", rd + "/sbin")]
    n = relativi(rd, mappa) + relativi(ad, mappa) + sum(relativi(d, mappa) for d, _ in ext)
    rad = os.path.join(S, "radici", appid)
    radice(rad, rd, ad)
    # RUNPATH assoluti (percorsi veri) e add-ld-path delle estensioni: per LD_LIBRARY_PATH
    cache = os.path.join(S, "cache", "runpath-" + os.path.basename(os.path.dirname(rd)))
    if os.path.exists(cache):
        rt_rp = open(cache).read().split("\n")
    else:
        rt_rp = runpath_assoluti(rd)
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        open(cache, "w").write("\n".join(rt_rp))
    ld = []
    for x in [y for y in rt_rp if y] + runpath_assoluti(ad):
        for pre, vero in mappa:
            if x == pre or x.startswith(pre + "/"):
                v = vero + x[len(pre):]
                if os.path.isdir(v) and v not in ld:
                    ld.append(v)
                break
    for _, l in ext:
        ld += [x for x in l if os.path.isdir(x) and x not in ld]
    with open(os.path.join(rad, ".ld-path"), "w") as f:
        f.write(":".join(ld) + "\n")
    with open(os.path.join(rad, ".firma"), "w") as f:
        f.write(firma(rd, ad))
    if not zitto:
        print(f"{appid}: {len(ext)} estensioni, {n} collegamenti resi relativi, {len(ld)} cartelle di librerie "
              f"({time.time() - t0:.1f} s)")
    return rad, rd, ad, meta_app, meta_rt


# --- avvio ----------------------------------------------------------------------------------------------

def glibc(rd):
    """caricatore e libc del RUNTIME, corretti come quelli di Debian (share/diretto/correggi.py)."""
    d = os.path.join(S, "glibc", os.path.basename(os.path.dirname(rd)))
    if not os.path.exists(os.path.join(d, "ld.so")):
        os.makedirs(os.path.join(d, "lib"), exist_ok=True)
        lib = os.path.join(rd, "lib", "aarch64-linux-gnu")
        subprocess.run([sys.executable, os.environ["TOD_STORE_CORREGGI"],
                        os.path.join(lib, "ld-linux-aarch64.so.1"), os.path.join(d, "ld.so"),
                        os.path.join(lib, "libc.so.6"), os.path.join(d, "lib", "libc.so.6")],
                       check=True, stdout=subprocess.DEVNULL)
    return d


def gpu_env():
    """La GPU del container (lib/gpu.sh: zink sopra turnip KGSL). Il Mesa di Flathub non conosce KGSL
    (andrebbe in llvmpipe): il suo zink usa il turnip del container, con un ICD che punta al file vero."""
    root = os.environ.get("TOD_STORE_ROOTFS", "")
    out = {}
    f = os.path.join(root, "etc", "tod", "gpu.env")
    if not root or not os.path.exists(f):
        return out
    for l in open(f):
        l = l.strip()
        if "=" in l and not l.startswith("#"):
            k, v = l.split("=", 1)
            out[k] = v
    icd = out.pop("VK_ICD_FILENAMES", "")
    nuovi = []
    for j in [x for x in icd.split(":") if x]:
        try:
            dati = json.load(open(root + j))
            lp = dati["ICD"]["library_path"]
            if lp.startswith("/"):
                dati["ICD"]["library_path"] = os.path.realpath(root + lp)
            dst = os.path.join(S, "gl", os.path.basename(j))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w") as h:
                json.dump(dati, h)
            nuovi.append(dst)
        except (OSError, ValueError, KeyError):
            continue
    if nuovi:
        out["VK_ICD_FILENAMES"] = ":".join(nuovi)
    return out


# programmi di Flatpak che fuori da Flatpak non funzionano: i nostri vanno per primi nel PATH
AIUTANTI = {
    # zypak: la sandbox di Chromium/Electron fatta con Flatpak (Boxy SVG, VS Code, Signal...). Senza Flatpak
    # (e senza namespace: Android) l'app parte da sola, senza sandbox, come le app Electron di Debian; TMPDIR
    # corto, perché i socket di Chromium stanno sotto i 108 caratteri (lo script dell'app lo allunga)
    # Il programma parte dal suo percorso VERO: con /app/... /proc/self/exe e i collegamenti risolti davano due
    # percorsi diversi, e Chromium si fermava subito (SIGTRAP, Boxy SVG, 9 novembre)
    "zypak-wrapper": """#!/bin/sh
export TMPDIR="${TOD_TMPDIR:-$TMPDIR}"
p=$(readlink -f "$1" 2>/dev/null) || p=$1
shift
exec "$p" --no-sandbox --disable-dev-shm-usage "$@"
""",
    # flatpak-spawn --host ...: niente sandbox, il comando parte com'è (le opzioni si tolgono)
    "flatpak-spawn": """#!/bin/sh
while [ $# -gt 0 ]; do
  case $1 in
    --env=*) export "${1#--env=}" ;;
    --directory=*) cd "${1#--directory=}" || exit 1 ;;
    --) shift; break ;;
    -*) ;;
    *) break ;;
  esac
  shift
done
exec "$@"
""",
}


def aiutanti():
    d = os.path.join(S, "aiutanti")
    os.makedirs(d, exist_ok=True)
    for n, testo in AIUTANTI.items():
        p = os.path.join(d, n)
        try:
            uguale = open(p).read() == testo
        except OSError:
            uguale = False
        if not uguale:
            with open(p + ".tmp", "w") as f:
                f.write(testo)
            os.chmod(p + ".tmp", 0o755)
            os.replace(p + ".tmp", p)
    return d


def ambiente(appid, rad, rd, ad, meta_app, meta_rt, proot):
    casa = os.environ.get("TOD_STORE_CASA") or os.path.expanduser("~")
    tmp = os.environ.get("TMPDIR", "/data/data/com.termux/files/usr/tmp")
    var = os.path.join(casa, ".var", "app", appid)
    xdg = {k: os.path.join(var, v) for k, v in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                                                 ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", ".local/state"))}
    for v in xdg.values():
        os.makedirs(v, exist_ok=True)
    n = os.environ.get("DISPLAY", ":0").split(":")[-1].split(".")[0] or "0"
    utente = os.environ.get("TOD_STORE_UTENTE", "dex")
    a = {"PATH": aiutanti() + ":/app/bin:/usr/bin", "TOD_TMPDIR": tmp, "HOME": casa, "USER": utente, "LOGNAME": utente, "SHELL": "/bin/sh",
         "LANG": os.environ.get("TOD_STORE_LANG") or "C.UTF-8", "TMPDIR": tmp,
         "XDG_RUNTIME_DIR": os.environ.get("TOD_STORE_RUNTIME_DIR") or os.path.join(tmp, "xdg-store"),
         "DISPLAY": os.path.join(tmp, ".X11-unix", "X" + n), "FLATPAK_ID": appid,
         "GDK_BACKEND": "x11", "QT_QPA_PLATFORM": "xcb", "PULSE_SERVER": "127.0.0.1", "NO_AT_BRIDGE": "1"}
    a.update(xdg)
    for m in (meta_rt, meta_app):   # [Environment] di runtime e app, come flatpak
        if m.has_section("Environment"):
            a.update(dict(m["Environment"]))
    for k in ("DBUS_SESSION_BUS_ADDRESS", "XDG_CURRENT_DESKTOP", "DESKTOP_STARTUP_ID"):
        if os.environ.get(k):
            a[k] = os.environ[k]
    a.update(gpu_env())
    # il glibc apre lingue, conversioni e fusi orari al suo interno (fakechroot non lo vede): percorsi veri
    a.update({"LOCPATH": os.path.join(rd, "lib", "locale"), "GCONV_PATH": os.path.join(rd, "lib", ARCH + "-linux-gnu", "gconv"),
              "TZDIR": os.path.join(rd, "share", "zoneinfo")})
    if os.environ.get("TOD_STORE_TZ"):
        a["TZ"] = os.environ["TOD_STORE_TZ"]
    ldx = open(os.path.join(rad, ".ld-path")).read().strip()
    lib = os.path.join(rd, "lib", ARCH + "-linux-gnu")
    libs = [os.path.join(ad, "lib"), lib, os.path.join(lib, "GL", "default", "lib")] + [x for x in ldx.split(":") if x]
    # Android vieta i collegamenti fisici anche alle app: i file temporanei O_TMPFILE poi collegati al
    # nome vero (QSaveFile di Qt: KWrite non salvava le impostazioni, "Disk full?") ripiegano su
    # temporaneo + rinomina (share/store/tod-nolink.c)
    nolink = os.environ.get("TOD_STORE_NOLINK", os.path.join(S, "bin", "tod-nolink.so"))
    nolink = nolink if os.path.exists(nolink) else ""
    if proot:
        a["LD_LIBRARY_PATH"] = ":".join(["/app/lib"] + libs)
        a["PROOT_TMP_DIR"] = tmp
        if nolink:
            a["LD_PRELOAD"] = nolink
    else:
        g = glibc(rd)
        a["LD_LIBRARY_PATH"] = ":".join([os.path.join(g, "lib")] + libs)
        a.update({"GLIBC_TUNABLES": "glibc.pthread.rseq=0", "FAKECHROOT_BASE": rad,
                  "FAKECHROOT_ELFLOADER": os.path.join(g, "ld.so"), "FAKECHROOT_ELFLOADER_OPT_ARGV0": "--argv0",
                  "FAKECHROOT_EXCLUDE_PATH": FUORI,
                  "LD_PRELOAD": " ".join(x for x in (nolink, os.environ["TOD_STORE_DIRETTO"], os.environ["TOD_STORE_FC"]) if x)})
    os.makedirs(a["XDG_RUNTIME_DIR"], mode=0o700, exist_ok=True)
    os.makedirs(os.path.join(a["XDG_RUNTIME_DIR"], "app", appid), exist_ok=True)   # come flatpak
    return a


def lancio(rad, prog):
    """senza proot: (programma da dare al caricatore, suo percorso vero, argomenti prima di quelli dell'utente).
    Il caricatore avvia solo programmi veri: per gli script (#!, es. Boxy SVG) l'interprete del runtime, con lo
    script come argomento, come fa il kernel (lib/diretto.sh fa lo stesso per le app di Debian)."""
    # il percorso VERO (usr e app nella radice sono collegamenti verso la cartella di flatpak): tod-diretto.so
    # risponde a /proc/self/exe con questo, e coincide con quello che si trova risolvendo i collegamenti.
    # Con /app/... Chromium/Electron vedevano due percorsi diversi per sé stessi e si fermavano (SIGTRAP: Boxy SVG)
    vero = prog if prog.startswith(("/data/", "/storage/")) else os.path.realpath(rad + prog)
    try:
        with open(vero, "rb") as f:
            riga = f.readline(256)
    except OSError:
        riga = b""
    if not riga.startswith(b"#!"):
        return prog, vero, []
    parti = riga[2:].decode(errors="replace").split()
    if not parti:
        return prog, vero, []
    interp, extra = parti[0], parti[1:]
    if os.path.basename(interp) == "env" and extra:   # #!/usr/bin/env python3
        nome, extra = extra[0], extra[1:]
        interp = next((d + "/" + nome for d in ("/app/bin", "/usr/bin") if os.path.exists(rad + d + "/" + nome)),
                      "/usr/bin/" + nome)
    return interp, os.path.realpath(rad + interp), extra + [prog]


def no_file():
    return os.path.join(S, "diretto-no")


def avvia(appid, args):
    proot = os.environ.get("TOD_STORE_PROOT") == "1"
    cmd = None
    while args and args[0] in ("--proot", "--cmd"):
        if args[0] == "--proot":
            proot, args = True, args[1:]
        else:
            cmd, args = args[1], args[2:]
    ref, ad, meta_app = app_ref(appid)
    rad = os.path.join(S, "radici", appid)
    rd = deploy("runtime", meta_app["Application"]["runtime"])
    try:
        ok = open(os.path.join(rad, ".firma")).read() == firma(rd, ad)
    except OSError:
        ok = False
    if ok:
        meta_rt = leggi(os.path.join(rd, "..", "metadata"))
    else:   # prima volta, o app/runtime aggiornati (deploy nuovo, senza i nostri collegamenti)
        rad, rd, ad, meta_app, meta_rt = prepara(appid, zitto=True)
    prog = "/app/bin/" + (cmd or meta_app["Application"]["command"]) if not (cmd or "").startswith("/") else cmd
    if not proot and os.path.exists(no_file()) and appid in open(no_file()).read().split():
        proot = True
    if not proot:
        a = ambiente(appid, rad, rd, ad, meta_app, meta_rt, False)
        ld = a["FAKECHROOT_ELFLOADER"]
        nome, vero, prima = lancio(rad, prog)
        t0 = time.time()
        cmd = [ld, "--argv0", nome, vero] + prima + args
        if os.environ.get("TOD_STORE_STRACE"):   # diagnosi: strace di Termux con l'ambiente solo per l'app (-E)
            cmd = ["strace", "-f", "-o", os.environ["TOD_STORE_STRACE"]] + \
                [x for kv in a.items() for x in ("-E", "%s=%s" % kv)] + cmd
            a = {k: os.environ[k] for k in ("PATH", "PREFIX", "TMPDIR", "HOME") if k in os.environ}
        rc = subprocess.call(cmd, env=a)
        # caduto subito (chiamata vietata 159, crash): segnato, si riparte in proot (come diretto_run)
        if rc in (132, 133, 134, 135, 139, 159, -4, -5, -6, -7, -11, -31) and time.time() - t0 < ENTRO:
            with open(no_file(), "a") as f:
                f.write(appid + "\n")
            print(f"{appid}: senza proot non parte (uscita {rc}): d'ora in poi con proot", file=sys.stderr)
        else:
            sys.exit(rc if rc >= 0 else 128 - rc)
    a = ambiente(appid, rad, rd, ad, meta_app, meta_rt, True)
    b = []
    for x in ("/data/data/com.termux", "/storage", "/sdcard", "/dev", "/proc", "/sys", S):
        if os.path.exists(x):
            b += ["-b", x]
    pr = shutil.which("proot") or "/data/data/com.termux/files/usr/bin/proot"
    # l'ambiente dell'app solo DENTRO proot (env -i): proot è un programma di Termux, e con LD_LIBRARY_PATH del
    # runtime il linker di Android caricava la libc del runtime ("bad ELF magic": Boxy SVG, 9 novembre)
    fuori = {k: os.environ[k] for k in ("PATH", "PREFIX", "TMPDIR", "HOME", "LANG") if k in os.environ}
    fuori["PROOT_TMP_DIR"] = a["PROOT_TMP_DIR"]
    dentro = ["%s=%s" % kv for kv in a.items() if kv[0] != "PROOT_TMP_DIR"]
    os.execve(pr, [pr, "--kill-on-exit", "-r", rad] + b + ["-w", a["HOME"], "/usr/bin/env", "-i"] + dentro +
              [prog] + args, fuori)


# --- menu ----------------------------------------------------------------------------------------------

def icona(ad, nome):
    if not nome or nome.startswith("/"):
        return nome
    base = os.path.join(ad, "share", "icons", "hicolor")
    for d in ("scalable", "512x512", "256x256", "192x192", "128x128", "96x96", "64x64", "48x48"):
        for e in (".svg", ".png"):
            p = os.path.join(base, d, "apps", nome + e)
            if os.path.exists(p):
                return p
    return nome


def menu_dir():
    return os.environ.get("TOD_STORE_MENU") or os.path.expanduser("~/.local/share/applications")


def menu(appid):
    ref, ad, meta_app = app_ref(appid)
    comando = meta_app["Application"]["command"]
    tod = os.environ.get("TOD_STORE_TOD", "tod")
    attivo = os.path.join(FP, "app", ref, "active", "files")   # stabile dopo gli aggiornamenti
    fatti = []
    # come flatpak: solo le voci col nome dell'app (KWrite porta anche org.kde.kate.desktop, che non c'è)
    for src in sorted(glob.glob(os.path.join(ad, "share", "applications", appid + "*.desktop"))):
        righe, sez = [], ""
        for l in open(src, encoding="utf-8", errors="replace").read().split("\n"):
            if l.startswith("["):
                sez = l
                righe.append(l)
                if l == "[Desktop Entry]":
                    righe += ["X-TOD-Store=flathub", "StartupNotify=true"]
                continue
            if l.startswith(("TryExec=", "DBusActivatable=", "StartupNotify=")) and sez.startswith("[Desktop"):
                continue
            if l.startswith("Exec=") and sez.startswith("[Desktop"):
                parti = l[5:].split(" ", 1)
                p = os.path.basename(parti[0])
                extra = "" if p == comando else f" --cmd {p}"
                l = f"Exec={tod} store avvia {appid}{extra}" + (" " + parti[1] if len(parti) > 1 else "")
            elif l.startswith("Icon=") and sez.startswith("[Desktop"):
                i = icona(ad, l[5:])
                l = "Icon=" + (i.replace(ad, attivo, 1) if i.startswith(ad) else i)
            righe.append(l)
        dst = os.path.join(menu_dir(), "tod-store-" + os.path.basename(src))
        os.makedirs(menu_dir(), exist_ok=True)
        with open(dst, "w") as f:
            f.write("\n".join(righe))
        fatti.append(dst)
    print("\n".join(fatti))


def togli_menu(appid):
    for p in glob.glob(os.path.join(menu_dir(), "tod-store-*.desktop")):
        try:
            if f" store avvia {appid}" in open(p).read():
                os.unlink(p)
                print(p)
        except OSError:
            pass


def elenco():
    """id<TAB>nome<TAB>versione<TAB>icona (percorso stabile anche dopo gli aggiornamenti)."""
    for r in sorted(glob.glob(os.path.join(FP, "app", "*", ARCH, "*", "active"))):
        appid = r.split("/")[-4]
        ad = os.path.realpath(os.path.join(r, "files"))
        nome, ic = appid, ""
        for src in glob.glob(os.path.join(ad, "share", "applications", appid + ".desktop")):
            c = leggi(src)
            if c.has_section("Desktop Entry"):
                e = c["Desktop Entry"]
                nome = e.get("Name[" + (os.environ.get("TOD_STORE_LANG") or "it")[:2] + "]") or e.get("Name") or appid
                ic = icona(ad, e.get("Icon", ""))
                ic = os.path.join(r, "files", ic[len(ad) + 1:]) if ic.startswith(ad + "/") else ""
        versione = ""
        for mi in glob.glob(os.path.join(ad, "share", "metainfo", "*.xml")):
            t = open(mi, encoding="utf-8", errors="replace").read()
            i = t.find("<release ")
            j = t.find('version="', i)
            if i >= 0 and j >= 0:
                versione = t[j + 9:t.find('"', j + 9)]
                break
        print(f"{appid}\t{nome}\t{versione}\t{ic}")


def main(a):
    if not a:
        sys.exit(__doc__)
    c = a[0]
    if c == "prepara" and len(a) == 2:
        prepara(a[1])
    elif c == "avvia" and len(a) >= 2:
        avvia(a[1], a[2:])
    elif c == "menu" and len(a) == 2:
        menu(a[1])
    elif c == "togli-menu" and len(a) == 2:
        togli_menu(a[1])
    elif c == "elenco":
        elenco()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
