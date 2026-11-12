#!/usr/bin/env python3
"""Impostazioni, come quelle di macOS: barra laterale con le voci, a destra il pannello.

  tod-settings.py [voce]        voce: rete, batteria, aspetto, schermo, suono, notifiche, telefono,
                                tastiera, stampanti, app, aggiornamenti, info, avanzate

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
LAYOUTS = [("it", "Italiano"), ("us", T("Inglese (USA)", "English (US)")), ("gb", T("Inglese (Regno Unito)", "English (UK)")),
           ("de", T("Tedesco", "German")), ("fr", T("Francese", "French")), ("es", T("Spagnolo", "Spanish")),
           ("ch", T("Svizzero", "Swiss")), ("pt", T("Portoghese", "Portuguese"))]


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
        ("tastiera", T("Tastiera e mouse", "Keyboard & Mouse"), "input-keyboard-symbolic", "gray",
         "layout lingua scorciatoie mouse puntatore"),
        ("stampanti", T("Stampanti e scanner", "Printers & Scanners"), "printer-symbolic", "gray",
         "stampante stampare stampa scanner usb wifi pdf coda lavori driver"),
        ("app", T("App", "Apps"), "applications-system-symbolic", "orange",
         "app installate disinstalla rimuovi elimina predefinite browser apri con file"),
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
        self.show_all()
        self.sidebar.select_row(self.rows.get(first) or self.rows["aspetto"])

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

    def pane_tastiera(self, box):
        cur = xq("keyboard-layout", "/Default/XkbLayout") or "it"
        combo = Gtk.ComboBoxText()
        for code, name in LAYOUTS:
            combo.append(code, name)
        if not combo.set_active_id(cur.split(",")[0]):
            combo.append(cur, cur)
            combo.set_active_id(cur)
        combo.connect("changed", lambda c: self.set_layout(c.get_active_id()))
        self.group(box, T("Tastiera", "Keyboard"), [
            self.row(T("Lingua della tastiera", "Keyboard layout"), combo),
            self.row(T("Scorciatoie da tastiera", "Keyboard shortcuts"),
                     self.button(T("Apri", "Open"), lambda: spawn(["xfce4-keyboard-settings"])))])
        self.group(box, T("Mouse e touchpad", "Mouse & touchpad"), [
            self.row(T("Velocità e pulsanti", "Speed and buttons"),
                     self.button(T("Apri", "Open"), lambda: spawn(["xfce4-mouse-settings"])))])

    def set_layout(self, code):
        if not code:
            return
        xq("keyboard-layout", "/Default/XkbDisable", "-t", "bool", "-s", "false")
        xq("keyboard-layout", "/Default/XkbLayout", "-t", "string", "-s", code)
        xq("keyboard-layout", "/Default/XkbVariant", "-t", "string", "-s", "")
        spawn(["setxkbmap", code])

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
        if disk:
            rows.append(self.row(T("Spazio", "Storage"), self.value(disk)))
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
