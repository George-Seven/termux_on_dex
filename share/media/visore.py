#!/usr/bin/env python3
"""Il visore delle foto: in comune tra Foto e l'Anteprima (barra spaziatrice in Esplora file e sulla scrivania).

carica(): la foto (o l'animazione) già girata secondo l'EXIF, ridotta se serve. Visore: la foto adattata alla
finestra, zoom sul puntatore, trascinamento, rotazione, dissolvenza delle presentazioni. Chi lo usa ("fin") dà
colori (comune.colori()) e riceve foto_aperta(), zoom_cambiato(), mouse_mosso(). Gira in Termux (GTK 3).
"""
import math
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comune as C  # noqa: E402
from comune import Gdk, GdkPixbuf, GLib, Gtk, px, T  # noqa: E402

L = C.L
try:
    C.gi.require_version("GExiv2", "0.16")
    from gi.repository import GExiv2
except (ValueError, ImportError):
    GExiv2 = None

MAX_LATO = 8192      # una foto da 200 MP aperta per intero sarebbero 600 MB: al più 8192 px di lato


# --- caricare le foto ------------------------------------------------------------------------------------------
def dati_file(percorso):
    """(formato, larghezza, altezza) senza leggere tutta l'immagine; le misure già girate secondo l'EXIF."""
    try:
        fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(percorso)
    except (GLib.Error, TypeError):
        return None, 0, 0
    if fmt is None:
        return None, 0, 0
    nome = fmt.get_name()
    if nome == "jpeg" and GExiv2:
        try:
            m = GExiv2.Metadata.new()
            m.open_path(percorso)
            if int(m.get_orientation()) in (5, 6, 7, 8):
                w, h = h, w
        except GLib.Error:
            pass
    return nome, w, h


def carica(percorso, lato):
    """(pixbuf o animazione, larghezza vera, altezza vera): ridotta a `lato` px (0 = intera, al più MAX_LATO)."""
    fmt, w, h = dati_file(percorso)
    if fmt in ("gif", "webp", "ani"):
        try:
            an = GdkPixbuf.PixbufAnimation.new_from_file(percorso)
            if not an.is_static_image():
                return an, an.get_width(), an.get_height()
        except GLib.Error:
            pass
    tetto = lato or MAX_LATO
    if w and h and max(w, h) > tetto:
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(percorso, tetto, tetto, True)
    else:
        pb = GdkPixbuf.Pixbuf.new_from_file(percorso)
    pb = pb.apply_embedded_orientation() or pb
    if not w or not h:
        w, h = pb.get_width(), pb.get_height()
    elif (pb.get_width() > pb.get_height()) != (w > h) and w != h:
        w, h = h, w
    return pb, w, h

class Visore(Gtk.DrawingArea):
    """la foto: adattata alla finestra (mai ingrandita oltre il 100%), zoom sul puntatore, trascinamento."""

    def __init__(self, finestra):
        super().__init__(can_focus=True)
        self.fin = finestra
        self.add_events(Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK | Gdk.EventMask.BUTTON_PRESS_MASK |
                        Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("draw", self.disegna)
        self.connect("scroll-event", self.rotella)
        self.connect("button-press-event", self.premi)
        self.connect("button-release-event", self.rilascia)
        self.connect("motion-notify-event", self.moto)
        self.connect("size-allocate", lambda *_: self.limita())
        self.percorso = None
        self.pb = None            # Pixbuf mostrato (forse ridotto)
        self.sup = None           # la sua superficie cairo (convertita una volta sola)
        self.anim = None          # iteratore di un'animazione
        self.vero = (0, 0)        # misure vere della foto
        self.zoom = None          # None = adatta; se no scala sui pixel veri
        self.ox = self.oy = 0.0
        self.giro = 0             # rotazione in più (gradi, senso orario)
        self.errore = None
        self.trascina = None
        self.cache = {}           # percorso → (pb, w, h): la precedente e la successiva, già pronte
        self.ordine_cache = []
        self.gen = 0
        self.dissolvi = None      # (superficie vecchia, inizio) nelle presentazioni
        self.intera_chiesta = None

    # --- caricamento ---
    def lato_schermo(self):
        mon = self.get_display().get_monitor_at_window(self.get_window()) if self.get_window() else None
        g = mon.get_geometry() if mon else None
        return max(1920, (max(g.width, g.height) if g else 1920))

    def apri(self, percorso, dissolvi=False):
        self.gen += 1
        gen = self.gen
        vecchia = self.sup if dissolvi else None
        self.percorso, self.errore = percorso, None
        self.zoom, self.ox, self.oy, self.giro = None, 0.0, 0.0, 0
        self.giro_salvato = 0     # quanta rotazione è già scritta nel file
        self.intera_chiesta = None
        self.ferma_anim()
        if percorso in self.cache:
            self.metti(*self.cache[percorso], vecchia=vecchia)
            return
        lato = self.lato_schermo()

        def lavoro():
            try:
                r = carica(percorso, lato)
                err = None
            except (GLib.Error, OSError) as e:
                r, err = None, getattr(e, "message", str(e))
            GLib.idle_add(fatto, r, err)

        def fatto(r, err):
            if gen != self.gen:
                return False
            if r is None:
                self.pb, self.sup, self.errore = None, None, err or T("Impossibile aprire il file", "Can't open the file")
                self.queue_draw()
                self.fin.foto_aperta(self)
            else:
                self.ricorda(percorso, r)
                self.metti(*r, vecchia=vecchia)
            return False
        threading.Thread(target=lavoro, daemon=True).start()

    def ricorda(self, percorso, r):
        self.cache[percorso] = r
        if percorso in self.ordine_cache:
            self.ordine_cache.remove(percorso)
        self.ordine_cache.append(percorso)
        while len(self.ordine_cache) > 4:
            self.cache.pop(self.ordine_cache.pop(0), None)

    def prepara(self, percorso):
        """la foto che probabilmente viene dopo, in sottofondo (frecce: subito)."""
        if not percorso or percorso in self.cache or L.tipo_di(percorso) != "immagine":
            return
        lato = self.lato_schermo()

        def lavoro():
            try:
                r = carica(percorso, lato)
            except (GLib.Error, OSError):
                return
            GLib.idle_add(lambda: self.ricorda(percorso, r) and False)
        threading.Thread(target=lavoro, daemon=True).start()

    def metti(self, img, w, h, vecchia=None):
        self.vero = (w, h)
        if isinstance(img, GdkPixbuf.PixbufAnimation):
            self.anim = img.get_iter(None)
            self.pb = self.anim.get_pixbuf()
            self.sup = Gdk.cairo_surface_create_from_pixbuf(self.pb, 1, None)
            self.passo_anim()
        else:
            self.pb = img
            self.sup = Gdk.cairo_surface_create_from_pixbuf(img, 1, None)
        if vecchia is not None:
            self.dissolvi = (vecchia, GLib.get_monotonic_time())
            GLib.timeout_add(16, self.passo_dissolvi)
        self.queue_draw()
        self.fin.foto_aperta(self)

    def passo_dissolvi(self):
        if self.dissolvi is None:
            return False
        self.queue_draw()
        if GLib.get_monotonic_time() - self.dissolvi[1] > 400000:
            self.dissolvi = None
            return False
        return True

    def ferma_anim(self):
        if getattr(self, "_t_anim", None):
            GLib.source_remove(self._t_anim)
        self._t_anim = None
        self.anim = None

    def passo_anim(self):
        self._t_anim = None
        if self.anim is None:
            return False
        self.anim.advance(None)
        self.pb = self.anim.get_pixbuf()
        self.sup = Gdk.cairo_surface_create_from_pixbuf(self.pb, 1, None)
        self.queue_draw()
        d = self.anim.get_delay_time()
        if d >= 0:
            self._t_anim = GLib.timeout_add(max(20, d), self.passo_anim)
        return False

    def intera(self):
        """con lo zoom oltre i pixel caricati: la foto intera (al più MAX_LATO), in sottofondo."""
        if self.pb is None or self.anim is not None or self.intera_chiesta == self.percorso:
            return
        if self.pb.get_width() >= min(self.vero[0], MAX_LATO) - 1:
            return
        self.intera_chiesta = p = self.percorso
        gen = self.gen

        def lavoro():
            try:
                r = carica(p, 0)
            except (GLib.Error, OSError):
                return
            GLib.idle_add(fatto, r)

        def fatto(r):
            if gen == self.gen and isinstance(r[0], GdkPixbuf.Pixbuf):
                self.pb = r[0]
                self.sup = Gdk.cairo_surface_create_from_pixbuf(self.pb, 1, None)
                self.queue_draw()
            return False
        threading.Thread(target=lavoro, daemon=True).start()

    # --- misure ---
    def misure_girate(self):
        w, h = self.vero
        return (h, w) if self.giro % 180 else (w, h)

    def adatta(self):
        al = self.get_allocation()
        w, h = self.misure_girate()
        if not w or not h:
            return 1.0
        return min(al.width / w, al.height / h, 1.0)

    def scala(self):
        return self.zoom if self.zoom is not None else self.adatta()

    def percento(self):
        return int(round(self.scala() * 100))

    def limita(self):
        al = self.get_allocation()
        w, h = self.misure_girate()
        s = self.scala()
        mx = max(0.0, (w * s - al.width) / 2)
        my = max(0.0, (h * s - al.height) / 2)
        self.ox = max(-mx, min(mx, self.ox))
        self.oy = max(-my, min(my, self.oy))

    def zooma(self, nuova, x=None, y=None):
        al = self.get_allocation()
        vecchia = self.scala()
        nuova = max(min(self.adatta(), 0.05), min(16.0, nuova))
        if x is None:
            x, y = al.width / 2, al.height / 2
        dx, dy = x - al.width / 2, y - al.height / 2
        f = nuova / vecchia
        self.ox = (self.ox - dx) * f + dx
        self.oy = (self.oy - dy) * f + dy
        self.zoom = None if abs(nuova - self.adatta()) < 1e-3 else nuova
        self.limita()
        if self.pb is not None and nuova * self.vero[0] > self.pb.get_width() * 1.05:
            self.intera()
        self.queue_draw()
        self.fin.zoom_cambiato()

    def adatta_finestra(self):
        self.zoom, self.ox, self.oy = None, 0.0, 0.0
        self.queue_draw()
        self.fin.zoom_cambiato()

    def ruota(self):
        self.giro = (self.giro + 90) % 360
        self.zoom, self.ox, self.oy = None, 0.0, 0.0
        self.queue_draw()
        self.fin.zoom_cambiato()

    # --- disegno ---
    def disegna(self, w, cr):
        import cairo
        c = self.fin.colori
        al = w.get_allocation()
        cr.set_source_rgb(*C.rgb(c["VISTA"]))
        cr.paint()
        if self.errore:
            ic = C.pixbuf_icona("image-missing-symbolic", px(48), c["TENUE"])
            lay = w.create_pango_layout(T("Impossibile aprire questo file", "We can't open this file"))
            tw, th = lay.get_pixel_size()
            if ic:
                Gdk.cairo_set_source_pixbuf(cr, ic, (al.width - ic.get_width()) / 2, al.height / 2 - px(60))
                cr.paint()
            cr.set_source_rgb(*C.rgb(c["TESTO2"]))
            cr.move_to((al.width - tw) / 2, al.height / 2)
            from gi.repository import PangoCairo
            PangoCairo.show_layout(cr, lay)
            return False
        if self.sup is None:
            return False
        if self.dissolvi is not None:
            t = min(1.0, (GLib.get_monotonic_time() - self.dissolvi[1]) / 400000)
            vecchia = self.dissolvi[0]
            cr.save()
            self._disegna_sup(cr, al, vecchia, None, adatta=True)
            cr.restore()
            cr.push_group()
            self._disegna_sup(cr, al, self.sup, self.pb)
            cr.pop_group_to_source()
            cr.paint_with_alpha(t)
            return False
        self._disegna_sup(cr, al, self.sup, self.pb, filtro=cairo.FILTER_GOOD)
        return False

    def _disegna_sup(self, cr, al, sup, pb, adatta=False, filtro=None):
        import cairo
        if pb is None:   # la vecchia di una dissolvenza: adattata
            sw, sh = sup.get_width(), sup.get_height()
            s = min(al.width / sw, al.height / sh, 1.0)
            cr.translate(al.width / 2, al.height / 2)
            cr.scale(s, s)
            cr.set_source_surface(sup, -sw / 2, -sh / 2)
            cr.paint()
            return
        s = self.scala()
        vw, vh = self.vero
        cr.translate(al.width / 2 + self.ox, al.height / 2 + self.oy)
        cr.rotate(math.radians(self.giro))
        k = s * vw / pb.get_width()
        cr.scale(k, k)
        cr.set_source_surface(sup, -pb.get_width() / 2, -pb.get_height() / 2)
        pat = cr.get_source()
        pat.set_filter(cairo.FILTER_NEAREST if k > 3 else (filtro or cairo.FILTER_GOOD))
        cr.paint()

    # --- mouse ---
    def rotella(self, w, ev):
        if self.sup is None:
            return False
        if ev.direction == Gdk.ScrollDirection.SMOOTH:
            ok, dx, dy = ev.get_scroll_deltas()
            f = 1.12 ** (-dy)
        elif ev.direction == Gdk.ScrollDirection.UP:
            f = 1.15
        elif ev.direction == Gdk.ScrollDirection.DOWN:
            f = 1 / 1.15
        else:
            return False
        self.zooma(self.scala() * f, ev.x, ev.y)
        return True

    def premi(self, w, ev):
        self.grab_focus()
        if ev.type == Gdk.EventType._2BUTTON_PRESS and ev.button == 1:
            if self.zoom is None and self.adatta() < 0.999:
                self.zooma(1.0, ev.x, ev.y)
            else:
                self.adatta_finestra()
            return True
        if ev.button == 1 and self.zoom is not None:
            self.trascina = (ev.x, ev.y, self.ox, self.oy)
            self.get_window().set_cursor(Gdk.Cursor.new_from_name(self.get_display(), "grabbing"))
        return False

    def rilascia(self, w, ev):
        if self.trascina:
            self.trascina = None
            self.get_window().set_cursor(Gdk.Cursor.new_from_name(self.get_display(), "grab"))
        return False

    def moto(self, w, ev):
        self.fin.mouse_mosso()
        if self.trascina:
            x0, y0, ox, oy = self.trascina
            self.ox, self.oy = ox + ev.x - x0, oy + ev.y - y0
            self.limita()
            self.queue_draw()
        elif self.get_window():
            self.get_window().set_cursor(Gdk.Cursor.new_from_name(self.get_display(), "grab")
                                         if self.zoom is not None else None)
        return False
