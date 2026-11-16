"""priorita_dati.py — priorità e memoria del desktop, senza grafica (15 novembre 2026): lo usano share/tod-priorita.py
(il servizio della sessione), share/prova_fluidita.py (la misura) e i test.

Pietro: "mi interessa che ci sia una gestione della memoria tale da mantenere sempre tutto fluido anche quando sotto
sforzo". Misurato quel giorno: Android dà a Termux, che è in primo piano, la priorità -10, e ogni programma avviato dal
desktop la eredita: 89 processi alla pari del server grafico e del gestore delle finestre, compresi i lavori pesanti
(compilazioni, prove, Chromium, app Electron). Con il processore frenato dal calore lo schermo scattava insieme a tutto.
Senza root Termux può cambiare la priorità dei suoi processi (RLIMIT_NICE 40: da -20 a 19, in su e in giù) e dire chi
sacrificare per primo se la memoria finisce (oom_score_adj, da 0 in su).

Le classi, in ordine:
  schermo   ciò che fa muovere lo schermo: gestore delle finestre, barra, Start, audio... → non si tocca (-10)
  pesante   compilatori, apt, prove, schermi finti... e tutto ciò che lanciano → nice 10
  attiva    l'albero della finestra attiva → torna alla priorità di partenza (-10)
  app       l'albero delle altre finestre → nice 0
  altro     i servizi senza finestra → nice 0
L'app Android (com.termux, con dentro Termux:X11) la gestisce Android: mai toccata.

  leggi_processi(proc, nomi)               {pid: Processo}, leggero (niente memoria per processo)
  classi(processi, attiva, finestre)       {pid: classe}
  nuovo_nice(classe, attuale, partenza)    il nice da dare a un filo, o None
  OOM                                      oom_score_adj per classe
  da_liberare(...)                         se chiedere ad Android di chiudere le app del telefono ferme in cache
"""
import os

SCHERMO = {"xfwm4", "tod-barra.py", "tod-pannelli.py", "tod-pannello", "tod-start.py", "tod-edges.py", "pulseaudio",
           "dbus-daemon", "xfdesktop", "xfsettingsd", "tod-va-chip", "tod-cattura.py", "tod-avvisi-host.py",
           "tod-priorita.py",
           # senza Adreno tutto l'OpenGL del desktop passa dal server virgl (e dai suoi figli, uno per app): se
           # scende di priorità scatta ogni finestra, se Android lo sacrifica si chiudono tutte le app con la GPU
           "virgl_test_server_android", "virgl_test_server", "virgl_test_serv", "virgl_render_server",
           "virgl_render_se"}
PESANTI = {"clang", "clang++", "cc1", "cc1plus", "ld", "ld.lld", "lld", "make", "ninja", "cmake", "cargo", "rustc",
           "gcc", "g++", "d8", "javac", "apt", "apt-get", "dpkg", "pkg", "graphify", "Xvfb", "du", "aapt2",
           "rsvg-convert", "tar", "xz", "zstd", "gzip", "unzip", "nmap", "run.sh", "prova.sh", "compila.sh",
           "tod-updates.py", "tumblerd", "pip", "npm", "node-gyp"}
INTOCCABILI = {"com.termux",   # il processo dell'app Android: lo gestisce Android
               "prova-carico", "prova_fluidita.py"}   # la misura sceglie da sé le priorità che confronta
NICE = {"pesante": 10, "app": 0, "altro": 0}
OOM = {"schermo": 0, "attiva": 0, "app": 300, "altro": 500, "pesante": 800}


class Processo:
    __slots__ = ("pid", "genitore", "nome", "comm", "inizio")

    def __init__(self, pid, genitore, nome, comm, inizio=0):
        self.pid, self.genitore, self.nome, self.comm, self.inizio = pid, genitore, nome, comm, inizio


def leggi_processi(proc="/proc", nomi=None):
    """{pid: Processo} dei processi nostri, leggero (solo stat e, la prima volta, cmdline: niente memoria, a
    differenza della Gestione attività). NOMI: {(pid, avvio): nome}, per non rileggere la riga di comando."""
    import sys
    qui = os.path.dirname(os.path.abspath(__file__))
    if qui not in sys.path:
        sys.path.insert(1, qui)
    import attivita_dati as D
    out = {}
    try:
        voci = os.listdir(proc)
    except OSError:
        return out
    for n in voci:
        if not n.isdigit():
            continue
        try:
            with open(f"{proc}/{n}/stat") as f:
                st = f.read()
            comm = st[st.index("(") + 1:st.rindex(")")]
            campi = st.rsplit(")", 1)[1].split()
            genitore, inizio = int(campi[1]), int(campi[19])
        except (OSError, ValueError, IndexError):
            continue
        chiave = (int(n), inizio)
        nome = nomi.get(chiave) if nomi is not None else None
        if nome is None:
            try:
                with open(f"{proc}/{n}/cmdline", "rb") as f:
                    argv = [x.decode("utf-8", "replace") for x in f.read().split(b"\0")]
            except OSError:
                argv = []
            nome = D.nome_processo(argv, comm)
            if nomi is not None:
                nomi[chiave] = nome
        out[int(n)] = Processo(int(n), genitore, nome, comm, inizio)
    return out


def classi(processi, attiva, finestre):
    """processi: {pid: Processo} (genitore, nome, comm); attiva: pid della finestra attiva (o None); finestre: i pid
    delle finestre aperte. Una classe per ogni processo, ereditata dagli antenati: un compilatore lanciato da un
    terminale è "pesante" anche se il terminale è la finestra attiva."""
    memo = {}

    def nome(p):
        return p.nome

    def di(pid, passi=0):
        if pid in memo:
            return memo[pid]
        p = processi.get(pid)
        if p is None or passi > 64:
            return None
        n = nome(p)
        if n in INTOCCABILI or p.comm in INTOCCABILI:
            c = None
        elif n in SCHERMO or p.comm in SCHERMO:
            c = "schermo"
        elif n in PESANTI or p.comm in PESANTI:
            c = "pesante"
        elif pid == attiva:
            c = "attiva"
        elif pid in finestre:
            c = "app"
        else:
            sopra = di(p.genitore, passi + 1)
            c = sopra if sopra in ("pesante", "attiva", "app") else "altro"
        memo[pid] = c
        return c

    return {pid: di(pid) for pid in processi}


def nuovo_nice(classe, attuale, partenza):
    """il nice da dare a un filo, o None per lasciarlo com'è.
    - schermo e intoccabili: mai;
    - pesante, app, altro: si abbassa soltanto (un filo già più in basso, come le schede nascoste di Chromium, resta);
    - attiva: torna alla priorità di partenza del processo, ma solo dai valori che mettiamo noi (0 e 10): un filo che
      l'app ha abbassato da sé resta dov'è; e mai più in alto di com'era all'inizio."""
    if classe in NICE:
        return NICE[classe] if attuale < NICE[classe] else None
    if classe == "attiva" and partenza is not None and partenza < attuale and attuale in NICE.values():
        return partenza
    return None


def da_liberare(disponibile_kb, totale_kb, ultima, ora, soglia=0.12, pausa=120.0):
    """sotto il 12% di memoria disponibile, al più una volta ogni 2 minuti"""
    return totale_kb > 0 and disponibile_kb < totale_kb * soglia and ora - ultima >= pausa


def nice_di(tid, proc="/proc"):
    try:
        with open(f"{proc}/{tid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[16])
    except (OSError, ValueError, IndexError):
        return None


def fili(pid, proc="/proc"):
    try:
        return [int(t) for t in os.listdir(f"{proc}/{pid}/task") if t.isdigit()]
    except OSError:
        return []
