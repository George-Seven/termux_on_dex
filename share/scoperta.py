"""Chi c'è nella rete di casa, senza root: Bonjour (mDNS), WS-Discovery e il nome NetBIOS dei PC Windows.

Android scarta gli annunci mandati a tutta la rete (multicast) se nessuna app tiene un "multicast lock"; ma le
risposte a una domanda nostra arrivano dirette a noi: Bonjour con il bit QU, WS-Discovery e NetBIOS rispondono
sempre a chi chiede. La usano share/stampa/tod-stampa.py (stampanti) e share/rete/tod-rete.py (PC e NAS).
"""
import re
import socket
import struct
import time
import uuid

MDNS = ("224.0.0.251", 5353)
WSD = ("239.255.255.250", 3702)
PTR, TXT, SRV, A = 12, 16, 33, 1


# --- Bonjour (mDNS) -------------------------------------------------------------------------------------------

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


def chiedi_bonjour(tipi, attesa=2.0):
    """Le risposte Bonjour ai TIPI ("_ipp._tcp.local"...): (record, sorgenti, errore). SORGENTI: nome dell'istanza →
    indirizzo di chi ha risposto (per chi non manda il record A). Senza rete locale (dati mobili): nessuna risposta."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    record, sorgenti, errore = [], {}, ""
    tipi = [t.lower() for t in tipi]

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
            s.sendto(domanda([(t, PTR) for t in tipi]), MDNS)
            raccogli(attesa / 2)
        # chi ha risposto solo con il nome: si chiedono indirizzo e dettagli
        nomi = {d for n, t, d in record if t == PTR and n.lower() in tipi}
        con_srv = {n.lower() for n, t, _ in record if t == SRV}
        con_txt = {n.lower() for n, t, _ in record if t == TXT}
        manca = [(n, SRV) for n in nomi if n.lower() not in con_srv] + [(n, TXT) for n in nomi if n.lower() not in con_txt]
        dest = {d[1] for n, t, d in record if t == SRV}
        con_a = {n.lower() for n, t, _ in record if t == A}
        manca += [(d, A) for d in dest if d.lower() not in con_a]
        if manca:
            s.sendto(domanda(manca[:30]), MDNS)
            raccogli(attesa / 2)
    except OSError as e:
        errore = str(e)
    finally:
        s.close()
    return record, sorgenti, errore


# --- WS-Discovery (i PC Windows e i NAS con wsdd) ---------------------------------------------------------------

def sonda_wsd():
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope" xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery">
<soap:Header><wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>
<wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</wsa:Action>
<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID></soap:Header>
<soap:Body><wsd:Probe/></soap:Body></soap:Envelope>""".encode()


def analizza_wsd(d):
    """Da una ProbeMatches: (tipi, indirizzi XAddrs)."""
    t = re.search(rb"<[^>]*:Types>([^<]*)<", d)
    x = re.search(rb"<[^>]*:XAddrs>([^<]*)<", d)
    return (" ".join(t.group(1).decode(errors="replace").split()) if t else "",
            x.group(1).decode(errors="replace").split() if x else [])


def chiedi_wsd(attesa=2.0):
    """I computer che rispondono a WS-Discovery: {indirizzo: tipi}. Le stampanti (wprt:) restano fuori."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    trovati = {}
    try:
        s.bind(("", 0))
        s.sendto(sonda_wsd(), WSD)
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
            tipi, _ = analizza_wsd(d)
            if "Computer" in tipi:
                trovati[ip] = tipi
    except OSError:
        pass
    finally:
        s.close()
    return trovati


# --- NetBIOS: il nome di un PC Windows ---------------------------------------------------------------------------

def pacchetto_nbstat():
    """Domanda "chi sei" (NBSTAT, nome "*") per la porta 137 di un PC."""
    nome = b"*" + b"\0" * 15
    codificato = bytes(c for b in nome for c in (0x41 + (b >> 4), 0x41 + (b & 15)))
    return struct.pack(">HHHHHH", 0x746f, 0, 1, 0, 0, 0) + b"\x20" + codificato + b"\0" + struct.pack(">HH", 0x21, 1)


def analizza_nbstat(d):
    """Il nome del computer dalla risposta: il primo nome unico (non di gruppo) con suffisso 0x00 o 0x20."""
    try:
        i = 12
        i = i + 2 if d[i] & 0xC0 == 0xC0 else i + 1 + d[i] + 1   # nome (o puntatore)
        i += 10   # tipo, classe, durata, lunghezza
        n = d[i]
        i += 1
        for _ in range(n):
            nome, suff, flag = d[i:i + 15], d[i + 15], struct.unpack(">H", d[i + 16:i + 18])[0]
            i += 18
            if suff in (0x00, 0x20) and not flag & 0x8000:
                return nome.decode("latin-1").strip()
    except (IndexError, struct.error):
        pass
    return ""


def nome_netbios(ip, attesa=1.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(attesa)
    try:
        s.sendto(pacchetto_nbstat(), (ip, 137))
        d, _ = s.recvfrom(2048)
        return analizza_nbstat(d)
    except OSError:
        return ""
    finally:
        s.close()
