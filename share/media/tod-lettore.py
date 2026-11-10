#!/usr/bin/env python3
"""Lettore multimediale (video) e Lettore musicale di Termux On DeX, come Media Player di Windows 11.

  tod-lettore.py --video [file...]     Lettore multimediale: raccolta video, riproduzione, schermo intero
  tod-lettore.py --musica [file...]    Lettore musicale: raccolta (brani, album, artisti), coda, copertine

Un solo programma in due modi (in Windows 11 sono la stessa app), ognuno con la sua voce nello Start e la sua
finestra (prgname tod-video / tod-musica, id com.termuxondex.Video / .Musica: un secondo avvio passa i file al primo).

Riproduzione con GStreamer (playbin; decodifica software, misurata il 9 novembre su S26 Ultra: 1080p H.264 a
240 fotogrammi/s, HEVC a 190). Il video va a schermo con glimagesink (GPU, zink): in una finestra 960×540 un 1080p a
30 fps senza scatti con il 42% di un core; il disegno con cairo dello stesso video costava 94%, ximagesink 85%,
xvimagesink non c'è (Termux:X11 non ha porte Xv). Senza GPU (llvmpipe, schermo finto) si ripiega sul disegno con
cairo; TOD_VIDEO_USCITA=gl|cairo lo sceglie a mano. Gira in Termux (GTK 3).
"""
import os
import sys
import threading

MODO = "musica" if "--musica" in sys.argv else "video"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comune as C  # noqa: E402
from comune import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, px, T  # noqa: E402

L = C.L
C.gi.require_version("GdkX11", "3.0")
from gi.repository import GdkX11  # noqa: E402,F401  (get_xid delle finestre native)

VIDEO = MODO == "video"
NOME = T("Lettore multimediale", "Media Player") if VIDEO else T("Lettore musicale", "Music Player")
GLib.set_prgname("tod-video" if VIDEO else "tod-musica")   # la barra riconosce la finestra (.desktop)
GLib.set_application_name(NOME)
from motore import Gst, GstPbutils, Motore, copertina_cartella, leggi_brano, sottotitoli_di, uri_di  # noqa: E402

IMPOSTAZIONI = os.path.join(L.config_dir(), "lettore.json")
RACCOLTA = os.path.join(L.cache_dir(), "musica.json")
VELOCITA = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]

CSS = """
.w11 .meta-titolo { font-weight: 600; }
.w11 .tempo { font-size: 8.5pt; color: TESTO2; min-width: 44px; }
.w11 .copertina-piccola { border-radius: 4px; }
.w11 .ora-titolo { font-size: 26pt; font-weight: 700; }
.w11 .ora-artista { font-size: 14pt; color: TESTO2; }
.w11 list.scheda-coda { background-color: CARTA; border: 1px solid LINEA; border-radius: 8px; padding: 4px; }
.w11 .scheda-coda row { border-radius: 4px; padding: 0 8px; }
.w11 .scheda-coda row:hover { background-color: PASSAGGIO; }
.w11 .in-corso { color: ACCTESTO; font-weight: 600; }
.w11 .barra.sopra-video { background-color: alpha(BARRA, 0.97); border-top: none; }
.w11 .barra.sopra-video.intero { border-radius: 8px; border: 1px solid LINEA; }
.w11 .vuoto-grande { font-size: 15pt; font-weight: 600; }
"""


def leggi_imp():
    d = L.leggi_json(IMPOSTAZIONI, {})
    return {"volume": d.get("volume", 70), "muto": d.get("muto", False), "casuale": d.get("casuale", False),
            "ripeti": d.get("ripeti", "no"), "scheda": d.get("scheda", "brani")}


def scrivi_imp(d):
    tutto = L.leggi_json(IMPOSTAZIONI, {})
    tutto.update(d)
    L.scrivi_json(IMPOSTAZIONI, tutto)


# --- la raccolta musicale: tag con GstPbutils.Discoverer, in cache ------------------------------------------------

class Raccolta:
    """i brani delle cartelle Musica (e Download), con i tag letti una volta e tenuti in ~/.cache/tod/musica.json."""

    def __init__(self):
        self.brani = {}
        self.lettura = False

    def aggiorna(self, avanzamento, fatto):
        if self.lettura:
            return
        self.lettura = True

        def lavoro():
            cache = L.leggi_json(RACCOLTA, {})
            file = []
            for _nome, d, prof in L.cartelle_musica():
                file += L.scansiona(d, L.AUDIO, prof)
            nuovi, da_leggere = {}, []
            for p, mt, dim in file:
                c = cache.get(p)
                if c and c.get("mtime") == mt and c.get("dim") == dim:
                    nuovi[p] = c
                else:
                    da_leggere.append((p, mt, dim))
            scopri = GstPbutils.Discoverer.new(8 * Gst.SECOND) if da_leggere else None
            cartelle = {}
            for i, (p, mt, dim) in enumerate(da_leggere):
                b = leggi_brano(p, mt, dim, scopri)
                if not b["copertina"]:
                    b["copertina"] = copertina_cartella(os.path.dirname(p), cartelle)
                nuovi[p] = b
                if i % 25 == 24:
                    L.scrivi_json(RACCOLTA, nuovi)
                    GLib.idle_add(avanzamento, i + 1, len(da_leggere), dict(nuovi))
            for p, b in nuovi.items():   # una copertina messa nella cartella dopo
                if not b.get("copertina") or not os.path.exists(b["copertina"]):
                    b["copertina"] = copertina_cartella(os.path.dirname(p), cartelle)
            L.scrivi_json(RACCOLTA, nuovi)
            GLib.idle_add(fine, nuovi)

        def fine(nuovi):
            self.brani = nuovi
            self.lettura = False
            fatto()
            return False
        self.brani = L.leggi_json(RACCOLTA, {})
        threading.Thread(target=lavoro, daemon=True).start()

    def brano(self, p):
        b = self.brani.get(p)
        if b is None:
            try:
                st = os.stat(p)
                b = L.brano_vuoto(p, st.st_mtime, st.st_size)
            except OSError:
                b = L.brano_vuoto(p)
        return b


# --- l'app -----------------------------------------------------------------------------------------------------
class Lettore(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.termuxondex.Video" if VIDEO else "com.termuxondex.Musica",
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None

    def do_command_line(self, cl):
        cwd = cl.get_cwd() or os.getcwd()
        file = []
        for a in cl.get_arguments()[1:]:
            if a.startswith("--"):
                continue
            if a.startswith("file://"):
                a = GLib.filename_from_uri(a)[0]
            file.append(os.path.join(cwd, a) if not os.path.isabs(a) else a)
        # un video aperto con il lettore musicale (o il contrario): all'app giusta
        altri = [f for f in file if L.tipo_di(f) == ("audio" if VIDEO else "video")]
        if altri and len(altri) == len(file):
            C.apri_con_app("tod-musica.desktop" if VIDEO else "tod-video.desktop", altri,
                           [sys.executable, os.path.abspath(__file__), "--musica" if VIDEO else "--video"])
            if not self.win:
                return 0
            file = []
        if not self.win:
            self.win = Finestra(self)
        if file:
            self.win.apri_file(file)
        self.win.present()
        return 0


class Finestra(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=NOME)
        C.imposta_scala()
        w, h = px(1180), px(760)
        mon = Gdk.Display.get_default().get_primary_monitor() or Gdk.Display.get_default().get_monitor(0)
        if mon:
            a = mon.get_workarea()
            w, h = min(w, int(a.width * 0.9)), min(h, int(a.height * 0.9))
        self.set_default_size(w, h)
        self.set_icon_name("multimedia-video-player" if VIDEO else "multimedia-audio-player")
        C.classe(self, "w11")
        self.colori = C.colori()
        self.stile = C.Stile(CSS, self.tema_cambiato)
        self.imp = leggi_imp()
        self.mini = C.Miniature(1 if VIDEO else 2)
        self.posizioni = L.Posizioni()
        self.coda = L.Coda()
        self.coda.ripeti = self.imp["ripeti"] if self.imp["ripeti"] in ("no", "tutti", "uno") else "no"
        self.coda.metti_casuale(self.imp["casuale"] and not VIDEO)
        self.continua = not VIDEO      # video: si ferma alla fine, se non si è chiesto "riproduci tutto"
        self.trascino = False
        self.ultimo_salto = 0
        self.durata = 0.0
        self.schermo_intero = False
        self._t_nascondi = None
        self._t_pos = GLib.timeout_add(250, self.tic)
        self.ult_puntatore = None
        self.raccolta = Raccolta()
        self.mappata = {"finestra": False, "area": False}
        self.attesa_area = None
        self.elementi_video = []
        self.sezione = "raccolta"
        self.motore = Motore(self, VIDEO)
        self.motore.metti_volume(self.imp["volume"], self.imp["muto"])
        radice = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(radice)
        self.corpo = Gtk.Box()
        radice.pack_start(self.corpo, True, True, 0)
        self.nav = self.costruisci_nav()
        self.corpo.pack_start(self.nav, False, False, 0)
        self.sovra = Gtk.Overlay()
        self.strato = C.classe(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), "strato")
        self.sovra.add(self.strato)
        self.corpo.pack_start(self.sovra, True, True, 0)
        self.pagine = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        self.strato.pack_start(self.pagine, True, True, 0)
        self.barra = self.costruisci_barra()
        self.box_barra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.box_barra.pack_start(self.barra, False, False, 0)
        radice.pack_end(self.box_barra, False, False, 0)
        self.popup = None
        if VIDEO:
            self.costruisci_raccolta_video()
            self.costruisci_schermo()
        else:
            self.costruisci_musica()
        self.connect("key-press-event", self.tasto)
        self.connect("window-state-event", self.stato_finestra)
        self.connect("map-event", lambda *_: self.mappa("finestra", True))
        self.connect("unmap-event", lambda *_: self.mappa("finestra", False))
        self.connect("delete-event", self.chiudi)
        self.connect("focus-out-event", lambda *_: self.nascondi_controlli(forza=True) and False)
        self.show_all()
        self.aggiorna_controlli()
        self.barra_visibile()
        GLib.idle_add(lambda: self.set_focus(None) and False)   # niente fuoco sulla ricerca all'apertura
        if VIDEO:
            self.mostra_raccolta()
        else:
            self.carica_raccolta()

    def tema_cambiato(self):
        self.colori = C.colori()
        for g in getattr(self, "griglie", []):
            g.ridisegna()
        if VIDEO:
            self.v_area.queue_draw()
        else:
            self.sfondo_ora.queue_draw()

    # ======================================================================================== navigazione
    def costruisci_nav(self):
        nav = C.Nav(self.scegli, cerca=self.cerca, larga=260)
        if VIDEO:
            nav.voce("raccolta", T("Raccolta video", "Video library"), "folder-videos-symbolic")
            self.cartelle = L.cartelle_video()
            if len(self.cartelle) > 1:
                nav.gruppo(T("Cartelle", "Folders"))
                for i, (nome, _p, _x) in enumerate(self.cartelle):
                    nav.voce("cartella:%d" % i, nome, "folder-symbolic")
        else:
            nav.voce("raccolta", T("Raccolta musicale", "Music library"), "folder-music-symbolic")
            nav.voce("coda", T("Coda di riproduzione", "Play queue"), "view-list-symbolic")
            nav.voce("ora", T("Riproduzione in corso", "Now playing"), "media-playback-start-symbolic")
        nav.voce("apri", T("Apri file", "Open file(s)"), "document-open-symbolic", in_fondo=True)
        return nav

    def scegli(self, k):
        if k == "apri":
            self.nav.segna(self.sezione)
            self.scegli_file()
            return
        if k == "coda":
            self.mostra_coda()
        elif k == "ora":
            self.mostra_ora()
        else:
            self.sezione = k
            self.mostra_raccolta()

    def cerca(self, testo):
        self.testo_cerca = testo
        if self.pagine.get_visible_child_name() not in ("raccolta", "brani", "album", "artisti"):
            self.mostra_raccolta()
        else:
            self.riempi_raccolta()

    def scegli_file(self):
        d = Gtk.FileChooserNative.new(T("Apri file", "Open file(s)"), self, Gtk.FileChooserAction.OPEN,
                                      T("Apri", "Open"), T("Annulla", "Cancel"))
        d.set_select_multiple(True)
        f = Gtk.FileFilter()
        f.set_name(T("Video", "Videos") if VIDEO else T("Musica", "Music"))
        for e in sorted(L.VIDEO if VIDEO else L.AUDIO):
            f.add_pattern("*" + e)
            f.add_pattern("*" + e.upper())
        d.add_filter(f)
        if d.run() == Gtk.ResponseType.ACCEPT:
            self.apri_file([g.get_path() for g in d.get_files() if g.get_path()], tutti=True)
        d.destroy()

    # ======================================================================================== barra in basso
    def costruisci_barra(self):
        b = C.classe(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0), "barra")
        # riga 1: tempo e cursore
        r1 = Gtk.Box(spacing=px(10))
        r1.set_margin_start(px(18))
        r1.set_margin_end(px(18))
        r1.set_margin_top(px(6))
        self.t_ora = C.etichetta("0:00", "tempo numeri", xalign=0.0, ellissi=False)
        self.t_tot = C.etichetta("0:00", "tempo numeri", xalign=1.0, ellissi=False)
        self.cursore = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.cursore.set_draw_value(False)
        self.cursore.set_can_focus(False)
        self.cursore.connect("button-press-event", self.cursore_premuto)
        self.cursore.connect("button-release-event", self.cursore_rilasciato)
        self.cursore.connect("change-value", self.cursore_mosso)
        r1.pack_start(self.t_ora, False, False, 0)
        r1.pack_start(self.cursore, True, True, 0)
        r1.pack_start(self.t_tot, False, False, 0)
        b.pack_start(r1, False, False, 0)
        # riga 2: titolo, comandi, opzioni
        r2 = Gtk.Box(spacing=px(4))
        r2.set_margin_start(px(14))
        r2.set_margin_end(px(14))
        r2.set_margin_bottom(px(10))
        r2.set_margin_top(px(2))
        sx = Gtk.Box(spacing=px(12))
        sx.set_size_request(px(300), -1)
        if VIDEO:   # sopra il video (finestra di glimagesink) un widget GTK non si vedrebbe: qui nella barra
            self.v_indietro = C.bottone("go-previous-symbolic", suggerimento=T("Torna alla raccolta (Esc)",
                                                                              "Back to library (Esc)"),
                                        azione=self.mostra_raccolta)
            sx.pack_start(self.v_indietro, False, False, 0)
        else:
            self.copertina = Gtk.DrawingArea()
            self.copertina.set_size_request(px(52), px(52))
            self.copertina.set_valign(Gtk.Align.CENTER)
            self.copertina.connect("draw", self.disegna_copertina_barra)
            self.pb_barra = None
            ev = Gtk.EventBox()
            ev.set_tooltip_text(T("Riproduzione in corso", "Now playing"))
            ev.add(self.copertina)
            ev.connect("button-release-event", lambda *_: self.mostra_ora())
            sx.pack_start(ev, False, False, 0)
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(2), valign=Gtk.Align.CENTER)
        self.m_titolo = C.etichetta("", "meta-titolo")
        self.m_sotto = C.etichetta("", "tenue")
        tb.pack_start(self.m_titolo, False, False, 0)
        tb.pack_start(self.m_sotto, False, False, 0)
        self.m_avviso = Gtk.Box(spacing=px(6))
        self.m_avviso_testo = C.etichetta("", "sotto")
        self.m_avviso.pack_start(self.m_avviso_testo, False, False, 0)
        self.m_avviso_b = C.classe(Gtk.Button(), "lieve")
        self.m_avviso_b.set_can_focus(False)
        self.m_avviso_b.connect("clicked", lambda *_: self._avviso_azione())
        self.m_avviso.pack_start(self.m_avviso_b, False, False, 0)
        self.m_avviso.set_no_show_all(True)
        tb.pack_start(self.m_avviso, False, False, 0)
        sx.pack_start(tb, True, True, 0)
        r2.pack_start(sx, False, False, 0)
        centro = Gtk.Box(spacing=px(6), halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        if not VIDEO:
            self.b_casuale = C.bottone_disegno("casuale", suggerimento=T("Riproduzione casuale", "Shuffle"),
                                               azione=self.cambia_casuale)
            centro.pack_start(self.b_casuale, False, False, 0)
        self.b_prec = C.bottone("media-skip-backward-symbolic", suggerimento=T("Precedente (Ctrl+B)", "Previous (Ctrl+B)"),
                                azione=self.precedente)
        centro.pack_start(self.b_prec, False, False, 0)
        if VIDEO:
            centro.pack_start(C.bottone_salto(10, False, T("Indietro di 10 secondi", "Skip back 10 seconds"),
                                              lambda: self.salta(-10)), False, False, 0)
        self.b_play = C.bottone("media-playback-start-symbolic", suggerimento=T("Riproduci (Ctrl+P)", "Play (Ctrl+P)"),
                                azione=self.alterna, cls="play", lato=18)
        centro.pack_start(self.b_play, False, False, px(6))
        if VIDEO:
            centro.pack_start(C.bottone_salto(30, True, T("Avanti di 30 secondi", "Skip forward 30 seconds"),
                                              lambda: self.salta(30)), False, False, 0)
        self.b_succ = C.bottone("media-skip-forward-symbolic", suggerimento=T("Successivo (Ctrl+F)", "Next (Ctrl+F)"),
                                azione=self.successivo)
        centro.pack_start(self.b_succ, False, False, 0)
        if not VIDEO:
            self.b_ripeti = C.bottone_disegno("ripeti", suggerimento=T("Ripeti", "Repeat"), azione=self.cambia_ripeti)
            centro.pack_start(self.b_ripeti, False, False, 0)
        r2.set_center_widget(centro)
        dx = Gtk.Box(spacing=px(2), halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        dx.set_size_request(px(300), -1)
        self.b_volume = self.pulsante_volume()
        dx.pack_end(self.b_intero if VIDEO else self.pulsante_ora(), False, False, 0)
        dx.pack_end(self.menu_altro(), False, False, 0)
        if VIDEO:
            dx.pack_end(self.menu_sottotitoli(), False, False, 0)
        dx.pack_end(self.b_volume, False, False, 0)
        r2.pack_end(dx, False, False, 0)
        b.pack_start(r2, False, False, 0)
        return b

    def avviso(self, testo, azione=None, testo_azione=None, secondi=5):
        """un avviso al posto della riga sotto il titolo, nella barra (si vede anche sopra il video)."""
        self.m_avviso_testo.set_text(testo)
        self._azione_avviso = azione
        self.m_avviso_b.set_label(testo_azione or "")
        self.m_avviso_b.set_visible(bool(azione))
        self.m_avviso_testo.show()
        self.m_avviso.show()
        self.m_sotto.hide()
        self.mostra_controlli()
        if getattr(self, "_t_avviso", None):
            GLib.source_remove(self._t_avviso)
        self._t_avviso = GLib.timeout_add(secondi * 1000, self._avviso_via)

    def _avviso_via(self):
        self._t_avviso = None
        self.m_avviso.hide()
        self.m_sotto.show()
        return False

    def _avviso_azione(self):
        if self._azione_avviso:
            self._azione_avviso()
        self._avviso_via()

    @property
    def b_intero(self):
        if not hasattr(self, "_b_intero"):
            self._b_intero = C.bottone("view-fullscreen-symbolic", suggerimento=T("Schermo intero (F11)", "Full screen (F11)"),
                                       azione=self.cambia_intero)
        return self._b_intero

    def pulsante_ora(self):
        self.b_ora = C.bottone("view-dual-symbolic", suggerimento=T("Riproduzione in corso", "Now playing"),
                               azione=self.mostra_ora)
        return self.b_ora

    def pulsante_volume(self):
        mb = Gtk.MenuButton()
        C.classe(mb, "icona")
        mb.set_can_focus(False)
        mb.set_tooltip_text(T("Volume", "Volume"))
        self.i_volume = C.immagine_icona("audio-volume-high-symbolic")
        mb.add(self.i_volume)
        pop = C.classe(Gtk.Popover(), "menu-w11")
        hb = Gtk.Box(spacing=px(8))
        hb.set_margin_start(px(8))
        hb.set_margin_end(px(14))
        hb.set_margin_top(px(8))
        hb.set_margin_bottom(px(8))
        self.b_muto = C.bottone("audio-volume-high-symbolic", suggerimento=T("Disattiva audio (M)", "Mute (M)"),
                                azione=self.cambia_muto)
        hb.pack_start(self.b_muto, False, False, 0)
        self.s_volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.s_volume.set_size_request(px(200), -1)
        self.s_volume.set_draw_value(False)
        self.s_volume.set_value(self.imp["volume"])
        self.s_volume.connect("value-changed", lambda s: self.metti_volume(s.get_value()))
        hb.pack_start(self.s_volume, True, True, 0)
        self.l_volume = C.etichetta(str(int(self.imp["volume"])), "numeri", xalign=1.0, ellissi=False)
        self.l_volume.set_width_chars(3)
        hb.pack_start(self.l_volume, False, False, 0)
        hb.show_all()
        pop.add(hb)
        mb.set_popover(pop)
        return mb

    def menu_altro(self):
        mb = Gtk.MenuButton()
        C.classe(mb, "icona")
        mb.set_can_focus(False)
        mb.set_tooltip_text(T("Altre opzioni", "More options"))
        mb.add(C.immagine_icona("view-more-symbolic"))
        pop = C.classe(Gtk.Popover(), "menu-w11")
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vb.set_margin_start(px(4))
        vb.set_margin_end(px(4))
        vb.set_margin_top(px(4))
        vb.set_margin_bottom(px(4))
        if VIDEO:
            vb.pack_start(C.etichetta(T("Velocità", "Speed"), "tenue"), False, False, px(4))
            self.r_velocita = {}
            prima = None
            for v in VELOCITA:
                testo = (T("Normale", "Normal") if v == 1 else ("%gx" % v).replace(".", "," if L.ITALIANO else "."))
                rb = Gtk.RadioButton.new_with_label_from_widget(prima, testo)
                prima = prima or rb
                rb.set_active(v == 1)
                rb.connect("toggled", lambda b, v=v: b.get_active() and self.motore.metti_velocita(v))
                self.r_velocita[v] = rb
                vb.pack_start(rb, False, False, 0)
            vb.pack_start(Gtk.Separator(), False, False, px(4))
        for testo, az in ((T("Apri file…", "Open file(s)…"), self.scegli_file),
                          (T("Mostra nella cartella", "Show in folder"), self.mostra_cartella),
                          (T("Proprietà del file", "File properties"), self.proprieta)):
            b = Gtk.ModelButton(text=testo)
            b.connect("clicked", lambda _b, az=az: (pop.popdown(), az()))
            vb.pack_start(b, False, False, 0)
        vb.show_all()
        pop.add(vb)
        mb.set_popover(pop)
        return mb

    def menu_sottotitoli(self):
        mb = Gtk.MenuButton()
        C.classe(mb, "icona")
        mb.set_can_focus(False)
        mb.set_tooltip_text(T("Sottotitoli e lingua", "Subtitles and languages"))
        mb.add(C.immagine_icona("media-view-subtitles-symbolic"))
        self.pop_sub = C.classe(Gtk.Popover(), "menu-w11")
        mb.set_popover(self.pop_sub)
        self.pop_sub.connect("show", lambda *_: self.riempi_sottotitoli())
        self.b_sub = mb
        return mb

    def riempi_sottotitoli(self):
        if self.pop_sub.get_child():
            self.pop_sub.remove(self.pop_sub.get_child())
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vb.set_margin_start(px(8))
        vb.set_margin_end(px(12))
        vb.set_margin_top(px(8))
        vb.set_margin_bottom(px(8))
        for tipo, titolo, spento in (("text", T("Sottotitoli", "Subtitles"), T("Disattivati", "Off")),
                                     ("audio", T("Audio", "Audio"), None)):
            nomi = self.motore.tracce(tipo)
            if tipo == "audio" and len(nomi) < 2:
                continue
            vb.pack_start(C.etichetta(titolo, "tenue"), False, False, px(4))
            prima = None
            cor = self.motore.traccia(tipo)
            attivi = bool(self.motore.pb.get_property("flags") & 0x4)
            voci = ([(-1, spento)] if spento else []) + list(enumerate(nomi))
            if tipo == "text" and not nomi:
                vb.pack_start(C.etichetta(T("Nessun sottotitolo", "No subtitles"), "sotto"), False, False, 0)
                continue
            for i, nome in voci:
                rb = Gtk.RadioButton.new_with_label_from_widget(prima, nome)
                prima = prima or rb
                rb.set_active((i == cor and (attivi or tipo == "audio")) or (i == -1 and not attivi))
                rb.connect("toggled", lambda b, t=tipo, i=i: b.get_active() and self.motore.scegli_traccia(t, i))
                vb.pack_start(rb, False, False, 0)
        vb.show_all()
        self.pop_sub.add(vb)

    # --- comandi ---
    def alterna(self):
        if self.motore.uri is None:
            if self.coda.corrente():
                self.suona_corrente()
            elif not VIDEO and self.raccolta.brani:
                self.suona_tutto(casuale=False)
            return
        if self.motore.suona_ora():
            self.motore.pausa()
        else:
            self.motore.suona()

    def salta(self, s):
        if self.motore.uri:
            d = self.motore.durata()
            self.motore.vai_a(max(0, min(self.motore.posizione() + s, d - 0.5 if d else 1e9)))
            self.mostra_controlli()

    def precedente(self):
        if self.motore.uri and self.motore.posizione() > 3:   # come Media Player: prima all'inizio del brano
            self.motore.vai_a(0)
            return
        if self.coda.indietro() is not None:
            self.suona_corrente()

    def successivo(self):
        if self.coda.avanti() is not None:
            self.suona_corrente()

    def cambia_casuale(self):
        self.coda.metti_casuale(not self.coda.casuale)
        scrivi_imp({"casuale": self.coda.casuale})
        self.aggiorna_controlli()
        self.riempi_coda()

    def cambia_ripeti(self):
        self.coda.giro_ripeti()
        scrivi_imp({"ripeti": self.coda.ripeti})
        self.aggiorna_controlli()

    def metti_volume(self, v):
        self.imp["volume"] = v
        if self.imp["muto"] and v > 0:
            self.imp["muto"] = False
        self.motore.metti_volume(v, self.imp["muto"])
        self.l_volume.set_text(str(int(v)))
        self.icona_volume()
        if getattr(self, "_t_vol", None):
            GLib.source_remove(self._t_vol)
        self._t_vol = GLib.timeout_add(600, lambda: scrivi_imp({"volume": int(self.imp["volume"]),
                                                                 "muto": self.imp["muto"]}) or setattr(self, "_t_vol", None))

    def cambia_muto(self):
        self.imp["muto"] = not self.imp["muto"]
        self.motore.metti_volume(self.imp["volume"], self.imp["muto"])
        self.icona_volume()
        scrivi_imp({"muto": self.imp["muto"]})

    def icona_volume(self):
        v = self.imp["volume"]
        n = "audio-volume-muted-symbolic" if self.imp["muto"] or v == 0 else \
            "audio-volume-low-symbolic" if v < 34 else "audio-volume-medium-symbolic" if v < 67 else "audio-volume-high-symbolic"
        self.i_volume.set_from_icon_name(n, Gtk.IconSize.BUTTON)
        self.i_volume.set_pixel_size(px(16))
        C.metti_icona(self.b_muto, n)

    def aggiorna_controlli(self):
        suona = self.motore.suona_ora()
        C.metti_icona(self.b_play, "media-playback-pause-symbolic" if suona else "media-playback-start-symbolic", 18)
        self.b_play.set_tooltip_text(T("Pausa (Ctrl+P)", "Pause (Ctrl+P)") if suona else T("Riproduci (Ctrl+P)", "Play (Ctrl+P)"))
        n = len(self.coda)
        self.b_prec.set_sensitive(n > 0)
        self.b_succ.set_sensitive(n > 1 or self.coda.ripeti != "no")
        if not VIDEO:
            for b, si in ((self.b_casuale, self.coda.casuale), (self.b_ripeti, self.coda.ripeti != "no")):
                ctx = b.get_style_context()
                (ctx.add_class if si else ctx.remove_class)("attivo")
            self.b_ripeti.icona.metti("ripeti1" if self.coda.ripeti == "uno" else "ripeti")
            self.b_ripeti.set_tooltip_text({"no": T("Ripeti: no", "Repeat: off"), "tutti": T("Ripeti: tutti", "Repeat: all"),
                                            "uno": T("Ripeti: uno", "Repeat: one")}[self.coda.ripeti])
            self.b_casuale.set_tooltip_text(T("Riproduzione casuale: sì", "Shuffle: on") if self.coda.casuale
                                            else T("Riproduzione casuale: no", "Shuffle: off"))
        self.icona_volume()

    # --- cursore della posizione ---
    def cursore_premuto(self, w, ev):
        self.trascino = True
        return False

    def cursore_rilasciato(self, w, ev):
        self.trascino = False
        if self.motore.uri:
            self.motore.vai_a(self.cursore.get_value(), preciso=True)
        return False

    def cursore_mosso(self, w, scroll, v):
        if not self.motore.uri:
            return True
        v = max(0, min(v, self.durata or v))
        self.t_ora.set_text(L.tempo(v))
        ora = GLib.get_monotonic_time()
        if not self.trascino:
            self.motore.vai_a(v)
        elif ora - self.ultimo_salto > 120000:   # mentre si trascina: ai fotogrammi chiave, al più ~8 volte al secondo
            self.ultimo_salto = ora
            self.motore.vai_a(v, preciso=False)
        return False

    def tic(self):
        if self.motore.uri and not self.trascino:
            p = self.motore.posizione()
            if self.durata <= 0:
                self.durata_nota()
            if self.durata > 0:
                self.cursore.set_value(min(p, self.durata))
            self.t_ora.set_text(L.tempo(p))
        if self.schermo_intero:
            self.guarda_puntatore()
        if VIDEO and not self.motore.cairo and not getattr(self.motore, "sink_ok", False) and self.motore.suona_ora():
            self.controlla_gl()
        return True

    def controlla_gl(self):
        """dopo 5 s di video: se glimagesink ne mostra meno di 2/3 (niente GPU vera), si passa al disegno con cairo."""
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

    def durata_nota(self):
        d = self.motore.durata()
        if d > 0 and abs(d - self.durata) > 0.5:
            self.durata = d
            self.cursore.set_range(0, d)
            self.t_tot.set_text(L.tempo(d))

    # --- dal motore ---
    def stato(self, st):
        self.aggiorna_controlli()
        if VIDEO:
            self.mostra_controlli()
        return False

    def fine(self):
        if VIDEO:
            self.ricorda_posizione(fine=True)
        if self.continua or self.coda.ripeti == "uno":
            if self.coda.avanti(automatico=True) is not None:
                self.suona_corrente()
                return
        # finito: si torna all'inizio, in pausa (Media Player)
        self.motore.pausa()
        self.motore.vai_a(0)
        if not VIDEO:
            self.motore.ferma()
            self.cursore.set_value(0)
            self.t_ora.set_text("0:00")
        self.aggiorna_controlli()
        self.mostra_controlli()

    def errore(self, msg):
        self.avviso(T("Impossibile riprodurre: %s", "Can't play: %s") % msg, secondi=8)
        if self.continua and len(self.coda) > 1 and self.coda.prossimo_indice() is not None:
            self.coda.avanti()
            GLib.timeout_add(800, lambda: self.suona_corrente() and False)
        else:
            self.motore.ferma()
            self.aggiorna_controlli()

    def tag(self, tags):
        pass

    def cambio(self):
        """il brano successivo è partito da solo (senza pausa): la coda va avanti."""
        GLib.idle_add(self._cambio)

    def _cambio(self):
        self.coda.avanti(automatico=True)
        self.durata = 0
        self.mostra_meta()
        self.riempi_coda()
        return False

    def prossimo_uri(self):
        """dal filo di GStreamer, a fine brano: il prossimo, se si continua."""
        if VIDEO or not self.continua:
            return None
        i = self.coda.prossimo_indice(automatico=True)
        if i is None:
            return None
        p = self.coda.elementi[i]
        return uri_di(p) if os.path.exists(p) else None

    # --- riproduzione ---
    def apri_file(self, file, tutti=False):
        file = [f for f in file if os.path.exists(f)]
        if not file:
            return
        if len(file) == 1 and not tutti:
            if VIDEO:
                elenco, i = L.fratelli(file[0], L.VIDEO)
                self.continua = False
            else:
                elenco, i = L.fratelli(file[0], L.AUDIO)
                self.continua = True
        else:
            elenco, i = file, 0
            self.continua = True
        self.coda.imposta(elenco, i)
        self.suona_corrente()

    def suona_corrente(self):
        p = self.coda.corrente()
        if not p:
            return
        if VIDEO:
            self.ricorda_posizione()
            self.mostra_schermo()
        self.durata = 0
        self.cursore.set_range(0, 1)
        self.cursore.set_value(0)
        self.t_tot.set_text("0:00")
        self.mostra_meta()
        self.aggiorna_controlli()
        if VIDEO and not self.motore.cairo and not (self.mappata["finestra"] and self.mappata["area"]):
            # glimagesink crea la sua finestra dentro l'area: se l'area non è ancora davvero a schermo (finestra
            # appena aperta, pagina appena cambiata) zink non riesce a creare la swapchain e il processo cade
            # (10 nov, sempre all'apertura da Esplora file). GDK la dà per visibile subito: conta il MapNotify vero
            self.motore.ferma()
            self.attesa_area = p
            return
        self.avvia(p)

    def mappa(self, chi, si):
        """map-event/unmap-event veri (MapNotify di X) della finestra e dell'area del video."""
        self.mappata[chi] = si
        p = getattr(self, "attesa_area", None)
        if si and p and self.mappata["finestra"] and self.mappata["area"]:
            self.attesa_area = None
            GLib.timeout_add(120, lambda: p == self.coda.corrente() and self.avvia(p) and False)
        return False

    def avvia(self, p):
        if p != self.coda.corrente():
            return False
        sub = (sottotitoli_di(p) or [None])[0] if VIDEO else None
        self.motore.apri(uri_di(p), sub)
        if VIDEO and self.motore.cairo:
            al = self.v_area.get_allocation()
            self.motore.misura_uscita(al.width, al.height)
        da = self.posizioni.leggi(p) if VIDEO else 0
        if da:
            self.motore.pausa()
            self.motore.pb.get_state(3 * Gst.SECOND)
            self.motore.vai_a(da)
            self.avviso(T("Riprendo da %s", "Resuming from %s") % L.tempo(da), lambda: self.motore.vai_a(0),
                        T("Dall'inizio", "Start over"), secondi=7)
        self.motore.suona()
        if VIDEO and hasattr(self, "r_velocita"):
            self.r_velocita[1.0].set_active(True)
        self.mostra_meta()
        self.riempi_coda()
        self.aggiorna_controlli()

    def ricorda_posizione(self, fine=False):
        if VIDEO and self.motore.uri:
            p = self.coda.corrente()
            if p:
                d = self.motore.durata()
                self.posizioni.scrivi(p, d if fine else self.motore.posizione(), d)

    def mostra_meta(self):
        p = self.coda.corrente()
        if not p:
            self.m_titolo.set_text("")
            self.m_sotto.set_text("")
            self.set_title(NOME)
            return
        if VIDEO:
            titolo = os.path.splitext(os.path.basename(p))[0]
            sotto = os.path.basename(os.path.dirname(p))
        else:
            b = self.raccolta.brano(p)
            titolo, sotto = b["titolo"], " · ".join(x for x in (L.artista_di(b), L.album_di(b)) if x)
            self.metti_copertina(b)
            if self.pagine.get_visible_child_name() == "ora":
                self.riempi_ora()
        self.m_titolo.set_text(titolo)
        self.m_sotto.set_text(sotto)
        self.set_title("%s - %s" % (titolo, NOME))

    def mostra_cartella(self):
        p = self.coda.corrente()
        if p:
            C.mostra_nella_cartella(p)

    def proprieta(self):
        p = self.coda.corrente()
        if not p:
            return
        try:
            st = os.stat(p)
        except OSError:
            return
        righe = [(T("Nome", "Name"), os.path.basename(p)), (T("Cartella", "Folder"), os.path.dirname(p)),
                 (T("Dimensione", "Size"), L.misura(st.st_size)), (T("Durata", "Length"), L.tempo(self.motore.durata()))]
        if not VIDEO:
            b = self.raccolta.brano(p)
            righe[1:1] = [(T("Titolo", "Title"), b["titolo"]), (T("Artista", "Artist"), L.artista_di(b)),
                          (T("Album", "Album"), L.album_di(b))]
        d = Gtk.Dialog(title=T("Proprietà del file", "File properties"), transient_for=self, modal=True)
        C.classe(d, "w11")
        d.add_button(T("Chiudi", "Close"), Gtk.ResponseType.CLOSE)
        g = Gtk.Grid(column_spacing=px(16), row_spacing=px(8))
        g.set_margin_start(px(20))
        g.set_margin_end(px(20))
        g.set_margin_top(px(16))
        g.set_margin_bottom(px(12))
        for i, (k, v) in enumerate(righe):
            g.attach(C.etichetta(k, "tenue"), 0, i, 1, 1)
            g.attach(C.etichetta(v or "—", larga=48), 1, i, 1, 1)
        d.get_content_area().add(g)
        d.show_all()
        d.run()
        d.destroy()

    def chiudi(self, *_):
        self.ricorda_posizione()
        self.motore.ferma()
        if self.popup:
            self.popup.destroy()
        return False

    # ======================================================================================== video
    def costruisci_raccolta_video(self):
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        testa = Gtk.Box(spacing=px(12))
        testa.set_margin_start(px(24))
        testa.set_margin_end(px(16))
        testa.set_margin_top(px(18))
        testa.set_margin_bottom(px(6))
        self.r_titolo = C.etichetta(T("Raccolta video", "Video library"), "titolo")
        testa.pack_start(self.r_titolo, False, False, 0)
        self.r_conto = C.etichetta("", "tenue")
        testa.pack_start(self.r_conto, False, False, 0)
        self.r_giro = Gtk.Spinner()
        testa.pack_start(self.r_giro, False, False, 0)
        testa.pack_end(C.bottone("view-refresh-symbolic", suggerimento=T("Aggiorna", "Refresh"),
                                 azione=lambda: self.leggi_video()), False, False, 0)
        self.b_tutti = C.bottone("media-playback-start-symbolic", T("Riproduci tutto", "Play all"), cls="accento",
                                 azione=self.suona_tutti_video)
        testa.pack_end(self.b_tutti, False, False, 0)
        vb.pack_start(testa, False, False, 0)
        self.g_video = C.Griglia(self.mini, lato=230, proporzione=9 / 16, righe_testo=2, spazio=14)
        self.g_video.attiva = lambda e: self.suona_video(e)
        self.griglie = [self.g_video]
        vb.pack_start(self.g_video, True, True, 0)
        self.pagine.add_named(vb, "raccolta")

    def leggi_video(self):
        self.r_giro.start()

        def lavoro():
            out = []
            for i, (nome, d, prof) in enumerate(self.cartelle):
                for p, mt, dim in L.scansiona(d, L.VIDEO, prof):
                    out.append({"percorso": p, "mtime": mt, "tipo": "video", "cartella": i, "data": L.data_da_nome(p, mt),
                                "titolo": os.path.splitext(os.path.basename(p))[0], "sotto": nome})
            GLib.idle_add(fatto, out)

        def fatto(out):
            self.elementi_video = out
            self.r_giro.stop()
            self.riempi_raccolta()
            return False
        threading.Thread(target=lavoro, daemon=True).start()

    def mostra_raccolta(self):
        if VIDEO:
            self.uscita_schermo()
            self.pagine.set_visible_child_name("raccolta")
            self.nav.segna(self.sezione)
            if not self.elementi_video:
                self.leggi_video()
            else:
                self.riempi_raccolta()
        else:
            self.pagine.set_visible_child_name("raccolta")
            self.nav.segna("raccolta")
            self.riempi_raccolta()

    def video_sezione(self):
        el = self.elementi_video
        if self.sezione.startswith("cartella:"):
            i = int(self.sezione.split(":")[1])
            el = [e for e in el if e["cartella"] == i]
        t = getattr(self, "testo_cerca", "")
        return [e for e in el if L.cerca(t, e["titolo"], e["sotto"])]

    def riempi_raccolta(self):
        if not VIDEO:
            self.riempi_musica()
            return
        el = self.video_sezione()
        if self.sezione.startswith("cartella:"):
            self.r_titolo.set_text(self.cartelle[int(self.sezione.split(":")[1])][0])
        else:
            self.r_titolo.set_text(T("Raccolta video", "Video library"))
        self.r_conto.set_text(L.elementi(len(el)) if el else "")
        self.b_tutti.set_sensitive(bool(el))
        self.g_video.vuoto = T("Nessun video. I video delle cartelle Video e Fotocamera compaiono qui.",
                               "No videos. Videos in your Videos and Camera folders show up here.")
        self.gruppi_video = L.per_mese(el)
        self.g_video.metti(self.gruppi_video)

    def suona_video(self, e):
        elenco = [x["percorso"] for _, g in self.gruppi_video for x in g]
        self.continua = False
        self.coda.imposta(elenco, elenco.index(e["percorso"]))
        self.suona_corrente()

    def suona_tutti_video(self):
        elenco = [x["percorso"] for _, g in getattr(self, "gruppi_video", []) for x in g]
        if elenco:
            self.continua = True
            self.coda.imposta(elenco, 0)
            self.suona_corrente()

    def costruisci_schermo(self):
        """la pagina del video: l'area nera (finestra nativa per glimagesink, o disegno con cairo)."""
        self.v_sovra = Gtk.Box()
        self.v_area = C.classe(Gtk.DrawingArea(), "nero")
        self.v_area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                               Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK |
                               Gdk.EventMask.STRUCTURE_MASK)
        self.v_area.connect("map-event", lambda *_: self.mappa("area", True))
        self.v_area.connect("unmap-event", lambda *_: self.mappa("area", False))
        self.v_area.connect("realize", self.area_pronta)
        self.v_area.connect("draw", self.disegna_video)
        self.v_area.connect("size-allocate", self.area_misura)
        self.v_area.connect("button-press-event", self.clic_video)
        self.v_area.connect("motion-notify-event", lambda *_: self.mostra_controlli())
        self.v_sovra.pack_start(self.v_area, True, True, 0)
        self.pagine.add_named(self.v_sovra, "schermo")
        self.clic_attesa = None

    def area_pronta(self, w):
        if not self.motore.cairo:
            win = w.get_window()
            win.ensure_native()
            self.motore.xid = win.get_xid()
            w.set_double_buffered(False)

    def uscita_cambiata(self):
        """il motore è passato al disegno con cairo: l'area torna normale."""
        self.v_area.set_double_buffered(True)
        al = self.v_area.get_allocation()
        self.motore.misura_uscita(al.width, al.height)
        self.v_area.queue_draw()

    def area_misura(self, w, al):
        if self.motore.cairo:
            self.motore.misura_uscita(al.width, al.height)
        elif self.motore.sink is not None and not self.motore.suona_ora():
            GLib.idle_add(lambda: self.motore.sink.expose() and False)

    def ridisegna_video(self):
        self.v_area.queue_draw()
        return False

    def disegna_video(self, w, cr):
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

    def clic_video(self, w, ev):
        """un clic: pausa (dopo un attimo, per distinguerlo dal doppio clic); doppio clic: schermo intero."""
        if ev.button != 1:
            return False
        if ev.type == Gdk.EventType._2BUTTON_PRESS:
            if self.clic_attesa:
                GLib.source_remove(self.clic_attesa)
                self.clic_attesa = None
            self.cambia_intero()
            return True
        if ev.type == Gdk.EventType.BUTTON_PRESS:
            if self.clic_attesa:
                GLib.source_remove(self.clic_attesa)

            def uno():
                self.clic_attesa = None
                self.alterna()
                return False
            self.clic_attesa = GLib.timeout_add(260, uno)
        return True

    def mostra_schermo(self):
        if self.pagine.get_visible_child_name() != "schermo":
            self.nav.hide()
            C.classe(self.strato, "pieno")
            self.pagine.set_transition_type(Gtk.StackTransitionType.NONE)
            self.pagine.set_visible_child_name("schermo")
            self.pagine.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
            self.barra_visibile()

    def uscita_schermo(self):
        """dal video alla raccolta: il video si ferma (la posizione resta per la prossima volta)."""
        if self.pagine.get_visible_child_name() == "schermo":
            self.ricorda_posizione()
            self.motore.ferma()
            if self.schermo_intero:
                self.unfullscreen()
            self.nav.show()
            self.strato.get_style_context().remove_class("pieno")
            self.mostra_meta()
            self.aggiorna_controlli()
        self.barra_visibile()

    def barra_visibile(self):
        """video: la barra dei comandi solo mentre si guarda un video."""
        if VIDEO:
            self.box_barra.set_visible(self.pagine.get_visible_child_name() == "schermo" and not self.schermo_intero)

    # --- schermo intero: la barra sopra il video, in una finestrella a parte (glimagesink disegna in una
    # finestra X sua, sopra la quale un widget GTK non si vedrebbe) ---
    def cambia_intero(self):
        if self.schermo_intero:
            self.unfullscreen()
        elif VIDEO and self.pagine.get_visible_child_name() == "schermo":
            self.fullscreen()

    def stato_finestra(self, w, ev):
        intero = bool(ev.new_window_state & Gdk.WindowState.FULLSCREEN)
        if intero == self.schermo_intero:
            return False
        self.schermo_intero = intero
        C.metti_icona(self.b_intero, "view-restore-symbolic" if intero else "view-fullscreen-symbolic")
        self.b_intero.set_tooltip_text(T("Esci da schermo intero (Esc)", "Exit full screen (Esc)") if intero
                                       else T("Schermo intero (F11)", "Full screen (F11)"))
        if intero:
            self.box_barra.remove(self.barra)
            self.popup = self.popup or self.crea_popup()
            self.popup.add(self.barra)
            C.classe(self.barra, "sopra-video")
            self.v_indietro.hide()
            self.mostra_controlli()
        else:
            if self.popup and self.barra.get_parent() is self.popup:
                self.popup.remove(self.barra)
                self.popup.hide()
            self.barra.get_style_context().remove_class("sopra-video")
            self.box_barra.pack_start(self.barra, False, False, 0)
            self.v_indietro.show()
            if self.v_area.get_window():
                self.v_area.get_window().set_cursor(None)
        self.barra_visibile()
        return False

    def crea_popup(self):
        p = Gtk.Window(type=Gtk.WindowType.POPUP)
        C.classe(p, "w11")
        scr = p.get_screen()
        if scr.is_composited() and scr.get_rgba_visual():
            p.set_visual(scr.get_rgba_visual())
            p.set_app_paintable(True)
        p.set_transient_for(self)
        return p

    def posiziona_popup(self):
        w = self.get_window()
        if not w or not self.popup:
            return
        mon = self.get_display().get_monitor_at_window(w)
        g = mon.get_geometry()
        larga = min(g.width - px(48), px(1100))
        self.barra.set_size_request(larga, -1)
        _, nat = self.barra.get_preferred_height()
        self.popup.resize(larga, nat)
        self.popup.move(g.x + (g.width - larga) // 2, g.y + g.height - nat - px(24))

    def mostra_controlli(self):
        """mouse mosso o pausa: comandi visibili; in riproduzione a schermo intero spariscono dopo 2,5 s."""
        if not (VIDEO and self.schermo_intero):
            return
        if self.popup and not self.popup.get_visible():
            self.posiziona_popup()
            self.popup.show_all()
        if self.popup and self.popup.get_window():
            self.popup.get_window().raise_()
        if self.v_area.get_window():
            self.v_area.get_window().set_cursor(None)
        if self._t_nascondi:
            GLib.source_remove(self._t_nascondi)
        self._t_nascondi = GLib.timeout_add(2500, self.nascondi_controlli)

    def nascondi_controlli(self, forza=False):
        self._t_nascondi = None
        if not (VIDEO and self.schermo_intero and self.popup):
            return False
        if not forza:
            if not self.motore.suona_ora():
                return False
            # il puntatore sopra la barra: resta
            seat = self.get_display().get_default_seat()
            _s, x, y = seat.get_pointer().get_position()
            pw = self.popup.get_window()
            if pw:
                ox, oy = pw.get_origin()[1:]
                if ox <= x <= ox + pw.get_width() and oy <= y <= oy + pw.get_height():
                    self._t_nascondi = GLib.timeout_add(1000, self.nascondi_controlli)
                    return False
        self.popup.hide()
        if self.v_area.get_window():
            self.v_area.get_window().set_cursor(Gdk.Cursor.new_from_name(self.get_display(), "none"))
        return False

    def guarda_puntatore(self):
        """a schermo intero il video (finestra di glimagesink) può non passare i movimenti: si guarda il puntatore."""
        seat = self.get_display().get_default_seat()
        _s, x, y = seat.get_pointer().get_position()
        if self.ult_puntatore and (x, y) != self.ult_puntatore and self.is_active():
            self.mostra_controlli()
        self.ult_puntatore = (x, y)

    # ======================================================================================== coda
    def costruisci_coda(self):
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        testa = Gtk.Box(spacing=px(12))
        testa.set_margin_start(px(24))
        testa.set_margin_end(px(16))
        testa.set_margin_top(px(18))
        testa.set_margin_bottom(px(10))
        testa.pack_start(C.etichetta(T("Coda di riproduzione", "Play queue"), "titolo"), False, False, 0)
        self.c_conto = C.etichetta("", "tenue")
        testa.pack_start(self.c_conto, False, False, 0)
        testa.pack_end(C.bottone("edit-clear-all-symbolic", T("Cancella", "Clear"), cls="lieve",
                                 azione=self.svuota_coda), False, False, 0)
        vb.pack_start(testa, False, False, 0)
        # icona, titolo, artista, album, durata, indice
        self.m_coda = Gtk.ListStore(str, str, str, str, str, int, int)
        tv = Gtk.TreeView(model=self.m_coda, headers_visible=False, enable_search=False)
        tv.set_activate_on_single_click(False)
        r = Gtk.CellRendererPixbuf()
        c = Gtk.TreeViewColumn("", r, icon_name=0)
        c.set_min_width(px(36))
        tv.append_column(c)
        for i, (titolo, espandi) in enumerate(((T("Titolo", "Title"), True), (T("Artista", "Artist"), True),
                                               (T("Album", "Album"), True), (T("Durata", "Length"), False)), 1):
            r = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END, ypad=px(9), xpad=px(8))
            if i == 4:
                r.set_property("xalign", 1.0)
            c = Gtk.TreeViewColumn(titolo, r, text=i, weight=6)
            c.set_expand(espandi)
            if VIDEO and i in (2, 3):
                c.set_visible(False)
            tv.append_column(c)
        tv.connect("row-activated", lambda t, path, col: self.vai_coda(self.m_coda[path][5]))
        tv.connect("button-press-event", self.menu_coda)
        self.tv_coda = tv
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_margin_start(px(14))
        sc.set_margin_end(px(14))
        sc.add(tv)
        vb.pack_start(sc, True, True, 0)
        self.c_vuoto = C.etichetta(T("La coda è vuota. Riproduci un brano o un album dalla raccolta.",
                                     "The queue is empty. Play a song or an album from your library.") if not VIDEO else
                                   T("La coda è vuota.", "The queue is empty."), "sotto", xalign=0.5)
        self.c_vuoto.set_margin_top(px(40))
        vb.pack_start(self.c_vuoto, False, False, 0)
        self.pagine.add_named(vb, "coda")

    def mostra_coda(self):
        if VIDEO:
            self.uscita_schermo()
        self.pagine.set_visible_child_name("coda")
        self.nav.segna("coda")
        self.riempi_coda()

    def riempi_coda(self):
        if not hasattr(self, "m_coda") or self.pagine.get_visible_child_name() != "coda":
            return
        self.m_coda.clear()
        ordine = self.coda.ordine if self.coda.casuale else range(len(self.coda.elementi))
        for i in ordine:
            p = self.coda.elementi[i]
            cor = i == self.coda.indice
            if VIDEO:
                titolo, art, alb, dur = os.path.splitext(os.path.basename(p))[0], os.path.basename(os.path.dirname(p)), "", ""
                d = self.mini.durate.get(p)
                dur = L.tempo(d) if d else ""
            else:
                b = self.raccolta.brano(p)
                titolo, art, alb, dur = b["titolo"], L.artista_di(b), L.album_di(b), L.tempo(b.get("durata"))
            self.m_coda.append(["media-playback-start-symbolic" if cor else "", titolo, art, alb, dur, i,
                                Pango.Weight.BOLD if cor else Pango.Weight.NORMAL])
        n = len(self.coda)
        self.c_conto.set_text(L.elementi(n) if n else "")
        self.c_vuoto.set_visible(n == 0)
        self.tv_coda.get_parent().set_visible(n > 0)

    def vai_coda(self, i):
        self.coda.vai(i)
        self.continua = True
        self.suona_corrente()

    def menu_coda(self, tv, ev):
        if ev.button != 3:
            return False
        r = tv.get_path_at_pos(int(ev.x), int(ev.y))
        if not r:
            return False
        i = self.m_coda[r[0]][5]
        m = C.classe(Gtk.Menu(), "w11")
        for testo, az in ((T("Riproduci", "Play"), lambda: self.vai_coda(i)),
                          (T("Togli dalla coda", "Remove from queue"), lambda: self.togli_coda(i))):
            it = Gtk.MenuItem(label=testo)
            it.connect("activate", lambda _i, az=az: az())
            m.append(it)
        m.show_all()
        m.attach_to_widget(tv, None)
        m.popup_at_pointer(ev)
        return True

    def togli_coda(self, i):
        era = i == self.coda.indice
        self.coda.togli(i)
        if era:
            if self.coda.corrente():
                self.suona_corrente()
            else:
                self.motore.ferma()
                self.mostra_meta()
        self.riempi_coda()
        self.aggiorna_controlli()

    def svuota_coda(self):
        self.motore.ferma()
        self.coda.svuota()
        self.mostra_meta()
        self.riempi_coda()
        self.aggiorna_controlli()
        self.cursore.set_value(0)
        self.t_ora.set_text("0:00")
        self.t_tot.set_text("0:00")
        if not VIDEO:
            self.metti_copertina(None)

    # ======================================================================================== musica
    def costruisci_musica(self):
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        testa = Gtk.Box(spacing=px(12))
        testa.set_margin_start(px(24))
        testa.set_margin_end(px(16))
        testa.set_margin_top(px(18))
        testa.pack_start(C.etichetta(T("Raccolta musicale", "Music library"), "titolo"), False, False, 0)
        self.r_giro = Gtk.Spinner()
        testa.pack_start(self.r_giro, False, False, 0)
        self.r_stato = C.etichetta("", "tenue")
        testa.pack_start(self.r_stato, False, False, 0)
        testa.pack_end(C.bottone("view-refresh-symbolic", suggerimento=T("Aggiorna la raccolta", "Refresh library"),
                                 azione=self.carica_raccolta), False, False, 0)
        vb.pack_start(testa, False, False, 0)
        riga = Gtk.Box(spacing=px(12))
        riga.set_margin_start(px(24))
        riga.set_margin_end(px(16))
        riga.set_margin_top(px(10))
        riga.set_margin_bottom(px(8))
        self.schede = C.Schede([("brani", T("Brani", "Songs")), ("album", T("Album", "Albums")),
                                ("artisti", T("Artisti", "Artists"))], self.scheda_scelta)
        riga.pack_start(self.schede, False, False, 0)
        self.b_casuale_tutto = C.bottone_disegno("casuale", testo=T("Riproduzione casuale", "Shuffle and play"),
                                                 cls="accento", azione=lambda: self.suona_tutto(casuale=True))
        riga.pack_end(self.b_casuale_tutto, False, False, 0)
        self.r_conto = C.etichetta("", "tenue")
        riga.pack_end(self.r_conto, False, False, px(8))
        vb.pack_start(riga, False, False, 0)
        self.sotto_pagine = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=100)
        vb.pack_start(self.sotto_pagine, True, True, 0)
        # brani: titolo, artista, album, anno, genere, durata, percorso, peso
        self.m_brani = Gtk.ListStore(str, str, str, str, str, str, str, int)
        tv = Gtk.TreeView(model=self.m_brani, enable_search=False)
        tv.set_activate_on_single_click(False)
        for i, (titolo, espandi) in enumerate(((T("Titolo", "Title"), True), (T("Artista", "Artist"), True),
                                               (T("Album", "Album"), True), (T("Anno", "Year"), False),
                                               (T("Genere", "Genre"), False), (T("Durata", "Length"), False))):
            r = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END, ypad=px(9), xpad=px(8))
            if i == 5:
                r.set_property("xalign", 1.0)
            c = Gtk.TreeViewColumn(titolo, r, text=i, weight=7)
            c.set_expand(espandi)
            c.set_resizable(espandi)
            c.set_sort_column_id(i)
            tv.append_column(c)
        tv.connect("row-activated", lambda t, path, col: self.suona_da_brani(path))
        tv.connect("button-press-event", self.menu_brano)
        self.tv_brani = tv
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_margin_start(px(14))
        sc.set_margin_end(px(14))
        sc.add(tv)
        self.sotto_pagine.add_named(sc, "brani")
        self.g_album = C.Griglia(self.mini, lato=170, proporzione=1.0, righe_testo=2, spazio=16)
        self.g_album.attiva = lambda e: self.mostra_album(e["chiave"])
        self.sotto_pagine.add_named(self.g_album, "album")
        self.g_artisti = C.Griglia(self.mini, lato=150, proporzione=1.0, righe_testo=2, rotonda=True, spazio=16)
        self.g_artisti.attiva = lambda e: self.mostra_artista(e["chiave"])
        self.sotto_pagine.add_named(self.g_artisti, "artisti")
        self.griglie = [self.g_album, self.g_artisti]
        self.r_vuoto = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(10), valign=Gtk.Align.CENTER)
        self.r_vuoto.pack_start(C.etichetta(T("La raccolta è vuota", "Your library is empty"), "vuoto-grande", xalign=0.5),
                                False, False, 0)
        self.r_vuoto.pack_start(C.etichetta(T("Metti la musica nella cartella Musica: compare qui da sola.",
                                              "Put music in your Music folder and it shows up here."), "sotto", xalign=0.5),
                                False, False, 0)
        self.sotto_pagine.add_named(self.r_vuoto, "vuoto")
        self.pagine.add_named(vb, "raccolta")
        self.pagine.add_named(self.costruisci_dettaglio(), "dettaglio")
        self.costruisci_coda()
        self.pagine.add_named(self.costruisci_ora(), "ora")
        self.schede.scegli(self.imp["scheda"] if self.imp["scheda"] in ("brani", "album", "artisti") else "brani",
                           avvisa=False)
        self.sotto_pagine.set_visible_child_name(self.schede.scelta)

    def scheda_scelta(self, k):
        scrivi_imp({"scheda": k})
        self.riempi_musica()

    def carica_raccolta(self):
        self.r_giro.start()
        self.r_stato.set_text(T("Sto leggendo la raccolta…", "Reading your library…"))

        def avanzamento(i, n, brani):
            self.r_stato.set_text(T("Sto leggendo la raccolta… %d di %d", "Reading your library… %d of %d") % (i, n))
            self.raccolta.brani = brani
            self.riempi_musica()
            return False

        def fatto():
            self.r_giro.stop()
            self.r_stato.set_text("")
            self.riempi_musica()
            self.mostra_meta()
        self.raccolta.aggiorna(avanzamento, fatto)
        self.riempi_musica()

    def brani_filtrati(self):
        t = getattr(self, "testo_cerca", "")
        return {p: b for p, b in self.raccolta.brani.items()
                if L.cerca(t, b.get("titolo"), b.get("artista"), b.get("album"), b.get("artista_album"))}

    def riempi_musica(self):
        if VIDEO:
            return
        brani = self.brani_filtrati()
        scheda = self.schede.scelta
        self.b_casuale_tutto.set_sensitive(bool(brani))
        if not self.raccolta.brani and not self.raccolta.lettura:
            self.sotto_pagine.set_visible_child_name("vuoto")
            self.r_conto.set_text("")
            return
        self.sotto_pagine.set_visible_child_name(scheda)
        if scheda == "brani":
            self.r_conto.set_text(L.brani(len(brani)))
            self.m_brani.clear()
            cor = self.coda.corrente()
            ordinati = sorted(brani.items(), key=lambda kv: (not (kv[1].get("artista_album") or kv[1].get("artista")),
                                                              L.chiave_ordine(L.artista_di(kv[1])),
                                                              L.chiave_ordine(L.album_di(kv[1])),
                                                              kv[1].get("disco") or 0, kv[1].get("traccia") or 0,
                                                              L.chiave_naturale(kv[1]["titolo"])))
            for p, b in ordinati:
                self.m_brani.append([b["titolo"], L.artista_di(b), L.album_di(b), str(b["anno"]) if b.get("anno") else "",
                                     b.get("genere") or "", L.tempo(b.get("durata")), p,
                                     Pango.Weight.BOLD if p == cor else Pango.Weight.NORMAL])
        elif scheda == "album":
            album = L.raggruppa_album(brani)
            self.r_conto.set_text(T("%s album", "%s albums") % L.numero(len(album)))
            el = []
            for a in sorted(album.values(), key=lambda a: (a["titolo"] == L.album_di({}), L.chiave_ordine(a["titolo"]),
                                                           L.chiave_ordine(a["artista"]))):
                el.append(self.elemento_copertina(a["copertina"], a["titolo"], a["artista"], a["chiave"], "media-optical-symbolic"))
            self.album = album
            self.g_album.metti([(None, el)], in_cima=False)
        else:
            art = L.raggruppa_artisti(brani)
            self.r_conto.set_text(T("%s artisti", "%s artists") % L.numero(len(art)))
            el = []
            for k, a in sorted(art.items(), key=lambda kv: (kv[1]["nome"] == L.artista_di({}), L.chiave_ordine(kv[1]["nome"]))):
                n = len(a["album"])
                e = self.elemento_copertina(a["copertina"], a["nome"],
                                            T("%d album", "%d albums") % n if n != 1 else T("1 album", "1 album"), k, None)
                if not a["copertina"]:
                    e["lettere"] = "".join(w[0] for w in a["nome"].split()[:2]).upper()
                el.append(e)
            self.artisti = art
            self.g_artisti.metti([(None, el)], in_cima=False)

    def elemento_copertina(self, cop, titolo, sotto, chiave, icona):
        try:
            mt = os.path.getmtime(cop) if cop else 0
        except OSError:
            mt, cop = 0, ""
        return {"percorso": cop or ("nessuna:" + str(chiave)), "mtime": mt, "tipo": "immagine" if cop else "nessuna",
                "titolo": titolo, "sotto": sotto, "chiave": chiave, "icona": icona or "avatar-default-symbolic"}

    def ordinati_brani(self):
        return [r[6] for r in self.m_brani]

    def suona_da_brani(self, path):
        elenco = self.ordinati_brani()
        i = path.get_indices()[0]
        self.suona_elenco(elenco, i)

    def suona_elenco(self, elenco, i=0, casuale=None):
        if not elenco:
            return
        self.continua = True
        if casuale is not None:
            self.coda.casuale = casuale
            scrivi_imp({"casuale": casuale})
        if casuale:
            i = L.random.randrange(len(elenco))
        self.coda.imposta(elenco, i)
        self.suona_corrente()

    def suona_tutto(self, casuale=False):
        brani = self.brani_filtrati()
        elenco = sorted(brani, key=lambda p: (L.chiave_ordine(L.artista_di(brani[p])), L.chiave_ordine(L.album_di(brani[p])),
                                              brani[p].get("disco") or 0, brani[p].get("traccia") or 0))
        self.suona_elenco(elenco, 0, casuale=casuale)

    def menu_brano(self, tv, ev):
        if ev.button != 3:
            return False
        r = tv.get_path_at_pos(int(ev.x), int(ev.y))
        if not r:
            return False
        tv.get_selection().select_path(r[0])
        p = self.m_brani[r[0]][6]
        self.menu_su([p], ev, tv)
        return True

    def menu_su(self, percorsi, ev, w):
        m = C.classe(Gtk.Menu(), "w11")
        for testo, az in ((T("Riproduci", "Play"), lambda: self.suona_elenco(percorsi, 0)),
                          (T("Riproduci dopo", "Play next"), lambda: self.in_coda(percorsi, True)),
                          (T("Aggiungi alla coda", "Add to queue"), lambda: self.in_coda(percorsi, False)),
                          (T("Mostra nella cartella", "Show in folder"), lambda: C.mostra_nella_cartella(percorsi[0]))):
            it = Gtk.MenuItem(label=testo)
            it.connect("activate", lambda _i, az=az: az())
            m.append(it)
        m.show_all()
        m.attach_to_widget(w, None)
        m.popup_at_pointer(ev)

    def in_coda(self, percorsi, subito):
        vuota = not len(self.coda)
        self.coda.aggiungi(percorsi, subito_dopo=subito)
        self.continua = True
        if vuota:
            self.suona_corrente()
        self.riempi_coda()
        self.aggiorna_controlli()
        self.avviso(T("Aggiunto alla coda", "Added to queue"), secondi=2)

    # --- album e artista ---
    def costruisci_dettaglio(self):
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.dettaglio = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(14))
        self.dettaglio.set_margin_start(px(28))
        self.dettaglio.set_margin_end(px(28))
        self.dettaglio.set_margin_top(px(16))
        self.dettaglio.set_margin_bottom(px(24))
        sc.add(self.dettaglio)
        return sc

    def pixbuf_copertina(self, percorso, lato):
        if not percorso:
            return None
        try:
            return GdkPixbuf.Pixbuf.new_from_file_at_scale(percorso, lato, lato, True)
        except GLib.Error:
            return None

    def immagine_copertina(self, percorso, lato, tonda=False):
        pb = self.pixbuf_copertina(percorso, lato)
        a = Gtk.DrawingArea()
        a.set_size_request(lato, lato)
        col = self.colori

        def disegna(w, cr):
            import math
            if tonda:
                cr.arc(lato / 2, lato / 2, lato / 2, 0, 2 * math.pi)
            else:
                C.rettangolo_tondo(cr, 0, 0, lato, lato, px(8))
            cr.clip()
            if pb is not None:
                C.pixbuf_coprente(cr, pb, 0, 0, lato, lato)
            else:
                cr.set_source_rgb(*C.rgb(col["CARTASOPRA"]))
                cr.paint()
                ic = C.pixbuf_icona("avatar-default-symbolic" if tonda else "media-optical-symbolic", int(lato / 3),
                                    col["TENUE"])
                if ic:
                    Gdk.cairo_set_source_pixbuf(cr, ic, (lato - ic.get_width()) / 2, (lato - ic.get_height()) / 2)
                    cr.paint()
            return False
        a.connect("draw", disegna)
        a.set_valign(Gtk.Align.START)
        return a

    def svuota_dettaglio(self):
        for f in self.dettaglio.get_children():
            self.dettaglio.remove(f)
        indietro = C.bottone("go-previous-symbolic", T("Raccolta musicale", "Music library"), cls="lieve",
                             azione=self.mostra_raccolta)
        indietro.set_halign(Gtk.Align.START)
        self.dettaglio.pack_start(indietro, False, False, 0)

    def mostra_album(self, chiave):
        a = getattr(self, "album", {}).get(chiave) or L.raggruppa_album(self.raccolta.brani).get(chiave)
        if not a:
            return
        self.svuota_dettaglio()
        testa = Gtk.Box(spacing=px(24))
        testa.pack_start(self.immagine_copertina(a["copertina"], px(200)), False, False, 0)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(6), valign=Gtk.Align.END)
        info.pack_start(C.etichetta(a["titolo"], "grande", righe=2, ellissi=True), False, False, 0)
        info.pack_start(C.etichetta(a["artista"], "titolo2"), False, False, 0)
        dur = sum(self.raccolta.brano(p).get("durata") or 0 for p in a["brani"])
        info.pack_start(C.etichetta(" · ".join(x for x in (str(a["anno"]) if a["anno"] else "", L.brani(len(a["brani"])),
                                                            L.tempo(dur)) if x), "sotto"), False, False, 0)
        pul = Gtk.Box(spacing=px(8))
        pul.set_margin_top(px(10))
        pul.pack_start(C.bottone("media-playback-start-symbolic", T("Riproduci tutto", "Play all"), cls="accento",
                                 azione=lambda: self.suona_elenco(a["brani"], 0, casuale=False)), False, False, 0)
        pul.pack_start(C.bottone_disegno("casuale", testo=T("Casuale", "Shuffle"), cls=None,
                                         azione=lambda: self.suona_elenco(a["brani"], 0, casuale=True)), False, False, 0)
        pul.pack_start(C.bottone("list-add-symbolic", T("Aggiungi alla coda", "Add to queue"), cls=None,
                                 azione=lambda: self.in_coda(a["brani"], False)), False, False, 0)
        info.pack_start(pul, False, False, 0)
        testa.pack_start(info, True, True, 0)
        self.dettaglio.pack_start(testa, False, False, 0)
        self.dettaglio.pack_start(self.elenco_brani(a["brani"], numeri=True), False, False, 0)
        self.dettaglio.show_all()
        self.pagine.set_visible_child_name("dettaglio")

    def mostra_artista(self, chiave):
        art = getattr(self, "artisti", {}).get(chiave) or L.raggruppa_artisti(self.raccolta.brani).get(chiave)
        if not art:
            return
        self.svuota_dettaglio()
        brani_d = {p: self.raccolta.brano(p) for p in art["brani"]}
        album = sorted(L.raggruppa_album(brani_d).values(), key=lambda a: -(a["anno"] or 0))
        ordinati = [p for a in album for p in a["brani"]]
        testa = Gtk.Box(spacing=px(24))
        testa.pack_start(self.immagine_copertina(art["copertina"], px(160), tonda=True), False, False, 0)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(6), valign=Gtk.Align.CENTER)
        info.pack_start(C.etichetta(art["nome"], "grande"), False, False, 0)
        info.pack_start(C.etichetta("%s · %s" % (T("%d album", "%d albums") % len(album), L.brani(len(ordinati))), "sotto"),
                        False, False, 0)
        pul = Gtk.Box(spacing=px(8))
        pul.set_margin_top(px(10))
        pul.pack_start(C.bottone("media-playback-start-symbolic", T("Riproduci tutto", "Play all"), cls="accento",
                                 azione=lambda: self.suona_elenco(ordinati, 0, casuale=False)), False, False, 0)
        pul.pack_start(C.bottone_disegno("casuale", testo=T("Casuale", "Shuffle"), cls=None,
                                         azione=lambda: self.suona_elenco(ordinati, 0, casuale=True)), False, False, 0)
        info.pack_start(pul, False, False, 0)
        testa.pack_start(info, True, True, 0)
        self.dettaglio.pack_start(testa, False, False, 0)
        for a in album:
            self.dettaglio.pack_start(C.etichetta("%s%s" % (a["titolo"], " (%d)" % a["anno"] if a["anno"] else ""),
                                                  "titolo2"), False, False, px(4))
            self.dettaglio.pack_start(self.elenco_brani(a["brani"], numeri=True), False, False, 0)
        self.dettaglio.show_all()
        self.pagine.set_visible_child_name("dettaglio")

    def elenco_brani(self, percorsi, numeri=False):
        lb = C.classe(Gtk.ListBox(), "scheda-coda")
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        cor = self.coda.corrente()
        for i, p in enumerate(percorsi):
            b = self.raccolta.brano(p)
            r = Gtk.ListBoxRow()
            hb = Gtk.Box(spacing=px(14))
            hb.set_margin_top(px(8))
            hb.set_margin_bottom(px(8))
            n = C.etichetta(str(b.get("traccia") or i + 1) if numeri else "", "tenue numeri", xalign=1.0, ellissi=False)
            n.set_width_chars(3)
            hb.pack_start(n, False, False, 0)
            t = C.etichetta(b["titolo"], "in-corso" if p == cor else None)
            hb.pack_start(t, True, True, 0)
            hb.pack_start(C.etichetta(L.artista_di(b), "sotto", larga=24), False, False, 0)
            hb.pack_start(C.etichetta(L.tempo(b.get("durata")), "tenue numeri", xalign=1.0, ellissi=False), False, False, 0)
            r.add(hb)
            r.percorso = p
            lb.add(r)
        lb.connect("row-activated", lambda _l, r: self.suona_elenco(percorsi, percorsi.index(r.percorso)))
        lb.set_activate_on_single_click(False)

        def tasto_dx(w, ev):
            if ev.button == 3:
                r = lb.get_row_at_y(int(ev.y))
                if r:
                    self.menu_su([r.percorso], ev, lb)
                    return True
            return False
        lb.connect("button-press-event", tasto_dx)
        return lb

    # --- Riproduzione in corso ---
    def costruisci_ora(self):
        sov = Gtk.Overlay()
        self.sfondo_ora = Gtk.DrawingArea()
        self.sfondo_ora.connect("draw", self.disegna_sfondo_ora)
        sov.add(self.sfondo_ora)
        hb = Gtk.Box(spacing=px(36))
        hb.set_margin_start(px(48))
        hb.set_margin_end(px(48))
        hb.set_margin_top(px(36))
        hb.set_margin_bottom(px(28))
        self.ora_cop = Gtk.DrawingArea()
        self.ora_cop.connect("draw", self.disegna_copertina_ora)
        self.ora_cop.set_size_request(px(220), px(220))
        hb.pack_start(self.ora_cop, True, True, 0)
        dx = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(8))
        dx.set_size_request(px(340), -1)
        dx.set_valign(Gtk.Align.CENTER)
        self.ora_titolo = C.etichetta("", "ora-titolo", righe=2)
        self.ora_artista = C.etichetta("", "ora-artista")
        self.ora_album = C.etichetta("", "sotto")
        for w in (self.ora_titolo, self.ora_artista, self.ora_album):
            dx.pack_start(w, False, False, 0)
        succ = C.etichetta(T("Successivi", "Up next"), "titolo2")
        succ.set_margin_top(px(26))
        self.ora_succ = succ
        dx.pack_start(succ, False, False, 0)
        self.ora_lista = C.classe(Gtk.ListBox(), "scheda-coda")
        self.ora_lista.set_selection_mode(Gtk.SelectionMode.NONE)
        self.ora_lista.connect("row-activated", lambda _l, r: self.vai_coda(r.indice))
        dx.pack_start(self.ora_lista, False, False, 0)
        hb.pack_start(dx, False, False, 0)
        sov.add_overlay(hb)
        self.pb_ora = None
        return sov

    def mostra_ora(self):
        self.pagine.set_visible_child_name("ora")
        self.nav.segna("ora")
        self.riempi_ora()

    def riempi_ora(self):
        p = self.coda.corrente()
        b = self.raccolta.brano(p) if p else None
        self.ora_titolo.set_text(b["titolo"] if b else T("Niente in riproduzione", "Nothing playing"))
        self.ora_artista.set_text(L.artista_di(b) if b else "")
        self.ora_album.set_text(" · ".join(x for x in (L.album_di(b), str(b["anno"]) if b.get("anno") else "") if x)
                                if b else "")
        self.pb_ora = self.pixbuf_copertina(b.get("copertina") if b else None, px(600))
        self.medio_ora = C.colore_medio(self.pb_ora) if self.pb_ora else None
        for f in self.ora_lista.get_children():
            self.ora_lista.remove(f)
        for i in self.coda.successivi(6):
            bb = self.raccolta.brano(self.coda.elementi[i])
            r = Gtk.ListBoxRow()
            r.indice = i
            hb = Gtk.Box(spacing=px(10))
            hb.set_margin_top(px(6))
            hb.set_margin_bottom(px(6))
            hb.pack_start(C.etichetta(bb["titolo"], larga=30), True, True, 0)
            hb.pack_start(C.etichetta(L.tempo(bb.get("durata")), "tenue numeri", ellissi=False), False, False, 0)
            r.add(hb)
            self.ora_lista.add(r)
        self.ora_lista.show_all()
        vuota = not self.ora_lista.get_children()
        self.ora_lista.set_visible(not vuota)
        self.ora_succ.set_visible(not vuota)
        self.sfondo_ora.queue_draw()
        self.ora_cop.queue_draw()

    def disegna_sfondo_ora(self, w, cr):
        import cairo
        al = w.get_allocation()
        base = C.rgb(self.colori["STRATO"])
        cr.set_source_rgb(*base)
        cr.paint()
        m = getattr(self, "medio_ora", None)
        if m:
            g = cairo.LinearGradient(0, 0, al.width, al.height)
            g.add_color_stop_rgba(0, m[0], m[1], m[2], 0.55 if self.colori["scuro"] else 0.35)
            g.add_color_stop_rgba(1, m[0], m[1], m[2], 0.08)
            cr.set_source(g)
            cr.paint()
        return False

    def disegna_copertina_ora(self, w, cr):
        al = w.get_allocation()
        lato = min(al.width, al.height, px(460))
        x, y = (al.width - lato) / 2, (al.height - lato) / 2
        cr.save()
        C.rettangolo_tondo(cr, x, y, lato, lato, px(10))
        cr.clip()
        if self.pb_ora is not None:
            C.pixbuf_coprente(cr, self.pb_ora, x, y, lato, lato)
        else:
            cr.set_source_rgb(*C.rgb(self.colori["CARTASOPRA"]))
            cr.paint()
            ic = C.pixbuf_icona("audio-x-generic-symbolic", int(lato / 3), self.colori["TENUE"])
            if ic:
                Gdk.cairo_set_source_pixbuf(cr, ic, x + (lato - ic.get_width()) / 2, y + (lato - ic.get_height()) / 2)
                cr.paint()
        cr.restore()
        return False

    def metti_copertina(self, b):
        self.pb_barra = self.pixbuf_copertina(b.get("copertina") if b else None, px(104))
        self.copertina.queue_draw()

    def disegna_copertina_barra(self, w, cr):
        al = w.get_allocation()
        C.rettangolo_tondo(cr, 0, 0, al.width, al.height, px(4))
        cr.clip()
        if self.pb_barra is not None:
            C.pixbuf_coprente(cr, self.pb_barra, 0, 0, al.width, al.height)
        else:
            cr.set_source_rgb(*C.rgb(self.colori["CARTASOPRA"]))
            cr.paint()
            ic = C.pixbuf_icona("audio-x-generic-symbolic", px(24), self.colori["TENUE"])
            if ic:
                Gdk.cairo_set_source_pixbuf(cr, ic, (al.width - ic.get_width()) / 2, (al.height - ic.get_height()) / 2)
                cr.paint()
        return False

    # ======================================================================================== tastiera
    def tasto(self, w, ev):
        k = Gdk.keyval_name(ev.keyval)
        ctrl = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)
        if isinstance(self.get_focus(), Gtk.Entry) and not ctrl and k not in ("Escape", "F11"):
            return False
        schermo = VIDEO and self.pagine.get_visible_child_name() == "schermo"
        if k == "space" and (schermo or not isinstance(self.get_focus(), (Gtk.TreeView, Gtk.ListBox))) or \
                (ctrl and k in ("p", "P")) or k in ("AudioPlay", "XF86AudioPlay"):
            self.alterna()
        elif ctrl and k in ("b", "B") or k in ("AudioPrev", "XF86AudioPrev"):
            self.precedente()
        elif ctrl and k in ("f", "F") or k in ("AudioNext", "XF86AudioNext"):
            self.successivo()
        elif ctrl and k in ("o", "O"):
            self.scegli_file()
        elif k in ("Left", "j", "J") and (schermo or not VIDEO) and not isinstance(self.get_focus(), Gtk.TreeView):
            self.salta(-10 if VIDEO else -5)
        elif k in ("Right", "l", "L") and (schermo or not VIDEO) and not isinstance(self.get_focus(), Gtk.TreeView):
            self.salta(30 if VIDEO else 5)
        elif k == "Up" and schermo:
            self.s_volume.set_value(min(100, self.s_volume.get_value() + 5))
        elif k == "Down" and schermo:
            self.s_volume.set_value(max(0, self.s_volume.get_value() - 5))
        elif k in ("m", "M") and not ctrl:
            self.cambia_muto()
        elif k in ("F11", "f") and schermo:
            self.cambia_intero()
        elif k == "Escape":
            if self.schermo_intero:
                self.unfullscreen()
            elif schermo or self.pagine.get_visible_child_name() == "dettaglio":
                self.mostra_raccolta()
            else:
                return False
        else:
            return False
        return True


if __name__ == "__main__":
    sys.exit(Lettore().run([a for a in sys.argv if a not in ("--video", "--musica")]))
