#!/usr/bin/env python3
"""Stampanti del desktop: il servizio di stampa (CUPS di Debian) e le stampanti Wi-Fi e USB, aggiunte da sole.

CUPS gira nel container, senza root e senza proot (`tod cx run -- cupsd`), con la configurazione in
~/.config/tod/stampa e il socket in $TMPDIR/tod-stampa/cups.sock. client.conf di Termux e del container puntano
lì (lib/stampa.sh), quindi la finestra di stampa di ogni app (GTK, LibreOffice, Chromium, Electron) e `lp`
vedono le stesse stampanti. Le converte Debian (cups-filters, Ghostscript): stampanti IPP Everywhere, AirPrint
e Mopria senza driver.

- Wi-Fi: domande Bonjour (mDNS) dirette, con risposta diretta. Android scarta gli annunci a tutta la rete
  senza un "multicast lock", ma una domanda con il bit QU riceve la risposta solo a chi la fa.
- USB: tod-usb (share/usb/tod-usb) apre la stampante con termux-usb e un ponte (tod-stampante-usb.py) la offre
  qui su 127.0.0.1, descritta in $TMPDIR/tod-stampa/usb/<id>.json.

Una stampante tolta dall'utente non si riaggiunge da sola (quelle USB sì, alla prossima volta che si collegano).

  tod-stampa.py sorveglia             servizio della sessione (dall'hook di lib/session.sh)
  tod-stampa.py ferma                 fine della sessione: servizio e CUPS chiusi
  tod-stampa.py stato
  tod-stampa.py cerca [--json]        stampanti in rete
  tod-stampa.py elenco [--json]       stampanti installate, per le Impostazioni
  tod-stampa.py aggiungi INDIRIZZO    ipp://..., un indirizzo IP o un nome; o la chiave di una trovata in rete
  tod-stampa.py togli NOME | predefinita NOME | prova NOME | annulla LAVORO|NOME
"""
import fcntl
import glob
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import time

PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
TMP = os.environ.get("TMPDIR", os.path.join(PREFIX, "tmp"))
TH = os.environ.get("TOD_TERMUX_HOME") or os.environ.get("HOME", "")
RUN = os.path.join(TMP, "tod-stampa")
SOCK = os.path.join(RUN, "cups.sock")
USB = os.path.join(RUN, "usb")
CONF = os.path.join(TH, ".config/tod/stampa")
STATO = os.path.join(TH, ".local/state/tod/stampa")
CACHE = os.path.join(TH, ".cache/tod/stampa")
DB = os.path.join(STATO, "stampanti.json")
TOD = os.environ.get("TOD_BIN", "tod")
ITALIAN = os.environ.get("LANGUAGE", os.environ.get("LANG", "")).startswith("it")
RETE_OGNI = 60   # secondi tra una ricerca in rete e l'altra
PID_CUPSD = "servizio-cupsd.pid"   # il nostro `tod cx run -- cupsd` (cupsd.pid lo scrive cupsd stesso, con il suo pid)
VISTA_PER = 300  # una stampante di rete è "raggiungibile" se è stata vista negli ultimi 5 minuti


def T(it, en):
    return it if ITALIAN else en


def log(msg):
    print(f"{time.strftime('%F %T')} {msg}", flush=True)


def avvisa(titolo, testo):
    try:
        subprocess.Popen(["notify-send", "-a", T("Stampanti", "Printers"), "-i", "printer", titolo, testo],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# --- CUPS ---------------------------------------------------------------------------------------------------

# le variabili del modo diretto (lib/diretto.sh): CUPS lancia filtri e collegamenti con un ambiente ripulito, e
# senza queste partono senza fakechroot né tod-diretto.so e Android li ferma alla prima chiamata vietata
# (rseq: segnale 31). In cups-files.conf: in cupsd.conf CUPS 2.4 le ignora ("Please move", 6 novembre)
PASSA = ("LD_PRELOAD LD_LIBRARY_PATH GLIBC_TUNABLES GCONV_PATH TZ TZDIR FAKECHROOT FAKECHROOT_BASE "
         "FAKECHROOT_ELFLOADER FAKECHROOT_ELFLOADER_OPT_ARGV0 FAKECHROOT_EXCLUDE_PATH FAKECHROOT_VERSION "
         "TOD_FS_RUN TOD_DIRETTO_PROOT TOD_DIRETTO_THOME")


def testo_cupsd():
    # un CUPS dell'utente, su un socket in una cartella sua (0700): nessuna autenticazione
    # ErrorPolicy retry-job: una stampante USB staccata o Wi-Fi spenta non resta "ferma", il lavoro aspetta
    return f"""# Termux On DeX (share/stampa/tod-stampa.py): riscritto a ogni avvio del servizio
LogLevel warn
MaxLogSize 1m
Listen {SOCK}
Browsing Off
DefaultAuthType None
WebInterface No
DefaultShared No
ErrorPolicy retry-job
JobRetryInterval 20
JobRetryLimit 180
PreserveJobHistory 1d
PreserveJobFiles No
<Location />
  Order allow,deny
  Allow all
</Location>
<Policy default>
  JobPrivateAccess all
  JobPrivateValues none
  SubscriptionPrivateAccess all
  SubscriptionPrivateValues none
  <Limit All>
    Order deny,allow
  </Limit>
</Policy>
"""


def testo_files():
    return f"""# Termux On DeX (share/stampa/tod-stampa.py): riscritto a ogni avvio del servizio
PassEnv {PASSA}
ServerRoot {CONF}
RequestRoot {STATO}/spool
CacheDir {CACHE}
StateDir {RUN}
TempDir {RUN}/tmp
AccessLog {RUN}/access_log
ErrorLog {STATO}/error_log
PageLog {RUN}/page_log
FileDevice No
Sandboxing Relaxed
"""


def scrivi(percorso, testo):
    try:
        with open(percorso) as f:
            if f.read() == testo:
                return
    except OSError:
        pass
    with open(percorso + ".nuovo", "w") as f:
        f.write(testo)
    os.replace(percorso + ".nuovo", percorso)


# le stampanti solo AirPrint: il PDF diventa Apple Raster con share/stampa/tod-pdftourf (CUPS legge i *.convs della
# sua ServerRoot)
CONVS = "# Termux On DeX (share/stampa/tod-pdftourf)\napplication/vnd.cups-pdf\timage/urf\t100\ttod-pdftourf\n"


def scrivi_conf():
    for d in (RUN, os.path.join(RUN, "tmp"), USB, os.path.join(CONF, "ppd"), os.path.join(STATO, "spool"), CACHE):
        os.makedirs(d, exist_ok=True)
    os.chmod(RUN, 0o700)
    scrivi(os.path.join(CONF, "cupsd.conf"), testo_cupsd())
    scrivi(os.path.join(CONF, "cups-files.conf"), testo_files())
    scrivi(os.path.join(CONF, "tod.convs"), CONVS)


def ambiente():
    # i programmi di CUPS di Termux (lp, lpstat, lpadmin) parlano con il nostro CUPS; in inglese, per leggerli
    a = dict(os.environ, CUPS_SERVER=SOCK, LANG="C", LC_ALL="C")
    a.pop("LANGUAGE", None)
    return a


def cups(*args, timeout=90):
    try:
        return subprocess.run(list(args), env=ambiente(), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def risponde():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(SOCK)
        return True
    except OSError:
        return False
    finally:
        s.close()


def pid_di(nome, parola):
    """Il pid scritto in RUN/nome, se è vivo e la sua riga di comando contiene parola."""
    try:
        pid = int(open(os.path.join(RUN, nome)).read().strip())
        riga = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ")
    except (OSError, ValueError):
        return None
    return pid if parola.encode() in riga else None


def cupsd_orfani():
    """I cupsd con la nostra configurazione rimasti senza servizio (una sessione chiusa male): per argomenti
    esatti, mai per un testo che può stare nel comando di qualcun altro."""
    conf = b"\0-c\0" + os.path.join(CONF, "cupsd.conf").encode() + b"\0"
    vivo = pid_di(PID_CUPSD, "cupsd")
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == vivo:
            continue
        try:
            riga = open(f"/proc/{d}/cmdline", "rb").read()
        except OSError:
            continue
        if conf in riga and b"cupsd" in riga:
            try:
                os.kill(int(d), signal.SIGTERM)
                log(f"chiuso un cupsd rimasto ({d})")
            except OSError:
                pass


def avvia_cupsd():
    if pid_di(PID_CUPSD, "cupsd") and risponde():
        return True
    cupsd_orfani()
    scrivi_conf()
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    out = open(os.path.join(STATO, "cupsd.log"), "ab")
    # gruppo di processi suo: alla chiusura si ferma tutto, anche il cupsd che `tod cx run` ha lanciato
    p = subprocess.Popen([TOD, "cx", "run", "--", "cupsd", "-f", "-c", os.path.join(CONF, "cupsd.conf"),
                          "-s", os.path.join(CONF, "cups-files.conf")],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
    with open(os.path.join(RUN, PID_CUPSD), "w") as f:
        f.write(str(p.pid))
    for _ in range(60):
        if risponde():
            log(f"CUPS acceso (pid {p.pid})")
            return True
        if p.poll() is not None:
            break
        time.sleep(0.5)
    log("CUPS non parte (registro in " + os.path.join(STATO, "cupsd.log") + ")")
    return False


def ferma_gruppo(pid):
    """Il processo e il suo gruppo (`tod cx run` e il cupsd che ha lanciato)."""
    try:
        g = os.getpgid(pid)
        os.killpg(g, signal.SIGTERM)
    except OSError:
        return
    for _ in range(20):
        try:
            os.killpg(g, 0)
        except OSError:
            return
        time.sleep(0.25)
    try:
        os.killpg(g, signal.SIGKILL)
    except OSError:
        pass


# --- stampanti installate --------------------------------------------------------------------------------------

def carica_db():
    try:
        d = json.load(open(DB))
    except (OSError, ValueError):
        d = {}
    d.setdefault("stampanti", {})   # chiave → nome della coda, indirizzo, tipo, modello
    d.setdefault("tolte", [])       # chiavi tolte dall'utente: non si riaggiungono da sole
    d.setdefault("viste", {})       # chiave → ultima volta vista in rete (o collegata via USB)
    return d


def salva_db(d):
    os.makedirs(STATO, exist_ok=True)
    with open(DB + ".nuovo", "w") as f:
        json.dump(d, f, indent=1, ensure_ascii=False)
    os.replace(DB + ".nuovo", DB)


def nome_coda(testo, usati=()):
    """Il nome di una coda di CUPS (lettere, cifre, - e _, al massimo 127) da quello della stampante."""
    n = re.sub(r"[^A-Za-z0-9_-]+", "_", testo or "").strip("_")[:120] or "Stampante"
    if n not in usati:
        return n
    i = 2
    while f"{n}_{i}" in usati:
        i += 1
    return f"{n}_{i}"


def code():
    """Le code di CUPS: {nome: indirizzo}."""
    r = cups("lpstat", "-v")
    out = {}
    for riga in r.stdout.splitlines():
        m = re.match(r"device for (\S+): (.*)", riga)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def predefinita():
    m = re.search(r"system default destination: (\S+)", cups("lpstat", "-d").stdout)
    return m.group(1) if m else ""


def crea_coda(nome, uri, descr, luogo, modello="everywhere"):
    """lpadmin: una coda nuova (o aggiornata). IPP Everywhere: CUPS chiede alla stampante cosa sa fare."""
    r = cups("lpadmin", "-p", nome, "-E", "-v", uri, "-m", modello, "-D", descr, "-L", luogo,
             "-o", "printer-is-shared=false", timeout=120)
    if r.returncode:
        return (r.stderr or r.stdout).strip() or "lpadmin"
    if not predefinita():   # la prima stampante diventa quella predefinita, come su Windows
        cups("lpadmin", "-d", nome)
    return ""


# --- Wi-Fi: Bonjour (mDNS) --------------------------------------------------------------------------------------

MDNS = ("224.0.0.251", 5353)
TIPI = ("_ipp._tcp.local", "_ipps._tcp.local")
PTR, TXT, SRV, A = 12, 16, 33, 1


def nome_dns(n):
    return b"".join(bytes([len(p)]) + p.encode() for p in n.rstrip(".").split(".")) + b"\0"


def domanda(richieste):
    """Una domanda mDNS: [(nome, tipo)], tutte con il bit QU (risposta diretta a chi chiede)."""
    q = struct.pack(">HHHHHH", 0, 0, len(richieste), 0, 0, 0)
    for nome, tipo in richieste:
        q += nome_dns(nome) + struct.pack(">HH", tipo, 0x8001)
    return q


def leggi_nome(d, i, salti=0):
    parti = []
    while i < len(d):
        n = d[i]
        if n == 0:
            return ".".join(parti), i + 1
        if n & 0xC0 == 0xC0:   # puntatore: il resto del nome è altrove nel messaggio
            if salti > 20 or i + 1 >= len(d):
                break
            resto, _ = leggi_nome(d, ((n & 0x3F) << 8) | d[i + 1], salti + 1)
            return ".".join(parti + ([resto] if resto else [])), i + 2
        parti.append(d[i + 1:i + 1 + n].decode("utf-8", "replace"))
        i += 1 + n
    raise ValueError("nome DNS troncato")


def analizza(d):
    """I record di una risposta DNS: [(nome, tipo, dati)]. PTR → nome; SRV → (porta, destinazione);
    TXT → {chiave: valore}; A → indirizzo. Gli altri tipi si saltano."""
    if len(d) < 12:
        return []
    _, flag, nq, na, nn, nr = struct.unpack(">HHHHHH", d[:12])
    if not flag & 0x8000:   # una domanda (anche la nostra, rimandata dal multicast), non una risposta
        return []
    i = 12
    for _ in range(nq):
        _, i = leggi_nome(d, i)
        i += 4
    out = []
    for _ in range(na + nn + nr):
        nome, i = leggi_nome(d, i)
        if i + 10 > len(d):
            break
        tipo, _, _, lung = struct.unpack(">HHIH", d[i:i + 10])
        i += 10
        dati = d[i:i + lung]
        if tipo == PTR:
            out.append((nome, PTR, leggi_nome(d, i)[0]))
        elif tipo == SRV and lung >= 7:
            out.append((nome, SRV, (struct.unpack(">H", dati[4:6])[0], leggi_nome(d, i + 6)[0])))
        elif tipo == TXT:
            kv, j = {}, 0
            while j < len(dati):
                s = dati[j + 1:j + 1 + dati[j]].decode("utf-8", "replace")
                j += 1 + dati[j]
                if "=" in s:
                    k, v = s.split("=", 1)
                    kv[k.lower()] = v
            out.append((nome, TXT, kv))
        elif tipo == A and lung == 4:
            out.append((nome, A, socket.inet_ntoa(dati)))
        i += lung
    return out


def stampanti_da(record, sorgenti):
    """Dai record raccolti: una voce per stampante (ipp preferito a ipps, una sola per UUID)."""
    per = {}
    for nome, tipo, dati in record:
        per.setdefault(nome.lower(), {}).setdefault(tipo, dati)
    trovate = {}
    for nome, tipo, dati in record:
        if tipo != PTR or nome.lower() not in TIPI:
            continue
        ist = per.get(dati.lower(), {})
        srv, txt = ist.get(SRV), ist.get(TXT, {})
        if not srv:
            continue
        porta, dest = srv
        ip = per.get(dest.lower(), {}).get(A) or sorgenti.get(dati.lower())
        if not ip:
            continue
        sicura = nome.lower().startswith("_ipps")
        rp = txt.get("rp", "ipp/print").strip("/")
        p = {
            "chiave": (txt.get("uuid") or f"{ip}/{rp}").lower(),
            "nome": dati.split("._ipp")[0],
            "modello": txt.get("ty") or txt.get("product", "").strip("()") or dati.split("._ipp")[0],
            "uri": f"{'ipps' if sicura else 'ipp'}://{ip}:{porta}/{rp}",
            "ip": ip,
            "colore": txt.get("color", "").upper() == "T",
            "fronte_retro": txt.get("duplex", "").upper() == "T",
            # IPP Everywhere (PWG raster), PDF o AirPrint (URF, con tod-pdftourf): niente driver
            "senza_driver": bool(re.search(r"image/pwg-raster|application/pdf|image/urf", txt.get("pdl", ""), re.I)
                                 or txt.get("urf")),
            "sicura": sicura,
        }
        vecchia = trovate.get(p["chiave"])
        if not vecchia or (vecchia["sicura"] and not sicura):
            trovate[p["chiave"]] = p
    return list(trovate.values())


def cerca(attesa=2.0):
    """Le stampanti IPP in rete (Bonjour). [] senza rete locale (dati mobili): nessuno risponde."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    record, sorgenti = [], {}

    def raccogli(secondi):
        fine = time.time() + secondi
        while True:
            resta = fine - time.time()
            if resta <= 0:
                return
            s.settimeout(resta)
            try:
                d, (ip, _) = s.recvfrom(9000)
            except OSError:
                return
            try:
                rr = analizza(d)
            except (ValueError, IndexError, struct.error):
                continue
            record.extend(rr)
            for nome, tipo, dati in rr:
                if tipo in (SRV, TXT):
                    sorgenti.setdefault(nome.lower(), ip)

    try:
        s.bind(("", 0))
        for _ in range(2):
            s.sendto(domanda([(t, PTR) for t in TIPI]), MDNS)
            raccogli(attesa / 2)
        # chi ha risposto solo con il nome: si chiedono indirizzo e dettagli
        nomi = {d for n, t, d in record if t == PTR and n.lower() in TIPI}
        con_srv = {n.lower() for n, t, _ in record if t == SRV}
        manca = [(n, SRV) for n in nomi if n.lower() not in con_srv] + [(n, TXT) for n in nomi if n.lower() not in con_srv]
        dest = {d[1] for n, t, d in record if t == SRV}
        con_a = {n.lower() for n, t, _ in record if t == A}
        manca += [(d, A) for d in dest if d.lower() not in con_a]
        if manca:
            s.sendto(domanda(manca[:30]), MDNS)
            raccogli(attesa / 2)
    except OSError as e:
        log(f"ricerca in rete: {e}")
    finally:
        s.close()
    return stampanti_da(record, sorgenti)


def aggiungi_rete(db, p, da_utente=False):
    if p["chiave"] in db["stampanti"]:
        st = db["stampanti"][p["chiave"]]
        if st["uri"] != p["uri"] and st["nome"] in code():   # la stampante ha cambiato indirizzo (DHCP)
            cups("lpadmin", "-p", st["nome"], "-v", p["uri"])
            log(f"{st['nome']}: indirizzo nuovo {p['uri']}")
            st["uri"] = p["uri"]
        return ""
    if p["chiave"] in db["tolte"] and not da_utente:
        return ""
    nome = nome_coda(p["modello"], code())
    err = crea_coda(nome, p["uri"], p["modello"], "Wi-Fi")
    if err:
        log(f"non aggiunta {p['modello']} ({p['uri']}): {err}")
        return err
    db["stampanti"][p["chiave"]] = {"nome": nome, "uri": p["uri"], "tipo": "rete", "modello": p["modello"]}
    if p["chiave"] in db["tolte"]:
        db["tolte"].remove(p["chiave"])
    log(f"aggiunta {nome} ({p['uri']})")
    avvisa(p["modello"], T("Stampante pronta: la trovi nella finestra di stampa di ogni app.",
                           "Printer ready: it's in every app's print dialog."))
    return ""


def giro_rete(db):
    trovate = cerca()
    adesso = int(time.time())
    for p in trovate:
        db["viste"][p["chiave"]] = adesso
        if p["senza_driver"]:
            aggiungi_rete(db, p)
    with open(os.path.join(RUN, "trovate.json.nuovo"), "w") as f:
        json.dump(trovate, f, ensure_ascii=False)
    os.replace(os.path.join(RUN, "trovate.json.nuovo"), os.path.join(RUN, "trovate.json"))
    return trovate


# --- USB (i ponti di tod-stampante-usb.py) ----------------------------------------------------------------------

def driver_per(device_id):
    """Il driver di una stampante USB senza IPP, dall'ID IEEE 1284 (MFG, MDL, CMD): quello che CUPS propone
    tra i driver installati, sennò quello generico per PostScript o PCL. "" = nessuno."""
    r = cups("lpinfo", "--device-id", device_id, "-m", timeout=60)
    for riga in r.stdout.splitlines():
        nome = riga.split(" ", 1)[0]
        if nome and nome != "everywhere" and not nome.startswith("raw"):
            return nome
    cmd = ""
    for parte in device_id.split(";"):
        k, _, v = parte.partition(":")
        if k.strip().upper() in ("CMD", "COMMAND SET"):
            cmd = v.upper()
    tutti = cups("lpinfo", "-m", timeout=60).stdout
    for chiave, cerca_ppd in (("POSTSCRIPT", r"^(\S*generic\S*\.ppd)\s.*PostScript"), ("PCL", r"^(\S*generpcl\S*)\s")):
        if chiave in cmd:
            m = re.search(cerca_ppd, tutti, re.M | re.I)
            if m:
                return m.group(1)
    return ""


def giro_usb(db, collegate_prima):
    """Code per le stampanti USB collegate adesso. Ritorna l'insieme delle loro chiavi."""
    collegate = set()
    for f in sorted(glob.glob(os.path.join(USB, "*.json"))):
        try:
            u = json.load(open(f))
        except (OSError, ValueError):
            continue
        chiave = "usb:" + (u.get("seriale") or f"{u.get('vendor')}:{u.get('product')}").lower()
        collegate.add(chiave)
        db["viste"][chiave] = int(time.time())
        uri = (f"ipp://127.0.0.1:{u['porta']}/ipp/print" if u.get("tipo") == "ipp"
               else f"socket://127.0.0.1:{u['porta']}")
        st = db["stampanti"].get(chiave)
        if st and st["nome"] in code():
            if st["uri"] != uri:   # ricollegata: il ponte ha una porta nuova
                cups("lpadmin", "-p", st["nome"], "-v", uri)
                cups("cupsenable", st["nome"])
                st["uri"] = uri
            continue
        if chiave in db["tolte"] and chiave in collegate_prima:
            continue   # tolta mentre era collegata: si riaggiunge solo alla prossima volta che la si collega
        nome_vis = u.get("nome") or T("Stampante USB", "USB printer")
        if u.get("tipo") == "ipp":
            modello = "everywhere"
        else:
            modello = driver_per(u.get("device_id", ""))
            if not modello:
                if db.get("senza_driver", {}).get(chiave) is None:
                    db.setdefault("senza_driver", {})[chiave] = u
                    log(f"{nome_vis}: nessun driver ({u.get('device_id', '')})")
                    avvisa(nome_vis, T("Questa stampante vuole un driver: aprila da Impostazioni → Stampanti e scanner.",
                                       "This printer needs a driver: open it from Settings → Printers & scanners."))
                continue
        nome = nome_coda(nome_vis, code())
        err = crea_coda(nome, uri, nome_vis, "USB", modello)
        if err:
            log(f"non aggiunta {nome_vis} ({uri}): {err}")
            continue
        db["stampanti"][chiave] = {"nome": nome, "uri": uri, "tipo": "usb", "modello": nome_vis}
        if chiave in db["tolte"]:
            db["tolte"].remove(chiave)
        db.get("senza_driver", {}).pop(chiave, None)
        log(f"aggiunta {nome} ({uri}, {modello})")
        avvisa(nome_vis, T("Stampante USB pronta: la trovi nella finestra di stampa di ogni app.",
                           "USB printer ready: it's in every app's print dialog."))
    return collegate


# --- servizio --------------------------------------------------------------------------------------------------

def sorveglia():
    os.makedirs(RUN, exist_ok=True)
    os.makedirs(STATO, exist_ok=True)
    blocco = open(os.path.join(RUN, "sorveglia.lock"), "w")
    try:
        fcntl.flock(blocco, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0   # c'è già
    with open(os.path.join(RUN, "sorveglia.pid"), "w") as f:
        f.write(str(os.getpid()))

    def fine(*_):
        pid = pid_di(PID_CUPSD, "cupsd")
        if pid:
            ferma_gruppo(pid)
        sys.exit(0)

    signal.signal(signal.SIGTERM, fine)
    log("servizio di stampa acceso")
    db = carica_db()
    ultima_rete = 0.0
    collegate = set()
    falliti, riprova = 0, 0.0
    x = os.path.join(TMP, ".X11-unix/X0")
    while True:
        if not os.path.exists(x) and not os.environ.get("TOD_STAMPA_SENZA_X"):
            log("desktop chiuso: servizio di stampa spento")
            fine()
        if not (pid_di(PID_CUPSD, "cupsd") and risponde()):
            if time.time() >= riprova:
                if avvia_cupsd():
                    falliti = 0
                else:   # non parte: si riprova sempre più di rado (fino a 5 minuti), senza riempire il registro
                    falliti += 1
                    riprova = time.time() + min(300, 10 * 2 ** falliti)
            if not risponde():
                time.sleep(2)
                continue
        prima = json.dumps(db, sort_keys=True)
        try:
            collegate = giro_usb(db, collegate)
            for k in list(db["tolte"]):   # USB tolta e poi staccata: alla prossima volta si riaggiunge
                if k.startswith("usb:") and k not in collegate:
                    db["tolte"].remove(k)
            if time.time() - ultima_rete >= RETE_OGNI:
                ultima_rete = time.time()
                giro_rete(db)
        except Exception as e:   # un giro andato storto non deve fermare il servizio
            log(f"giro: {type(e).__name__}: {e}")
        if json.dumps(db, sort_keys=True) != prima:
            salva_db(db)
        time.sleep(2)


def ferma():
    for nome, parola in (("sorveglia.pid", "tod-stampa"), (PID_CUPSD, "cupsd")):
        pid = pid_di(nome, parola)
        if pid and nome == "sorveglia.pid":
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        elif pid:
            ferma_gruppo(pid)
    return 0


# --- comandi (tod stampa, Impostazioni) -------------------------------------------------------------------------

def elenco():
    """Le stampanti installate, con stato, lavori in coda e come sono collegate."""
    if not risponde():
        return {"servizio": False, "stampanti": []}
    db = carica_db()
    per_nome = {v["nome"]: (k, v) for k, v in db["stampanti"].items()}
    vie = code()
    pred = predefinita()
    stati, cur = {}, None
    # "printer X is idle.  enabled since …", "printer X now printing X-3.  enabled since …",
    # "printer X disabled since … -"; sotto, con un tab: il messaggio della stampante, poi Description:, Location:…
    subito = False   # la riga subito sotto quella della stampante: il suo messaggio
    for riga in cups("lpstat", "-l", "-p").stdout.splitlines():
        m = re.match(r"printer (\S+) (.*)", riga)
        if m:
            cur, resto = m.group(1), m.group(2)
            fa = re.match(r"is (\w+)", resto)
            stati[cur] = {"stato": "stampa" if resto.startswith("now printing") else (fa.group(1) if fa else ""),
                          "attiva": "disabled" not in resto, "descrizione": cur, "luogo": "", "motivo": ""}
            subito = True
            continue
        if cur and riga.startswith("\t"):
            r = riga.strip()
            if r.startswith("Description:"):
                stati[cur]["descrizione"] = r.split(":", 1)[1].strip()
            elif r.startswith("Location:"):
                stati[cur]["luogo"] = r.split(":", 1)[1].strip()
            elif subito and r and ":" not in r[:20] and r.rstrip(".") != "Idle":
                stati[cur]["motivo"] = r
        subito = False
    lavori = {}
    for riga in cups("lpstat", "-o").stdout.splitlines():
        m = re.match(r"(\S+)-(\d+)\s+(\S+)\s+(\d+)", riga)
        if m:
            lavori.setdefault(m.group(1), []).append({"id": f"{m.group(1)}-{m.group(2)}", "utente": m.group(3),
                                                      "byte": int(m.group(4))})
    adesso = time.time()
    out = []
    for nome, uri in sorted(vie.items()):
        chiave, info = per_nome.get(nome, ("", {}))
        tipo = info.get("tipo") or ("usb" if "127.0.0.1" in uri else "rete")
        vista = db["viste"].get(chiave, 0)
        st = stati.get(nome, {})
        out.append({
            "nome": nome, "descrizione": st.get("descrizione", nome), "uri": uri, "tipo": tipo,
            "modello": info.get("modello", st.get("descrizione", nome)), "predefinita": nome == pred,
            "stato": st.get("stato", ""), "attiva": st.get("attiva", True), "motivo": st.get("motivo", ""),
            # trovata in rete o USB: vista di recente; aggiunta a mano con l'indirizzo: non si sa
            "raggiungibile": (adesso - vista < VISTA_PER) if chiave and not chiave.startswith("uri:") else None,
            "lavori": lavori.get(nome, []),
        })
    return {"servizio": True, "stampanti": out}


def pdf_prova(percorso, stampante):
    """Una pagina di prova A4: testo, quadrati nei colori di stampa e una scala di grigi."""
    righe = [(28, T("Pagina di prova", "Test page")), (14, "Termux On DeX"),
             (12, T("Stampante: ", "Printer: ") + stampante), (12, time.strftime("%d/%m/%Y %H:%M")),
             (11, T("Se leggi questa pagina, la stampante funziona.", "If you can read this page, the printer works."))]
    c, y = [], 770
    for dim, testo in righe:
        t = testo.encode("latin-1", "replace").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
        c.append(b"BT /F1 %d Tf 60 %d Td (%s) Tj ET" % (dim, y, t))
        y -= dim + 16
    for i, colore in enumerate((b"1 0 0 0 k", b"0 1 0 0 k", b"0 0 1 0 k", b"0 0 0 1 k")):
        c.append(colore + b" %d 520 90 90 re f" % (60 + i * 110))
    for i in range(10):
        c.append(b"%.2f g %d 470 45 30 re f" % (i / 9, 60 + i * 47))
    c.append(b"0 G 1 w 50 450 495 180 re S")
    flusso = b"\n".join(c)
    oggetti = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n" % len(flusso) + flusso + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    out, pos = b"%PDF-1.4\n", []
    for i, o in enumerate(oggetti, 1):
        pos.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(oggetti) + 1) + b"".join(b"%010d 00000 n \n" % p for p in pos)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(oggetti) + 1, xref)
    with open(percorso, "wb") as f:
        f.write(out)


def indirizzo_uri(testo):
    t = testo.strip()
    if re.match(r"^(ipps?|socket|lpd|https?)://", t):
        return t
    return f"ipp://{t}/ipp/print"   # un indirizzo IP o un nome: IPP Everywhere, porta 631


def principale(argv):
    cmd = argv[1] if len(argv) > 1 else "stato"
    js = "--json" in argv
    args = [a for a in argv[2:] if a != "--json"]
    if cmd == "sorveglia":
        return sorveglia()
    if cmd == "ferma":
        return ferma()
    if cmd == "stato":
        acceso = bool(pid_di(PID_CUPSD, "cupsd")) and risponde()
        print(T("servizio di stampa: ", "print service: ") + (T("acceso", "on") if acceso else T("spento", "off")))
        for p in elenco()["stampanti"]:
            print(f"  {p['descrizione']} ({p['nome']}, {p['tipo']}){' *' if p['predefinita'] else ''}: {p['uri']}")
        return 0
    if cmd == "cerca":
        trovate = cerca()
        if js:
            print(json.dumps(trovate, ensure_ascii=False))
        else:
            for p in trovate:
                print(f"{p['modello']}: {p['uri']}" + ("" if p["senza_driver"] else T(" (serve un driver)", " (needs a driver)")))
            if not trovate:
                print(T("nessuna stampante trovata in rete", "no printers found on the network"))
        return 0
    if cmd == "elenco":
        e = elenco()
        print(json.dumps(e, ensure_ascii=False) if js else "\n".join(p["nome"] for p in e["stampanti"]))
        return 0
    if not risponde():
        print(T("il servizio di stampa è spento", "the print service is off"), file=sys.stderr)
        return 1
    db = carica_db()
    if cmd == "aggiungi" and args:
        trovate = {p["chiave"]: p for p in json.load(open(os.path.join(RUN, "trovate.json")))} \
            if os.path.exists(os.path.join(RUN, "trovate.json")) else {}
        if args[0].lower() in trovate:
            err = aggiungi_rete(db, trovate[args[0].lower()], da_utente=True)
        else:
            uri = indirizzo_uri(args[0])
            descr = args[1] if len(args) > 1 else re.sub(r"^\w+://([^/:]+).*", r"\1", uri)
            nome = nome_coda(descr, code())
            err = crea_coda(nome, uri, descr, T("Rete", "Network"))
            if not err:
                db["stampanti"]["uri:" + uri] = {"nome": nome, "uri": uri, "tipo": "rete", "modello": descr}
        salva_db(db)
        if err:
            print(err, file=sys.stderr)
            return 1
        return 0
    if cmd == "togli" and args:
        r = cups("lpadmin", "-x", args[0])
        for k, v in list(db["stampanti"].items()):
            if v["nome"] == args[0]:
                del db["stampanti"][k]
                if k not in db["tolte"]:
                    db["tolte"].append(k)
        salva_db(db)
        return r.returncode
    if cmd == "predefinita" and args:
        return cups("lpadmin", "-d", args[0]).returncode
    if cmd == "prova" and args:
        f = os.path.join(RUN, "pagina-di-prova.pdf")
        pdf_prova(f, args[0])
        r = cups("lp", "-d", args[0], "-t", T("Pagina di prova", "Test page"), f)
        print((r.stdout or r.stderr).strip())
        return r.returncode
    if cmd == "annulla" and args:   # un lavoro (NOME-123) o tutti quelli di una stampante (NOME)
        return (cups("cancel", args[0]) if re.search(r"-\d+$", args[0]) else cups("cancel", "-a", args[0])).returncode
    print("uso: tod-stampa.py sorveglia|ferma|stato|cerca|elenco|aggiungi INDIRIZZO|togli NOME|predefinita NOME|"
          "prova NOME|annulla LAVORO", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(principale(sys.argv))
