"""La finestra Apri / Salva come quella di macOS (11 novembre 2026, Pietro: "sì, rifacciamo" la finestra Apri/Salva).

La usano le app di Debian: chiedono i file con zenity --file-selection (rfd delle app Craft), lo zenity del container
(share/tod-zenity) lo passa al servizio del desktop (share/tod-avvisi-host.py), che apre questa finestra. Aspetto
DeX OS approvato: fondi pieni (barra laterale #f2f2f5 / #242428, contenuto #ffffff / #2e2e33), Inter, solo l'accento
del sistema, pulsanti a pillola, angoli tondi senza la barra del titolo di Linux.

  scegli(richiesta) → (codice, stampa)   come zenity: 0 e i percorsi, 1 se annullato
richiesta: {"modo": "apri|salva|cartella", "multipli": bool, "nome": "...", "cartella": "...", "filtri": [[nome, [motivi]]],
            "conferma": bool (sovrascrivere), "titolo": "...", "separatore": "|"}
I percorsi in ingresso e in uscita sono quelli del container (radice = TOD_CX_ROOTFS); qui si lavora con quelli veri.
"""
import fnmatch
import hashlib
import json
import os
import threading
import time
import urllib.parse

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

ITALIANO = os.environ.get("LANGUAGE", os.environ.get("LANG", "it")).startswith("it")
PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
ROOTFS = os.environ.get("TOD_CX_ROOTFS") or os.path.join(PREFIX, "var/lib/proot-distro/containers/debian/rootfs")
TMP = os.environ.get("TMPDIR", os.path.join(PREFIX, "tmp"))
TELEFONO = os.environ.get("TOD_TELEFONO_DIR", "/storage/emulated/0")
# percorsi che l'app del container vede uguali (senza proot sono fuori da fakechroot, in proot sono collegati)
COMUNI = ("/data/", "/storage/", "/sdcard/", "/media/")


def T(it, en):
    return it if ITALIANO else en


def casa():
    return os.environ.get("HOME") or os.path.expanduser("~")


def conf_file():
    return os.path.join(casa(), ".config/tod/apri-salva.json")   # (la home della sessione: .config è quella di Termux)


# --- percorsi del container ↔ veri ------------------------------------------------------------------------------
def dal_container(p, radice=None):
    """Un percorso come lo vede l'app del container → quello vero (come share/tod-open-host)."""
    radice = radice or ROOTFS
    if not p:
        return None
    if p.startswith("/tmp/"):
        return os.path.join(TMP, p[5:])
    if p == "/tmp":
        return TMP
    if p.startswith(COMUNI) or p in ("/data", "/storage", "/sdcard", "/media"):
        return p
    if p.startswith("/"):
        return radice.rstrip("/") + p
    return None


def al_container(p, radice=None):
    """Un percorso vero → come lo vede l'app: dentro il container senza la radice, il resto con i collegamenti
    risolti (Documenti è /storage/emulated/0/Documents; la home del desktop in proot non si vede)."""
    radice = os.path.realpath(radice or ROOTFS)
    tmp = os.path.realpath(TMP)
    cartella, nome = os.path.split(p)
    vero = os.path.join(os.path.realpath(cartella), nome)   # il file può ancora non esserci (Salva)
    if vero == radice or vero.startswith(radice + "/"):
        return vero[len(radice):] or "/"
    if vero.startswith(tmp + "/"):
        return "/tmp/" + vero[len(tmp) + 1:]
    return vero


def rete_entra(percorso):
    """L'utente apre un PC della Rete (/media/rete/<PC>): lo si dice al servizio (share/rete/tod-rete.py, "ENTRA"),
    che solo così chiede la password di quel PC. Chi guarda dentro i PC da solo non fa comparire finestre."""
    if not percorso.startswith("/media/rete/"):
        return
    nome = percorso[len("/media/rete/"):].split("/", 1)[0]
    presa = os.path.join(os.environ.get("TOD_FS_RETE") or os.path.join(TMP, "tod-rete"), "servizio.sock")
    if not nome or not os.path.exists(presa):
        return
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(1)
        s.connect(presa)
        s.sendall(f"ENTRA\t{nome}\n".encode())
        s.recv(8)
    except OSError:
        pass
    finally:
        s.close()


def filtri_zenity(testi):
    """--file-filter "Immagini | *.png *.jpg" → [["Immagini", ["*.png", "*.jpg"]]]"""
    out = []
    for t in testi or []:
        nome, sep, motivi = t.partition("|")
        if not sep:
            motivi, nome = nome, nome
        lista = motivi.split()
        if lista:
            out.append([nome.strip() or " ".join(lista), lista])
    return out


def corrisponde(nome, motivi):
    n = nome.lower()
    return not motivi or any(fnmatch.fnmatch(n, m.lower()) for m in motivi)


def data_breve(t, ora=None):
    """oggi, 14:32 · ieri, 09:10 · 3 nov 2026"""
    ora = ora or time.time()
    lt, oggi = time.localtime(t), time.localtime(ora)
    hm = time.strftime("%H:%M", lt)
    if lt[:3] == oggi[:3]:
        return T(f"oggi, {hm}", f"today, {hm}")
    ieri = time.localtime(ora - 86400)
    if lt[:3] == ieri[:3]:
        return T(f"ieri, {hm}", f"yesterday, {hm}")
    mesi = T("gen feb mar apr mag giu lug ago set ott nov dic", "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec").split()
    return f"{lt.tm_mday} {mesi[lt.tm_mon - 1]} {lt.tm_year}"


def dimensione(n):
    """6 byte · 12 KB · 1,2 MB (come il Finder: multipli di 1000, la virgola in italiano). Da Python GLib.format_size
    non passa dal traduttore del desktop (tod-gettext.c): resterebbe "6 bytes"."""
    if n < 1000:
        return f"{n} byte" if ITALIANO else (f"{n} byte" if n == 1 else f"{n} bytes")
    for unita in ("KB", "MB", "GB", "TB"):
        n /= 1000
        if n < 1000 or unita == "TB":
            testo = f"{n:.0f}" if n >= 100 or unita == "KB" else f"{n:.1f}"
            return (testo.replace(".", ",") if ITALIANO else testo) + " " + unita
    return ""


def nome_senza_estensione(nome):
    base, punto, est = nome.rpartition(".")
    return len(base) if punto and base and len(est) <= 5 else len(nome)


# --- dove: barra laterale ---------------------------------------------------------------------------------------
def cartelle_utente():
    h = casa()
    out = {}
    try:
        with open(os.path.join(h, ".config", "user-dirs.dirs"), encoding="utf-8") as f:
            for r in f:
                r = r.strip()
                if r.startswith("XDG_") and "_DIR=" in r:
                    k, _, v = r.partition("=")
                    out[k[4:-4]] = v.strip('"').replace("$HOME", h)
    except OSError:
        pass
    return out


def posti():
    """[(sezione, nome, icona, percorso)] per la barra laterale; solo quelli che ci sono."""
    u = cartelle_utente()
    utente = os.environ.get("TOD_USER") or os.path.basename(casa())
    voci = [("pref", T("Recenti", "Recents"), "document-open-recent-symbolic", ":recenti"),
            ("pref", utente[:1].upper() + utente[1:], "user-home-symbolic", casa())]
    for k, it, en, icona in (("DESKTOP", "Desktop", "Desktop", "user-desktop-symbolic"),
                             ("DOCUMENTS", "Documenti", "Documents", "folder-documents-symbolic"),
                             ("DOWNLOAD", "Download", "Downloads", "folder-download-symbolic"),
                             ("PICTURES", "Immagini", "Pictures", "folder-pictures-symbolic"),
                             ("MUSIC", "Musica", "Music", "folder-music-symbolic"),
                             ("VIDEOS", "Video", "Videos", "folder-videos-symbolic")):
        p = u.get(k)
        if p and os.path.isdir(p) and os.path.realpath(p) != os.path.realpath(casa()):
            voci.append(("pref", os.path.basename(p.rstrip("/")) or T(it, en), icona, p))
    if os.path.isdir(TELEFONO):
        voci.append(("posti", T("Telefono", "Phone"), "phone-symbolic", TELEFONO))
    usb = "/media/usb"
    try:
        for n in sorted(os.listdir(usb)):
            if os.path.isdir(os.path.join(usb, n)):
                voci.append(("posti", n, "drive-removable-media-symbolic", os.path.join(usb, n)))
    except OSError:
        pass
    if os.path.isdir("/media/rete"):
        voci.append(("posti", T("Rete", "Network"), "network-workgroup-symbolic", "/media/rete"))
    return voci


def recenti(limite=60):
    try:
        voci = Gtk.RecentManager.get_default().get_items()
    except Exception:
        return []
    out = []
    for v in voci:
        uri = v.get_uri()
        if not uri.startswith("file://"):
            continue
        p = urllib.parse.unquote(uri[7:])
        if os.path.exists(p):
            m = v.get_modified()   # GTK 3: un numero; GTK 4: GLib.DateTime
            out.append((m.to_unix() if hasattr(m, "to_unix") else int(m or 0), p))
    out.sort(reverse=True)
    return [p for _, p in out[:limite]]


# --- aspetto --------------------------------------------------------------------------------------------------
COLORI = {
    False: dict(lato="#f2f2f5", fondo="#ffffff", testo="#1d1d1f", tenue="#6e6e73", linea="rgba(0, 0, 0, 0.08)",
                sopra="rgba(0, 0, 0, 0.05)", scelto_lato="rgba(0, 0, 0, 0.08)", bordo="rgba(0, 0, 0, 0.12)",
                ombra="rgba(0, 0, 0, 0.32)", campo="#ffffff", bottone="#ffffff", bottone_su="#f7f7f9",
                pari="#f7f7f9"),
    True: dict(lato="#242428", fondo="#2e2e33", testo="#f5f5f7", tenue="#a1a1a6", linea="rgba(255, 255, 255, 0.08)",
               sopra="rgba(255, 255, 255, 0.06)", scelto_lato="rgba(255, 255, 255, 0.10)",
               bordo="rgba(255, 255, 255, 0.12)", ombra="rgba(0, 0, 0, 0.60)", campo="#1c1c1f", bottone="#3a3a40",
               bottone_su="#44444a", pari="#323237"),
}
CSS = """
window.tod-file, window.tod-file.background { background-color: {fondo}; color: {testo}; border-radius: 12px;
  font-family: Inter, sans-serif; }
window.tod-file decoration { border-radius: 12px; box-shadow: 0 18px 50px {ombra}, 0 0 0 1px {bordo}; }
window.tod-file > .tod-file-testa { min-height: 0; padding: 0; margin: 0; border: none; background: none; box-shadow: none; }
.tod-file .tod-file-lato { background-color: {lato}; border-right: 1px solid {linea};
  border-top-left-radius: 12px; border-bottom-left-radius: 12px; }
.tod-file .tod-file-lato list, .tod-file .tod-file-lato row { background: none; }
.tod-file .tod-file-sezione { font-size: 8pt; font-weight: 700; color: {tenue}; margin: 14px 0 3px 14px; }
.tod-file .tod-file-lato row { margin: 0 8px; padding: 4px 8px; border-radius: 7px; min-height: 22px; }
.tod-file .tod-file-lato row:hover { background-color: {sopra}; }
.tod-file .tod-file-lato row:selected, .tod-file .tod-file-lato row:selected:focus { background-color: {scelto_lato}; color: {testo}; }
.tod-file .tod-file-lato row image { color: {accento}; }
.tod-file .tod-file-lato row label { color: {testo}; font-size: 9.5pt; }
.tod-file .tod-file-barra { padding: 8px 12px; border-bottom: 1px solid {linea}; }
.tod-file .tod-file-barra button, .tod-file .tod-file-fondo button.tod-file-semplice {
  min-height: 24px; min-width: 24px; padding: 2px 8px; border-radius: 7px; border: none; background: none;
  box-shadow: none; color: {testo}; }
.tod-file .tod-file-barra button:hover, .tod-file .tod-file-fondo button.tod-file-semplice:hover { background-color: {sopra}; }
.tod-file .tod-file-barra button:disabled image { opacity: 0.35; }
.tod-file .tod-file-barra button.tod-file-cartella label { font-weight: 700; font-size: 10.5pt; }
.tod-file .tod-file-barra button:checked { background-color: {scelto_lato}; }
.tod-file .tod-file-vista { border-radius: 7px; background-color: {sopra}; padding: 1px; }
.tod-file .tod-file-vista button { border-radius: 6px; min-height: 22px; padding: 1px 8px; }
.tod-file entry { min-height: 26px; border-radius: 8px; background-image: none; background-color: {campo};
  color: {testo}; border: 1px solid {bordo}; box-shadow: none; padding: 1px 9px; }
.tod-file entry:focus { border-color: {accento}; box-shadow: 0 0 0 3px alpha({accento}, 0.30); }
.tod-file .tod-file-salva { padding: 14px 16px 10px 16px; border-bottom: 1px solid {linea}; }
.tod-file .tod-file-salva label { font-weight: 600; }
.tod-file treeview.view { background-color: {fondo}; color: {testo}; font-size: 9.5pt; }
.tod-file treeview.view:hover { background-color: {sopra}; }
.tod-file treeview.view:selected, .tod-file treeview.view:selected:focus { background-color: {accento}; color: #ffffff; }
.tod-file treeview.view:disabled { color: {tenue}; }
.tod-file treeview.view header button { background: none; border: none; box-shadow: none; color: {tenue};
  font-size: 8.5pt; font-weight: 600; padding: 2px 8px; border-bottom: 1px solid {linea}; }
.tod-file iconview.view { background-color: {fondo}; color: {testo}; font-size: 9pt; }
.tod-file iconview.view:disabled { color: {tenue}; }
.tod-file iconview.view:selected, .tod-file iconview.view:selected:focus { background-color: alpha({accento}, 0.20);
  color: {testo}; border-radius: 8px; }
.tod-file .tod-file-vuota { color: {tenue}; }
.tod-file .tod-file-fondo { padding: 10px 14px; border-top: 1px solid {linea}; }
.tod-file .tod-file-fondo label { color: {tenue}; font-size: 9pt; }
.tod-file .tod-file-fondo combobox button { border-radius: 7px; min-height: 24px; padding: 1px 8px; }
.tod-file .tod-file-fondo button.tod-file-pillola { min-height: 24px; min-width: 76px; padding: 2px 16px;
  border-radius: 999px; border: none; background-image: none; background-color: {bottone}; color: {testo};
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.16), inset 0 0 0 1px {bordo}; font-weight: 500; }
.tod-file .tod-file-fondo button.tod-file-pillola label { color: {testo}; }
.tod-file .tod-file-fondo button.tod-file-pillola:hover { background-color: {bottone_su}; }
.tod-file .tod-file-fondo button.tod-file-pillola.tod-file-principale { background-color: {accento};
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.22); }
.tod-file .tod-file-fondo button.tod-file-pillola.tod-file-principale label { color: #ffffff; }
.tod-file .tod-file-fondo button.tod-file-pillola.tod-file-principale:hover { background-color: shade({accento}, 1.08); }
.tod-file .tod-file-fondo button.tod-file-pillola:disabled { opacity: 0.45; }
.tod-file scrollbar { background: none; border: none; }
"""


def accento():
    """Il colore d'accento del sistema (ACCENTO in tod.conf), sennò quello del tema"""
    try:
        with open(os.path.join(casa(), ".config/tod/tod.conf")) as f:
            for r in f:
                if r.startswith("ACCENTO="):
                    v = r.split("=", 1)[1].strip().strip('"')
                    if len(v) == 7 and v.startswith("#"):
                        return v
    except OSError:
        pass
    return "@theme_selected_bg_color"


def scuro():
    nome = Gtk.Settings.get_default().get_property("gtk-theme-name") or ""
    return "dark" in nome.lower()


def carica_css():
    t = CSS
    for k, v in dict(COLORI[scuro()], accento=accento()).items():
        t = t.replace("{" + k + "}", v)
    p = Gtk.CssProvider()
    p.load_from_data(t.encode())
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), p, 850)


# --- icone e miniature ------------------------------------------------------------------------------------------
_ICONE = {}


def icona_di(percorso, dimensione, cartella=None):
    tema = Gtk.IconTheme.get_default()
    nomi = []
    if cartella:
        speciali = {os.path.realpath(v): k for k, v in cartelle_utente().items()}
        k = speciali.get(os.path.realpath(percorso))
        nomi = {"DESKTOP": ["user-desktop"], "DOCUMENTS": ["folder-documents"], "DOWNLOAD": ["folder-download"],
                "PICTURES": ["folder-pictures"], "MUSIC": ["folder-music"], "VIDEOS": ["folder-videos"]}.get(k, [])
        nomi.append("folder")
    else:
        tipo, _ = Gio.content_type_guess(percorso, None)
        gicon = Gio.content_type_get_icon(tipo)
        nomi = list(gicon.get_names()) if hasattr(gicon, "get_names") else []
        nomi += ["text-x-generic", "unknown"]
    chiave = (tuple(nomi), dimensione)
    if chiave not in _ICONE:
        info = tema.choose_icon(nomi, dimensione, Gtk.IconLookupFlags.FORCE_SIZE)
        try:
            _ICONE[chiave] = info.load_icon() if info else None
        except GLib.Error:
            _ICONE[chiave] = None
    return _ICONE[chiave]


def miniatura(percorso, dimensione):
    """la miniatura di un'immagine: dalla cache di freedesktop (condivisa con Esplora file), sennò dal file"""
    uri = "file://" + urllib.parse.quote(percorso)
    h = hashlib.md5(uri.encode()).hexdigest()
    cache = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(casa(), ".cache"), "thumbnails")
    for d in ("large", "normal"):
        p = os.path.join(cache, d, h + ".png")
        if os.path.exists(p):
            try:
                return GdkPixbuf.Pixbuf.new_from_file_at_scale(p, dimensione, dimensione, True)
            except GLib.Error:
                pass
    try:
        if os.path.getsize(percorso) < 25 * 1024 * 1024:
            return GdkPixbuf.Pixbuf.new_from_file_at_scale(percorso, dimensione, dimensione, True)
    except (OSError, GLib.Error):
        pass
    return None


IMMAGINI = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".svg")


# --- la finestra ----------------------------------------------------------------------------------------------
class Pannello(Gtk.Window):
    # colonne del modello
    PICCOLA, GRANDE, NOME, PERCORSO, CARTELLA, DATA, DIM, TIPO, ATTIVA, ORDINE = range(10)

    def __init__(self, r, fine):
        super().__init__(type_hint=Gdk.WindowTypeHint.DIALOG, title=r.get("titolo") or "")
        self.r, self.fine = r, fine
        self.modo = r.get("modo", "apri")
        self.multipli = bool(r.get("multipli")) and self.modo != "salva"
        self.filtri = r.get("filtri") or []
        self.filtro = self.filtri[0][1] if self.filtri else []
        self.nascosti = False
        self.indietro, self.avanti = [], []
        self.qui = None
        self.lavoro = 0
        self.sc = max(1.0, (Gdk.Screen.get_default().get_resolution() or 96) / 96)
        S = lambda n: int(n * self.sc + 0.5)   # noqa: E731
        self.S = S
        self.i_posti = posti()
        testa = Gtk.Box()
        testa.get_style_context().add_class("tod-file-testa")
        self.set_titlebar(testa)   # cornice di GTK: angoli tondi e ombra, niente barra di Linux
        self.get_style_context().add_class("tod-file")
        self.set_default_size(S(760), S(470))
        self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        self.set_keep_above(True)
        # l'icona dell'app che chiede: la barra la mostra, e gli avvisi di questa finestra ("Sostituire?") la mettono in alto
        self.set_icon_name(r.get("icona") or "system-file-manager")
        self.connect("delete-event", lambda *_: self.chiudi(None) or True)
        self.connect("key-press-event", self.tasto)
        tutto = Gtk.Box()
        self.add(tutto)
        tutto.pack_start(self.lato(), False, False, 0)
        destra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tutto.pack_start(destra, True, True, 0)
        if self.modo == "salva":
            destra.pack_start(self.riga_salva(), False, False, 0)
        destra.pack_start(self.barra(), False, False, 0)
        destra.pack_start(self.contenuto(), True, True, 0)
        destra.pack_start(self.fondo(), False, False, 0)
        self.vai(self.partenza(), storia=False)
        self.show_all()
        self.mostra_vista(self.conf().get("vista", "elenco"))
        if self.modo == "salva":
            self.nome.grab_focus()
            self.nome.select_region(0, nome_senza_estensione(self.nome.get_text()))

    # memoria: ultima cartella e vista
    def conf(self):
        try:
            return json.load(open(conf_file()))
        except (OSError, ValueError):
            return {}

    def salva_conf(self, **k):
        c = self.conf()
        c.update(k)
        try:
            os.makedirs(os.path.dirname(conf_file()), exist_ok=True)
            with open(conf_file(), "w") as f:
                json.dump(c, f)
        except OSError:
            pass

    def partenza(self):
        d = self.r.get("cartella")
        vero = dal_container(d) if d else None
        # la home dell'utente del container non dice niente all'utente: meglio l'ultima cartella usata
        case_cx = {os.path.realpath(os.path.join(ROOTFS, "home", x)) for x in ("dex", os.environ.get("USER", "dex"))}
        if vero and os.path.isdir(vero) and os.path.realpath(vero) not in case_cx and os.path.realpath(vero) != "/":
            return vero
        ultima = self.conf().get("ultima")
        if ultima and os.path.isdir(ultima):
            return ultima
        return cartelle_utente().get("DOCUMENTS") or casa()

    # barra laterale
    def lato(self):
        S = self.S
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.get_style_context().add_class("tod-file-lato")
        box.set_size_request(S(178), -1)
        presa = Gtk.EventBox()   # il lato in alto sposta la finestra (niente barra del titolo)
        presa.set_size_request(-1, S(12))
        presa.connect("button-press-event", self.trascina)
        box.pack_start(presa, False, False, 0)
        self.lista_posti = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.lista_posti.set_header_func(self.intestazione, None)
        self.lista_posti.connect("row-activated", lambda _l, riga: self.vai(riga.percorso))
        for sezione, nome, icona, percorso in self.i_posti:
            riga = Gtk.ListBoxRow()
            riga.percorso, riga.sezione = percorso, sezione
            h = Gtk.Box(spacing=S(8))
            img = Gtk.Image.new_from_icon_name(icona, Gtk.IconSize.MENU)
            h.pack_start(img, False, False, 0)
            lab = Gtk.Label(label=nome, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            h.pack_start(lab, True, True, 0)
            riga.add(h)
            self.lista_posti.add(riga)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.add(self.lista_posti)
        box.pack_start(sc, True, True, 0)
        return box

    def intestazione(self, riga, prima, _dati):
        if prima is None or prima.sezione != riga.sezione:
            t = Gtk.Label(label=T("Preferiti", "Favorites") if riga.sezione == "pref" else T("Posizioni", "Locations"),
                          xalign=0)
            t.get_style_context().add_class("tod-file-sezione")
            riga.set_header(t)
        else:
            riga.set_header(None)

    def trascina(self, _w, ev):
        if ev.button == 1 and ev.type == Gdk.EventType.BUTTON_PRESS:
            self.begin_move_drag(1, int(ev.x_root), int(ev.y_root), ev.time)
        return False

    # Salva come
    def riga_salva(self):
        S = self.S
        box = Gtk.Box(spacing=S(10))
        box.get_style_context().add_class("tod-file-salva")
        presa = Gtk.EventBox()
        presa.connect("button-press-event", self.trascina)
        presa.add(Gtk.Label(label=T("Salva come:", "Save As:"), xalign=1))
        box.pack_start(presa, True, True, 0)
        self.nome = Gtk.Entry(text=self.r.get("nome") or T("Senza titolo", "Untitled"), activates_default=True)
        self.nome.set_size_request(S(300), -1)
        self.nome.connect("changed", lambda *_: self.aggiorna_pulsante())
        box.pack_start(self.nome, False, False, 0)
        box.pack_start(Gtk.Box(), True, True, 0)
        return box

    # barra in alto
    def barra(self):
        S = self.S
        box = Gtk.Box(spacing=S(4))
        box.get_style_context().add_class("tod-file-barra")
        self.b_indietro = self.bottone("go-previous-symbolic", lambda *_: self.storia(-1), T("Indietro", "Back"))
        self.b_avanti = self.bottone("go-next-symbolic", lambda *_: self.storia(1), T("Avanti", "Forward"))
        box.pack_start(self.b_indietro, False, False, 0)
        box.pack_start(self.b_avanti, False, False, 0)
        self.b_cartella = Gtk.MenuButton()
        self.b_cartella.get_style_context().add_class("tod-file-cartella")
        h = Gtk.Box(spacing=S(6))
        self.icona_cartella = Gtk.Image()
        self.nome_cartella = Gtk.Label(ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=26)
        h.pack_start(self.icona_cartella, False, False, 0)
        h.pack_start(self.nome_cartella, False, False, 0)
        h.pack_start(Gtk.Image.new_from_icon_name("pan-down-symbolic", Gtk.IconSize.MENU), False, False, 0)
        self.b_cartella.add(h)
        box.pack_start(self.b_cartella, False, False, S(6))
        presa = Gtk.EventBox()   # lo spazio vuoto della barra sposta la finestra
        presa.connect("button-press-event", self.trascina)
        box.pack_start(presa, True, True, 0)
        vista = Gtk.Box()
        vista.get_style_context().add_class("tod-file-vista")
        self.b_elenco = Gtk.ToggleButton()
        self.b_elenco.add(Gtk.Image.new_from_icon_name("view-list-symbolic", Gtk.IconSize.MENU))
        self.b_elenco.set_tooltip_text(T("Elenco", "List"))
        self.b_icone = Gtk.ToggleButton()
        self.b_icone.add(Gtk.Image.new_from_icon_name("view-grid-symbolic", Gtk.IconSize.MENU))
        self.b_icone.set_tooltip_text(T("Icone", "Icons"))
        self.b_elenco.connect("clicked", lambda b: b.get_active() and self.mostra_vista("elenco"))
        self.b_icone.connect("clicked", lambda b: b.get_active() and self.mostra_vista("icone"))
        vista.pack_start(self.b_elenco, False, False, 0)
        vista.pack_start(self.b_icone, False, False, 0)
        box.pack_start(vista, False, False, S(6))
        self.cerca = Gtk.SearchEntry(placeholder_text=T("Cerca", "Search"))
        self.cerca.set_size_request(S(170), -1)
        self.cerca.connect("search-changed", lambda *_: self.riempi())
        box.pack_start(self.cerca, False, False, 0)
        return box

    def bottone(self, icona, azione, aiuto):
        b = Gtk.Button()
        b.add(Gtk.Image.new_from_icon_name(icona, Gtk.IconSize.MENU))
        b.set_tooltip_text(aiuto)
        b.connect("clicked", azione)
        return b

    # contenuto: elenco e icone sullo stesso modello
    def contenuto(self):
        S = self.S
        self.modello = Gtk.ListStore(GdkPixbuf.Pixbuf, GdkPixbuf.Pixbuf, str, str, bool, str, str, str, bool, str)
        self.ordinato = Gtk.TreeModelSort(model=self.modello)
        self.ordinato.set_sort_column_id(self.ORDINE, Gtk.SortType.ASCENDING)
        self.pila = Gtk.Stack()
        # elenco
        tv = Gtk.TreeView(model=self.ordinato, headers_visible=True, enable_search=False, activate_on_single_click=False)
        tv.set_rubber_banding(self.multipli)
        sel = tv.get_selection()
        sel.set_mode(Gtk.SelectionMode.MULTIPLE if self.multipli else Gtk.SelectionMode.SINGLE)
        sel.set_select_function(self.selezionabile, None)
        sel.connect("changed", lambda *_: self.scelta_cambiata())
        tv.connect("row-activated", lambda _t, path, _c: self.attiva(self.ordinato[path][self.PERCORSO]))
        col = Gtk.TreeViewColumn(T("Nome", "Name"))
        col.set_expand(True)
        col.set_sort_column_id(self.ORDINE)
        r_img = Gtk.CellRendererPixbuf(xpad=S(4))
        r_nome = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.MIDDLE, ypad=S(3))
        col.pack_start(r_img, False)
        col.pack_start(r_nome, True)
        col.add_attribute(r_img, "pixbuf", self.PICCOLA)
        col.add_attribute(r_nome, "text", self.NOME)
        col.add_attribute(r_nome, "sensitive", self.ATTIVA)
        col.add_attribute(r_img, "sensitive", self.ATTIVA)
        tv.append_column(col)
        for titolo, c, larghezza in ((T("Data di modifica", "Date Modified"), self.DATA, 150),
                                     (T("Dimensione", "Size"), self.DIM, 90), (T("Tipo", "Kind"), self.TIPO, 130)):
            r = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
            r.set_property("xpad", S(6))
            cc = Gtk.TreeViewColumn(titolo, r, text=c, sensitive=self.ATTIVA)
            cc.set_fixed_width(S(larghezza))
            cc.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            tv.append_column(cc)
        self.elenco = tv
        sc = Gtk.ScrolledWindow()
        sc.add(tv)
        self.pila.add_named(sc, "elenco")
        # icone
        # celle proprie (non pixbuf_column/text_column): così i file che il formato esclude restano in grigio
        iv = Gtk.IconView(model=self.ordinato)
        r_img = Gtk.CellRendererPixbuf()
        r_nome = Gtk.CellRendererText(alignment=Pango.Alignment.CENTER, xalign=0.5, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                      wrap_width=S(96), ellipsize=Pango.EllipsizeMode.MIDDLE)
        iv.pack_start(r_img, False)
        iv.pack_start(r_nome, False)
        iv.add_attribute(r_img, "pixbuf", self.GRANDE)
        iv.add_attribute(r_img, "sensitive", self.ATTIVA)
        iv.add_attribute(r_nome, "text", self.NOME)
        iv.add_attribute(r_nome, "sensitive", self.ATTIVA)
        iv.set_item_width(S(100))
        iv.set_column_spacing(S(6))
        iv.set_selection_mode(Gtk.SelectionMode.MULTIPLE if self.multipli else Gtk.SelectionMode.SINGLE)
        iv.set_activate_on_single_click(False)
        iv.connect("selection-changed", self.solo_attive)
        iv.connect("item-activated", lambda _i, path: self.attiva(self.ordinato[path][self.PERCORSO]))
        self.icone = iv
        sc2 = Gtk.ScrolledWindow()
        sc2.add(iv)
        self.pila.add_named(sc2, "icone")
        self.vuota = Gtk.Label(label=T("Nessun elemento", "No items"))
        self.vuota.get_style_context().add_class("tod-file-vuota")
        self.pila.add_named(self.vuota, "vuota")
        return self.pila

    def selezionabile(self, _sel, modello, path, _gia, _dati):
        return modello[path][self.ATTIVA]

    def solo_attive(self, iv):
        for path in iv.get_selected_items():   # GtkIconView non ha una funzione di selezione: si toglie dopo
            if not self.ordinato[path][self.ATTIVA]:
                iv.unselect_path(path)
        self.scelta_cambiata()

    def mostra_vista(self, quale):
        self.vista = quale
        self.b_elenco.set_active(quale == "elenco")
        self.b_icone.set_active(quale == "icone")
        if len(self.modello):
            self.pila.set_visible_child_name(quale)
        self.salva_conf(vista=quale)
        if quale == "icone":
            self.miniature()

    # fondo: formato, nuova cartella, pulsanti
    def fondo(self):
        S = self.S
        box = Gtk.Box(spacing=S(8))
        box.get_style_context().add_class("tod-file-fondo")
        if self.modo in ("salva", "cartella"):
            b = Gtk.Button(label=T("Nuova cartella", "New Folder"))
            b.get_style_context().add_class("tod-file-pillola")
            b.connect("clicked", lambda *_: self.nuova_cartella())
            box.pack_start(b, False, False, 0)
        if len(self.filtri) > 1:
            box.pack_start(Gtk.Label(label=T("Formato:", "Format:")), False, False, 0)
            c = Gtk.ComboBoxText()
            for nome, _m in self.filtri:
                c.append_text(nome)
            c.set_active(0)
            c.connect("changed", lambda c: self.cambia_filtro(c.get_active()))
            box.pack_start(c, False, False, 0)
        elif self.filtri:
            box.pack_start(Gtk.Label(label=self.filtri[0][0], ellipsize=Pango.EllipsizeMode.END), False, False, 0)
        presa = Gtk.EventBox()
        presa.connect("button-press-event", self.trascina)
        box.pack_start(presa, True, True, 0)
        annulla = Gtk.Button(label=T("Annulla", "Cancel"))
        annulla.get_style_context().add_class("tod-file-pillola")
        annulla.connect("clicked", lambda *_: self.chiudi(None))
        box.pack_start(annulla, False, False, 0)
        testo = {"apri": T("Apri", "Open"), "salva": T("Salva", "Save"), "cartella": T("Scegli", "Choose")}[self.modo]
        self.principale = Gtk.Button(label=testo, can_default=True)
        self.principale.get_style_context().add_class("tod-file-pillola")
        self.principale.get_style_context().add_class("tod-file-principale")
        self.principale.connect("clicked", lambda *_: self.conferma())
        box.pack_start(self.principale, False, False, 0)
        self.set_default(self.principale)
        return box

    def cambia_filtro(self, i):
        self.filtro = self.filtri[i][1] if 0 <= i < len(self.filtri) else []
        self.riempi()

    # navigazione
    def vai(self, percorso, storia=True):
        if percorso != ":recenti":
            percorso = os.path.normpath(percorso)   # "/home/dex/Lavori/" di zenity: senza la barra finale
            rete_entra(percorso)
            if not os.path.isdir(percorso):
                return
        if storia and self.qui and self.qui != percorso:
            self.indietro.append(self.qui)
            self.avanti.clear()
        self.qui = percorso
        self.cerca.set_text("")
        self.riempi()
        self.aggiorna_barra()
        for riga in self.lista_posti.get_children():
            if riga.percorso == percorso or (percorso != ":recenti" and riga.percorso != ":recenti"
                                             and os.path.realpath(riga.percorso) == os.path.realpath(percorso)):
                self.lista_posti.select_row(riga)
                break
        else:
            self.lista_posti.unselect_all()

    def storia(self, passo):
        da, a = (self.indietro, self.avanti) if passo < 0 else (self.avanti, self.indietro)
        if da:
            a.append(self.qui)
            self.qui = da.pop()
            self.cerca.set_text("")
            self.riempi()
            self.aggiorna_barra()

    def aggiorna_barra(self):
        self.b_indietro.set_sensitive(bool(self.indietro))
        self.b_avanti.set_sensitive(bool(self.avanti))
        if self.qui == ":recenti":
            self.nome_cartella.set_text(T("Recenti", "Recents"))
            self.icona_cartella.set_from_icon_name("document-open-recent-symbolic", Gtk.IconSize.MENU)
            self.b_cartella.set_popup(None)
        else:
            self.nome_cartella.set_text(self.nome_mostrato(self.qui))
            pb = icona_di(self.qui, self.S(18), cartella=True)
            if pb:
                self.icona_cartella.set_from_pixbuf(pb)
            menu = Gtk.Menu()
            p = self.qui
            while True:   # il percorso fino alla radice, come il menu della cartella di macOS
                voce = Gtk.MenuItem(label=self.nome_mostrato(p))
                voce.connect("activate", lambda _v, d=p: self.vai(d))
                menu.append(voce)
                su = os.path.dirname(p)
                if su == p:
                    break
                p = su
            menu.show_all()
            self.b_cartella.set_popup(menu)
        self.aggiorna_pulsante()

    def nome_mostrato(self, p):
        for _s, nome, _i, d in self.i_posti:
            if d != ":recenti" and os.path.realpath(d) == os.path.realpath(p):
                return nome
        return os.path.basename(p.rstrip("/")) or "/"

    def riempi(self):
        """il contenuto della cartella: prima le cartelle, poi i file, per nome; nascosti e non adatti in grigio/via"""
        self.lavoro += 1
        # un modello nuovo ogni volta (svuotare quello ordinato sotto le viste fa protestare GTK)
        self.modello = Gtk.ListStore(GdkPixbuf.Pixbuf, GdkPixbuf.Pixbuf, str, str, bool, str, str, str, bool, str)
        cerca = self.cerca.get_text().strip().lower()
        if self.qui == ":recenti":
            voci = [(os.path.basename(p), p) for p in recenti()]
        else:
            try:
                voci = [(e.name, e.path) for e in os.scandir(self.qui)]
            except OSError:
                voci = []
        S = self.S
        for nome, p in voci:
            if not self.nascosti and nome.startswith("."):
                continue
            if cerca and cerca not in nome.lower():
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            cartella = os.path.isdir(p)
            if self.modo == "cartella" and not cartella:
                attiva = False
            else:
                attiva = cartella or corrisponde(nome, self.filtro)
            if cartella:
                dim, tipo = "--", T("Cartella", "Folder")
            else:
                dim = dimensione(st.st_size)
                ct, _ = Gio.content_type_guess(nome, None)
                tipo = Gio.content_type_get_description(ct)
            self.modello.append([icona_di(p, S(18), cartella), icona_di(p, S(64), cartella), nome, p, cartella,
                                 data_breve(st.st_mtime), dim, tipo, attiva, ("0" if cartella else "1") + nome.lower()])
        self.ordinato = Gtk.TreeModelSort(model=self.modello)
        self.ordinato.set_sort_column_id(self.ORDINE, Gtk.SortType.ASCENDING)
        self.elenco.set_model(self.ordinato)
        self.icone.set_model(self.ordinato)
        if len(self.modello):
            self.pila.set_visible_child_name(getattr(self, "vista", "elenco"))
        else:
            self.pila.set_visible_child_name("vuota")
        if getattr(self, "vista", "") == "icone":
            self.miniature()
        self.aggiorna_pulsante()

    def miniature(self):
        """le miniature delle immagini nella vista a icone, in sottofondo"""
        giro = self.lavoro
        da_fare = [(Gtk.TreeRowReference.new(self.modello, r.path), r[self.PERCORSO]) for r in self.modello
                   if r[self.PERCORSO].lower().endswith(IMMAGINI)][:300]
        S = self.S

        def lavora():
            for rif, p in da_fare:
                if giro != self.lavoro:
                    return
                pb = miniatura(p, S(64))
                if pb:
                    GLib.idle_add(metti, rif, pb)

        def metti(rif, pb):
            if giro == self.lavoro and rif.valid():
                self.modello[rif.get_path()][self.GRANDE] = pb
            return False
        if da_fare:
            threading.Thread(target=lavora, daemon=True).start()

    # scelta
    def scelti(self):
        if self.pila.get_visible_child_name() == "icone":
            paths = self.icone.get_selected_items()
        else:
            _m, paths = self.elenco.get_selection().get_selected_rows()
        return [self.ordinato[p][self.PERCORSO] for p in paths]

    def scelta_cambiata(self):
        sel = self.scelti()
        if self.modo == "salva" and len(sel) == 1 and not os.path.isdir(sel[0]):
            self.nome.set_text(os.path.basename(sel[0]))   # come macOS: un file scelto dà il nome
        self.aggiorna_pulsante()

    def aggiorna_pulsante(self):
        if not hasattr(self, "principale"):
            return
        sel = self.scelti() if hasattr(self, "elenco") else []
        if self.modo == "apri":
            ok = any(not os.path.isdir(p) for p in sel) or (len(sel) == 1 and os.path.isdir(sel[0]))
        elif self.modo == "salva":
            ok = bool(self.nome.get_text().strip()) and self.qui != ":recenti"
        else:
            ok = self.qui != ":recenti" or bool(sel)
        self.principale.set_sensitive(ok)

    def attiva(self, p):
        if os.path.isdir(p):
            self.vai(p)
        elif self.modo == "apri":
            self.chiudi([p])
        elif self.modo == "salva":
            self.nome.set_text(os.path.basename(p))
            self.conferma()

    def conferma(self):
        sel = self.scelti()
        if self.modo == "apri":
            file = [p for p in sel if not os.path.isdir(p)]
            if not file and len(sel) == 1:
                self.vai(sel[0])   # una cartella scelta: ci si entra, come macOS
                return
            if file:
                self.chiudi(file if self.multipli else file[:1])
        elif self.modo == "cartella":
            cartelle = [p for p in sel if os.path.isdir(p)] or ([self.qui] if self.qui != ":recenti" else [])
            if cartelle:
                self.chiudi(cartelle if self.multipli else cartelle[:1])
        else:
            nome = self.nome.get_text().strip()
            if not nome or "/" in nome or self.qui == ":recenti":
                return
            p = os.path.join(self.qui, nome)
            if os.path.isdir(p):
                self.vai(p)
                return
            if os.path.exists(p) and self.r.get("conferma", True) and not self.sostituire(nome):
                return
            self.chiudi([p])

    def sostituire(self, nome):
        import tod_avviso
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                              buttons=Gtk.ButtonsType.NONE, text=T(f"«{nome}» esiste già. Vuoi sostituirlo?",
                                                                    f"“{nome}” already exists. Do you want to replace it?"))
        d.format_secondary_text(T(f"In «{self.nome_mostrato(self.qui)}» c'è già un elemento con lo stesso nome: "
                                  "sostituendolo, il suo contenuto verrà sovrascritto.",
                                  f"An item with the same name already exists in “{self.nome_mostrato(self.qui)}”. "
                                  "Replacing it will overwrite its current contents."))
        d.add_button(T("Sostituisci", "Replace"), Gtk.ResponseType.OK)
        d.add_button(T("Annulla", "Cancel"), Gtk.ResponseType.CANCEL).get_style_context().add_class("suggested-action")
        d.set_default_response(Gtk.ResponseType.CANCEL)
        ok = tod_avviso.come_macos(d).run() == Gtk.ResponseType.OK
        d.destroy()
        return ok

    def nuova_cartella(self):
        if self.qui == ":recenti":
            return
        base = T("cartella senza titolo", "untitled folder")
        p, n = os.path.join(self.qui, base), 2
        while os.path.exists(p):
            p, n = os.path.join(self.qui, f"{base} {n}"), n + 1
        try:
            os.mkdir(p)
        except OSError:
            return
        self.vai(p)

    def tasto(self, _w, ev):
        k = ev.keyval
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        if k == Gdk.KEY_Escape:
            self.chiudi(None)
            return True
        if ctrl and k in (Gdk.KEY_h, Gdk.KEY_H, Gdk.KEY_period):
            self.nascosti = not self.nascosti
            self.riempi()
            return True
        if ctrl and k == Gdk.KEY_f:
            self.cerca.grab_focus()
            return True
        alt = ev.state & Gdk.ModifierType.MOD1_MASK
        if (alt and k == Gdk.KEY_Up) or (k == Gdk.KEY_BackSpace and not isinstance(self.get_focus(), Gtk.Entry)):
            if self.qui != ":recenti" and os.path.dirname(self.qui) != self.qui:
                self.vai(os.path.dirname(self.qui))
            return True
        if alt and k == Gdk.KEY_Left:
            self.storia(-1)
            return True
        if alt and k == Gdk.KEY_Right:
            self.storia(1)
            return True
        return False

    def chiudi(self, percorsi):
        if self.fine is None:
            return
        fine, self.fine = self.fine, None
        if percorsi and self.qui != ":recenti":
            self.salva_conf(ultima=self.qui)
        elif percorsi:
            self.salva_conf(ultima=os.path.dirname(percorsi[0]))
        for p in percorsi or []:   # come macOS: aperti e salvati finiscono nei Recenti
            if self.modo != "cartella":
                try:
                    Gtk.RecentManager.get_default().add_item("file://" + urllib.parse.quote(p))
                except Exception:
                    pass
        self.destroy()
        fine(percorsi)


def scegli(r):
    """Apre il pannello e aspetta. → (codice, stampa) come zenity, con i percorsi come li vede l'app del container"""
    percorsi = scegli_veri(r)
    if not percorsi:
        return 1, ""
    sep = r.get("separatore") or "|"
    return 0, sep.join(al_container(x) for x in percorsi) + "\n"


def scegli_veri(r):
    """Apre il pannello e aspetta (ciclo annidato: dal filo di GTK del servizio). → i percorsi veri scelti ([] se
    annullato): per il portale (share/tod_portale.py), dove valgono per ogni app, con e senza proot"""
    carica_css()
    esito = {}
    ciclo = GLib.MainLoop()

    def fine(percorsi):
        esito["p"] = percorsi
        ciclo.quit()
    p = Pannello(r, fine)
    p.present()
    ciclo.run()
    return list(esito.get("p") or [])
