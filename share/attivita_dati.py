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
"""
import os
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
    import json
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

