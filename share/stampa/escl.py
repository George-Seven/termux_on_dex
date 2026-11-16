"""Gli scanner eSCL (AirScan): trovarli, chiedere cosa sanno fare, scansionare, fare il PDF. Niente SANE, niente GTK.

eSCL è HTTP con un po' di XML: GET <base>/ScannerCapabilities, POST <base>/ScanJobs con le impostazioni (risposta 201 e
l'indirizzo del lavoro), poi GET <lavoro>/NextDocument per ogni pagina (404: finite; 503: aspetta). Lo parlano quasi
tutte le multifunzione Wi-Fi di oggi (annunciate su Bonjour come _uscan._tcp, _uscans._tcp in HTTPS) e, sullo stesso
canale della stampa, quelle USB con IPP via USB (classe 7/1/4): share/stampa/tod-stampante-usb.py le porta su
127.0.0.1 e lo scrive in $TMPDIR/tod-stampa/usb/<id>.json. Gli scanner USB solo con driver propri restano fuori.
La finestra è share/stampa/tod-scanner.py; le prove in tests/run.sh (test_scanner_*, con tests/escl_finto.py).
"""
import glob
import http.client
import json
import os
import socket
import ssl
import struct
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(1, os.path.dirname(os.path.abspath(__file__)))   # wsd.py, caricato quando serve
import scoperta  # noqa: E402

PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
TMP = os.environ.get("TMPDIR", os.path.join(PREFIX, "tmp"))
TH = os.environ.get("TOD_TERMUX_HOME") or os.environ.get("HOME", "")
STAMPA_USB = os.path.join(TMP, "tod-stampa", "usb")   # i ponti USB di tod-stampante-usb.py
CONF = os.path.join(TH, ".config/tod/scanner.json")
ITALIAN = os.environ.get("LANGUAGE", os.environ.get("LANG", "")).startswith("it")

NS_SCAN = "http://schemas.hp.com/imaging/escl/2011/05/03"
NS_PWG = "http://www.pwg.org/schemas/2010/12/sm"
TIPI = ("_uscan._tcp.local", "_uscans._tcp.local")
# i fogli in 1/300 di pollice (le unità di eSCL): A4 = 210 × 297 mm, Lettera = 8,5 × 11 pollici
FOGLI = {"a4": (2480, 3508), "lettera": (2550, 3300)}
DPI_COMUNI = (75, 100, 150, 200, 300, 400, 600, 1200)


def T(it, en):
    return it if ITALIAN else en


class Errore(Exception):
    """Un errore da mostrare così com'è."""


# --- configurazione: scanner aggiunti con l'indirizzo, l'ultimo usato, le scelte della finestra ----------------

def carica_conf():
    try:
        c = json.load(open(CONF))
    except (OSError, ValueError):
        c = {}
    if not isinstance(c, dict):
        c = {}
    c.setdefault("manuali", [])   # [{"chiave", "nome", "url"}]
    return c


def salva_conf(c):
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    with open(CONF + ".nuovo", "w") as f:
        json.dump(c, f, indent=1, ensure_ascii=False)
    os.replace(CONF + ".nuovo", CONF)


# --- HTTP ---------------------------------------------------------------------------------------------------------

def richiesta(metodo, url, corpo=None, attesa=30):
    """(codice, intestazioni in minuscolo, dati). Un collegamento per richiesta: sul ponte USB ognuno tiene
    un'interfaccia, e chiuderlo la libera. Su 127.0.0.1 (USB) "Host: localhost", come ipp-usb: alcune HP
    rifiutano altro. HTTPS senza controllo del certificato (gli scanner hanno il loro, firmato da sé)."""
    u = urllib.parse.urlsplit(url)
    try:
        porta = u.port
    except ValueError:   # "1:8080": per chi chiama è uno scanner che non c'è
        raise OSError(f"indirizzo non valido: {url}")
    if not u.hostname:
        raise OSError(f"indirizzo non valido: {url}")
    if u.scheme == "https":
        c = http.client.HTTPSConnection(u.hostname, porta or 443, timeout=attesa,
                                        context=ssl._create_unverified_context())
    else:
        c = http.client.HTTPConnection(u.hostname, porta or 80, timeout=attesa)
    h = {"Connection": "close", "Accept": "*/*"}
    if u.hostname in ("127.0.0.1", "localhost", "::1"):
        h["Host"] = "localhost"
    if corpo is not None:
        h["Content-Type"] = "text/xml"
    try:
        c.request(metodo, u.path or "/", body=corpo, headers=h)
        r = c.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        c.close()


def _assoluto(base, posizione):
    """L'indirizzo del lavoro: il percorso di Location con l'origine di BASE (sul ponte USB lo scanner dice
    http://localhost/..., in rete a volte un nome che Android non risolve)."""
    b, p = urllib.parse.urlsplit(base), urllib.parse.urlsplit(posizione)
    percorso = p.path if p.path.startswith("/") else urllib.parse.urljoin(b.path.rstrip("/") + "/", posizione)
    return f"{b.scheme}://{b.netloc}{percorso}"


# --- cosa sa fare uno scanner ---------------------------------------------------------------------------------

def _nome(e):
    return e.tag.rsplit("}", 1)[-1]


def _primo(e, nome, d=""):
    for x in e.iter():
        if _nome(x) == nome and x.text and x.text.strip():
            return x.text.strip()
    return d


def _int(s, d):
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return d


def _caps(c):
    colori, formati, dpi = [], [], set()
    for x in c.iter():
        n, t = _nome(x), (x.text or "").strip()
        if n == "ColorMode" and t and t not in colori:
            colori.append(t)
        elif n in ("DocumentFormat", "DocumentFormatExt") and t and t not in formati:
            formati.append(t)
        elif n == "DiscreteResolution":
            xr = _int(_primo(x, "XResolution"), 0)
            if xr and _int(_primo(x, "YResolution"), xr) == xr:   # solo quelle quadrate
                dpi.add(xr)
        elif n == "XResolutionRange":
            lo, hi = _int(_primo(x, "Min"), 0), _int(_primo(x, "Max"), 0)
            dpi.update(d for d in DPI_COMUNI if lo <= d <= hi)
    return {"colori": colori, "formati": formati, "dpi": sorted(dpi) or [300],
            "max_l": _int(_primo(c, "MaxWidth"), 2550), "max_a": _int(_primo(c, "MaxHeight"), 3508)}


def analizza_capacita(xml):
    """Da ScannerCapabilities: {"modello", "versione", "fonti": {"Platen"|"Feeder": {colori, formati, dpi, max_l,
    max_a[, fronte_retro]}}}."""
    try:
        radice = ET.fromstring(xml)
    except ET.ParseError as e:
        raise Errore(T(f"Risposta dello scanner non valida ({e})", f"Invalid scanner reply ({e})"))
    out = {"modello": _primo(radice, "MakeAndModel"), "versione": _primo(radice, "Version", "2.0"), "fonti": {}}
    for x in radice.iter():
        n = _nome(x)
        if n == "PlatenInputCaps" and "Platen" not in out["fonti"]:
            out["fonti"]["Platen"] = _caps(x)
        elif n == "AdfSimplexInputCaps" and "Feeder" not in out["fonti"]:
            out["fonti"]["Feeder"] = _caps(x)
    if "Feeder" in out["fonti"]:
        out["fonti"]["Feeder"]["fronte_retro"] = any(_nome(x) == "AdfDuplexInputCaps" for x in radice.iter())
    if not out["fonti"]:
        raise Errore(T("Lo scanner non dice cosa sa fare", "The scanner doesn't say what it can do"))
    return out


def capacita(url, attesa=10):
    if url.startswith("wsd+"):   # scanner WSD ("Scanner di rete" di Windows): share/stampa/wsd.py
        import wsd
        return wsd.capacita(url, attesa)
    try:
        cod, _, dati = richiesta("GET", url + "/ScannerCapabilities", attesa=attesa)
    except (OSError, http.client.HTTPException) as e:
        raise Errore(T(f"Scanner non raggiungibile ({e})", f"Scanner not reachable ({e})"))
    if cod != 200:
        raise Errore(T(f"Lo scanner ha risposto {cod}", f"The scanner answered {cod}"))
    return analizza_capacita(dati)


def ha_escl(url, attesa=5):
    try:
        capacita(url, attesa)
        return True
    except Errore:
        return False


# --- trovare gli scanner ------------------------------------------------------------------------------------

def scanner_da(record, sorgenti):
    """Dai record Bonjour: uno per scanner (HTTP preferito a HTTPS, uno solo per UUID)."""
    per = {}
    for nome, tipo, dati in record:
        per.setdefault(nome.lower(), {}).setdefault(tipo, dati)
    trovati = {}
    for nome, tipo, dati in record:
        if tipo != scoperta.PTR or nome.lower() not in TIPI:
            continue
        ist = per.get(dati.lower(), {})
        srv, txt = ist.get(scoperta.SRV), ist.get(scoperta.TXT, {})
        if not srv:
            continue
        porta, dest = srv
        ip = per.get(dest.lower(), {}).get(scoperta.A) or sorgenti.get(dati.lower())
        if not ip:
            continue
        sicuro = nome.lower().startswith("_uscans")
        rs = txt.get("rs", "eSCL").strip("/")
        url = f"{'https' if sicuro else 'http'}://{ip}:{porta}" + (f"/{rs}" if rs else "")
        s = {"chiave": (txt.get("uuid") or url).lower(), "nome": txt.get("ty") or dati.split("._uscan")[0],
             "url": url, "dove": "wifi", "ip": ip, "sicuro": sicuro}
        vecchio = trovati.get(s["chiave"])
        if not vecchio or (vecchio["sicuro"] and not sicuro):
            trovati[s["chiave"]] = s
    return sorted(trovati.values(), key=lambda s: s["nome"].lower())


def scanner_usb(prova=ha_escl):
    """Le multifunzione USB con IPP via USB (ponte di tod-stampante-usb.py) che rispondono a eSCL."""
    out = []
    for f in sorted(glob.glob(os.path.join(STAMPA_USB, "*.json"))):
        try:
            info = json.load(open(f))
        except (OSError, ValueError):
            continue
        if info.get("tipo") != "ipp" or not info.get("porta"):
            continue
        url = f"http://127.0.0.1:{info['porta']}/eSCL"
        if prova(url):
            ident = (info.get("seriale") or info.get("id") or str(info["porta"])).lower()
            out.append({"chiave": f"usb:{ident}", "nome": info.get("nome") or T("Scanner USB", "USB scanner"),
                        "url": url, "dove": "usb", "ip": ""})
    return out


def cerca(attesa=2.0, rete=True):
    """Tutti gli scanner: Wi-Fi (Bonjour), USB, aggiunti con l'indirizzo. Senza rete locale solo gli altri."""
    trovati = []
    if rete:
        record, sorgenti, _ = scoperta.chiedi_bonjour(list(TIPI), attesa)
        trovati += scanner_da(record, sorgenti)
        ip_noti = {s.get("ip") for s in trovati}
        trovati += [s for s in scanner_delle_stampanti() if s["ip"] not in ip_noti]
        ip_noti = {s.get("ip") for s in trovati}
        try:   # WSD solo per chi non ha eSCL (la stessa macchina con tutti e due: eSCL, più semplice e collaudato)
            import wsd
            trovati += [s for s in wsd.cerca(attesa) if s["ip"] not in ip_noti]
        except Exception as e:   # (uno scanner che risponde male non deve togliere gli altri dall'elenco)
            print(f"wsd: {type(e).__name__}: {e}", file=sys.stderr)
    trovati += scanner_usb()
    visti = {s["url"] for s in trovati} | {s["chiave"] for s in trovati}
    for m in carica_conf()["manuali"]:
        if m.get("url") and m["url"] not in visti and m.get("chiave") not in visti:
            ip = urllib.parse.urlsplit(m["url"]).hostname or ""
            # (aggiunto a mano: non si annuncia, quindi si guarda se risponde; sennò nell'elenco è "Offline")
            trovati.append({"chiave": m["chiave"], "nome": m.get("nome") or ip, "url": m["url"], "dove": "indirizzo",
                            "ip": ip, "raggiungibile": ha_escl(m["url"], attesa=2) if rete else None})
    return trovati


def candidati_indirizzo(testo):
    t = testo.strip().rstrip("/")
    if "://" in t:
        return [t] if t.lower().endswith("/escl") else [t + "/eSCL", t]
    if t.count(":") == 1 and t.rsplit(":", 1)[1].isdigit():   # la porta c'è già
        return [f"http://{t}/eSCL", f"https://{t}/eSCL"]
    # 80 e 443 quasi tutti; 8080 e 9095 certe HP e Samsung, 631 la porta della stampa, 8443 certe Kyocera e Lexmark
    return [f"http://{t}/eSCL", f"https://{t}/eSCL", f"http://{t}:8080/eSCL", f"http://{t}:9095/eSCL",
            f"http://{t}:631/eSCL", f"https://{t}:8443/eSCL"]


def risponde_qualcuno(testo):
    """C'è un dispositivo acceso a quell'indirizzo? (per dire all'utente se è spento o se non sa fare la scansione)"""
    host = urllib.parse.urlsplit(testo if "://" in testo else "//" + testo.strip()).hostname or ""
    for porta in (80, 443, 631, 9100, 8080):
        c = socket.socket()
        c.settimeout(1.5)
        try:
            c.connect((host, porta))
            return True
        except OSError:
            pass
        finally:
            c.close()
    return False


_provati = {}   # indirizzo → (quando, url o None): le stampanti già guardate, per non ribussare a ogni ricerca


def scanner_delle_stampanti(prova=None, ora=None):
    """Le multifunzione che annunciano la stampa ma non lo scanner: si prova l'eSCL agli indirizzi delle stampanti
    trovate in rete dal servizio di stampa ($TMPDIR/tod-stampa/trovate.json). Un indirizzo già provato: dopo 10 minuti."""
    prova = prova or (lambda url: ha_escl(url, attesa=2))
    ora = time.time() if ora is None else ora
    try:
        with open(os.path.join(TMP, "tod-stampa", "trovate.json")) as f:
            stampanti = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for p in stampanti:
        ip = p.get("ip")
        if not ip:
            continue
        quando, url = _provati.get(ip, (0, None))
        if ora - quando > 600:
            url = next((u for u in (f"http://{ip}/eSCL", f"https://{ip}/eSCL") if prova(u)), None)
            _provati[ip] = (ora, url)
        if url:
            out.append({"chiave": f"stampante:{ip}", "nome": p.get("modello") or ip, "url": url, "dove": "rete", "ip": ip})
    return out


def aggiungi_indirizzo(testo):
    """Uno scanner con l'indirizzo ("192.168.1.50", "http://…/eSCL"): lo prova e lo ricorda. La voce, o Errore."""
    if not testo.strip():
        raise Errore(T("Scrivi l'indirizzo dello scanner", "Type the scanner's address"))
    for url in candidati_indirizzo(testo):
        try:
            cap = capacita(url, attesa=5)
        except Errore:
            continue
        ip = urllib.parse.urlsplit(url).hostname or ""
        voce = {"chiave": f"indirizzo:{url.lower()}", "nome": cap["modello"] or ip, "url": url}
        conf = carica_conf()
        conf["manuali"] = [m for m in conf["manuali"] if m.get("url") != url] + [voce]
        salva_conf(conf)
        return dict(voce, dove="indirizzo", ip=ip)
    host = urllib.parse.urlsplit(testo if "://" in testo else "//" + testo.strip()).hostname or ""
    import wsd   # senza eSCL: è uno scanner WSD? (risponde solo alla ricerca multicast, come per Windows)
    voce = next((s for s in wsd.cerca(3.0) if s["ip"] == host), None)
    if voce:
        conf = carica_conf()
        conf["manuali"] = [m for m in conf["manuali"] if m.get("url") != voce["url"]] + \
            [{"chiave": voce["chiave"], "nome": voce["nome"], "url": voce["url"]}]
        salva_conf(conf)
        return dict(voce, dove="indirizzo")
    if not risponde_qualcuno(testo):
        raise Errore(T(f"Nessun dispositivo risponde a «{testo.strip()}». Controlla che sia acceso e sulla stessa rete Wi-Fi.",
                       f"No device answers at “{testo.strip()}”. Check that it is on and on the same Wi-Fi network."))
    raise Errore(T(f"Il dispositivo a «{testo.strip()}» risponde, ma non offre la scansione in rete (AirScan/eSCL o WSD).",
                   f"The device at “{testo.strip()}” answers, but offers no network scanning (AirScan/eSCL or WSD)."))


def togli(chiave):
    conf = carica_conf()
    prima = len(conf["manuali"])
    conf["manuali"] = [m for m in conf["manuali"] if m.get("chiave") != chiave]
    salva_conf(conf)
    return len(conf["manuali"]) < prima


# --- scansionare ----------------------------------------------------------------------------------------------

def area(cap_fonte, foglio):
    """(larghezza, altezza) in 1/300 di pollice: il foglio scelto, mai più grande dello scanner."""
    l, a = (cap_fonte["max_l"], cap_fonte["max_a"]) if foglio == "vetro" else FOGLI.get(foglio, FOGLI["a4"])
    return min(l, cap_fonte["max_l"]), min(a, cap_fonte["max_a"])


def impostazioni_xml(fonte, colore, dpi, larghezza, altezza, versione="2.0", fronte_retro=False,
                     formato="image/jpeg"):
    """ScanSettings (pwg:InputSource Platen|Feeder, scan:ColorMode RGB24|Grayscale8...)."""
    try:
        nuova = float(versione) >= 2.1
    except ValueError:
        nuova = False
    r = ['<?xml version="1.0" encoding="UTF-8"?>',
         f'<scan:ScanSettings xmlns:scan="{NS_SCAN}" xmlns:pwg="{NS_PWG}">',
         f"  <pwg:Version>{versione}</pwg:Version>",
         '  <pwg:ScanRegions pwg:MustHonor="false">',
         "    <pwg:ScanRegion>",
         "      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>",
         "      <pwg:XOffset>0</pwg:XOffset>",
         "      <pwg:YOffset>0</pwg:YOffset>",
         f"      <pwg:Width>{larghezza}</pwg:Width>",
         f"      <pwg:Height>{altezza}</pwg:Height>",
         "    </pwg:ScanRegion>",
         "  </pwg:ScanRegions>",
         f"  <pwg:InputSource>{fonte}</pwg:InputSource>",
         f"  <scan:ColorMode>{colore}</scan:ColorMode>",
         f"  <scan:XResolution>{dpi}</scan:XResolution>",
         f"  <scan:YResolution>{dpi}</scan:YResolution>",
         f"  <pwg:DocumentFormat>{formato}</pwg:DocumentFormat>"]
    if nuova:
        r.append(f"  <scan:DocumentFormatExt>{formato}</scan:DocumentFormatExt>")
    if fonte == "Feeder":
        r.append(f"  <scan:Duplex>{'true' if fronte_retro else 'false'}</scan:Duplex>")
    r.append("</scan:ScanSettings>")
    return "\n".join(r) + "\n"


def _aspetta(secondi, annulla):
    if annulla:
        annulla.wait(secondi)
    else:
        time.sleep(secondi)


def scansiona(url, xml, pagina, annulla=None, una_sola=False, pausa=1.0, attesa=180, tentativi=60):
    """Un lavoro: POST ScanJobs, poi una pagina dopo l'altra (PAGINA(dati) per ognuna) finché lo scanner dice
    che sono finite (dal vetro una sola: UNA_SOLA). 503 = si sta scaldando o è occupato: si riprova. Alla
    fine il lavoro si cancella (DELETE: alcuni scanner sennò restano occupati). Quante pagine; Errore se nessuna."""
    if url.startswith("wsd+"):
        import wsd
        return wsd.scansiona(url, xml, pagina, annulla, una_sola, pausa, attesa, tentativi)

    def chiedi(metodo, u, corpo=None, quanto=30):
        try:
            return richiesta(metodo, u, corpo, quanto)
        except (OSError, http.client.HTTPException) as e:
            raise Errore(T(f"Scanner non raggiungibile ({e})", f"Scanner not reachable ({e})"))

    for _ in range(tentativi):
        cod, h, _ = chiedi("POST", url + "/ScanJobs", xml.encode())
        if cod != 503 or (annulla and annulla.is_set()):
            break
        _aspetta(pausa, annulla)
    if annulla and annulla.is_set():
        if cod in (200, 201) and h.get("location"):   # annullata proprio mentre partiva
            try:
                richiesta("DELETE", _assoluto(url, h["location"]).rstrip("/"), attesa=10)
            except (OSError, http.client.HTTPException):
                pass
        return 0
    if cod == 409:
        raise Errore(T("Lo scanner è occupato, o l'alimentatore è vuoto", "The scanner is busy, or the feeder is empty"))
    if cod == 503:
        raise Errore(T("Lo scanner è occupato: riprova tra poco", "The scanner is busy: try again shortly"))
    if cod not in (200, 201) or not h.get("location"):
        raise Errore(T(f"Lo scanner ha rifiutato la scansione ({cod})", f"The scanner refused to scan ({cod})"))
    lavoro = _assoluto(url, h["location"]).rstrip("/")
    n, occupato = 0, 0
    try:
        while not (annulla and annulla.is_set()):
            cod, _, dati = chiedi("GET", lavoro + "/NextDocument", quanto=attesa)
            if cod == 200 and dati:
                n += 1
                occupato = 0
                pagina(dati)
                if una_sola:
                    break
            elif cod == 503 and occupato < tentativi:
                occupato += 1
                _aspetta(pausa, annulla)
            elif cod in (404, 410) and n:
                break   # pagine finite
            elif cod in (404, 409, 410):
                raise Errore(T("Nessun foglio da scansionare: mettilo nell'alimentatore", "Nothing to scan: load the feeder"))
            else:
                raise Errore(T(f"Lo scanner si è fermato ({cod})", f"The scanner stopped ({cod})"))
    finally:
        try:
            richiesta("DELETE", lavoro, attesa=10)
        except (OSError, http.client.HTTPException):
            pass
    return n


# --- il PDF (senza librerie: i JPEG dello scanner entrano così come sono) -----------------------------------

_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def misure_jpeg(d):
    """(larghezza, altezza, componenti, dpi o 0) dall'intestazione di un JPEG; None se non lo è."""
    if d[:2] != b"\xff\xd8":
        return None
    i, dpi = 2, 0
    while i + 4 <= len(d):
        if d[i] != 0xFF:
            return None
        m = d[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m == 0x01 or 0xD0 <= m <= 0xD8:
            i += 2
            continue
        if m == 0xD9:   # fine dell'immagine senza intestazione
            return None
        lung = struct.unpack(">H", d[i + 2:i + 4])[0]
        seg = d[i + 4:i + 2 + lung]
        if m == 0xE0 and seg[:5] == b"JFIF\0" and len(seg) >= 12:
            unita, xd = seg[7], struct.unpack(">H", seg[8:10])[0]
            dpi = xd if unita == 1 else round(xd * 2.54) if unita == 2 else 0
        elif m in _SOF and len(seg) >= 6:
            a, l = struct.unpack(">HH", seg[1:5])
            return l, a, seg[5], dpi
        i += 2 + lung
    return None


def pdf(pagine):
    """Un PDF dalle PAGINE [(jpeg, dpi)]: una pagina per immagine, grande quanto il foglio scansionato (il dpi del
    JPEG se c'è, sennò quello chiesto)."""
    ogg = [b"", b""]   # 1 catalogo, 2 pagine: si scrivono alla fine

    def nuovo(b):
        ogg.append(b)
        return len(ogg)

    figli = []
    for jpg, dpi in pagine:
        m = misure_jpeg(jpg)
        if not m:
            raise Errore(T("Lo scanner non ha dato un'immagine JPEG", "The scanner didn't send a JPEG image"))
        l, a, comp, dpi_jpeg = m
        dpi = dpi_jpeg if 50 <= dpi_jpeg <= 4800 else dpi
        spazio = {1: b"/DeviceGray", 4: b"/DeviceCMYK"}.get(comp, b"/DeviceRGB")
        decode = b" /Decode [1 0 1 0 1 0 1 0]" if comp == 4 else b""
        im = nuovo(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s /BitsPerComponent 8"
                   b" /Filter /DCTDecode%s /Length %d >>\nstream\n" % (l, a, spazio, decode, len(jpg)) + jpg + b"\nendstream")
        pl, pa = l * 72 / dpi, a * 72 / dpi
        disegno = b"q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q" % (pl, pa)
        c = nuovo(b"<< /Length %d >>\nstream\n" % len(disegno) + disegno + b"\nendstream")
        figli.append(nuovo(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] /Resources << /XObject << /Im0 %d 0 R"
                           b" >> >> /Contents %d 0 R >>" % (pl, pa, im, c)))
    ogg[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    ogg[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in figli), len(figli))
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    pos = []
    for n, b in enumerate(ogg, 1):
        pos.append(len(out))
        out += b"%d 0 obj\n" % n + b + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(ogg) + 1)
    for p in pos:
        out += b"%010d 00000 n \n" % p
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(ogg) + 1, xref)
    return bytes(out)


def salva(pagine, percorso):
    """PDF (tutte le pagine) o JPEG (una per file: "nome (2).jpg" dalla seconda). I file scritti."""
    base, est = os.path.splitext(percorso)
    if est.lower() in (".jpg", ".jpeg"):
        scritti = []
        for i, (jpg, _) in enumerate(pagine):
            p = percorso if i == 0 else f"{base} ({i + 1}){est}"
            with open(p, "wb") as f:
                f.write(jpg)
            scritti.append(p)
        return scritti
    if est.lower() != ".pdf":
        percorso += ".pdf"
    dati = pdf(pagine)
    with open(percorso + ".nuovo", "wb") as f:
        f.write(dati)
    os.replace(percorso + ".nuovo", percorso)
    return [percorso]
