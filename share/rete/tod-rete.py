#!/usr/bin/env python3
"""La Rete in Esplora file: i PC e i NAS di casa come cartelle, in /media/rete/<PC>/<cartella condivisa>.

Ogni PC ha il suo share/rete/tod-smb (Samba), che tod-fs.so raggiunge come le partizioni dei dischi USB: le cartelle
condivise sono cartelle vere per ogni app del desktop, anche nel container. Qui:
- si cercano i PC ogni minuto: Bonjour (_smb._tcp: Mac e NAS) e WS-Discovery (Windows, NAS con wsdd), con il nome
  NetBIOS per i Windows; più quelli aggiunti con l'indirizzo (~/.config/tod/rete.json). Un PC che non risponde da
  10 minuti sparisce (quelli aggiunti a mano restano);
- l'elenco va in $TMPDIR/tod-rete/volumi (lo legge tod-fs.so) e per ogni PC gira un tod-smb;
- quando un PC vuole nome utente e password, tod-smb li chiede qui ($TMPDIR/tod-rete/servizio.sock): dal portachiavi
  del desktop, o con una finestra. La prima volta si prova da ospite: le cartelle aperte a tutti non chiedono niente.

  tod-rete.py sorveglia | ferma | stato | cerca [--json] | elenco --json
  tod-rete.py aggiungi INDIRIZZO [NOME] | togli NOME | dimentica NOME
"""
import fcntl
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scoperta  # noqa: E402

PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
TMP = os.environ.get("TMPDIR", os.path.join(PREFIX, "tmp"))
TH = os.environ.get("TOD_TERMUX_HOME") or os.environ.get("HOME", "")
RUN = os.path.join(TMP, "tod-rete")
VOLUMI = os.path.join(RUN, "volumi")
SOCK = os.path.join(RUN, "servizio.sock")
CONF = os.path.join(TH, ".config/tod/rete.json")
SMB = os.environ.get("TOD_SMB_BIN") or os.path.join(TH, ".local/share/tod-rete/tod-smb")
ITALIAN = os.environ.get("LANGUAGE", os.environ.get("LANG", "")).startswith("it")
OGNI = 60       # secondi tra una ricerca e l'altra
SCADE = 600     # un PC che non risponde da 10 minuti sparisce
PAUSA = 15      # dopo "Annulla" quel PC non chiede la password per 15 secondi: Esplora file riprova di continuo, e l'8
                # novembre la FRITZ!Box ha riaperto la finestra 20 volte in 20 secondi
SCHEMA = "org.termuxondex.Rete"


def T(it, en):
    return it if ITALIAN else en


def log(msg):
    print(f"{time.strftime('%F %T')} {msg}", flush=True)


def alias_di(nome):
    """Come alias_di di tod-fs.c, byte per byte: il nome del socket (lkl-<alias>.sock)."""
    out = []
    for b in nome.encode():
        c = chr(b)
        c = "-" if c == " " else "_" if c == "/" else c.lower() if "A" <= c <= "Z" else c
        out.append(c if (c.isascii() and (c.isalnum() or c in "._-")) else "-")
    return "".join(out)


def nome_cartella(n):
    """Il nome del PC come cartella di /media/rete: niente / né caratteri di controllo."""
    n = "".join(c if c.isprintable() and c not in "/\t\n" else "-" for c in n).strip(" .")
    return n or "PC"


# --- configurazione: PC aggiunti a mano, utente ricordato per PC (le password stanno nel portachiavi) ----------

def carica_conf():
    try:
        c = json.load(open(CONF))
    except (OSError, ValueError):
        c = {}
    c.setdefault("pc", [])            # [{"nome", "indirizzo"}]
    c.setdefault("credenziali", {})   # nome → {"utente", "dominio", "indirizzo", "porta"} (l'ultimo indirizzo visto)
    return c


CONF_M = threading.Lock()   # le scritture del servizio (fili diversi): carica, cambia e salva insieme


def salva_conf(c):
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    with open(CONF + ".nuovo", "w") as f:
        json.dump(c, f, indent=1, ensure_ascii=False)
    os.replace(CONF + ".nuovo", CONF)


def segreto():
    """Il portachiavi del desktop (libsecret: gnome-keyring); None se non c'è."""
    try:
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret
        return Secret, Secret.Schema.new(SCHEMA, Secret.SchemaFlags.NONE,
                                         {"pc": Secret.SchemaAttributeType.STRING,
                                          "utente": Secret.SchemaAttributeType.STRING})
    except (ImportError, ValueError):
        return None


def password_di(pc, utente):
    s = segreto()
    if not s:
        return None
    try:
        return s[0].password_lookup_sync(s[1], {"pc": pc, "utente": utente}, None)
    except Exception:   # portachiavi chiuso o assente
        return None


def ricorda_password(pc, utente, password):
    s = segreto()
    if not s:
        return False
    try:
        return s[0].password_store_sync(s[1], {"pc": pc, "utente": utente}, s[0].COLLECTION_DEFAULT,
                                        T(f"Cartelle condivise di {pc}", f"Shared folders on {pc}"), password, None)
    except Exception:
        return False


def pc_ricordati(conf):
    """I PC con la password salvata e il loro ultimo indirizzo, {nome: (ip, porta)}: la ricerca li prova subito. 8 novembre:
    DESKTOP-I78AUDA di Pietro risponde alla ricerca 3 volte su 4, e dopo l'avvio compariva solo dopo un minuto."""
    manuali = {nome_cartella(m["nome"]) for m in conf["pc"]}   # (quelli aggiunti a mano ci sono sempre)
    return {n: (c["indirizzo"], int(c.get("porta", 445))) for n, c in conf["credenziali"].items()
            if c.get("indirizzo") and n not in manuali}


def ricorda_indirizzo(nome, ip, porta):
    """L'ultimo indirizzo di un PC con la password salvata (il router può dargliene un altro)."""
    with CONF_M:
        conf = carica_conf()
        c = conf["credenziali"].get(nome)
        if c is None or (c.get("indirizzo"), int(c.get("porta", 445))) == (ip, porta):
            return
        c["indirizzo"], c["porta"] = ip, porta
        salva_conf(conf)


def dimentica_password(pc, utente):
    s = segreto()
    if s:
        try:
            s[0].password_clear_sync(s[1], {"pc": pc, "utente": utente}, None)
        except Exception:
            pass


# --- la ricerca dei PC ----------------------------------------------------------------------------------------

def nome_da_dns(ip):
    try:
        return socket.gethostbyaddr(ip)[0].split(".")[0]
    except OSError:
        return ""


def solo_indirizzo(nome):
    """"192-168-178-1": un nome che è solo l'indirizzo (la FRITZ!Box risponde così a NetBIOS e a Bonjour)."""
    return bool(re.match(r"^\d{1,3}([-.]\d{1,3}){3}$", nome or ""))


def candidati(record, sorgenti, wsd):
    """Dalle risposte: {indirizzo: {"nomi": [nomi Bonjour], "porta", "da"}}. Un nome con più indirizzi (8 novembre: "fritz.box"
    era anche 192.168.178.96, che non è un PC) prende quello di chi ha risposto."""
    indirizzi, per = {}, {}
    for nome, tipo, dati in record:
        if tipo == scoperta.A:
            indirizzi.setdefault(nome.lower(), [])
            if dati not in indirizzi[nome.lower()]:
                indirizzi[nome.lower()].append(dati)
        else:
            per.setdefault(nome.lower(), {}).setdefault(tipo, dati)
    trovati = {}
    for nome, tipo, dati in record:
        if tipo != scoperta.PTR or nome.lower() != "_smb._tcp.local":
            continue
        srv = per.get(dati.lower(), {}).get(scoperta.SRV)
        if not srv:
            continue
        chi = sorgenti.get(dati.lower())
        ips = indirizzi.get(srv[1].lower(), [])
        ip = chi if chi and (chi in ips or not ips) else (ips[0] if ips else None)
        if not ip:
            continue
        c = trovati.setdefault(ip, {"nomi": [], "porta": srv[0], "da": "bonjour"})
        istanza = dati.split("._smb")[0]
        if istanza not in c["nomi"]:
            c["nomi"].append(istanza)
    for ip in wsd:
        trovati.setdefault(ip, {"nomi": [], "porta": 445, "da": "wsd"})
    return trovati


def nome_migliore(nomi, ip):
    """Il primo nome che non sia solo l'indirizzo; sennò "PC <indirizzo>"."""
    return next((n for n in nomi if n and not solo_indirizzo(n)), f"PC {ip}")


def porta_aperta(ip, porta, attesa=1.0):
    try:
        socket.create_connection((ip, porta), attesa).close()
        return True
    except OSError:
        return False


def stesso_pc(nome, netbios):
    """Il nome NetBIOS (al più 15 lettere) è di questo PC? Vuoto o solo l'indirizzo: non si sa, si tiene."""
    if not netbios or solo_indirizzo(netbios):
        return True
    base, nb = nome.split(" (")[0].lower(), netbios.lower()
    return base == nb or (len(nb) == 15 and base.startswith(nb))


def cerca_pc(attesa=2.0, ricordati=None):
    """I PC e i NAS in rete con le cartelle condivise (porta SMB aperta): [{"nome", "ip", "porta", "da"}]. RICORDATI
    (pc_ricordati) si provano anche al loro ultimo indirizzo, se la ricerca non li ha trovati."""
    record, sorgenti, _ = scoperta.chiedi_bonjour(["_smb._tcp.local"], attesa)
    trovati = candidati(record, sorgenti, scoperta.chiedi_wsd(attesa))
    for nome, (ip, porta) in (ricordati or {}).items():
        if ip not in trovati:
            trovati[ip] = {"nomi": [nome], "porta": porta, "da": "ricordato"}
    out, m = [], threading.Lock()

    def uno(ip, c):
        if not porta_aperta(ip, c["porta"]):
            return
        if c["da"] == "ricordato" and not stesso_pc(c["nomi"][0], scoperta.nome_netbios(ip)):
            return   # a quell'indirizzo ora c'è un altro PC
        nomi = list(c["nomi"])
        if not any(not solo_indirizzo(n) for n in nomi):   # il nome dei Windows, poi quello del router (DNS)
            nomi += [scoperta.nome_netbios(ip), nome_da_dns(ip)]
        with m:
            out.append({"nome": nome_migliore(nomi, ip), "ip": ip, "porta": c["porta"], "da": c["da"]})

    fili = [threading.Thread(target=uno, args=(ip, c)) for ip, c in trovati.items()]
    for f in fili:
        f.start()
    for f in fili:
        f.join()
    return sorted(out, key=lambda p: p["ip"])


def tutti_i_nomi(pc_list):
    """Nomi unici per le cartelle: due PC con lo stesso nome prendono anche l'indirizzo."""
    visti, out = {}, []
    for p in pc_list:
        n = nome_cartella(p["nome"])
        if n.lower() in visti:
            n = f"{n} ({p['ip']})"
        visti[n.lower()] = 1
        out.append(dict(p, nome=n))
    return out


# --- il servizio -------------------------------------------------------------------------------------------------

class Servizio:
    def __init__(self):
        self.pc = {}          # nome → {"ip", "porta", "visto", "manuale"}
        self.proc = {}        # nome → Popen di tod-smb
        self.memoria = {}     # nome → {"utente", "dominio", "password"}: le credenziali di questa sessione
        self.rifiutate = set()
        self.annullate = {}   # nome → quando è stato premuto "Annulla" (PAUSA)
        self.lucchetti = {}   # nome → Lock: una finestra alla volta per PC
        self.m = threading.Lock()
        self.fine = False
        self.conf_mtime = 0

    # tod-smb: uno per PC, nel suo gruppo di processi (con i figli dei collegamenti)
    def avvia_smb(self, nome, p):
        if not os.access(SMB, os.X_OK):
            return
        dove = p["ip"] if p.get("porta", 445) == 445 else f"{p['ip']}:{p['porta']}"
        amb = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
        out = open(os.path.join(RUN, "smb.log"), "ab")
        self.proc[nome] = subprocess.Popen([SMB, RUN, alias_di(nome), dove, nome], env=amb, stdin=subprocess.DEVNULL,
                                           stdout=out, stderr=out, start_new_session=True)

    def ferma_smb(self, nome):
        p = self.proc.pop(nome, None)
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            os.unlink(os.path.join(RUN, f"lkl-{alias_di(nome)}.sock"))
        except OSError:
            pass

    def scrivi_volumi(self):
        righe = "".join(f"{n}\t{p['ip']}\n" for n, p in sorted(self.pc.items(), key=lambda x: x[0].lower()))
        try:
            if open(VOLUMI).read() == righe:
                return
        except OSError:
            pass
        with open(VOLUMI + ".nuovo", "w") as f:
            f.write(righe)
        os.replace(VOLUMI + ".nuovo", VOLUMI)

    def aggiorna(self, trovati):
        """Elenco nuovo: trovati in rete + aggiunti a mano. I tod-smb seguono."""
        adesso = time.time()
        conf = carica_conf()
        with self.m:
            for p in trovati:
                vecchio = next((n for n, q in self.pc.items() if q["ip"] == p["ip"]), None)
                if vecchio and vecchio != p["nome"] and not self.pc[vecchio].get("manuale"):
                    self.ferma_smb(vecchio)
                    del self.pc[vecchio]
                if not (vecchio and self.pc.get(vecchio, {}).get("manuale")):
                    self.pc[p["nome"]] = {"ip": p["ip"], "porta": p["porta"], "visto": adesso, "manuale": False}
            for m in conf["pc"]:
                nome = nome_cartella(m["nome"])
                self.pc[nome] = {"ip": m["indirizzo"], "porta": int(m.get("porta", 445)), "visto": adesso, "manuale": True}
            manuali = {nome_cartella(m["nome"]) for m in conf["pc"]}
            for nome in list(self.pc):
                q = self.pc[nome]
                if (q["manuale"] and nome not in manuali) or (not q["manuale"] and adesso - q["visto"] > SCADE):
                    self.ferma_smb(nome)
                    del self.pc[nome]
            for nome, q in self.pc.items():
                if nome not in self.proc or self.proc[nome].poll() is not None:
                    self.avvia_smb(nome, q)
            self.scrivi_volumi()
        for p in trovati:
            if p["nome"] in conf["credenziali"]:
                ricorda_indirizzo(p["nome"], p["ip"], p["porta"])

    def giro_ricerca(self):
        ultima = 0.0
        while not self.fine:
            try:
                mt = os.stat(CONF).st_mtime
            except OSError:
                mt = 0
            if time.time() - ultima >= OGNI:
                ultima = time.time()
                try:
                    self.aggiorna(tutti_i_nomi(cerca_pc(ricordati=pc_ricordati(carica_conf()))))
                except Exception as e:
                    log(f"ricerca: {type(e).__name__}: {e}")
            elif mt != self.conf_mtime:   # un PC aggiunto o tolto (tod rete aggiungi, Impostazioni)
                self.conf_mtime = mt
                self.aggiorna([])
            else:
                with self.m:   # un tod-smb caduto si riaccende
                    for nome, q in self.pc.items():
                        if nome not in self.proc or self.proc[nome].poll() is not None:
                            self.avvia_smb(nome, q)
            time.sleep(2)

    # le richieste di tod-smb: "AUTH\t<nome>\t<pc>\t<cartella>\t<motivo>", motivo primo|ospite|rifiutate
    def risposta(self, riga, chiedi):
        """La risposta per tod-smb. CHIEDI(nome, utente, dominio, errore) mostra la finestra: dict o None."""
        c = riga.rstrip("\n").split("\t")
        if len(c) < 5 or c[0] != "AUTH":
            return "NO\n"
        nome, cartella, motivo = c[1], c[3], c[4]
        rifiutate = motivo == "rifiutate"
        with self.lucchetti.setdefault(nome, threading.Lock()):
            cred = self.memoria.get(nome)
            if cred is None:
                salvate = carica_conf()["credenziali"].get(nome)
                if salvate:
                    pw = password_di(nome, salvate["utente"])
                    if pw is not None:
                        cred = dict(salvate, password=pw)
                        self.memoria[nome] = cred
                    else:
                        log(f"password di {nome}: non nel portachiavi")
            if cred and rifiutate:   # non andavano bene: via, e si richiedono
                self.memoria.pop(nome, None)
                self.rifiutate.add(nome)
                errore, cred = True, None
                log(f"password di {nome} rifiutata ({cartella})")
            else:
                errore = nome in self.rifiutate
            if cred:
                return self.ok(cred)
            if motivo == "primo" and not errore:
                log(f"{nome}/{cartella}: da ospite")
                return "NO\n"   # la prima volta da ospite: le cartelle aperte a tutti non chiedono niente
            if time.monotonic() - self.annullate.get(nome, -PAUSA) < PAUSA:
                return "NO\n"   # appena annullata: anche le richieste rimaste in coda intanto, senza finestra
            ultimo = carica_conf()["credenziali"].get(nome, {})
            log(f"{nome}/{cartella}: chiedo la password")
            nuove = chiedi(nome, ultimo.get("utente", ""), ultimo.get("dominio", ""), errore)
            if not nuove:
                log(f"{nome}: password non data")
                self.annullate[nome] = time.monotonic()
                return "NO\n"
            self.annullate.pop(nome, None)
            self.rifiutate.discard(nome)
            self.memoria[nome] = nuove
            if nuove.pop("ricorda", False):
                q = self.pc.get(nome, {})
                with CONF_M:
                    conf = carica_conf()
                    c = conf["credenziali"][nome] = {"utente": nuove["utente"], "dominio": nuove.get("dominio", "")}
                    if q.get("ip"):
                        c.update(indirizzo=q["ip"], porta=q.get("porta", 445))
                    salva_conf(conf)
                ricorda_password(nome, nuove["utente"], nuove["password"])
            return self.ok(nuove)

    @staticmethod
    def ok(c):
        dom = quote(c.get("dominio") or "-", safe="")
        return f"OK\t{dom}\t{quote(c['utente'], safe='')}\t{quote(c['password'], safe='')}\n"

    def giro_porta(self, chiedi):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(SOCK)
        except OSError:
            pass
        s.bind(SOCK)
        os.chmod(SOCK, 0o600)
        s.listen(16)

        def uno(c):
            try:
                riga = c.makefile("r", encoding="utf-8", errors="replace").readline()
                c.sendall(self.risposta(riga, chiedi).encode())
            except OSError:
                pass
            finally:
                c.close()

        while not self.fine:
            c, _ = s.accept()
            threading.Thread(target=uno, args=(c,), daemon=True).start()

    def chiudi(self):
        self.fine = True
        with self.m:
            for nome in list(self.proc):
                self.ferma_smb(nome)
            self.pc.clear()
        for f in (VOLUMI, SOCK):
            try:
                os.unlink(f)
            except OSError:
                pass


# --- la finestra della password (GTK, nel filo principale) ------------------------------------------------------

def finestra_password(Gtk, nome, utente, dominio, errore):
    d = Gtk.Dialog(title=T("Cartelle condivise", "Shared folders"), modal=True)
    d.set_keep_above(True)
    d.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
    d.set_default_size(420, -1)
    d.set_icon_name("network-server")
    d.add_button(T("Annulla", "Cancel"), Gtk.ResponseType.CANCEL)
    b = d.add_button(T("Connetti", "Connect"), Gtk.ResponseType.OK)
    b.get_style_context().add_class("suggested-action")
    d.set_default_response(Gtk.ResponseType.OK)
    box = d.get_content_area()
    box.set_spacing(10)
    box.set_border_width(18)
    testa = Gtk.Box(spacing=12)
    img = Gtk.Image.new_from_icon_name("network-server", Gtk.IconSize.DIALOG)
    testa.pack_start(img, False, False, 0)
    t = Gtk.Label(xalign=0, wrap=True)
    t.set_markup(T(f"<b>Connetti a «{nome}»</b>\nIl computer chiede nome utente e password per aprire le sue cartelle condivise.",
                   f"<b>Connect to “{nome}”</b>\nThis computer asks for a user name and password to open its shared folders."))
    testa.pack_start(t, True, True, 0)
    box.pack_start(testa, False, False, 0)
    if errore:
        e = Gtk.Label(xalign=0, wrap=True)
        e.set_markup("<span foreground='#c42b1c'>" + T("Nome utente o password non accettati: riprova.",
                                                      "User name or password not accepted: try again.") + "</span>")
        box.pack_start(e, False, False, 0)
    g = Gtk.Grid(column_spacing=10, row_spacing=8)
    voci = {}
    for i, (k, etichetta, valore, nascosto) in enumerate((
            ("utente", T("Nome utente", "User name"), utente, False),
            ("password", T("Password", "Password"), "", True),
            ("dominio", T("Dominio (facoltativo)", "Domain (optional)"), dominio, False))):
        g.attach(Gtk.Label(label=etichetta, xalign=1), 0, i, 1, 1)
        en = Gtk.Entry(text=valore, visibility=not nascosto, activates_default=True, hexpand=True)
        g.attach(en, 1, i, 1, 1)
        voci[k] = en
    box.pack_start(g, False, False, 0)
    nota = Gtk.Label(xalign=0, wrap=True, label=T("Su Windows: il nome dell'account del PC, o l'email dell'account Microsoft.",
                                                  "On Windows: the PC account name, or the Microsoft account email."))
    nota.get_style_context().add_class("dim-label")
    box.pack_start(nota, False, False, 0)
    ricorda = Gtk.CheckButton(label=T("Ricorda la password", "Remember the password"), active=True)
    box.pack_start(ricorda, False, False, 0)

    def pieno(*_):   # "Connetti" solo con un nome utente: un Invio a vuoto non chiude la finestra
        d.set_response_sensitive(Gtk.ResponseType.OK, bool(voci["utente"].get_text().strip()))
    voci["utente"].connect("changed", pieno)
    pieno()
    d.show_all()
    (voci["password"] if utente else voci["utente"]).grab_focus()
    r = d.run()
    out = None
    if r == Gtk.ResponseType.OK and voci["utente"].get_text().strip():
        out = {"utente": voci["utente"].get_text().strip(), "password": voci["password"].get_text(),
               "dominio": voci["dominio"].get_text().strip(), "ricorda": ricorda.get_active()}
    d.destroy()
    return out


def sorveglia():
    os.makedirs(RUN, exist_ok=True)
    os.chmod(RUN, 0o700)
    blocco = open(os.path.join(RUN, "sorveglia.lock"), "w")
    try:
        fcntl.flock(blocco, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    with open(os.path.join(RUN, "sorveglia.pid"), "w") as f:
        f.write(str(os.getpid()))
    srv = Servizio()
    Gtk = GLib = None
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import GLib, Gtk  # noqa: F811
        GLib.set_prgname("tod-rete")
        if not Gtk.init_check(sys.argv)[0]:
            Gtk = None
    except (ImportError, ValueError):
        Gtk = None

    def chiedi(nome, utente, dominio, errore):
        if Gtk is None or os.environ.get("TOD_RETE_SENZA_FINESTRA"):   # (le prove: mai una finestra sullo schermo)
            return None
        esito, fatto = {}, threading.Event()

        def mostra():
            esito["r"] = finestra_password(Gtk, nome, utente, dominio, errore)
            fatto.set()
            return False
        GLib.idle_add(mostra)
        fatto.wait()
        return esito.get("r")

    def fine(*_):
        srv.chiudi()
        log("servizio della rete spento")
        os._exit(0)

    signal.signal(signal.SIGTERM, fine)
    log("servizio della rete acceso")
    srv.aggiorna([])   # subito i PC aggiunti a mano
    threading.Thread(target=srv.giro_ricerca, daemon=True).start()
    threading.Thread(target=srv.giro_porta, args=(chiedi,), daemon=True).start()
    x = os.path.join(TMP, ".X11-unix/X0")

    def desktop_acceso():
        if not os.path.exists(x) and not os.environ.get("TOD_RETE_SENZA_X"):
            log("desktop chiuso")
            fine()
        return True

    if Gtk is not None:
        GLib.timeout_add_seconds(5, desktop_acceso)
        Gtk.main()
    else:
        while True:
            desktop_acceso()
            time.sleep(5)
    return 0


def ferma():
    try:
        pid = int(open(os.path.join(RUN, "sorveglia.pid")).read())
        if b"tod-rete" in open(f"/proc/{pid}/cmdline", "rb").read():
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass
    return 0


def elenco():
    pc = []
    try:
        for riga in open(VOLUMI):
            n, _, ip = riga.rstrip("\n").partition("\t")
            pc.append({"nome": n, "ip": ip})
    except OSError:
        pass
    conf = carica_conf()
    manuali = {nome_cartella(m["nome"]) for m in conf["pc"]}
    for p in pc:
        p["manuale"] = p["nome"] in manuali
        p["password"] = p["nome"] in conf["credenziali"]
    acceso = os.path.exists(SOCK)
    return {"servizio": acceso, "pc": pc}


def principale(argv):
    cmd = argv[1] if len(argv) > 1 else "stato"
    js = "--json" in argv
    args = [a for a in argv[2:] if a != "--json"]
    if cmd == "sorveglia":
        return sorveglia()
    if cmd == "ferma":
        return ferma()
    if cmd == "stato":
        e = elenco()
        print(T("rete: ", "network: ") + (T("acceso", "on") if e["servizio"] else T("spento", "off")))
        for p in e["pc"]:
            print(f"  {p['nome']} ({p['ip']}){' *' if p['password'] else ''}")
        return 0
    if cmd == "cerca":
        trovati = tutti_i_nomi(cerca_pc(ricordati=pc_ricordati(carica_conf())))
        if js:
            print(json.dumps(trovati, ensure_ascii=False))
        else:
            for p in trovati:
                print(f"{p['nome']}: {p['ip']} ({p['da']})")
            if not trovati:
                print(T("nessun PC trovato in rete", "no computers found on the network"))
        return 0
    if cmd == "elenco":
        print(json.dumps(elenco(), ensure_ascii=False))
        return 0
    conf = carica_conf()
    if cmd == "aggiungi" and args:
        indirizzo = args[0].strip().removeprefix("smb://").strip("/\\")
        porta = 445
        if ":" in indirizzo:
            indirizzo, _, p = indirizzo.partition(":")
            porta = int(p) if p.isdigit() else 445
        nome = nome_cartella(args[1] if len(args) > 1 else indirizzo)
        conf["pc"] = [m for m in conf["pc"] if nome_cartella(m["nome"]) != nome] + \
            [{"nome": nome, "indirizzo": indirizzo, "porta": porta}]
        salva_conf(conf)
        return 0
    if cmd == "togli" and args:
        conf["pc"] = [m for m in conf["pc"] if nome_cartella(m["nome"]) != args[0]]
        salva_conf(conf)
        return 0
    if cmd == "dimentica" and args:
        c = conf["credenziali"].pop(args[0], None)
        salva_conf(conf)
        if c:
            dimentica_password(args[0], c["utente"])
        return 0
    print("uso: tod-rete.py sorveglia|ferma|stato|cerca|elenco|aggiungi INDIRIZZO [NOME]|togli NOME|dimentica NOME",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(principale(sys.argv))
