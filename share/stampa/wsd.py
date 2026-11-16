"""Gli scanner WSD (WS-Scan, "Scanner di rete" di Windows): trovarli, chiedere cosa sanno fare, scansionare.

13 novembre, Pietro: "su Windows funziona lo scanner di rete". La sua Ricoh MP C2011 non ha eSCL (404) ma risponde a
WS-Discovery (solo in multicast) come wscn:ScanDeviceType. È SOAP su HTTP:
- Probe multicast su 239.255.255.250:3702 → ProbeMatch: l'identità (urn:uuid) e l'indirizzo del dispositivo (XAddrs);
- WS-Transfer Get al dispositivo (wsa:To = l'identità) → i servizi ospitati: quello con ScannerServiceType è lo scanner;
- GetScannerElements → ScannerConfiguration (vetro, alimentatore, risoluzioni, colori, formati; misure in 1/1000 di pollice);
- CreateScanJob con lo ScanTicket → JobId e JobToken; RetrieveImage finché lo scanner dice ClientErrorNoImagesAvailable
  (immagini in MTOM: multipart/related, la parte binaria è il JPEG).
Lo scanner entra nell'elenco di escl.py con l'indirizzo "wsd+<indirizzo del servizio>": escl.capacita e escl.scansiona
passano la mano a questo modulo, così la finestra Scanner e le Impostazioni non cambiano. Le misure sono convertite nelle
unità di eSCL (1/300 di pollice) e le impostazioni lette dallo ScanSettings di eSCL che la finestra prepara già.
Prove: tests/wsd_finto.py (scanner finto) e test_scanner_wsd in tests/run.sh.
"""
import http.client
import os
import re
import socket
import sys
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import escl  # noqa: E402

T, Errore = escl.T, escl.Errore
PREFISSO = "wsd+"
WSD = ("239.255.255.250", 3702)
NS = {"soap": "http://www.w3.org/2003/05/soap-envelope", "wsa": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
      "wsd": "http://schemas.xmlsoap.org/ws/2005/04/discovery", "wsdp": "http://schemas.xmlsoap.org/ws/2006/02/devprof",
      "wscn": "http://schemas.microsoft.com/windows/2006/08/wdp/scan",
      "mex": "http://schemas.xmlsoap.org/ws/2004/09/mex"}
AZ_SCAN = NS["wscn"] + "/"
ANONIMO = NS["wsa"] + "/role/anonymous"
OCCUPATO = ("ServerErrorNotAcceptingJobs", "ServerErrorTemporaryError", "ScannerBusy", "ServerErrorBusy")


def e_wsd(url):
    return (url or "").startswith(PREFISSO)


def _nome(e):
    return e.tag.rsplit("}", 1)[-1]


def _figli(e, nome):
    return [x for x in e.iter() if _nome(x) == nome]


def _testo(e, nome, d=""):
    for x in e.iter():
        if _nome(x) == nome and x.text and x.text.strip():
            return x.text.strip()
    return d


# --- SOAP ---------------------------------------------------------------------------------------------------------

def busta(to, azione, corpo=""):
    return (f'<?xml version="1.0" encoding="utf-8"?>\n'
            f'<soap:Envelope xmlns:soap="{NS["soap"]}" xmlns:wsa="{NS["wsa"]}" xmlns:wscn="{NS["wscn"]}">'
            f"<soap:Header><wsa:To>{to}</wsa:To><wsa:Action>{azione}</wsa:Action>"
            f"<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>"
            f"<wsa:ReplyTo><wsa:Address>{ANONIMO}</wsa:Address></wsa:ReplyTo></soap:Header>"
            f"<soap:Body>{corpo}</soap:Body></soap:Envelope>").encode()


class Guasto(Exception):
    """Un soap:Fault: il codice (il Subcode più interno, senza prefisso) e il motivo."""
    def __init__(self, codice, motivo):
        super().__init__(f"{codice}: {motivo}")
        self.codice, self.motivo = codice, motivo


def _parti(tipo, dati):
    """Le parti di un multipart/related (MTOM): [(intestazioni, corpo)]."""
    m = re.search(r'boundary="?([^";]+)"?', tipo, re.I)
    if not m:
        return []
    sep = b"--" + m.group(1).encode()
    out = []
    for pezzo in dati.split(sep)[1:]:
        if pezzo.startswith(b"--"):
            break
        pezzo = pezzo[2:] if pezzo.startswith(b"\r\n") else pezzo.lstrip(b"\n")
        testa, _, corpo = pezzo.partition(b"\r\n\r\n")
        if corpo.endswith(b"\r\n"):
            corpo = corpo[:-2]
        h = {}
        for riga in testa.decode(errors="replace").split("\r\n"):
            k, _, v = riga.partition(":")
            h[k.strip().lower()] = v.strip()
        out.append((h, corpo))
    return out


def chiama(url, azione, corpo="", to=None, attesa=30):
    """POST SOAP. → (radice XML della risposta, [allegati binari]). Guasto per un soap:Fault, OSError per la rete."""
    u = urllib.parse.urlsplit(url)
    if not u.hostname:
        raise OSError(f"indirizzo non valido: {url}")
    c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=attesa)
    try:
        c.request("POST", u.path or "/", body=busta(to or url, azione, corpo),
                  headers={"Content-Type": "application/soap+xml; charset=utf-8", "Connection": "close"})
        r = c.getresponse()
        tipo, dati = r.getheader("Content-Type", ""), r.read()
    finally:
        c.close()
    allegati = []
    if "multipart" in tipo.lower():
        parti = _parti(tipo, dati)
        xml = next((b for h, b in parti if "xml" in h.get("content-type", "")), b"")
        allegati = [b for h, b in parti if "xml" not in h.get("content-type", "")]
    else:
        xml = dati
    try:
        radice = ET.fromstring(xml)
    except ET.ParseError:
        raise OSError(f"risposta non valida ({r.status})")
    guasto = next(iter(_figli(radice, "Fault")), None)
    if guasto is not None:
        codici = [x.text.strip() for x in guasto.iter() if _nome(x) == "Value" and x.text]
        raise Guasto(codici[-1].rsplit(":", 1)[-1] if codici else str(r.status), _testo(guasto, "Text"))
    if r.status >= 400:
        raise OSError(f"HTTP {r.status}")
    return radice, allegati


# --- trovarli -----------------------------------------------------------------------------------------------------

def sonda():
    return (f'<?xml version="1.0" encoding="utf-8"?>\n<soap:Envelope xmlns:soap="{NS["soap"]}" xmlns:wsa="{NS["wsa"]}" '
            f'xmlns:wsd="{NS["wsd"]}" xmlns:wscn="{NS["wscn"]}"><soap:Header>'
            f"<wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>"
            f"<wsa:Action>{NS['wsd']}/Probe</wsa:Action><wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>"
            f"</soap:Header><soap:Body><wsd:Probe><wsd:Types>wscn:ScanDeviceType</wsd:Types></wsd:Probe>"
            f"</soap:Body></soap:Envelope>").encode()


def analizza_risposta(d):
    """Da una ProbeMatch: (identità, [XAddrs], tipi) o None se non è uno scanner."""
    try:
        r = ET.fromstring(d)
    except ET.ParseError:
        return None
    tipi = " ".join(_testo(r, "Types").split())
    if "ScanDeviceType" not in tipi:
        return None
    ident = ""
    for ep in _figli(r, "EndpointReference"):
        ident = _testo(ep, "Address") or ident
    return ident, _testo(r, "XAddrs").split(), tipi


def scopri(attesa=2.0, dest=WSD):
    """Gli scanner WSD che rispondono alla sonda: {identità: (ip, [XAddrs])}. Due invii: l'UDP si perde."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    trovati = {}
    try:
        s.bind(("", 0))
        for _ in range(2):
            s.sendto(sonda(), dest)
        fine = time.time() + attesa
        while True:
            resta = fine - time.time()
            if resta <= 0:
                break
            s.settimeout(resta)
            try:
                d, (ip, _) = s.recvfrom(65535)
            except OSError:
                break
            r = analizza_risposta(d)
            if r and r[0]:
                trovati.setdefault(r[0], (ip, r[1]))
    except OSError:
        pass
    finally:
        s.close()
    return trovati


def descrivi(ident, xaddrs, ip, attesa=5):
    """WS-Transfer Get al dispositivo → la voce per l'elenco (url del servizio di scansione), o None."""
    for x in xaddrs:
        u = urllib.parse.urlsplit(x)
        if u.scheme != "http":   # (https: certificati fatti da sé, e nessuno scanner visto lo vuole solo)
            continue
        if u.hostname and u.hostname != ip and not re.match(r"^[\d.]+$", u.hostname):
            x = x.replace(u.hostname, ip, 1)   # un nome che Android non risolve: l'indirizzo da cui ha risposto
        try:
            r, _ = chiama(x, "http://schemas.xmlsoap.org/ws/2004/09/transfer/Get", to=ident, attesa=attesa)
        except (OSError, Guasto, http.client.HTTPException):
            continue
        servizio = ""
        for h in _figli(r, "Hosted"):
            if "ScannerServiceType" in _testo(h, "Types"):
                servizio = _testo(h, "Address")
                break
        if not servizio:
            continue
        su = urllib.parse.urlsplit(servizio)
        if su.hostname and su.hostname != ip:
            servizio = servizio.replace(su.hostname, ip, 1)
        nome = _testo(r, "FriendlyName") or " ".join(filter(None, (_testo(r, "Manufacturer"), _testo(r, "ModelName"))))
        return {"chiave": f"wsd:{ident.lower()}", "nome": nome or ip, "url": PREFISSO + servizio, "dove": "wifi",
                "ip": ip}
    return None


_descritti = {}   # identità → (quando, voce): la descrizione si richiede ogni 10 minuti, non a ogni ricerca


def cerca(attesa=2.0, ora=None):
    ora = time.time() if ora is None else ora
    out = []
    for ident, (ip, xaddrs) in scopri(attesa).items():
        quando, voce = _descritti.get(ident, (0, None))
        if not voce or voce["ip"] != ip or ora - quando > 600:
            voce = descrivi(ident, xaddrs, ip)
            _descritti[ident] = (ora, voce)
        if voce:
            out.append(dict(voce))
    return out


# --- cosa sa fare -------------------------------------------------------------------------------------------------

def _300(millesimi, d):
    """1/1000 di pollice (WSD) → 1/300 (eSCL)."""
    try:
        return int(int(millesimi) * 300 / 1000)
    except (TypeError, ValueError):
        return d


def _fonte(e, prefisso, formati):
    colori = [x.text.strip() for x in _figli(e, "ColorEntry") if x.text and x.text.strip() in ("RGB24", "Grayscale8")]
    dpi = set()
    for r in _figli(e, prefisso + "Resolutions"):
        larghezze = {_int(x.text) for x in _figli(r, "Width") if x.text}
        altezze = {_int(x.text) for x in _figli(r, "Height") if x.text}
        dpi |= {d for d in larghezze if d and (not altezze or d in altezze)}
    massimo = next(iter(_figli(e, prefisso + "MaximumSize")), None)
    return {"colori": colori or ["RGB24"], "formati": formati, "dpi": sorted(dpi) or [300],
            "max_l": _300(_testo(massimo, "Width") if massimo is not None else None, 2550),
            "max_a": _300(_testo(massimo, "Height") if massimo is not None else None, 3508)}


def _int(s):
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def analizza_capacita(radice):
    """Da GetScannerElementsResponse: la stessa forma di escl.analizza_capacita (versione "wsd")."""
    formati = [x.text.strip() for x in _figli(radice, "FormatValue") if x.text]
    out = {"modello": _testo(radice, "ScannerName"), "versione": "wsd", "fonti": {}, "formati_wsd": formati}
    fmt = ["image/jpeg"] if "jfif" in formati or not formati else []
    piano = next(iter(_figli(radice, "Platen")), None)
    if piano is not None:
        out["fonti"]["Platen"] = _fonte(piano, "Platen", fmt)
    adf = next(iter(_figli(radice, "ADFFront")), None)
    if adf is not None:
        out["fonti"]["Feeder"] = _fonte(adf, "ADF", fmt)
        out["fonti"]["Feeder"]["fronte_retro"] = _testo(radice, "ADFSupportsDuplex").lower() in ("true", "1")
    if not out["fonti"]:
        raise Errore(T("Lo scanner non dice cosa sa fare", "The scanner doesn't say what it can do"))
    if not fmt:
        raise Errore(T(f"Lo scanner non dà immagini JPEG (solo {', '.join(formati)})",
                       f"The scanner doesn't send JPEG images (only {', '.join(formati)})"))
    return out


def elementi_xml():
    return ("<wscn:GetScannerElementsRequest><wscn:RequestedElements>"
            "<wscn:Name>wscn:ScannerDescription</wscn:Name><wscn:Name>wscn:ScannerConfiguration</wscn:Name>"
            "<wscn:Name>wscn:ScannerStatus</wscn:Name></wscn:RequestedElements></wscn:GetScannerElementsRequest>")


def capacita(url, attesa=10):
    try:
        r, _ = chiama(url[len(PREFISSO):], AZ_SCAN + "GetScannerElements", elementi_xml(), attesa=attesa)
    except Guasto as g:
        raise Errore(T(f"Lo scanner ha risposto: {g.motivo or g.codice}", f"The scanner answered: {g.motivo or g.codice}"))
    except (OSError, http.client.HTTPException) as e:
        raise Errore(T(f"Scanner non raggiungibile ({e})", f"Scanner not reachable ({e})"))
    return analizza_capacita(r)


# --- scansionare --------------------------------------------------------------------------------------------------

def da_escl(xml):
    """Lo ScanSettings di eSCL (escl.impostazioni_xml) → {fonte, colore, dpi, larghezza, altezza (1/300), fronte_retro}."""
    r = ET.fromstring(xml)
    return {"fonte": _testo(r, "InputSource", "Platen"), "colore": _testo(r, "ColorMode", "RGB24"),
            "dpi": _int(_testo(r, "XResolution", "300")) or 300, "larghezza": _int(_testo(r, "Width", "2480")),
            "altezza": _int(_testo(r, "Height", "3508")), "fronte_retro": _testo(r, "Duplex") == "true"}


def biglietto_xml(s):
    """Lo ScanTicket di CreateScanJob. Misure da 1/300 a 1/1000 di pollice."""
    l, a = round(s["larghezza"] * 1000 / 300), round(s["altezza"] * 1000 / 300)
    fonte = "Platen" if s["fonte"] == "Platen" else ("ADFDuplex" if s["fronte_retro"] else "ADF")
    lato = (f"<wscn:ScanRegion><wscn:ScanRegionXOffset>0</wscn:ScanRegionXOffset>"
            f"<wscn:ScanRegionYOffset>0</wscn:ScanRegionYOffset><wscn:ScanRegionWidth>{l}</wscn:ScanRegionWidth>"
            f"<wscn:ScanRegionHeight>{a}</wscn:ScanRegionHeight></wscn:ScanRegion>"
            f"<wscn:ColorProcessing>{s['colore']}</wscn:ColorProcessing>"
            f"<wscn:Resolution><wscn:Width>{s['dpi']}</wscn:Width><wscn:Height>{s['dpi']}</wscn:Height></wscn:Resolution>")
    lati = f"<wscn:MediaFront>{lato}</wscn:MediaFront>" + (f"<wscn:MediaBack>{lato}</wscn:MediaBack>"
                                                          if fonte == "ADFDuplex" else "")
    return ("<wscn:CreateScanJobRequest><wscn:ScanTicket><wscn:JobDescription><wscn:JobName>Scansione</wscn:JobName>"
            "<wscn:JobOriginatingUserName>DeX</wscn:JobOriginatingUserName></wscn:JobDescription>"
            "<wscn:DocumentParameters><wscn:Format>jfif</wscn:Format>"
            f"<wscn:ImagesToTransfer>{1 if fonte == 'Platen' else 0}</wscn:ImagesToTransfer>"
            f"<wscn:InputSource>{fonte}</wscn:InputSource>"
            f"<wscn:InputSize><wscn:InputMediaSize><wscn:Width>{l}</wscn:Width><wscn:Height>{a}</wscn:Height>"
            "</wscn:InputMediaSize></wscn:InputSize>"
            f"<wscn:MediaSides>{lati}</wscn:MediaSides></wscn:DocumentParameters></wscn:ScanTicket>"
            "</wscn:CreateScanJobRequest>")


def immagine_xml(lavoro, gettone, n):
    return (f"<wscn:RetrieveImageRequest><wscn:JobId>{lavoro}</wscn:JobId><wscn:JobToken>{gettone}</wscn:JobToken>"
            f"<wscn:DocumentDescription><wscn:DocumentName>Pagina{n:03d}.jpg</wscn:DocumentName>"
            "</wscn:DocumentDescription></wscn:RetrieveImageRequest>")


def scansiona(url, xml, pagina, annulla=None, una_sola=False, pausa=1.0, attesa=180, tentativi=60):
    """Come escl.scansiona: CreateScanJob, poi RetrieveImage una pagina dopo l'altra (PAGINA(jpeg)) finché lo scanner
    dice che non ce ne sono più. Occupato = si riprova. Alla fine CancelJob se si è annullato a metà. Quante pagine."""
    servizio = url[len(PREFISSO):]
    s = da_escl(xml)

    def errore_rete(e):
        return Errore(T(f"Scanner non raggiungibile ({e})", f"Scanner not reachable ({e})"))

    risposta = None
    for _ in range(tentativi):
        if annulla and annulla.is_set():
            return 0
        try:
            risposta, _ = chiama(servizio, AZ_SCAN + "CreateScanJob", biglietto_xml(s), attesa=30)
            break
        except Guasto as g:
            if g.codice in OCCUPATO:
                escl._aspetta(pausa, annulla)
                continue
            if "Feeder" in g.codice or "NoImages" in g.codice or "MediaEmpty" in g.codice:
                raise Errore(T("Nessun foglio da scansionare: mettilo nell'alimentatore", "Nothing to scan: load the feeder"))
            raise Errore(T(f"Lo scanner ha rifiutato la scansione ({g.motivo or g.codice})",
                           f"The scanner refused to scan ({g.motivo or g.codice})"))
        except (OSError, http.client.HTTPException) as e:
            raise errore_rete(e)
    if risposta is None:
        raise Errore(T("Lo scanner è occupato: riprova tra poco", "The scanner is busy: try again shortly"))
    lavoro, gettone = _testo(risposta, "JobId"), _testo(risposta, "JobToken")
    n, occupato, finito = 0, 0, False
    try:
        while not (annulla and annulla.is_set()):
            try:
                _, allegati = chiama(servizio, AZ_SCAN + "RetrieveImage", immagine_xml(lavoro, gettone, n + 1),
                                     attesa=attesa)
            except Guasto as g:
                if g.codice == "ClientErrorNoImagesAvailable" and n:
                    finito = True
                    break
                if g.codice in OCCUPATO and occupato < tentativi:
                    occupato += 1
                    escl._aspetta(pausa, annulla)
                    continue
                if g.codice == "ClientErrorNoImagesAvailable":
                    raise Errore(T("Nessun foglio da scansionare: mettilo nell'alimentatore", "Nothing to scan: load the feeder"))
                raise Errore(T(f"Lo scanner si è fermato ({g.motivo or g.codice})", f"The scanner stopped ({g.motivo or g.codice})"))
            except (OSError, http.client.HTTPException) as e:
                raise errore_rete(e)
            dati = next((a for a in allegati if a[:2] == b"\xff\xd8"), None)
            if not dati:
                raise Errore(T("Lo scanner non ha dato un'immagine JPEG", "The scanner didn't send a JPEG image"))
            n, occupato = n + 1, 0
            pagina(dati)
            if una_sola or s["fonte"] == "Platen":
                finito = True
                break
    finally:
        if not finito and lavoro:
            try:
                chiama(servizio, AZ_SCAN + "CancelJob", f"<wscn:CancelJobRequest><wscn:JobId>{lavoro}</wscn:JobId>"
                       "</wscn:CancelJobRequest>", attesa=10)
            except (OSError, Guasto, http.client.HTTPException):
                pass
    return n
