"""adb_dati.py — ADB senza fili verso il telefono stesso, senza grafica (15 novembre 2026): lo usano share/tod-adb.py
(la finestra in stile DeX OS), il servizio del telefono (tod-phone-status.py) e i test.

Pietro: "prepariamo un'interfaccia di sistema in stile DexOS per il collegamento adb wireless quando mi collego ad una
rete wifi nuova". Android spegne il Debug wireless a ogni riavvio e a ogni cambio di rete, e Termux non può
riaccenderlo da sé (servirebbe WRITE_SECURE_SETTINGS, che varrebbe per tutte le app del desktop: no). Acceso quello, il
resto è automatico: la porta (a caso) la annuncia il telefono stesso con Bonjour (_adb-tls-connect._tcp, chiesto col
bit QU come in share/scoperta.py) e con Termux già associato il collegamento non chiede niente. La prima volta serve il
codice a 6 cifre di "Associa dispositivo con codice di associazione": la porta di associazione si trova allo stesso
modo. Nessuna porta aperta da noi (Pietro, 15 novembre, su adb tcpip: "non voglio problemi di sicurezza").

  collegato()              il telefono già collegato ("127.0.0.1:porta"), o None
  porte(ip)                {"connect": porta, "pairing": porta} annunciate dal telefono stesso
  porte_aperte()           ripiego senza Bonjour: le porte in ascolto su 127.0.0.1 (nmap, come lib/phantom.sh)
  collega(porta)           adb connect → (riuscito, messaggio)
  associa(porta, codice)   adb pair → (riuscito, messaggio)
  rete_wifi(stato)         la rete Wi-Fi attuale (da $TMPDIR/tod-phone.json): {chiave, nome, ip} o None
  da_chiedere(...)         se la finestra deve comparire
"""
import json
import os
import re
import shutil
import subprocess
import sys

QUI = os.path.dirname(os.path.abspath(__file__))
TMP = os.environ.get("TMPDIR", "/data/data/com.termux/files/usr/tmp")
CASA = os.environ.get("TOD_TERMUX_HOME") or os.path.expanduser("~")
RETI = os.path.join(CASA, ".config/tod/adb-reti.json")   # le reti dove non chiedere più
CONF = os.path.join(CASA, ".config/tod/tod.conf")
STATO = os.path.join(TMP, "tod-phone.json")
ADB = os.environ.get("TOD_ADB", "adb")
CONNECT, PAIRING = "_adb-tls-connect._tcp.local", "_adb-tls-pairing._tcp.local"
SRV = 33
LOCALE = re.compile(r"^(127\.0\.0\.1|localhost):\d+$")


def adb(*argomenti, attesa=20):
    """(codice d'uscita, uscita) di un comando adb"""
    try:
        r = subprocess.run([ADB, *argomenti], capture_output=True, text=True, timeout=attesa, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def presente():
    return shutil.which(ADB) is not None


def dispositivi(uscita=None):
    """{seriale: stato} da "adb devices" (device, offline, unauthorized...)"""
    if uscita is None:
        _rc, uscita = adb("devices", attesa=10)
    out = {}
    for riga in uscita.splitlines()[1:] if uscita.startswith("List of devices") else uscita.splitlines():
        p = riga.split()
        if len(p) >= 2:
            out[p[0]] = p[1]
    return out


def collegato(uscita=None):
    """il telefono stesso collegato (127.0.0.1 o localhost, come adb_local_device di lib/phantom.sh), o None"""
    for s, stato in dispositivi(uscita).items():
        if stato == "device" and LOCALE.match(s):
            return s
    return None


def porte(ip=None, risposte=None):
    """{"connect": porta, "pairing": porta} dal Bonjour del telefono stesso. Gli altri telefoni della rete con il Debug
    wireless acceso si scartano, riconosciuti dall'indirizzo di chi risponde (se si sa il nostro: IP)."""
    if risposte is None:
        if QUI not in sys.path:
            sys.path.insert(1, QUI)
        import scoperta
        record, sorgenti, _errore = scoperta.chiedi_bonjour([CONNECT, PAIRING], attesa=1.5)
    else:
        record, sorgenti = risposte
    nostri = {"127.0.0.1", ip}
    out = {}
    for nome, tipo, dati in record:
        if tipo != SRV:
            continue
        n = nome.lower()
        if ip and sorgenti.get(n) not in nostri:
            continue
        for k, t in (("connect", CONNECT), ("pairing", PAIRING)):
            if n.endswith("." + t):
                out[k] = int(dati[0])
    return out


def porte_aperte(uscita=None):
    """le porte in ascolto su 127.0.0.1 tra quelle che Android dà a caso (32768-60999), con nmap se c'è"""
    if uscita is None:
        if shutil.which("nmap") is None:
            return []
        try:
            uscita = subprocess.run(["nmap", "-Pn", "-sT", "-T4", "--open", "-p", "32768-60999", "127.0.0.1", "-oG", "-"],
                                    capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL).stdout
        except (OSError, subprocess.SubprocessError):
            return []
    return [int(p) for p in re.findall(r"(\d+)/open", uscita)]


def pulisci():
    """via i collegamenti locali morti (porte di accensioni passate: "offline"), che confondono adb -s"""
    for s, stato in dispositivi().items():
        if stato != "device" and LOCALE.match(s):
            adb("disconnect", s, attesa=5)


def collega(porta):
    """adb connect 127.0.0.1:PORTA → (riuscito, messaggio). Riuscito solo se adb devices lo dà "device": senza
    associazione il collegamento può aprirsi ma restare "unauthorized"."""
    pulisci()
    s = f"127.0.0.1:{porta}"
    _rc, out = adb("connect", s, attesa=10)
    stato = dispositivi().get(s)
    if stato == "device":
        return True, out
    if stato is not None:
        adb("disconnect", s, attesa=5)
    return False, out


def associa(porta, codice):
    """adb pair 127.0.0.1:PORTA CODICE → (riuscito, messaggio). adb pair può dire "Failed" ed uscire con 0: conta il
    testo (come in lib/phantom.sh)"""
    _rc, out = adb("pair", f"127.0.0.1:{porta}", codice, attesa=25)
    return "successfully paired" in out.lower(), out


def codice_valido(testo):
    """"123 456", "123-456" → "123456"; None se non sono 6 cifre"""
    c = re.sub(r"[\s-]", "", testo or "")
    return c if re.fullmatch(r"\d{6}", c) else None


def rete_wifi(stato=None):
    """la rete Wi-Fi a cui è collegato il telefono (da Termux:API, letto da tod-phone-status.py): {chiave, nome, ip}
    o None. Senza il permesso Posizione Android non dà né nome né BSSID: la chiave è allora la rete /24."""
    if stato is None:
        try:
            with open(STATO) as f:
                stato = json.load(f)
        except (OSError, ValueError):
            return None
    w = (stato or {}).get("wifi") or {}
    ip = str(w.get("ip") or "")
    if w.get("supplicant_state") != "COMPLETED" or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip) or ip == "0.0.0.0":
        return None
    nome = str(w.get("ssid") or "").strip('"')
    if nome in ("", "<unknown ssid>"):
        nome = None
    bssid = str(w.get("bssid") or "").lower()
    chiave = f"bssid:{bssid}" if bssid and bssid != "02:00:00:00:00:00" else "ip:" + ip.rsplit(".", 1)[0]
    return {"chiave": chiave, "nome": nome, "ip": ip}


def conf(chiave, predefinito, file=CONF):
    try:
        with open(file) as f:
            for riga in f:
                if riga.startswith(chiave + "="):
                    return riga.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return predefinito


def reti_mai(file=RETI):
    try:
        with open(file) as f:
            return set(json.load(f).get("mai", []))
    except (OSError, ValueError, AttributeError):
        return set()


def segna_mai(chiave, file=RETI):
    mai = reti_mai(file) | {chiave}
    os.makedirs(os.path.dirname(file), exist_ok=True)
    tmp = f"{file}.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump({"mai": sorted(mai)}, f, indent=1)
    os.replace(tmp, file)


def da_chiedere(rete, gia_collegato, mai, acceso=True):
    """la domanda compare solo su una rete Wi-Fi, con ADB non collegato, se non è tra quelle "non su questa rete" e
    se la domanda non è spenta (ADB_CHIEDI=0)"""
    return bool(acceso and rete and not gia_collegato and rete["chiave"] not in mai)
