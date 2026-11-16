#!/data/data/com.termux/files/usr/bin/python3
"""tod-priorita.py — il servizio che tiene fluido lo schermo quando il telefono è sotto sforzo (15 novembre 2026).

Pietro: "mi interessa che ci sia una gestione della memoria tale da mantenere sempre tutto fluido anche quando sotto
sforzo". Due lavori (la logica è in share/priorita_dati.py):
  priorità   ogni 3 s, e subito quando cambia la finestra attiva: lo schermo (gestore delle finestre, barra, audio...)
             e l'app attiva restano in alto, le altre app e i servizi a nice 0, i lavori pesanti a nice 10; chi
             sacrificare per primo se la memoria finisce (oom_score_adj) va di pari passo
  memoria    sotto il 12% di memoria disponibile, con ADB collegato (share/tod-adb.py), si chiede ad Android di chiudere
             le app del telefono ferme in cache ("am kill-all": solo quelle che Android stesso chiuderebbe per prime);
             senza ADB un avviso, al più ogni mezz'ora

  tod-priorita.py          il servizio (dall'hook di sessione, lib/priorita.sh; uno solo)
  tod-priorita.py stato    quanti processi per classe e a che priorità, memoria, servizio acceso o no
  tod-priorita.py ferma    ferma il servizio, che rimette le priorità di partenza
"""
import ctypes
import ctypes.util
import fcntl
import os
import signal
import subprocess
import sys
import time

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(1, QUI)
import attivita_dati as D  # noqa: E402
import priorita_dati as P  # noqa: E402

PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
TMP = os.environ.get("TMPDIR", os.path.join(PREFIX, "tmp"))
PIDFILE = os.path.join(TMP, "tod-priorita.pid")
ITALIAN = os.environ.get("LANGUAGE", os.environ.get("LANG", "it")).startswith("it")
OGNI = 3.0          # secondi tra un giro completo e l'altro
FINESTRA = 0.5      # quanto spesso si guarda la finestra attiva
MEMORIA = 5.0       # quanto spesso si guarda la memoria


def T(it, en):
    return it if ITALIAN else en


def registro(testo):
    print(f"{time.strftime('%F %T')} {testo}", flush=True)


class X:
    """finestra attiva e finestre aperte con Xlib via ctypes: niente xprop due volte al secondo"""

    def __init__(self):
        x = ctypes.CDLL(ctypes.util.find_library("X11") or os.path.join(PREFIX, "lib/libX11.so"))
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XInternAtom.restype = ctypes.c_ulong
        x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x.XGetWindowProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long,
                                         ctypes.c_long, ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                                         ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
                                         ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p)]
        x.XFree.argtypes = [ctypes.c_void_p]
        # una finestra chiusa mentre la si legge darebbe BadWindow, e il gestore di serie chiude il programma
        self._errori = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda d, e: 0)
        x.XSetErrorHandler(self._errori)
        self.x = x
        self.d = x.XOpenDisplay(None)
        if not self.d:
            raise OSError("nessuno schermo (DISPLAY)")
        self.radice = x.XDefaultRootWindow(self.d)
        self.atomi = {n: x.XInternAtom(self.d, n.encode(), 0)
                      for n in ("_NET_ACTIVE_WINDOW", "_NET_CLIENT_LIST", "_NET_WM_PID")}

    def numeri(self, finestra, nome):
        tipo, formato = ctypes.c_ulong(), ctypes.c_int()
        n, resto, dati = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_void_p()
        if self.x.XGetWindowProperty(self.d, finestra, self.atomi[nome], 0, 4096, 0, 0, ctypes.byref(tipo),
                                     ctypes.byref(formato), ctypes.byref(n), ctypes.byref(resto),
                                     ctypes.byref(dati)) != 0 or not dati.value:
            return []
        try:
            if formato.value != 32:
                return []
            return list(ctypes.cast(dati, ctypes.POINTER(ctypes.c_ulong))[:n.value])
        finally:
            self.x.XFree(dati)

    def pid(self, finestra):
        p = self.numeri(finestra, "_NET_WM_PID") if finestra else []
        return int(p[0]) if p else None

    def attiva(self):
        f = self.numeri(self.radice, "_NET_ACTIVE_WINDOW")
        return self.pid(f[0]) if f else None

    def finestre(self):
        return {p for p in (self.pid(f) for f in self.numeri(self.radice, "_NET_CLIENT_LIST")) if p}


class Servizio:
    def __init__(self):
        self.nomi = {}        # (pid, avvio) → nome
        self.partenza = {}    # (pid, avvio) → nice del filo principale alla prima vista
        self.visti = {}       # (pid, avvio) → (classe, fili già sistemati)
        self.ultima_mem, self.pausa_mem, self.ultimo_avviso, self.avvisato = -1e9, 120.0, -1e9, False

    # --- priorità ---------------------------------------------------------------------------------------------------
    def giro(self, attiva, finestre):
        proc = P.leggi_processi(nomi=self.nomi)
        cl = P.classi(proc, attiva, finestre)
        vivi = set()
        for pid, c in cl.items():
            chiave = (pid, proc[pid].inizio)
            vivi.add(chiave)
            if c is None:
                continue
            if chiave not in self.partenza:
                self.partenza[chiave] = P.nice_di(pid)
            classe_prima, fatti = self.visti.get(chiave, (None, set()))
            if classe_prima != c:
                fatti = set()
                self.oom(pid, c)
            for t in P.fili(pid):
                if t in fatti:
                    continue
                n = P.nice_di(t)
                nuovo = P.nuovo_nice(c, n, self.partenza[chiave]) if n is not None else None
                if nuovo is not None:
                    try:
                        os.setpriority(os.PRIO_PROCESS, t, nuovo)
                    except OSError:
                        pass
                fatti.add(t)
            self.visti[chiave] = (c, fatti)
        for k in [k for k in self.visti if k not in vivi]:
            self.visti.pop(k, None)
            self.partenza.pop(k, None)
        for k in [k for k in self.nomi if k not in vivi]:
            self.nomi.pop(k, None)

    @staticmethod
    def oom(pid, classe):
        if classe in P.OOM:
            try:
                with open(f"/proc/{pid}/oom_score_adj", "w") as f:
                    f.write(str(P.OOM[classe]))
            except OSError:
                pass

    def rimetti(self):
        """alla fine: le priorità di partenza ai fili che abbiamo abbassato, oom_score_adj a 0"""
        for (pid, _avvio), (classe, _fatti) in list(self.visti.items()):
            partenza = self.partenza.get((pid, _avvio))
            if classe is None or partenza is None:
                continue
            for t in P.fili(pid):
                n = P.nice_di(t)
                if n is not None and n in P.NICE.values() and partenza < n:
                    try:
                        os.setpriority(os.PRIO_PROCESS, t, partenza)
                    except OSError:
                        pass
            self.oom(pid, "schermo")

    # --- memoria ----------------------------------------------------------------------------------------------------
    def memoria(self, ora):
        tot, disp = D.memoria_telefono()
        if not P.da_liberare(disp, tot, self.ultima_mem, ora, pausa=self.pausa_mem):
            return
        self.ultima_mem = ora
        import adb_dati as A
        seriale = A.collegato() if A.presente() else None
        if seriale:
            A.adb("-s", seriale, "shell", "am", "kill-all", attesa=20)
            time.sleep(3)
            dopo = D.memoria_telefono()[1]
            liberati = max(0, dopo - disp) // 1024
            registro(f"memoria: {disp // 1024} MB disponibili su {tot // 1024}: chiuse le app del telefono in cache "
                     f"(am kill-all), +{liberati} MB")
            # se non è servito (la memoria la tengono le app del desktop), non si ripete ogni 2 minuti
            self.pausa_mem = 120.0 if liberati >= 100 else 600.0
            if not self.avvisato and liberati >= 100:
                self.avvisato = True
                avviso(T("Memoria liberata", "Memory freed"),
                       T(f"Ho chiuso le app del telefono ferme in pausa: {liberati} MB in più per il desktop.",
                         f"I closed the phone apps that were paused: {liberati} MB more for the desktop."))
        elif ora - self.ultimo_avviso >= 1800:
            self.ultimo_avviso = ora
            registro(f"memoria: {disp // 1024} MB disponibili su {tot // 1024}, ADB non collegato: solo un avviso")
            avviso(T("Memoria quasi finita", "Memory almost full"),
                   T("Chiudi qualche app che non usi: la Gestione attività mostra anche quelle del telefono.",
                     "Close some apps you aren't using: Task Manager also shows the phone's apps."))


def avviso(titolo, testo):
    try:
        subprocess.Popen(["notify-send", "-a", "Termux On DeX", "-i", "utilities-system-monitor", titolo, testo],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def servizio():
    blocco = open(os.path.join(TMP, "tod-priorita.lock"), "w")
    try:   # uno solo per sessione
        fcntl.flock(blocco, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    try:
        x = X()
    except OSError as e:
        registro(f"priorità: {e}")
        return 1
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    s = Servizio()
    fine = []
    signal.signal(signal.SIGTERM, lambda *_: fine.append(1))
    registro("priorità: servizio avviato")
    attiva_prima, giro, mem = object(), -1e9, -1e9
    try:
        while not fine:
            ora = time.monotonic()
            attiva = x.attiva()
            if attiva != attiva_prima or ora - giro >= OGNI:
                s.giro(attiva, x.finestre())
                attiva_prima, giro = attiva, ora
            if ora - mem >= MEMORIA:
                mem = ora
                s.memoria(ora)
            time.sleep(FINESTRA)
    finally:
        s.rimetti()
        registro("priorità: servizio fermato, priorità di partenza rimesse")
        try:
            if open(PIDFILE).read().strip() == str(os.getpid()):
                os.unlink(PIDFILE)
        except OSError:
            pass
    return 0


def stato():
    import collections
    proc = P.leggi_processi()
    try:
        attiva, finestre = (lambda x: (x.attiva(), x.finestre()))(X())
    except OSError:
        attiva, finestre = None, set()
    cl = P.classi(proc, attiva, finestre)
    conti = collections.Counter(c or "android" for c in cl.values())
    nomi = {"schermo": T("schermo", "screen"), "attiva": T("app attiva", "active app"), "app": T("altre app", "other apps"),
            "altro": T("servizi", "services"), "pesante": T("lavori pesanti", "heavy work"),
            "android": T("app Android (non toccata)", "Android app (untouched)")}
    acceso = False
    try:
        pid = int(open(PIDFILE).read().strip())
        acceso = b"tod-priorita.py" in open(f"/proc/{pid}/cmdline", "rb").read()
    except (OSError, ValueError):
        pass
    print(T("Servizio delle priorità: ", "Priority service: ") + (T("acceso", "on") if acceso else T("spento", "off")))
    for c in ("schermo", "attiva", "app", "altro", "pesante", "android"):
        nice = sorted({P.nice_di(p) for p, k in cl.items() if (k or "android") == c} - {None})
        if conti[c]:
            print(f"  {nomi[c]:28} {conti[c]:4d}   nice {', '.join(map(str, nice))}")
    tot, disp = D.memoria_telefono()
    if tot:
        print(T(f"Memoria disponibile: {disp // 1024} MB su {tot // 1024} MB ({disp * 100 // tot}%)",
                f"Available memory: {disp // 1024} MB of {tot // 1024} MB ({disp * 100 // tot}%)"))
    return 0


def ferma():
    try:
        pid = int(open(PIDFILE).read().strip())
        if b"tod-priorita.py" in open(f"/proc/{pid}/cmdline", "rb").read():
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    comando = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit({"stato": stato, "status": stato, "ferma": ferma, "stop": ferma}.get(comando, servizio)())
