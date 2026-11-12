#!/data/data/com.termux/files/usr/bin/python3
"""tod-cattura.py — Strumento di cattura come Windows 11 e macOS (12 novembre 2026).

Pietro: lo strumento di XFCE "ha l'aspetto Linux". Stamp ferma lo schermo, lo oscura e mostra in alto una barretta:
Rettangolo (si trascina), Finestra (si sceglie con un clic), Schermo intero; Esc annulla. Lo scatto va negli appunti e
nella cartella degli screenshot del telefono (DCIM/Screenshots, la stessa della Galleria di Android e della nostra app
Foto); in basso a destra un'anteprima come su macOS: un clic la apre in Foto.

  tod-cattura.py                 la barretta (Stamp, Win+Maiusc+S)
  tod-cattura.py --finestra      subito la finestra attiva (Alt+Stamp)
  tod-cattura.py --schermo       subito tutto lo schermo (Maiusc+Stamp)
"""
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "media"))
import comune as C  # noqa: E402
from comune import Gdk, GdkPixbuf, GLib, Gtk, px, T  # noqa: E402

try:
    import gi
    gi.require_version("GdkX11", "3.0")
    from gi.repository import GdkX11
except (ValueError, ImportError):
    GdkX11 = None

GLib.set_prgname("tod-cattura")
PIDFILE = os.path.join(os.environ.get("TMPDIR", "/tmp"), "tod-cattura.pid")


def via_la_precedente():
    """L'anteprima dello scatto prima non deve finire in quello nuovo (come macOS): SIGUSR1 al processo di prima, che la
    nasconde (resta vivo per gli appunti), e un attimo perché sparisca davvero dallo schermo."""
    try:
        with open(PIDFILE) as f:
            vecchio = int(f.read().strip())
        with open(f"/proc/{vecchio}/cmdline", "rb") as f:
            if b"tod-cattura.py" not in f.read() or vecchio == os.getpid():
                vecchio = 0
    except (OSError, ValueError):
        vecchio = 0
    if vecchio:
        try:
            os.kill(vecchio, __import__("signal").SIGUSR1)
            time.sleep(0.25)
        except OSError:
            pass
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))


def cartella_screenshot():
    """Dove li mette il telefono (Samsung: DCIM/Screenshots; altri: Pictures/Screenshots). TOD_CATTURA_CARTELLA per le prove."""
    if os.environ.get("TOD_CATTURA_CARTELLA"):
        return os.environ["TOD_CATTURA_CARTELLA"]
    for c in (os.path.join(C.L.TELEFONO, "DCIM", "Screenshots"), os.path.join(C.L.TELEFONO, "Pictures", "Screenshots")):
        if os.path.isdir(c):
            return c
    pic = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) or os.path.expanduser("~/Immagini")
    return os.path.join(pic, "Screenshots")


def nome_file(ora=None):
    """Screenshot_20261112-180512.png, come il telefono: la Galleria li ordina tutti insieme"""
    return time.strftime("Screenshot_%Y%m%d-%H%M%S.png", time.localtime(ora))


def xprop(*a):
    try:
        return subprocess.run(["xprop", "-notype", *a], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def finestre():
    """[(x, y, w, h)] delle finestre visibili con la loro cornice, dall'alto al basso (l'ordine di sovrapposizione)"""
    disp = Gdk.Display.get_default()
    out = []
    ids = re.findall(r"0x[0-9a-f]+", xprop("-root", "_NET_CLIENT_LIST_STACKING"))
    for xid in reversed(ids):
        p = xprop("-id", xid, "_NET_WM_STATE", "_NET_WM_WINDOW_TYPE", "_NET_WM_DESKTOP")
        if "HIDDEN" in p or re.search(r"_NET_WM_WINDOW_TYPE = .*_(DOCK|DESKTOP)", p):
            continue
        r = cornice(disp, int(xid, 16))
        if r and r[2] > 1 and r[3] > 1:
            out.append(r)
    return out


def cornice(disp, xid):
    if GdkX11 is None:
        return None
    try:
        w = GdkX11.X11Window.foreign_new_for_display(disp, xid)
        e = w.get_frame_extents() if w else None
    except Exception:   # noqa: BLE001 - finestra sparita intanto
        return None
    return (e.x, e.y, e.width, e.height) if e else None


def finestra_attiva():
    m = re.search(r"window id # (0x[0-9a-f]+)", xprop("-root", "_NET_ACTIVE_WINDOW"))
    if not m or int(m.group(1), 16) == 0:
        return None
    return cornice(Gdk.Display.get_default(), int(m.group(1), 16))


def dentro(r, W, H):
    """un rettangolo tagliato ai bordi dello schermo (le finestre possono sporgere)"""
    x, y, w, h = r
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    return (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None


CSS = """
.cattura-barra { background-color: rgba(250, 250, 252, 0.94); border-radius: 14px; padding: 6px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.28); border: 1px solid rgba(0, 0, 0, 0.08); }
.cattura-barra button { background: transparent; border: none; box-shadow: none; border-radius: 9px; padding: 7px 12px;
  color: #1d1d1f; min-height: 0; }
.cattura-barra button:hover { background-color: rgba(0, 0, 0, 0.06); }
.cattura-barra button:checked { background-color: ACCENTO; color: #ffffff; }
.cattura-barra button:checked image, .cattura-barra button:checked label { color: #ffffff; }
.cattura-barra separator { background-color: rgba(0, 0, 0, 0.12); min-width: 1px; margin: 6px 4px; }
.cattura-anteprima { background-color: rgba(250, 250, 252, 0.97); border-radius: 14px; padding: 10px;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.30); border: 1px solid rgba(0, 0, 0, 0.08); color: #1d1d1f; }
.cattura-anteprima .titolo { font-weight: 600; font-size: 10pt; }
.cattura-anteprima .sotto { color: #6e6e73; font-size: 8.5pt; }
.cattura-anteprima button { background: rgba(0, 0, 0, 0.05); border: none; box-shadow: none; border-radius: 99px;
  padding: 3px 12px; color: #1d1d1f; min-height: 0; font-size: 9pt; }
.cattura-anteprima button:hover { background: rgba(0, 0, 0, 0.10); }
.cattura-anteprima button.chiudi { padding: 3px 7px; }
.cattura-anteprima .immagine { border-radius: 8px; }
"""


def stile():
    p = Gtk.CssProvider()
    p.load_from_data(CSS.replace("ACCENTO", C.accento()).encode())
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)


def trasparente(w):
    """finestra con il canale alfa (angoli tondi e ombra): solo con il compositore acceso"""
    vis = w.get_screen().get_rgba_visual()
    if vis is not None and w.get_screen().is_composited():
        w.set_visual(vis)
        w.set_app_paintable(True)
        w.connect("draw", lambda _w, cr: (cr.set_source_rgba(0, 0, 0, 0), cr.set_operator(1), cr.paint(),
                                           cr.set_operator(2)) and False)


class Sovrapposta(Gtk.Window):
    """lo schermo fermo, oscurato; la zona scelta torna chiara. Modi: rettangolo, finestra, schermo"""

    def __init__(self, foto, finestre_viste, fatto):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.foto, self.fin, self.fatto = foto, finestre_viste, fatto
        self.W, self.H = foto.get_width(), foto.get_height()
        self.modo = "rettangolo"
        self.inizio = self.sel = None
        self.sopra = None   # finestra sotto il puntatore (modo finestra)
        self.move(0, 0)
        self.set_default_size(self.W, self.H)
        self.resize(self.W, self.H)
        ov = Gtk.Overlay()
        self.add(ov)
        self.area = Gtk.DrawingArea()
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                             Gdk.EventMask.POINTER_MOTION_MASK)
        self.area.connect("draw", self.disegna)
        self.area.connect("button-press-event", self.premuto)
        self.area.connect("motion-notify-event", self.mosso)
        self.area.connect("button-release-event", self.rilasciato)
        ov.add(self.area)
        barra = C.classe(Gtk.Box(spacing=px(2)), "cattura-barra")
        barra.set_halign(Gtk.Align.CENTER)
        barra.set_valign(Gtk.Align.START)
        barra.set_margin_top(px(22))
        self.pulsanti = {}
        prima = None
        for k, icona, testo in (("rettangolo", "selection-mode-symbolic", T("Rettangolo", "Rectangle")),
                                ("finestra", "focus-windows-symbolic", T("Finestra", "Window")),
                                ("schermo", "video-display-symbolic", T("Schermo intero", "Full screen"))):
            b = Gtk.RadioButton.new_from_widget(prima)
            b.set_mode(False)
            prima = prima or b
            box = Gtk.Box(spacing=px(7))
            box.pack_start(C.immagine_icona(icona, 16), False, False, 0)
            box.pack_start(Gtk.Label(label=testo), False, False, 0)
            b.add(box)
            b.connect("toggled", lambda w, k=k: w.get_active() and self.cambia(k))
            self.pulsanti[k] = b
            barra.pack_start(b, False, False, 0)
        barra.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        chiudi = Gtk.Button()
        chiudi.add(C.immagine_icona("window-close-symbolic", 16))
        chiudi.set_tooltip_text(T("Annulla (Esc)", "Cancel (Esc)"))
        chiudi.connect("clicked", lambda *_: self.fine(None))
        barra.pack_start(chiudi, False, False, 0)
        ov.add_overlay(barra)
        self.connect("key-press-event", self.tasto)
        self.connect("map-event", lambda *_: self.prendi())
        self.show_all()

    def prendi(self):
        """tastiera e puntatore tutti per noi (una finestra POPUP non riceve i tasti da sola)"""
        gw = self.get_window()
        seat = Gdk.Display.get_default().get_default_seat()
        cursore = Gdk.Cursor.new_from_name(Gdk.Display.get_default(), "crosshair")
        gw.set_cursor(cursore)
        for _ in range(20):   # il gestore delle finestre può tenere la tastiera per un attimo
            if seat.grab(gw, Gdk.SeatCapabilities.ALL, True, cursore, None, None, None) == Gdk.GrabStatus.SUCCESS:
                break
            time.sleep(0.05)
        return False

    def cambia(self, k):
        self.modo = k
        self.sel = self.inizio = None
        if k == "schermo":
            self.fine((0, 0, self.W, self.H))
            return
        self.area.queue_draw()

    def disegna(self, _w, cr):
        Gdk.cairo_set_source_pixbuf(cr, self.foto, 0, 0)
        cr.paint()
        zona = self.sel if self.modo == "rettangolo" else self.sopra
        cr.set_source_rgba(0, 0, 0, 0.42)
        cr.rectangle(0, 0, self.W, self.H)
        if zona:
            cr.set_fill_rule(1)   # pari-dispari: il buco della zona scelta resta chiaro
            cr.rectangle(*zona)
        cr.fill()
        if zona:
            r, g, b = C.rgb(C.accento())
            cr.set_source_rgb(r, g, b)
            cr.set_line_width(px(2))
            x, y, w, h = zona
            cr.rectangle(x + 1, y + 1, max(0, w - 2), max(0, h - 2))
            cr.stroke()
            if self.modo == "rettangolo" and w > 30 and h > 20:
                testo = f"{w} × {h}"
                cr.select_font_face("Inter")
                cr.set_font_size(px(12))
                ext = cr.text_extents(testo)
                tx, ty = x + w - ext.width - px(14), y + h + px(22)
                if ty > self.H - 4:
                    ty = y + h - px(10)
                cr.set_source_rgba(0, 0, 0, 0.7)
                C.rettangolo_tondo(cr, tx - px(8), ty - ext.height - px(6), ext.width + px(16), ext.height + px(12), px(6))
                cr.fill()
                cr.set_source_rgb(1, 1, 1)
                cr.move_to(tx, ty)
                cr.show_text(testo)
        return False

    def finestra_in(self, x, y):
        for r in self.fin:
            if r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]:
                return dentro(r, self.W, self.H)
        return (0, 0, self.W, self.H)   # niente finestre: la scrivania, cioè tutto lo schermo

    def premuto(self, _w, ev):
        if ev.button != 1:
            self.fine(None) if ev.button == 3 else None
            return True
        if self.modo == "rettangolo":
            self.inizio = (int(ev.x), int(ev.y))
            self.sel = None
        return True

    def mosso(self, _w, ev):
        if self.modo == "rettangolo" and self.inizio:
            x0, y0 = self.inizio
            x1, y1 = int(ev.x), int(ev.y)
            self.sel = (min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
            self.area.queue_draw()
        elif self.modo == "finestra":
            z = self.finestra_in(ev.x, ev.y)
            if z != self.sopra:
                self.sopra = z
                self.area.queue_draw()
        return True

    def rilasciato(self, _w, ev):
        if ev.button != 1:
            return True
        if self.modo == "rettangolo":
            if self.sel and self.sel[2] > 4 and self.sel[3] > 4:
                self.fine(self.sel)
            else:
                self.inizio = self.sel = None
                self.area.queue_draw()
        elif self.modo == "finestra":
            self.fine(self.finestra_in(ev.x, ev.y))
        return True

    def tasto(self, _w, ev):
        k = Gdk.keyval_name(ev.keyval)
        if k == "Escape":
            self.fine(None)
        elif k in ("Return", "KP_Enter") and self.sel:
            self.fine(self.sel)
        elif k in ("1", "2", "3"):
            self.pulsanti[("rettangolo", "finestra", "schermo")[int(k) - 1]].set_active(True)
        return True

    def fine(self, zona):
        Gdk.Display.get_default().get_default_seat().ungrab()
        self.hide()
        GLib.timeout_add(60, lambda: self.fatto(zona) and False)   # che l'oscuramento sparisca prima dello scatto


class Anteprima(Gtk.Window):
    """in basso a destra, come macOS: la miniatura, "Copiato negli appunti", Apri e Mostra nella cartella"""

    def __init__(self, percorso, pb, chiusa):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.percorso, self.chiusa, self.sopra = percorso, chiusa, False
        trasparente(self)
        scheda = C.classe(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(8)), "cattura-anteprima")
        for m in ("top", "bottom", "start", "end"):
            getattr(scheda, f"set_margin_{m}")(px(14))
        lato = px(260)
        sc = min(lato / pb.get_width(), px(160) / pb.get_height(), 1.0)
        mini = pb.scale_simple(max(1, int(pb.get_width() * sc)), max(1, int(pb.get_height() * sc)),
                               GdkPixbuf.InterpType.BILINEAR)
        ev = Gtk.EventBox()
        ev.add(C.classe(Gtk.Image.new_from_pixbuf(mini), "immagine"))
        ev.set_tooltip_text(T("Apri in Foto", "Open in Photos"))
        ev.connect("button-release-event", lambda *_: (self.apri(), True)[1])
        scheda.pack_start(ev, False, False, 0)
        testa = Gtk.Box(spacing=px(6))
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(1))
        v.pack_start(C.etichetta(T("Screenshot salvato", "Screenshot saved"), "titolo"), False, False, 0)
        v.pack_start(C.etichetta(T("Copiato anche negli appunti", "Also copied to the clipboard"), "sotto"), False, False, 0)
        testa.pack_start(v, True, True, 0)
        x = C.classe(Gtk.Button(), "chiudi")
        x.add(C.immagine_icona("window-close-symbolic", 12))
        x.connect("clicked", lambda *_: self.chiudi())
        x.set_valign(Gtk.Align.START)
        testa.pack_end(x, False, False, 0)
        scheda.pack_start(testa, False, False, 0)
        pulsanti = Gtk.Box(spacing=px(6))
        for testo, f in ((T("Apri", "Open"), self.apri), (T("Mostra nella cartella", "Show in folder"), self.cartella)):
            b = Gtk.Button(label=testo)
            b.connect("clicked", lambda _b, f=f: f())
            pulsanti.pack_start(b, False, False, 0)
        scheda.pack_start(pulsanti, False, False, 0)
        fuori = Gtk.EventBox()
        fuori.add(scheda)
        fuori.connect("enter-notify-event", lambda *_: setattr(self, "sopra", True))
        fuori.connect("leave-notify-event", lambda *_: setattr(self, "sopra", False))
        self.add(fuori)
        # (una finestra POPUP non ha gravità: la posizione si calcola dalla misura vera, appena la si conosce)
        self.connect("size-allocate", lambda _w, al: self.posiziona(al.width, al.height))
        _m, nat = fuori.get_preferred_size()
        self.posiziona(nat.width, nat.height)
        self.show_all()
        self.scade = time.monotonic() + 7
        GLib.timeout_add(500, self.controlla)

    def posiziona(self, w, h):
        d = Gdk.Display.get_default()
        a = (d.get_primary_monitor() or d.get_monitor(0)).get_workarea()
        pos = (a.x + a.width - w - px(6), a.y + a.height - h - px(6))
        if pos != getattr(self, "_pos", None):
            self._pos = pos
            self.move(*pos)

    def controlla(self):
        if self.sopra:   # il puntatore sopra: resta, come su macOS
            self.scade = time.monotonic() + 3
        if time.monotonic() >= self.scade:
            self.chiudi()
            return False
        return True

    def apri(self):
        foto = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media", "tod-foto.py")
        subprocess.Popen([sys.executable, foto, self.percorso], start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.chiudi()

    def cartella(self):
        C.mostra_nella_cartella(self.percorso)
        self.chiudi()

    def chiudi(self):
        if self.get_visible():
            self.hide()
            self.chiusa()


class Cattura:
    def __init__(self, modo):
        C.imposta_scala()
        stile()
        via_la_precedente()
        self.anteprima, self.appunti_miei, self.fine_entro = None, False, 0
        root = Gdk.get_default_root_window()
        self.foto = Gdk.pixbuf_get_from_window(root, 0, 0, root.get_width(), root.get_height())
        if self.foto is None:
            print("tod-cattura: schermo non leggibile", file=sys.stderr)
            sys.exit(1)
        W, H = self.foto.get_width(), self.foto.get_height()
        if modo == "schermo":
            GLib.idle_add(lambda: self.salva((0, 0, W, H)) and False)
        elif modo == "finestra":
            r = finestra_attiva()
            GLib.idle_add(lambda: self.salva(dentro(r, W, H) if r else (0, 0, W, H)) and False)
        else:
            Sovrapposta(self.foto, finestre(), self.salva)

    def salva(self, zona):
        if not zona:
            Gtk.main_quit()
            return
        x, y, w, h = zona
        img = self.foto.new_subpixbuf(x, y, w, h).copy()
        cartella = cartella_screenshot()
        os.makedirs(cartella, exist_ok=True)
        percorso = os.path.join(cartella, nome_file())
        img.savev(percorso, "png", [], [])
        print(percorso)
        self.anteprima = Anteprima(percorso, img, lambda: None)
        # gli appunti dopo l'anteprima: store() aspetta il gestore degli appunti, e l'anteprima arrivava in ritardo
        GLib.timeout_add(150, lambda: self.appunti(img) and False)

    def appunti(self, img):
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        cb.set_image(img)
        cb.store()
        # gli appunti restano finché c'è questo processo: si esce quando un'altra app li prende, o dopo 10 minuti
        GLib.timeout_add(800, lambda: cb.connect("owner-change", lambda *_: self.altri_appunti()) and False)
        self.fine_entro = time.monotonic() + 600
        GLib.timeout_add_seconds(5, self.basta)
        self.appunti_miei = True

    def altri_appunti(self):
        self.appunti_miei = False
        if self.anteprima is None or not self.anteprima.get_visible():
            Gtk.main_quit()

    def basta(self):
        if (not self.appunti_miei or time.monotonic() > self.fine_entro) and \
                (self.anteprima is None or not self.anteprima.get_visible()):
            Gtk.main_quit()
            return False
        return True


CATTURA = [None]


def main(argv):
    import signal
    # Android tiene SIGUSR1 bloccato nei processi avviati da Termux: va sbloccato (vedi tod-start.py)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGUSR1})
    nascondi = lambda: (CATTURA[0] and CATTURA[0].anteprima and CATTURA[0].anteprima.chiudi(), True)[1]   # noqa: E731
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGUSR1, nascondi)
    modo = "schermo" if "--schermo" in argv else "finestra" if "--finestra" in argv else "rettangolo"
    if "--ritardo" in argv:   # per le prove: lo schermo come sarà fra poco
        time.sleep(float(argv[argv.index("--ritardo") + 1]))
    CATTURA[0] = Cattura(modo)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
