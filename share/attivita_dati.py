"""attivita_dati.py — i dati della Gestione attività (12 novembre 2026), senza grafica: li usano tod-attivita.py e i test.

Android mostra a Termux solo i propri processi (lo stesso utente): tutto il desktop, le app di Termux e quelle di Debian,
non le app del telefono. /proc/stat non si legge, quindi il processore è la somma del tempo dei nostri processi, in
percentuale del totale dei core (come la colonna CPU di Windows).

  Campione(proc)                 letture di un istante: {pid: Proc}, ora, memoria del telefono
  cpu(prima, dopo)               {pid: percento del processore} tra due campioni
  assegna(processi, radici)      {chiave app: [pid]} seguendo i genitori, e i pid che non sono di nessuna app
  nome_processo(argv, comm)      un nome leggibile (le app di Debian partono con ld.so --argv0 NOME)
  protetto(proc)                 vero per i pezzi del desktop che non si possono terminare
  chip_video(), chip_stato()     il video col chip (lib/videochip.sh): video in corso, totali, acceso/spento e perché
  Telefono(), app_android()      le app del telefono aperte (share/android/AppAndroid.java), con l'"Accesso ai dati di
                                 utilizzo" dato a Termux
"""
import json
import os
import select
import subprocess
import time

PROC = os.environ.get("TOD_ATTIVITA_PROC", "/proc")
TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
CORE = os.cpu_count() or 1

# i pezzi del desktop: nome leggibile nell'elenco dei processi in background (italiano, inglese)
NOMI = {
    "xfwm4": ("Gestore delle finestre", "Window manager"),
    "xfdesktop": ("Scrivania", "Desktop"),
    "thunar": ("Esplora file", "File Explorer"),
    "xfsettingsd": ("Impostazioni del desktop", "Desktop settings"),
    "xfconfd": ("Archivio delle impostazioni", "Settings storage"),
    "xfce4-session": ("Sessione del desktop", "Desktop session"),
    "xfce4-notifyd": ("Notifiche", "Notifications"),
    "pulseaudio": ("Audio", "Audio"),
    "dbus-daemon": ("Messaggi tra le app (D-Bus)", "App messaging (D-Bus)"),
    "tod-barra.py": ("Barra delle applicazioni", "Taskbar"),
    "tod-pannelli.py": ("Pannelli della barra", "Taskbar panels"),
    "tod-start.py": ("Start", "Start"),
    "tod-edges.py": ("Bordi delle finestre", "Window edges"),
    "tod-phone-status.py": ("Batteria, rete e notifiche del telefono", "Phone battery, network and notifications"),
    "tod-rete.py": ("Computer in rete", "Network computers"),
    "tod-stampa.py": ("Servizio di stampa", "Print service"),
    "cupsd": ("Servizio di stampa (CUPS)", "Print service (CUPS)"),
    "tod-avvisi-host.py": ("Finestre Apri/Salva e avvisi", "Open/Save dialogs and alerts"),
    "tod-open-host": ("Apertura dei link", "Link opener"),
    "tod-updates.py": ("Aggiornamenti", "Updates"),
    "tod-lkl": ("Dischi USB", "USB drives"),
    "tod-smb": ("Cartelle condivise in rete", "Network shared folders"),
    "gvfsd": ("File system virtuali", "Virtual file systems"),
    "tumblerd": ("Miniature", "Thumbnails"),
    "gnome-keyring-daemon": ("Portachiavi", "Keyring"),
    "termux-bridge": ("Ponte di sviluppo", "Development bridge"),
}
# chi tiene in piedi il desktop: terminarlo chiuderebbe tutto, o lascerebbe il desktop a metà
PROTETTI = {"xfwm4", "xfdesktop", "xfce4-session", "xfsettingsd", "xfconfd", "dbus-daemon", "pulseaudio",
            "tod-barra.py", "tod-pannelli.py", "tod-start.py", "tod-edges.py", "tod-phone-status.py", "termux-x11",
            "app_process", "app_process64", "bash", "sh", "login", "proot", "tod-attivita.py"}


class Proc:
    __slots__ = ("pid", "genitore", "tick", "kb", "argv", "comm", "nome")

    def __init__(self, pid, genitore, tick, kb, argv, comm):
        self.pid, self.genitore, self.tick, self.kb, self.argv, self.comm = pid, genitore, tick, kb, argv, comm
        self.nome = nome_processo(argv, comm)


def nome_processo(argv, comm=""):
    """Il nome del programma: le app di Debian senza proot partono come "ld.so --argv0 /usr/bin/app ...", gli script
    come "python3 /percorso/tod-foto.py", "bash /percorso/tod-usb"."""
    a = [x for x in argv if x]
    if not a:
        return comm or "?"
    if os.path.basename(a[0]).startswith("ld") and "--argv0" in a:
        i = a.index("--argv0")
        if i + 1 < len(a):
            return os.path.basename(a[i + 1])
    base = os.path.basename(a[0])
    if base.rstrip("0123456789.") in ("python", "bash", "sh", "node", "perl") and len(a) > 1:
        for x in a[1:]:
            if not x.startswith("-"):
                return os.path.basename(x)
    return base or comm or "?"


def leggi(pid, proc=PROC):
    """Un processo (None se è sparito o non si legge)."""
    try:
        with open(f"{proc}/{pid}/stat") as f:
            st = f.read()
        comm = st[st.index("(") + 1:st.rindex(")")]
        campi = st.rsplit(")", 1)[1].split()
        genitore, tick = int(campi[1]), int(campi[11]) + int(campi[12])
    except (OSError, ValueError, IndexError):
        return None
    try:
        with open(f"{proc}/{pid}/cmdline", "rb") as f:
            argv = [x.decode("utf-8", "replace") for x in f.read().split(b"\0")]
    except OSError:
        argv = []
    kb = 0
    try:   # PSS: le parti condivise divise tra chi le usa, così la somma non conta due volte
        with open(f"{proc}/{pid}/smaps_rollup") as f:
            kb = next((int(r.split()[1]) for r in f if r.startswith("Pss:")), 0)
    except (OSError, ValueError):
        try:
            with open(f"{proc}/{pid}/status") as f:
                kb = next((int(r.split()[1]) for r in f if r.startswith("VmRSS:")), 0)
        except (OSError, ValueError):
            pass
    return Proc(int(pid), genitore, tick, kb, argv, comm)


def memoria_telefono(proc=PROC):
    """(totale, disponibile) in kB"""
    m = {}
    try:
        with open(f"{proc}/meminfo") as f:
            for r in f:
                k, _, v = r.partition(":")
                if k in ("MemTotal", "MemAvailable"):
                    m[k] = int(v.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return m.get("MemTotal", 0), m.get("MemAvailable", 0)


class Campione:
    def __init__(self, proc=PROC, ora=None):
        self.ora = time.monotonic() if ora is None else ora
        self.processi = {}
        try:
            voci = os.listdir(proc)
        except OSError:
            voci = []
        for n in voci:
            if n.isdigit():
                p = leggi(n, proc)
                if p is not None:
                    self.processi[p.pid] = p
        self.mem_totale, self.mem_libera = memoria_telefono(proc)


def cpu(prima, dopo, tick=TICK, core=CORE):
    """{pid: percento del processore intero} tra due campioni (un processo nuovo conta da zero)."""
    dt = dopo.ora - prima.ora if prima is not None else 0
    if dt <= 0:
        return {pid: 0.0 for pid in dopo.processi}
    out = {}
    for pid, p in dopo.processi.items():
        q = prima.processi.get(pid)
        usati = p.tick - q.tick if q is not None and q.tick <= p.tick else 0
        out[pid] = max(0.0, usati / tick / dt / core * 100.0)
    return out


def assegna(processi, radici):
    """RADICI: {pid della finestra: chiave dell'app}. Ogni processo va all'app del primo antenato che è una radice
    (i processi di un'app Electron stanno sotto la sua finestra). → ({chiave: [pid]}, [pid senza app])"""
    app, resto = {}, []
    for pid in processi:
        p, passi = pid, 0
        while p in processi and p not in radici and passi < 64:
            p, passi = processi[p].genitore, passi + 1
        if p in radici:
            app.setdefault(radici[p], []).append(pid)
        else:
            resto.append(pid)
    return app, resto


def discendenti(processi, pid):
    """pid e tutti i suoi discendenti, i figli prima dei genitori (per terminarli dal basso)."""
    figli = {}
    for p in processi.values():
        figli.setdefault(p.genitore, []).append(p.pid)
    out, coda = [], [pid]
    while coda:
        q = coda.pop()
        out.append(q)
        coda.extend(figli.get(q, []))
    return list(reversed(out))


def protetto(p, io=None):
    """Non si termina: i pezzi che tengono in piedi il desktop, e la Gestione attività stessa."""
    return p.nome in PROTETTI or p.comm in PROTETTI or p.pid == (io or os.getpid())


def nome_leggibile(p, italiano=True):
    n = NOMI.get(p.nome) or NOMI.get(p.comm)
    return n[0 if italiano else 1] if n else p.nome


def dimensione(kb, italiano=True):
    """1,2 GB · 340 MB · 512 KB, come Windows"""
    if kb >= 1024 * 1024:
        t = f"{kb / 1024 / 1024:.1f} GB"
    elif kb >= 1024:
        t = f"{kb / 1024:.1f} MB" if kb < 10 * 1024 else f"{kb / 1024:.0f} MB"
    else:
        t = f"{kb} KB"
    return t.replace(".", ",") if italiano else t


def percento(x, italiano=True):
    t = f"{x:.1f}%" if x < 10 else f"{x:.0f}%"
    return t.replace(".", ",") if italiano else t


# --- il chip video (13 novembre, Pietro: "ho modo di vedere come si comporta questo chip?" → "perché in gestione
# attività no?"). Come la GPU "Video Decode" di Windows. L'aiutante (share/video/tod-va-chip.c) scrive uno stato per video
# in $TMPDIR/tod-va-chip/stato/<pid>.json ogni secondo e una riga per video finito nello storico della sessione.
CHIP_DIR = os.path.join(os.environ.get("TMPDIR", "/data/data/com.termux/files/usr/tmp"), "tod-va-chip")
CODEC = {"h264": "H.264", "vp9": "VP9", "hevc": "HEVC", "av1": "AV1"}


def chip_video(cartella=CHIP_DIR, ora=None):
    """([video in corso], (video della sessione, fotogrammi della sessione)). Un video in corso:
    {pid (processo dell'app), codec, w, h, fotogrammi, inizio, agg}; quelli fermi da più di 5 s non contano."""
    ora = time.time() if ora is None else ora
    attivi = []
    try:
        nomi = sorted(os.listdir(os.path.join(cartella, "stato")))
    except OSError:
        nomi = []
    for n in nomi:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(cartella, "stato", n)) as f:
                v = json.load(f)
            if ora - float(v.get("agg", 0)) <= 5:
                v["id"] = n[:-5]
                attivi.append(v)
        except (OSError, ValueError):
            pass
    video, fotogrammi = len(attivi), sum(int(v.get("fotogrammi", 0)) for v in attivi)
    try:
        with open(os.path.join(cartella, "storico")) as f:
            for riga in f:
                p = riga.split()
                if len(p) >= 2 and p[1].isdigit():
                    video += 1
                    fotogrammi += int(p[1])
    except OSError:
        pass
    return attivi, (video, fotogrammi)


def chip_al_secondo(prima, dopo, secondi):
    """{id: fotogrammi al secondo} tra due letture di chip_video"""
    vecchi = {v["id"]: int(v.get("fotogrammi", 0)) for v in prima or []}
    out = {}
    for v in dopo:
        n = int(v.get("fotogrammi", 0)) - vecchi.get(v["id"], int(v.get("fotogrammi", 0)))
        out[v["id"]] = max(0.0, n / secondi) if secondi > 0 else 0.0
    return out


def chip_stato(casa=None, tmp=None):
    """("attivo" | "spento" | "non_disponibile" | "in_preparazione", motivo)"""
    casa = casa or os.environ.get("TOD_TERMUX_HOME") or os.environ.get("HOME", "")
    tmp = tmp or os.environ.get("TMPDIR", "/data/data/com.termux/files/usr/tmp")
    try:
        for riga in open(os.path.join(casa, ".config/tod/tod.conf")):
            if riga.startswith("VIDEO_CHIP=") and riga.split("=", 1)[1].strip().strip('"') == "0":
                return "spento", ""
    except OSError:
        pass
    try:
        prova = open(os.path.join(casa, ".local/share/tod-va/prova.txt")).read()
    except OSError:
        prova = ""
    for riga in prova.splitlines():
        if riga.startswith("ESITO no: "):
            return "non_disponibile", riga[len("ESITO no: "):]
    try:
        pid = int(open(os.path.join(tmp, "tod-va-chip.pid")).read().strip())
        if b"tod-va-chip" in open(f"/proc/{pid}/cmdline", "rb").read() and "ESITO ok" in prova:
            return "attivo", ""
    except (OSError, ValueError):
        pass
    return "in_preparazione", ""



# --- le app del telefono (15 novembre, Pietro: "mi serve che gestione attività mi permetta di gestire le app android
# aperte"). Android non mostra a Termux i loro processi; con l'"Accesso ai dati di utilizzo" dato a Termux l'aiutante
# share/android/AppAndroid.java (app_process, utente di Termux) dice quali sono aperte e in che stato. Processore e
# memoria di quelle app Android non li dà: solo lo stato.
ANDROID_DEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "android", "tod-app-android.dex")
NOSTRE = ("com.termux",)   # Termux, Termux:X11, Termux:API: il desktop stesso, non si mostra né si chiude
PRIMA_APP = 10000          # Process.FIRST_APPLICATION_UID: sotto ci sono gli uid condivisi di sistema
STATI_ANDROID = {"primo_piano": ("In primo piano", "Foreground"), "background": ("In background", "Background"),
                 "sospesa": ("Sospesa", "Suspended")}


def stato_android(uid, stato, visibile, prima=False):
    """dallo stato del processo (ActivityManager.PROCESS_STATE_*) a "primo_piano" | "background" | "sospesa", o None
    se l'app non è aperta: 19 = processo in cache senza finestre (chiusa dalle recenti), 20 = nessun processo.
    Le app con l'uid condiviso di sistema (Impostazioni gira come "system", sempre vivo) contano solo se visibili.
    6-11, 13, 14 (un servizio, una notifica ricevuta, un lavoro in sottofondo) non dicono se l'app ha finestre: resta
    com'era (PRIMA = era già nell'elenco), così un'app chiusa che si sveglia un attimo non compare e scompare."""
    if uid < PRIMA_APP:
        return "primo_piano" if visibile else None
    if stato >= 19:
        return None
    if visibile or stato <= 3 or stato == 12:   # persistente, in primo piano, legata a chi è in primo piano, schermo spento
        return "primo_piano"
    if stato >= 16:   # in cache con le sue finestre o nelle recenti: Android la tiene ferma (congelata)
        return "sospesa"
    if stato in (4, 5, 15):   # servizio in primo piano (musica, navigatore: ha la sua notifica), l'app di prima
        return "background"
    return "background" if prima else None


def app_android(righe, note, prima=()):
    """righe dell'aiutante [[pacchetto, uid, stato, ultimo evento in ms, visibile]] → [{pkg, stato, ultimo}] delle app
    aperte, solo quelle in NOTE (che si avviano dal launcher: niente finestrelle di sistema), non le nostre. PRIMA: i
    pacchetti dell'elenco precedente"""
    out = []
    for r in righe or []:
        try:
            pkg, uid, stato, ultimo, vis = str(r[0]), int(r[1]), int(r[2]), int(r[3]), bool(r[4])
        except (TypeError, ValueError, IndexError):
            continue
        if pkg not in note or pkg.startswith(NOSTRE):
            continue
        s = stato_android(uid, stato, vis, pkg in prima)
        if s:
            out.append({"pkg": pkg, "stato": s, "ultimo": ultimo})
    return out


def dex_pronto(sorgente=ANDROID_DEX, cartella=None):
    """la copia in sola lettura del .dex (da Android 14 app_process rifiuta i dex scrivibili) → percorso, o None"""
    cartella = cartella or os.path.join(os.environ.get("TOD_TERMUX_HOME") or os.path.expanduser("~"),
                                        ".local/share/tod-android")
    try:
        with open(sorgente, "rb") as f:
            dati = f.read()
    except OSError:
        return None
    dest = os.path.join(cartella, "tod-app-android.dex")
    try:
        with open(dest, "rb") as f:
            if f.read() == dati:
                return dest
    except OSError:
        pass
    try:
        os.makedirs(cartella, exist_ok=True)
        tmp = f"{dest}.{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(dati)
        os.chmod(tmp, 0o400)
        os.replace(tmp, dest)
        return dest
    except OSError:
        return None


class Telefono:
    """l'aiutante acceso finché la Gestione attività è aperta (avviarlo costa ~1 s): "stato" → una riga JSON.
    chiedi() → {"ok": True, "app": [...]} | {"ok": False, "errore": "permesso" | ...} | None (aiutante assente o muto)"""

    def __init__(self, comando=None, env=None):
        finto = os.environ.get("TOD_ATTIVITA_TELEFONO")   # per le prove a vista: un aiutante finto
        if comando is None and finto:
            import shlex
            comando = shlex.split(finto)
        self.comando, self.env, self.p = comando, env, None

    def _avvia(self):
        if self.comando is not None:
            cmd, env = self.comando, self.env
        else:
            dex = dex_pronto()
            if dex is None or not os.path.exists("/system/bin/app_process"):
                return False
            # come "am" di Termux: senza i moduli del desktop (LD_PRELOAD) dentro il Java di Android
            env = {k: v for k, v in os.environ.items() if k not in ("LD_PRELOAD", "LD_LIBRARY_PATH")}
            env["CLASSPATH"] = dex
            cmd = ["/system/bin/app_process", "-Xnoimage-dex2oat", "/", "com.termuxondex.AppAndroid"]
        try:
            self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      env=env, start_new_session=True)
        except OSError:
            self.p = None
            return False
        return True

    def chiedi(self, attesa=10.0):
        if self.p is None or self.p.poll() is not None:
            if not self._avvia():
                return None
        try:
            self.p.stdin.write(b"stato\n")
            self.p.stdin.flush()
            pronto, _, _ = select.select([self.p.stdout], [], [], attesa)
            riga = self.p.stdout.readline() if pronto else b""
            if not riga:
                raise OSError("l'aiutante non risponde")
            return json.loads(riga)
        except (OSError, ValueError):
            self.ferma()
            return None

    def ferma(self):
        """subito: l'aiutante non ha niente da salvare (alla chiusura della finestra non si aspetta Java che finisce)"""
        p, self.p = self.p, None
        if p is None:
            return
        try:
            p.kill()
            p.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
