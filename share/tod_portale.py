"""tod_portale.py — il portale dei file del desktop (11 novembre 2026).

Pietro: nelle app Electron (Boxy SVG…) "Apri" e "Aggiungi file" mostravano i percorsi del container, e il file scelto non veniva
allegato. Le app Electron e Chromium chiedono i file al portale (org.freedesktop.portal.FileChooser) e, se non c'è,
aprono la finestra di GTK dentro il loro processo: quella di Debian, con le cartelle del container. Qui il portale
lo offre il servizio del desktop (share/tod-avvisi-host.py): la finestra è la nostra (share/tod_apri_salva.py) e i
percorsi che tornano all'app sono quelli veri, validi con e senza proot e anche per le parti delle app che non
passano da fakechroot.

Solo la scelta dei file: le altre interfacce del portale non ci sono e le app ripiegano da sole.
  avvia(app_di)   da chiamare nel filo di GTK del servizio; app_di(argomenti) → (nome, icona) dell'app che chiede
"""
import mimetypes
import os
import secrets
import sys

from gi.repository import Gio, GLib

NOME = "org.freedesktop.portal.Desktop"
OGGETTO = "/org/freedesktop/portal/desktop"
VERSIONE = 4

_ARGOMENTI = ('<arg type="s" name="parent_window" direction="in"/><arg type="s" name="title" direction="in"/>'
              '<arg type="a{sv}" name="options" direction="in"/><arg type="o" name="handle" direction="out"/>')
XML = f"""<node>
 <interface name="org.freedesktop.portal.FileChooser">
  <method name="OpenFile">{_ARGOMENTI}</method>
  <method name="SaveFile">{_ARGOMENTI}</method>
  <method name="SaveFiles">{_ARGOMENTI}</method>
  <property name="version" type="u" access="read"/>
 </interface>
 <interface name="org.freedesktop.portal.Request">
  <method name="Close"/>
  <signal name="Response"><arg type="u" name="response"/><arg type="a{{sv}}" name="results"/></signal>
 </interface>
</node>"""


def testo(v):
    """Un percorso del portale ("ay" con lo zero finale) → stringa"""
    if isinstance(v, str):
        return v
    try:
        return bytes(v).split(b"\0", 1)[0].decode("utf-8", "replace") or None
    except (TypeError, ValueError):
        return None


def motivi_del_tipo(tipo):
    """Un tipo MIME (anche image/*) → i motivi dei nomi (*.png ...)"""
    tipo = tipo.lower()
    if tipo.endswith("/*"):
        inizio = tipo[:-1]
        est = sorted(e for e, t in mimetypes.types_map.items() if t.startswith(inizio))
    else:
        est = mimetypes.guess_all_extensions(tipo)
    return ["*" + e for e in est]


def filtri(lista):
    """a(sa(us)) del portale → [[nome, [motivi]]] della nostra finestra (0 = motivo, 1 = tipo MIME)"""
    out = []
    for voce in lista or []:
        try:
            nome, regole = voce
        except (TypeError, ValueError):
            continue
        motivi, tutto = [], False
        for genere, valore in regole:
            if genere == 0:
                motivi.append(valore)
            else:
                trovati = motivi_del_tipo(valore)
                if not trovati:
                    tutto = True   # tipo che non sappiamo tradurre: meglio mostrare tutto che nascondere il file
                motivi += trovati
        if tutto or "*" in motivi:
            motivi = []
        out.append([nome, motivi])
    return out


def richiesta(metodo, titolo, opz):
    """Gli argomenti del portale → la richiesta per tod_apri_salva (percorsi come li manda l'app)"""
    r = {"titolo": titolo or None, "filtri": filtri(opz.get("filters")), "conferma": True,
         "cartella": testo(opz.get("current_folder"))}
    if metodo == "OpenFile":
        r["modo"] = "cartella" if opz.get("directory") else "apri"
        r["multipli"] = bool(opz.get("multiple"))
    elif metodo == "SaveFile":
        r["modo"] = "salva"
        r["nome"] = opz.get("current_name") or None
        f = testo(opz.get("current_file"))
        if f:
            r["cartella"] = r["cartella"] or os.path.dirname(f)
            r["nome"] = r["nome"] or os.path.basename(f)
    else:   # SaveFiles: una cartella dove mettere i file già nominati dall'app
        r["modo"] = "cartella"
    return r


def percorso_richiesta(mittente, opz):
    """Come lo calcolano le app (si mettono in ascolto prima della risposta)"""
    gettone = str(opz.get("handle_token") or secrets.token_hex(8))
    return f"{OGGETTO}/request/{mittente.lstrip(':').replace('.', '_')}/{gettone}"


class Portale:
    def __init__(self, conn, app_di):
        self.conn = conn
        self.app_di = app_di
        self.aperte = {}   # percorso della richiesta → id dell'oggetto registrato
        nodo = Gio.DBusNodeInfo.new_for_xml(XML)
        self.scelta = nodo.lookup_interface("org.freedesktop.portal.FileChooser")
        self.domanda = nodo.lookup_interface("org.freedesktop.portal.Request")
        conn.register_object(OGGETTO, self.scelta, self.metodo, self.proprieta, None)

    def proprieta(self, _conn, _mittente, _oggetto, _interfaccia, nome):
        return GLib.Variant("u", VERSIONE) if nome == "version" else None

    def chi(self, mittente):
        try:
            pid = self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                      "GetConnectionUnixProcessID", GLib.Variant("(s)", (mittente,)), None,
                                      Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return [x.decode(errors="replace") for x in f.read().split(b"\0") if x][:6]
        except (GLib.Error, OSError):
            return []

    def metodo(self, conn, mittente, _oggetto, _interfaccia, metodo, parametri, invocazione):
        _genitore, titolo, opz = parametri.unpack()
        h = percorso_richiesta(mittente, opz)
        if h not in self.aperte:
            self.aperte[h] = conn.register_object(h, self.domanda, self.chiudi, None, None)
        invocazione.return_value(GLib.Variant("(o)", (h,)))
        r = richiesta(metodo, titolo, opz)
        r["icona"] = self.app_di(self.chi(mittente))[1]
        nomi = [testo(x) for x in opz.get("files") or []] if metodo == "SaveFiles" else None
        GLib.idle_add(self.apri, mittente, h, r, nomi)

    def chiudi(self, _conn, _mittente, oggetto, _interfaccia, _metodo, _parametri, invocazione):
        self.togli(oggetto)   # l'app non aspetta più: la risposta non partirà
        invocazione.return_value(None)

    def togli(self, h):
        rid = self.aperte.pop(h, None)
        if rid:
            self.conn.unregister_object(rid)
        return rid

    def apri(self, mittente, h, r, nomi):
        import tod_apri_salva
        try:
            scelti = tod_apri_salva.scegli_veri(r)
        except Exception as e:   # mai un'app appesa ad aspettare: risponde "annullato"
            print(f"tod-portale: {type(e).__name__}: {e}", file=sys.stderr)
            scelti = []
        if nomi and scelti:
            scelti = [os.path.join(scelti[0], n) for n in nomi if n]
        if self.togli(h):
            uri = [Gio.File.new_for_path(p).get_uri() for p in scelti]
            esito = {"uris": GLib.Variant("as", uri)} if uri else {}
            self.conn.emit_signal(mittente, h, "org.freedesktop.portal.Request", "Response",
                                  GLib.Variant("(ua{sv})", (0 if uri else 1, esito)))
        return False


def avvia(app_di):
    """Mette il portale sul D-Bus della sessione. Se un portale vero c'è già, il nome resta a lui."""
    try:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        portale = Portale(conn, app_di)
    except GLib.Error as e:
        print(f"tod-portale: niente D-Bus: {e.message}", file=sys.stderr)
        return None
    Gio.bus_own_name_on_connection(conn, NOME, Gio.BusNameOwnerFlags.NONE, None, None)
    return portale
