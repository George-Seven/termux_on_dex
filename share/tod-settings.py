#!/usr/bin/env python3
"""Impostazioni, come quelle di macOS: barra laterale con le voci, a destra il pannello.

  tod-settings.py [voce]        voce: rete, batteria, aspetto, schermo, suono, notifiche, telefono,
                                tastiera, mouse, stampanti, app, archiviazione, aggiornamenti, info, avanzate

Una finestra sola al posto dei tanti pannelli di XFCE. Le cose semplici si fanno qui (tema, sfondo,
dimensioni, volume, non disturbare, app di Android...); per quelle complesse (scorciatoie, mouse,
tipi di file) c'è il pulsante che apre il pannello XFCE già esistente. Le opzioni tecniche stanno
in "Avanzate". Gira in Termux (PyGObject, GTK 3) con l'ambiente della sessione.
"""
import json
import os
import signal
import re
import shutil
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")   # senza, Termux carica Gdk 4 e GTK 3 non parte
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402
sys.path.insert(1, os.path.dirname(os.path.abspath(__file__)))   # share/ anche se caricato da altrove (i test)
import tod_avviso  # noqa: E402  (avvisi come macOS, share/tod_avviso.py)

try:   # GLib 2.86: le funzioni Unix di Gio stanno in GioUnix
    gi.require_version("GioUnix", "2.0")
    from gi.repository import GioUnix
    DesktopAppInfo = GioUnix.DesktopAppInfo
except (ValueError, ImportError):
    DesktopAppInfo = Gio.DesktopAppInfo

GLib.set_prgname("tod-settings")   # la barra riconosce la finestra (tod-settings.desktop) e ne mostra l'icona

PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
HOME = os.environ.get("TOD_TERMUX_HOME") or os.environ.get("HOME", "/data/data/com.termux/files/home")
TOD = os.path.join(PREFIX, "bin/tod")
TMP = os.environ.get("TMPDIR", os.path.join(PREFIX, "tmp"))
CONF = os.path.join(HOME, ".config/tod/tod.conf")
BARRA = os.path.join(HOME, ".config/tod/barra.json")   # impostazioni della barra (share/tod-barra.py)
STATE = os.path.join(HOME, ".local/state/tod")
HERE = os.path.dirname(os.path.abspath(__file__))
STAMPA = os.path.join(HERE, "stampa/tod-stampa.py")   # il servizio di stampa (lib/stampa.sh)
SCANNER = os.path.join(HERE, "stampa/tod-scanner.py")  # gli scanner (eSCL) e la loro finestra
RETE = os.path.join(HERE, "rete/tod-rete.py")         # la Rete in Esplora file (lib/rete.sh)
ITALIAN = os.environ.get("LANGUAGE", os.environ.get("LANG", "")).startswith("it")

def T(it, en):
    return it if ITALIAN else en


DEVICES = {"SM-S948": "Galaxy S26 Ultra", "SM-S947": "Galaxy S26+", "SM-S942": "Galaxy S26",
           "SM-S938": "Galaxy S25 Ultra", "SM-S936": "Galaxy S25+", "SM-S931": "Galaxy S25",
           "SM-S928": "Galaxy S24 Ultra", "SM-F966": "Galaxy Z Fold7", "SM-F956": "Galaxy Z Fold6"}
CHIPS = {"SM8850": "Snapdragon 8 Elite Gen 5", "SM8750": "Snapdragon 8 Elite", "SM8650": "Snapdragon 8 Gen 3",
         "SM8550": "Snapdragon 8 Gen 2"}
SIZES = [(T("Più piccolo", "Smaller"), 96), (T("Predefinito", "Default"), 0),
         (T("Grande", "Large"), 128), (T("Più grande", "Larger"), 144)]
ACCENTS = [("blu", "#0067c0", T("Blu", "Blue")), ("viola", "#8a5cf5", T("Viola", "Purple")),
           ("rosa", "#c239b3", T("Rosa", "Pink")), ("rosso", "#d13438", T("Rosso", "Red")),
           ("arancione", "#ca5010", T("Arancione", "Orange")), ("giallo", "#b58900", T("Giallo", "Yellow")),
           ("verde", "#107c10", T("Verde", "Green")), ("acqua", "#038387", T("Verde acqua", "Teal")),
           ("grafite", "#68768a", T("Grafite", "Graphite"))]


# --- piccoli aiuti ------------------------------------------------------------------------
def sh(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def spawn(cmd):
    try:
        subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def tod(*args, then=None):
    """tod in sottofondo (alcuni comandi richiedono qualche secondo), poi then() nella UI."""
    def work():
        try:
            subprocess.run(["bash", TOD, *args], capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if then:
            GLib.idle_add(lambda: then() and False)
    threading.Thread(target=work, daemon=True).start()


def conf_get(key, default=""):
    try:
        for line in open(CONF):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return default


def conf_set(key, value):
    lines = []
    try:
        lines = [l for l in open(CONF) if not l.startswith(key + "=")]
    except OSError:
        os.makedirs(os.path.dirname(CONF), exist_ok=True)
    lines.append(f'{key}="{value}"\n')
    with open(CONF + ".tmp", "w") as f:
        f.writelines(lines)
    os.replace(CONF + ".tmp", CONF)


def xq(channel, prop, *set_args):
    if set_args:
        sh(["xfconf-query", "-c", channel, "-p", prop, "-n", *set_args])
        return None
    return sh(["xfconf-query", "-c", channel, "-p", prop])


# i valori di partenza di XFCE (quelli che xfsettingsd usa se non c'è niente): Ripristina predefiniti li riscrive
TASTIERA_PREDEFINITI = [("keyboards", "/Default/KeyRepeat", True), ("keyboards", "/Default/KeyRepeat/Rate", 20),
                        ("keyboards", "/Default/KeyRepeat/Delay", 500), ("keyboards", "/Default/Numlock", False),
                        ("xsettings", "/Net/CursorBlink", True)]
MOUSE_PREDEFINITI = [("RightHanded", True), ("ReverseScrolling", False)]
DOPPIO_CLIC_PREDEFINITO = 400


def senza_rotella(w):
    """13 novembre, Pietro: scorrendo la pagina con la rotellina, se il puntatore passava sopra un cursore lo
    cambiava ("e non so più com'era"); lo stesso le tendine. Come su macOS la rotellina sopra di loro scorre la
    pagina: il loro scroll-event si ferma qui (prima del gestore di GTK) e passa alla pagina. Si chiama su ogni pagina
    appena fatta e sulle parti ridisegnate dopo (Tastiera); una volta sola per controllo."""
    def passa(widget, ev):
        sw = widget.get_ancestor(Gtk.ScrolledWindow)
        if sw is not None:
            sw.event(ev)
        return True
    if isinstance(w, (Gtk.Scale, Gtk.ComboBox, Gtk.SpinButton)) and not getattr(w, "_senza_rotella", False):
        w._senza_rotella = True
        w.connect("scroll-event", passa)
    if isinstance(w, Gtk.Container):
        w.forall(senza_rotella)


def xf_valore(canale, prop, predefinito):
    """un valore di xfconf con il suo tipo (bool, int o testo); se non c'è, il predefinito di XFCE"""
    v = xq(canale, prop)
    if not v:
        return predefinito
    if isinstance(predefinito, bool):
        return v.strip().lower() == "true"
    if isinstance(predefinito, int):
        try:
            return int(float(v))
        except ValueError:
            return predefinito
    return v


def xf_scrivi(canale, prop, valore):
    tipo = "bool" if isinstance(valore, bool) else "int" if isinstance(valore, int) else "string"
    testo = ("true" if valore else "false") if isinstance(valore, bool) else str(valore)
    sh(["xfconf-query", "-c", canale, "-p", prop, "-n", "-t", tipo, "-s", testo])


def phone_state():
    try:
        return json.load(open(os.path.join(TMP, "tod-phone.json")))
    except (OSError, ValueError):
        return {}


def am(action):
    spawn([os.path.join(PREFIX, "bin/am"), "start", "--user", "0", "-a", action])


def firma_rete():
    """Cosa mostra la pagina Wi-Fi e rete: i PC in rete (l'elenco del servizio) e lo stato del Wi-Fi."""
    try:
        pc = open(os.path.join(TMP, "tod-rete/volumi")).read()
    except OSError:
        pc = ""
    w = phone_state().get("wifi") or {}
    return pc, w.get("ip"), w.get("ssid"), w.get("supplicant_state")


# --- finestra -----------------------------------------------------------------------------
CSS = b"""
.side { background-color: alpha(@theme_fg_color, 0.035); border-right: 1px solid alpha(@theme_fg_color, 0.08); }
.side list, .side row { background: transparent; }
.side row { border-radius: 6px; margin: 1px 8px; padding: 5px 6px; }
.side row:selected { background-color: @theme_selected_bg_color; color: @theme_selected_fg_color; }
.badge { border-radius: 6px; min-width: 24px; min-height: 24px; color: #ffffff; }
.b-blue { background-color: #0a84ff; } .b-green { background-color: #30b94d; } .b-purple { background-color: #8e5cf7; }
.b-red { background-color: #ff453a; } .b-orange { background-color: #ff9f0a; } .b-gray { background-color: #8e8e93; }
.b-teal { background-color: #30b0c7; } .b-pink { background-color: #ff375f; } .b-indigo { background-color: #5e5ce6; }
.pane-title { font-size: 17pt; font-weight: bold; }
.group { background-color: alpha(@theme_fg_color, 0.04); border: 1px solid alpha(@theme_fg_color, 0.08); border-radius: 10px; }
.group-row { padding: 10px 14px; }
.group-sep { background-color: alpha(@theme_fg_color, 0.08); min-height: 1px; }
.group-title { font-weight: bold; opacity: 0.75; margin: 8px 4px 2px 4px; }
.hint { opacity: 0.65; font-size: 9pt; }
.big-num { font-size: 26pt; font-weight: bold; }
.choice { padding: 10px; border-radius: 10px; }
.choice.selected { box-shadow: inset 0 0 0 2px @theme_selected_bg_color; }
.thumb { border-radius: 8px; padding: 3px; }
.thumb.selected { box-shadow: inset 0 0 0 3px @theme_selected_bg_color; }
.arch-uso { font-size: 13pt; font-weight: bold; }
.arch-legenda { font-size: 9pt; opacity: 0.8; }
.arch-voce { padding: 9px 14px; border: none; border-radius: 0; background: none; box-shadow: none; }
.arch-voce:hover { background-color: alpha(@theme_fg_color, 0.05); }
.arch-voce label, .arch-voce image { color: @theme_fg_color; }
.arch-voce:first-child { border-top-left-radius: 10px; border-top-right-radius: 10px; }
.arch-voce:last-child { border-bottom-left-radius: 10px; border-bottom-right-radius: 10px; }
.arch-misura { opacity: 0.7; font-feature-settings: "tnum"; }
.arch-indietro { color: @theme_selected_bg_color; padding: 2px 6px 2px 0; }
.kb-sigla { font-size: 8pt; font-weight: bold; padding: 1px 5px; border-radius: 4px;
            border: 1px solid alpha(@theme_fg_color, 0.45); color: alpha(@theme_fg_color, 0.8); }
"""


class Settings(Gtk.Window):
    PANES = [  # chiave, titolo, icona, colore, parole per la ricerca
        ("rete", T("Wi-Fi e rete", "Wi-Fi & Network"), "network-wireless-symbolic", "blue",
         "wifi internet rete mobile pc nas computer cartelle condivise samba smb windows"),
        ("batteria", T("Batteria", "Battery"), "battery-good-symbolic", "green", "energia carica"),
        ("aspetto", T("Aspetto", "Appearance"), "preferences-desktop-wallpaper-symbolic", "purple",
         "tema scuro chiaro sfondo wallpaper colori"),
        ("schermo", T("Schermo", "Display"), "video-display-symbolic", "indigo", "dimensioni testo grandezza dpi risoluzione"),
        ("barra", T("Barra delle applicazioni", "Taskbar"), "view-app-grid-symbolic", "blue",
         "barra applicazioni fissate icone allineamento centro sinistra anteprime start"),
        ("suono", T("Suono", "Sound"), "audio-volume-high-symbolic", "pink", "volume audio muto"),
        ("notifiche", T("Notifiche", "Notifications"), "preferences-system-notifications-symbolic", "red",
         "non disturbare avvisi"),
        ("telefono", T("Telefono", "Phone"), "phone-symbolic", "teal", "android app notifiche telefono"),
        ("tastiera", T("Tastiera", "Keyboard"), "input-keyboard-symbolic", "gray",
         "layout lingua scorciatoie ripetizione tasti bloc num cursore lampeggiante combinazioni"),
        ("mouse", T("Mouse", "Mouse"), "input-mouse-symbolic", "gray",
         "puntatore velocità doppio clic scorrimento naturale tasto destro sinistro mancino trackpad"),
        ("stampanti", T("Stampanti e scanner", "Printers & Scanners"), "printer-symbolic", "gray",
         "stampante stampare stampa scanner usb wifi pdf coda lavori driver"),
        ("app", T("App", "Apps"), "applications-system-symbolic", "orange",
         "app installate disinstalla rimuovi elimina predefinite browser apri con file"),
        ("archiviazione", T("Archiviazione", "Storage"), "drive-harddisk-symbolic", "gray",
         "spazio memoria disco occupato libero pieno pulisci cache cestino temporanei file grandi storage"),
        ("aggiornamenti", T("Aggiornamenti", "Updates"), "software-update-available-symbolic", "blue", "update pacchetti"),
        ("info", T("Informazioni", "About"), "help-about-symbolic", "gray", "dispositivo modello memoria spazio versione"),
        ("avanzate", T("Avanzate", "Advanced"), "preferences-other-symbolic", "gray", "gpu grafica sviluppo xfce"),
    ]

    def __init__(self, first=None, app=None):
        super().__init__(title=T("Impostazioni", "Settings"), application=app)
        self.set_default_size(920, 640)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name("preferences-system")
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        if app is None:
            self.connect("destroy", Gtk.main_quit)

        outer = Gtk.Box()
        self.add(outer)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        side.get_style_context().add_class("side")
        side.set_size_request(250, -1)
        outer.pack_start(side, False, False, 0)
        self.search = Gtk.SearchEntry(placeholder_text=T("Cerca", "Search"), margin=12)
        self.search.set_margin_bottom(0)
        self.search.connect("search-changed", lambda *a: self.sidebar.invalidate_filter())
        side.pack_start(self.search, False, False, 0)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar.set_filter_func(self.filter_row)
        self.sidebar.connect("row-selected", self.on_select)
        sw.add(self.sidebar)
        side.pack_start(sw, True, True, 0)
        self.rows = {}
        for key, title, icon, color, words in self.PANES:
            r = Gtk.ListBoxRow()
            r.key, r.words = key, (title + " " + words).lower()
            h = Gtk.Box(spacing=10)
            badge = Gtk.Box()
            badge.get_style_context().add_class("badge")
            badge.get_style_context().add_class("b-" + color)
            img = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU)
            img.set_margin_start(4)
            img.set_margin_end(4)
            badge.pack_start(img, True, True, 0)
            h.pack_start(badge, False, False, 0)
            h.pack_start(Gtk.Label(label=title, xalign=0), True, True, 0)
            r.add(h)
            if key == "info":   # come macOS: le voci di sistema separate dalle altre
                r.set_margin_top(10)
            self.sidebar.add(r)
            self.rows[key] = r

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        outer.pack_start(self.stack, True, True, 0)
        self.built = {}
        self.gone = set()
        self.arch_dati, self.arch_aperta, self.arch_in_misura = None, None, False
        self.show_all()
        self.sidebar.select_row(self.rows.get(first) or self.rows["aspetto"])

    def ricostruisci(self, key):
        """ridisegna una pagina già aperta (dopo Ripristina predefiniti)"""
        old = self.stack.get_child_by_name(key)
        if old:
            self.stack.remove(old)
        page = self.page(key)
        self.stack.add_named(page, key)
        page.show_all()
        self.built[key] = True
        self.stack.set_visible_child_name(key)

    def ripristina(self, titolo, testo, fatto):
        """il pulsante in fondo alla pagina, come macOS; chiede conferma (non si torna indietro)"""
        b = Gtk.Button(label=T("Ripristina predefiniti…", "Restore Defaults…"), halign=Gtk.Align.END)

        def chiedi(*_):
            d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                  buttons=Gtk.ButtonsType.NONE, text=titolo)
            d.format_secondary_text(testo)
            d.add_button(T("Annulla", "Cancel"), Gtk.ResponseType.CANCEL)
            d.add_button(T("Ripristina", "Restore"), Gtk.ResponseType.OK)
            d.set_default_response(Gtk.ResponseType.OK)
            tod_avviso.come_macos(d)
            r = d.run()
            d.destroy()
            if r == Gtk.ResponseType.OK:
                fatto()
        b.connect("clicked", chiedi)
        b.set_margin_top(6)
        return b

    def open_pane(self, key):
        if key in self.rows:
            self.search.set_text("")
            self.sidebar.select_row(self.rows[key])
        self.present()

    def filter_row(self, row):
        q = self.search.get_text().strip().lower()
        return not q or all(w in row.words for w in q.split())

    def on_select(self, _lb, row):
        if row is None:
            return
        key = row.key
        if key not in self.built or key in ("rete", "batteria", "aggiornamenti", "suono", "stampanti"):   # dati che cambiano
            old = self.stack.get_child_by_name(key)
            if old:
                self.stack.remove(old)
            page = self.page(key)
            self.stack.add_named(page, key)
            page.show_all()
            self.built[key] = True
        self.stack.set_visible_child_name(key)
        if key == "archiviazione" and self.arch_dati and time.time() - self.arch_dati[0].get("quando", 0) > 300:
            self.arch_misura()   # come macOS: tornandoci dopo un po' si rifanno i conti

    # --- mattoncini come macOS: titolo, gruppi arrotondati con righe ---------------------
    def page(self, key):
        title = next(p[1] for p in self.PANES if p[0] == key)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=28)
        box.set_margin_top(20)
        lab = Gtk.Label(label=title, xalign=0)
        lab.get_style_context().add_class("pane-title")
        box.pack_start(lab, False, False, 4)
        getattr(self, "pane_" + key)(box)
        senza_rotella(box)
        sw.add(box)
        return sw

    def group(self, box, title=None, rows=()):
        if title:
            t = Gtk.Label(label=title, xalign=0)
            t.get_style_context().add_class("group-title")
            box.pack_start(t, False, False, 0)
        g = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        g.get_style_context().add_class("group")
        for i, r in enumerate(rows):
            if i:
                sep = Gtk.Box()
                sep.get_style_context().add_class("group-sep")
                g.pack_start(sep, False, False, 0)
            g.pack_start(r, False, False, 0)
        box.pack_start(g, False, False, 0)
        return g

    def row(self, label, widget=None, hint=None):
        h = Gtk.Box(spacing=12)
        h.get_style_context().add_class("group-row")
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        v.pack_start(Gtk.Label(label=label, xalign=0, wrap=True), False, False, 0)
        if hint:
            hl = Gtk.Label(label=hint, xalign=0, wrap=True)
            hl.get_style_context().add_class("hint")
            v.pack_start(hl, False, False, 0)
        h.pack_start(v, True, True, 0)
        if widget is not None:
            widget.set_valign(Gtk.Align.CENTER)
            h.pack_end(widget, False, False, 0)
        return h

    def switch(self, active, on_change):
        s = Gtk.Switch(active=active)
        s.connect("notify::active", lambda w, _p: on_change(w.get_active()))
        return s

    def button(self, label, cb):
        b = Gtk.Button(label=label)
        b.connect("clicked", lambda *a: cb())
        return b

    def value(self, text):
        lab = Gtk.Label(label=text, selectable=True)
        lab.get_style_context().add_class("dim-label")
        return lab

    # --- pannelli -----------------------------------------------------------------------------
    def pane_rete(self, box):
        st = phone_state()
        w, ph = st.get("wifi") or {}, st.get("phone") or {}
        rows = []
        if w.get("supplicant_state") == "COMPLETED" or (w.get("ip") not in (None, "0.0.0.0") and w.get("rssi", -127) > -127):
            ssid = (w.get("ssid") or "").strip('"') or T("rete Wi-Fi", "Wi-Fi network")
            rows.append(self.row("Wi-Fi", self.value(T("Connesso", "Connected")), ssid))
            rows.append(self.row(T("Segnale", "Signal"), self.value(f"{w.get('rssi')} dBm")))
            if w.get("ip"):
                rows.append(self.row(T("Indirizzo IP", "IP address"), self.value(w["ip"])))
        elif "error" in w:
            rows.append(self.row("Wi-Fi", self.value(T("Non disponibile", "Unavailable")),
                                 T("Per vedere la rete Wi-Fi, dai a Termux:API il permesso Posizione nelle impostazioni del telefono.",
                                   "To see the Wi-Fi network, give Termux:API the Location permission in the phone settings.")))
        else:
            rows.append(self.row("Wi-Fi", self.value(T("Non connesso", "Not connected"))))
        if ph.get("network_operator_name"):
            rows.append(self.row(T("Rete mobile", "Mobile network"),
                                 self.value(f"{ph.get('network_operator_name')} {ph.get('network_type') or ''}".strip())))
        self.group(box, None, rows)
        hint = Gtk.Label(label=T("La connessione la gestisce il telefono: per cambiare rete usa le sue impostazioni.",
                                 "The phone manages the connection: use its settings to change network."), xalign=0, wrap=True)
        hint.get_style_context().add_class("hint")
        box.pack_start(hint, False, False, 0)
        self.group(box, None, [self.row(T("Impostazioni di rete del telefono", "Phone network settings"),
                                        self.button(T("Apri", "Open"), lambda: am("android.settings.WIFI_SETTINGS")))])
        self.rete_pc(box)

    # --- i computer in rete (share/rete/tod-rete.py): le loro cartelle condivise in Esplora file, sotto "Rete" ----
    def rete_pc(self, box):
        try:
            e = json.loads(sh([sys.executable, RETE, "elenco", "--json"], timeout=10) or "{}")
        except ValueError:
            e = {}
        if not e.get("servizio"):
            return
        righe = []
        for p in e.get("pc", []):
            voci = []
            if p.get("password"):
                voci.append(self.button(T("Dimentica la password", "Forget password"),
                                        lambda n=p["nome"]: self.rete_cmd("dimentica", n)))
            if p.get("manuale"):
                voci.append(self.button(T("Rimuovi", "Remove"), lambda n=p["nome"]: self.rete_cmd("togli", n)))
            hb = Gtk.Box(spacing=6)
            for v in voci:
                hb.pack_start(v, False, False, 0)
            righe.append(self.row(p["nome"], hb, p["ip"]))
        if not righe:
            righe.append(self.row(T("Nessun computer trovato", "No computers found"), None,
                                  T("PC e NAS della rete Wi-Fi compaiono da soli.", "PCs and NAS on the Wi-Fi network appear automatically.")))
        # "Aggiorna" (9 novembre, Pietro): il servizio cerca subito; da solo lo fa ogni minuto e quando cambi rete
        cerca = self.button(T("Aggiorna", "Refresh"), lambda: self.rete_aggiorna(cerca))
        righe.append(self.row(T("Cerca di nuovo in rete", "Search the network again"), cerca,
                              T("I computer si cercano anche da soli ogni minuto e quando il telefono cambia rete.",
                                "Computers are also searched for automatically every minute and when the phone changes network.")))
        voce = Gtk.Entry(placeholder_text=T("192.168.1.10 o nome", "192.168.1.10 or name"), width_chars=20)
        self.rete_voce = voce
        voce.connect("activate", lambda w: self.rete_cmd("aggiungi", w.get_text()))
        hb = Gtk.Box(spacing=6)
        hb.pack_start(voce, False, False, 0)
        hb.pack_start(self.button(T("Aggiungi", "Add"), lambda: self.rete_cmd("aggiungi", voce.get_text())), False, False, 0)
        righe.append(self.row(T("Aggiungi con l'indirizzo", "Add by address"), hb,
                              T("Per un computer che non compare da solo.", "For a computer that doesn't appear by itself.")))
        righe.append(self.row(T("Apri la Rete in Esplora file", "Open Network in the file manager"),
                              self.button(T("Apri", "Open"), lambda: spawn(["exo-open", "--launch", "FileManager", "/media/rete"]))))
        self.group(box, T("Computer in rete", "Computers on the network"), righe)
        self.rete_firma = firma_rete()
        if not getattr(self, "rete_segue", False):
            self.rete_segue = True
            GLib.timeout_add_seconds(3, self.segui_rete)

    def segui_rete(self):
        """Pagina aperta: si rifà se cambiano i PC (il servizio li aggiunge e li toglie da sé) o il Wi-Fi. Non mentre si
        scrive un indirizzo da aggiungere."""
        if self.stack.get_visible_child_name() != "rete":
            self.rete_segue = False
            return False
        voce = getattr(self, "rete_voce", None)
        if firma_rete() != self.rete_firma and not (voce and (voce.has_focus() or voce.get_text().strip())):
            self.ricarica_rete()
        return True

    def rete_aggiorna(self, pulsante):
        pulsante.set_sensitive(False)
        pulsante.set_label(T("Cerco…", "Searching…"))

        def work():
            subprocess.run([sys.executable, RETE, "aggiorna"], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
            GLib.idle_add(lambda: self.ricarica_rete() and False)
        threading.Thread(target=work, daemon=True).start()

    def rete_cmd(self, *args):
        if args[-1].strip() == "":
            return

        def work():
            subprocess.run([sys.executable, RETE, *[a.strip() for a in args]], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL)
            GLib.timeout_add(2500, lambda: self.ricarica_rete() and False)   # il servizio aggiorna l'elenco in 2 s
        threading.Thread(target=work, daemon=True).start()

    def ricarica_rete(self):
        if self.stack.get_visible_child_name() == "rete":
            old = self.stack.get_child_by_name("rete")
            self.stack.remove(old)
            page = self.page("rete")
            self.stack.add_named(page, "rete")
            page.show_all()
            self.stack.set_visible_child_name("rete")
        return False

    def pane_batteria(self, box):
        b = (phone_state().get("battery") or {})
        if "percentage" not in b:
            self.group(box, None, [self.row(T("Stato della batteria non disponibile", "Battery status unavailable"),
                                            None, T("Serve l'app Termux:API.", "Termux:API is required."))])
            return
        p = int(b["percentage"])
        charging = b.get("status") in ("CHARGING", "FULL") or b.get("plugged") not in (None, "UNPLUGGED")
        top = Gtk.Box(spacing=16, margin=14)
        num = Gtk.Label(label=f"{p}%")
        num.get_style_context().add_class("big-num")
        top.pack_start(num, False, False, 0)
        lvl = Gtk.LevelBar(min_value=0, max_value=100, value=p, valign=Gtk.Align.CENTER, hexpand=True)
        top.pack_start(lvl, True, True, 0)
        g = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        g.get_style_context().add_class("group")
        g.pack_start(top, False, False, 0)
        box.pack_start(g, False, False, 0)
        plug = {"PLUGGED_AC": T("caricatore", "charger"), "PLUGGED_USB": "USB",
                "PLUGGED_WIRELESS": T("wireless", "wireless")}.get(b.get("plugged"), "")
        rows = [self.row(T("Stato", "Status"), self.value(
            (T("In carica", "Charging") + (f" ({plug})" if plug else "")) if charging else T("Con la batteria", "On battery")))]
        if b.get("temperature"):
            rows.append(self.row(T("Temperatura", "Temperature"), self.value(f"{float(b['temperature']):.0f} °C"),
                                 T("Sopra i 38 °C il telefono rallenta per non scaldarsi.",
                                   "Above 38 °C the phone slows down to cool off.")))
        if b.get("health"):
            rows.append(self.row(T("Condizioni", "Health"), self.value(
                {"GOOD": T("Buone", "Good"), "OVERHEAT": T("Surriscaldata", "Overheated")}.get(b["health"], b["health"].title()))))
        self.group(box, None, rows)
        self.group(box, None, [self.row(T("Opzioni batteria del telefono", "Phone battery options"),
                                        self.button(T("Apri", "Open"), lambda: am("android.intent.action.POWER_USAGE_SUMMARY")),
                                        T("Protezione della batteria, risparmio energetico…", "Battery protection, power saving…"))])

    def pane_aspetto(self, box):
        dark = conf_get("TEMA", "chiaro") == "scuro"
        modes = Gtk.Box(spacing=14, margin=14, halign=Gtk.Align.START)
        self.mode_btns = {}
        for key, label, colors in (("chiaro", T("Chiaro", "Light"), ("#f3f3f3", "#ffffff", "#1a1a1a")),
                                   ("scuro", T("Scuro", "Dark"), ("#202020", "#2b2b2b", "#f3f3f3"))):
            btn = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
            btn.get_style_context().add_class("choice")
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            v.pack_start(self.mode_preview(*colors), False, False, 0)
            v.pack_start(Gtk.Label(label=label), False, False, 0)
            btn.add(v)
            btn.connect("clicked", lambda _b, k=key: self.set_theme(k))
            modes.pack_start(btn, False, False, 0)
            self.mode_btns[key] = btn
        self.mark_theme("scuro" if dark else "chiaro")
        g = self.group(box, T("Tema", "Theme"), [modes])
        g.set_halign(Gtk.Align.FILL)

        # colore d'accento: pulsanti, selezioni, barra delle app, Start (tema DeX OS ridisegnato)
        cur = conf_get("ACCENTO", ACCENTS[0][1]).lower()
        dots = Gtk.Box(spacing=10, margin=14)
        self.accent_btns = []
        for name, hexv, label in ACCENTS:
            b = Gtk.Button(relief=Gtk.ReliefStyle.NONE, tooltip_text=label)
            da = Gtk.DrawingArea()
            da.set_size_request(30, 30)
            da.connect("draw", self.draw_dot, hexv, b)
            b.add(da)
            b.hexv = hexv
            b.selected = hexv.lower() == cur
            b.connect("clicked", lambda btn, n=name: self.set_accent(btn, n))
            dots.pack_start(b, False, False, 0)
            self.accent_btns.append(b)
        self.accent_label = Gtk.Label(label=next((l for _n, h, l in ACCENTS if h.lower() == cur), cur), xalign=0)
        self.accent_label.get_style_context().add_class("hint")
        dots.pack_start(self.accent_label, False, False, 8)
        self.group(box, T("Colore", "Accent color"), [dots])

        cur = self.wallpaper_current()
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4, min_children_per_line=2,
                           column_spacing=8, row_spacing=8, margin=12, homogeneous=True)
        self.thumbs = []
        for path in self.wallpapers():
            btn = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
            btn.get_style_context().add_class("thumb")
            img = Gtk.Image()
            img.set_size_request(150, 90)
            btn.add(img)
            btn.path = path
            if os.path.realpath(path) == os.path.realpath(cur or "-"):
                btn.get_style_context().add_class("selected")
            btn.connect("clicked", lambda b: self.set_wallpaper(b.path))
            flow.add(btn)
            self.thumbs.append(btn)
        self.group(box, T("Sfondo", "Wallpaper"), [flow])
        self.load_thumbs([(b.get_child(), b.path) for b in self.thumbs])
        self.group(box, None, [self.row(T("Usa una tua immagine", "Use your own picture"),
                                        self.button(T("Scegli…", "Choose…"), self.pick_wallpaper))])

    THUMBS = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache"), "tod/sfondi")

    def load_thumbs(self, items):
        """Anteprime degli sfondi: decodificare 20 e più foto grandi costa secondi, quindi si fa in
        sottofondo e si tiene una copia piccola in cache (si rifà se lo sfondo cambia)."""
        def thumb(path):
            st = os.stat(path)
            key = re.sub(r"[^A-Za-z0-9]", "_", path)[-120:] + f"-{int(st.st_mtime)}-{st.st_size}.png"
            cached = os.path.join(self.THUMBS, key)
            try:
                return GdkPixbuf.Pixbuf.new_from_file(cached)
            except GLib.Error:
                pass
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 150, 90, False)
            try:
                os.makedirs(self.THUMBS, exist_ok=True)
                pb.savev(cached + ".tmp", "png", [], [])
                os.replace(cached + ".tmp", cached)
            except (GLib.Error, OSError):
                pass
            return pb

        def work():
            for img, path in items:
                try:
                    pb = thumb(path)
                except (GLib.Error, OSError):
                    continue
                GLib.idle_add(img.set_from_pixbuf, pb)
        threading.Thread(target=work, daemon=True).start()

    def draw_dot(self, _w, cr, hexv, btn):
        r, g, b = (int(hexv[i:i + 2], 16) / 255 for i in (1, 3, 5))
        cr.set_source_rgb(r, g, b)
        cr.arc(15, 15, 11, 0, 6.2832)
        cr.fill()
        if btn.selected:   # anello come macOS sul colore scelto
            cr.set_line_width(2)
            cr.set_source_rgba(r, g, b, 0.55)
            cr.arc(15, 15, 14, 0, 6.2832)
            cr.stroke()
            cr.set_source_rgb(1, 1, 1)
            cr.arc(15, 15, 3.5, 0, 6.2832)
            cr.fill()

    def set_accent(self, btn, name):
        for b in self.accent_btns:
            b.selected = b is btn
            b.queue_draw()
        self.accent_label.set_text(next(l for n, _h, l in ACCENTS if n == name))
        tod("aspetto", "accento", name)

    def mode_preview(self, bg, card, fg):
        da = Gtk.DrawingArea()
        da.set_size_request(150, 90)

        def draw(_w, cr):
            def rgb(h):
                return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
            cr.set_source_rgb(*rgb(bg)); cr.rectangle(0, 0, 150, 90); cr.fill()
            cr.set_source_rgb(*rgb(card)); cr.rectangle(14, 14, 122, 50); cr.fill()
            cr.set_source_rgb(*rgb(fg)); cr.rectangle(24, 26, 60, 6); cr.fill(); cr.rectangle(24, 40, 90, 4); cr.fill()
            cr.set_source_rgb(0.0, 0.37, 0.72); cr.rectangle(0, 76, 150, 14); cr.fill()
        da.connect("draw", draw)
        return da

    def mark_theme(self, key):
        for k, b in self.mode_btns.items():
            (b.get_style_context().add_class if k == key else b.get_style_context().remove_class)("selected")

    def set_theme(self, key):
        self.mark_theme(key)
        tod("aspetto", "tema", key)

    SFONDI = os.path.join(HOME, ".local/share/backgrounds/dexos")
    FAMIGLIE = ("fiore", "seta", "paesaggio", "vetro")

    def wallpapers(self):
        """le quattro famiglie DeX OS nella variante del tema (chiara o scura), più l'immagine scelta dall'utente"""
        v = "scuro" if conf_get("TEMA", "chiaro") == "scuro" else "chiaro"
        out = []
        for fam in self.FAMIGLIE:
            for ext in ("jpg", "png"):
                p = os.path.join(self.SFONDI, f"{fam}-{v}.{ext}")
                if os.path.isfile(p):
                    out.append(p)
                    break
        mio = conf_get("SFONDO", "fiore")
        if mio.startswith("/") and os.path.isfile(mio):
            out.insert(0, mio)
        return out

    def wallpaper_current(self):
        mio = conf_get("SFONDO", "fiore")
        if mio.startswith("/"):
            return mio
        v = "scuro" if conf_get("TEMA", "chiaro") == "scuro" else "chiaro"
        for ext in ("jpg", "png"):
            p = os.path.join(self.SFONDI, f"{mio}-{v}.{ext}")
            if os.path.isfile(p):
                return p
        return ""

    def set_wallpaper(self, path):
        for b in self.thumbs:
            (b.get_style_context().add_class if b.path == path else b.get_style_context().remove_class)("selected")
        nome = os.path.basename(path).rsplit("-", 1)[0]
        if os.path.dirname(path) == self.SFONDI and nome in self.FAMIGLIE:
            tod("aspetto", "sfondo", nome)   # una famiglia: segue tema chiaro/scuro e accento
        else:
            tod("aspetto", "sfondo", path)

    def pick_wallpaper(self):
        d = Gtk.FileChooserDialog(title=T("Scegli un'immagine", "Choose a picture"), transient_for=self,
                                  action=Gtk.FileChooserAction.OPEN)
        d.add_buttons(T("Annulla", "Cancel"), Gtk.ResponseType.CANCEL, T("Usa come sfondo", "Set as wallpaper"), Gtk.ResponseType.OK)
        f = Gtk.FileFilter()
        f.set_name(T("Immagini", "Pictures"))
        f.add_mime_type("image/*")
        d.add_filter(f)
        pics = os.path.join(os.environ.get("HOME", HOME), T("Immagini", "Pictures"))
        if os.path.isdir(pics):
            d.set_current_folder(pics)
        if d.run() == Gtk.ResponseType.OK and d.get_filename():
            self.set_wallpaper(d.get_filename())
        d.destroy()

    def pane_schermo(self, box):
        dpi = conf_get("DPI", "auto")
        cur = 0 if dpi == "auto" else int(dpi) if dpi.isdigit() else 0
        res = ""
        for line in sh(["xrandr", "--current"]).splitlines():
            m = re.search(r"current (\d+) x (\d+)", line)
            if m:
                res = f"{m.group(1)} × {m.group(2)}"
                break
        hb = Gtk.Box(spacing=0, homogeneous=True, margin=14)
        group = None
        for label, val in SIZES:
            rb = Gtk.RadioButton.new_with_label_from_widget(group, label)
            rb.set_mode(False)   # come pulsanti uniti
            group = group or rb
            rb.set_active(val == cur or (val == 0 and cur not in [v for _, v in SIZES]))
            rb.connect("toggled", lambda b, v=val: b.get_active() and self.set_size(v))
            hb.pack_start(rb, True, True, 0)
        hb.get_style_context().add_class("linked")
        self.group(box, T("Dimensioni di testo e oggetti", "Text and item size"), [hb])
        hint = Gtk.Label(label=T("Predefinito sceglie da solo la misura giusta per lo schermo collegato.",
                                 "Default picks the right size for the connected screen."), xalign=0, wrap=True)
        hint.get_style_context().add_class("hint")
        box.pack_start(hint, False, False, 0)
        self.group(box, None, [self.row(T("Risoluzione", "Resolution"), self.value(res or "—"),
                                        T("La decide il telefono (DeX o Termux:X11).", "Set by the phone (DeX or Termux:X11)."))])

    # --- barra delle applicazioni (share/tod-barra.py, ~/.config/tod/barra.json) ---
    def barra_conf(self):
        try:
            c = json.load(open(BARRA))
        except (OSError, ValueError):
            c = {}
        c.setdefault("fissate", [])
        c.setdefault("allineamento", "centro")
        c.setdefault("grandezza", "media")
        c.setdefault("anteprime", True)
        return c

    def barra_salva(self, c):
        os.makedirs(os.path.dirname(BARRA), exist_ok=True)
        with open(BARRA + ".tmp", "w") as f:
            json.dump(c, f, indent=1)
        os.replace(BARRA + ".tmp", BARRA)
        try:   # la barra in esecuzione rilegge (SIGUSR1)
            pid = int(open(os.path.join(os.environ.get("TMPDIR", "/tmp"), "tod-barra.pid")).read().strip())
            if b"tod-barra.py" in open(f"/proc/{pid}/cmdline", "rb").read():
                os.kill(pid, signal.SIGUSR1)
        except (OSError, ValueError):
            pass

    def barra_scelta(self, opzioni, attuale, chiave):
        hb = Gtk.Box(spacing=0, homogeneous=True, margin=14)
        group = None
        for label, val in opzioni:
            rb = Gtk.RadioButton.new_with_label_from_widget(group, label)
            rb.set_mode(False)
            group = group or rb
            rb.set_active(val == attuale)
            rb.connect("toggled", lambda b, v=val: b.get_active() and self.barra_imposta(chiave, v))
            hb.pack_start(rb, True, True, 0)
        hb.get_style_context().add_class("linked")
        return hb

    def barra_imposta(self, chiave, val):
        c = self.barra_conf()
        c[chiave] = val
        self.barra_salva(c)

    def pane_barra(self, box):
        c = self.barra_conf()
        self.group(box, T("Posizione delle app", "App alignment"), [self.barra_scelta(
            [(T("Al centro", "Center"), "centro"), (T("A sinistra", "Left"), "sinistra")], c["allineamento"], "allineamento")])
        self.group(box, T("Grandezza", "Size"), [self.barra_scelta(
            [(T("Piccola", "Small"), "piccola"), (T("Media", "Medium"), "media"), (T("Grande", "Large"), "grande")],
            c["grandezza"], "grandezza")])
        self.group(box, None, [self.row(T("Anteprime delle finestre", "Window previews"),
                                        self.switch(c["anteprime"], lambda v: self.barra_imposta("anteprime", v)),
                                        T("Passando sopra un'app aperta compaiono le sue finestre.",
                                          "Hovering an open app shows its windows."))])
        righe = []
        for ident in c["fissate"]:
            try:
                info = DesktopAppInfo.new(ident)
            except TypeError:
                info = None
            if info is None:
                continue
            r = Gtk.Box(spacing=12)
            r.get_style_context().add_class("group-row")
            img = Gtk.Image.new_from_gicon(info.get_icon(), Gtk.IconSize.LARGE_TOOLBAR) if info.get_icon() else Gtk.Image()
            img.set_pixel_size(24)
            r.pack_start(img, False, False, 0)
            r.pack_start(Gtk.Label(label=info.get_display_name() or ident, xalign=0), True, True, 0)
            b = Gtk.Button(label=T("Rimuovi", "Unpin"))
            b.connect("clicked", lambda _b, i=ident, riga=r: self.barra_togli(i, riga))
            r.pack_end(b, False, False, 0)
            righe.append(r)
        if righe:
            self.group(box, T("App fissate", "Pinned apps"), righe)
        hint = Gtk.Label(label=T("Per cambiare l'ordine tieni premuta un'icona nella barra e trascinala. Con il tasto destro "
                                 "su un'app la fissi o la togli; puoi anche trascinare un'app dallo Start alla barra.",
                                 "Drag icons on the taskbar to reorder them. Right-click an app to pin or unpin it; you can "
                                 "also drag an app from Start to the taskbar."), xalign=0, wrap=True)
        hint.get_style_context().add_class("hint")
        box.pack_start(hint, False, False, 0)

    def barra_togli(self, ident, riga):
        c = self.barra_conf()
        c["fissate"] = [x for x in c["fissate"] if x != ident]
        self.barra_salva(c)
        riga.get_parent().remove(riga) if riga.get_parent() else None

    def set_size(self, val):
        tod("display", "auto" if val == 0 else str(val))

    def pane_suono(self, box):
        tv = None   # il volume del telefono (share/tod_volume.py): uno solo, come su un PC
        try:
            sys.path.insert(1, os.path.dirname(os.path.abspath(__file__)))
            import tod_volume
            if tod_volume.telefono() and (tod_volume.stato() or tod_volume.leggi_telefono(forza=True)) is not None:
                tv = tod_volume
        except ImportError:
            pass
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        scale.set_size_request(260, -1)
        scale.set_draw_value(False)
        if tv is not None:
            level, muted = tv.stato()
            scale.set_value(level)
            scale.connect("value-changed", lambda s: tv.metti(int(s.get_value())))

            def silenzio(on):
                tv.muto(on)
                scale.set_value((tv.stato() or (0, True))[0])
        else:
            vol = sh(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
            m = re.search(r"(\d+)%", vol)
            level = int(m.group(1)) if m else 100
            mute = sh(["pactl", "get-sink-mute", "@DEFAULT_SINK@"]).lower()
            muted = mute.endswith(("yes", "sì", "si"))
            scale.set_value(level)
            scale.connect("value-changed",
                          lambda s: sh(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{int(s.get_value())}%"]))

            def silenzio(on):
                sh(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if on else "0"])
        rows = [self.row(T("Volume", "Volume"), scale),
                self.row(T("Silenzioso", "Mute"), self.switch(muted, silenzio))]
        sinks = [l.split("\t") for l in sh(["pactl", "list", "sinks", "short"]).splitlines() if "\t" in l]
        if sinks:
            out = T("Altoparlanti del telefono o DeX", "Phone or DeX speakers") if re.search("opensl|sles|aaudio", sinks[0][1].lower()) else sinks[0][1]
            rows.append(self.row(T("Uscita", "Output"), self.value(out),
                                 T("Cuffie e altoparlanti Bluetooth li sceglie il telefono.", "Bluetooth devices are chosen by the phone.")))
        self.group(box, None, rows)
        if shutil.which("pavucontrol"):
            self.group(box, None, [self.row(T("Regolazioni per ogni app", "Per-app volume"),
                                            self.button(T("Apri", "Open"), lambda: spawn(["pavucontrol"])))])

    def pane_notifiche(self, box):
        dnd = xq("xfce4-notifyd", "/do-not-disturb") == "true"
        phone_off = os.path.exists(os.path.join(HOME, ".config/tod/notifiche-telefono-spente"))
        self.group(box, None, [
            self.row(T("Non disturbare", "Do not disturb"),
                     self.switch(dnd, lambda on: xq("xfce4-notifyd", "/do-not-disturb", "-t", "bool", "-s", "true" if on else "false")),
                     T("Le notifiche arrivano lo stesso nel centro notifiche, ma senza comparire.",
                       "Notifications still reach the notification center, silently.")),
            self.row(T("Notifiche del telefono", "Phone notifications"),
                     self.switch(not phone_off, lambda on: tod("telefono", "notifiche", "accese" if on else "spente")),
                     T("Messaggi e avvisi del telefono anche sul desktop.", "Phone messages and alerts on the desktop too."))])
        self.group(box, None, [self.row(T("Posizione e durata degli avvisi", "Popup position and duration"),
                                        self.button(T("Apri", "Open"), lambda: spawn([os.path.join(PREFIX, "bin/xfce4-notifyd-config")])))])

    def pane_telefono(self, box):
        apps_on = conf_get("ANDROID_APPS", "1") != "0"
        api = os.path.exists(os.path.join(PREFIX, "bin/termux-battery-status"))
        self.group(box, None, [
            self.row(T("App del telefono nello Start", "Phone apps in Start"),
                     self.switch(apps_on, lambda on: tod("telefono", "acceso" if on else "spento")),
                     T("Le app di Android si aprono dal menu Start come le altre.", "Android apps open from Start like the others.")),
            self.row(T("Aggiorna l'elenco delle app", "Refresh the app list"),
                     self.button(T("Aggiorna", "Refresh"), lambda: tod("telefono", "aggiorna")),
                     T("Di solito si aggiorna da solo a ogni avvio.", "Usually refreshed at every start.")),
            self.row(T("Batteria, rete e notifiche", "Battery, network and notifications"),
                     self.value(T("Attive", "Active") if api else T("Serve Termux:API", "Termux:API needed")))])

    # --- Tastiera e Mouse (13 novembre, Pietro: "i pannelli di tastiera e mouse sono la cosa che mi dà più fastidio di
    # tutti"): niente più finestre di XFCE. I valori restano quelli di XFCE (xfconf: keyboards, pointers, xsettings,
    # xfce4-keyboard-shortcuts), che xfsettingsd applica subito. Con Termux:X11: la ripetizione dei tasti la fa il server X
    # (Termux:X11 ignora quella di Android: InputEventSender.java), quindi velocità e ritardo funzionano; il puntatore
    # invece arriva già posizionato dal telefono (pointerCapture spento), quindi la sua velocità la decide DeX.
    def piu_tardi(self, chiave, fatto, ms=250):
        """un cursore scrive solo quando ci si ferma (non a ogni passo)"""
        if not hasattr(self, "_tardi"):
            self._tardi = {}
        if chiave in self._tardi:
            GLib.source_remove(self._tardi[chiave])

        def ora():
            self._tardi.pop(chiave, None)
            fatto()
            return False
        self._tardi[chiave] = GLib.timeout_add(ms, ora)

    def cursore(self, chiave, valore, minimo, massimo, sinistra, destra, cambia, inverti=False):
        """un cursore come quelli di macOS, con le parole agli estremi (inverti: a sinistra il valore più alto)"""
        adj = Gtk.Adjustment(value=valore, lower=minimo, upper=massimo, step_increment=1, page_increment=10)
        sc = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj, draw_value=False)
        sc.set_size_request(240, -1)
        sc.set_inverted(inverti)
        sc.add_mark(massimo if inverti else minimo, Gtk.PositionType.BOTTOM, sinistra)
        sc.add_mark(minimo if inverti else massimo, Gtk.PositionType.BOTTOM, destra)
        sc.connect("value-changed", lambda w: self.piu_tardi(chiave, lambda: cambia(int(round(w.get_value())))))
        return sc

    def pane_tastiera(self, box):
        self.kb_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.pack_start(self.kb_box, False, False, 0)
        self.kb_scorciatoie = False
        self.kb_mostra()

    def kb_mostra(self):
        for f in self.kb_box.get_children():
            self.kb_box.remove(f)
            f.destroy()
        if self.kb_scorciatoie:
            self.kb_pagina_scorciatoie()
        else:
            self.kb_pagina_tastiera()
        senza_rotella(self.kb_box)
        self.kb_box.show_all()
        sw = self.kb_box.get_ancestor(Gtk.ScrolledWindow)
        if sw is not None:
            sw.get_vadjustment().set_value(0)

    def kb_pagina_tastiera(self):
        b = self.kb_box
        telefono = conf_get("TASTIERA_DA", "telefono") != "desktop"
        self.kb_lingue(b, telefono)
        rip = xf_valore("keyboards", "/Default/KeyRepeat", True)
        vel = self.cursore("rate", xf_valore("keyboards", "/Default/KeyRepeat/Rate", 20), 2, 60,
                           T("Lenta", "Slow"), T("Veloce", "Fast"),
                           lambda v: xf_scrivi("keyboards", "/Default/KeyRepeat/Rate", v))
        rit = self.cursore("delay", xf_valore("keyboards", "/Default/KeyRepeat/Delay", 500), 150, 1500,
                           T("Lungo", "Long"), T("Breve", "Short"),
                           lambda v: xf_scrivi("keyboards", "/Default/KeyRepeat/Delay", v), inverti=True)

        def ripeti(acceso):
            xf_scrivi("keyboards", "/Default/KeyRepeat", acceso)
            vel.set_sensitive(acceso)
            rit.set_sensitive(acceso)
        vel.set_sensitive(rip)
        rit.set_sensitive(rip)
        prova = Gtk.Entry(placeholder_text=T("Tieni premuto un tasto per provare", "Hold a key down to try it"))
        prova.set_size_request(260, -1)
        self.group(b, T("Ripetizione", "Repeat"), [
            self.row(T("Ripetizione dei tasti", "Key repeat"), self.switch(rip, ripeti),
                     T("Tenendo premuto un tasto, il carattere si ripete.", "Holding a key down repeats the character.")
                     if not telefono else
                     T("Con il layout del telefono lettere e numeri si ripetono alla velocità del telefono; "
                       "qui si regolano frecce, Canc e combinazioni.",
                       "With the phone's layout, letters and numbers repeat at the phone's speed; "
                       "here you set arrows, Delete and combinations.")),
            self.row(T("Velocità di ripetizione", "Key repeat rate"), vel),
            self.row(T("Ritardo prima della ripetizione", "Delay until repeat"), rit),
            self.row(T("Prova", "Try it"), prova)])
        self.group(b, None, [
            self.row(T("Bloc Num acceso all'avvio", "Num Lock on at startup"),
                     self.switch(xf_valore("keyboards", "/Default/Numlock", False),
                                 lambda a: xf_scrivi("keyboards", "/Default/Numlock", a))),
            self.row(T("Cursore del testo lampeggiante", "Blinking text cursor"),
                     self.switch(xf_valore("xsettings", "/Net/CursorBlink", True),
                                 lambda a: xf_scrivi("xsettings", "/Net/CursorBlink", a)))])
        self.group(b, T("Scorciatoie", "Shortcuts"), [
            self.row(T("Scorciatoie da tastiera", "Keyboard shortcuts"),
                     self.button(T("Modifica…", "Edit…"), lambda: (setattr(self, "kb_scorciatoie", True), self.kb_mostra())),
                     T("Le combinazioni di tasti per lo Start, le schermate, le app e le finestre.",
                       "Key combinations for Start, screenshots, apps and windows."))])
        b.pack_start(self.ripristina(
            T("Ripristinare la tastiera?", "Restore keyboard defaults?"),
            T("Ripetizione dei tasti, velocità, ritardo, Bloc Num e cursore lampeggiante tornano come all'inizio. "
              "Lingue, layout e scorciatoie restano come sono.",
              "Key repeat, rate, delay, Num Lock and the blinking cursor go back to how they were at the start. "
              "Languages, layouts and shortcuts stay as they are."),
            self.kb_predefiniti), False, False, 0)

    def kb_predefiniti(self):
        for canale, prop, valore in TASTIERA_PREDEFINITI:
            xf_scrivi(canale, prop, valore)
        self.kb_mostra()

    def kb_layout_voci(self):
        import tastiera_dati as K
        if not hasattr(self, "_kb_layout"):
            self._kb_layout = K.carica()
        return self._kb_layout

    def kb_sorgenti(self):
        """i layout scelti [(codice, variante)]: quelli salvati, sennò quello della lingua del desktop"""
        import tastiera_dati as K
        lista = K.scomponi(conf_get("TASTIERA_LAYOUT"), conf_get("TASTIERA_VARIANTI"))
        return lista or [(K.predefinito(conf_get("LINGUA", "it")), "")]

    def kb_lingue(self, b, telefono):
        """come Sorgenti di input di macOS. Con il layout del telefono le lettere arrivano già fatte da Android
        (Termux:X11): qui solo il rimando; spento, si scelgono qui fino a 4 layout (lib/x11.sh: tod tastiera)."""
        import tastiera_dati as K
        righe = [self.row(T("Usa il layout del telefono", "Use the phone's layout"), self.switch(telefono, self.kb_da_telefono),
                          T("Acceso: le lettere seguono la tastiera scelta in Android. Spento: scegli qui lingue e layout, "
                            "anche più di uno.",
                            "On: letters follow the keyboard chosen in Android. Off: choose languages and layouts here, "
                            "even more than one."))]
        if telefono:
            righe.append(self.row(T("Layout della tastiera fisica", "Physical keyboard layout"),
                                  self.button(T("Impostazioni del telefono", "Phone settings"),
                                              lambda: am("android.settings.HARD_KEYBOARD_SETTINGS")),
                                  T("Su Samsung: Gestione generale → Tastiera fisica.",
                                    "On Samsung: General management → Physical keyboard.")))
        else:
            voci = self.kb_layout_voci()
            lista = self.kb_sorgenti()
            for i, (c, v) in enumerate(lista):
                w = Gtk.Box(spacing=10)
                sig = Gtk.Label(label=K.sigla(c))
                sig.get_style_context().add_class("kb-sigla")
                w.pack_start(sig, False, False, 0)
                if len(lista) > 1:
                    w.pack_start(self.button(T("Rimuovi", "Remove"), lambda i=i: self.kb_salva(
                        [x for j, x in enumerate(self.kb_sorgenti()) if j != i])), False, False, 0)
                righe.append(self.row(K.nome_di(c, v, voci, ITALIAN), w,
                                      T("All'avvio", "At startup") if i == 0 and len(lista) > 1 else None))
            if len(lista) < K.MASSIMO:
                righe.append(self.row(T("Altre lingue e layout", "More languages and layouts"),
                                      self.button(T("Aggiungi…", "Add…"), self.kb_aggiungi)))
            if len(lista) > 1:
                cambio = Gtk.ComboBoxText()
                for opz, it, en in K.CAMBI:
                    cambio.append(opz, it if ITALIAN else en)
                if not cambio.set_active_id(conf_get("TASTIERA_CAMBIO", K.CAMBIO_PREDEFINITO)):
                    cambio.set_active_id(K.CAMBIO_PREDEFINITO)
                cambio.connect("changed", lambda c: self.kb_salva(self.kb_sorgenti(), c.get_active_id()))
                righe.append(self.row(T("Passa al layout successivo", "Switch to the next layout"), cambio,
                                      T("La lingua attiva si vede nella barra, accanto all'ora.",
                                        "The active language shows in the taskbar, next to the clock.")))
        self.group(b, T("Lingue e layout", "Languages and layouts"), righe)

    def kb_da_telefono(self, acceso):
        if acceso:
            tod("tastiera", "telefono", then=self.kb_mostra)
        else:
            self.kb_salva(self.kb_sorgenti())

    def kb_salva(self, lista, cambio=None):
        import tastiera_dati as K
        lay, var = K.componi(lista)
        if not lay:
            return
        tod("tastiera", "desktop", lay, var, cambio or conf_get("TASTIERA_CAMBIO", K.CAMBIO_PREDEFINITO),
            then=self.kb_mostra)

    def kb_aggiungi(self):
        """la finestrella per aggiungere una lingua o un layout: cerca, scegli, Aggiungi"""
        import tastiera_dati as K
        voci = self.kb_layout_voci()
        scelti = set(self.kb_sorgenti())
        d = Gtk.Dialog(transient_for=self, modal=True, title=T("Aggiungi una lingua", "Add a language"))
        d.add_button(T("Annulla", "Cancel"), Gtk.ResponseType.CANCEL)
        ok = d.add_button(T("Aggiungi", "Add"), Gtk.ResponseType.OK)
        ok.set_sensitive(False)
        d.set_default_response(Gtk.ResponseType.OK)
        area = d.get_content_area()
        area.set_spacing(10)
        area.pack_start(tod_avviso.testa("input-keyboard", T("Aggiungi una lingua o un layout", "Add a language or layout"),
                                         T("Cerca per lingua o paese, per esempio «inglese» o «svizzera».",
                                           "Search by language or country, for example “english” or “swiss”."),
                                         larghezza=380), False, False, 0)
        cerca = Gtk.SearchEntry(placeholder_text=T("Cerca", "Search"))
        area.pack_start(cerca, False, False, 0)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_size_request(380, 300)
        elenco = Gtk.ListBox(activate_on_single_click=False)
        sw.add(elenco)
        area.pack_start(sw, True, True, 0)

        def riempi(*_):
            for r in elenco.get_children():
                elenco.remove(r)
            for v in K.cerca(cerca.get_text(), voci, ITALIAN)[:200]:
                if v.chiave() in scelti:
                    continue
                r = Gtk.ListBoxRow()
                r.voce = v
                lab = Gtk.Label(label=K.nome(v, ITALIAN), xalign=0, margin=6, ellipsize=3)
                if v.variante:
                    lab.set_margin_start(22)
                r.add(lab)
                elenco.add(r)
            elenco.show_all()
            ok.set_sensitive(False)
        cerca.connect("search-changed", riempi)
        elenco.connect("row-selected", lambda _l, r: ok.set_sensitive(r is not None))
        elenco.connect("row-activated", lambda *_: d.response(Gtk.ResponseType.OK))
        riempi()
        tod_avviso.come_macos(d, libero=True)
        d.show_all()
        r = d.run()
        riga = elenco.get_selected_row()
        d.destroy()
        if r == Gtk.ResponseType.OK and riga is not None:
            self.kb_salva(self.kb_sorgenti() + [riga.voce.chiave()])

    def kb_voci(self):
        import scorciatoie_dati as SD
        return SD.leggi(sh(["xfconf-query", "-c", "xfce4-keyboard-shortcuts", "-lv"]))

    def kb_pagina_scorciatoie(self):
        import scorciatoie_dati as SD
        b = self.kb_box
        voci = self.kb_voci()
        indietro = Gtk.Button()
        indietro.set_relief(Gtk.ReliefStyle.NONE)
        indietro.get_style_context().add_class("arch-indietro")
        h = Gtk.Box(spacing=4)
        h.pack_start(Gtk.Image.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.MENU), False, False, 0)
        h.pack_start(Gtk.Label(label=T("Tastiera", "Keyboard")), False, False, 0)
        indietro.add(h)
        indietro.set_halign(Gtk.Align.START)
        indietro.connect("clicked", lambda *_: (setattr(self, "kb_scorciatoie", False), self.kb_mostra()))
        b.pack_start(indietro, False, False, 0)
        t = Gtk.Label(label=T("Scorciatoie da tastiera", "Keyboard shortcuts"), xalign=0)
        t.get_style_context().add_class("arch-uso")
        b.pack_start(t, False, False, 0)
        nota = Gtk.Label(label=T("Fai clic su «Cambia» e premi la nuova combinazione: vale subito. "
                                 "Win è il tasto con il logo (in DeX anche il tasto Samsung).",
                                 "Click “Change” and press the new combination: it applies right away. "
                                 "Win is the logo key (in DeX also the Samsung key)."), xalign=0, wrap=True)
        nota.get_style_context().add_class("hint")
        b.pack_start(nota, False, False, 0)
        for g, it, en in SD.GRUPPI:
            righe = []
            for a in SD.AZIONI:
                if a[0] != g:
                    continue
                combos = SD.di(a, voci)
                w = Gtk.Box(spacing=8)
                # al massimo due combinazioni, sennò la finestra si allarga (le altre restano e funzionano)
                nomi = [SD.etichetta(k, ITALIAN) for _p, k in combos]
                lab = Gtk.Label(label="   ".join(nomi[:2]) + ("   …" if len(nomi) > 2 else "") or T("Nessuna", "None"))
                lab.set_tooltip_text("\n".join(nomi) if len(nomi) > 2 else None)
                lab.get_style_context().add_class("dim-label")
                w.pack_start(lab, False, False, 0)
                w.pack_start(self.button(T("Cambia…", "Change…"), lambda a=a: self.kb_registra(a)), False, False, 0)
                righe.append(self.row(a[2] if ITALIAN else a[3], w))
            self.group(b, it if ITALIAN else en, righe)

    def kb_registra(self, a):
        """la finestrella che registra la nuova combinazione. Si prende tutta la tastiera (come fa XFCE): sennò un
        tasto già usato (Stamp, Win+Maiusc+4…) farebbe partire la sua azione invece di essere registrato."""
        nome = a[2] if ITALIAN else a[3]
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.INFO,
                              buttons=Gtk.ButtonsType.NONE, text=T(f"Nuova combinazione per «{nome}»", f"New shortcut for “{nome}”"))
        d.format_secondary_text(T("Premi i tasti insieme. Esc annulla, ⌫ toglie la scorciatoia.",
                                  "Press the keys together. Esc cancels, ⌫ removes the shortcut."))
        d.add_button(T("Annulla", "Cancel"), Gtk.ResponseType.CANCEL)
        tod_avviso.come_macos(d)
        preso = {}
        mod = Gdk.ModifierType
        modificatori = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Super_L", "Super_R",
                        "Meta_L", "Meta_R", "ISO_Level3_Shift", "Hyper_L", "Hyper_R"}

        def premuto(_w, ev):
            k = Gdk.keyval_name(ev.keyval) or ""
            m = ev.state & (mod.CONTROL_MASK | mod.SHIFT_MASK | mod.MOD1_MASK | mod.SUPER_MASK | mod.MOD4_MASK)
            if k == "Escape" and not m:
                d.response(Gtk.ResponseType.CANCEL)
            elif k == "BackSpace" and not m:
                preso["tasti"] = ""
                d.response(Gtk.ResponseType.OK)
            elif k in modificatori:
                preso["mod"] = k
            else:
                s = ("<Primary>" if m & mod.CONTROL_MASK else "") + ("<Alt>" if m & mod.MOD1_MASK else "") + \
                    ("<Shift>" if m & mod.SHIFT_MASK else "") + ("<Super>" if m & (mod.SUPER_MASK | mod.MOD4_MASK) else "")
                preso["tasti"] = s + k
                d.response(Gtk.ResponseType.OK)
            return True

        def lasciato(_w, ev):
            k = Gdk.keyval_name(ev.keyval) or ""
            if k in ("Super_L", "Super_R") and preso.get("mod") == k and "tasti" not in preso:
                preso["tasti"] = k   # Win da solo (lo Start)
                d.response(Gtk.ResponseType.OK)
            return True
        d.connect("key-press-event", premuto)
        d.connect("key-release-event", lasciato)
        sede = Gdk.Display.get_default().get_default_seat()

        def prendi(*_):
            sede.grab(d.get_window(), Gdk.SeatCapabilities.KEYBOARD, False, None, None, None, None)
            return False
        d.connect("map-event", prendi)
        r = d.run()
        sede.ungrab()
        d.destroy()
        if r == Gtk.ResponseType.OK and "tasti" in preso:
            self.kb_applica(a, preso["tasti"])

    def kb_applica(self, a, tasti):
        import scorciatoie_dati as SD
        voci = self.kb_voci()
        if tasti:
            altra, p = SD.chi_usa(tasti, voci, salvo=a)
            if p:
                chi = (altra[2] if ITALIAN else altra[3]) if altra else T("un'altra funzione", "another function")
                d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                      buttons=Gtk.ButtonsType.NONE,
                                      text=T(f"«{SD.etichetta(tasti, True)}» è già usata", f"“{SD.etichetta(tasti, False)}” is already used"))
                d.format_secondary_text(T(f"La usa «{chi}». Spostarla qui?", f"“{chi}” uses it. Move it here?"))
                d.add_button(T("Annulla", "Cancel"), Gtk.ResponseType.CANCEL)
                d.add_button(T("Sposta", "Move"), Gtk.ResponseType.OK)
                d.set_default_response(Gtk.ResponseType.OK)
                tod_avviso.come_macos(d)
                r = d.run()
                d.destroy()
                if r != Gtk.ResponseType.OK:
                    return
                sh(["xfconf-query", "-c", "xfce4-keyboard-shortcuts", "-p", p, "-r", "-R"])
        valore = SD.comando_di(a, voci) if a[4] == "cmd" else a[1]
        for p in SD.tutte_di(a, voci):
            sh(["xfconf-query", "-c", "xfce4-keyboard-shortcuts", "-p", p, "-r", "-R"])
        if tasti:
            sh(["xfconf-query", "-c", "xfce4-keyboard-shortcuts", "-p", SD.RADICE[a[4]] + tasti, "-n", "-t", "string",
                "-s", valore])
        self.kb_mostra()

    def ms_anteprima(self, tema, misura):
        """le forme del puntatore (freccia, mano, testo, attesa) lette dai file Xcursor veri"""
        import puntatore_dati as P
        riga = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        riga.set_size_request(150, 56)
        for forma in P.FORME:
            p = P.file_forma(tema, forma)
            img = None
            try:
                with open(p, "rb") as fh:
                    img = P.leggi(fh.read(), misura) if p else None
            except (OSError, TypeError):
                img = None
            if img:
                w, h, _x, _y, rgba = img
                pb = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(rgba), GdkPixbuf.Colorspace.RGB, True, 8, w, h, 4 * w)
                riga.pack_start(Gtk.Image.new_from_pixbuf(pb), False, False, 0)
        return riga

    def ms_stile(self, box):
        import puntatore_dati as P
        stili = P.stili(conf_get("DEXOS_NAMES"))
        dpi = xf_valore("xsettings", "/Xft/DPI", 96)
        if stili:
            cur = conf_get("PUNTATORE", "scuro")
            scelte = Gtk.Box(spacing=14, margin=14, halign=Gtk.Align.START)
            self.ms_btns = {}
            for chiave, tema in stili:
                btn = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
                btn.get_style_context().add_class("choice")
                v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                v.pack_start(self.ms_anteprima(tema, P.misura_px(100, dpi)), False, False, 0)
                v.pack_start(Gtk.Label(label=T("Scuro", "Dark") if chiave == "scuro" else T("Chiaro", "Light")),
                             False, False, 0)
                btn.add(v)
                btn.connect("clicked", lambda _b, k=chiave: self.ms_scegli(k))
                scelte.pack_start(btn, False, False, 0)
                self.ms_btns[chiave] = btn
            self.ms_segna(cur)
            g = self.group(box, T("Stile del puntatore", "Pointer style"), [scelte])
            g.set_halign(Gtk.Align.FILL)
        dim = self.cursore("puntatore", int(conf_get("PUNTATORE_DIM", "100") or 100), P.DIMENSIONI[0], P.DIMENSIONI[1],
                           T("Normale", "Normal"), T("Grande", "Large"),
                           lambda v: tod("aspetto", "puntatore", "dimensione", str(v)))
        self.group(box, None, [
            self.row(T("Dimensione del puntatore", "Pointer size"), dim,
                     T("Il colore segue quello d'accento scelto in Aspetto.",
                       "The color follows the accent chosen in Appearance."))])

    def ms_segna(self, chiave):
        for k, b in self.ms_btns.items():
            (b.get_style_context().add_class if k == chiave else b.get_style_context().remove_class)("selected")

    def ms_scegli(self, chiave):
        self.ms_segna(chiave)
        tod("aspetto", "puntatore", chiave)

    def pane_mouse(self, box):
        elenco = sh(["xfconf-query", "-c", "pointers", "-l"])
        dispositivi = sorted({l.split("/")[1] for l in elenco.splitlines() if l.count("/") >= 2}) or ["Lorie_mouse"]
        d0 = dispositivi[0]

        def per_tutti(prop, valore):
            for d in dispositivi:
                xf_scrivi("pointers", f"/{d}/{prop}", valore)
        tasto = Gtk.ComboBoxText()
        tasto.append("sinistro", T("Sinistro", "Left"))
        tasto.append("destro", T("Destro", "Right"))
        tasto.set_active_id("sinistro" if xf_valore("pointers", f"/{d0}/RightHanded", True) else "destro")
        tasto.connect("changed", lambda c: per_tutti("RightHanded", c.get_active_id() == "sinistro"))
        dc = self.cursore("doppio", xf_valore("xsettings", "/Net/DoubleClickTime", 400), 200, 900,
                          T("Lenta", "Slow"), T("Veloce", "Fast"),
                          lambda v: xf_scrivi("xsettings", "/Net/DoubleClickTime", v), inverti=True)
        prova = Gtk.Button(label=T("Fai doppio clic qui", "Double-click here"))

        def clic(w, ev):
            if ev.type == Gdk.EventType._2BUTTON_PRESS:
                w.set_label(T("Doppio clic riconosciuto ✓", "Double-click recognized ✓"))
                GLib.timeout_add(1500, lambda: (w.set_label(T("Fai doppio clic qui", "Double-click here")), False)[1])
            return False
        prova.connect("button-press-event", clic)
        self.group(box, None, [
            self.row(T("Tasto principale", "Primary button"), tasto,
                     T("Quello con cui fai clic e selezioni (destro per chi usa la sinistra).",
                       "The one you click and select with (right for left-handed use).")),
            self.row(T("Scorrimento naturale", "Natural scrolling"),
                     self.switch(xf_valore("pointers", f"/{d0}/ReverseScrolling", False),
                                 lambda a: per_tutti("ReverseScrolling", a)),
                     T("Il contenuto segue il movimento delle dita, come sul telefono.",
                       "Content follows your fingers, like on the phone.")),
            self.row(T("Velocità del doppio clic", "Double-click speed"), dc),
            self.row(T("Prova", "Try it"), prova)])
        self.ms_stile(box)
        self.group(box, None, [
            self.row(T("Velocità del puntatore", "Pointer speed"),
                     self.button(T("Impostazioni del telefono", "Phone settings"), lambda: am("android.settings.SETTINGS")),
                     T("In DeX il puntatore lo muove il telefono: la velocità si cambia nelle impostazioni del telefono, "
                       "in Samsung DeX → Mouse e trackpad.",
                       "In DeX the phone moves the pointer: change its speed in the phone's settings, "
                       "under Samsung DeX → Mouse and trackpad."))])
        box.pack_start(self.ripristina(
            T("Ripristinare il mouse?", "Restore mouse defaults?"),
            T("Tasto principale, scorrimento, velocità del doppio clic, stile e dimensione del puntatore tornano come "
              "all'inizio.",
              "Primary button, scrolling, double-click speed, pointer style and size go back to how they were at the "
              "start."),
            lambda: self.ms_predefiniti(dispositivi)), False, False, 0)

    def ms_predefiniti(self, dispositivi):
        for d in dispositivi:
            for prop, valore in MOUSE_PREDEFINITI:
                xf_scrivi("pointers", f"/{d}/{prop}", valore)
        xf_scrivi("xsettings", "/Net/DoubleClickTime", DOPPIO_CLIC_PREDEFINITO)
        # puntatore: prima la dimensione, poi lo stile (solo se cambiato), poi si ridisegna la pagina
        fine = lambda: self.ricostruisci("mouse")   # noqa: E731
        stile = (lambda: tod("aspetto", "puntatore", "scuro", then=fine)) if conf_get("PUNTATORE", "scuro") != "scuro" \
            else fine
        tod("aspetto", "puntatore", "dimensione", "100", then=stile)

    # --- app installate: tutte quelle dello Start (Termux, Debian, telefono), con Disinstalla ---
    PROTECTED = re.compile(r"^(thunar|xfce4-.*|xfwm4|xfdesktop|xfce4|termux-.*|python.*|tod-.*|libxfce.*|exo)$")

    def installed_apps(self):
        """[(nome, icona, dove, pacchetto, kB)] delle app visibili nello Start."""
        desk_pkg, sizes, installed, read = {}, {}, set(), set()
        info = os.path.join(PREFIX, "var/lib/dpkg/info")
        try:
            for f in os.listdir(info):
                if f.endswith(".list"):
                    pkg = f[:-5].split(":")[0]
                    for line in open(os.path.join(info, f), errors="replace"):
                        if "/share/applications/" in line and line.rstrip().endswith(".desktop"):
                            desk_pkg[os.path.basename(line.strip())] = pkg
        except OSError:
            pass
        exports = os.path.join(STATE, "cx-exports")
        root = (self.cx_status() or "").rsplit("/var/lib/dpkg/status", 1)[0]   # rootfs del container
        try:
            for f in os.listdir(exports):
                for line in open(os.path.join(exports, f), errors="replace"):
                    if line.strip().endswith(".desktop"):
                        desk_pkg[os.path.basename(line.strip())] = "cx:" + f[:-6]
                    elif line.startswith("#exe ") and f.startswith("home-"):
                        # app nella cartella personale del container (AppImage di un launcher):
                        # non è un pacchetto, la dimensione è quella del suo file
                        installed.add("cx:" + f[:-6])
                        try:
                            sizes["cx:" + f[:-6]] = os.path.getsize(root + line[5:].strip()) // 1024
                        except OSError:
                            pass
        except OSError:
            pass
        # Dischi: la voce è nostra (tod-usb dischi) ma l'app è gnome-disk-utility di Debian
        desk_pkg["tod-dischi.desktop"] = "cx:gnome-disk-utility"
        for status, tag in ((os.path.join(PREFIX, "var/lib/dpkg/status"), ""), (self.cx_status(), "cx:")):
            pkg = None
            try:
                for line in open(status, errors="replace"):
                    if line.startswith("Package: "):
                        pkg, size = line[9:].strip(), 0
                    elif line.startswith("Installed-Size: ") and pkg:
                        size = int(line[16:].strip() or 0)
                    elif line.startswith("Status: ") and pkg:
                        if line.split()[-1] == "installed":
                            installed.add(tag + pkg)
                    elif not line.strip() and pkg:
                        sizes.setdefault(tag + pkg, size)
                if pkg:
                    sizes[tag + pkg] = size
                read.add(tag)
            except (OSError, ValueError, TypeError):
                pass
        short = {}
        try:   # gli stessi nomi brevi dello Start (GIMP, Blocco note, Foto...)
            import ast
            src = open(os.path.join(HERE, "tod-start.py")).read()
            mm = re.search(r"^SHORT_NAMES = (\{.*?^\})", src, re.S | re.M)
            short = {k: v[0 if ITALIAN else 1] for k, v in ast.literal_eval(mm.group(1)).items() if v}
        except (OSError, ValueError, SyntaxError, AttributeError):
            pass
        names, seen, apps = {}, set(), []
        for a in Gio.AppInfo.get_all():
            if not a.should_show():
                continue
            i = a.get_id() or ""
            if i.startswith("tod-android-"):
                where, pkg = "android", i[len("tod-android-"):-len(".desktop")]
                if pkg in self.gone:   # appena disinstallata: lo Start non si è ancora allineato
                    continue
            elif i in desk_pkg:
                pkg = desk_pkg[i]
                where = "debian" if pkg.startswith("cx:") else "termux"
                if pkg not in installed and ("cx:" if where == "debian" else "") in read:   # appena disinstallata: la voce del menu sparisce un attimo dopo
                    continue
            else:
                continue   # voci nostre (Impostazioni, Start...) o senza pacchetto: non si disinstallano da qui
            if self.PROTECTED.match(pkg.replace("cx:", "")):
                continue
            names.setdefault(pkg, []).append((short.get(i) or a.get_display_name(), a.get_display_name()))
            if pkg in seen:
                continue
            seen.add(pkg)
            apps.append([None, a.get_icon(), where, pkg, sizes.get(pkg, 0)])
        for app in apps:   # più voci nello stesso pacchetto (Writer, Calc...): il nome comune, "LibreOffice"
            ns = names[app[3]]
            if len(ns) == 1:
                app[0] = ns[0][0]
            else:
                words = os.path.commonprefix([raw.split() for _s, raw in ns])
                app[0] = " ".join(words) or min((sh for sh, _r in ns), key=len)
        apps.sort(key=lambda x: x[0].casefold())
        return [tuple(a) for a in apps]

    def cx_status(self):
        for p in (f"{PREFIX}/var/lib/proot-distro/containers/{conf_get('CX_DISTRO', 'debian')}/rootfs/var/lib/dpkg/status",
                  f"{PREFIX}/var/lib/proot-distro/installed-rootfs/{conf_get('CX_DISTRO', 'debian')}/var/lib/dpkg/status"):
            if os.path.isfile(p):
                return p
        return None

    # --- stampanti: il servizio di stampa (share/stampa/tod-stampa.py) -----------------------
    def stampa(self, *args, then=None):
        """tod-stampa.py in sottofondo (lpadmin chiede alla stampante cosa sa fare: qualche secondo);
        then(codice, testo) nella UI."""
        def work():
            try:
                r = subprocess.run([sys.executable, STAMPA, *args], capture_output=True, text=True, timeout=180,
                                   stdin=subprocess.DEVNULL)
                esito = (r.returncode, (r.stdout + r.stderr).strip())
            except (OSError, subprocess.TimeoutExpired) as e:
                esito = (1, str(e))
            if then:
                GLib.idle_add(lambda: then(*esito) and False)
        threading.Thread(target=work, daemon=True).start()

    def pane_stampanti(self, box):
        self.st_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.pack_start(self.st_box, False, False, 0)
        self.st_msg = Gtk.Label(xalign=0, wrap=True)
        self.st_msg.get_style_context().add_class("hint")
        box.pack_start(self.st_msg, False, False, 0)
        self.st_box.pack_start(self.row(T("Cerco le stampanti…", "Looking for printers…"), Gtk.Spinner(active=True)),
                               False, False, 0)
        self.fill_stampanti()
        self.sc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.pack_start(self.sc_box, False, False, 0)
        self.sc_msg = Gtk.Label(xalign=0, wrap=True)
        self.sc_msg.get_style_context().add_class("hint")
        box.pack_start(self.sc_msg, False, False, 0)
        self.fill_scanner()

    # --- scanner: share/stampa/tod-scanner.py (eSCL: Wi-Fi e multifunzione USB) ---------------
    def fill_scanner(self, msg=None):
        if msg is not None:
            self.sc_msg.set_text(msg)
        for c in self.sc_box.get_children():
            self.sc_box.remove(c)
        self.group(self.sc_box, T("Scanner", "Scanners"), [
            self.row(T("Cerco gli scanner…", "Looking for scanners…"), Gtk.Spinner(active=True))])
        self.sc_box.show_all()

        def carica():   # la ricerca in rete dura un paio di secondi: le stampanti intanto ci sono già
            try:
                lista = json.loads(subprocess.run([sys.executable, SCANNER, "elenco", "--json"], capture_output=True,
                                                  text=True, timeout=40, stdin=subprocess.DEVNULL).stdout or "[]")
            except (OSError, ValueError, subprocess.TimeoutExpired):
                lista = []
            GLib.idle_add(lambda: self.show_scanner(lista) and False)
        threading.Thread(target=carica, daemon=True).start()

    def apri_scanner(self, chiave=None):
        try:
            subprocess.Popen([sys.executable, SCANNER] + (["--scanner", chiave] if chiave else []),
                             stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            self.sc_msg.set_text(str(e))

    def show_scanner(self, lista):
        for c in self.sc_box.get_children():
            self.sc_box.remove(c)
        righe = []
        for s in lista:
            dove = {"usb": "USB", "wifi": "Wi-Fi", "rete": "Wi-Fi", "indirizzo": T("Aggiunto con l'indirizzo", "Added by address")}
            destra = [self.button(T("Scansiona", "Scan"), lambda k=s["chiave"]: self.apri_scanner(k))]
            if s["dove"] == "indirizzo":
                menu = Gtk.Menu()
                mi = Gtk.MenuItem(label=T("Rimuovi", "Remove"))
                mi.connect("activate", lambda _w, k=s["chiave"]: self.togli_scanner(k))
                menu.append(mi)
                menu.show_all()
                mb = Gtk.MenuButton(popup=menu)
                mb.add(Gtk.Image.new_from_icon_name("view-more-symbolic", Gtk.IconSize.BUTTON))
                destra.append(mb)
            spento = s.get("raggiungibile") is False
            destra[0].set_sensitive(not spento)
            righe.append(self.riga_dispositivo("scanner", s["nome"], "grigio" if spento else "verde", " · ".join(
                x for x in (dove.get(s["dove"], ""), "Offline" if spento else T("Pronto", "Ready")) if x), None, destra))
        if not righe:
            righe.append(self.row(T("Nessuno scanner", "No scanners"), None,
                                  T("Accendi la multifunzione Wi-Fi (nella stessa rete) o collega quella USB.",
                                    "Turn on your Wi-Fi multifunction printer (same network) or plug in the USB one.")))
        self.group(self.sc_box, T("Scanner", "Scanners"), righe)
        self.sc_box.show_all()
        return False

    def aggiungi_scanner(self, cosa):
        cosa = cosa.strip()
        if not cosa:
            return
        self.sc_msg.set_text(T("Provo l'indirizzo…", "Trying the address…"))

        def work():
            try:
                r = subprocess.run([sys.executable, SCANNER, "aggiungi", cosa], capture_output=True, text=True,
                                   timeout=60, stdin=subprocess.DEVNULL)
                esito = (r.returncode, (r.stdout + r.stderr).strip())
            except (OSError, subprocess.TimeoutExpired) as e:
                esito = (1, str(e))
            GLib.idle_add(lambda: self.fill_scanner(
                T("Scanner aggiunto.", "Scanner added.") if esito[0] == 0 else esito[1]) and False)
        threading.Thread(target=work, daemon=True).start()

    def togli_scanner(self, chiave):
        subprocess.run([sys.executable, SCANNER, "togli", chiave], capture_output=True, timeout=30,
                       stdin=subprocess.DEVNULL)
        self.fill_scanner(T("Scanner rimosso.", "Scanner removed."))

    def fill_stampanti(self, msg=None):
        if msg is not None:
            self.st_msg.set_text(msg)

        def carica():
            try:
                e = json.loads(subprocess.run([sys.executable, STAMPA, "elenco", "--json"], capture_output=True,
                                              text=True, timeout=30, stdin=subprocess.DEVNULL).stdout or "{}")
            except (OSError, ValueError, subprocess.TimeoutExpired):
                e = {}
            try:   # quelle trovate in rete dall'ultima ricerca del servizio (ogni minuto)
                trovate = json.load(open(os.path.join(TMP, "tod-stampa/trovate.json")))
            except (OSError, ValueError):
                trovate = []
            GLib.idle_add(lambda: self.show_stampanti(e, trovate) and False)
        threading.Thread(target=carica, daemon=True).start()

    def riga_dispositivo(self, icona, nome, punto, stato, etichetta=None, destra=()):
        """Una stampante o uno scanner nell'elenco: icona in un riquadro, nome, pallino di stato, pulsanti a destra."""
        st_css()
        h = Gtk.Box(spacing=14)
        h.get_style_context().add_class("group-row")
        img = Gtk.Image.new_from_icon_name(icona, Gtk.IconSize.DND)
        img.set_pixel_size(34)
        riq = Gtk.Box(valign=Gtk.Align.CENTER)
        riq.get_style_context().add_class("st-icona")
        riq.pack_start(img, False, False, 0)
        h.pack_start(riq, False, False, 0)
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, valign=Gtk.Align.CENTER)
        su = Gtk.Box(spacing=8)
        n = Gtk.Label(label=nome, xalign=0, ellipsize=3)
        n.get_style_context().add_class("st-nome")
        su.pack_start(n, False, False, 0)
        if etichetta:
            e = Gtk.Label(label=etichetta, valign=Gtk.Align.CENTER)
            e.get_style_context().add_class("st-etichetta")
            su.pack_start(e, False, False, 0)
        v.pack_start(su, False, False, 0)
        giu = Gtk.Box(spacing=6)
        p = Gtk.Box(valign=Gtk.Align.CENTER)
        p.get_style_context().add_class("st-punto")
        p.get_style_context().add_class("p-" + punto)
        giu.pack_start(p, False, False, 0)
        sl = Gtk.Label(label=stato, xalign=0, ellipsize=3)
        sl.get_style_context().add_class("hint")
        giu.pack_start(sl, False, False, 0)
        v.pack_start(giu, False, False, 0)
        h.pack_start(v, True, True, 0)
        for w in reversed(list(destra)):
            w.set_valign(Gtk.Align.CENTER)
            h.pack_end(w, False, False, 0)
        return h

    def stato_stampante(self, p):
        """(pallino, frase) di una stampante dell'elenco di tod-stampa.py"""
        n = len(p["lavori"])
        coda = T(f" · {n} in coda", f" · {n} queued") if n else ""
        if p["raggiungibile"] is False:   # prima di tutto: spenta o su un'altra rete non è né pronta né in pausa
            return "grigio", (T("Non collegata", "Not connected") if p["tipo"] == "usb" else "Offline") + coda
        if not p["attiva"]:
            return "arancio", T("In pausa", "Paused") + coda
        if p["stato"] == "stampa" or n:
            return "blu", (T("Sta stampando", "Printing") if p["stato"] == "stampa" else T("In attesa", "Waiting")) + \
                T(f" · {n} in coda" if n != 1 else " · 1 documento", f" · {n} queued")
        return "verde", T("Pronta", "Ready")

    def show_stampanti(self, e, trovate):
        for c in self.st_box.get_children():
            self.st_box.remove(c)
        if not e.get("servizio"):
            self.group(self.st_box, None, [self.row(
                T("Il servizio di stampa è spento", "The print service is off"),
                self.button(T("Accendi", "Turn on"), lambda: tod("stampa", "riavvia", then=lambda: GLib.timeout_add_seconds(
                    4, lambda: self.fill_stampanti() and False))),
                T("Parte da solo con il desktop. Se non si accende, da un terminale: tod stampa prepara",
                  "It starts with the desktop. If it doesn't, from a terminal: tod stampa prepara"))])
            self.st_box.show_all()
            return False
        righe = []
        in_coda = False
        for p in e.get("stampanti", []):
            punto, frase = self.stato_stampante(p)
            dove = {"usb": "USB", "rete": "Wi-Fi"}.get(p["tipo"], p["tipo"])
            n = len(p["lavori"])
            in_coda = in_coda or bool(n)
            coda = self.button(T("Coda di stampa", "Print queue") + (f" ({n})" if n else ""),
                               lambda nome=p["nome"]: self.apri_coda(nome))
            menu = Gtk.Menu()
            voci = []
            if not p["predefinita"]:
                voci.append((T("Imposta come predefinita", "Set as default"), lambda n=p["nome"]: self.stampa(
                    "predefinita", n, then=lambda *_: self.fill_stampanti())))
            voci.append((T("Stampa una pagina di prova", "Print a test page"), lambda n=p["nome"]: self.stampa(
                "prova", n, then=lambda c, t: self.fill_stampanti(
                    T("Pagina di prova inviata.", "Test page sent.") if c == 0 else t))))
            if not p["attiva"]:
                voci.insert(0, (T("Riprendi la stampa", "Resume printing"), lambda n=p["nome"]: self.stampa(
                    "riprendi", n, then=lambda *_: self.fill_stampanti())))
            voci.append((T("Rimuovi", "Remove"), lambda q=p: self.togli_stampante(q)))
            for testo, cb in voci:
                mi = Gtk.MenuItem(label=testo)
                mi.connect("activate", lambda _w, f=cb: f())
                menu.append(mi)
            menu.show_all()
            mb = Gtk.MenuButton(popup=menu)
            mb.add(Gtk.Image.new_from_icon_name("view-more-symbolic", Gtk.IconSize.BUTTON))
            righe.append(self.riga_dispositivo("printer", p["descrizione"], punto, f"{dove} · {frase}",
                                               T("Predefinita", "Default") if p["predefinita"] else None, (coda, mb)))
            if not p["attiva"] and p.get("motivo"):
                motivo = p["motivo"]
                if ITALIAN and re.search(r"configuration is incorrect|no longer exists|Unable to locate|not responding", motivo):
                    motivo = ("La stampante non risponde o non è configurata bene. Controlla che sia accesa; "
                              "se non riparte, rimuovila e aggiungila di nuovo.")
                righe.append(self.row(T("Stampante ferma", "Printer stopped"),
                                      self.button(T("Riprendi", "Resume"), lambda n=p["nome"]: self.stampa(
                                          "riprendi", n, then=lambda *_: self.fill_stampanti())), motivo))
        if not righe:
            righe.append(self.row(T("Nessuna stampante", "No printers"), None,
                                  T("Accendi la stampante Wi-Fi o collega quella USB: di solito si aggiunge da sola.",
                                    "Turn on your Wi-Fi printer or plug in the USB one: it's usually added automatically.")))
        self.group(self.st_box, T("Stampanti", "Printers"), righe)
        if in_coda and not getattr(self, "_st_giro", 0):   # finché c'è coda l'elenco si aggiorna da solo
            def rifai():
                self._st_giro = 0
                if self.st_box.get_mapped():
                    self.fill_stampanti()
                return False
            self._st_giro = GLib.timeout_add_seconds(4, rifai)
        # stampanti viste in rete e non ancora aggiunte: lo dice il pulsante
        ip_installati = {re.sub(r"^\w+://([^/:]+).*", r"\1", p["uri"]) for p in e.get("stampanti", [])}
        nuove = [t for t in trovate if t["ip"] not in ip_installati]
        b = Gtk.Button(label=T("Aggiungi stampante o scanner…", "Add printer or scanner…"))
        b.get_style_context().add_class("suggested-action")
        b.connect("clicked", lambda *_: self.aggiunta_guidata())
        riga = Gtk.Box(spacing=12)
        riga.pack_start(b, False, False, 0)
        if nuove:
            av = Gtk.Label(label=T(f"Trovata in rete: {nuove[0]['modello']}" if len(nuove) == 1 else
                                   f"Trovate in rete: {len(nuove)} stampanti", f"Found on the network: {len(nuove)}"), xalign=0)
            av.get_style_context().add_class("hint")
            riga.pack_start(av, False, False, 0)
        self.st_box.pack_start(riga, False, False, 4)
        self.st_box.show_all()
        return False

    def apri_coda(self, nome):
        aperte = getattr(self, "_code", None)
        if aperte is None:
            aperte = self._code = {}
        w = aperte.get(nome)
        if w is None:
            w = aperte[nome] = Coda(self, nome)
            w.connect("destroy", lambda *_: (aperte.pop(nome, None), self.fill_stampanti()))
        w.present()

    def aggiunta_guidata(self):
        AggiuntaGuidata(self).present()

    def aggiungi_stampante(self, cosa):
        cosa = cosa.strip()
        if not cosa:
            return
        self.st_msg.set_text(T("Aggiungo la stampante…", "Adding the printer…"))
        self.stampa("aggiungi", cosa, then=lambda c, t: self.fill_stampanti(
            T("Stampante aggiunta.", "Printer added.") if c == 0 else T("Non sono riuscito ad aggiungerla: ", "Couldn't add it: ") + t))

    def ricerca_stampanti(self):
        self.st_msg.set_text(T("Cerco in rete…", "Searching the network…"))

        def fatto(c, t):
            try:   # il risultato della ricerca: lo stesso file che scrive il servizio
                trovate = json.loads(t.splitlines()[-1]) if t else []
                os.makedirs(os.path.join(TMP, "tod-stampa"), exist_ok=True)
                with open(os.path.join(TMP, "tod-stampa/trovate.json"), "w") as f:
                    json.dump(trovate, f, ensure_ascii=False)
                msg = T(f"Trovate in rete: {len(trovate)}.", f"Found on the network: {len(trovate)}.")
            except (ValueError, IndexError, OSError):
                msg = T("Ricerca non riuscita.", "Search failed.")
            self.fill_stampanti(msg)
        self.stampa("cerca", "--json", then=fatto)

    def togli_stampante(self, p):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.NONE, text=T(f"Rimuovere {p['descrizione']}?", f"Remove {p['descrizione']}?"))
        d.format_secondary_text(T("Non comparirà più nella finestra di stampa. Se è USB, torna quando la ricolleghi.",
                                  "It won't appear in print dialogs anymore. A USB printer comes back when you plug it in again."))
        d.add_button(T("Annulla", "Cancel"), Gtk.ResponseType.CANCEL)
        d.add_button(T("Rimuovi", "Remove"), Gtk.ResponseType.OK).get_style_context().add_class("destructive-action")
        ok = tod_avviso.come_macos(d).run() == Gtk.ResponseType.OK
        d.destroy()
        if ok:
            self.stampa("togli", p["nome"], then=lambda *_: self.fill_stampanti(T("Stampante rimossa.", "Printer removed.")))

    def pane_app(self, box):
        self.app_filter = Gtk.SearchEntry(placeholder_text=T("Cerca un'app", "Search apps"))
        box.pack_start(self.app_filter, False, False, 0)
        self.app_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.pack_start(self.app_box, False, False, 0)
        self.app_filter.connect("search-changed", lambda *a: self.fill_apps())
        self.apps_cache = self.installed_apps()
        self.fill_apps()
        self.pane_app_defaults(box)

    def fill_apps(self):
        for c in self.app_box.get_children():
            self.app_box.remove(c)
        q = self.app_filter.get_text().strip().casefold()
        where_label = {"termux": "", "debian": "", "android": ""}
        groups = {"desk": [], "android": []}
        for name, icon, where, pkg, kb in self.apps_cache:
            if q and q not in name.casefold():
                continue
            rows = groups["android" if where == "android" else "desk"]
            h = Gtk.Box(spacing=12)
            h.get_style_context().add_class("group-row")
            img = Gtk.Image.new_from_gicon(icon, Gtk.IconSize.DND) if icon else Gtk.Image.new_from_icon_name(
                "application-x-executable", Gtk.IconSize.DND)
            img.set_pixel_size(32)
            h.pack_start(img, False, False, 0)
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
            v.pack_start(Gtk.Label(label=name, xalign=0), False, False, 0)
            sub = " · ".join(x for x in (where_label[where], self.human(kb)) if x)
            if sub:
                sl = Gtk.Label(label=sub, xalign=0)
                sl.get_style_context().add_class("hint")
                v.pack_start(sl, False, False, 0)
            h.pack_start(v, True, True, 0)
            b = Gtk.Button(label=T("Disinstalla", "Uninstall"), valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda btn, n=name, w=where, p=pkg: self.uninstall(btn, n, w, p))
            h.pack_end(b, False, False, 0)
            rows.append(h)
        if not groups["desk"] and not groups["android"]:
            groups["desk"].append(self.row(T("Nessuna app trovata", "No apps found")))
        if groups["desk"]:
            self.group(self.app_box, T("App del desktop", "Desktop apps"), groups["desk"])
        if groups["android"]:
            self.group(self.app_box, T("App del telefono", "Phone apps"), groups["android"])
        self.app_box.show_all()

    @staticmethod
    def human(kb):
        if not kb:
            return ""
        mb = kb / 1024
        return f"{mb / 1024:.1f} GB".replace(".", ",") if mb >= 1024 else f"{mb:.0f} MB" if mb >= 1 else f"{kb} KB"

    def uninstall(self, btn, name, where, pkg):
        if where == "android":   # la conferma la chiede Android: si guarda quando l'app sparisce davvero
            spawn([os.path.join(PREFIX, "bin/am"), "start", "--user", "0", "-a", "android.intent.action.DELETE",
                   "-d", "package:" + pkg])
            self.watch_android_removal(pkg)
            return
        # il disinstallatore (tod-disinstalla.py): riepilogo di cosa se ne va, componenti rimasti orfani per
        # colpa sua, impostazioni e dati nel Cestino se scelto. Chiusa la finestra, l'elenco si aggiorna
        if where == "debian" and pkg.startswith("cx:home-"):   # AppImage & co.: il file e la sua voce
            dove, nome_pkg = "home", pkg[len("cx:home-"):]
        elif where == "debian":
            dove, nome_pkg = "debian", pkg.replace("cx:", "")
        else:
            dove, nome_pkg = "termux", pkg
        btn.set_sensitive(False)

        def attendi():
            try:
                subprocess.run(["python3", os.path.join(HERE, "tod-disinstalla.py"), dove, nome_pkg, name],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
            GLib.idle_add(lambda: self.reload_apps() and False)
        threading.Thread(target=attendi, daemon=True).start()

    def watch_android_removal(self, pkg, tries=60):
        """Ogni 2 s (per 2 minuti, il tempo di confermare): appena Android l'ha tolta, sparisce dall'elenco
        e lo Start si allinea. Se l'utente annulla, l'app resta e basta."""
        def check():
            out = sh(["/system/bin/pm", "list", "packages", pkg], timeout=10)
            gone = ("package:" + pkg) not in out.split()
            GLib.idle_add(lambda: self.android_removed(pkg) if gone else
                          (tries > 1 and GLib.timeout_add_seconds(2, lambda: self.watch_android_removal(pkg, tries - 1)))
                          and False)
        threading.Thread(target=check, daemon=True).start()
        return False

    def android_removed(self, pkg):
        self.gone.add(pkg)
        self.reload_apps()
        tod("telefono", "aggiorna", then=self.reload_apps)
        return False

    def reload_apps(self):
        self.apps_cache = self.installed_apps()
        self.fill_apps()
        # il menu delle app (GIO) si accorge dei file tolti con un attimo di ritardo: un secondo giro
        GLib.timeout_add_seconds(3, lambda: (setattr(self, "apps_cache", self.installed_apps()), self.fill_apps()) and False)

    def pane_app_defaults(self, box):
        browser = ""
        try:
            for line in open(os.path.join(os.environ.get("HOME", HOME), ".config/xfce4/helpers.rc")):
                if line.startswith("WebBrowser="):
                    browser = line.split("=", 1)[1].strip().title()
        except OSError:
            pass
        self.group(box, T("App predefinite", "Default apps"), [
            self.row(T("Browser web", "Web browser"), self.value(browser or "Chromium")),
            self.row(T("App per ogni tipo di file", "App for each file type"),
                     self.button(T("Apri", "Open"), lambda: spawn(["xfce4-mime-settings"])),
                     T("Per esempio quale app apre le foto o i PDF.", "For example which app opens photos or PDFs."))])

    def pane_aggiornamenti(self, box):
        try:
            st = json.load(open(os.path.join(STATE, "updates.json")))
        except (OSError, ValueError):
            st = {}
        n = len(st.get("termux", [])) + len(st.get("debian", []))
        auto = conf_get("AGGIORNAMENTI_AUTO", "1") == "1"
        status = (T(f"{n} aggiornamenti disponibili", f"{n} updates available") if n
                  else T("Il sistema è aggiornato", "Your system is up to date")) if st else T("Mai controllato", "Never checked")
        self.group(box, None, [
            self.row(status, self.button(T("Apri Aggiornamenti", "Open Updates"),
                                         lambda: spawn([sys.executable, os.path.join(HERE, "tod-updates.py")]))),
            self.row(T("Controlla automaticamente", "Check automatically"),
                     self.switch(auto, lambda on: conf_set("AGGIORNAMENTI_AUTO", "1" if on else "0")),
                     T("All'avvio e ogni 6 ore; ti avvisa con una notifica.", "At start and every 6 hours, with a notification."))])

    # --- Archiviazione (13 novembre, come macOS: Generali → Archiviazione; i conti in archivio_dati.py) ----------
    ARCH_SPIEGA = {
        "android": T("Foto, video, app del telefono e sistema Android: si gestiscono dalle impostazioni del telefono.",
                     "Photos, videos, phone apps and Android itself: managed from the phone's settings."),
        "termux": T("I programmi del desktop: XFCE, Python, GStreamer e tutto quello che lo fa funzionare.",
                    "The desktop's programs: XFCE, Python, GStreamer and everything that makes it work."),
        "debian": T("Il sistema Debian e le app che ci sono installate (LibreOffice, GIMP, VS Code…).",
                    "The Debian system and the apps installed in it (LibreOffice, GIMP, VS Code…)."),
        "store": T("Le app installate dallo Store.", "Apps installed from the Store."),
        "dati": T("Impostazioni, accessi e file che le app salvano per conto loro.",
                  "Settings, sign-ins and files the apps keep for themselves."),
        "documenti": T("I tuoi file nella casa del desktop: Scrivania, download e il resto.",
                       "Your files in the desktop's home: Desktop, downloads and the rest."),
        "sviluppo": T("Sorgenti, compilazioni e prove: si possono rifare quando servono.",
                      "Sources, builds and tests: they can be redone when needed."),
        "cache": T("Copie che le app ricreano da sole quando servono.", "Copies the apps recreate by themselves when needed."),
        "temporanei": T("File che le app usano mentre lavorano: di solito spariscono da soli.",
                        "Files the apps use while working: they usually go away by themselves."),
        "cestino": T("I file che hai buttato, finché non svuoti il Cestino.",
                     "Files you threw away, until you empty the Trash."),
    }

    def pane_archiviazione(self, box):
        import archivio_dati as AD
        self.ad = AD
        testa = Gtk.Box(spacing=8)
        self.arch_stato = Gtk.Label(xalign=0)
        self.arch_stato.get_style_context().add_class("hint")
        testa.pack_start(self.arch_stato, True, True, 0)
        self.arch_agg = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        self.arch_agg.set_relief(Gtk.ReliefStyle.NONE)
        self.arch_agg.set_tooltip_text(T("Misura di nuovo", "Measure again"))
        self.arch_agg.connect("clicked", lambda *_: self.arch_misura())
        testa.pack_end(self.arch_agg, False, False, 0)
        self.arch_rotella = Gtk.Spinner()
        testa.pack_end(self.arch_rotella, False, False, 0)
        box.pack_start(testa, False, False, 0)
        self.arch_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.pack_start(self.arch_box, False, False, 0)
        self.arch_aperta = None
        vecchia = AD.ultima()
        if vecchia:   # subito l'ultima misura (e i consigli, che costano poco), poi i conti nuovi
            self.arch_dati = (vecchia, AD.temporanei_grandi([os.path.join(AD.PREFIX, "tmp"),
                                                             os.path.join(AD.ROOTFS, "tmp")]), AD.cache_pulibili(vecchia))
            self.arch_mostra()
        self.arch_misura()

    def arch_colore_testo(self, w):
        c = w.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        return c.red, c.green, c.blue

    @staticmethod
    def arch_rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    @staticmethod
    def arch_tondo(cr, x, y, w, h, r):
        import math
        r = min(r, w / 2, h / 2)
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def arch_pallino(self, colore, lato=12):
        """il quadratino tondo del colore della categoria; None = come lo spazio libero"""
        a = Gtk.DrawingArea()
        a.set_size_request(lato, lato)
        a.set_valign(Gtk.Align.CENTER)

        def disegna(w, cr):
            self.arch_tondo(cr, 0, 0, lato, lato, lato * 0.3)
            if colore:
                cr.set_source_rgb(*self.arch_rgb(colore))
            else:
                cr.set_source_rgba(*self.arch_colore_testo(w), 0.14)
            cr.fill()
        a.connect("draw", disegna)
        return a

    def arch_barra(self, dati):
        """la barra a colori; al passaggio del mouse dice cosa è quel pezzo"""
        AD = self.ad
        cat = dati["categorie"]
        pezzi = [(k, cat[k]["byte"], col) for k, _it, _en, col, _i in AD.CATEGORIE if cat[k]["byte"] > 0]
        totale = dati["totale"] or sum(n for _k, n, _c in pezzi)
        nomi = {k: (it if ITALIAN else en) for k, it, en, *_ in AD.CATEGORIE}
        a = Gtk.DrawingArea()
        a.set_size_request(-1, 22)
        a.set_has_tooltip(True)

        def posizioni(larga):
            x, out = 0.0, []
            for k, n, col in pezzi:
                l = larga * n / totale if totale else 0
                out.append((k, n, col, x, l))
                x += l
            return out

        def disegna(w, cr):
            larga, alta = w.get_allocated_width(), w.get_allocated_height()
            self.arch_tondo(cr, 0, 0, larga, alta, 6)
            cr.save()
            cr.clip_preserve()
            cr.set_source_rgba(*self.arch_colore_testo(w), 0.14)   # lo spazio libero
            cr.fill()
            for _k, _n, col, x, l in posizioni(larga):
                if l < 0.5:
                    continue
                cr.rectangle(x, 0, max(1.0, l), alta)
                cr.set_source_rgb(*self.arch_rgb(col))
                cr.fill()
                if l > 3:   # una lineetta chiara tra un pezzo e l'altro, come macOS
                    cr.rectangle(x + l - 1, 0, 1, alta)
                    cr.set_source_rgba(1, 1, 1, 0.6)
                    cr.fill()
            cr.restore()
            return False

        def suggerimento(w, x, _y, _t, tip):
            for k, n, _c, x0, l in posizioni(w.get_allocated_width()):
                if x0 <= x < x0 + l:
                    tip.set_text(f"{nomi[k]}: {AD.dimensione(n, ITALIAN)}")
                    return True
            tip.set_text(T(f"Libero: {AD.dimensione(dati['libero'], ITALIAN)}",
                           f"Free: {AD.dimensione(dati['libero'], ITALIAN)}"))
            return True
        a.connect("draw", disegna)
        a.connect("query-tooltip", suggerimento)
        return a

    def arch_mostra(self):
        if self.arch_dati is None:
            return
        for f in self.arch_box.get_children():
            self.arch_box.remove(f)
            f.destroy()
        if self.arch_aperta:
            self.arch_dettaglio(self.arch_aperta)
        else:
            self.arch_panoramica()
        self.arch_box.show_all()
        quando = self.arch_dati[0].get("quando")
        if not self.arch_in_misura and quando:
            self.arch_stato.set_text(T(f"Misurato alle {time.strftime('%H:%M', time.localtime(quando))}",
                                       f"Measured at {time.strftime('%H:%M', time.localtime(quando))}"))

    def arch_panoramica(self):
        AD, (dati, temp, pul) = self.ad, self.arch_dati
        cat = dati["categorie"]
        b = self.arch_box
        tot = dati["totale"] or sum(v["byte"] for v in cat.values())
        # il telefono e la barra
        g = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=16)
        riga = Gtk.Box(spacing=12)
        img = Gtk.Image.new_from_icon_name("drive-harddisk", Gtk.IconSize.DIALOG)
        img.set_pixel_size(40)
        riga.pack_start(img, False, False, 0)
        nomi = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        nomi.pack_start(Gtk.Label(label=T("Memoria del telefono", "Phone storage"), xalign=0), False, False, 0)
        model = sh(["getprop", "ro.product.model"], timeout=3)
        sotto = Gtk.Label(label=next((v for k, v in DEVICES.items() if model.startswith(k)), model) if model
                          else T("Spazio interno", "Internal storage"), xalign=0)
        sotto.get_style_context().add_class("hint")
        nomi.pack_start(sotto, False, False, 0)
        riga.pack_start(nomi, True, True, 0)
        uso = Gtk.Label(label=T(f"{AD.dimensione(dati['usato'], ITALIAN)} di {AD.dimensione(tot, ITALIAN)} usati",
                                f"{AD.dimensione(dati['usato'], ITALIAN)} of {AD.dimensione(tot, ITALIAN)} used"))
        uso.get_style_context().add_class("arch-uso")
        riga.pack_end(uso, False, False, 0)
        g.pack_start(riga, False, False, 0)
        g.pack_start(self.arch_barra(dati), False, False, 0)
        legenda = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=14, row_spacing=4,
                              max_children_per_line=5)
        voci = [(col, it if ITALIAN else en) for k, it, en, col, _i in AD.CATEGORIE if cat[k]["byte"] > 0]
        voci.append((None, T(f"Libero · {AD.dimensione(dati['libero'], ITALIAN)}",
                             f"Free · {AD.dimensione(dati['libero'], ITALIAN)}")))
        for col, testo in voci:
            v = Gtk.Box(spacing=6)
            v.pack_start(self.arch_pallino(col, 10), False, False, 0)
            lab = Gtk.Label(label=testo)
            lab.get_style_context().add_class("arch-legenda")
            v.pack_start(lab, False, False, 0)
            legenda.add(v)
        g.pack_start(legenda, False, False, 0)
        carta = Gtk.Box()
        carta.get_style_context().add_class("group")
        carta.pack_start(g, True, True, 0)
        b.pack_start(carta, False, False, 0)
        # consigli
        righe = []
        for k, n, extra in AD.consigli(dati, temp, pul):
            if k == "temporanei":
                righe.append(self.row(T("Elimina i file temporanei grandi", "Delete large temporary files"),
                                      self.button(T("Elimina…", "Delete…"), self.arch_elimina_temporanei),
                                      T(f"{len(extra)} file che nessun programma sta usando occupano "
                                        f"{AD.dimensione(n, ITALIAN)} (il più grande: {os.path.basename(extra[0][0])}).",
                                        f"{len(extra)} files no program is using take {AD.dimensione(n, ITALIAN)} "
                                        f"(the largest: {os.path.basename(extra[0][0])}).")))
            elif k == "cestino":
                righe.append(self.row(T("Svuota il Cestino", "Empty the Trash"),
                                      self.button(T("Svuota…", "Empty…"), self.arch_svuota_cestino),
                                      T(f"Il Cestino occupa {AD.dimensione(n, ITALIAN)}.",
                                        f"The Trash takes {AD.dimensione(n, ITALIAN)}.")))
            else:
                righe.append(self.row(T("Pulisci la cache", "Clean the cache"),
                                      self.button(T("Pulisci…", "Clean…"), self.arch_pulisci_cache),
                                      T(f"{AD.dimensione(n, ITALIAN)} di copie che le app ricreano quando servono "
                                        "(quelle delle app aperte non si toccano).",
                                        f"{AD.dimensione(n, ITALIAN)} of copies apps recreate when needed "
                                        "(those of open apps are left alone).")))
        if righe:
            self.group(b, T("Consigli", "Recommendations"), righe)
        # le categorie, dalla più grande: un clic apre il dettaglio
        righe = []
        for k, it, en, col, _i in sorted(AD.CATEGORIE, key=lambda c: -cat[c[0]]["byte"]):
            if cat[k]["byte"] <= 0:
                continue
            bt = Gtk.Button()
            bt.set_relief(Gtk.ReliefStyle.NONE)
            bt.get_style_context().add_class("arch-voce")
            h = Gtk.Box(spacing=12)
            h.pack_start(self.arch_pallino(col, 14), False, False, 0)
            h.pack_start(Gtk.Label(label=it if ITALIAN else en, xalign=0), True, True, 0)
            m = Gtk.Label(label=AD.dimensione(cat[k]["byte"], ITALIAN))
            m.get_style_context().add_class("arch-misura")
            h.pack_start(m, False, False, 0)
            h.pack_start(Gtk.Image.new_from_icon_name("go-next-symbolic", Gtk.IconSize.MENU), False, False, 0)
            bt.add(h)
            bt.connect("clicked", lambda _b, k=k: self.arch_apri(k))
            righe.append(bt)
        self.group(b, T("Cosa occupa lo spazio", "What uses the space"), righe)
        nota = Gtk.Label(label=T("«Android e altre app» è il resto dello spazio occupato sul telefono: lo gestiscono le "
                                 "impostazioni del telefono.", "“Android and other apps” is the rest of the used space "
                                 "on the phone: the phone's settings manage it."), xalign=0, wrap=True)
        nota.get_style_context().add_class("hint")
        b.pack_start(nota, False, False, 0)

    def arch_apri(self, k):
        self.arch_aperta = k
        self.arch_mostra()
        sw = self.arch_box.get_ancestor(Gtk.ScrolledWindow)
        if sw is not None:
            sw.get_vadjustment().set_value(0)

    def arch_dettaglio(self, k):
        AD, (dati, temp, pul) = self.ad, self.arch_dati
        nome = next((it if ITALIAN else en, col) for kk, it, en, col, _i in AD.CATEGORIE if kk == k)
        cat = dati["categorie"][k]
        b = self.arch_box
        indietro = Gtk.Button()
        indietro.set_relief(Gtk.ReliefStyle.NONE)
        indietro.get_style_context().add_class("arch-indietro")
        h = Gtk.Box(spacing=4)
        h.pack_start(Gtk.Image.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.MENU), False, False, 0)
        h.pack_start(Gtk.Label(label=T("Tutte le categorie", "All categories")), False, False, 0)
        indietro.add(h)
        indietro.set_halign(Gtk.Align.START)
        indietro.connect("clicked", lambda *_: (setattr(self, "arch_aperta", None), self.arch_mostra()))
        b.pack_start(indietro, False, False, 0)
        testa = Gtk.Box(spacing=10)
        testa.pack_start(self.arch_pallino(nome[1], 18), False, False, 0)
        t = Gtk.Label(label=nome[0], xalign=0)
        t.get_style_context().add_class("arch-uso")
        testa.pack_start(t, True, True, 0)
        m = Gtk.Label(label=AD.dimensione(cat["byte"], ITALIAN))
        m.get_style_context().add_class("arch-uso")
        testa.pack_end(m, False, False, 0)
        b.pack_start(testa, False, False, 0)
        spiega = Gtk.Label(label=self.ARCH_SPIEGA[k], xalign=0, wrap=True)
        spiega.get_style_context().add_class("hint")
        b.pack_start(spiega, False, False, 0)
        azione = {"cestino": (T("Svuota il Cestino…", "Empty the Trash…"), self.arch_svuota_cestino),
                  "cache": (T("Pulisci la cache…", "Clean the cache…"), self.arch_pulisci_cache),
                  "temporanei": (T("Elimina i file grandi non in uso…", "Delete large unused files…"),
                                 self.arch_elimina_temporanei),
                  "android": (T("Apri le impostazioni del telefono", "Open the phone's settings"),
                              lambda: am("android.settings.INTERNAL_STORAGE_SETTINGS"))}.get(k)
        if azione and (k != "temporanei" or any(not x[2] for x in temp)) and (k != "cache" or pul) and \
                (k != "cestino" or cat["byte"] > 0):
            bt = self.button(*azione)
            bt.set_halign(Gtk.Align.START)
            b.pack_start(bt, False, False, 0)
        parti = [p for p in cat["parti"] if p[0]]
        righe = []
        for p, n in parti[:60]:
            w = Gtk.Box(spacing=8)
            m = Gtk.Label(label=AD.dimensione(n, ITALIAN))
            m.get_style_context().add_class("arch-misura")
            w.pack_start(m, False, False, 0)
            if os.path.exists(p):
                mostra = Gtk.Button.new_from_icon_name("folder-open-symbolic", Gtk.IconSize.BUTTON)
                mostra.set_relief(Gtk.ReliefStyle.NONE)
                mostra.set_tooltip_text(T("Mostra in Esplora file", "Show in File Explorer"))
                mostra.connect("clicked", lambda _b, p=p: self.arch_mostra_file(p))
                w.pack_start(mostra, False, False, 0)
            righe.append(self.row(os.path.basename(p.rstrip("/")) or p, w, AD.leggibile(p)))
        if righe:
            self.group(b, None, righe)
        if len(parti) > 60:
            resto = sum(n for _p, n in parti[60:])
            l = Gtk.Label(label=T(f"e altre {len(parti) - 60} cartelle: {AD.dimensione(resto, ITALIAN)}",
                                  f"and {len(parti) - 60} more folders: {AD.dimensione(resto, ITALIAN)}"), xalign=0)
            l.get_style_context().add_class("hint")
            b.pack_start(l, False, False, 0)

    def arch_mostra_file(self, p):
        """Esplora file con la cartella scelta (D-Bus FileManager1 di Thunar; se no la cartella che la contiene)"""
        uri = Gio.File.new_for_path(p).get_uri()
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1",
                          "ShowItems", GLib.Variant("(ass)", ([uri], "")), None, Gio.DBusCallFlags.NONE, 3000, None)
        except GLib.Error:
            spawn(["xdg-open", os.path.dirname(p)])

    def arch_misura(self):
        if self.arch_in_misura:
            return
        AD = self.ad
        self.arch_in_misura = True
        self.arch_rotella.start()
        self.arch_agg.set_sensitive(False)
        self.arch_stato.set_text(T("Misuro lo spazio…", "Measuring the space…"))

        def lavoro():
            try:
                dati = AD.analizza(lambda n: GLib.idle_add(lambda: self.arch_stato.set_text(
                    T(f"Misuro lo spazio… ({n} di 4)", f"Measuring the space… ({n} of 4)")) and False))
                temp = AD.temporanei_grandi([os.path.join(AD.PREFIX, "tmp"), os.path.join(AD.ROOTFS, "tmp")])
                pul = AD.cache_pulibili(dati)
                ris = (dati, temp, pul)
            except Exception as e:   # noqa: BLE001 - una misura andata male non chiude le Impostazioni
                print(f"tod-settings: archiviazione: {type(e).__name__}: {e}", file=sys.stderr)
                ris = None
            GLib.idle_add(lambda: self.arch_misurato(ris) and False)
        threading.Thread(target=lavoro, daemon=True).start()

    def arch_misurato(self, ris):
        self.arch_in_misura = False
        self.arch_rotella.stop()
        self.arch_agg.set_sensitive(True)
        if ris is None:
            self.arch_stato.set_text(T("Misura non riuscita", "Measuring failed"))
            return
        self.arch_dati = ris
        self.arch_mostra()

    def arch_chiedi(self, titolo, testo, si, pericoloso=True):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                              buttons=Gtk.ButtonsType.NONE, text=titolo)
        d.format_secondary_text(testo)
        d.add_button(T("Annulla", "Cancel"), Gtk.ResponseType.CANCEL)
        bt = d.add_button(si, Gtk.ResponseType.OK)
        if pericoloso:
            bt.get_style_context().add_class("destructive-action")
        d.set_default_response(Gtk.ResponseType.CANCEL)
        tod_avviso.come_macos(d)
        r = d.run()
        d.destroy()
        return r == Gtk.ResponseType.OK

    def arch_dopo(self, lavoro):
        """un'azione in sottofondo, poi una misura nuova"""
        self.arch_stato.set_text(T("Libero lo spazio…", "Freeing space…"))
        self.arch_rotella.start()

        def giro():
            try:
                lavoro()
            except Exception as e:   # noqa: BLE001
                print(f"tod-settings: archiviazione: {type(e).__name__}: {e}", file=sys.stderr)
            GLib.idle_add(lambda: (self.arch_rotella.stop(), self.arch_misura()) and False)
        threading.Thread(target=giro, daemon=True).start()

    def arch_elimina_temporanei(self):
        AD = self.ad
        liberi = [t for t in self.arch_dati[1] if not t[2]]
        if not liberi:
            return
        n = sum(t[1] for t in liberi)
        elenco = "\n".join(f"• {os.path.basename(p)} ({AD.dimensione(x, ITALIAN)})" for p, x, _a in liberi[:6])
        if not self.arch_chiedi(T(f"Eliminare {len(liberi)} file temporanei?", f"Delete {len(liberi)} temporary files?"),
                                T(f"Si libera {AD.dimensione(n, ITALIAN)}. Nessun programma li sta usando; non vanno nel "
                                  "Cestino.\n\n", f"This frees {AD.dimensione(n, ITALIAN)}. No program is using them; "
                                  "they don't go to the Trash.\n\n") + elenco, T("Elimina", "Delete")):
            return

        def lavoro():
            in_uso = AD.aperti()   # di nuovo, subito prima: un programma potrebbe averli aperti nel frattempo
            for p, _x, _a in liberi:
                if p not in in_uso:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        self.arch_dopo(lavoro)

    def arch_svuota_cestino(self):
        AD = self.ad
        n = self.arch_dati[0]["categorie"]["cestino"]["byte"]
        if not self.arch_chiedi(T("Svuotare il Cestino?", "Empty the Trash?"),
                                T(f"I file nel Cestino ({AD.dimensione(n, ITALIAN)}) vengono eliminati per sempre: non si "
                                  "possono più recuperare.", f"The files in the Trash ({AD.dimensione(n, ITALIAN)}) are "
                                  "deleted forever: they can't be recovered."), T("Svuota il Cestino", "Empty Trash")):
            return

        def lavoro():
            try:
                subprocess.run(["gio", "trash", "--empty"], timeout=600, check=True, capture_output=True)
            except (OSError, subprocess.SubprocessError):
                base = os.path.join(AD.CASA, ".local/share/Trash")
                for sotto in ("files", "info"):
                    shutil.rmtree(os.path.join(base, sotto), ignore_errors=True)
                    os.makedirs(os.path.join(base, sotto), exist_ok=True)
        self.arch_dopo(lavoro)

    def arch_pulisci_cache(self):
        AD = self.ad
        pul = list(self.arch_dati[2])
        if not pul:
            return
        n = sum(x for _p, x in pul)
        nomi = ", ".join(os.path.basename(p) for p, _x in pul[:6])
        if not self.arch_chiedi(T("Pulire la cache?", "Clean the cache?"),
                                T(f"Si libera {AD.dimensione(n, ITALIAN)} ({nomi}). Le app ricreano le copie che servono; "
                                  "la prima apertura può essere un po' più lenta.",
                                  f"This frees {AD.dimensione(n, ITALIAN)} ({nomi}). Apps recreate the copies they "
                                  "need; the first start may be a little slower."), T("Pulisci", "Clean"),
                                pericoloso=False):
            return
        self.arch_dopo(lambda: [shutil.rmtree(p, ignore_errors=True) for p, _x in pul])

    def pane_info(self, box):
        model = sh(["getprop", "ro.product.model"]) or "?"
        name = next((v for k, v in DEVICES.items() if model.startswith(k)), model)
        soc = sh(["getprop", "ro.soc.model"])
        oneui = sh(["getprop", "ro.build.version.oneui"])
        android = sh(["getprop", "ro.build.version.release"])
        ram = ""
        try:
            kb = int(next(l for l in open("/proc/meminfo") if l.startswith("MemTotal")).split()[1])
            gb = kb / 1048576   # Android ne riserva una parte: si mostra la taglia commerciale (11,4 → 12 GB)
            ram = f"{next((n for n in (2, 3, 4, 6, 8, 12, 16, 24, 32) if n >= gb), round(gb))} GB"
        except (OSError, StopIteration, ValueError):
            pass
        disk = ""
        try:
            s = os.statvfs("/storage/emulated/0")
            tot, free = s.f_blocks * s.f_frsize, s.f_bavail * s.f_frsize
            disk = T(f"{free / 1e9:.0f} GB liberi su {tot / 1e9:.0f} GB", f"{free / 1e9:.0f} GB free of {tot / 1e9:.0f} GB")
        except OSError:
            pass
        ver = sh(["bash", TOD, "version"]).replace("Termux On DeX ", "")
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=18)
        img = Gtk.Image.new_from_icon_name("computer", Gtk.IconSize.DIALOG)
        img.set_pixel_size(96)
        top.pack_start(img, False, False, 0)
        t = Gtk.Label(label=name)
        t.get_style_context().add_class("pane-title")
        top.pack_start(t, False, False, 0)
        g = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        g.get_style_context().add_class("group")
        g.pack_start(top, False, False, 0)
        box.pack_start(g, False, False, 0)
        rows = [self.row(T("Modello", "Model"), self.value(model))]
        if soc:
            rows.append(self.row(T("Chip", "Chip"), self.value(CHIPS.get(soc, soc))))
        if ram:
            rows.append(self.row(T("Memoria", "Memory"), self.value(ram)))
        if disk:   # come "Impostazioni archiviazione…" di macOS
            spazio = Gtk.Box(spacing=10)
            spazio.pack_start(self.value(disk), False, False, 0)
            spazio.pack_start(self.button(T("Gestisci…", "Manage…"), lambda: self.open_pane("archiviazione")),
                              False, False, 0)
            rows.append(self.row(T("Spazio", "Storage"), spazio))
        if android:
            sysname = f"Android {android}" + (f" · One UI {int(oneui) // 10000}" if oneui.isdigit() else "")
            rows.append(self.row(T("Telefono", "Phone software"), self.value(sysname)))
        rows.append(self.row(T("Desktop", "Desktop"), self.value(f"Termux On DeX {ver}")))
        self.group(box, None, rows)

    def pane_avanzate(self, box):
        hint = Gtk.Label(label=T("Opzioni per chi sa cosa sta facendo: di solito non servono.",
                                 "Options for advanced users: usually not needed."), xalign=0, wrap=True)
        hint.get_style_context().add_class("hint")
        box.pack_start(hint, False, False, 0)
        gpu = conf_get("GPU_PROFILE", "?")
        self.group(box, T("Grafica", "Graphics"), [
            self.row(T("Profilo grafico", "Graphics profile"), self.value(gpu),
                     T("Scelto con una prova reale all'installazione.", "Chosen with a real test at install time.")),
            self.row(T("Centro di controllo", "Control center"),
                     self.button(T("Apri", "Open"), lambda: spawn(["xfce4-terminal", "-x", TOD, "menu"])),
                     T("Profilo GPU, benchmark, DPI e diagnostica.", "GPU profile, benchmark, DPI and diagnostics."))])
        # ADB senza fili (15 novembre): la finestra di collegamento (share/tod-adb.py) e la domanda sulle reti Wi-Fi;
        # riaccenderla dimentica anche le reti "non su questa rete"
        def chiedi_adb(on):
            conf_set("ADB_CHIEDI", "1" if on else "0")
            if on:
                try:
                    os.unlink(os.path.join(HOME, ".config/tod/adb-reti.json"))
                except OSError:
                    pass
        self.group(box, T("ADB senza fili", "Wireless ADB"), [
            self.row(T("Collega ADB", "Connect ADB"),
                     self.button(T("Collega…", "Connect…"), lambda: spawn([sys.executable, os.path.join(HERE, "tod-adb.py")])),
                     T("Per chiudere le app del telefono dalla Gestione attività. Serve il Debug wireless di Android.",
                       "To close phone apps from Task Manager. It needs Android's Wireless debugging.")),
            self.row(T("Chiedi quando ti colleghi a un Wi-Fi", "Ask when you join a Wi-Fi network"),
                     self.switch(conf_get("ADB_CHIEDI", "1") != "0", chiedi_adb),
                     T("Riaccenderla chiede di nuovo anche sulle reti scelte con «Non su questa rete».",
                       "Turning it back on also asks again on networks set to “Not on this network”."))])
        self.group(box, T("Sviluppo", "Development"), [
            self.row(T("Modalità sviluppo", "Developer mode"),
                     self.switch(conf_get("SVILUPPO", "0") == "1", lambda on: tod("sviluppo", "on" if on else "off")),
                     T("Ponte dei comandi di Android e ADB ricollegato all'avvio.", "Android command bridge and ADB reconnect at start."))])
        self.group(box, T("Altro", "Other"), [
            self.row(T("Tutte le impostazioni di XFCE", "All XFCE settings"),
                     self.button(T("Apri", "Open"), lambda: spawn([os.path.join(PREFIX, "bin/xfce4-settings-manager")])))])


# --- Stampanti e scanner: stile dell'elenco, coda di stampa, aggiunta guidata (12 novembre) ------------------------
ST_CSS = b"""
.st-nome { font-weight: 600; font-size: 10.5pt; }
.st-icona { background-color: alpha(@theme_fg_color, 0.06); border-radius: 12px; padding: 9px; }
.st-punto { border-radius: 99px; min-width: 8px; min-height: 8px; }
.p-verde { background-color: #30b94d; } .p-arancio { background-color: #ff9f0a; } .p-blu { background-color: #0a84ff; }
.p-grigio { background-color: alpha(@theme_fg_color, 0.3); }
.st-etichetta { background-color: alpha(@theme_fg_color, 0.09); border-radius: 99px; padding: 1px 9px; font-size: 8pt; }
.st-titolo { font-weight: 700; font-size: 14pt; }
.st-vuoto { opacity: 0.55; font-size: 10.5pt; }
.st-finestra { background-color: @theme_base_color; }
"""
_st_css_fatto = False


def st_css():
    global _st_css_fatto
    if not _st_css_fatto:
        _st_css_fatto = True
        p = Gtk.CssProvider()
        p.load_from_data(ST_CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)


def st_elenco():
    """L'elenco di tod-stampa.py (stampanti, stato, lavori); {} se non risponde."""
    try:
        return json.loads(subprocess.run([sys.executable, STAMPA, "elenco", "--json"], capture_output=True, text=True,
                                         timeout=30, stdin=subprocess.DEVNULL).stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


class Coda(Gtk.Window):
    """La coda di stampa di una stampante, come su Windows e macOS (Pietro, 12 novembre: "ci vorrebbe la finestra per
    gestire la coda di stampa"): i documenti in coda e gli ultimi stampati, Annulla per documento, pausa e ripresa."""

    def __init__(self, madre, nome):
        super().__init__(title=T("Coda di stampa", "Print queue"))
        st_css()
        self.madre, self.nome, self.visto, self.fermo = madre, nome, None, False
        self.set_default_size(560, 440)
        self.set_icon_name("printer")
        self.get_style_context().add_class("st-finestra")
        fuori = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(fuori)
        testa = Gtk.Box(spacing=14)
        for m in ("top", "bottom", "start", "end"):
            getattr(testa, f"set_margin_{m}")(18)
        img = Gtk.Image.new_from_icon_name("printer", Gtk.IconSize.DIALOG)
        img.set_pixel_size(44)
        testa.pack_start(img, False, False, 0)
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, valign=Gtk.Align.CENTER)
        self.l_nome = Gtk.Label(label=nome, xalign=0, ellipsize=3)
        self.l_nome.get_style_context().add_class("st-titolo")
        v.pack_start(self.l_nome, False, False, 0)
        riga = Gtk.Box(spacing=6)
        self.punto = Gtk.Box(valign=Gtk.Align.CENTER)
        self.punto.get_style_context().add_class("st-punto")
        riga.pack_start(self.punto, False, False, 0)
        self.l_stato = Gtk.Label(xalign=0)
        self.l_stato.get_style_context().add_class("hint")
        riga.pack_start(self.l_stato, False, False, 0)
        v.pack_start(riga, False, False, 0)
        testa.pack_start(v, True, True, 0)
        self.b_pausa = Gtk.Button(label=T("Metti in pausa", "Pause"), valign=Gtk.Align.CENTER)
        self.b_pausa.connect("clicked", lambda *_: self.comando("riprendi" if self.fermo else "pausa", self.nome))
        testa.pack_end(self.b_pausa, False, False, 0)
        fuori.pack_start(testa, False, False, 0)
        fuori.pack_start(Gtk.Separator(), False, False, 0)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.lista = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sw.add(self.lista)
        fuori.pack_start(sw, True, True, 0)
        fuori.pack_start(Gtk.Separator(), False, False, 0)
        fondo = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(fondo, f"set_margin_{m}")(12)
        self.b_tutti = Gtk.Button(label=T("Annulla tutti", "Cancel all"))
        self.b_tutti.connect("clicked", lambda *_: self.comando("annulla", self.nome))
        fondo.pack_start(self.b_tutti, False, False, 0)
        prova = Gtk.Button(label=T("Pagina di prova", "Test page"))
        prova.connect("clicked", lambda *_: self.comando("prova", self.nome))
        fondo.pack_start(prova, False, False, 0)
        chiudi = Gtk.Button(label=T("Chiudi", "Close"))
        chiudi.connect("clicked", lambda *_: self.destroy())
        fondo.pack_end(chiudi, False, False, 0)
        fuori.pack_start(fondo, False, False, 0)
        self.giro = GLib.timeout_add_seconds(2, self.aggiorna)
        self.connect("destroy", lambda *_: GLib.source_remove(self.giro))
        self.aggiorna()
        fuori.show_all()

    def comando(self, *args):
        self.madre.stampa(*args, then=lambda *_: self.aggiorna())

    def aggiorna(self):
        def carica():
            e = st_elenco()
            GLib.idle_add(lambda: self.mostra(e) and False)
        threading.Thread(target=carica, daemon=True).start()
        return True

    def riga(self, titolo, sotto, pulsante=None, spento=False):
        h = Gtk.Box(spacing=12)
        h.get_style_context().add_class("group-row")
        img = Gtk.Image.new_from_icon_name("x-office-document", Gtk.IconSize.DND)
        img.set_pixel_size(28)
        h.pack_start(img, False, False, 0)
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        v.pack_start(Gtk.Label(label=titolo, xalign=0, ellipsize=3), False, False, 0)
        s = Gtk.Label(label=sotto, xalign=0)
        s.get_style_context().add_class("hint")
        v.pack_start(s, False, False, 0)
        h.pack_start(v, True, True, 0)
        if pulsante is not None:
            pulsante.set_valign(Gtk.Align.CENTER)
            h.pack_end(pulsante, False, False, 0)
        if spento:
            h.set_opacity(0.6)
        return h

    def mostra(self, e):
        p = next((x for x in e.get("stampanti", []) if x["nome"] == self.nome), None)
        if p is None:
            if e.get("servizio"):   # la stampante è stata tolta
                self.destroy()
            return
        impronta = json.dumps(p, sort_keys=True)
        if impronta == self.visto:
            return
        self.visto = impronta
        punto, frase = self.madre.stato_stampante(p)
        self.l_nome.set_text(p["descrizione"])
        self.l_stato.set_text(frase)
        ctx = self.punto.get_style_context()
        for c in ("p-verde", "p-arancio", "p-blu", "p-grigio"):
            ctx.remove_class(c)
        ctx.add_class("p-" + punto)
        self.fermo = not p["attiva"]
        self.b_pausa.set_label(T("Riprendi", "Resume") if self.fermo else T("Metti in pausa", "Pause"))
        self.b_tutti.set_sensitive(bool(p["lavori"]))
        for c in self.lista.get_children():
            self.lista.remove(c)
        for i, lav in enumerate(p["lavori"]):
            numero = lav["id"].rsplit("-", 1)[1]
            dove = (T("In stampa", "Printing") if i == 0 and p["stato"] == "stampa" and p["attiva"]
                    else T("In pausa", "Paused") if self.fermo else T("In attesa", "Waiting"))
            b = Gtk.Button(label=T("Annulla", "Cancel"))
            b.connect("clicked", lambda _w, j=lav["id"]: self.comando("annulla", j))
            self.lista.pack_start(self.riga(lav.get("titolo") or T(f"Documento {numero}", f"Document {numero}"),
                                            f"{dove} · {max(1, lav.get('byte', 0) // 1024)} KB", b), False, False, 0)
            self.lista.pack_start(Gtk.Separator(), False, False, 0)
        if not p["lavori"]:
            vuoto = Gtk.Label(label=T("Nessun documento in coda", "No documents in the queue"))
            vuoto.get_style_context().add_class("st-vuoto")
            vuoto.set_margin_top(28)
            vuoto.set_margin_bottom(20)
            self.lista.pack_start(vuoto, False, False, 0)
        for lav in p.get("fatti", []):
            numero = lav["id"].rsplit("-", 1)[1]
            self.lista.pack_start(self.riga(T(f"Documento {numero}", f"Document {numero}"),
                                            T("Inviato alla stampante", "Sent to the printer") + " · " + lav.get("quando", ""),
                                            spento=True), False, False, 0)
        self.lista.show_all()


class AggiuntaGuidata(Gtk.Window):
    """Aggiungere una stampante o uno scanner in tre passi (Pietro, 12 novembre: "un'installazione guidata"): le trovate
    in rete (o l'indirizzo), la configurazione, la pagina di prova."""

    def __init__(self, madre):
        super().__init__(title=T("Aggiungi stampante o scanner", "Add printer or scanner"))
        st_css()
        self.madre = madre
        self.set_transient_for(madre)
        self.set_modal(True)
        self.set_default_size(520, 460)
        self.set_icon_name("printer")
        self.get_style_context().add_class("st-finestra")
        self.pile = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.add(self.pile)
        # 1. scegli
        p1 = self.pagina()
        p1.pack_start(self.titolo(T("Quale vuoi aggiungere?", "Which one do you want to add?"),
                                  T("Accendila e collegala alla stessa rete Wi-Fi di questo dispositivo.",
                                    "Turn it on and connect it to the same Wi-Fi network as this device.")), False, False, 0)
        self.trovate = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.trovate.get_style_context().add_class("group")
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.add(self.trovate)
        p1.pack_start(sw, True, True, 0)
        riga = Gtk.Box(spacing=8)
        self.indirizzo = Gtk.Entry(placeholder_text=T("Non c'è? Scrivi il suo indirizzo: 192.168.1.50", "Not there? Type its address: 192.168.1.50"))
        self.indirizzo.connect("activate", lambda w: self.aggiungi(w.get_text().strip(), w.get_text().strip()))
        riga.pack_start(self.indirizzo, True, True, 0)
        b = Gtk.Button(label=T("Aggiungi", "Add"))
        b.connect("clicked", lambda *_: self.aggiungi(self.indirizzo.get_text().strip(), self.indirizzo.get_text().strip()))
        riga.pack_start(b, False, False, 0)
        p1.pack_start(riga, False, False, 0)
        fondo = Gtk.Box(spacing=8)
        ancora = Gtk.Button(label=T("Cerca di nuovo", "Search again"))
        ancora.connect("clicked", lambda *_: self.cerca())
        fondo.pack_start(ancora, False, False, 0)
        chiudi = Gtk.Button(label=T("Chiudi", "Close"))
        chiudi.connect("clicked", lambda *_: self.destroy())
        fondo.pack_end(chiudi, False, False, 0)
        p1.pack_start(fondo, False, False, 0)
        self.pile.add_named(p1, "scegli")
        # 2. configurazione in corso
        p2 = self.pagina()
        centro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, valign=Gtk.Align.CENTER)
        gira = Gtk.Spinner(active=True)
        gira.set_size_request(40, 40)
        centro.pack_start(gira, False, False, 0)
        self.l_lavoro = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.l_lavoro.get_style_context().add_class("st-nome")
        centro.pack_start(self.l_lavoro, False, False, 0)
        sotto = Gtk.Label(label=T("Chiedo al dispositivo cosa sa fare e scelgo il driver. Qualche secondo.",
                                  "Asking the device what it can do and choosing the driver. A few seconds."), wrap=True,
                          justify=Gtk.Justification.CENTER)
        sotto.get_style_context().add_class("hint")
        centro.pack_start(sotto, False, False, 0)
        p2.pack_start(centro, True, True, 0)
        self.pile.add_named(p2, "lavoro")
        # 3. fatto, o no
        p3 = self.pagina()
        centro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        self.i_esito = Gtk.Image()
        centro.pack_start(self.i_esito, False, False, 0)
        self.l_esito = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.l_esito.get_style_context().add_class("st-titolo")
        centro.pack_start(self.l_esito, False, False, 0)
        self.l_dettaglio = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=52)
        self.l_dettaglio.get_style_context().add_class("hint")
        centro.pack_start(self.l_dettaglio, False, False, 0)
        p3.pack_start(centro, True, True, 0)
        fondo = Gtk.Box(spacing=8)
        self.b_indietro = Gtk.Button(label=T("Indietro", "Back"))
        self.b_indietro.connect("clicked", lambda *_: self.pile.set_visible_child_name("scegli"))
        fondo.pack_start(self.b_indietro, False, False, 0)
        fine = Gtk.Button(label=T("Fine", "Done"))
        fine.get_style_context().add_class("suggested-action")
        fine.connect("clicked", lambda *_: self.destroy())
        fondo.pack_end(fine, False, False, 0)
        self.b_prova = Gtk.Button(label=T("Stampa una pagina di prova", "Print a test page"))
        self.b_prova.connect("clicked", lambda *_: self.prova())
        fondo.pack_end(self.b_prova, False, False, 0)
        p3.pack_start(fondo, False, False, 0)
        self.pile.add_named(p3, "esito")
        self.pile.show_all()
        self.connect("destroy", lambda *_: (madre.fill_stampanti(""), madre.fill_scanner("")))
        self.cerca()

    def pagina(self):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for m in ("top", "bottom", "start", "end"):
            getattr(b, f"set_margin_{m}")(22)
        return b

    def titolo(self, testo, sotto):
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        t = Gtk.Label(label=testo, xalign=0)
        t.get_style_context().add_class("st-titolo")
        v.pack_start(t, False, False, 0)
        s = Gtk.Label(label=sotto, xalign=0, wrap=True)
        s.get_style_context().add_class("hint")
        v.pack_start(s, False, False, 0)
        return v

    def riempi(self, righe):
        for c in self.trovate.get_children():
            self.trovate.remove(c)
        for i, r in enumerate(righe):
            if i:
                self.trovate.pack_start(Gtk.Separator(), False, False, 0)
            self.trovate.pack_start(r, False, False, 0)
        self.trovate.show_all()

    def cerca(self):
        self.pile.set_visible_child_name("scegli")
        self.riempi([self.madre.row(T("Cerco nella rete…", "Searching the network…"), Gtk.Spinner(active=True))])

        def lavoro():
            try:
                t = subprocess.run([sys.executable, STAMPA, "cerca", "--json"], capture_output=True, text=True, timeout=60,
                                   stdin=subprocess.DEVNULL).stdout
                trovate = json.loads(t.splitlines()[-1]) if t.strip() else []
            except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                trovate = []
            e = st_elenco()
            GLib.idle_add(lambda: self.mostra_trovate(trovate, e) and False)
        threading.Thread(target=lavoro, daemon=True).start()

    def mostra_trovate(self, trovate, e):
        installati = {re.sub(r"^\w+://([^/:]+).*", r"\1", p["uri"]) for p in e.get("stampanti", [])}
        righe = []
        for t in trovate:
            gia = t["ip"] in installati
            b = Gtk.Button(label=T("Aggiungi", "Add"), sensitive=not gia)
            if not gia:
                b.get_style_context().add_class("suggested-action")
            b.connect("clicked", lambda _w, x=t: self.aggiungi(x["chiave"], x["modello"]))
            righe.append(self.madre.riga_dispositivo(
                "printer", t["modello"], "verde" if not gia else "grigio",
                (T("Già aggiunta", "Already added") if gia else "Wi-Fi") + " · " + t["ip"], None, (b,)))
        if not righe:
            righe.append(self.madre.row(T("Nessuna stampante nuova in rete", "No new printers on the network"), None,
                                        T("Se è accesa e collegata, scrivi qui sotto il suo indirizzo (lo trovi nel menu "
                                          "Rete della stampante).", "If it is on and connected, type its address below "
                                          "(you'll find it in the printer's Network menu).")))
        self.riempi(righe)

    def aggiungi(self, cosa, nome):
        if not cosa:
            return
        self.aggiunta = None
        self.l_lavoro.set_text(T(f"Configuro {nome}…", f"Setting up {nome}…"))
        self.pile.set_visible_child_name("lavoro")

        def lavoro():
            prima = {p["nome"] for p in st_elenco().get("stampanti", [])}
            try:
                r = subprocess.run([sys.executable, STAMPA, "aggiungi", cosa], capture_output=True, text=True, timeout=200,
                                   stdin=subprocess.DEVNULL)
                cod, testo = r.returncode, (r.stdout + r.stderr).strip()
            except (OSError, subprocess.TimeoutExpired) as e:
                cod, testo = 1, str(e)
            nuova = next((p for p in st_elenco().get("stampanti", []) if p["nome"] not in prima), None)
            scanner = None
            if re.match(r"^[\w.\-:]+$", cosa) and "." in cosa:   # un indirizzo: può essere (anche) uno scanner
                try:
                    rs = subprocess.run([sys.executable, SCANNER, "aggiungi", cosa], capture_output=True, text=True,
                                        timeout=60, stdin=subprocess.DEVNULL)
                    scanner = rs.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    scanner = False
            GLib.idle_add(lambda: self.esito(cod, testo, nuova, scanner, nome) and False)
        threading.Thread(target=lavoro, daemon=True).start()

    def esito(self, cod, testo, nuova, scanner, nome):
        bene = cod == 0 or scanner
        self.aggiunta = nuova["nome"] if nuova else None
        self.i_esito.set_from_icon_name("emblem-ok-symbolic" if bene else "dialog-warning-symbolic", Gtk.IconSize.DIALOG)
        self.i_esito.set_pixel_size(48)
        if cod == 0:
            self.l_esito.set_text(T(f"{nuova['descrizione'] if nuova else nome} è pronta",
                                    f"{nuova['descrizione'] if nuova else nome} is ready"))
            self.l_dettaglio.set_text(T("La trovi nella finestra di stampa di ogni app.", "It's in every app's print dialog.")
                                      + (T(" Anche il suo scanner è stato aggiunto.", " Its scanner was added too.") if scanner else ""))
        elif scanner:
            self.l_esito.set_text(T("Scanner aggiunto", "Scanner added"))
            self.l_dettaglio.set_text(T("Lo trovi nell'app Scanner. Come stampante non è stato possibile: ",
                                        "It's in the Scanner app. As a printer it could not be added: ") + testo)
        else:
            self.l_esito.set_text(T("Non sono riuscito ad aggiungerla", "Couldn't add it"))
            self.l_dettaglio.set_text(testo)
        self.b_prova.set_visible(cod == 0 and bool(self.aggiunta))
        self.b_indietro.set_visible(not bene)
        self.pile.set_visible_child_name("esito")

    def prova(self):
        self.b_prova.set_sensitive(False)
        self.madre.stampa("prova", self.aggiunta, then=lambda c, t: self.l_dettaglio.set_text(
            T("Pagina di prova inviata: tra poco esce dalla stampante.", "Test page sent: it will come out shortly.")
            if c == 0 else t))


def main():
    # una finestra sola, come macOS: aprirle di nuovo (es. dal tasto destro sulla scrivania)
    # riporta davanti quella già aperta, sulla voce chiesta
    app = Gtk.Application(application_id="com.termuxondex.Settings",
                          flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)

    def on_command_line(a, cmd):
        args = cmd.get_arguments()[1:]
        key = args[0] if args else None
        win = a.get_active_window()
        if win is None:
            win = Settings(key, a)
        else:
            win.open_pane(key)
        win.present()
        return 0
    app.connect("command-line", on_command_line)
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
