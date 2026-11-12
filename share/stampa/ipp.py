"""ipp.py — chiedere a una stampante di rete chi è e cosa sa fare, prima di aggiungerla (11 novembre 2026).

Pietro, con una stampante Wi-Fi vera: non trovata da sola, e aggiunta con l'indirizzo "funziona anche con un IP a
caso" ma non stampa. L'aggiunta dava per buono `ipp://INDIRIZZO/ipp/print` senza guardare: CUPS creava la coda anche
quando la stampante non c'era o non accettava quel percorso. Qui un client IPP minimo (Get-Printer-Attributes), senza
librerie e senza GTK: lo usano tod-stampa.py (aggiunta per indirizzo, ricerca per indirizzi) e i test.

  attributi(host, porta, percorso, sicura=False)  → (dict degli attributi, "") oppure (None, motivo)
  sonda(host)                                     → (stampante, "") oppure (None, motivo per l'utente)
  indirizzi_vicini(mio_ip), porte_aperte(...)     → chi nella rete di casa ha la porta delle stampanti aperta
stampante = {"uri", "modello", "formati", "device_id", "uuid", "senza_driver", "tipo": "ipp"|"socket"|"lpd", "ip"}
"""
import http.client
import select
import socket
import ssl
import struct

RICHIESTI = ("printer-make-and-model", "printer-info", "printer-name", "document-format-supported", "printer-device-id",
             "printer-uuid", "urf-supported", "printer-state", "printer-state-reasons", "ipp-versions-supported",
             "color-supported", "sides-supported")
PERCORSI = ("ipp/print", "ipp", "printer", "ipp/printer", "ipp/port1", "printers/lp", "")   # AirPrint, poi i percorsi storici
SENZA_DRIVER = ("image/pwg-raster", "image/urf", "application/pdf")


def richiesta(uri, versione=(2, 0), operazione=0x000B, attributi_chiesti=RICHIESTI):
    def voce(tag, nome, valore):
        n, v = nome.encode(), valore.encode()
        return struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(v)) + v
    b = struct.pack(">BBHI", versione[0], versione[1], operazione, 1) + b"\x01"
    b += voce(0x47, "attributes-charset", "utf-8") + voce(0x48, "attributes-natural-language", "en")
    b += voce(0x45, "printer-uri", uri)
    for i, a in enumerate(attributi_chiesti):
        b += voce(0x44, "requested-attributes" if i == 0 else "", a)
    return b + b"\x03"


def leggi(dati):
    """Una risposta IPP → (stato, {nome: [valori]}). Testi come str, interi ed enum come int, il resto come bytes."""
    if len(dati) < 8:
        raise ValueError("risposta troppo corta")
    stato = struct.unpack(">H", dati[2:4])[0]
    out, i, nome = {}, 8, None
    while i < len(dati):
        tag = dati[i]
        i += 1
        if tag == 0x03:
            break
        if tag < 0x10:   # inizio di un gruppo
            continue
        if i + 2 > len(dati):
            break
        ln = struct.unpack(">H", dati[i:i + 2])[0]
        i += 2
        n = dati[i:i + ln].decode("utf-8", "replace")
        i += ln
        if i + 2 > len(dati):
            break
        lv = struct.unpack(">H", dati[i:i + 2])[0]
        i += 2
        v = dati[i:i + lv]
        i += lv
        if n:
            nome = n
        if nome is None:
            continue
        if tag in (0x21, 0x23) and lv == 4:
            v = struct.unpack(">i", v)[0]
        elif tag == 0x22 and lv == 1:
            v = bool(v[0])
        elif tag >= 0x40 or tag in (0x35, 0x36):
            if tag in (0x35, 0x36) and lv >= 4:   # testo con la lingua davanti
                ll = struct.unpack(">H", v[:2])[0]
                v = v[4 + ll:]
            v = v.decode("utf-8", "replace")
        out.setdefault(nome, []).append(v)
    return stato, out


def attributi(host, porta=631, percorso="ipp/print", sicura=False, attesa=5.0):
    """Get-Printer-Attributes. → (attributi, "") o (None, motivo: "muta" = nessuno in ascolto, "no" = non è IPP lì)"""
    uri = f"{'ipps' if sicura else 'ipp'}://{host}:{porta}/{percorso}"
    for versione in ((2, 0), (1, 1)):
        try:
            if sicura:
                c = http.client.HTTPSConnection(host, porta, timeout=attesa, context=ssl._create_unverified_context())
            else:
                c = http.client.HTTPConnection(host, porta, timeout=attesa)
            c.request("POST", "/" + percorso, richiesta(uri, versione), {"Content-Type": "application/ipp"})
            r = c.getresponse()
            corpo = r.read(262144)
            c.close()
        except (OSError, http.client.HTTPException) as e:
            return None, "muta" if isinstance(e, (ConnectionRefusedError, socket.timeout, TimeoutError)) or \
                getattr(e, "errno", None) in (101, 110, 111, 112, 113) else "no"
        if r.status != 200:
            return None, "no"
        try:
            stato, a = leggi(corpo)
        except (ValueError, struct.error):
            return None, "no"
        if stato == 0x0503:   # versione non supportata: le stampanti vecchie parlano solo IPP 1.1
            continue
        if stato >= 0x0100 or not a:
            return None, "no"
        return a, ""
    return None, "no"


def porta_aperta(host, porta, attesa=1.5):
    s = socket.socket()
    s.settimeout(attesa)
    try:
        s.connect((host, porta))
        return True
    except OSError:
        return False
    finally:
        s.close()


def descrivi(a, uri, host):
    primo = lambda k: next((x for x in a.get(k, []) if isinstance(x, str) and x), "")   # noqa: E731
    formati = [x.lower() for x in a.get("document-format-supported", []) if isinstance(x, str)]
    return {
        "uri": uri, "tipo": "ipp", "ip": host,
        "modello": primo("printer-make-and-model") or primo("printer-info") or primo("printer-name") or host,
        "formati": formati, "device_id": primo("printer-device-id"),
        "uuid": primo("printer-uuid").replace("urn:uuid:", "").lower(),
        "senza_driver": any(f in formati for f in SENZA_DRIVER) or bool(a.get("urf-supported")),
        "colore": bool(a.get("color-supported", [False])[0]),
    }


def sonda(host, porte=((631, False), (443, True)), attesa=4.0):
    """Che stampante c'è a HOST? Prova IPP sui percorsi noti, poi le porte delle stampanti senza IPP.
    → (stampante, "") o (None, "spenta" = nessuna risposta | "non-stampante" = risponde ma non è una stampante)"""
    qualcuno = False
    for porta, sicura in porte:
        if not porta_aperta(host, porta, min(attesa, 2.0)):
            continue
        qualcuno = True
        for percorso in PERCORSI:
            a, motivo = attributi(host, porta, percorso, sicura, attesa)
            if a is not None:
                return descrivi(a, f"{'ipps' if sicura else 'ipp'}://{host}:{porta}/{percorso}", host), ""
            if motivo == "muta":
                break
    for porta, tipo, uri in ((9100, "socket", f"socket://{host}:9100"), (515, "lpd", f"lpd://{host}/lp")):
        if porta_aperta(host, porta, min(attesa, 2.0)):
            return {"uri": uri, "tipo": tipo, "ip": host, "modello": host, "formati": [], "device_id": "", "uuid": "",
                    "senza_driver": False, "colore": False}, ""
    if not qualcuno:
        qualcuno = any(porta_aperta(host, p, 1.0) for p in (80, 445, 22))
    return None, "non-stampante" if qualcuno else "spenta"


def rete_di_casa(ip, wifi_ip=None):
    """Si possono cercare le stampanti indirizzo per indirizzo? Solo sul Wi-Fi: mai sulla rete dell'operatore (10.x
    dei dati mobili). WIFI_IP: l'indirizzo Wi-Fi che dice il telefono, se si sa."""
    try:
        a = [int(x) for x in ip.split(".")]
    except (ValueError, AttributeError):
        return False
    if len(a) != 4:
        return False
    if wifi_ip:
        return wifi_ip == ip
    return (a[0] == 192 and a[1] == 168) or (a[0] == 172 and 16 <= a[1] <= 31)


def indirizzi_vicini(ip):
    """Gli altri indirizzi della stessa rete (/24, come quasi tutte le reti di casa)."""
    base = ip.rsplit(".", 1)[0]
    return [f"{base}.{i}" for i in range(1, 255) if f"{base}.{i}" != ip]


def porte_aperte(indirizzi, porta=631, attesa=1.2):
    """Chi tra INDIRIZZI ha PORTA aperta: tutti insieme, senza fili (254 indirizzi in ~1 s)."""
    in_corso = {}
    for h in indirizzi:
        s = socket.socket()
        s.setblocking(False)
        try:
            s.connect_ex((h, porta))
        except OSError:
            s.close()
            continue
        in_corso[s] = h
    aperti, fine = [], __import__("time").time() + attesa
    while in_corso:
        resta = fine - __import__("time").time()
        if resta <= 0:
            break
        _l, pronti, _e = select.select([], list(in_corso), [], resta)
        for s in pronti:
            if s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                aperti.append(in_corso[s])
            s.close()
            del in_corso[s]
    for s in in_corso:
        s.close()
    return aperti
