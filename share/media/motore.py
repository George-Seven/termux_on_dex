#!/usr/bin/env python3
"""Il motore del Lettore multimediale e del Lettore musicale, in comune con l'Anteprima (barra spaziatrice).

Motore: playbin di GStreamer e i suoi messaggi; il video a schermo con glimagesink (GPU) incastrato in una finestra
GTK, o disegnato con cairo (senza GPU, o se glimagesink non va). Sottotitoli disegnati da noi (Termux non ha il
plugin "pango" di GStreamer). leggi_brano: i tag di un brano e la copertina incorporata (GstPbutils.Discoverer).
Chi usa Motore riceve stato(), fine(), errore(), tag(), cambio(), durata_nota(), ridisegna_video(),
uscita_cambiata(), prossimo_uri(). Gira in Termux (GTK 3).
"""
import hashlib
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comune as C  # noqa: E402
from comune import GLib, Pango, T  # noqa: E402

L = C.L
Gst = C.gst()
C.gi.require_version("GstPbutils", "1.0")
C.gi.require_version("GstVideo", "1.0")
from gi.repository import GstPbutils, GstVideo  # noqa: E402,F401

COPERTINE = os.path.join(L.cache_dir(), "copertine")
LINGUE = {"it": "Italiano", "ita": "Italiano", "en": "English", "eng": "English", "fr": "Français", "fra": "Français",
          "fre": "Français", "de": "Deutsch", "deu": "Deutsch", "ger": "Deutsch", "es": "Español", "spa": "Español",
          "pt": "Português", "por": "Português", "ja": "日本語", "jpn": "日本語"}
NOMI_COPERTINA = ("cover", "folder", "front", "album", "albumart", "albumartsmall", "copertina")


def uri_di(p):
    return C.uri_di(p) if "://" not in p else p


def sottotitoli_di(p):
    """i sottotitoli accanto al video (film.srt, film.it.srt, film.ass...)."""
    base = os.path.splitext(p)[0]
    d = os.path.dirname(p)
    try:
        nomi = os.listdir(d)
    except OSError:
        return []
    out = []
    radice = os.path.basename(base).lower()
    for n in sorted(nomi):
        b, e = os.path.splitext(n)
        if e.lower() in (".srt", ".ass", ".ssa", ".vtt", ".sub") and (b.lower() == radice or b.lower().startswith(radice + ".")):
            out.append(os.path.join(d, n))
    return out


# --- sottotitoli: disegnati da noi (Termux non ha il plugin "pango" di GStreamer, quindi niente textoverlay) ---------
class Sottotitoli:
    """playbin dà il testo a un appsink (text-sink); overlaycomposition, davanti all'uscita video, chiede per ogni
    fotogramma cosa disegnare: il testo del momento, scritto con Pango su un'immagine trasparente. glimagesink lo fonde
    con la GPU; nel disegno con cairo lo fonde overlaycomposition stesso."""

    def __init__(self):
        self.cue = {}            # inizio (ns) → (fine, markup)
        self.lock = threading.Lock()
        self.misura = (0, 0)
        self.fatto = None        # (markup, w, h, composizione)

    def sink(self):
        a = Gst.ElementFactory.make("appsink", "testo")
        a.set_property("caps", Gst.Caps.from_string("text/x-raw"))
        a.set_property("sync", False)
        a.set_property("emit-signals", True)
        a.set_property("max-buffers", 200)
        a.connect("new-sample", self.nuovo)
        return a

    def nuovo(self, a):
        smp = a.emit("pull-sample")
        if smp is None:
            return Gst.FlowReturn.OK
        buf = smp.get_buffer()
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            testo = bytes(mi.data).decode("utf-8", "replace").strip("\x00").strip()
        finally:
            buf.unmap(mi)
        if buf.pts != Gst.CLOCK_TIME_NONE:
            durata = buf.duration if buf.duration != Gst.CLOCK_TIME_NONE else 4 * Gst.SECOND
            fmt = smp.get_caps().get_structure(0).get_string("format") or "utf8"
            if fmt != "pango-markup":
                testo = GLib.markup_escape_text(testo)
            with self.lock:
                self.cue[buf.pts] = (buf.pts + durata, testo)
                if len(self.cue) > 4000:
                    for k in sorted(self.cue)[:1000]:
                        del self.cue[k]
        return Gst.FlowReturn.OK

    def svuota(self):
        with self.lock:
            self.cue.clear()
            self.fatto = None

    def caps(self, oc, caps, ww, wh):
        st = caps.get_structure(0)
        self.misura = (st.get_value("width"), st.get_value("height"))

    def disegna(self, oc, campione):
        buf = campione.get_buffer()
        t = buf.pts
        if t == Gst.CLOCK_TIME_NONE or not self.cue:
            return None
        with self.lock:
            testi = [m for ini, (fine, m) in self.cue.items() if ini <= t < fine]
        if not testi:
            return None
        markup = "\n".join(testi)
        w, h = self.misura
        if not w or not h:
            return None
        if self.fatto and self.fatto[:3] == (markup, w, h):
            return self.fatto[3]
        comp = self.componi(markup, w, h)
        self.fatto = (markup, w, h, comp)
        return comp

    @staticmethod
    def componi(markup, w, h):
        import cairo
        from gi.repository import PangoCairo
        fs = max(14, int(h * 0.047))
        larga = int(w * 0.86)
        prova = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        lay = PangoCairo.create_layout(cairo.Context(prova))
        fd = Pango.FontDescription.from_string("Inter Semi-Bold")
        fd.set_absolute_size(fs * Pango.SCALE)
        lay.set_font_description(fd)
        lay.set_width(larga * Pango.SCALE)
        lay.set_alignment(Pango.Alignment.CENTER)
        lay.set_wrap(Pango.WrapMode.WORD_CHAR)
        try:
            lay.set_markup(markup, -1)
        except GLib.Error:
            lay.set_text(markup, -1)
        _ink, log = lay.get_pixel_extents()
        bordo = max(4, fs // 8)
        rw, rh = log.width + 2 * bordo + 4, log.height + 2 * bordo + 4
        sup = cairo.ImageSurface(cairo.FORMAT_ARGB32, rw, rh)
        cr = cairo.Context(sup)
        dx, dy = bordo + 2 - log.x, bordo + 2 - log.y
        cr.move_to(dx, dy)
        PangoCairo.layout_path(cr, lay)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_source_rgba(0, 0, 0, 0.75)
        cr.set_line_width(bordo)
        cr.stroke()
        cr.move_to(dx, dy)
        cr.set_source_rgb(1, 1, 1)
        PangoCairo.show_layout(cr, lay)
        sup.flush()
        dati = bytes(sup.get_data())
        gb = Gst.Buffer.new_wrapped(dati)
        GstVideo.buffer_add_video_meta(gb, GstVideo.VideoFrameFlags.NONE, GstVideo.VideoFormat.BGRA, rw, rh)
        x = (w - rw) // 2
        y = h - rh - int(h * 0.06)
        ret = GstVideo.VideoOverlayRectangle.new_raw(gb, x, y, rw, rh,
                                                     GstVideo.VideoOverlayFormatFlags.PREMULTIPLIED_ALPHA)
        return GstVideo.VideoOverlayComposition.new(ret)


# --- il motore: GStreamer -----------------------------------------------------------------------------------------
def gl_lento():
    """niente GPU vera (llvmpipe, schermo finto): glimagesink andrebbe a scatti."""
    s = os.environ.get("TOD_VIDEO_USCITA", "")
    if s in ("gl", "cairo"):
        return s == "cairo"
    prof = os.environ.get("TOD_GPU_PROFILE", "") + os.environ.get("GALLIUM_DRIVER", "")
    return "llvmpipe" in prof or "softpipe" in prof or os.environ.get("LIBGL_ALWAYS_SOFTWARE") == "1"


class Motore:
    """playbin e i suoi messaggi. ui è la finestra: riceve stato(), fine(), errore(), tag(), cambio()."""

    def __init__(self, ui, video):
        self.ui, self.video = ui, video
        self.pb = Gst.ElementFactory.make("playbin", "lettore")
        if not video:
            self.pb.set_property("video-sink", Gst.ElementFactory.make("fakesink", None))
            self.pb.set_property("flags", 0x2 | 0x10)   # solo audio, volume nostro
        else:
            st = Gst.ElementFactory.make("scaletempo", None)   # a 1,5x le voci restano voci
            if st:
                self.pb.set_property("audio-filter", st)
        self.sub = Sottotitoli() if video and Gst.ElementFactory.find("overlaycomposition") else None
        if self.sub:
            self.pb.set_property("text-sink", self.sub.sink())
        self.cairo = gl_lento()
        self.xid = None
        self.sink = None
        self.fotogramma = None    # (byte, w, h) per il disegno con cairo
        self.capsf = None
        self.velocita = 1.0
        self.uri = None
        self.successivo = None    # uri già messo da about-to-finish (senza pause tra i brani)
        self.lock = threading.Lock()
        if video:
            self.metti_uscita()
        bus = self.pb.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.messaggio)
        bus.enable_sync_message_emission()
        bus.connect("sync-message::element", self.sync)
        if not video:
            self.pb.connect("about-to-finish", self.quasi_finito)

    # uscita video
    def metti_uscita(self):
        oc = "overlaycomposition name=oc ! " if self.sub else ""
        if not self.cairo:
            try:
                # i sottotitoli li fonde overlaycomposition (capsfilter senza la "meta" della composizione): passata a
                # glimagesink incastrato nella finestra GTK, zink cadeva creando la swapchain (10 nov, H.264 1080p)
                b = Gst.parse_bin_from_description(
                    (oc + "video/x-raw ! " if oc else "") + "glimagesink name=s handle-events=false force-aspect-ratio=true",
                    True)
            except GLib.Error:
                b = None
            if b is not None:   # handle-events=false: clic e movimenti del mouse arrivano alla finestra GTK
                self.sink = b.get_by_name("s")
                self.collega_sub(b)
                self.pb.set_property("video-sink", b)
                return
            self.cairo = True
        b = Gst.parse_bin_from_description(
            oc + "videoconvertscale add-borders=true ! capsfilter name=cf caps=video/x-raw,format=BGRx,pixel-aspect-ratio=1/1"
            " ! appsink name=a emit-signals=true max-buffers=1 drop=true sync=true", True)
        self.collega_sub(b)
        self.capsf = b.get_by_name("cf")
        a = b.get_by_name("a")
        a.connect("new-sample", self.nuovo_fotogramma)
        self.sink = b
        self.pb.set_property("video-sink", b)

    def collega_sub(self, b):
        oc = b.get_by_name("oc")
        if oc is not None and self.sub:
            oc.connect("caps-changed", self.sub.caps)
            oc.connect("draw", self.sub.disegna)

    def misura_uscita(self, w, h):
        """cairo: GStreamer scala già alla misura dell'area (con le bande nere), il disegno copia e basta."""
        if self.capsf is not None and w > 1 and h > 1:
            w, h = min(w, 3840), min(h, 2160)
            self.capsf.set_property("caps", Gst.Caps.from_string(
                "video/x-raw,format=BGRx,pixel-aspect-ratio=1/1,width=%d,height=%d" % (w - w % 2, h - h % 2)))

    def nuovo_fotogramma(self, a):
        s = a.emit("pull-sample")
        if s is None:
            return Gst.FlowReturn.OK
        st = s.get_caps().get_structure(0)
        buf = s.get_buffer()
        ok, mi = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                self.fotogramma = (bytes(mi.data), st.get_value("width"), st.get_value("height"))
            finally:
                buf.unmap(mi)
            GLib.idle_add(self.ui.ridisegna_video)
        return Gst.FlowReturn.OK

    def sync(self, bus, msg):
        if GstVideo.is_video_overlay_prepare_window_handle_message(msg) and self.xid:
            msg.src.set_window_handle(self.xid)

    def passa_a_cairo(self):
        """glimagesink non va (niente GL): stessa posizione, disegno con cairo."""
        pos = self.posizione()
        stava = self.suona_ora()
        self.pb.set_state(Gst.State.NULL)
        self.cairo = True
        self.metti_uscita()
        self.ui.uscita_cambiata()
        if self.uri:
            self.pb.set_property("uri", self.uri)
            self.pb.set_state(Gst.State.PAUSED)
            self.pb.get_state(3 * Gst.SECOND)
            if pos:
                self.vai_a(pos)
            if stava:
                self.suona()

    # comandi
    def apri(self, uri, sub=None):
        self.pb.set_state(Gst.State.READY)
        self.uri = uri
        self.successivo = None
        self.velocita = 1.0
        self.pb.set_property("uri", uri)
        if self.video:
            self.pb.set_property("suburi", uri_di(sub) if sub else None)
            fl = self.pb.get_property("flags")
            self.pb.set_property("flags", (fl | 0x4) if sub else fl)
        self.fotogramma = None
        if self.sub:
            self.sub.svuota()

    def suona(self):
        self.pb.set_state(Gst.State.PLAYING)

    def pausa(self):
        self.pb.set_state(Gst.State.PAUSED)

    def ferma(self):
        self.pb.set_state(Gst.State.NULL)
        self.uri = None

    def suona_ora(self):
        st = self.pb.get_state(0)
        return st[1] == Gst.State.PLAYING or st[2] == Gst.State.PLAYING

    def posizione(self):
        ok, p = self.pb.query_position(Gst.Format.TIME)
        return p / Gst.SECOND if ok and p >= 0 else 0.0

    def durata(self):
        ok, d = self.pb.query_duration(Gst.Format.TIME)
        return d / Gst.SECOND if ok and d > 0 else 0.0

    def vai_a(self, s, preciso=True):
        fl = Gst.SeekFlags.FLUSH | (Gst.SeekFlags.ACCURATE if preciso else Gst.SeekFlags.KEY_UNIT)
        if self.velocita != 1.0:
            self.pb.seek(self.velocita, Gst.Format.TIME, fl, Gst.SeekType.SET, int(max(0, s) * Gst.SECOND),
                         Gst.SeekType.NONE, 0)
        else:
            self.pb.seek_simple(Gst.Format.TIME, fl, int(max(0, s) * Gst.SECOND))

    def metti_velocita(self, r):
        self.velocita = r
        p = self.posizione()
        self.pb.seek(r, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE, Gst.SeekType.SET,
                     int(p * Gst.SECOND), Gst.SeekType.NONE, 0)

    def metti_volume(self, v, muto):
        """v 0..100 come lo vede l'utente (curva cubica, come i cursori di volume dei sistemi)."""
        self.pb.set_property("volume", (max(0, min(100, v)) / 100.0) ** 3)
        self.pb.set_property("mute", bool(muto))

    # tracce
    def tracce(self, tipo):
        n = self.pb.get_property("n-" + tipo)
        out = []
        for i in range(n):
            tags = self.pb.emit("get-%s-tags" % tipo, i)
            nome = None
            if tags:
                ok, lingua = tags.get_string("language-code")
                if not ok:
                    ok, lingua = tags.get_string("language-name")
                if ok and lingua:
                    nome = LINGUE.get(lingua.lower(), lingua)
                ok, tit = tags.get_string("title")
                if ok and tit:
                    nome = "%s (%s)" % (tit, nome) if nome else tit
            out.append(nome or T("Traccia %d", "Track %d") % (i + 1))
        return out

    def traccia(self, tipo):
        return self.pb.get_property("current-" + tipo)

    def scegli_traccia(self, tipo, i):
        if tipo == "text":
            fl = self.pb.get_property("flags")
            if i < 0:
                self.pb.set_property("flags", fl & ~0x4)
                return
            self.pb.set_property("flags", fl | 0x4)
        self.pb.set_property("current-" + tipo, i)

    # senza pause tra i brani: nel filo di GStreamer si mette subito il prossimo
    def quasi_finito(self, pb):
        with self.lock:
            u = self.ui.prossimo_uri()
            if u:
                self.successivo = u
                pb.set_property("uri", u)

    def messaggio(self, bus, msg):
        t = msg.type
        if t == Gst.MessageType.EOS:
            self.ui.fine()
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            if self.video and not self.cairo and msg.src is not None and \
                    ("glimage" in (msg.src.get_name() or "") or "GL" in (err.message or "")):
                GLib.idle_add(lambda: self.passa_a_cairo() and False)
                return
            self.ui.errore(err.message)
        elif t == Gst.MessageType.STATE_CHANGED and msg.src == self.pb:
            self.ui.stato(msg.parse_state_changed()[1])
        elif t == Gst.MessageType.STREAM_START:
            with self.lock:
                if self.successivo:
                    self.uri, self.successivo = self.successivo, None
                    self.ui.cambio()
        elif t == Gst.MessageType.TAG:
            self.ui.tag(msg.parse_tag())
        elif t in (Gst.MessageType.DURATION_CHANGED, Gst.MessageType.ASYNC_DONE):
            self.ui.durata_nota()


def leggi_brano(percorso, mtime, dim, scopri):
    b = L.brano_vuoto(percorso, mtime, dim)
    try:
        info = scopri.discover_uri(C.uri_di(percorso))
    except GLib.Error:
        return b
    b["durata"] = (info.get_duration() or 0) / Gst.SECOND
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        tags = info.get_tags()
    if tags is None:
        st = info.get_audio_streams()
        tags = st[0].get_tags() if st else None
    if tags is None:
        return b

    def s(n):
        ok, v = tags.get_string(n)
        return v.strip() if ok and v else ""
    if s("title"):
        b["titolo"] = s("title")
    b["artista"], b["album"], b["artista_album"], b["genere"] = s("artist"), s("album"), s("album-artist"), s("genre")
    ok, n = tags.get_uint("track-number")
    if ok:
        b["traccia"] = n
    ok, n = tags.get_uint("album-disc-number")
    if ok:
        b["disco"] = n
    ok, dt = tags.get_date_time("datetime")
    if ok and dt and dt.has_year():
        b["anno"] = dt.get_year()
    else:
        ok, d = tags.get_date("date")
        if ok and d:
            b["anno"] = d.get_year()
    for nome in ("image", "preview-image"):
        ok, campione = tags.get_sample(nome)
        if ok and campione:
            buf = campione.get_buffer()
            ok2, mi = buf.map(Gst.MapFlags.READ)
            if ok2:
                try:
                    dati = bytes(mi.data)
                finally:
                    buf.unmap(mi)
                k = hashlib.sha1(("%s|%s" % L.chiave_album(b)).encode()).hexdigest()
                f = os.path.join(COPERTINE, k + ".img")
                if not os.path.exists(f):
                    try:
                        os.makedirs(COPERTINE, exist_ok=True)
                        with open(f + ".tmp", "wb") as o:
                            o.write(dati)
                        os.replace(f + ".tmp", f)
                    except OSError:
                        f = ""
                b["copertina"] = f
                break
    return b


def copertina_cartella(d, cache):
    if d in cache:
        return cache[d]
    trovata = ""
    try:
        nomi = os.listdir(d)
    except OSError:
        nomi = []
    for n in sorted(nomi):
        b, e = os.path.splitext(n.lower())
        if e in (".jpg", ".jpeg", ".png", ".webp") and (b in NOMI_COPERTINA or b.startswith("albumart")):
            trovata = os.path.join(d, n)
            break
    cache[d] = trovata
    return trovata
