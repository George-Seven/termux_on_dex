#!/usr/bin/env python3
"""Anteprima di Termux On DeX: la barra spaziatrice su un file in Esplora file o sulla scrivania, come Quick Look
(Visualizzazione rapida) di macOS.

  tod-anteprima.py file...               una finestra senza cornice, al centro dello schermo, con il contenuto
  tod-anteprima.py --indice N file...    tutti i file della vista di Esplora file, nel suo ordine: si mostra l'N-esimo
                                         (da 0) e le frecce sfogliano gli altri

Foto (anche GIF animate; zoom con la rotella), video e musica (partono da soli), PDF (tutte le pagine), testo e
codice (con i colori), cartelle e ogni altro file (icona, tipo, dimensione, data). Spazio o Esc chiude; ← → (o ↑ ↓)
passano agli altri file scelti, o con un file solo a quelli della sua cartella, nell'ordine di Esplora file; Invio
apre con l'app predefinita ("Apri con Foto", in alto a destra); F: schermo intero. Un secondo avvio con gli stessi
file chiude l'anteprima (di nuovo la barra spaziatrice), con altri file la cambia.

La lancia tod-gettext.so (Thunar, xfdesktop) con `tod-anteprima` (share/bin). Usa il visore di Foto (visore.py) e il
motore del Lettore multimediale (motore.py): stesso video con la GPU, stessi sottotitoli. Gira in Termux (GTK 3).
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comune as C  # noqa: E402
from comune import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, px, T  # noqa: E402

L = C.L
C.gi.require_version("GdkX11", "3.0")
from gi.repository import GdkX11  # noqa: E402,F401  (get_xid della finestra del video)

GLib.set_prgname("tod-anteprima")
GLib.set_application_name(T("Anteprima", "Quick Look"))

MARGINE = 26     # intorno alla finestra, per l'ombra (solo con il compositore)
RAGGIO = 12
ATTRIBUTI = "standard::content-type,standard::icon,standard::size,standard::display-name,time::modified"

CSS = """
window.w11.ap-fuori { background-color: transparent; }
.w11 .ap-cornice {
  background-color: FONDO; border: 1px solid LINEA; border-radius: 12px;
  box-shadow: 0 12px 34px OMBRA, 0 1px 4px OMBRA;
}
.w11 .ap-cornice.piatta { border-radius: 0; border: none; box-shadow: none; }
.w11 .ap-testa { padding: 6px 8px; }
.w11 .ap-titolo { font-weight: 600; }
.w11 .ap-conto { color: TENUE; font-size: 8.5pt; font-feature-settings: "tnum"; }
.w11 button.ap-apri { border-radius: 999px; padding: 3px 14px; font-weight: 600; font-size: 9pt; }
.w11 .ap-nome { font-size: 15pt; font-weight: 600; }
.w11 .ap-dati { color: TESTO2; }
.w11 .ap-controlli { padding: 6px 12px; border-top: 1px solid LINEA; }
.w11 .ap-tempo { font-size: 8.5pt; color: TESTO2; font-feature-settings: "tnum"; }
.w11 .ap-pagine { background-color: VISTA; }
.w11 .ap-codice, .w11 .ap-codice text { font-family: monospace; font-size: 9.5pt; }
"""


def info_di(percorso):
    try:
        return Gio.File.new_for_path(percorso).query_info(ATTRIBUTI, Gio.FileQueryInfoFlags.NONE, None)
    except GLib.Error:
        return None


def tipo_mime(info):
    return (info.get_content_type() or "") if info else ""


def e_testo(tipo):
    return bool(tipo) and (Gio.content_type_is_a(tipo, "text/plain") or tipo.startswith("text/"))


def mtime(info, percorso):
    if info:
        dt = info.get_modification_date_time()
        if dt:
            return dt.to_unix()
    try:
        return os.stat(percorso).st_mtime
    except OSError:
        return 0


def icona_grande(info, lato):
    """l'icona del tipo di file, grande (quella del tema DeX OS)."""
    tema = Gtk.IconTheme.get_default()
    gi = info.get_icon() if info else None
    if gi is not None:
        try:
            ii = tema.lookup_by_gicon(gi, lato, Gtk.IconLookupFlags.FORCE_SIZE)
            if ii:
                return ii.load_icon()
        except GLib.Error:
            pass
    return C.pixbuf_icona("text-x-generic", lato)


# --- i contenuti: ognuno ha widget, misura(larghezza, altezza massime) → (l, a), chiudi() ------------------------
class Immagine:
    """la foto con il visore di Foto (adattata, zoom con la rotella, trascinamento, GIF animate)."""

    def __init__(self, fin, percorso, info):
        import visore
        self.visore = visore
        self.v = visore.Visore(fin)
        self.v.set_can_focus(False)
        self.widget = self.v
        self.cambia(percorso)

    def cambia(self, percorso):
        _, self.w, self.h = self.visore.dati_file(percorso)
        self.v.apri(percorso)

    def prepara(self, percorso):
        if percorso and L.tipo_di(percorso) == "immagine":
            self.v.prepara(percorso)

    def misura(self, mw, mh):
        w, h = self.w or 800, self.h or 600
        s = min(mw / w, mh / h, 1.0)    # mai più grande del vero (come Quick Look)
        return max(px(360), int(w * s)), max(px(240), int(h * s))

    def chiudi(self):
        self.v.ferma_anim()


class Lettore:
    """video (con la GPU, come il Lettore multimediale) o musica (copertina, titolo, artista): partono da soli."""

    def __init__(self, fin, percorso, info, video):
        import motore as M
        self.M, self.fin, self.p, self.video = M, fin, percorso, video
        self.Gst = M.Gst
        self.durata = 0.0
        self.trascina = False
        self.partito = False
        self.area_ok = False
        self.vw, self.vh = 16.0, 9.0
        self.motore = M.Motore(self, video)
        imp = L.leggi_json(os.path.join(L.config_dir(), "lettore.json"), {})   # il volume del Lettore
        self.motore.metti_volume(imp.get("volume", 70), imp.get("muto", False))
        scopri = M.GstPbutils.Discoverer.new(3 * M.Gst.SECOND)
        corpo = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        if video:
            self.misure_video(scopri)
            self.area = C.classe(Gtk.DrawingArea(), "nero")
            if fin.vetro:   # la finestra è trasparente (angoli tondi): il video no
                self.area.set_visual(fin.get_screen().get_system_visual())
            self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.STRUCTURE_MASK)
            self.area.connect("realize", self.area_pronta)
            self.area.connect("draw", self.disegna)
            self.area.connect("size-allocate", self.area_misura)
            self.area.connect("map-event", lambda *_: self.mappata(area=True))
            self.area.connect("button-press-event", self.clic)
            corpo.pack_start(self.area, True, True, 0)
            corpo.pack_start(self.controlli(), False, False, 0)
        else:
            corpo.pack_start(self.scheda_brano(scopri), True, True, 0)
        self.widget = corpo
        self.timer = GLib.timeout_add(250, self.passo)
        if not video:
            self.avvia()

    # video: misure (per la finestra) e uscita
    def misure_video(self, scopri):
        try:
            inf = scopri.discover_uri(C.uri_di(self.p))
        except GLib.Error:
            return
        self.durata = (inf.get_duration() or 0) / self.Gst.SECOND
        vs = inf.get_video_streams()
        if vs and vs[0].get_width() and vs[0].get_height():
            par = vs[0].get_par_num() / max(1, vs[0].get_par_denom())
            self.vw, self.vh = vs[0].get_width() * (par or 1), float(vs[0].get_height())

    def area_pronta(self, w):
        if not self.motore.cairo:
            win = w.get_window()
            win.ensure_native()
            self.motore.xid = win.get_xid()
            w.set_double_buffered(False)

    def mappata(self, area=False):
        """glimagesink parte solo con la finestra e l'area davvero a schermo (MapNotify veri), sennò zink cade."""
        self.area_ok = self.area_ok or area
        if not self.partito and self.fin.mappata and self.area_ok:
            GLib.timeout_add(120, lambda: self.avvia() and False)
        return False

    def avvia(self):
        if self.partito or self.timer is None:
            return False
        self.partito = True
        self.motore.apri(self.M.uri_di(self.p), (self.M.sottotitoli_di(self.p) or [None])[0] if self.video else None)
        if self.video and self.motore.cairo:
            al = self.area.get_allocation()
            self.motore.misura_uscita(al.width, al.height)
        self.motore.suona()
        return False

    def uscita_cambiata(self):
        self.area.set_double_buffered(True)
        al = self.area.get_allocation()
        self.motore.misura_uscita(al.width, al.height)
        self.area.queue_draw()

    def area_misura(self, w, al):
        if self.motore.cairo:
            self.motore.misura_uscita(al.width, al.height)
        elif self.motore.sink is not None and self.motore.uri and not self.motore.suona_ora():
            GLib.idle_add(lambda: self.motore.sink.expose() and False)

    def ridisegna_video(self):
        self.area.queue_draw()
        return False

    def disegna(self, w, cr):
        import cairo
        al = w.get_allocation()
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        if not self.motore.cairo:
            if self.motore.sink is not None and self.motore.uri:
                self.motore.sink.expose()
            return False
        f = self.motore.fotogramma
        if f:
            dati, fw, fh = f
            try:
                sup = cairo.ImageSurface.create_for_data(bytearray(dati), cairo.FORMAT_RGB24, fw, fh, fw * 4)
            except (ValueError, TypeError):
                return False
            s = min(al.width / fw, al.height / fh)
            cr.translate((al.width - fw * s) / 2, (al.height - fh * s) / 2)
            if abs(s - 1) > 0.01:
                cr.scale(s, s)
            cr.set_source_surface(sup, 0, 0)
            cr.paint()
        return False

    def clic(self, w, ev):
        if ev.button == 1 and ev.type == Gdk.EventType.BUTTON_PRESS:
            self.play_pausa()
        return True

    # musica: copertina e dati
    def scheda_brano(self, scopri):
        try:
            st = os.stat(self.p)
            b = self.M.leggi_brano(self.p, st.st_mtime, st.st_size, scopri)
        except OSError:
            b = L.brano_vuoto(self.p)
        self.durata = b.get("durata") or 0.0
        cop = b.get("copertina") or self.M.copertina_cartella(os.path.dirname(self.p), {})
        self.copertina = None
        if cop:
            try:
                self.copertina = GdkPixbuf.Pixbuf.new_from_file_at_scale(cop, px(168), px(168), True)
            except GLib.Error:
                pass
        box = Gtk.Box(spacing=px(20))
        box.set_border_width(px(20))
        da = Gtk.DrawingArea()
        da.set_size_request(px(168), px(168))
        da.set_valign(Gtk.Align.CENTER)
        da.connect("draw", self.disegna_copertina)
        box.pack_start(da, False, False, 0)
        dx = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(4))
        dx.set_valign(Gtk.Align.CENTER)
        dx.pack_start(C.etichetta(b.get("titolo") or os.path.basename(self.p), "ap-nome", righe=2), False, False, 0)
        for testo in (L.artista_di(b) if b.get("artista") else "", b.get("album") or ""):
            if testo:
                dx.pack_start(C.etichetta(testo, "ap-dati"), False, False, 0)
        riga = self.controlli(scheda=True)
        riga.set_margin_top(px(14))
        dx.pack_start(riga, False, False, 0)
        box.pack_start(dx, True, True, 0)
        return box

    def disegna_copertina(self, w, cr):
        lato = w.get_allocated_width()
        C.rettangolo_tondo(cr, 0, 0, lato, lato, px(8))
        cr.clip()
        if self.copertina:
            C.pixbuf_coprente(cr, self.copertina, 0, 0, lato, lato)
            return False
        c = self.fin.colori   # niente copertina: la nota sull'accento, come il Lettore musicale
        cr.set_source_rgb(*C.rgb(c["ACCENTO"]))
        cr.paint()
        ic = C.pixbuf_icona("audio-x-generic-symbolic", lato // 3, c["SUACCENTO"])
        if ic:
            Gdk.cairo_set_source_pixbuf(cr, ic, (lato - ic.get_width()) / 2, (lato - ic.get_height()) / 2)
            cr.paint()
        return False

    # comandi
    def controlli(self, scheda=False):
        riga = Gtk.Box(spacing=px(8))
        if not scheda:
            C.classe(riga, "ap-controlli")
        self.b_play = C.bottone("media-playback-pause-symbolic", None, T("Pausa", "Pause"), self.play_pausa,
                                cls="play" if scheda else "icona", lato=16 if scheda else 18)
        self.b_play.set_valign(Gtk.Align.CENTER)
        riga.pack_start(self.b_play, False, False, 0)
        self.t_ora = C.etichetta("0:00", "ap-tempo", xalign=1.0, ellissi=False)
        self.cursore = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, max(1.0, self.durata), 1)
        self.cursore.set_draw_value(False)
        self.cursore.set_can_focus(False)
        self.cursore.connect("change-value", self.cerca)
        self.cursore.connect("button-press-event", lambda *_: setattr(self, "trascina", True) or False)
        self.cursore.connect("button-release-event", lambda *_: setattr(self, "trascina", False) or False)
        self.t_tot = C.etichetta(L.tempo(self.durata), "ap-tempo", xalign=0.0, ellissi=False)
        riga.pack_start(self.t_ora, False, False, 0)
        riga.pack_start(self.cursore, True, True, 0)
        riga.pack_start(self.t_tot, False, False, 0)
        return riga

    def play_pausa(self):
        if not self.partito:
            self.avvia()
        elif self.motore.suona_ora():
            self.motore.pausa()
        else:
            if self.motore.uri is None:
                self.partito = False
                self.avvia()
            self.motore.suona()

    def cerca(self, scala, tipo, valore):
        if self.motore.uri:
            self.motore.vai_a(max(0.0, min(valore, self.durata or valore)), preciso=False)
        self.t_ora.set_text(L.tempo(valore))
        return False

    def passo(self):
        if self.motore.uri and not self.trascina:
            p = self.motore.posizione()
            self.cursore.set_value(p)
            self.t_ora.set_text(L.tempo(p))
        if self.video and not self.motore.cairo and not getattr(self.motore, "sink_ok", False) and self.motore.suona_ora():
            self.controlla_gl()
        return True

    def controlla_gl(self):
        """come il Lettore: se glimagesink perde più di un terzo dei fotogrammi (niente GPU vera), si passa a cairo."""
        s = self.motore.sink
        el = s.get_property("sink") if s is not None and s.find_property("sink") else s
        if el is None or not el.find_property("stats"):
            return
        st = el.get_property("stats")
        r, d = st.get_value("rendered"), st.get_value("dropped")
        if r + d < 150:
            return
        if d > (r + d) / 3:
            self.motore.passa_a_cairo()
        else:
            self.motore.sink_ok = True

    # dal motore
    def stato(self, st):
        suona = st == self.Gst.State.PLAYING
        C.metti_icona(self.b_play, "media-playback-pause-symbolic" if suona else "media-playback-start-symbolic",
                      16 if not self.video else 18)
        self.b_play.set_tooltip_text(T("Pausa", "Pause") if suona else T("Riproduci", "Play"))

    def fine(self):
        self.motore.pausa()
        self.motore.vai_a(0)

    def errore(self, msg):
        self.t_tot.set_text(T("Impossibile riprodurre", "Can't play"))
        self.t_tot.set_tooltip_text(msg)
        self.motore.ferma()

    def tag(self, tags):
        pass

    def cambio(self):
        pass

    def prossimo_uri(self):
        return None

    def durata_nota(self):
        d = self.motore.durata()
        if d > 0 and abs(d - self.durata) > 0.5:
            self.durata = d
            self.cursore.set_range(0, d)
            self.t_tot.set_text(L.tempo(d))

    def misura(self, mw, mh):
        if not self.video:
            return min(mw, px(600)), px(212)
        barra = px(46)
        s = min(mw / self.vw, (mh - barra) / self.vh, 1.0)
        w, h = self.vw * s, self.vh * s
        if w < px(480):    # i video piccoli un po' più grandi, come Quick Look
            w, h = px(480), h * px(480) / w
        return int(w), int(h) + barra

    def chiudi(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None
        self.motore.pb.set_state(self.Gst.State.NULL)
        self.motore.pb.get_bus().remove_signal_watch()


class Pdf:
    """tutte le pagine, una sotto l'altra, disegnate quando si vedono (Poppler)."""

    def __init__(self, fin, percorso, info):
        C.gi.require_version("Poppler", "0.18")
        from gi.repository import Poppler
        self.doc = Poppler.Document.new_from_file(C.uri_di(percorso), None)   # GLib.Error se protetto: scheda
        self.pagine = [self.doc.get_page(i) for i in range(min(self.doc.get_n_pages(), 500))]
        self.misure = [p.get_size() for p in self.pagine]
        self.cache, self.ordine, self.larga = {}, [], 0
        self.sw = C.classe(Gtk.ScrolledWindow(), "ap-pagine")
        self.sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(12))
        self.box.set_border_width(px(16))
        self.aree = []
        for i in range(len(self.pagine)):
            da = Gtk.DrawingArea()
            da.connect("draw", self.disegna, i)
            self.box.pack_start(da, False, False, 0)
            self.aree.append(da)
        self.sw.add(self.box)
        self.sw.connect("size-allocate", lambda w, al: GLib.idle_add(self.allarga, al.width))
        self.widget = self.sw

    def allarga(self, larga):
        w = larga - 2 * px(16) - px(8)
        if w < 50 or w == self.larga:
            return False
        self.larga = w
        for da, (pw, ph) in zip(self.aree, self.misure):
            da.set_size_request(w, int(w * ph / max(1.0, pw)))
        return False

    def disegna(self, da, cr, i):
        import cairo
        w, h = da.get_allocated_width(), da.get_allocated_height()
        if w < 50 or h < 50 or self.larga == 0:
            return False   # misure non ancora date (allarga)
        sup = self.cache.get(i)
        if sup is None or (sup.get_width(), sup.get_height()) != (w, h):
            sup = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
            c = cairo.Context(sup)
            c.set_source_rgb(1, 1, 1)
            c.paint()
            c.scale(w / max(1.0, self.misure[i][0]), h / max(1.0, self.misure[i][1]))
            self.pagine[i].render(c)
            self.cache[i] = sup
            self.ordine = [k for k in self.ordine if k != i] + [i]
            while len(self.ordine) > 12:
                self.cache.pop(self.ordine.pop(0), None)
        cr.set_source_surface(sup, 0, 0)
        cr.paint()
        cr.set_source_rgba(0, 0, 0, 0.12)
        cr.set_line_width(1)
        cr.rectangle(0.5, 0.5, w - 1, h - 1)
        cr.stroke()
        return False

    def misura(self, mw, mh):
        return min(mw, px(720)), mh

    def scorri(self, k):
        return scorri(self.sw, k)

    def chiudi(self):
        self.cache.clear()


class Testo:
    """testo e codice (i primi 512 KB), con i colori di GtkSourceView, a capo come in Quick Look."""

    def __init__(self, fin, percorso, info):
        with open(percorso, "rb") as f:
            dati = f.read(512 * 1024)
        if b"\0" in dati[:8192]:
            raise ValueError("binario")
        testo = dati.decode("utf-8", errors="replace")
        try:
            C.gi.require_version("GtkSource", "4")
            from gi.repository import GtkSource
            buf = GtkSource.Buffer()
            lingua = GtkSource.LanguageManager.get_default().guess_language(os.path.basename(percorso), tipo_mime(info) or None)
            if lingua:
                buf.set_language(lingua)
            schemi = GtkSource.StyleSchemeManager.get_default()
            for nome in (("Adwaita-dark", "oblivion", "cobalt") if fin.colori["scuro"] else ("Adwaita", "kate", "classic")):
                s = schemi.get_scheme(nome)
                if s:
                    buf.set_style_scheme(s)
                    break
            vista = GtkSource.View(buffer=buf)
            vista.set_show_line_numbers(bool(lingua))
        except (ValueError, ImportError):
            buf = Gtk.TextBuffer()
            vista = Gtk.TextView(buffer=buf)
        buf.set_text(testo)
        vista.set_editable(False)
        vista.set_cursor_visible(False)
        vista.set_can_focus(False)
        vista.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        for lato in ("left", "right", "top", "bottom"):
            getattr(vista, "set_%s_margin" % lato)(px(12))
        C.classe(vista, "ap-codice")
        self.sw = Gtk.ScrolledWindow()
        self.sw.add(vista)
        self.widget = self.sw

    def misura(self, mw, mh):
        return min(mw, px(700)), min(mh, px(560))

    def scorri(self, k):
        return scorri(self.sw, k)

    def chiudi(self):
        pass


class Scheda:
    """cartelle e ogni altro file: icona grande (o la miniatura di Esplora file), nome, tipo, dimensione, data."""

    def __init__(self, fin, percorso, info):
        self.p = percorso
        cartella = os.path.isdir(percorso)
        lato = px(128)
        pb = None
        if not cartella:
            pb, _ = C.leggi_miniatura(percorso, mtime(info, percorso))
            pb = C.riduci(pb, lato) if pb else None
        pb = pb or icona_grande(info, lato)
        box = Gtk.Box(spacing=px(22))
        box.set_border_width(px(24))
        im = Gtk.Image.new_from_pixbuf(pb) if pb else Gtk.Image()
        im.set_size_request(lato, lato)
        box.pack_start(im, False, False, 0)
        dx = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(4))
        dx.set_valign(Gtk.Align.CENTER)
        nome = (info.get_display_name() if info else None) or os.path.basename(percorso) or percorso
        dx.pack_start(C.etichetta(nome, "ap-nome", righe=3), False, False, 0)
        tipo = tipo_mime(info)
        descr = T("Cartella", "Folder") if cartella else (Gio.content_type_get_description(tipo) if tipo else "")
        if descr:
            dx.pack_start(C.etichetta(descr, "ap-dati"), False, False, 0)
        self.l_misura = C.etichetta("", "ap-dati")
        dx.pack_start(self.l_misura, False, False, 0)
        if cartella:
            threading.Thread(target=self.conta, daemon=True).start()
        elif info:
            self.l_misura.set_text(L.misura(info.get_size()))
        quando = mtime(info, percorso)
        if quando:
            dx.pack_start(C.etichetta(T("Modificato: %s", "Modified: %s") % L.data_lunga(quando), "ap-dati"),
                          False, False, 0)
        box.pack_start(dx, True, True, 0)
        self.widget = box

    def conta(self):
        try:
            n = sum(1 for e in os.scandir(self.p) if not e.name.startswith("."))
        except OSError:
            return
        GLib.idle_add(self.l_misura.set_text, L.elementi(n))

    def misura(self, mw, mh):
        return min(mw, px(580)), px(196)

    def chiudi(self):
        pass


def scorri(sw, k):
    """Pagina su/giù, Inizio/Fine nel testo e nei PDF (le frecce passano da un file all'altro)."""
    a = sw.get_vadjustment()
    passo = a.get_page_size() * 0.9
    v = {Gdk.KEY_Page_Up: a.get_value() - passo, Gdk.KEY_Page_Down: a.get_value() + passo,
         Gdk.KEY_Home: a.get_lower(), Gdk.KEY_End: a.get_upper()}.get(k)
    if v is None:
        return False
    a.set_value(max(a.get_lower(), min(v, a.get_upper() - a.get_page_size())))
    return True


# --- la finestra ---------------------------------------------------------------------------------------------
class Anteprima(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.termuxondex.Anteprima", flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.fin = None

    def do_command_line(self, cl):
        cwd = cl.get_cwd() or os.getcwd()
        args = cl.get_arguments()[1:]
        indice = None
        if len(args) > 2 and args[0] == "--indice" and args[1].isdigit():
            indice, args = int(args[1]), args[2:]
        percorsi = [os.path.normpath(os.path.join(cwd, GLib.filename_from_uri(a)[0] if a.startswith("file://") else a))
                    for a in args]
        scelto = percorsi[indice] if indice is not None and indice < len(percorsi) else None
        file = [p for p in percorsi if os.path.lexists(p)]
        indice = file.index(scelto) if scelto in file else None
        if self.fin and (file, indice) == self.fin.richiesti:
            self.fin.chiudi()     # di nuovo la barra spaziatrice sugli stessi file: si chiude
            return 0
        if not file:
            return 0 if self.fin else 1
        if not self.fin:
            self.fin = Finestra(self)
            self.fin.connect("destroy", lambda *_: setattr(self, "fin", None))
        self.fin.apri(file, indice)
        self.fin.present()
        return 0


class Finestra(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=T("Anteprima", "Quick Look"))
        C.imposta_scala()
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        C.classe(self, "w11 ap-fuori")
        scr = self.get_screen()
        vis = scr.get_rgba_visual()
        self.vetro = bool(vis) and scr.is_composited()   # angoli tondi e ombra solo con il compositore
        if self.vetro:
            self.set_visual(vis)
        self.colori = dict(C.colori(), VISTA=C.colori()["FONDO"])   # il visore dipinge il fondo della finestra
        self.stile = C.Stile(CSS, self.tema_cambiato)
        self.margine = px(MARGINE) if self.vetro else 0
        self.intero = False
        self.mappata = False
        self.elenco, self.indice, self.richiesti = [], 0, []
        self.gen = 0
        self.contenuto = None
        self.app_apri = None

        self.cornice = C.classe(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), "ap-cornice")
        if not self.vetro:
            C.classe(self.cornice, "piatta")
        self.metti_margini()
        self.add(self.cornice)
        dentro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        dentro.connect("draw", self.ritaglia)
        self.cornice.pack_start(dentro, True, True, 0)

        self.testa = C.classe(Gtk.Box(spacing=px(2)), "ap-testa")
        presa = Gtk.EventBox()
        presa.add(self.testa)
        presa.connect("button-press-event", self.premi_testa)
        self.testa.pack_start(C.bottone("window-close-symbolic", None, T("Chiudi (spazio)", "Close (Space)"),
                                        self.chiudi), False, False, 0)
        self.b_intero = C.bottone("view-fullscreen-symbolic", None, T("Schermo intero (F)", "Full screen (F)"),
                                  self.cambia_intero)
        self.testa.pack_start(self.b_intero, False, False, 0)
        centro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        centro.set_valign(Gtk.Align.CENTER)
        self.titolo = C.etichetta("", "ap-titolo", xalign=0.5)
        self.titolo.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.titolo.set_max_width_chars(56)
        self.conto = C.etichetta("", "ap-conto", xalign=0.5, ellissi=False)
        centro.pack_start(self.titolo, False, False, 0)
        centro.pack_start(self.conto, False, False, 0)
        self.testa.set_center_widget(centro)
        self.b_apri = C.bottone(None, "", None, self.apri_app, cls="ap-apri")
        self.b_apri.set_valign(Gtk.Align.CENTER)
        self.b_apri.set_margin_end(px(4))
        self.testa.pack_end(self.b_apri, False, False, 0)
        self.b_succ = C.bottone("go-next-symbolic", None, T("Successivo (→)", "Next (→)"), lambda: self.vai(1))
        self.b_prec = C.bottone("go-previous-symbolic", None, T("Precedente (←)", "Previous (←)"), lambda: self.vai(-1))
        self.testa.pack_end(self.b_succ, False, False, 0)
        self.testa.pack_end(self.b_prec, False, False, 0)
        dentro.pack_start(presa, False, False, 0)
        self.corpo = Gtk.Box()
        dentro.pack_start(self.corpo, True, True, 0)

        self.add_events(Gdk.EventMask.STRUCTURE_MASK)
        self.connect("key-press-event", self.tasto)
        self.connect("window-state-event", self.stato_finestra)
        self.connect("size-allocate", lambda *_: self.forma())
        self.connect("map-event", self.mappa)
        self.connect("unmap-event", lambda *_: setattr(self, "mappata", False) or False)
        self.cornice.show_all()   # visibili già prima della finestra: l'intestazione si misura giusta

    def tema_cambiato(self):
        self.colori = dict(C.colori(), VISTA=C.colori()["FONDO"])
        self.queue_draw()

    # aspetto
    def metti_margini(self):
        m = 0 if self.intero else self.margine
        for lato in ("top", "bottom", "start", "end"):
            getattr(self.cornice, "set_margin_" + lato)(m)

    def ritaglia(self, w, cr):
        """i figli dentro gli angoli tondi (GTK 3 non lo fa da sé)."""
        if self.vetro and not self.intero:
            C.rettangolo_tondo(cr, 0, 0, w.get_allocated_width(), w.get_allocated_height(), px(RAGGIO) - 1)
            cr.clip()
        return False

    def forma(self):
        """i clic sull'ombra passano a quello che c'è sotto."""
        gw = self.get_window()
        if not self.vetro or gw is None:
            return
        import cairo
        m = 0 if self.intero else self.margine
        w, h = self.get_allocated_width(), self.get_allocated_height()
        try:
            gw.input_shape_combine_region(cairo.Region(cairo.RectangleInt(m, m, max(1, w - 2 * m), max(1, h - 2 * m))), 0, 0)
        except (TypeError, AttributeError):
            pass

    def mappa(self, *_):
        self.mappata = True
        if isinstance(self.contenuto, Lettore):
            self.contenuto.mappata()
        return False

    def area_lavoro(self):
        d = Gdk.Display.get_default()
        mon = None
        try:
            _, x, y = d.get_default_seat().get_pointer().get_position()
            mon = d.get_monitor_at_point(x, y)
        except (AttributeError, TypeError):
            pass
        mon = mon or d.get_primary_monitor() or d.get_monitor(0)
        return mon.get_workarea()

    def dimensiona(self):
        if self.intero or self.contenuto is None:
            return
        wa = self.area_lavoro()
        testa = self.testa.get_preferred_height()[1]
        bordi = 2 * self.margine + 2
        cw, ch = self.contenuto.misura(int(wa.width * 0.78) - bordi, int(wa.height * 0.84) - testa - bordi)
        w, h = cw + bordi, ch + testa + bordi
        if os.environ.get("TOD_ANTEPRIMA_DEBUG"):
            mi = self.get_preferred_size()[0]
            print("anteprima: area %dx%d, testa %d, contenuto %dx%d, finestra %dx%d, minima %dx%d" % (
                wa.width, wa.height, testa, cw, ch, w, h, mi.width, mi.height), file=sys.stderr)
        if self.get_realized():
            self.resize(w, h)
        else:
            self.set_default_size(w, h)

    # i file
    def apri(self, percorsi, indice=None):
        self.richiesti = (list(percorsi), indice)
        self.elenco, self.indice = list(percorsi), indice or 0
        self.gen += 1
        self.mostra()
        if len(percorsi) == 1:   # un file solo (dalla scrivania): le frecce sfogliano la cartella, in ordine di nome
            g = self.gen

            def lavoro():
                lista, i = L.vicini(percorsi[0])
                GLib.idle_add(fatto, lista, i)

            def fatto(lista, i):
                if g == self.gen and len(self.elenco) == 1:
                    self.elenco, self.indice = lista, i
                    self.aggiorna_testa()
                    self.prepara_vicini()
                return False
            threading.Thread(target=lavoro, daemon=True).start()

    def mostra(self):
        p = self.elenco[self.indice]
        info = info_di(p)
        tipo = tipo_mime(info)
        g = L.genere(p, tipo, e_testo(tipo))
        if g == "immagine" and isinstance(self.contenuto, Immagine):
            self.contenuto.cambia(p)    # stessa finestra del visore: le foto vicine sono già pronte
        else:
            if self.contenuto is not None:
                self.contenuto.chiudi()
                self.corpo.remove(self.contenuto.widget)
            self.contenuto = self.crea(g, p, info)
            self.corpo.pack_start(self.contenuto.widget, True, True, 0)
            self.contenuto.widget.show_all()
        self.metti_apri(p, info)
        self.aggiorna_testa()
        self.dimensiona()
        self.prepara_vicini()
        if not self.get_visible():
            self.show()

    def crea(self, g, p, info):
        try:
            if g == "immagine":
                return Immagine(self, p, info)
            if g in ("video", "audio"):
                return Lettore(self, p, info, g == "video")
            if g == "pdf":
                return Pdf(self, p, info)
            if g == "testo":
                return Testo(self, p, info)
        except (GLib.Error, OSError, ValueError, ImportError) as e:
            print("anteprima: %s: %s" % (p, e), file=sys.stderr)
        return Scheda(self, p, info)

    def prepara_vicini(self):
        if isinstance(self.contenuto, Immagine) and len(self.elenco) > 1:
            self.contenuto.prepara(self.elenco[(self.indice + 1) % len(self.elenco)])

    def aggiorna_testa(self):
        p = self.elenco[self.indice]
        self.titolo.set_text(os.path.basename(p) or p)
        self.titolo.set_tooltip_text(p)
        piu = len(self.elenco) > 1
        # sempre due righe (anche vuota la seconda): l'intestazione non cambia altezza quando arriva il conteggio
        self.conto.set_text(T("%d di %d", "%d of %d") % (self.indice + 1, len(self.elenco)) if piu else " ")
        self.b_prec.set_visible(piu)
        self.b_succ.set_visible(piu)
        self.b_apri.set_visible(self.app_apri is not None)

    def vai(self, passo):
        if len(self.elenco) > 1:
            self.indice = (self.indice + passo) % len(self.elenco)
            self.mostra()

    def metti_apri(self, p, info):
        """"Apri con Foto": l'app predefinita per quel tipo di file (per le cartelle Esplora file)."""
        tipo = tipo_mime(info) or ("inode/directory" if os.path.isdir(p) else "")
        self.app_apri = Gio.AppInfo.get_default_for_type(tipo, False) if tipo else None
        if self.app_apri is not None:
            self.b_apri.set_label(T("Apri con %s", "Open with %s") % self.app_apri.get_display_name())

    def apri_app(self):
        if self.app_apri is None:
            return
        p = self.elenco[self.indice]
        try:
            self.app_apri.launch([Gio.File.new_for_path(p)], Gdk.Display.get_default().get_app_launch_context())
        except GLib.Error as e:
            print("anteprima: %s" % e, file=sys.stderr)
            return
        self.chiudi()

    def chiudi(self):
        if self.contenuto is not None:
            self.contenuto.chiudi()
            self.contenuto = None
        self.destroy()

    # schermo intero, spostamento, tasti
    def cambia_intero(self):
        if self.intero:
            self.unfullscreen()
        else:
            self.fullscreen()

    def stato_finestra(self, w, ev):
        intero = bool(ev.new_window_state & Gdk.WindowState.FULLSCREEN)
        if intero != self.intero:
            self.intero = intero
            self.metti_margini()
            if self.vetro and intero:
                C.classe(self.cornice, "piatta")
            elif self.vetro:
                self.cornice.get_style_context().remove_class("piatta")
            C.metti_icona(self.b_intero, "view-restore-symbolic" if intero else "view-fullscreen-symbolic")
            self.forma()
            if not intero:
                self.dimensiona()
        return False

    def premi_testa(self, w, ev):
        if ev.button != 1:
            return False
        if ev.type == Gdk.EventType._2BUTTON_PRESS:
            self.cambia_intero()
        elif ev.type == Gdk.EventType.BUTTON_PRESS and not self.intero:
            self.set_position(Gtk.WindowPosition.NONE)   # spostata a mano: resta lì
            self.begin_move_drag(ev.button, int(ev.x_root), int(ev.y_root), ev.time)
        return True

    def tasto(self, w, ev):
        k = ev.keyval
        if ev.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK):
            if k in (Gdk.KEY_w, Gdk.KEY_W):
                self.chiudi()
                return True
            return False
        if k in (Gdk.KEY_space, Gdk.KEY_Escape):
            self.chiudi()
        elif k in (Gdk.KEY_Left, Gdk.KEY_Up):
            self.vai(-1)
        elif k in (Gdk.KEY_Right, Gdk.KEY_Down):
            self.vai(1)
        elif k in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.apri_app()
        elif k in (Gdk.KEY_f, Gdk.KEY_F, Gdk.KEY_F11):
            self.cambia_intero()
        elif hasattr(self.contenuto, "scorri"):
            return self.contenuto.scorri(k)
        else:
            return False
        return True

    # il visore di Foto chiede questi
    def foto_aperta(self, visore):
        pass

    def zoom_cambiato(self):
        pass

    def mouse_mosso(self):
        pass


if __name__ == "__main__":
    sys.exit(Anteprima().run(sys.argv))
