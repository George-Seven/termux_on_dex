"""Foto, Video e Musica di Termux On DeX: la parte grafica in comune (GTK 3 di Termux).

Stile delle app Foto e Media Player di Windows 11 (Pietro, 9 novembre): fondo "Mica" pieno, riquadro del contenuto
con l'angolo arrotondato accanto alla barra di navigazione, voce scelta con la lineetta nell'accento, cursori con
la manopola tonda e il punto nell'accento, pulsante di riproduzione tondo pieno. Colori: chiaro o scuro come il
tema del desktop, l'accento è quello scelto in Impostazioni (ACCENTO in tod.conf), mai un blu fisso.

Qui anche la Griglia (migliaia di miniature disegnate senza un widget per elemento: si disegna solo ciò che si
vede) e le Miniature (nella cache di freedesktop, ~/.cache/thumbnails, la stessa di Esplora file).
"""
import bisect
import collections
import hashlib
import math
import os
import re
import sys
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")   # senza, Termux carica Gdk 4 e GTK 3 non parte
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402

try:   # GLib 2.86: le funzioni Unix di Gio stanno in GioUnix
    gi.require_version("GioUnix", "2.0")
    from gi.repository import GioUnix
    DesktopAppInfo = GioUnix.DesktopAppInfo
except (ValueError, ImportError):
    DesktopAppInfo = Gio.DesktopAppInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import libreria as L  # noqa: E402

T = L.T
QUI = os.path.dirname(os.path.abspath(__file__))
TOD_ROOT = os.path.dirname(os.path.dirname(QUI))
CASA_TERMUX = os.environ.get("TOD_TERMUX_HOME") or os.environ.get("HOME", "/data/data/com.termux/files/home")
SCALA = 1.0


def px(n):
    return int(round(n * SCALA))


def imposta_scala():
    global SCALA
    scr = Gdk.Screen.get_default()
    res = scr.get_resolution() if scr else 96
    SCALA = res / 96.0 if res and res > 0 else 1.0
    return SCALA


def accento():
    a = "#0067c0"
    for f in (os.path.join(CASA_TERMUX, ".config/tod/tod.conf"), os.path.join(L.config_dir(), "tod.conf")):
        try:
            for r in open(f):
                if r.startswith("ACCENTO="):
                    v = r.split("=", 1)[1].strip().strip('"')
                    if re.fullmatch(r"#[0-9a-fA-F]{6}", v):
                        a = v
            break
        except OSError:
            continue
    return a


def rgb(h):
    return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))


def esadecimale(c):
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(x * 255))) for x in c)


def schiarisci(h, t):
    return esadecimale(tuple(x + (1 - x) * t for x in rgb(h)))


def scurisci(h, t):
    return esadecimale(tuple(x * (1 - t) for x in rgb(h)))


def luminanza(h):
    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(x) for x in rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def scuro():
    forzato = os.environ.get("GTK_THEME", "")   # GTK_THEME=Tema:dark o un tema -Dark vince sulle impostazioni
    if forzato:
        return "dark" in forzato.lower()
    s = Gtk.Settings.get_default()
    return bool(s) and "dark" in (s.get_property("gtk-theme-name") or "").lower()


# fondi pieni come Windows 11 (Mica senza trasparenza: DeX OS non vuole il vetro)
COLORI = {
    False: dict(FONDO="#f3f3f3", STRATO="#f9f9f9", CARTA="#ffffff", CARTASOPRA="#f6f6f6", TESTO="#1a1a1a",
                TESTO2="#5c5c5c", TENUE="#8a8a8a", LINEA="rgba(0, 0, 0, 0.08)", BORDO="rgba(0, 0, 0, 0.06)",
                PASSAGGIO="rgba(0, 0, 0, 0.04)", PREMUTO="rgba(0, 0, 0, 0.025)", CAMPO="#ffffff",
                CAMPOBORDO="rgba(0, 0, 0, 0.45)", BARRA="#fbfbfb", TRACCIA="rgba(0, 0, 0, 0.45)",
                MANOPOLA="#ffffff", VISTA="#f3f3f3", OMBRA="rgba(0, 0, 0, 0.14)"),
    True: dict(FONDO="#202020", STRATO="#272727", CARTA="#2d2d2d", CARTASOPRA="#323232", TESTO="#ffffff",
               TESTO2="#c8c8c8", TENUE="#9a9a9a", LINEA="rgba(255, 255, 255, 0.08)", BORDO="rgba(0, 0, 0, 0.25)",
               PASSAGGIO="rgba(255, 255, 255, 0.06)", PREMUTO="rgba(255, 255, 255, 0.04)", CAMPO="#2d2d2d",
               CAMPOBORDO="rgba(255, 255, 255, 0.55)", BARRA="#2a2a2a", TRACCIA="rgba(255, 255, 255, 0.55)",
               MANOPOLA="#454545", VISTA="#1c1c1c", OMBRA="rgba(0, 0, 0, 0.4)"),
}


def colori():
    """i valori di oggi (chiaro/scuro e accento): per il CSS e per i disegni con cairo."""
    s = scuro()
    a = accento()
    pieno = schiarisci(a, 0.45) if s else a          # Windows 11: nello scuro l'accento "chiaro"
    su_acc = "#000000" if luminanza(pieno) > 0.36 else "#ffffff"
    c = dict(COLORI[s], ACCENTO=pieno, SUACCENTO=su_acc, ACCTESTO=schiarisci(a, 0.45) if s else scurisci(a, 0.08),
             ACCSOPRA=schiarisci(pieno, 0.12) if not s else scurisci(pieno, 0.08))
    c["scuro"] = s
    return c


CSS_BASE = """
.w11 { background-color: FONDO; color: TESTO; font-family: Inter, sans-serif; font-size: 10pt; }
.w11 .strato { background-color: STRATO; border-top-left-radius: 8px; border-left: 1px solid LINEA; border-top: 1px solid LINEA; }
.w11 .strato.pieno { border-radius: 0; border: none; }
.w11 .nav { background-color: FONDO; }
.w11 .nav list, .w11 .nav row { background: transparent; }
.w11 .nav row { border-radius: 4px; margin: 2px 6px; padding: 0 10px; min-height: 36px; outline: none; }
.w11 .nav row:hover { background-color: PASSAGGIO; }
.w11 .nav row:selected, .w11 .nav row.scelta {
  background-color: PASSAGGIO; background-image: linear-gradient(ACCENTO, ACCENTO);
  background-size: 3px 16px; background-position: 0 center; background-repeat: no-repeat;
}
.w11 .nav row label { color: TESTO; }
.w11 .nav row image { color: TESTO; }
.w11 .nav .gruppo { color: TENUE; font-size: 8.5pt; font-weight: 600; margin: 14px 18px 4px 18px; }
.w11 entry {
  background-color: CAMPO; background-image: none; color: TESTO; border: 1px solid LINEA; border-bottom-color: CAMPOBORDO;
  border-radius: 4px; box-shadow: none; min-height: 30px; padding: 0 8px;
}
.w11 entry:focus { border-bottom: 2px solid ACCENTO; }
.w11 entry image { color: TENUE; }
.w11 .titolo { font-size: 20pt; font-weight: 600; }
.w11 .titolo2 { font-size: 14pt; font-weight: 600; }
.w11 .grande { font-size: 26pt; font-weight: 700; }
.w11 .nome { font-weight: 600; }
.w11 .sotto { color: TESTO2; }
.w11 .tenue { color: TENUE; font-size: 9pt; }
.w11 .numeri { font-feature-settings: "tnum"; }
.w11 button {
  background-color: CARTA; background-image: none; color: TESTO; border: 1px solid LINEA; border-bottom-color: BORDO;
  border-radius: 4px; box-shadow: none; text-shadow: none; padding: 4px 12px; min-height: 22px; outline: none;
  -gtk-icon-shadow: none;
}
.w11 button:hover { background-color: CARTASOPRA; }
.w11 button:active { background-color: PREMUTO; color: TESTO2; }
.w11 button:disabled { color: TENUE; }
.w11 button label, .w11 button image { color: inherit; }
.w11 button.accento { background-color: ACCENTO; color: SUACCENTO; border-color: transparent; }
.w11 button.accento:hover { background-color: ACCSOPRA; }
.w11 button.lieve, .w11 button.icona {
  background-color: transparent; border-color: transparent; padding: 6px; min-width: 20px; min-height: 20px;
}
.w11 button.lieve { padding: 6px 10px; }
.w11 button.lieve:hover, .w11 button.icona:hover, .w11 button.icona:checked { background-color: PASSAGGIO; }
.w11 button.icona:active, .w11 button.lieve:active { background-color: PREMUTO; }
.w11 button.icona:checked image { color: ACCTESTO; }
.w11 button.attivo image, .w11 button.attivo { color: ACCTESTO; }
.w11 button.play {
  background-color: ACCENTO; color: SUACCENTO; border: none; border-radius: 999px; padding: 0;
  min-width: 40px; min-height: 40px;
}
.w11 button.play:hover { background-color: ACCSOPRA; }
.w11 button.play image { color: SUACCENTO; }
.w11 button.tondo { border-radius: 999px; padding: 8px; min-width: 24px; min-height: 24px; }
.w11 button.scheda {
  background: transparent; border: none; padding: 6px 2px 9px 2px; margin: 0 10px 0 0; color: TESTO2;
  border-radius: 0; font-weight: 400;
}
.w11 button.scheda:hover { color: TESTO; background: transparent; }
.w11 button.scheda.scelta {
  color: TESTO; font-weight: 600; background-image: linear-gradient(ACCENTO, ACCENTO);
  background-size: 16px 3px; background-position: center bottom; background-repeat: no-repeat;
}
.w11 scale { padding: 6px 0; }
.w11 scale trough { min-height: 4px; min-width: 4px; border-radius: 2px; background-color: TRACCIA; border: none; }
.w11 scale highlight { min-height: 4px; min-width: 4px; border-radius: 2px; background-color: ACCENTO; border: none; }
.w11 scale slider {
  min-width: 20px; min-height: 20px; margin: -8px; border-radius: 999px; border: 1px solid LINEA;
  background-color: MANOPOLA; background-image: radial-gradient(circle closest-side, ACCENTO 54%, MANOPOLA 62%);
  box-shadow: 0 1px 2px OMBRA;
}
.w11 scale slider:hover { background-image: radial-gradient(circle closest-side, ACCENTO 66%, MANOPOLA 74%); }
.w11 scale slider:active { background-image: radial-gradient(circle closest-side, ACCENTO 44%, MANOPOLA 52%); }
.w11 .barra { background-color: BARRA; border-top: 1px solid LINEA; }
.w11 .scheda-carta { background-color: CARTA; border: 1px solid LINEA; border-radius: 8px; }
.w11 .pannello { background-color: STRATO; border-left: 1px solid LINEA; }
.w11 .avviso { background-color: CARTA; border: 1px solid LINEA; border-radius: 8px; padding: 10px 14px; box-shadow: 0 4px 12px OMBRA; }
.w11 separator { background-color: LINEA; min-height: 1px; min-width: 1px; }
.w11 scrollbar { background: transparent; border: none; }
.w11 scrollbar slider { min-width: 3px; min-height: 3px; border-radius: 3px; background-color: alpha(TESTO, 0.35); border: none; }
.w11 scrollbar:hover slider { min-width: 6px; }
.w11 treeview.view { background-color: transparent; color: TESTO; }
.w11 treeview.view:hover { background-color: PASSAGGIO; }
.w11 treeview.view:selected { background-color: alpha(ACCENTO, 0.22); color: TESTO; }
.w11 treeview.view header button {
  background: transparent; border: none; border-bottom: 1px solid LINEA; color: TESTO2; font-weight: 600;
  font-size: 9pt; padding: 6px 8px; border-radius: 0;
}
.w11 popover, .w11 menu, .w11 .menu, .w11 .context-menu { background-color: CARTA; color: TESTO; border: 1px solid LINEA; border-radius: 8px; }
.w11 popover modelbutton { padding: 6px 12px; border-radius: 4px; color: TESTO; }
.w11 popover modelbutton:hover { background-color: PASSAGGIO; }
.w11 popover.menu-w11 { padding: 4px; }
.w11 menuitem { padding: 6px 12px; color: TESTO; }
.w11 menuitem:hover { background-color: PASSAGGIO; }
.w11 checkbutton check, .w11 radiobutton radio { color: ACCENTO; }
.w11 .vista { background-color: VISTA; }
.w11 .nero { background-color: #000000; }
.w11 .rosso { background-color: #c42b1c; color: #ffffff; border-color: transparent; }
.w11 .rosso:hover { background-color: #b02718; }
"""


def css(extra=""):
    c = colori()
    out = CSS_BASE + extra
    for k in sorted((k for k in c if k != "scuro"), key=len, reverse=True):
        out = out.replace(k, c[k])
    return re.sub(r"(\d+(?:\.\d+)?)px", lambda m: "%dpx" % max(1, px(float(m.group(1)))) if float(m.group(1)) < 900
                  else m.group(0), out).encode()


class Stile:
    """il CSS dell'app, rifatto quando cambia il tema (chiaro/scuro) o l'accento."""

    def __init__(self, extra="", cambiato=None):
        self.extra, self.cambiato = extra, cambiato
        self.prov = Gtk.CssProvider()
        self.prov.load_from_data(css(extra))
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), self.prov,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.Settings.get_default().connect("notify::gtk-theme-name", lambda *_: self.rifai())

    def rifai(self):
        self.prov.load_from_data(css(self.extra))
        if self.cambiato:
            self.cambiato()


# --- piccoli widget -------------------------------------------------------------------------------------------
def classe(w, *nomi):
    for n in nomi:
        for x in n.split():
            w.get_style_context().add_class(x)
    return w


def etichetta(testo="", cls=None, xalign=0.0, ellissi=True, righe=1, larga=0):
    lab = Gtk.Label(label=testo, xalign=xalign)
    if ellissi:
        lab.set_ellipsize(Pango.EllipsizeMode.END)
    if righe > 1:
        lab.set_line_wrap(True)
        lab.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lab.set_lines(righe)
    if larga:
        lab.set_max_width_chars(larga)
    if cls:
        classe(lab, cls)
    return lab


def immagine_icona(nome, lato=16):
    im = Gtk.Image.new_from_icon_name(nome, Gtk.IconSize.BUTTON)
    im.set_pixel_size(px(lato))
    return im


def bottone(icona=None, testo=None, suggerimento=None, azione=None, cls="icona", lato=16, attiva=False):
    b = Gtk.ToggleButton() if attiva else Gtk.Button()
    b.set_can_focus(False)
    if icona and testo:
        box = Gtk.Box(spacing=px(8))
        box.pack_start(immagine_icona(icona, lato), False, False, 0)
        box.pack_start(Gtk.Label(label=testo), False, False, 0)
        b.add(box)
    elif icona:
        b.add(immagine_icona(icona, lato))
    elif testo:
        b.set_label(testo)
    if suggerimento:
        b.set_tooltip_text(suggerimento)
    if azione:
        b.connect("toggled" if attiva else "clicked", lambda *_: azione())
    return classe(b, cls) if cls else b


def metti_icona(b, nome, lato=16):
    figlio = b.get_child()
    if isinstance(figlio, Gtk.Image):
        figlio.set_from_icon_name(nome, Gtk.IconSize.BUTTON)
        figlio.set_pixel_size(px(lato))


class Schede(Gtk.Box):
    """le schede a testo di Media Player (Brani · Album · Artisti) con la lineetta nell'accento sotto la scelta."""

    def __init__(self, voci, cambiata):
        super().__init__(spacing=0)
        self.pulsanti, self.cambiata, self.scelta = {}, cambiata, None
        for k, testo in voci:
            b = classe(Gtk.Button(label=testo), "scheda")
            b.set_can_focus(False)
            b.connect("clicked", lambda _b, k=k: self.scegli(k))
            self.pulsanti[k] = b
            self.pack_start(b, False, False, 0)

    def scegli(self, k, avvisa=True):
        for kk, b in self.pulsanti.items():
            ctx = b.get_style_context()
            (ctx.add_class if kk == k else ctx.remove_class)("scelta")
        self.scelta = k
        if avvisa:
            self.cambiata(k)


class Nav(Gtk.Box):
    """la barra di navigazione a sinistra (NavigationView di Windows 11): ricerca, voci, gruppi."""

    def __init__(self, scelta, cerca=None, larga=280):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        classe(self, "nav")
        self.set_size_request(px(larga), -1)
        self.scelta_cb, self.voci = scelta, {}
        self.cerca = None
        if cerca is not None:
            self.cerca = Gtk.SearchEntry(placeholder_text=T("Cerca", "Search"))
            self.cerca.set_margin_start(px(12))
            self.cerca.set_margin_end(px(12))
            self.cerca.set_margin_top(px(10))
            self.cerca.set_margin_bottom(px(8))
            self.cerca.connect("search-changed", lambda e: cerca(e.get_text()))
            self.pack_start(self.cerca, False, False, 0)
        self.lista = Gtk.ListBox()
        self.lista.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.lista.connect("row-activated", lambda _l, r: getattr(r, "chiave", None) and self.scelta_cb(r.chiave))
        self.pack_start(self.lista, False, False, 0)
        self.fondo = Gtk.ListBox()
        self.fondo.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.fondo.connect("row-activated", lambda _l, r: getattr(r, "chiave", None) and self.scelta_cb(r.chiave))
        self.pack_end(self.fondo, False, False, px(6))

    def voce(self, chiave, testo, icona, in_fondo=False):
        r = Gtk.ListBoxRow()
        r.chiave = chiave
        b = Gtk.Box(spacing=px(14))
        b.pack_start(immagine_icona(icona, 16), False, False, 0)
        b.pack_start(etichetta(testo), True, True, 0)
        r.add(b)
        (self.fondo if in_fondo else self.lista).add(r)
        self.voci[chiave] = r
        return r

    def gruppo(self, testo):
        r = Gtk.ListBoxRow(selectable=False, activatable=False)
        r.chiave = None
        r.add(etichetta(testo, "gruppo"))
        classe(r, "gruppo-riga")
        self.lista.add(r)
        return r

    def segna(self, chiave):
        for lb in (self.lista, self.fondo):
            lb.unselect_all()
        r = self.voci.get(chiave)
        if r:
            r.get_parent().select_row(r)


def avviso(overlay, testo, azione=None, testo_azione=None, secondi=5):
    """un riquadro piccolo in basso al centro, che sparisce da solo (Media Player: "Riprendo da 12:34")."""
    box = classe(Gtk.Box(spacing=px(12)), "avviso")
    box.set_halign(Gtk.Align.CENTER)
    box.set_valign(Gtk.Align.END)
    box.set_margin_bottom(px(24))
    box.pack_start(etichetta(testo, ellissi=False), False, False, 0)
    if azione:
        def fatto(*_):
            azione()
            if box.get_parent():
                overlay.remove(box)
        box.pack_start(bottone(testo=testo_azione, cls="lieve", azione=fatto), False, False, 0)
    overlay.add_overlay(box)
    box.show_all()

    def via():
        if box.get_parent():
            overlay.remove(box)
        return False
    GLib.timeout_add(secondi * 1000, via)
    return box


# --- disegni con cairo ----------------------------------------------------------------------------------------
def rettangolo_tondo(cr, x, y, w, h, r):
    r = max(0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def pixbuf_coprente(cr, pb, x, y, w, h, filtro=None):
    """l'immagine che riempie tutto il riquadro (tagliata al centro), come le miniature di Foto."""
    import cairo
    if pb is None or w <= 0 or h <= 0:
        return
    s = max(w / pb.get_width(), h / pb.get_height())
    cr.save()
    cr.rectangle(x, y, w, h)
    cr.clip()
    cr.translate(x + (w - pb.get_width() * s) / 2, y + (h - pb.get_height() * s) / 2)
    cr.scale(s, s)
    Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
    cr.get_source().set_filter(filtro if filtro is not None else cairo.FILTER_GOOD)
    cr.paint()
    cr.restore()


def colore_medio(pb):
    """il colore medio di un'immagine (fondo di "Riproduzione in corso")."""
    try:
        p = pb.scale_simple(1, 1, GdkPixbuf.InterpType.TILES)
        d = p.get_pixels()
        return d[0] / 255, d[1] / 255, d[2] / 255
    except (GLib.Error, AttributeError, IndexError):
        return None


_ICONE_CACHE = {}


def pixbuf_icona(nome, lato, colore=None):
    k = (nome, lato, colore)
    if k not in _ICONE_CACHE:
        pb = None
        try:
            info = Gtk.IconTheme.get_default().lookup_icon(nome, lato, Gtk.IconLookupFlags.FORCE_SIZE)
            if info:
                if colore:
                    rgba = Gdk.RGBA()
                    rgba.parse(colore)
                    pb = info.load_symbolic(rgba, None, None, None)[0]
                else:
                    pb = info.load_icon()
        except GLib.Error:
            pb = None
        _ICONE_CACHE[k] = pb
    return _ICONE_CACHE[k]


class IconaSalto(Gtk.DrawingArea):
    """il simbolo "indietro di 10 s" / "avanti di 30 s" di Media Player: freccia tonda con il numero dentro."""

    def __init__(self, secondi, avanti):
        super().__init__()
        self.secondi, self.avanti = secondi, avanti
        self.set_size_request(px(20), px(20))
        self.connect("draw", self.disegna)

    def disegna(self, w, cr):
        al = w.get_allocation()
        c = w.get_style_context().get_color(w.get_state_flags())
        cr.set_source_rgba(c.red, c.green, c.blue, c.alpha)
        cx, cy, r = al.width / 2, al.height / 2, min(al.width, al.height) / 2 - px(1.5)
        cr.set_line_width(max(1.2, px(1.4)))
        if self.avanti:
            cr.arc(cx, cy, r, math.radians(-60), math.radians(240))
            ax, ay = cx + r * math.cos(math.radians(-60)), cy + r * math.sin(math.radians(-60))
        else:
            cr.arc_negative(cx, cy, r, math.radians(240), math.radians(-60))
            ax, ay = cx + r * math.cos(math.radians(240)), cy + r * math.sin(math.radians(240))
        cr.stroke()
        d = px(3.2)
        s = 1 if self.avanti else -1
        cr.move_to(ax + s * d * 0.2, ay - d)
        cr.line_to(ax + s * d * 0.9, ay + d * 0.15)
        cr.line_to(ax - s * d * 0.6, ay + d * 0.5)
        cr.close_path()
        cr.fill()
        lay = PangoCairo.create_layout(cr)
        fd = Pango.FontDescription.from_string("Inter Bold %.1f" % (6.0 * 96 / 72))
        fd.set_absolute_size(px(7.5) * Pango.SCALE)
        lay.set_font_description(fd)
        lay.set_text(str(self.secondi), -1)
        tw, th = lay.get_pixel_size()
        cr.move_to(cx - tw / 2, cy - th / 2 + px(0.5))
        PangoCairo.show_layout(cr, lay)
        return False


class IconaDisegnata(Gtk.DrawingArea):
    """casuale, ripeti, ripeti uno: disegnate qui (il tema DeX OS non le ha: ripiegava sull'icona "elenco")."""

    def __init__(self, tipo, lato=16):
        super().__init__()
        self.tipo = tipo
        self.set_size_request(px(lato), px(lato))
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self.disegna)

    def metti(self, tipo):
        self.tipo = tipo
        self.queue_draw()

    def disegna(self, w, cr):
        al = w.get_allocation()
        c = w.get_style_context().get_color(w.get_state_flags())
        cr.set_source_rgba(c.red, c.green, c.blue, c.alpha)
        s = min(al.width, al.height) / 16.0
        cr.translate((al.width - 16 * s) / 2, (al.height - 16 * s) / 2)
        cr.scale(s, s)
        cr.set_line_width(1.35)
        cr.set_line_cap(1)   # tondo
        cr.set_line_join(1)

        def punta(x, y, d):
            cr.move_to(x - 2.6 * d, y - 2.4)
            cr.line_to(x, y)
            cr.line_to(x - 2.6 * d, y + 2.4)
            cr.stroke()
        if self.tipo == "casuale":
            cr.move_to(1.5, 4.5)
            cr.line_to(4.5, 4.5)
            cr.curve_to(8.5, 4.5, 7.5, 11.5, 11.5, 11.5)
            cr.line_to(14.2, 11.5)
            cr.stroke()
            cr.move_to(1.5, 11.5)
            cr.line_to(4.5, 11.5)
            cr.curve_to(8.5, 11.5, 7.5, 4.5, 11.5, 4.5)
            cr.line_to(14.2, 4.5)
            cr.stroke()
            punta(14.5, 4.5, 1)
            punta(14.5, 11.5, 1)
        else:
            cr.move_to(10.5, 3.5)
            cr.line_to(4.5, 3.5)
            cr.arc_negative(4.5, 7.5, 4, -math.pi / 2, math.pi)
            cr.line_to(0.5, 8.5)
            cr.arc_negative(4.5, 8.5, 4, math.pi, math.pi / 2)
            cr.line_to(11.5, 12.5)
            cr.arc_negative(11.5, 8.5, 4, math.pi / 2, 0)
            cr.line_to(15.5, 7.5)
            cr.arc_negative(11.5, 7.5, 4, 0, -math.pi / 2)
            cr.stroke()
            punta(11.2, 3.5, 1)
            if self.tipo == "ripeti1":
                cr.set_line_width(1.2)
                cr.move_to(7.0, 6.9)
                cr.line_to(8.4, 5.9)
                cr.line_to(8.4, 10.2)
                cr.stroke()
        return False


def bottone_disegno(tipo, suggerimento=None, azione=None, testo=None, cls="icona"):
    b = Gtk.Button()
    b.set_can_focus(False)
    ic = IconaDisegnata(tipo)
    if testo:
        box = Gtk.Box(spacing=px(8))
        box.pack_start(ic, False, False, 0)
        box.pack_start(Gtk.Label(label=testo), False, False, 0)
        b.add(box)
    else:
        b.add(ic)
    b.icona = ic
    if suggerimento:
        b.set_tooltip_text(suggerimento)
    if azione:
        b.connect("clicked", lambda *_: azione())
    return classe(b, cls) if cls else b


def bottone_salto(secondi, avanti, suggerimento, azione):
    b = classe(Gtk.Button(), "icona")
    b.set_can_focus(False)
    b.add(IconaSalto(secondi, avanti))
    b.set_tooltip_text(suggerimento)
    b.connect("clicked", lambda *_: azione())
    return b


# --- miniature ------------------------------------------------------------------------------------------------
_GST = None


def gst():
    """GStreamer solo quando serve (Foto lo usa per le miniature dei video della fotocamera)."""
    global _GST
    if _GST is None:
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        _GST = Gst
    return _GST


def uri_di(percorso):
    return GLib.filename_to_uri(os.path.abspath(percorso), None)


def cartella_miniature():
    return os.path.join(GLib.get_user_cache_dir(), "thumbnails", "large")


def leggi_miniatura(percorso, mtime):
    """quella di freedesktop (fatta da Esplora file o da noi), se è ancora del file così com'è."""
    m = os.path.join(cartella_miniature(), hashlib.md5(uri_di(percorso).encode()).hexdigest() + ".png")
    try:
        pb = GdkPixbuf.Pixbuf.new_from_file(m)
    except GLib.Error:
        return None, None
    if pb.get_option("tEXt::Thumb::MTime") != str(int(mtime)):
        return None, None
    dur = pb.get_option("tEXt::Thumb::Movie::Length")
    try:
        dur = float(dur) if dur else None
    except ValueError:
        dur = None
    return pb, dur


def salva_miniatura(percorso, mtime, pb, durata=None):
    d = cartella_miniature()
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        uri = uri_di(percorso)
        m = os.path.join(d, hashlib.md5(uri.encode()).hexdigest() + ".png")
        chiavi = ["tEXt::Thumb::URI", "tEXt::Thumb::MTime", "tEXt::Software"]
        valori = [uri, str(int(mtime)), "Termux On DeX"]
        if durata:
            chiavi.append("tEXt::Thumb::Movie::Length")
            valori.append(str(int(durata)))
        tmp = "%s.%d.tmp" % (m, os.getpid())
        pb.savev(tmp, "png", chiavi, valori)
        os.chmod(tmp, 0o600)
        os.replace(tmp, m)
    except (GLib.Error, OSError):
        pass


def riduci(pb, lato):
    w, h = pb.get_width(), pb.get_height()
    s = lato / max(w, h)
    if s >= 1:
        return pb
    return pb.scale_simple(max(1, round(w * s)), max(1, round(h * s)), GdkPixbuf.InterpType.BILINEAR)


def miniatura_immagine(percorso, lato=256):
    try:
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(percorso, lato, lato, True)
    except GLib.Error:
        # GIF animate: gdk-pixbuf 2.42 vuole caricarle intere ("Not all frames..."): il primo fotogramma
        pb = riduci(GdkPixbuf.PixbufAnimation.new_from_file(percorso).get_static_image(), lato)
    return pb.apply_embedded_orientation() or pb


def fotogramma(percorso, larghezza=0, dove=0.1):
    """un fotogramma del video come pixbuf (in un filo a parte: GStreamer lo permette) e la durata in secondi."""
    Gst = gst()
    pb_ = Gst.ElementFactory.make("playbin", None)
    if pb_ is None:
        return None, None
    pb_.set_property("uri", uri_di(percorso))
    pb_.set_property("flags", 0x1)   # solo video: niente audio
    sink = Gst.parse_bin_from_description(
        "videoconvertscale ! video/x-raw,format=RGB,pixel-aspect-ratio=1/1 ! appsink name=a sync=false max-buffers=1",
        True)
    pb_.set_property("video-sink", sink)
    app = sink.get_by_name("a")
    try:
        pb_.set_state(Gst.State.PAUSED)
        if pb_.get_state(8 * Gst.SECOND)[0] == Gst.StateChangeReturn.FAILURE:
            return None, None
        ok, d = pb_.query_duration(Gst.Format.TIME)
        durata = d / Gst.SECOND if ok and d > 0 else None
        if durata and dove:
            t = min(durata * dove, 15.0) if dove < 1 else min(dove, durata * 0.9)
            pb_.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, int(t * Gst.SECOND))
            pb_.get_state(8 * Gst.SECOND)
        campione = app.emit("pull-preroll")
        if campione is None:
            return None, durata
        s = campione.get_caps().get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        buf = campione.get_buffer()
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None, durata
        try:
            dati = bytes(mi.data)
        finally:
            buf.unmap(mi)
        passo = (w * 3 + 3) & ~3
        img = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(dati), GdkPixbuf.Colorspace.RGB, False, 8, w, h, passo)
        return (riduci(img, larghezza) if larghezza else img), durata
    finally:
        pb_.set_state(Gst.State.NULL)


class Miniature:
    """le miniature in sottofondo, con l'ultima chiesta per prima (quelle che si vedono adesso).
    In memoria al più MAX; su disco nella cache di freedesktop, condivisa con Esplora file."""
    MAX = 500

    def __init__(self, fili=2):
        self.ram = collections.OrderedDict()
        self.durate = {}
        self.attese = {}       # chiave → [richiamate]
        self.coda = []         # (chiave, percorso, mtime, tipo, voluto)
        self.falliti = set()
        self.cv = threading.Condition()
        for _ in range(fili):
            threading.Thread(target=self._lavora, daemon=True).start()

    def subito(self, percorso):
        pb = self.ram.get(percorso)
        if pb is not None:
            self.ram.move_to_end(percorso)
        return pb

    def chiedi(self, percorso, mtime, tipo, fatto, voluto=None):
        """la miniatura se c'è già, se no None e fatto(percorso, pixbuf) quando è pronta."""
        pb = self.subito(percorso)
        if pb is not None or percorso in self.falliti:
            return pb
        with self.cv:
            if percorso in self.attese:
                self.attese[percorso].append(fatto)
                # di nuovo in cima: è tornata visibile
                for i, v in enumerate(self.coda):
                    if v[0] == percorso:
                        self.coda.append(self.coda.pop(i))
                        break
            else:
                self.attese[percorso] = [fatto]
                self.coda.append((percorso, percorso, mtime, tipo, voluto))
            self.cv.notify()
        return None

    def _lavora(self):
        while True:
            with self.cv:
                while not self.coda:
                    self.cv.wait()
                chiave, percorso, mtime, tipo, voluto = self.coda.pop()
            if voluto is not None and not voluto(chiave):
                with self.cv:
                    self.attese.pop(chiave, None)
                continue
            pb, dur = None, None
            try:
                pb, dur = leggi_miniatura(percorso, mtime)
                if pb is None:
                    if tipo == "video":
                        img, dur = fotogramma(percorso)
                        pb = riduci(img, 256) if img is not None else None
                    else:
                        pb = miniatura_immagine(percorso)
                    if pb is not None and tipo in ("video", "immagine"):
                        salva_miniatura(percorso, mtime, pb, dur)
            except (GLib.Error, OSError, ValueError, TypeError):
                pb = None
            GLib.idle_add(self._consegna, chiave, pb, dur)

    def _consegna(self, chiave, pb, dur):
        with self.cv:
            cbs = self.attese.pop(chiave, [])
        if pb is None:
            self.falliti.add(chiave)
        else:
            self.ram[chiave] = pb
            while len(self.ram) > self.MAX:
                self.ram.popitem(last=False)
        if dur:
            self.durate[chiave] = dur
        for cb in cbs:
            cb(chiave, pb)
        return False

    def dimentica(self, percorso):
        self.ram.pop(percorso, None)
        self.falliti.discard(percorso)


# --- la griglia --------------------------------------------------------------------------------------------------
class Griglia(Gtk.Box):
    """una griglia di miniature (foto, video, copertine) a gruppi con titolo ("novembre 2026"); disegna solo le righe
    visibili, quindi regge anche le 3000 foto della fotocamera. Gli elementi sono dizionari: percorso (miniatura),
    mtime, tipo (immagine/video/nessuna), titolo e sotto (testo sotto, se righe_testo), segno (in basso a destra:
    durata), cuore, icona (se non c'è miniatura)."""

    def __init__(self, miniature, lato=180, proporzione=1.0, righe_testo=0, rotonda=False, spazio=4, margine=24):
        super().__init__()
        self.mini = miniature
        self.lato, self.prop, self.righe_testo, self.rotonda = lato, proporzione, righe_testo, rotonda
        self.spazio, self.margine = spazio, margine
        self.area = Gtk.DrawingArea(can_focus=True, hexpand=True, vexpand=True)
        self.area.add_events(Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK |
                             Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                             Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK |
                             Gdk.EventMask.KEY_PRESS_MASK)
        self.adj = Gtk.Adjustment(value=0, lower=0, upper=1, step_increment=40, page_increment=400, page_size=1)
        self.barra = Gtk.Scrollbar(orientation=Gtk.Orientation.VERTICAL, adjustment=self.adj)
        self.pack_start(self.area, True, True, 0)
        self.pack_start(self.barra, False, False, 0)
        self.adj.connect("value-changed", lambda *_: self.area.queue_draw())
        self.area.connect("draw", self._disegna)
        self.area.connect("size-allocate", lambda *_: self._impagina())
        self.area.connect("scroll-event", self._rotella)
        self.area.connect("motion-notify-event", self._moto)
        self.area.connect("leave-notify-event", lambda *_: self._sopra(None))
        self.area.connect("button-press-event", self._premi)
        self.area.connect("button-release-event", self._rilascia)
        self.area.connect("key-press-event", self._tasto)
        self.gruppi, self.righe, self.ys = [], [], []
        self.sopra, self.premuto = None, None
        self.visibili = set()
        self.attiva = None      # attiva(elemento)
        self.menu = None        # menu(elemento, evento)
        self.vuoto = ""
        self.colori = colori()
        self.cella = (0, 0, 0)

    def metti(self, gruppi, in_cima=True):
        self.gruppi = [(t, list(e)) for t, e in gruppi]
        self._impagina()
        if in_cima:
            self.adj.set_value(0)
        self.area.queue_draw()

    def ridisegna(self):
        self.colori = colori()
        self.area.queue_draw()

    def metti_lato(self, lato):
        self.lato = lato
        self._impagina()
        self.area.queue_draw()

    def _impagina(self):
        al = self.area.get_allocation()
        W, H = al.width, al.height
        if W <= 1:
            return
        m, g = px(self.margine), px(self.spazio)
        utile = max(1, W - 2 * m)
        col = max(1, int((utile + g) // (px(self.lato) + g)))
        cw = (utile - (col - 1) * g) / col
        ih = cw * self.prop
        th = (px(8) + self.righe_testo * px(19)) if self.righe_testo else 0
        rh = ih + th
        y = px(8)
        righe = []
        for titolo, el in self.gruppi:
            if titolo:
                righe.append(("titolo", y, px(48), titolo))
                y += px(48)
            for i in range(0, len(el), col):
                righe.append(("riga", y, rh, el[i:i + col]))
                y += rh + (g if not self.righe_testo else px(14))
            y += px(12)
        self.righe = righe
        self.ys = [r[1] for r in righe]
        self.cella = (col, cw, ih)
        tot = y + px(24)
        v = min(self.adj.get_value(), max(0, tot - H))
        self.adj.configure(v, 0, max(tot, H), px(60), max(1, H * 0.9), max(1, H))

    def _visibili_righe(self, margine_extra=0):
        al = self.area.get_allocation()
        top = self.adj.get_value() - margine_extra
        bot = self.adj.get_value() + al.height + margine_extra
        i = max(0, bisect.bisect_right(self.ys, top) - 1)
        while i < len(self.righe) and self.righe[i][1] < bot:
            r = self.righe[i]
            if r[1] + r[2] >= top:
                yield r
            i += 1

    def _x_di(self, k):
        col, cw, ih = self.cella
        return px(self.margine) + k * (cw + px(self.spazio))

    def _disegna(self, w, cr):
        c = self.colori
        al = w.get_allocation()
        off = self.adj.get_value()
        col, cw, ih = self.cella
        ctx = w.get_pango_context()
        # prima cosa si vede (più una schermata sopra e sotto, pronta prima di arrivarci): le miniature chieste
        # per righe ormai lontane si saltano
        self.visibili = {e.get("percorso") for t, _y, _h, d in self._visibili_righe(al.height) if t == "riga"
                         for e in d}
        if not self.righe and self.vuoto:
            lay = Pango.Layout.new(ctx)
            lay.set_text(self.vuoto, -1)
            lay.set_width(int((al.width - px(80)) * Pango.SCALE))
            lay.set_alignment(Pango.Alignment.CENTER)
            r, g, b = rgb(c["TENUE"])
            cr.set_source_rgb(r, g, b)
            cr.move_to(px(40), al.height / 3)
            PangoCairo.show_layout(cr, lay)
        for tipo, y, h, dato in self._visibili_righe():
            yy = y - off
            if tipo == "titolo":
                lay = Pango.Layout.new(ctx)
                fd = ctx.get_font_description().copy()
                fd.set_size(int(12.5 * Pango.SCALE))
                fd.set_weight(Pango.Weight.SEMIBOLD)
                lay.set_font_description(fd)
                lay.set_text(dato, -1)
                cr.set_source_rgb(*rgb(c["TESTO"]))
                cr.move_to(px(self.margine), yy + px(18))
                PangoCairo.show_layout(cr, lay)
                continue
            for k, e in enumerate(dato):
                self._cella(cr, ctx, e, self._x_di(k), yy, cw, ih)
        return False

    def voluta(self, percorso):
        return percorso in self.visibili

    def _cella(self, cr, ctx, e, x, y, cw, ih):
        c = self.colori
        r = 0 if not self.rotonda else ih / 2
        rr = px(4) if not self.rotonda else ih / 2
        pb = None
        p = e.get("percorso")
        if p and e.get("tipo") in ("immagine", "video"):
            pb = self.mini.chiedi(p, e.get("mtime", 0), e["tipo"], self._pronta, self.voluta)
        cr.save()
        if self.rotonda:
            cr.arc(x + cw / 2, y + ih / 2, min(cw, ih) / 2, 0, 2 * math.pi)
        else:
            rettangolo_tondo(cr, x, y, cw, ih, rr)
        cr.clip()
        if pb is not None:
            pixbuf_coprente(cr, pb, x, y, cw, ih)
        else:
            cr.set_source_rgb(*rgb(c["CARTA"] if not c["scuro"] else c["CARTASOPRA"]))
            cr.paint()
            if e.get("lettere"):
                cr.set_source_rgb(*rgb(c["ACCENTO"]))
                cr.paint_with_alpha(0.22)
                lay = Pango.Layout.new(ctx)
                fd = ctx.get_font_description().copy()
                fd.set_absolute_size(ih * 0.32 * Pango.SCALE)
                fd.set_weight(Pango.Weight.SEMIBOLD)
                lay.set_font_description(fd)
                lay.set_text(e["lettere"], -1)
                tw, th = lay.get_pixel_size()
                cr.set_source_rgb(*rgb(c["TESTO"]))
                cr.move_to(x + (cw - tw) / 2, y + (ih - th) / 2)
                PangoCairo.show_layout(cr, lay)
            else:
                ic = pixbuf_icona(e.get("icona") or ("video-x-generic-symbolic" if e.get("tipo") == "video"
                                                     else "image-x-generic-symbolic"),
                                  px(min(48, int(ih / 3))), c["TENUE"])
                if ic:
                    Gdk.cairo_set_source_pixbuf(cr, ic, x + (cw - ic.get_width()) / 2, y + (ih - ic.get_height()) / 2)
                    cr.paint()
        if e is self.sopra:
            cr.set_source_rgba(1, 1, 1, 0.10) if c["scuro"] else cr.set_source_rgba(0, 0, 0, 0.06)
            cr.paint()
        cr.restore()
        if e is self.sopra and not self.rotonda:
            rettangolo_tondo(cr, x + 0.5, y + 0.5, cw - 1, ih - 1, rr)
            cr.set_source_rgba(*rgb(c["ACCENTO"]), 0.9)
            cr.set_line_width(max(1, px(1.5)))
            cr.stroke()
        segno = e.get("segno")
        if segno is None and e.get("tipo") == "video" and p in self.mini.durate:
            segno = L.tempo(self.mini.durate[p])
        if segno:
            lay = Pango.Layout.new(ctx)
            fd = ctx.get_font_description().copy()
            fd.set_size(int(8 * Pango.SCALE))
            fd.set_weight(Pango.Weight.SEMIBOLD)
            lay.set_font_description(fd)
            lay.set_text(segno, -1)
            tw, th = lay.get_pixel_size()
            bx, by = x + cw - tw - px(14), y + ih - th - px(10)
            rettangolo_tondo(cr, bx - px(5), by - px(2), tw + px(10), th + px(4), px(4))
            cr.set_source_rgba(0, 0, 0, 0.62)
            cr.fill()
            cr.set_source_rgb(1, 1, 1)
            cr.move_to(bx, by)
            PangoCairo.show_layout(cr, lay)
        if e.get("tipo") == "video" and not self.righe_testo:
            ic = pixbuf_icona("media-playback-start-symbolic", px(14), "#ffffff")
            if ic:
                cr.arc(x + px(17), y + ih - px(17), px(11), 0, 2 * math.pi)
                cr.set_source_rgba(0, 0, 0, 0.55)
                cr.fill()
                Gdk.cairo_set_source_pixbuf(cr, ic, x + px(17) - ic.get_width() / 2 + px(1),
                                            y + ih - px(17) - ic.get_height() / 2)
                cr.paint()
        if e.get("cuore"):
            self._cuore(cr, x + cw - px(18), y + px(16), px(7))
        if self.righe_testo:
            ty = y + ih + px(8)
            for i, (testo, peso, col) in enumerate(((e.get("titolo", ""), Pango.Weight.SEMIBOLD, c["TESTO"]),
                                                    (e.get("sotto", ""), Pango.Weight.NORMAL, c["TESTO2"]))):
                if i >= self.righe_testo or not testo:
                    continue
                lay = Pango.Layout.new(ctx)
                fd = ctx.get_font_description().copy()
                fd.set_weight(peso)
                if i:
                    fd.set_size(int(9 * Pango.SCALE))
                lay.set_font_description(fd)
                lay.set_text(testo, -1)
                lay.set_width(int(cw * Pango.SCALE))
                lay.set_ellipsize(Pango.EllipsizeMode.END)
                if self.rotonda:
                    lay.set_alignment(Pango.Alignment.CENTER)
                cr.set_source_rgb(*rgb(col))
                cr.move_to(x, ty + i * px(19))
                PangoCairo.show_layout(cr, lay)

    @staticmethod
    def _cuore(cr, cx, cy, r):
        cr.save()
        cr.translate(cx, cy)
        cr.move_to(0, r * 0.9)
        cr.curve_to(-r * 1.6, -r * 0.1, -r * 0.8, -r * 1.3, 0, -r * 0.45)
        cr.curve_to(r * 0.8, -r * 1.3, r * 1.6, -r * 0.1, 0, r * 0.9)
        cr.close_path()
        cr.set_source_rgba(0, 0, 0, 0.35)
        cr.set_line_width(px(2))
        cr.stroke_preserve()
        cr.set_source_rgb(1, 1, 1)
        cr.fill()
        cr.restore()

    def _pronta(self, percorso, pb):
        if percorso in self.visibili:
            self.area.queue_draw()

    def elemento_a(self, x, y):
        yy = y + self.adj.get_value()
        i = bisect.bisect_right(self.ys, yy) - 1
        if i < 0 or i >= len(self.righe):
            return None
        tipo, ry, h, dato = self.righe[i]
        if tipo != "riga" or yy > ry + h:
            return None
        col, cw, ih = self.cella
        for k, e in enumerate(dato):
            x0 = self._x_di(k)
            if x0 <= x <= x0 + cw:
                return e
        return None

    def _sopra(self, e):
        if e is not self.sopra:
            self.sopra = e
            win = self.area.get_window()
            if win:
                win.set_cursor(Gdk.Cursor.new_from_name(self.area.get_display(), "pointer") if e else None)
            self.area.queue_draw()

    def _moto(self, w, ev):
        self._sopra(self.elemento_a(ev.x, ev.y))
        return False

    def _premi(self, w, ev):
        self.area.grab_focus()
        self.premuto = self.elemento_a(ev.x, ev.y)
        if ev.button == 3 and self.premuto is not None and self.menu:
            self.menu(self.premuto, ev)
            return True
        return False

    def _rilascia(self, w, ev):
        e = self.elemento_a(ev.x, ev.y)
        if ev.button == 1 and e is not None and e is self.premuto and self.attiva:
            self.attiva(e)
        self.premuto = None
        return False

    def _rotella(self, w, ev):
        passo = px(100)
        if ev.direction == Gdk.ScrollDirection.SMOOTH:
            ok, dx, dy = ev.get_scroll_deltas()
            d = dy * passo * 0.6
        elif ev.direction == Gdk.ScrollDirection.UP:
            d = -passo
        elif ev.direction == Gdk.ScrollDirection.DOWN:
            d = passo
        else:
            return False
        v = self.adj.get_value() + d
        self.adj.set_value(max(0, min(v, self.adj.get_upper() - self.adj.get_page_size())))
        return True

    def _tasto(self, w, ev):
        k = Gdk.keyval_name(ev.keyval)
        pag = self.adj.get_page_size()
        v = {"Page_Down": pag * 0.9, "Page_Up": -pag * 0.9, "Down": px(60), "Up": -px(60)}.get(k)
        if k == "Home":
            self.adj.set_value(0)
        elif k == "End":
            self.adj.set_value(self.adj.get_upper())
        elif v:
            self.adj.set_value(self.adj.get_value() + v)
        else:
            return False
        return True


def apri_con_app(desktop, percorsi, ripiego=None):
    """i file nell'app di quella voce dello Start (tod-video.desktop...); se manca, ripiego (un comando)."""
    try:
        info = DesktopAppInfo.new(desktop)
    except (TypeError, GLib.Error):
        info = None
    if info:
        try:
            info.launch([Gio.File.new_for_path(p) for p in percorsi], None)
            return True
        except GLib.Error:
            pass
    if ripiego:
        import subprocess
        subprocess.Popen(ripiego + list(percorsi), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    return False


def apri_con(finestra, percorso):
    """"Apri con un'altra app" (la finestra di scelta di GTK, tradotta come tutte)."""
    f = Gio.File.new_for_path(percorso)
    dlg = Gtk.AppChooserDialog.new(finestra, Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT, f)

    def risposta(d, r):
        if r == Gtk.ResponseType.OK:
            info = d.get_app_info()
            if info:
                try:
                    info.launch([f], None)
                except GLib.Error:
                    pass
        d.destroy()
    dlg.connect("response", risposta)
    dlg.show()


def mostra_nella_cartella(percorso):
    """Esplora file sulla cartella, con il file scelto (D-Bus FileManager1 di Thunar; se no la sola cartella)."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1",
                      "ShowItems", GLib.Variant("(ass)", ([uri_di(percorso)], "")), None,
                      Gio.DBusCallFlags.NONE, 3000, None)
        return
    except GLib.Error:
        pass
    try:
        Gio.AppInfo.launch_default_for_uri(uri_di(os.path.dirname(percorso)), None)
    except GLib.Error:
        pass


def nel_cestino(percorso):
    """il file nel Cestino; None se è andata, se no il messaggio d'errore."""
    try:
        Gio.File.new_for_path(percorso).trash(None)
        return None
    except GLib.Error as e:
        return e.message


def elimina_davvero(percorso):
    try:
        os.remove(percorso)
        return None
    except OSError as e:
        return e.strerror
