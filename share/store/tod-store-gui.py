#!/usr/bin/env python3
"""Store: le app di Flathub per aarch64, come l'App Store di macOS, nei colori di DeX OS.

  tod-store-gui.py [id]          (tod store, tod store finestra [id])

Come l'App Store (Pietro, 9 novembre: "la grafica è troppo Linux, la vorrei tipo l'App Store"): barra laterale con
la ricerca e Scopri, Crea, Lavora, Gioca, Sviluppa, Categorie, Installate (icone nell'accento, voce scelta in
grigio); a destra fondo pieno, schede grandi fatte con le schermate delle app, scaffali di app senza riquadri,
pulsanti a capsula "Ottieni" / "Apri", l'anello che si riempie mentre scarica; la pagina dell'app con la striscia
delle informazioni, le schermate, la descrizione, le novità della versione.

Installa, Apri e Rimuovi passano da "tod store" (lib/store.sh), letto da un terminale finto: così flatpak e apt
danno la percentuale (TOD_STORE_AVANZAMENTO=1). L'app arriva da Termux, se no da Debian, se no da Flathub; quelle
che il sistema ha già si aprono da qui ("Apri"). Catalogo: share/store/catalogo.py. Gira in Termux (GTK 3).
"""
import fcntl
import glob
import hashlib
import math
import os
import pty
import re
import struct
import subprocess
import sys
import termios
import threading
import time
import urllib.request

import gi

try:   # la sfumatura sotto il testo delle copertine (pycairo): senza, solo la schermata
    import cairo
except ImportError:
    cairo = None

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")   # senza, Termux carica Gdk 4 e GTK 3 non parte
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

try:   # GLib 2.86: le funzioni Unix di Gio stanno in GioUnix
    gi.require_version("GioUnix", "2.0")
    from gi.repository import GioUnix
    DesktopAppInfo = GioUnix.DesktopAppInfo
except (ValueError, ImportError):
    DesktopAppInfo = Gio.DesktopAppInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import catalogo  # noqa: E402

GLib.set_prgname("tod-store")   # la barra riconosce la finestra (tod-store.desktop)
GLib.set_application_name("Store")

PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
CASA = os.environ.get("TOD_TERMUX_HOME") or os.environ.get("HOME", "/data/data/com.termux/files/home")
TOD = os.environ.get("TOD_STORE_COMANDO") or os.path.join(PREFIX, "bin/tod")
TOD_ROOT = os.path.dirname(os.path.dirname(HERE))
STORE = os.environ.get("TOD_STORE_DIR") or os.path.join(CASA, ".local/share/tod-store")
LINGUA = (os.environ.get("LANGUAGE") or os.environ.get("LANG") or "it")[:2]
ITALIANO = LINGUA == "it"


def T(it, en):
    return it if ITALIANO else en


# categorie principali di freedesktop → nome e icona simbolica (nell'accento: niente colori fissi)
CATEGORIE = {
    "Office": (T("Produttività", "Productivity"), "document-edit-symbolic"),
    "Graphics": (T("Grafica e foto", "Graphics & Photos"), "image-x-generic-symbolic"),
    "AudioVideo": (T("Audio e video", "Audio & Video"), "media-playback-start-symbolic"),
    "Network": (T("Internet e comunicazione", "Internet & Communication"), "web-browser-symbolic"),
    "Game": (T("Giochi", "Games"), "input-gaming-symbolic"),
    "Development": (T("Sviluppo", "Development"), "utilities-terminal-symbolic"),
    "Education": (T("Istruzione", "Education"), "x-office-presentation-symbolic"),
    "Science": (T("Scienza", "Science"), "applications-science-symbolic"),
    "Utility": (T("Utilità", "Utilities"), "applications-utilities-symbolic"),
    "System": (T("Sistema", "System"), "applications-system-symbolic"),
}
# le voci della barra laterale, come l'App Store
SEZIONI = [
    ("scopri", T("Scopri", "Discover"), "starred-symbolic", None),
    ("crea", T("Crea", "Create"), "color-select-symbolic", ["Graphics", "AudioVideo"]),
    ("lavora", T("Lavora", "Work"), "mail-send-symbolic", ["Office", "Network"]),
    ("gioca", T("Gioca", "Play"), "input-gaming-symbolic", ["Game"]),
    ("sviluppa", T("Sviluppa", "Develop"), "utilities-terminal-symbolic", ["Development"]),
    ("categorie", T("Categorie", "Categories"), "view-app-grid-symbolic", None),
]
# in evidenza: app note; si mostrano solo quelle che Flathub ha per aarch64 e che il sistema non ha già
CONSIGLIATE = [
    "org.kde.krita", "org.inkscape.Inkscape", "org.libreoffice.LibreOffice", "org.videolan.VLC",
    "com.obsproject.Studio", "org.audacityteam.Audacity", "org.kde.kdenlive", "org.shotcut.Shotcut",
    "org.telegram.desktop", "org.mozilla.Thunderbird", "org.darktable.Darktable", "org.freecad.FreeCAD",
    "org.kicad.KiCad", "com.github.xournalpp.xournalpp", "org.musescore.MuseScore", "org.kde.okular",
    "com.github.johnfactotum.Foliate", "de.haeckerfelix.Shortwave", "com.rafaelmardojai.Blanket",
    "app.drey.Dialect", "io.github.alainm23.planify", "net.supertuxkart.SuperTuxKart", "org.qbittorrent.qBittorrent",
    "com.boxy_svg.BoxySVG", "org.gimp.GIMP", "org.mozilla.firefox", "org.kde.kwrite",
]

CSS = """
.store { background-color: CONTENUTO; font-family: Inter, sans-serif; color: TESTO; }
.store .lato { background-color: LATO; border-right: 1px solid LINEA; }
.store .lato list, .store .lato row { background: transparent; }
.store .lato row { border-radius: 7px; margin: 0 10px; padding: 6px 8px; }
.store .lato row:hover { background-color: PASSAGGIO; }
.store .lato row:selected { background-color: SCELTA; }
.store .lato row image { color: ACCTESTO; }
.store .lato row label, .store .lato row:selected label { color: TESTO; }
.store .gruppo { font-weight: 600; font-size: 8.5pt; color: TENUE; }
.store entry.cerca {
  background-color: CAMPO; background-image: none; border: none; border-radius: 8px; box-shadow: none;
  padding: 0 8px; min-height: 30px; color: TESTO;
}
.store entry.cerca:focus { box-shadow: 0 0 0 3px alpha(ACCENTO, 0.35); }
.store .titolo-pagina { font-size: 22pt; font-weight: 700; }
.store .sezione { font-size: 14pt; font-weight: 700; }
.store .nome { font-size: 10.5pt; font-weight: 600; }
.store .sotto { font-size: 9pt; color: TENUE; }
.store .grande { font-size: 22pt; font-weight: 700; }
.store .sottotitolo { font-size: 12pt; color: TENUE; }
.store .testo { font-size: 10pt; }
.store .riga { padding: 10px 0; border-bottom: 1px solid LINEA; }
.store flowboxchild { padding: 0; background: transparent; }
.store button.ottieni {
  background-color: CAPSULA; background-image: none; border: none; box-shadow: none; border-radius: 14px;
  padding: 3px 14px; min-height: 0; min-width: 0; color: ACCTESTO; font-size: 9.5pt; font-weight: 700;
  text-shadow: none; outline: none;
}
.store button.ottieni label { color: ACCTESTO; }
.store button.ottieni:hover { background-color: CAPSULASOPRA; }
.store button.ottieni:disabled label { color: TENUE; }
.store button.ottieni.bianco { background-color: rgba(255, 255, 255, 0.92); }
.store button.ottieni.bianco label { color: ACCENTO; }
.store button.collegamento {
  background: none; background-image: none; border: none; box-shadow: none; padding: 2px 2px; min-height: 0;
  min-width: 0; color: ACCTESTO; font-weight: 500; text-shadow: none; outline: none;
}
.store button.collegamento label, .store button.collegamento image { color: ACCTESTO; }
.store button.collegamento.pericolo label { color: #e0443a; }
.store .colonna { padding: 2px 22px; border-left: 1px solid LINEA; }
.store .colonna.prima { border-left: none; padding-left: 0; }
.store .etic { font-size: 7.5pt; font-weight: 700; color: TENUE; }
.store .valore { font-size: 13pt; font-weight: 700; color: TESTO2; }
.store .piccolo { font-size: 8.5pt; color: TENUE; }
.store .banner-marchio { font-size: 8pt; font-weight: 700; color: rgba(255, 255, 255, 0.85); }
.store .banner-nome { font-size: 17pt; font-weight: 700; color: #ffffff; }
.store .banner-sotto { font-size: 9.5pt; color: rgba(255, 255, 255, 0.88); }
.store .cat { background-color: CAPSULA; border-radius: 12px; padding: 12px 14px; }
.store .cat:hover { background-color: CAPSULASOPRA; }
.store .icona-cat { background-color: alpha(ACCENTO, 0.14); border-radius: 9px; min-width: 36px; min-height: 36px; color: ACCTESTO; }
.store .nota { background-color: CAPSULA; border-radius: 12px; padding: 12px 16px; }
.store .avanz-testo { font-size: 9pt; color: TENUE; }
.store progressbar trough { min-height: 5px; border-radius: 3px; background-color: CAPSULA; border: none; }
.store progressbar progress { min-height: 5px; border-radius: 3px; background-color: ACCENTO; border: none; }
.store separator { background-color: LINEA; min-height: 1px; }
.store scrollbar { background: transparent; border: none; }
.store scrollbar slider { min-width: 4px; background-color: alpha(TESTO, 0.3); border-radius: 3px; border: none; }
"""
COLORI = {   # fondi pieni (DeX OS: niente vetro)
    False: dict(CONTENUTO="#ffffff", LATO="#f2f2f5", LINEA="rgba(0, 0, 0, 0.08)", TESTO="#1d1d1f",
                TESTO2="#3c3c43", TENUE="#86868b", PASSAGGIO="rgba(0, 0, 0, 0.04)", SCELTA="rgba(0, 0, 0, 0.08)",
                CAMPO="rgba(0, 0, 0, 0.06)", CAPSULA="#f0f0f3", CAPSULASOPRA="#e6e6ea"),
    True: dict(CONTENUTO="#1e1e21", LATO="#28282c", LINEA="rgba(255, 255, 255, 0.09)", TESTO="#f5f5f7",
               TESTO2="#d1d1d6", TENUE="#98989d", PASSAGGIO="rgba(255, 255, 255, 0.05)",
               SCELTA="rgba(255, 255, 255, 0.10)", CAMPO="rgba(255, 255, 255, 0.08)", CAPSULA="#2f2f34",
               CAPSULASOPRA="#3a3a40"),
}
SCALA = 1.0


def px(n):
    return int(round(n * SCALA))


def accento():
    a = "#0067c0"
    try:
        for r in open(os.path.join(CASA, ".config/tod/tod.conf")):
            if r.startswith("ACCENTO="):
                v = r.split("=", 1)[1].strip().strip('"')
                if re.fullmatch(r"#[0-9a-fA-F]{6}", v):
                    a = v
    except OSError:
        pass
    r, g, b = (int(a[i:i + 2], 16) for i in (1, 3, 5))
    chiaro = "#%02x%02x%02x" % tuple(int(c + (255 - c) * 0.45) for c in (r, g, b))
    return a, chiaro


def scuro():
    return "dark" in (Gtk.Settings.get_default().get_property("gtk-theme-name") or "").lower()


def colore_accento():
    """l'accento per i disegni (anello): nello scuro quello schiarito, come il testo."""
    a, chiaro = accento()
    h = chiaro if scuro() else a
    return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))


def css():
    a, chiaro = accento()
    s = scuro()
    c = dict(COLORI[s], ACCENTO=a, ACCTESTO=chiaro if s else a)
    out = CSS
    for k in sorted(c, key=len, reverse=True):
        out = out.replace(k, c[k])
    return re.sub(r"(\d+(?:\.\d+)?)px", lambda m: "%dpx" % max(1, px(float(m.group(1)))), out).encode()


def nome_runtime(ref):
    p = ref.split("/")
    base = {"org.gnome.Platform": "GNOME", "org.kde.Platform": "KDE", "org.freedesktop.Platform": "Freedesktop"}
    return "%s %s" % (base.get(p[0], p[0].rsplit(".", 1)[-1]), p[-1] if len(p) == 3 else "")


def misura(b):
    for u in ("B", "kB", "MB", "GB"):
        if b < 1000 or u == "GB":
            return ("%.0f %s" if u in ("B", "kB") or b >= 100 else "%.1f %s") % (b, u)
        b /= 1000.0
    return ""


def etichetta(testo, classe=None, righe=0, xalign=0.0, ellissi=False, larga=0):
    """larga: caratteri della larghezza naturale (senza, un testo lungo chiede tutta la riga)."""
    l = Gtk.Label(label=testo, xalign=xalign)
    if larga or righe or ellissi:
        l.set_max_width_chars(larga or (60 if righe else 30))
    for c in (classe or "").split():
        l.get_style_context().add_class(c)
    if righe:
        l.set_line_wrap(True)
        l.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        if righe > 0:
            l.set_lines(righe)
            l.set_ellipsize(Pango.EllipsizeMode.END)
    elif ellissi:
        l.set_ellipsize(Pango.EllipsizeMode.END)
    return l


def classe(w, *nomi):
    for n in nomi:
        w.get_style_context().add_class(n)
    return w


def cliccabile(w, azione):
    ev = Gtk.EventBox()
    ev.add(w)
    ev.connect("button-release-event", lambda *_: azione())
    ev.connect("realize", lambda e: e.get_window().set_cursor(
        Gdk.Cursor.new_from_name(e.get_display(), "pointer")))
    return ev


_icone = {}


def icona(a, misura_px):
    """l'icona dell'app (già scaricata col catalogo), scalata; se manca, quella generica del tema."""
    k = (a.get("icona"), misura_px)
    if k not in _icone:
        pb = None
        if a.get("icona"):
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(a["icona"], misura_px, misura_px, True)
            except GLib.Error:
                pb = None
        if pb is None:
            try:
                pb = Gtk.IconTheme.get_default().load_icon("application-x-executable", misura_px, 0)
            except GLib.Error:
                pb = None
        _icone[k] = pb
    img = Gtk.Image.new_from_pixbuf(_icone[k]) if _icone[k] else Gtk.Image()
    img.set_size_request(misura_px, misura_px)
    img.set_valign(Gtk.Align.CENTER)
    return img


def scarica(url, then):
    """un'immagine (schermata) in cache, in sottofondo; then(percorso o None) nella UI."""
    cache = os.path.join(STORE, "cache", "schermate")
    p = os.path.join(cache, hashlib.sha1(url.encode()).hexdigest() + os.path.splitext(url)[1][:5])

    def lavoro():
        ok = os.path.exists(p)
        if not ok:
            try:
                os.makedirs(cache, exist_ok=True)
                r = urllib.request.Request(url, headers={"User-Agent": "TermuxOnDeX-Store/1"})
                with urllib.request.urlopen(r, timeout=20) as f:
                    dati = f.read()
                with open(p + ".tmp", "wb") as f:
                    f.write(dati)
                os.replace(p + ".tmp", p)
                ok = True
            except (OSError, ValueError):
                ok = False
        GLib.idle_add(lambda: then(p if ok else None) and False)
    threading.Thread(target=lavoro, daemon=True).start()


def arrotonda(cr, w, h, r):
    cr.new_sub_path()
    for cx, cy, a1 in ((w - r, r, -90), (w - r, h - r, 0), (r, h - r, 90), (r, r, 180)):
        cr.arc(cx, cy, r, math.radians(a1), math.radians(a1 + 90))
    cr.close_path()


class Immagine(Gtk.DrawingArea):
    """un'immagine con gli angoli arrotondati: schermata (alta h, larga quanto serve) o copertina (riempie tutto,
    con la sfumatura scura in basso per il testo bianco)."""

    def __init__(self, h, copertina=False):
        super().__init__()
        self.pb, self.sorgente, self.h, self.copertina = None, None, h, copertina
        if copertina:
            self.set_size_request(-1, h)
        else:
            self.set_size_request(int(h * 16 / 10), h)
        self.connect("draw", self.disegna)

    def metti(self, percorso):
        try:
            if self.copertina:
                self.sorgente = GdkPixbuf.Pixbuf.new_from_file(percorso)
            else:
                self.pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(percorso, -1, self.h, True)
                self.set_size_request(self.pb.get_width(), self.h)
        except GLib.Error:
            return
        self.queue_draw()

    def disegna(self, w, cr):
        a = w.get_allocation()
        arrotonda(cr, a.width, a.height, px(14))
        cr.clip()
        r, g, b = colore_accento()
        cr.set_source_rgb(r * 0.55, g * 0.55, b * 0.55)
        cr.paint()
        if self.copertina and self.sorgente:
            sw, sh = self.sorgente.get_width(), self.sorgente.get_height()
            s = max(a.width / sw, a.height / sh)
            if not self.pb or self.pb.get_width() != int(sw * s) or self.pb.get_height() != int(sh * s):
                self.pb = self.sorgente.scale_simple(max(1, int(sw * s)), max(1, int(sh * s)),
                                                     GdkPixbuf.InterpType.BILINEAR)
            Gdk.cairo_set_source_pixbuf(cr, self.pb, (a.width - self.pb.get_width()) / 2, 0)
            cr.paint()
        elif self.pb:
            Gdk.cairo_set_source_pixbuf(cr, self.pb, 0, 0)
            cr.paint()
        if self.copertina and cairo:   # sfumatura per il testo bianco
            gr = cairo.LinearGradient(0, a.height * 0.35, 0, a.height)
            gr.add_color_stop_rgba(0, 0, 0, 0, 0)
            gr.add_color_stop_rgba(1, 0, 0, 0, 0.72)
            cr.set_source(gr)
            cr.paint()


class Anello(Gtk.DrawingArea):
    """l'avanzamento come l'App Store: un anello che si riempie (senza percentuale: un arco che gira)."""

    def __init__(self, d):
        super().__init__()
        self.frazione, self.giro = None, 0.0
        self.set_size_request(d, d)
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self.disegna)
        self.timer = GLib.timeout_add(40, self.passo)
        self.connect("destroy", lambda *_: GLib.source_remove(self.timer))

    def passo(self):
        if self.frazione is None and self.get_mapped():
            self.giro = (self.giro + 0.18) % (2 * math.pi)
            self.queue_draw()
        return True

    def metti(self, frazione):
        self.frazione = frazione
        self.queue_draw()

    def disegna(self, w, cr):
        a = w.get_allocation()
        d = min(a.width, a.height)
        cx, cy, r = a.width / 2, a.height / 2, d / 2 - px(2)
        rr, gg, bb = colore_accento()
        cr.set_line_width(max(2, px(2.5)))
        cr.set_source_rgba(rr, gg, bb, 0.22)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()
        cr.set_source_rgb(rr, gg, bb)
        if self.frazione is None:
            cr.arc(cx, cy, r, self.giro, self.giro + 1.6)
        else:
            cr.arc(cx, cy, r, -math.pi / 2, -math.pi / 2 + 2 * math.pi * max(0.02, min(1, self.frazione)))
        cr.stroke()
        q = d * 0.18   # il quadratino al centro, come l'App Store
        cr.rectangle(cx - q / 2, cy - q / 2, q, q)
        cr.fill()


class Lavoro:
    """tod store installa|rimuovi in un terminale finto: flatpak e apt danno la percentuale."""
    ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
    PEZZO = re.compile(r"^\s*(\d+)\.\s+(\S+)\s+\S+\s+\S+\s+\S+\s+[<>]?\s*([\d.,]+)\s*([kMG]?B)")
    BARRA = re.compile(r"(Installing|Updating|Uninstalling) (\d+)/(\d+)\D.*?(\d+)%")
    UNITA = {"B": 1, "kB": 1e3, "MB": 1e6, "GB": 1e9}

    def __init__(self, appid, comando, avviso, fine):
        self.appid, self.comando, self.avviso, self.fine = appid, comando, avviso, fine
        self.frazione, self.testo, self.pezzi, self.righe, self.dove = None, "", {}, [], "flathub"
        threading.Thread(target=self.esegui, daemon=True).start()

    def esegui(self):
        rc = 1
        try:
            madre, figlia = pty.openpty()
            fcntl.ioctl(figlia, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
            env = dict(os.environ, TOD_STORE_AVANZAMENTO="1", TERM="xterm", COLUMNS="160")
            p = subprocess.Popen(["bash", TOD, "store", self.comando, self.appid], stdin=figlia, stdout=figlia,
                                 stderr=figlia, env=env, start_new_session=True, close_fds=True)
            os.close(figlia)
            resto = ""
            while True:
                try:
                    b = os.read(madre, 8192)
                except OSError:
                    break
                if not b:
                    break
                if b"\x1b[6n" in b:   # flatpak chiede dov'è il cursore e aspetta 0,1 s: si risponde subito
                    os.write(madre, b"\x1b[1;1R" * b.count(b"\x1b[6n"))
                t = resto + self.ANSI.sub("", b.decode("utf-8", "replace"))
                parti = re.split(r"[\r\n]", t)
                resto = parti.pop()
                for r in parti:
                    self.riga(r)
                if resto:
                    self.riga(resto, parziale=True)
            rc = p.wait()
            os.close(madre)
        except OSError:
            pass
        GLib.idle_add(lambda: self.fine(self, rc) and False)

    def riga(self, r, parziale=False):
        r = r.strip()
        if not r:
            return
        if not parziale:
            self.righe.append(r)
        prima = (self.frazione, self.testo)
        m = self.PEZZO.match(r)
        if m:   # la tabella di flatpak: quanto pesa ogni pezzo ("partial": traduzioni, se ne scarica una piccola parte)
            self.pezzi[int(m.group(1))] = float(m.group(3).replace(".", "").replace(",", ".")) * \
                self.UNITA.get(m.group(4), 1) * (0.05 if "partial" in r else 1)
        m = self.BARRA.search(r)
        if m:
            n, tot, pc = int(m.group(2)), int(m.group(3)), int(m.group(4))
            pesi = [self.pezzi.get(i, 0) for i in range(1, tot + 1)]
            if sum(pesi) > 0:
                fatto = sum(pesi[:n - 1]) + pesi[n - 1] * pc / 100
                self.frazione = fatto / sum(pesi)
                self.testo = T("Scarico %d%% · %s di %s", "Downloading %d%% · %s of %s") % (
                    int(self.frazione * 100), misura(fatto), misura(sum(pesi)))
            else:
                self.frazione = (n - 1 + pc / 100) / tot
                self.testo = T("Scarico %d%%", "Downloading %d%%") % int(self.frazione * 100)
            if m.group(1) == "Uninstalling":
                self.testo = T("Rimuovo %d%%", "Removing %d%%") % int(self.frazione * 100)
        m = re.search(r"dlstatus:[^:]*:([\d.]+):", r)   # apt (Termux o Debian): righe di stato
        if m:
            self.frazione = float(m.group(1)) / 100 * 0.75
            self.testo = T("Scarico %d%%", "Downloading %d%%") % int(float(m.group(1)))
        m = re.search(r"pmstatus:[^:]*:([\d.]+):", r)
        if m:
            self.frazione = 0.75 + float(m.group(1)) / 100 * 0.25
            self.testo = T("Installo %d%%", "Installing %d%%") % int(float(m.group(1)))
        m = re.search(r"(Need to get|È necessario scaricare|Bisogna prendere) ([\d.,]+ [kMG]?B)", r)
        if m and not self.testo:
            self.testo = T("Scarico %s", "Downloading %s") % m.group(2)
        m = re.search(r"(da|from) (Termux|Debian) \(", r)
        if m:
            self.dove = m.group(2).lower()
            self.testo = T("Da %s…", "From %s…") % m.group(2)
        if (self.frazione, self.testo) != prima:
            GLib.idle_add(lambda: self.avviso(self) and False)


class Store(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.termuxondex.Store", flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None

    def do_command_line(self, cl):
        args = cl.get_arguments()[1:]
        if not self.win:
            self.win = Finestra(self)
        self.win.present()
        if args:
            self.win.apri_id(args[0])
        return 0


class Finestra(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Store")
        global SCALA
        res = Gdk.Screen.get_default().get_resolution()
        SCALA = res / 96.0 if res and res > 0 else 1.0
        w, h = px(1180), px(780)   # mai più grande dello schermo
        mon = Gdk.Display.get_default().get_primary_monitor() or Gdk.Display.get_default().get_monitor(0)
        if mon:
            area = mon.get_workarea()
            w, h = min(w, int(area.width * 0.88)), min(h, int(area.height * 0.86))
        self.set_default_size(w, h)
        self.set_icon_name("system-software-install")
        classe(self, "store")
        self.prov = Gtk.CssProvider()
        self.prov.load_from_data(css())
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), self.prov,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.Settings.get_default().connect("notify::gtk-theme-name", lambda *_: self.prov.load_from_data(css()))
        self.app, self.per_id = [], {}
        self.sistema, self.voci_sistema, self.fonti = {}, {}, {}
        self.inst = set()
        self.lavori = {}     # id → Lavoro in corso
        self.esito = {}      # id → com'è finito l'ultimo lavoro
        self.pulsanti = {}   # id → pulsanti (Gtk.Stack: capsula / anello) da tenere aggiornati
        self.storia = []
        self.attesa = None
        self.costruisci()
        self.show_all()
        GLib.idle_add(lambda: self.set_focus(None) and False)   # niente fuoco sulla ricerca all'apertura
        self.carica()

    # --- struttura ---------------------------------------------------------------------------------
    def costruisci(self):
        radice = Gtk.Box()
        self.add(radice)
        lato = classe(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(6)), "lato")
        lato.set_size_request(px(230), -1)
        radice.pack_start(lato, False, False, 0)
        self.cerca = classe(Gtk.SearchEntry(placeholder_text=T("Cerca", "Search")), "cerca")
        self.cerca.set_margin_start(px(14))
        self.cerca.set_margin_end(px(14))
        self.cerca.set_margin_top(px(16))
        self.cerca.set_margin_bottom(px(8))
        self.cerca.connect("search-changed", lambda e: self.vai_cerca(e.get_text()))
        lato.pack_start(self.cerca, False, False, 0)
        self.lista = Gtk.ListBox()
        self.lista.set_selection_mode(Gtk.SelectionMode.SINGLE)
        for chiave, nome, ic, _ in SEZIONI:
            self.lista.add(self.voce(chiave, nome, ic))
        self.lista.add(self.titolo_lista(T("Le tue app", "Your apps")))
        self.lista.add(self.voce("installate", T("Installate", "Installed"), "folder-download-symbolic"))
        self.lista.connect("row-selected", self.riga_scelta)
        lato.pack_start(self.lista, False, False, 0)
        piede = etichetta(T("Le app arrivano da Termux, Debian e Flathub",
                            "Apps come from Termux, Debian and Flathub"), "piccolo", righe=2, larga=24)
        piede.set_margin_start(px(20))
        piede.set_margin_bottom(px(14))
        lato.pack_end(piede, False, False, 0)
        self.pagine = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        radice.pack_start(self.pagine, True, True, 0)
        att = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(12), valign=Gtk.Align.CENTER)
        self.giro = Gtk.Spinner(width_request=px(28), height_request=px(28))
        att.pack_start(self.giro, False, False, 0)
        self.att_testo = etichetta(T("Apro il catalogo…", "Opening the catalog…"), "sotto", xalign=0.5)
        att.pack_start(self.att_testo, False, False, 0)
        self.pagine.add_named(att, "attesa")

    def voce(self, chiave, nome, ic):
        r = Gtk.ListBoxRow()
        r.chiave = chiave
        b = Gtk.Box(spacing=px(10))
        im = Gtk.Image.new_from_icon_name(ic, Gtk.IconSize.MENU)
        im.set_pixel_size(px(17))
        b.pack_start(im, False, False, 0)
        b.pack_start(etichetta(nome, "testo", ellissi=True), True, True, 0)
        r.add(b)
        return r

    def titolo_lista(self, testo):
        r = Gtk.ListBoxRow(selectable=False, activatable=False)
        r.chiave = None
        l = etichetta(testo, "gruppo")
        l.set_margin_top(px(16))
        l.set_margin_start(px(8))
        r.add(l)
        return r

    def pagina(self, nome, contenuto):
        vecchia = self.pagine.get_child_by_name(nome)
        if vecchia:
            self.pagine.remove(vecchia)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.add(contenuto)
        sc.show_all()
        self.pagine.add_named(sc, nome)
        self.pagine.set_visible_child_name(nome)

    def corpo(self, titolo=None, sotto=None):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(14))
        b.set_margin_start(px(36))
        b.set_margin_end(px(36))
        b.set_margin_top(px(26))
        b.set_margin_bottom(px(36))
        if titolo:
            b.pack_start(etichetta(titolo, "titolo-pagina"), False, False, 0)
        if sotto:
            l = etichetta(sotto, "sotto", righe=-1, larga=90)
            l.set_margin_top(-px(10))
            b.pack_start(l, False, False, 0)
        return b

    # --- dati ---------------------------------------------------------------------------------------
    def carica(self):
        self.giro.start()
        self.pagine.set_visible_child_name("attesa")
        pronto = os.path.exists(os.path.join(catalogo.cartella(STORE), "appstream.xml.gz"))
        if not pronto:
            self.att_testo.set_text(T("Preparo lo Store la prima volta: circa un minuto…",
                                      "Preparing the Store for the first time: about a minute…"))

        def lavoro():
            if pronto:   # subito quello che c'è, poi si controlla se il catalogo è cambiato
                self.leggi_catalogo()
                self.leggi_sistema()
                GLib.idle_add(self.pronto)
            self.esegui("_catalogo")
            if self.leggi_catalogo() or not pronto:
                self.leggi_sistema()
                GLib.idle_add(self.pronto)
        threading.Thread(target=lavoro, daemon=True).start()

    def esegui(self, *args):
        try:
            return subprocess.run(["bash", TOD, "store", *args], capture_output=True, text=True,
                                  timeout=900, stdin=subprocess.DEVNULL).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def leggi_catalogo(self):
        try:
            app = catalogo.leggi(STORE, LINGUA)
        except (OSError, ValueError):
            return False
        if len(app) == len(self.app) and self.app:
            return False
        self.app = app
        self.per_id = {a["id"]: a for a in app}
        return True

    def chiavi(self, nomi):
        """i nomi → la chiave di share/tod-app-keys.awk (una app, un solo sistema), tutti in una volta."""
        try:
            out = subprocess.run(["awk", "-f", os.path.join(TOD_ROOT, "share/tod-app-keys.awk"), "-v", "mode=names"],
                                 input="\n".join(nomi) + "\n", capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        return out.split("\n")

    def leggi_sistema(self):
        """le app che Termux o Debian hanno già, e la loro voce dello Start (per "Apri")."""
        chiavi = {}
        for l in self.esegui("_chiavi").splitlines():
            if "\t" in l:
                k, p = l.split("\t", 1)
                chiavi.setdefault(k, p)
        ids = [a["id"] for a in self.app]
        chiavi_id = self.chiavi(ids)
        self.sistema = {i: chiavi[k] for i, k in zip(ids, chiavi_id) if k in chiavi}
        voci = []
        for d in (os.path.join(PREFIX, "share/applications"), os.path.join(CASA, ".local/share/applications"),
                  os.path.join(os.environ.get("HOME", CASA), ".local/share/applications")):
            for f in sorted(glob.glob(os.path.join(d, "*.desktop"))):
                n = os.path.basename(f)
                if n.startswith("tod-store-"):
                    continue
                try:
                    if "\nNoDisplay=true" in open(f, errors="replace").read():
                        continue
                except OSError:
                    continue
                voci.append((n[len("tod-cx-"):] if n.startswith("tod-cx-") else n, f))
        per_chiave = {}
        for (n, f), k in zip(voci, self.chiavi([n for n, _ in voci])):
            per_chiave.setdefault(k, f)
        self.voci_sistema = {i: per_chiave[k] for i, k in zip(ids, chiavi_id) if k in per_chiave and i in self.sistema}

    def installate(self):
        base = os.path.join(STORE, "flatpak", "app")
        try:
            return {n for n in os.listdir(base)
                    if any(os.path.exists(os.path.join(base, n, "aarch64", r, "active"))
                           for r in os.listdir(os.path.join(base, n, "aarch64")))}
        except OSError:
            return set()

    def pronto(self):
        self.giro.stop()
        self.inst = self.installate()
        if not self.app:
            self.att_testo.set_text(T("Non riesco a scaricare il catalogo di Flathub: controlla la rete.",
                                      "Cannot download the Flathub catalog: check the network."))
            return
        if not self.lista.get_selected_row():
            self.lista.select_row(self.lista.get_row_at_index(0))
        else:
            for i in list(self.pulsanti):
                self.aggiorna(i)
        if self.attesa:
            self.apri_id(self.attesa)
            self.attesa = None

    def ridisegna(self):
        r = self.lista.get_selected_row()
        if r is not None:
            self.riga_scelta(self.lista, r)

    # --- navigazione ----------------------------------------------------------------------------------
    def riga_scelta(self, lista, r):
        if r is None or not getattr(r, "chiave", None) or not self.app:
            return
        self.storia = []
        if self.cerca.get_text():
            self.cerca.set_text("")
        k = r.chiave
        if k == "scopri":
            self.mostra_scopri()
        elif k == "categorie":
            self.mostra_categorie()
        elif k == "installate":
            self.mostra_installate()
        else:
            self.mostra_sezione(k)

    def vai_cerca(self, testo):
        if not self.app:
            return
        if not testo.strip():
            self.ridisegna()
            return
        self.lista.unselect_all()
        ris = catalogo.cerca(self.app, testo)
        b = self.corpo(T("Risultati per «%s»", "Results for “%s”") % testo.strip(),
                       T("%d app", "%d apps") % len(ris) if ris else T("Nessuna app trovata", "No apps found"))
        self.scaffale(b, ris, 60)
        self.pagina("lista", b)

    def apri_id(self, appid):
        if not self.app:
            self.attesa = appid
            return
        a = self.per_id.get(appid)
        if a:
            self.mostra_app(a)

    def indietro(self):
        if self.storia:
            self.storia.pop()()
        else:
            self.ridisegna()

    def ricorda(self):
        nome = self.pagine.get_visible_child_name()
        if nome == "app" and getattr(self, "app_vista", None):
            a = self.app_vista
            return lambda: self.mostra_app(a, storia=False)
        if nome == "lista" and self.cerca.get_text().strip():
            t = self.cerca.get_text()
            return lambda: self.vai_cerca(t)
        if nome == "lista" and getattr(self, "cat_vista", None):
            c = self.cat_vista
            return lambda: self.mostra_categoria(c)
        return self.ridisegna if nome in ("scopri", "sezione", "categorie", "installate", "lista") else None

    # --- pagine ------------------------------------------------------------------------------------------
    def nuove(self, ids):
        """app da proporre: non nel sistema, non già installate dallo Store."""
        return [self.per_id[i] for i in ids if i in self.per_id and i not in self.sistema and i not in self.inst]

    def di_categorie(self, cats):
        adesso = time.time()
        l = [a for a in self.app if any(c in a["categorie"] for c in cats)]
        l.sort(key=lambda a: (a["id"] in self.sistema, -catalogo.punteggio(a, adesso), a["nome"].lower()))
        return l

    def mostra_scopri(self):
        b = self.corpo(T("Scopri", "Discover"))
        nuove = self.nuove(CONSIGLIATE)
        con_foto = [a for a in nuove if a["schermate"]][:2]
        if len(con_foto) == 2:
            b.pack_start(self.copertine(con_foto, T("IN EVIDENZA", "FEATURED")), False, False, 0)
        b.pack_start(self.titolo_sezione(T("App da non perdere", "Must-have apps")), False, False, 0)
        self.scaffale(b, [a for a in nuove if a not in con_foto][:9], 9)
        for cat in ("Graphics", "Office", "AudioVideo", "Game"):
            b.pack_start(self.titolo_sezione(CATEGORIE[cat][0], lambda c=cat: self.mostra_categoria(c, storia=True)),
                         False, False, 0)
            self.scaffale(b, self.di_categorie([cat])[:6], 6)
        self.pagina("scopri", b)

    def mostra_sezione(self, chiave):
        _, nome, _, cats = next(s for s in SEZIONI if s[0] == chiave)
        b = self.corpo(nome)
        tutte = self.di_categorie(cats)
        con_foto = [a for a in tutte[:40] if a["schermate"] and a["id"] not in self.sistema][:2]
        if len(con_foto) == 2:
            b.pack_start(self.copertine(con_foto, T("DA PROVARE", "TRY IT")), False, False, 0)
        if len(cats) == 1:
            b.pack_start(self.titolo_sezione(T("Le migliori", "Top apps")), False, False, 0)
            self.scaffale(b, [a for a in tutte if a not in con_foto], 30)
        else:
            for cat in cats:
                b.pack_start(self.titolo_sezione(CATEGORIE[cat][0], lambda c=cat: self.mostra_categoria(c, storia=True)),
                             False, False, 0)
                self.scaffale(b, [a for a in self.di_categorie([cat]) if a not in con_foto][:9], 9)
        self.pagina("sezione", b)

    def mostra_categorie(self):
        b = self.corpo(T("Categorie", "Categories"))
        fb = Gtk.FlowBox(homogeneous=True, selection_mode=Gtk.SelectionMode.NONE, column_spacing=px(12),
                         row_spacing=px(12), min_children_per_line=2, max_children_per_line=3)
        for cat, (nome, ic) in CATEGORIE.items():
            t = classe(Gtk.Box(spacing=px(12)), "cat")
            quad = classe(Gtk.Box(), "icona-cat")
            im = Gtk.Image.new_from_icon_name(ic, Gtk.IconSize.MENU)
            im.set_pixel_size(px(18))
            quad.set_center_widget(im)
            t.pack_start(quad, False, False, 0)
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
            v.pack_start(etichetta(nome, "nome", ellissi=True, larga=20), False, False, 0)
            v.pack_start(etichetta(T("%d app", "%d apps") % len(self.di_categorie([cat])), "sotto"), False, False, 0)
            t.pack_start(v, True, True, 0)
            fb.add(cliccabile(t, lambda c=cat: self.mostra_categoria(c, storia=True)))
        b.pack_start(fb, False, False, 0)
        self.pagina("categorie", b)

    def mostra_categoria(self, cat, storia=False):
        if storia:
            r = self.ricorda()
            if r:
                self.storia.append(r)
        self.cat_vista = cat
        lista = self.di_categorie([cat])
        b = self.corpo()
        if storia:
            b.pack_start(self.indietro_btn(), False, False, 0)
        b.pack_start(etichetta(CATEGORIE[cat][0], "titolo-pagina"), False, False, 0)
        l = etichetta(T("%d app", "%d apps") % len(lista), "sotto")
        l.set_margin_top(-px(10))
        b.pack_start(l, False, False, 0)
        self.scaffale(b, lista, 45)
        self.pagina("lista", b)

    def mostra_installate(self):
        self.inst = self.installate()
        b = self.corpo(T("Installate", "Installed"),
                       T("Le app scaricate da Flathub. Quelle arrivate da Termux o Debian stanno con le altre app del "
                         "sistema (Impostazioni → App).",
                         "Apps downloaded from Flathub. Those from Termux or Debian are with the other system apps "
                         "(Settings → Apps).") if self.inst else T("Nessuna app da Flathub, per ora",
                                                                  "No apps from Flathub yet"))
        if self.inst:
            riga = Gtk.Box(spacing=px(12))
            self.agg = classe(Gtk.Button(label=T("Cerca aggiornamenti", "Check for updates")), "ottieni")
            self.agg.connect("clicked", lambda *_: self.aggiorna_tutto())
            riga.pack_start(self.agg, False, False, 0)
            self.agg_testo = etichetta("", "sotto")
            riga.pack_start(self.agg_testo, False, False, 0)
            b.pack_start(riga, False, False, 0)
            lista = [self.per_id.get(i) or {"id": i, "nome": i, "sommario": "", "icona": "", "versione": "",
                                             "schermate": [], "categorie": []} for i in self.inst]
            lista.sort(key=lambda a: a["nome"].lower())
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            for a in lista:
                r = classe(Gtk.Box(spacing=px(14)), "riga")
                r.pack_start(icona(a, px(48)), False, False, 0)
                t = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
                t.pack_start(etichetta(a["nome"], "nome", ellissi=True), False, False, 0)
                t.pack_start(etichetta(T("Versione %s", "Version %s") % a["versione"] if a.get("versione") else
                                       a.get("sommario", ""), "sotto", ellissi=True), False, False, 0)
                r.pack_start(t, True, True, 0)
                r.pack_end(self.pulsante(a), False, False, 0)
                rim = classe(Gtk.Button(label=T("Rimuovi", "Remove")), "collegamento", "pericolo")
                rim.set_valign(Gtk.Align.CENTER)
                rim.connect("clicked", lambda w, a=a: self.rimuovi(w, a))
                r.pack_end(rim, False, False, px(8))
                col.pack_start(cliccabile(r, lambda a=a: self.mostra_app(a) if a["id"] in self.per_id else None),
                               False, False, 0)
            b.pack_start(col, False, False, 0)
        self.pagina("installate", b)

    def indietro_btn(self):
        ind = classe(Gtk.Button(), "collegamento")
        bi = Gtk.Box(spacing=px(2))
        bi.pack_start(Gtk.Image.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.MENU), False, False, 0)
        bi.pack_start(Gtk.Label(label=T("Indietro", "Back")), False, False, 0)
        ind.add(bi)
        ind.set_halign(Gtk.Align.START)
        ind.connect("clicked", lambda *_: self.indietro())
        return ind

    def mostra_app(self, a, storia=True):
        if storia:
            r = self.ricorda()
            if r:
                self.storia.append(r)
        b = self.corpo()
        b.pack_start(self.indietro_btn(), False, False, 0)
        # testa: icona, nome, sottotitolo, sviluppatore, Ottieni con l'avanzamento accanto
        testa = Gtk.Box(spacing=px(24))
        testa.pack_start(icona(a, px(128)), False, False, 0)
        t = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(4), valign=Gtk.Align.CENTER)
        t.pack_start(etichetta(a["nome"], "grande", righe=-1, larga=40), False, False, 0)
        if a["sommario"]:
            t.pack_start(etichetta(a["sommario"], "sottotitolo", righe=-1, larga=60), False, False, 0)
        if a["sviluppatore"]:
            t.pack_start(etichetta(a["sviluppatore"], "piccolo"), False, False, 0)
        az = Gtk.Box(spacing=px(14))
        az.set_margin_top(px(10))
        az.pack_start(self.pulsante(a, grande=True), False, False, 0)
        self.avanz = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(4), valign=Gtk.Align.CENTER)
        self.avanz_barra = Gtk.ProgressBar()
        self.avanz_barra.set_size_request(px(240), -1)
        self.avanz_barra.set_no_show_all(True)
        self.avanz_testo = etichetta("", "avanz-testo", ellissi=True, larga=50)
        self.avanz_testo.set_no_show_all(True)
        self.avanz.pack_start(self.avanz_barra, False, False, 0)
        self.avanz.pack_start(self.avanz_testo, False, False, 0)
        az.pack_start(self.avanz, False, False, 0)
        if a["id"] in self.inst:
            rim = classe(Gtk.Button(label=T("Rimuovi", "Remove")), "collegamento", "pericolo")
            rim.set_valign(Gtk.Align.CENTER)
            rim.connect("clicked", lambda w: self.rimuovi(w, a))
            az.pack_start(rim, False, False, 0)
        t.pack_start(az, False, False, 0)
        testa.pack_start(t, True, True, 0)
        b.pack_start(testa, False, False, 0)
        self.app_vista, self.passo_id = a, a["id"]
        # la striscia delle informazioni, come l'App Store
        striscia = Gtk.Box()
        striscia.set_margin_top(px(10))
        n, sotto = self.nome_fonte(a["id"])
        self.col_fonte = self.colonna(striscia, T("DA", "FROM"), n, sotto, prima=True)
        cat = next((CATEGORIE[c][0] for c in a["categorie"] if c in CATEGORIE), "—")
        self.colonna(striscia, T("CATEGORIA", "CATEGORY"), cat.split(" ")[0], "")
        self.colonna(striscia, T("SVILUPPATORE", "DEVELOPER"), "", a["sviluppatore"] or "—",
                     icona_nome="avatar-default-symbolic")
        self.colonna(striscia, T("LINGUA", "LANGUAGE"), "IT" if a["tradotta"] else "EN",
                     T("Italiano", "Italian") if a["tradotta"] else T("Inglese", "English"))
        self.colonna(striscia, T("VERSIONE", "VERSION"), a["versione"] or "—", self.data(a["data"]))
        if a["verificata"]:
            self.colonna(striscia, "FLATHUB", "", T("Verificata", "Verified"), icona_nome="emblem-ok-symbolic")
        b.pack_start(striscia, False, False, 0)
        b.pack_start(Gtk.Separator(), False, False, 0)
        if a["id"] in self.sistema:
            nota = classe(Gtk.Box(spacing=px(10)), "nota")
            nota.pack_start(Gtk.Image.new_from_icon_name("emblem-ok-symbolic", Gtk.IconSize.MENU), False, False, 0)
            nota.pack_start(etichetta(T("È già nel tuo sistema (%s): una app, un solo sistema.",
                                        "It is already on your system (%s): one app, one system.")
                                      % self.sistema[a["id"]], righe=-1, larga=80), True, True, 0)
            b.pack_start(nota, False, False, 0)
        if a["schermate"]:
            sc = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER)
            sc.set_min_content_height(px(340))
            fila = Gtk.Box(spacing=px(16))
            for url in a["schermate"]:
                im = Immagine(px(320))
                fila.pack_start(im, False, False, 0)
                scarica(url, lambda p, im=im: p and im.metti(p))
            sc.add(fila)
            b.pack_start(sc, False, False, 0)
        if a["descrizione"]:
            b.pack_start(self.testo_lungo(a["descrizione"]), False, False, 0)
        if a.get("novita") or a["versione"]:
            b.pack_start(Gtk.Separator(), False, False, 0)
            nv = Gtk.Box()
            nv.pack_start(etichetta(T("Novità", "What's New"), "sezione"), False, False, 0)
            nv.pack_end(etichetta(T("Versione %s", "Version %s") % a["versione"] if a["versione"] else "", "sotto"),
                        False, False, 0)
            b.pack_start(nv, False, False, 0)
            if self.data(a["data"]):
                b.pack_start(etichetta(self.data(a["data"]), "sotto"), False, False, 0)
            if a.get("novita"):
                b.pack_start(self.testo_lungo(a["novita"], 4), False, False, 0)
        b.pack_start(Gtk.Separator(), False, False, 0)
        b.pack_start(etichetta(T("Informazioni", "Information"), "sezione"), False, False, 0)
        griglia = Gtk.Grid(column_spacing=px(40), row_spacing=px(14), column_homogeneous=True)
        info = [(T("Fornitore", "Provider"), a["sviluppatore"] or "—"),
                (T("Categoria", "Category"), cat),
                (T("Licenza", "License"), self.licenza(a["licenza"])),
                (T("Da", "From"), "%s · %s" % (n, sotto) if sotto else n),
                (T("Base (runtime)", "Base (runtime)"), self.nome_base(a)),
                (T("Compatibilità", "Compatibility"), T("Questo telefono (aarch64)", "This phone (aarch64)"))]
        self.info_valori = {}
        for i, (k, v) in enumerate(info):
            c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(2))
            c.pack_start(etichetta(k, "sotto"), False, False, 0)
            l = etichetta(v, "testo", righe=2, larga=30)
            self.info_valori[k] = l
            c.pack_start(l, False, False, 0)
            griglia.attach(c, i % 3, i // 3, 1, 1)
        b.pack_start(griglia, False, False, 0)
        if a["sito"]:
            sito = classe(Gtk.Button(label=T("Sito dello sviluppatore ›", "Developer website ›")), "collegamento")
            sito.set_halign(Gtk.Align.START)
            sito.connect("clicked", lambda *_: self.apri_link(a["sito"]))
            b.pack_start(sito, False, False, 0)
        self.pagina("app", b)
        self.avanzamento(a["id"])
        self.chiedi_fonte(a["id"])

    def colonna(self, striscia, etic, valore, sotto, prima=False, icona_nome=None):
        c = classe(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(3)), "colonna")
        if prima:
            classe(c, "prima")
        c.pack_start(etichetta(etic, "etic", xalign=0.5), False, False, 0)
        if icona_nome:
            im = Gtk.Image.new_from_icon_name(icona_nome, Gtk.IconSize.LARGE_TOOLBAR)
            im.set_pixel_size(px(22))
            classe(im, "valore")
            c.pack_start(im, False, False, 0)
        v = etichetta(valore, "valore", xalign=0.5, ellissi=True, larga=14)
        if valore or not icona_nome:
            c.pack_start(v, False, False, 0)
        s = etichetta(sotto, "piccolo", xalign=0.5, ellissi=True, larga=18)
        c.pack_start(s, False, False, 0)
        striscia.pack_start(c, False, False, 0)
        return v, s

    @staticmethod
    def data(ts):
        if not ts:
            return ""
        mesi = (T("gen", "Jan"), T("feb", "Feb"), T("mar", "Mar"), T("apr", "Apr"), T("mag", "May"), T("giu", "Jun"),
                T("lug", "Jul"), T("ago", "Aug"), T("set", "Sep"), T("ott", "Oct"), T("nov", "Nov"), T("dic", "Dec"))
        t = time.localtime(ts)
        return "%d %s %d" % (t.tm_mday, mesi[t.tm_mon - 1], t.tm_year)

    def testo_lungo(self, testo, righe=5):
        """testo chiuso a poche righe, con "altro" per aprirlo, come l'App Store."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(4))
        l = etichetta(testo, "testo", righe=righe, larga=95)
        l.set_selectable(True)
        box.pack_start(l, False, False, 0)
        if testo.count("\n") >= righe - 1 or len(testo) > 380:
            altro = classe(Gtk.Button(label=T("altro", "more")), "collegamento")
            altro.set_halign(Gtk.Align.END)

            def apri(*_):
                l.set_lines(-1)
                l.set_ellipsize(Pango.EllipsizeMode.NONE)
                altro.hide()
            altro.connect("clicked", apri)
            box.pack_start(altro, False, False, 0)
        return box

    def nome_base(self, a):
        if a["id"] in self.sistema or self.fonti.get(a["id"], ("flathub",))[0] != "flathub":
            return T("non serve", "not needed")
        rt = a["runtime"].split("/")
        ha = len(rt) == 3 and os.path.isdir(os.path.join(STORE, "flatpak", "runtime", *rt))
        return "%s · %s" % (nome_runtime(a["runtime"]), T("già presente", "already here") if ha else
                            T("da scaricare la prima volta", "downloaded the first time"))

    @staticmethod
    def licenza(l):
        if not l:
            return "—"
        if "proprietary" in l.lower():
            return T("Proprietaria", "Proprietary")
        return l.split(" ")[0].replace("-or-later", "+").replace("-only", "")

    def apri_link(self, url):
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except GLib.Error:
            pass

    # --- da dove arriva l'app ----------------------------------------------------------------------------------
    def chiedi_fonte(self, appid):
        if appid in self.fonti or appid in self.sistema or appid in self.inst:
            return

        def lavoro():
            r = self.esegui("_fonte", appid).strip().split("\t")
            if len(r) == 2 and r[0] in ("termux", "debian", "flathub"):
                self.fonti[appid] = (r[0], r[1])
                GLib.idle_add(lambda: self.mostra_fonte(appid) and False)
        threading.Thread(target=lavoro, daemon=True).start()

    def nome_fonte(self, appid):
        if appid in self.inst:
            return "Flathub", T("installata", "installed")
        if appid in self.sistema:
            return T("Sistema", "System"), self.sistema[appid]
        dove = self.fonti.get(appid, (None,))[0]
        if dove == "termux":
            return "Termux", T("nativa", "native")
        if dove == "debian":
            return "Debian", T("pacchetto", "package")
        if dove == "flathub":
            return "Flathub", nome_runtime(self.per_id[appid]["runtime"]) if appid in self.per_id else ""
        return "…", ""

    def mostra_fonte(self, appid):
        if getattr(self, "passo_id", None) != appid:
            return
        v, s = self.col_fonte
        n, sotto = self.nome_fonte(appid)
        v.set_text(n)
        s.set_text(sotto)
        a = self.per_id[appid]
        k = T("Da", "From")
        if k in self.info_valori:
            self.info_valori[k].set_text("%s · %s" % (n, sotto) if sotto else n)
        k = T("Base (runtime)", "Base (runtime)")
        if k in self.info_valori:
            self.info_valori[k].set_text(self.nome_base(a))

    # --- righe, scaffali, copertine --------------------------------------------------------------------------
    def titolo_sezione(self, testo, tutte=None):
        r = Gtk.Box(spacing=px(8))
        r.set_margin_top(px(14))
        r.pack_start(etichetta(testo, "sezione"), False, False, 0)
        if tutte:
            v = classe(Gtk.Button(label=T("Vedi tutte", "See All")), "collegamento")
            v.connect("clicked", lambda *_: tutte())
            r.pack_end(v, False, False, 0)
        return r

    def copertine(self, lista, marchio):
        """le schede grandi dell'App Store: la prima schermata dell'app, con il nome in bianco sopra."""
        fila = Gtk.Box(spacing=px(18), homogeneous=True)
        for a in lista:
            ov = Gtk.Overlay()
            im = Immagine(px(250), copertina=True)
            scarica(a["schermate"][0], lambda p, im=im: p and im.metti(p))
            ov.add(im)
            testo = Gtk.Box(spacing=px(12), valign=Gtk.Align.END)
            testo.set_margin_start(px(20))
            testo.set_margin_end(px(20))
            testo.set_margin_bottom(px(18))
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(2))
            v.pack_start(etichetta(marchio, "banner-marchio"), False, False, 0)
            v.pack_start(etichetta(a["nome"], "banner-nome", ellissi=True, larga=24), False, False, 0)
            v.pack_start(etichetta(a["sommario"], "banner-sotto", ellissi=True, larga=40), False, False, 0)
            testo.pack_start(v, True, True, 0)
            testo.pack_end(self.pulsante(a, bianco=True), False, False, 0)
            ov.add_overlay(testo)
            fila.pack_start(cliccabile(ov, lambda a=a: self.mostra_app(a)), True, True, 0)
        return fila

    def scaffale(self, box, lista, quante):
        """app in tre colonne, una riga ciascuna con un filo sotto (niente riquadri), come l'App Store."""
        fb = Gtk.FlowBox(homogeneous=True, selection_mode=Gtk.SelectionMode.NONE, activate_on_single_click=True,
                         column_spacing=px(28), row_spacing=0, min_children_per_line=2, max_children_per_line=3)
        fb.connect("child-activated", lambda f, c: self.mostra_app(c.app))
        box.pack_start(fb, False, False, 0)
        resto = list(lista)
        altre = classe(Gtk.Button(label=T("Mostra altre", "Show more")), "ottieni")
        altre.set_halign(Gtk.Align.CENTER)

        def aggiungi(*_):
            for a in resto[:quante]:
                c = Gtk.FlowBoxChild()
                c.app = a
                c.add(self.riga_app(a))
                c.show_all()
                fb.add(c)
            del resto[:quante]
            if not resto:
                altre.hide()
        altre.connect("clicked", aggiungi)
        aggiungi()
        if resto:
            box.pack_start(altre, False, False, 0)

    def riga_app(self, a):
        r = classe(Gtk.Box(spacing=px(14)), "riga")
        r.pack_start(icona(a, px(56)), False, False, 0)
        t = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(1), valign=Gtk.Align.CENTER)
        t.pack_start(etichetta(a["nome"], "nome", ellissi=True, larga=16), False, False, 0)
        t.pack_start(etichetta(a["sommario"], "sotto", righe=2, larga=20), False, False, 0)
        r.pack_start(t, True, True, 0)
        r.pack_end(self.pulsante(a), False, False, 0)
        r.set_size_request(px(240), px(80))
        return r

    # --- pulsanti: Ottieni / Apri / anello ----------------------------------------------------------------------
    def pulsante(self, a, grande=False, bianco=False):
        st = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=150)
        st.set_valign(Gtk.Align.CENTER)
        st.set_halign(Gtk.Align.START if grande else Gtk.Align.END)
        b = classe(Gtk.Button(), "ottieni")
        if bianco:
            classe(b, "bianco")
        b.app = a
        b.connect("clicked", self.clic)
        st.add_named(b, "pulsante")
        an = Anello(px(30 if grande else 26))
        st.add_named(an, "anello")
        st.bottone, st.anello = b, an
        self.pulsanti.setdefault(a["id"], []).append(st)
        st.connect("destroy", self.togli_pulsante, a["id"])
        self.stato(st)
        return st

    def togli_pulsante(self, st, appid):
        l = self.pulsanti.get(appid, [])
        if st in l:
            l.remove(st)

    def stato(self, st):
        b, a = st.bottone, st.bottone.app
        i = a["id"]
        b.set_sensitive(True)
        b.set_tooltip_text(None)
        if i in self.lavori:
            st.anello.metti(self.lavori[i].frazione)
            st.set_visible_child_name("anello")
            return
        st.set_visible_child_name("pulsante")
        if i in self.inst:
            b.set_label(T("Apri", "Open"))
        elif i in self.sistema:
            if i in self.voci_sistema:
                b.set_label(T("Apri", "Open"))
            else:
                b.set_label(T("Installata", "Installed"))
                b.set_sensitive(False)
            b.set_tooltip_text(T("Già nel sistema: %s", "Already on the system: %s") % self.sistema[i])
        else:
            b.set_label(T("Ottieni", "Get"))

    def aggiorna(self, appid):
        for st in list(self.pulsanti.get(appid, [])):
            self.stato(st)
        self.avanzamento(appid)

    def avanzamento(self, appid):
        """la barra e il testo accanto all'Ottieni, nella pagina dell'app."""
        if getattr(self, "passo_id", None) != appid or not getattr(self, "avanz_barra", None):
            return
        l = self.lavori.get(appid)
        if l is None:
            self.avanz_barra.hide()
            fine = self.esito.get(appid, "")
            self.avanz_testo.set_text(fine)
            self.avanz_testo.set_visible(bool(fine))
            return
        self.avanz_barra.show()
        self.avanz_testo.show()
        if l.frazione is None:
            self.avanz_barra.pulse()
        else:
            self.avanz_barra.set_fraction(l.frazione)
        self.avanz_testo.set_text(l.testo or T("Preparo…", "Preparing…"))

    def clic(self, b):
        a = b.app
        i = a["id"]
        if i in self.inst:
            subprocess.Popen(["bash", TOD, "store", "avvia", i], start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        if i in self.sistema and i in self.voci_sistema:   # già nel sistema: si apre quella
            try:
                DesktopAppInfo.new_from_filename(self.voci_sistema[i]).launch([], None)
            except (GLib.Error, TypeError, AttributeError):
                pass
            return
        self.avvia_lavoro(i, "installa")

    def rimuovi(self, w, a):
        """Rimuovi: al secondo clic, entro 4 s."""
        if not getattr(w, "conferma", False):
            w.conferma = True
            w.set_label(T("Conferma: rimuovi", "Confirm: remove"))

            def torna():
                if w.get_parent():
                    w.conferma = False
                    w.set_label(T("Rimuovi", "Remove"))
            GLib.timeout_add(4000, lambda: torna() and False)
            return
        w.set_sensitive(False)
        self.avvia_lavoro(a["id"], "rimuovi")

    def avvia_lavoro(self, appid, comando):
        self.esito.pop(appid, None)
        self.lavori[appid] = Lavoro(appid, comando, lambda l: self.aggiorna(l.appid), self.fine)
        self.aggiorna(appid)

    def fine(self, lavoro, rc):
        appid = lavoro.appid
        self.lavori.pop(appid, None)
        self.inst = self.installate()
        if rc != 0:
            ultima = next((r for r in reversed(lavoro.righe) if r.startswith(("✘", "error", "Error", "E:"))),
                          lavoro.righe[-1] if lavoro.righe else "")
            self.esito[appid] = T("Non riuscito: ", "Failed: ") + re.sub(r"^\W+\s*", "", ultima)
        elif lavoro.comando == "rimuovi":
            self.esito[appid] = T("Rimossa", "Removed")
        elif lavoro.dove in ("termux", "debian"):
            self.esito[appid] = T("Installata da %s: la trovi nello Start", "Installed from %s: find it in Start") % \
                lavoro.dove.capitalize()
        else:
            self.esito[appid] = T("Installata: la trovi nello Start", "Installed: find it in Start")
        self.aggiorna(appid)
        if rc == 0 and lavoro.comando == "installa" and lavoro.dove != "flathub":
            def rileggi():   # arrivata da Termux o Debian: ora fa parte del sistema
                self.leggi_sistema()
                GLib.idle_add(lambda: [self.aggiorna(i) for i in list(self.pulsanti)] and False)
            threading.Thread(target=rileggi, daemon=True).start()
        if self.pagine.get_visible_child_name() == "installate":
            self.mostra_installate()
        elif rc == 0 and getattr(self, "passo_id", None) == appid and appid in self.per_id:
            self.mostra_app(self.per_id[appid], storia=False)   # Rimuovi compare o sparisce

    def aggiorna_tutto(self):
        self.agg.set_sensitive(False)
        self.agg_testo.set_text(T("Controllo…", "Checking…"))

        def esegui():
            out = self.esegui("aggiorna")
            ok = "✔" in out
            GLib.idle_add(lambda: self.agg_fatto(ok) and False)
        threading.Thread(target=esegui, daemon=True).start()

    def agg_fatto(self, ok):
        if self.agg.get_parent():
            self.agg.set_sensitive(True)
            self.agg_testo.set_text(T("Tutto aggiornato", "Everything up to date") if ok
                                    else T("Aggiornamento non riuscito: dettagli in tod.log", "Update failed: see tod.log"))


if __name__ == "__main__":
    sys.exit(Store().run(sys.argv))
