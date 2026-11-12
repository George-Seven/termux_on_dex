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
import subprocess
import sys
import time

# la ricerca in rete (Bonjour) è in comune con share/rete/tod-rete.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scoperta import A, MDNS, PTR, SRV, TXT, analizza, chiedi_bonjour, domanda, leggi_nome, nome_dns  # noqa: E402,F401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ipp  # noqa: E402

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
INDIRIZZI_OGNI = 600   # secondi tra una ricerca indirizzo per indirizzo e l'altra (subito se cambia la rete)


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


ROOTFS = os.environ.get("TOD_CX_ROOTFS") or os.path.join(PREFIX, "var/lib/proot-distro/containers/debian/rootfs")


def convs():
    """Il nostro filtro per l'Apple Raster solo se manca quello di CUPS (rastertopwg, pacchetto Debian `cups`).
    12 novembre, HP Envy 6100e di Pietro: "arriva alla stampante, fa cose, ma poi non stampa" ("Print job canceled at
    printer"). Il raster di Ghostscript (urfrgb) ha un'intestazione povera: 0 pagine dichiarate, fronte-retro e qualità
    a 0, larghezza 2479 invece di 2480; quello di CUPS la riempie dal driver e dalle opzioni di stampa."""
    if os.path.exists(os.path.join(ROOTFS, "usr/lib/cups/filter/rastertopwg")):
        return "# Termux On DeX: l'Apple Raster lo fa rastertopwg di CUPS (tod-pdftourf solo se manca)\n"
    return CONVS


def scrivi_conf():
    for d in (RUN, os.path.join(RUN, "tmp"), USB, os.path.join(CONF, "ppd"), os.path.join(STATO, "spool"), CACHE):
        os.makedirs(d, exist_ok=True)
    os.chmod(RUN, 0o700)
    scrivi(os.path.join(CONF, "cupsd.conf"), testo_cupsd())
    scrivi(os.path.join(CONF, "cups-files.conf"), testo_files())
    scrivi(os.path.join(CONF, "tod.convs"), convs())


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
    c_era = nome in code()
    r = cups("lpadmin", "-p", nome, "-E", "-v", uri, "-m", modello, "-D", descr, "-L", luogo,
             "-o", "printer-is-shared=false", timeout=120)
    if r.returncode:
        # 11 novembre: lpadmin crea la coda PRIMA di chiedere alla stampante cosa sa fare; se poi fallisce la coda
        # restava, senza driver ("funziona anche con un IP a caso", ma non stampa): si toglie
        if not c_era and nome in code():
            cups("lpadmin", "-x", nome)
        return (r.stderr or r.stdout).strip() or "lpadmin"
    if not predefinita():   # la prima stampante diventa quella predefinita, come su Windows
        cups("lpadmin", "-d", nome)
    return ""


# --- Wi-Fi: Bonjour (mDNS) --------------------------------------------------------------------------------------

TIPI = ("_ipp._tcp.local", "_ipps._tcp.local")


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


def mio_indirizzo():
    """L'indirizzo del telefono nella rete locale ("" senza rete). Come tod-rete.py: Android non dà le interfacce."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("224.0.0.251", 5353))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def wifi_del_telefono():
    """L'indirizzo Wi-Fi secondo il telefono (tod-phone-status.py), None se non si sa."""
    try:
        with open(os.path.join(TMP, "tod-phone.json")) as f:
            return (json.load(f).get("wifi") or {}).get("ip") or None
    except (OSError, ValueError):
        return None


def voce_da_sonda(st):
    """Una stampante descritta da ipp.sonda() → la voce delle stampanti trovate (come stampanti_da)."""
    resto = st["uri"].split("://", 1)[1].split("/", 1)
    return {"chiave": (st.get("uuid") or f"{st['ip']}/{resto[1] if len(resto) > 1 else ''}").lower(),
            "nome": st["modello"], "modello": st["modello"], "uri": st["uri"], "ip": st["ip"],
            "colore": st.get("colore", False), "fronte_retro": False, "senza_driver": st["senza_driver"],
            "sicura": st["uri"].startswith("ipps"), "formati": st.get("formati", []), "device_id": st.get("device_id", "")}


def cerca_indirizzi(gia=()):
    """Le stampanti che non si annunciano (Bonjour spento o filtrato dal router): chi nella rete di casa ha la porta
    IPP aperta, e che stampante è. Solo sul Wi-Fi, mai sulla rete dell'operatore. GIA: indirizzi già trovati."""
    ip = mio_indirizzo()
    if not ipp.rete_di_casa(ip, wifi_del_telefono()):
        return []
    trovate = []
    for h in ipp.porte_aperte(ipp.indirizzi_vicini(ip), 631):
        if h in gia:
            continue
        st, _motivo = ipp.sonda(h, porte=((631, False),), attesa=3.0)
        if st and st["tipo"] == "ipp":
            trovate.append(voce_da_sonda(st))
    return trovate


_viste_nel_registro = set()
_per_indirizzi = ("", [])   # (la mia rete, le stampanti trovate indirizzo per indirizzo): restano tra un giro e l'altro


def cerca(attesa=2.0, indirizzi=True):
    """Le stampanti IPP in rete: Bonjour e, con INDIRIZZI, anche la ricerca indirizzo per indirizzo.
    [] senza rete locale (dati mobili): nessuno risponde."""
    record, sorgenti, errore = chiedi_bonjour(TIPI, attesa)
    if errore:
        log(f"ricerca in rete: {errore}")
    global _per_indirizzi
    trovate = stampanti_da(record, sorgenti)
    io = mio_indirizzo()
    if indirizzi:
        try:
            _per_indirizzi = (io, cerca_indirizzi({p["ip"] for p in trovate}))
        except OSError as e:
            log(f"ricerca per indirizzi: {e}")
    if _per_indirizzi[0] == io:   # (su un'altra rete quelle di prima non valgono più)
        chiavi = {p["chiave"] for p in trovate}
        ip_noti = {p["ip"] for p in trovate}
        trovate += [p for p in _per_indirizzi[1] if p["chiave"] not in chiavi and p["ip"] not in ip_noti]
    for p in trovate:   # una riga per stampante nuova: al prossimo problema il registro dice cosa c'era in rete
        if p["chiave"] not in _viste_nel_registro:
            _viste_nel_registro.add(p["chiave"])
            log(f"in rete: {p['modello']} ({p['uri']})" + ("" if p["senza_driver"] else ", serve un driver")
                + (f", formati: {' '.join(p['formati'])}" if p.get("formati") else ""))
    return trovate


def host_di(uri):
    """L'indirizzo dentro un URI di stampa (ipp://192.168.1.17:631/ipp/print → 192.168.1.17)"""
    m = re.match(r"^\w+://\[?([^/\]:]+)", uri or "")
    return m.group(1).lower() if m else ""


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
    # 12 novembre, HP Envy di Pietro: aggiunta da lui con l'indirizzo e, un secondo dopo, di nuovo dalla ricerca
    # ("HP_Envy_6100e_series_2"). Una stampante che ha già una coda (stesso indirizzo) si adotta, non si raddoppia
    for nome_c, uri_c in code().items():
        if host_di(uri_c) == host_di(p["uri"]):
            for k in [k for k, v in db["stampanti"].items() if v["nome"] == nome_c and k != p["chiave"]]:
                del db["stampanti"][k]
            db["stampanti"][p["chiave"]] = {"nome": nome_c, "uri": uri_c, "tipo": "rete", "modello": p["modello"]}
            return "" if not da_utente else T("Questa stampante c'è già.", "This printer is already there.")
    if da_utente and not p["senza_driver"]:   # "Aggiungi" su una che vuole un driver: si guarda cosa sa fare
        return aggiungi_indirizzo(db, p["ip"], p["modello"])
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


def giro_rete(db, indirizzi=False):
    trovate = cerca(indirizzi=indirizzi)
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

def scegli_ppd(righe, formati=()):
    """Tra i driver che CUPS propone per una stampante (righe di lpinfo -m: "nome descrizione") quello giusto.
    Le stampanti da ufficio (Ricoh MP C2011 di Pietro, 12 novembre) ne hanno tre: PXL (PCL 6, c'è sempre), PS e PDF
    (opzioni che la macchina può non avere). Si prende il linguaggio che la stampante dichiara, PXL per primo."""
    nomi = [r.split(" ", 1)[0] for r in righe if r.strip()]
    nomi = [n for n in nomi if n != "everywhere" and not n.startswith("raw")]
    if not nomi:
        return ""
    f = " ".join(formati).lower()
    genere = lambda n: next((g for g in ("PXL", "PS", "PDF") if re.search(rf"[_/-]{g}\.ppd|/{g}/", n)), "")  # noqa: E731
    ordine = [g for g, parola in (("PXL", "pcl"), ("PS", "postscript"), ("PDF", "application/pdf")) if parola in f] \
        or ["PXL", "PS", "PDF"]
    for g in ordine:
        for n in nomi:
            if genere(n) == g:
                return n
    return nomi[0]


def driver_per(device_id, formati=(), modello=""):
    """Il driver di una stampante senza IPP Everywhere, dall'ID IEEE 1284 (MFG, MDL, CMD) o dal nome del modello:
    quello che CUPS propone tra i driver installati, sennò quello generico per PostScript o PCL. "" = nessuno."""
    for cerca_con in (("--device-id", device_id), ("--make-and-model", modello)):
        if not cerca_con[1] or (cerca_con[0] == "--device-id" and "MDL" not in cerca_con[1].upper()):
            continue
        r = cups("lpinfo", cerca_con[0], cerca_con[1], "-m", timeout=90)
        nome = scegli_ppd(r.stdout.splitlines(), formati)
        if nome:
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
    ultima_rete, ultimi_indirizzi, rete_prima = 0.0, 0.0, None
    data_vista = None
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
        try:   # un comando (le Impostazioni: aggiungi, togli) ha scritto l'elenco: vale il suo, non la nostra copia
            data_db = os.stat(DB).st_mtime
            if data_db != data_vista:
                db, data_vista = carica_db(), data_db
        except OSError:
            pass
        prima = json.dumps(db, sort_keys=True)
        try:
            collegate = giro_usb(db, collegate)
            for k in list(db["tolte"]):   # USB tolta e poi staccata: alla prossima volta si riaggiunge
                if k.startswith("usb:") and k not in collegate:
                    db["tolte"].remove(k)
            if time.time() - ultima_rete >= RETE_OGNI:
                ultima_rete = time.time()
                rete_ora = mio_indirizzo()
                # indirizzo per indirizzo ogni tanto, e subito quando si entra in una rete nuova
                completa = rete_ora != rete_prima or time.time() - ultimi_indirizzi >= INDIRIZZI_OGNI
                if completa:
                    rete_prima, ultimi_indirizzi = rete_ora, time.time()
                giro_rete(db, indirizzi=completa)
        except Exception as e:   # un giro andato storto non deve fermare il servizio
            log(f"giro: {type(e).__name__}: {e}")
        if json.dumps(db, sort_keys=True) != prima:
            salva_db(db)
            try:
                data_vista = os.stat(DB).st_mtime
            except OSError:
                pass
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

def porta_di(uri):
    """(host, porta) di una stampante di rete dal suo URI; None per le USB (127.0.0.1) e gli URI senza indirizzo."""
    m = re.match(r"^(ipps?|https?|socket|lpd)://\[?([^/\]:]+)\]?(?::(\d+))?", uri or "")
    if not m or m.group(2) in ("127.0.0.1", "localhost"):
        return None
    predefinite = {"ipp": 631, "ipps": 631, "http": 80, "https": 443, "socket": 9100, "lpd": 515}
    return m.group(2), int(m.group(3) or predefinite[m.group(1)])


def raggiungibili(uri, attesa=1.2):
    """{uri: risponde adesso} per le stampanti di rete: un collegamento alla loro porta, tutte insieme."""
    import threading
    esito = {}

    def prova(u, dove):
        esito[u] = ipp.porta_aperta(dove[0], dove[1], attesa)
    fili = []
    for u in set(uri):
        dove = porta_di(u)
        if dove:
            fili.append(threading.Thread(target=prova, args=(u, dove), daemon=True))
            fili[-1].start()
    for t in fili:
        t.join(attesa + 1)
    return esito


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
    titoli = {}   # lpq: "1st  pietro  8  Pagina di prova  2048 bytes" → il nome del documento, che lpstat non dà
    for riga in cups("lpq", "-a").stdout.splitlines():
        m = re.match(r"\S+\s+\S+\s+(\d+)\s+(.*?)\s+\d+ bytes\s*$", riga)
        if m:
            titoli[m.group(1)] = m.group(2).strip()
    for elenco_lavori in lavori.values():
        for lav in elenco_lavori:
            lav["titolo"] = titoli.get(lav["id"].rsplit("-", 1)[1], "")
    fatti = {}   # gli ultimi documenti mandati alla stampante (per la finestra della coda)
    for riga in cups("lpstat", "-W", "completed", "-o").stdout.splitlines():
        m = re.match(r"(\S+)-(\d+)\s+(\S+)\s+(\d+)\s+(.*)", riga)
        if m:
            quando = re.sub(r"^\w+ (\w+ +\d+) (\d+:\d+):\d+ \d+$", r"\1, \2", m.group(5).strip())
            fatti.setdefault(m.group(1), []).append({"id": f"{m.group(1)}-{m.group(2)}", "byte": int(m.group(4)),
                                                     "quando": quando})
    adesso = time.time()
    risponde_ora = raggiungibili(vie.values())
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
            # di rete: risponde adesso alla sua porta (12 novembre, Pietro: "quando è offline deve essere offline,
            # non deve spuntare pronta": quelle aggiunte a mano o rimaste da un'altra rete non si controllavano mai);
            # USB: vista di recente dal giro delle USB
            "raggiungibile": risponde_ora[uri] if uri in risponde_ora else
            ((adesso - vista < VISTA_PER) if chiave and not chiave.startswith("uri:") else None),
            "lavori": lavori.get(nome, []),
            "fatti": sorted(fatti.get(nome, []), key=lambda x: int(x["id"].rsplit("-", 1)[1]), reverse=True)[:5],
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


def modello_per(st):
    """Il driver per una stampante descritta da ipp.sonda(): "everywhere" se non ne serve, sennò quello che CUPS ha
    per il suo ID o quello generico per PostScript o PCL; "" = nessuno."""
    if st["senza_driver"]:
        return "everywhere"
    did = st.get("device_id", "")
    formati = " ".join(st.get("formati", []))
    if not re.search(r"CMD|COMMAND SET", did, re.I):   # dai formati che dichiara via IPP
        if "postscript" in formati:
            did += ";CMD:POSTSCRIPT;"
        elif "pcl" in formati:
            did += ";CMD:PCL;"
    modello = st.get("modello", "")
    if modello == st.get("ip"):   # (senza IPP il "modello" è solo l'indirizzo)
        modello = ""
    return driver_per(did, st.get("formati", ()), modello) if did or modello else ""


def aggiungi_indirizzo(db, testo, descr=""):
    """Una stampante con il suo indirizzo (o un URI completo). → "" o il motivo, in parole per l'utente."""
    t = testo.strip()
    if re.match(r"^(socket|lpd|https?)://", t):   # chi scrive un URI così sa cosa vuole
        st = {"uri": t, "modello": descr or re.sub(r"^\w+://([^/:]+).*", r"\1", t), "senza_driver": True, "tipo": "uri"}
    else:
        m = re.match(r"^(ipps?)://([^/:]+)(?::(\d+))?(?:/(.*))?$", t)
        host = m.group(2) if m else t
        if not re.match(r"^[\w.\-]+$", host):
            return T(f"«{t}» non è un indirizzo valido.", f"“{t}” is not a valid address.")
        if m and m.group(4) is not None:   # percorso scritto dall'utente: si prova quello
            porta = int(m.group(3) or (443 if m.group(1) == "ipps" else 631))
            a, _mot = ipp.attributi(host, porta, m.group(4), m.group(1) == "ipps")
            st = ipp.descrivi(a, f"{m.group(1)}://{host}:{porta}/{m.group(4)}", host) if a else None
            motivo = "" if st else ("spenta" if not ipp.porta_aperta(host, porta) else "non-stampante")
        else:
            st, motivo = ipp.sonda(host)
        if st is None:
            log(f"aggiunta di {t}: {motivo}")
            if motivo == "spenta":
                return T(f"Nessuna stampante risponde a {host}. Controlla che sia accesa e sulla stessa rete Wi-Fi.",
                         f"No printer answers at {host}. Check that it is on and on the same Wi-Fi network.")
            return T(f"A {host} risponde un dispositivo, ma non è una stampante di rete.",
                     f"A device answers at {host}, but it is not a network printer.")
        log(f"aggiunta di {t}: {st['modello']} ({st['uri']}), formati: {' '.join(st['formati']) or '-'}, "
            f"id: {st['device_id'] or '-'}")
    for nome_c, uri_c in code().items():
        if host_di(uri_c) == host_di(st["uri"]):
            return T(f"{st['modello']} c'è già tra le stampanti.", f"{st['modello']} is already among the printers.")
    modello = modello_per(st) if st["tipo"] != "uri" else "everywhere"
    if not modello:
        return T(f"{st['modello']}: questa stampante ha bisogno di un driver che non è installato.",
                 f"{st['modello']}: this printer needs a driver that is not installed.")
    nome = nome_coda(descr or st["modello"], code())
    err = crea_coda(nome, st["uri"], descr or st["modello"], T("Rete", "Network"), modello)
    if err and modello == "everywhere" and st["tipo"] == "ipp":   # dichiara i formati giusti ma CUPS non la accetta
        altro = modello_per(dict(st, senza_driver=False))
        if altro:
            log(f"{st['modello']}: senza driver non va ({err}); provo {altro}")
            err = crea_coda(nome, st["uri"], descr or st["modello"], T("Rete", "Network"), altro)
    if err:
        log(f"non aggiunta {st['modello']} ({st['uri']}): {err}")
        return T(f"{st['modello']}: non riesco a configurarla ({err}).", f"{st['modello']}: cannot set it up ({err}).")
    chiave = st.get("uuid") or "uri:" + st["uri"]   # (l'UUID è lo stesso che dà la ricerca: una voce sola)
    db["stampanti"][chiave] = {"nome": nome, "uri": st["uri"], "tipo": "rete", "modello": descr or st["modello"]}
    log(f"aggiunta {nome} ({st['uri']}, {modello})")
    return ""


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
    if cmd == "sonda" and args:   # diagnosi: cosa risponde a questo indirizzo
        st, motivo = ipp.sonda(args[0])
        print(json.dumps(st or {"errore": motivo}, ensure_ascii=False, indent=1))
        return 0 if st else 1
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
            err = aggiungi_indirizzo(db, args[0], args[1] if len(args) > 1 else "")
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
    if cmd == "riprendi" and args:   # una coda fermata da un errore o messa in pausa: si riaccende e riparte
        cups("cupsenable", args[0])
        return cups("lpadmin", "-p", args[0], "-E").returncode
    if cmd == "pausa" and args:   # dalla finestra della coda: i documenti restano, la stampante aspetta
        return cups("cupsdisable", args[0]).returncode
    if cmd == "annulla" and args:   # un lavoro (NOME-123) o tutti quelli di una stampante (NOME)
        return (cups("cancel", args[0]) if re.search(r"-\d+$", args[0]) else cups("cancel", "-a", args[0])).returncode
    print("uso: tod-stampa.py sorveglia|ferma|stato|cerca|elenco|aggiungi INDIRIZZO|togli NOME|predefinita NOME|"
          "prova NOME|annulla LAVORO|pausa NOME|riprendi NOME|sonda INDIRIZZO", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(principale(sys.argv))
