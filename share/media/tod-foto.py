#!/usr/bin/env python3
"""Foto: il visualizzatore di immagini di Termux On DeX, come l'app Foto di Windows 11 (al posto di Ristretto).

  tod-foto.py [file...]      senza file: la Galleria; con un file: la foto, e le altre della sua cartella

Galleria: barra di navigazione (Galleria, Preferiti, le cartelle: Fotocamera, Screenshot, Immagini, Download), foto
e video a gruppi per mese, dal più recente; la data viene dal nome del file (la fotocamera la scrive lì), se no dal
file. Vista: la foto adattata alla finestra, zoom con la rotella (sul punto del puntatore) o con − / + in basso,
trascinamento, doppio clic 100%/adatta; frecce ai lati, striscia delle miniature, pannello Informazioni (dati di
scatto con GExiv2), Ruota (le JPEG senza ricomprimere: si cambia solo l'orientamento), Elimina (nel Cestino),
Preferito, Presentazione, Schermo intero, Apri con, Mostra nella cartella, Copia, Imposta come sfondo.
I video della fotocamera si aprono nel Lettore multimediale. Gira in Termux (GTK 3).
"""
import os
import subprocess
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comune as C  # noqa: E402
from comune import Gdk, GdkPixbuf, Gio, GLib, Gtk, px, T  # noqa: E402

L = C.L

from visore import GExiv2, Visore, dati_file  # noqa: E402

GLib.set_prgname("tod-foto")   # la barra riconosce la finestra (tod-foto.desktop)
GLib.set_application_name(T("Foto", "Photos"))

PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
TOD = os.environ.get("TOD_MEDIA_COMANDO") or os.path.join(PREFIX, "bin/tod")
GALLERIA = L.IMMAGINI | L.VIDEO
LATI = {"piccole": 110, "medie": 150, "grandi": 220}

CSS = """
.w11 .freccia { background-color: alpha(CARTA, 0.86); border: 1px solid LINEA; border-radius: 999px; padding: 10px; }
.w11 .freccia:hover { background-color: CARTA; }
.w11 .zoom { min-width: 48px; }
.w11 .info-titolo { font-weight: 600; font-size: 11pt; }
.w11 .info-etic { color: TENUE; font-size: 8.5pt; }
.w11 .striscia { background-color: FONDO; }
.w11 .errore { color: TESTO2; }
.w11 button.cuore image { color: #e81123; }
"""


def comando_tod(*args):
    """tod in sottofondo (sfondo): non blocca la finestra."""
    def esegui():
        try:
            subprocess.run(["bash", TOD] + list(args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        except (OSError, subprocess.SubprocessError):
            pass
    threading.Thread(target=esegui, daemon=True).start()


def apri_video(percorsi):
    """i video nel Lettore multimediale (tod-video.desktop), se no direttamente."""
    C.apri_con_app("tod-video.desktop", percorsi, [sys.executable, os.path.join(C.QUI, "tod-lettore.py"), "--video"])


class Striscia(Gtk.DrawingArea):
    """le miniature della cartella sotto la foto (come la striscia di Foto), quella aperta al centro."""

    def __init__(self, finestra):
        super().__init__()
        self.fin = finestra
        self.set_size_request(-1, px(72))
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.SCROLL_MASK |
                        Gdk.EventMask.SMOOTH_SCROLL_MASK | Gdk.EventMask.POINTER_MOTION_MASK |
                        Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("draw", self.disegna)
        self.connect("button-press-event", self.premi)
        self.connect("scroll-event", self.rotella)
        self.connect("motion-notify-event", self.moto)
        self.connect("leave-notify-event", lambda *_: self._sopra(-1))
        self.elenco, self.indice, self.centro, self.sopra = [], 0, 0.0, -1
        self.visibili = set()
        self._t = None

    def metti(self, elenco, indice):
        self.elenco, self.indice = elenco, indice
        self.centro = float(indice)
        self.queue_draw()

    def vai(self, indice):
        self.indice = indice
        if self._t is None:
            self._t = GLib.timeout_add(16, self.passo)

    def passo(self):
        d = self.indice - self.centro
        if abs(d) < 0.02:
            self.centro = float(self.indice)
            self._t = None
            self.queue_draw()
            return False
        self.centro += d * 0.3
        self.queue_draw()
        return True

    def misure(self):
        al = self.get_allocation()
        lato = px(52)
        passo = lato + px(4)
        return al, lato, passo

    def disegna(self, w, cr):
        c = self.fin.colori
        al, lato, passo = self.misure()
        cr.set_source_rgb(*C.rgb(c["FONDO"]))
        cr.paint()
        if not self.elenco:
            return False
        meta = int(al.width / passo / 2) + 2
        y = (al.height - lato) / 2
        da, a = max(0, int(self.centro) - meta), min(len(self.elenco), int(self.centro) + meta + 1)
        self.visibili = set(self.elenco[da:a])
        for i in range(da, a):
            x = al.width / 2 + (i - self.centro) * passo - lato / 2
            p = self.elenco[i]
            try:
                mt = os.stat(p).st_mtime
            except OSError:
                mt = 0
            pb = self.fin.mini.chiedi(p, mt, L.tipo_di(p) or "immagine", self._pronta,
                                      lambda k: k in self.visibili)
            cr.save()
            C.rettangolo_tondo(cr, x, y, lato, lato, px(4))
            cr.clip()
            if pb is not None:
                C.pixbuf_coprente(cr, pb, x, y, lato, lato)
            else:
                cr.set_source_rgb(*C.rgb(c["CARTA"]))
                cr.paint()
            if i != self.indice:
                cr.set_source_rgba(0, 0, 0, 0.18 if i != self.sopra else 0.0)
                cr.paint()
            cr.restore()
            if i == self.indice:
                C.rettangolo_tondo(cr, x - px(1), y - px(1), lato + px(2), lato + px(2), px(5))
                cr.set_source_rgb(*C.rgb(c["ACCENTO"]))
                cr.set_line_width(px(2))
                cr.stroke()
        return False

    def _pronta(self, p, pb):
        if p in self.visibili:
            self.queue_draw()

    def _indice_a(self, x):
        al, lato, passo = self.misure()
        i = int(round(self.centro + (x - al.width / 2) / passo))
        return i if 0 <= i < len(self.elenco) else -1

    def premi(self, w, ev):
        i = self._indice_a(ev.x)
        if ev.button == 1 and i >= 0:
            self.fin.vai(i)
        return True

    def rotella(self, w, ev):
        if ev.direction == Gdk.ScrollDirection.SMOOTH:
            ok, dx, dy = ev.get_scroll_deltas()
            d = (dx or dy) * 1.5
        elif ev.direction in (Gdk.ScrollDirection.UP, Gdk.ScrollDirection.LEFT):
            d = -2
        elif ev.direction in (Gdk.ScrollDirection.DOWN, Gdk.ScrollDirection.RIGHT):
            d = 2
        else:
            return False
        self.centro = max(0.0, min(len(self.elenco) - 1.0, self.centro + d))
        self.queue_draw()
        return True

    def _sopra(self, i):
        if i != self.sopra:
            self.sopra = i
            self.queue_draw()

    def moto(self, w, ev):
        self._sopra(self._indice_a(ev.x))
        return False


# --- l'app -----------------------------------------------------------------------------------------------------
class Foto(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.termuxondex.Foto", flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None

    def do_command_line(self, cl):
        cwd = cl.get_cwd() or os.getcwd()
        file = []
        for a in cl.get_arguments()[1:]:
            if a.startswith("-"):
                continue
            if a.startswith("file://"):
                a = GLib.filename_from_uri(a)[0]
            file.append(os.path.join(cwd, a) if not os.path.isabs(a) else a)
        if not self.win:
            self.win = Finestra(self)
        if file:
            self.win.apri_file(file)
        elif self.win.pagine.get_visible_child_name() != "galleria" or not self.win.elementi:
            self.win.mostra_galleria()
        self.win.present()
        return 0


class Finestra(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=T("Foto", "Photos"))
        C.imposta_scala()
        w, h = px(1180), px(780)
        mon = Gdk.Display.get_default().get_primary_monitor() or Gdk.Display.get_default().get_monitor(0)
        if mon:
            a = mon.get_workarea()
            w, h = min(w, int(a.width * 0.9)), min(h, int(a.height * 0.9))
        self.set_default_size(w, h)
        self.set_icon_name("multimedia-photo-viewer")
        C.classe(self, "w11")
        self.colori = C.colori()
        self.stile = C.Stile(CSS, self.tema_cambiato)
        self.mini = C.Miniature(2)
        self.preferiti = L.Preferiti()
        self.elementi = []          # tutto quello che la galleria conosce
        self.sezione = "galleria"
        self.lato = LATI["medie"]
        self.elenco, self.indice = [], 0     # le foto della vista
        self.da_galleria = False
        self.schermo_intero = False
        self.presentazione = None
        self._t_frecce = None
        self._t_salva = None
        self.pagine = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        self.add(self.pagine)
        self.pagine.add_named(self.costruisci_galleria(), "galleria")
        self.pagine.add_named(self.costruisci_vista(), "vista")
        self.connect("key-press-event", self.tasto)
        self.connect("window-state-event", self.stato_finestra)
        self.connect("delete-event", lambda *_: self.salva_rotazione() and False)
        self.show_all()
        self.info.set_reveal_child(False)
        self.frecce_visibili(False)

    def tema_cambiato(self):
        self.colori = C.colori()
        self.griglia.ridisegna()
        self.visore.queue_draw()
        self.striscia.queue_draw()

    # ======================================================================================== galleria
    def costruisci_galleria(self):
        radice = Gtk.Box()
        self.nav = C.Nav(self.scegli_sezione, larga=240)
        self.nav.voce("galleria", T("Galleria", "Gallery"), "image-x-generic-symbolic")
        self.nav.voce("preferiti", T("Preferiti", "Favorites"), "emblem-favorite-symbolic")
        self.nav.gruppo(T("Cartelle", "Folders"))
        self.cartelle = C.L.cartelle_foto()
        icone = {T("Fotocamera", "Camera"): "camera-photo-symbolic", "Download": "folder-download-symbolic",
                 T("Screenshot", "Screenshots"): "applets-screenshooter-symbolic"}
        for i, (nome, p, _prof) in enumerate(self.cartelle):
            self.nav.voce("cartella:%d" % i, nome, icone.get(nome, "folder-pictures-symbolic"))
        radice.pack_start(self.nav, False, False, 0)
        strato = C.classe(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), "strato")
        testa = Gtk.Box(spacing=px(12))
        testa.set_margin_start(px(24))
        testa.set_margin_end(px(16))
        testa.set_margin_top(px(18))
        testa.set_margin_bottom(px(6))
        self.g_titolo = C.etichetta(T("Galleria", "Gallery"), "titolo")
        testa.pack_start(self.g_titolo, False, False, 0)
        self.g_conto = C.etichetta("", "tenue")
        self.g_conto.set_valign(Gtk.Align.BASELINE)
        testa.pack_start(self.g_conto, False, False, 0)
        self.g_giro = Gtk.Spinner()
        testa.pack_start(self.g_giro, False, False, 0)
        # dimensione delle miniature
        mb = Gtk.MenuButton()
        C.classe(mb, "lieve")
        mb.set_can_focus(False)
        bx = Gtk.Box(spacing=px(6))
        bx.pack_start(C.immagine_icona("view-grid-symbolic"), False, False, 0)
        bx.pack_start(Gtk.Label(label=T("Dimensione", "Size")), False, False, 0)
        mb.add(bx)
        pop = C.classe(Gtk.Popover(), "menu-w11")
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        prima = None
        for k, testo in (("piccole", T("Piccole", "Small")), ("medie", T("Medie", "Medium")),
                         ("grandi", T("Grandi", "Large"))):
            rb = Gtk.RadioButton.new_with_label_from_widget(prima, testo)
            prima = prima or rb
            rb.set_active(k == "medie")
            rb.set_margin_start(px(10))
            rb.set_margin_end(px(14))
            rb.set_margin_top(px(4))
            rb.set_margin_bottom(px(4))
            rb.connect("toggled", lambda b, k=k: b.get_active() and self.griglia.metti_lato(LATI[k]))
            vb.pack_start(rb, False, False, 0)
        vb.show_all()
        pop.add(vb)
        mb.set_popover(pop)
        testa.pack_end(mb, False, False, 0)
        testa.pack_end(C.bottone("view-refresh-symbolic", suggerimento=T("Aggiorna", "Refresh"),
                                 azione=lambda: self.scandisci()), False, False, 0)
        strato.pack_start(testa, False, False, 0)
        self.griglia = C.Griglia(self.mini, lato=self.lato, proporzione=1.0, spazio=4)
        self.griglia.attiva = self.apri_elemento
        self.griglia.menu = self.menu_elemento
        strato.pack_start(self.griglia, True, True, 0)
        radice.pack_start(strato, True, True, 0)
        return radice

    def mostra_galleria(self):
        self.salva_rotazione()
        if self.schermo_intero:
            self.unfullscreen()
        self.ferma_presentazione()
        self.pagine.set_visible_child_name("galleria")
        self.set_title(T("Foto", "Photos"))
        if not self.elementi:
            self.scandisci()
        else:
            self.mostra_sezione()
        self.nav.segna(self.sezione)

    def scandisci(self):
        self.g_giro.start()

        def lavoro():
            out, visti = [], set()
            for i, (nome, p, prof) in enumerate(self.cartelle):
                for f, mt, dim in L.scansiona(p, GALLERIA, prof):
                    vero = os.path.realpath(f)
                    if vero in visti:
                        continue
                    visti.add(vero)
                    out.append({"percorso": f, "mtime": mt, "dim": dim, "tipo": L.tipo_di(f),
                                "data": L.data_da_nome(f, mt), "cartella": i})
            GLib.idle_add(fatto, out)

        def fatto(out):
            self.elementi = out
            self.g_giro.stop()
            self.mostra_sezione()
            return False
        threading.Thread(target=lavoro, daemon=True).start()

    def scegli_sezione(self, k):
        self.sezione = k
        self.mostra_sezione()

    def elementi_sezione(self):
        if self.sezione == "preferiti":
            return [e for e in self.elementi if e["percorso"] in self.preferiti]
        if self.sezione.startswith("cartella:"):
            i = int(self.sezione.split(":")[1])
            return [e for e in self.elementi if e["cartella"] == i]
        return self.elementi

    def mostra_sezione(self):
        el = self.elementi_sezione()
        for e in el:
            e["cuore"] = e["percorso"] in self.preferiti
        if self.sezione == "preferiti":
            titolo = T("Preferiti", "Favorites")
            self.griglia.vuoto = T("Le foto con il cuore compaiono qui. Apri una foto e tocca ♡.",
                                   "Photos you mark with a heart show up here. Open a photo and select ♡.")
        elif self.sezione.startswith("cartella:"):
            titolo = self.cartelle[int(self.sezione.split(":")[1])][0]
            self.griglia.vuoto = T("Nessuna foto in questa cartella", "No photos in this folder")
        else:
            titolo = T("Galleria", "Gallery")
            self.griglia.vuoto = T("Nessuna foto trovata nelle cartelle Immagini e Fotocamera",
                                   "No photos found in your Pictures and Camera folders")
        self.g_titolo.set_text(titolo)
        self.g_conto.set_text(L.elementi(len(el)) if el else "")
        self.gruppi = L.per_mese(el)
        self.griglia.metti(self.gruppi)
        self.nav.segna(self.sezione)

    def ordine_galleria(self):
        return [e for _, g in self.gruppi for e in g]

    def apri_elemento(self, e):
        if e["tipo"] == "video":
            apri_video([e["percorso"]])
            return
        foto = [x["percorso"] for x in self.ordine_galleria() if x["tipo"] == "immagine"]
        self.da_galleria = True
        self.mostra_vista(foto, foto.index(e["percorso"]) if e["percorso"] in foto else 0)

    def menu_elemento(self, e, ev):
        m = Gtk.Menu()
        C.classe(m, "w11")
        voci = [(T("Apri", "Open"), lambda: self.apri_elemento(e)),
                (T("Togli dai preferiti", "Remove from favorites") if e["percorso"] in self.preferiti
                 else T("Aggiungi ai preferiti", "Add to favorites"), lambda: self.cuore_di(e["percorso"])),
                (T("Apri con…", "Open with…"), lambda: C.apri_con(self, e["percorso"])),
                (T("Mostra nella cartella", "Show in folder"), lambda: C.mostra_nella_cartella(e["percorso"])),
                (T("Sposta nel Cestino", "Move to Recycle Bin"), lambda: self.elimina(e["percorso"], self.griglia.area))]
        for testo, az in voci:
            it = Gtk.MenuItem(label=testo)
            it.connect("activate", lambda _i, az=az: az())
            m.append(it)
        m.show_all()
        m.attach_to_widget(self.griglia, None)
        m.popup_at_pointer(ev)

    def cuore_di(self, p):
        self.preferiti.cambia(p)
        for e in self.elementi:
            if e["percorso"] == p:
                e["cuore"] = p in self.preferiti
        if self.pagine.get_visible_child_name() == "galleria":
            self.mostra_sezione() if self.sezione == "preferiti" else self.griglia.area.queue_draw()

    # ======================================================================================== vista
    def costruisci_vista(self):
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # barra in alto
        self.alto = Gtk.Box(spacing=px(2))
        self.alto.set_margin_start(px(8))
        self.alto.set_margin_end(px(8))
        self.alto.set_margin_top(px(6))
        self.alto.set_margin_bottom(px(6))
        self.b_indietro = C.bottone("go-previous-symbolic", suggerimento=T("Galleria", "Gallery"),
                                    azione=self.mostra_galleria)
        self.alto.pack_start(self.b_indietro, False, False, 0)
        self.v_nome = C.etichetta("", "nome")
        self.v_nome.set_margin_start(px(8))
        self.v_nome.set_max_width_chars(36)
        self.alto.pack_start(self.v_nome, False, False, 0)
        centro = Gtk.Box(spacing=px(2))
        self.b_ruota = C.bottone("object-rotate-right-symbolic", suggerimento=T("Ruota (Ctrl+R)", "Rotate (Ctrl+R)"),
                                 azione=self.ruota)
        self.b_elimina = C.bottone("user-trash-symbolic", suggerimento=T("Elimina (Canc)", "Delete (Del)"),
                                   azione=lambda: self.elimina(self.corrente(), self.b_elimina))
        self.b_cuore = C.bottone("emblem-favorite-symbolic", suggerimento=T("Preferito", "Favorite"),
                                 azione=lambda: self.cuore())
        self.b_info = C.bottone("dialog-information-symbolic", suggerimento=T("Informazioni sul file (I)", "File info (I)"),
                                azione=self.mostra_info, attiva=True)
        self.b_pres = C.bottone("media-playback-start-symbolic", suggerimento=T("Presentazione (F5)", "Slideshow (F5)"),
                                azione=self.avvia_presentazione)
        for b in (self.b_ruota, self.b_elimina, self.b_cuore, self.b_info, self.b_pres):
            centro.pack_start(b, False, False, 0)
        centro.pack_start(self.menu_altro(), False, False, 0)
        self.alto.set_center_widget(centro)
        self.b_intero = C.bottone("view-fullscreen-symbolic", suggerimento=T("Schermo intero (F11)", "Full screen (F11)"),
                                  azione=self.cambia_intero)
        self.alto.pack_end(self.b_intero, False, False, 0)
        vb.pack_start(self.alto, False, False, 0)
        # la foto, le frecce, il pannello Informazioni
        mezzo = Gtk.Box()
        self.sovra = Gtk.Overlay()
        self.visore = Visore(self)
        self.sovra.add(self.visore)
        self.f_sx = C.classe(C.bottone("go-previous-symbolic", suggerimento=T("Precedente", "Previous"),
                                       azione=lambda: self.vai(self.indice - 1), cls="freccia"), "freccia")
        self.f_dx = C.classe(C.bottone("go-next-symbolic", suggerimento=T("Successiva", "Next"),
                                       azione=lambda: self.vai(self.indice + 1), cls="freccia"), "freccia")
        for f, al in ((self.f_sx, Gtk.Align.START), (self.f_dx, Gtk.Align.END)):
            f.set_halign(al)
            f.set_valign(Gtk.Align.CENTER)
            f.set_margin_start(px(14))
            f.set_margin_end(px(14))
            f.connect("enter-notify-event", lambda *_: self.mouse_mosso())
            self.sovra.add_overlay(f)
        mezzo.pack_start(self.sovra, True, True, 0)
        self.info = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_LEFT, transition_duration=150)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_size_request(px(300), -1)
        C.classe(sc, "pannello")
        self.info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(14))
        self.info_box.set_margin_start(px(18))
        self.info_box.set_margin_end(px(18))
        self.info_box.set_margin_top(px(16))
        self.info_box.set_margin_bottom(px(16))
        sc.add(self.info_box)
        self.info.add(sc)
        mezzo.pack_start(self.info, False, False, 0)
        vb.pack_start(mezzo, True, True, 0)
        # striscia e barra in basso
        self.striscia = Striscia(self)
        C.classe(self.striscia, "striscia")
        vb.pack_start(self.striscia, False, False, 0)
        self.basso = Gtk.Box(spacing=px(4))
        self.basso.set_margin_start(px(14))
        self.basso.set_margin_end(px(8))
        self.basso.set_margin_top(px(2))
        self.basso.set_margin_bottom(px(6))
        self.v_dati = C.etichetta("", "tenue numeri")
        self.basso.pack_start(self.v_dati, False, False, 0)
        self.b_striscia = C.bottone("view-paged-symbolic", suggerimento=T("Striscia delle foto", "Filmstrip"),
                                    azione=self.cambia_striscia, attiva=True)
        self.b_striscia.set_active(True)
        self.basso.pack_start(self.b_striscia, False, False, px(8))
        self.basso.pack_end(C.bottone("zoom-fit-best-symbolic", suggerimento=T("Adatta alla finestra (0)", "Fit to window (0)"),
                                      azione=lambda: self.visore.adatta_finestra()), False, False, 0)
        self.basso.pack_end(C.bottone("zoom-in-symbolic", suggerimento=T("Ingrandisci (+)", "Zoom in (+)"),
                                      azione=lambda: self.visore.zooma(self.visore.scala() * 1.25)), False, False, 0)
        self.v_zoom = C.classe(C.etichetta("100%", "numeri zoom", xalign=0.5), "zoom")
        self.basso.pack_end(self.v_zoom, False, False, 0)
        self.basso.pack_end(C.bottone("zoom-out-symbolic", suggerimento=T("Riduci (−)", "Zoom out (−)"),
                                      azione=lambda: self.visore.zooma(self.visore.scala() / 1.25)), False, False, 0)
        vb.pack_start(self.basso, False, False, 0)
        return C.classe(vb, "vista")

    def menu_altro(self):
        mb = Gtk.MenuButton()
        C.classe(mb, "icona")
        mb.set_can_focus(False)
        mb.set_tooltip_text(T("Altre opzioni", "See more"))
        mb.add(C.immagine_icona("view-more-symbolic"))
        pop = C.classe(Gtk.Popover(), "menu-w11")
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for testo, az in ((T("Apri con…", "Open with…"), lambda: C.apri_con(self, self.corrente())),
                          (T("Mostra nella cartella", "Show in folder"), lambda: C.mostra_nella_cartella(self.corrente())),
                          (T("Copia", "Copy") + "  (Ctrl+C)", self.copia),
                          (T("Imposta come sfondo", "Set as desktop background"), self.sfondo)):
            b = Gtk.ModelButton(text=testo)
            b.get_child().set_xalign(0) if isinstance(b.get_child(), Gtk.Label) else None
            b.connect("clicked", lambda _b, az=az: (pop.popdown(), az()))
            vb.pack_start(b, False, False, 0)
        vb.show_all()
        pop.add(vb)
        mb.set_popover(pop)
        return mb

    def apri_file(self, file):
        """dal doppio clic in Esplora file: la prima foto, con le altre della sua cartella."""
        p = file[0]
        if L.tipo_di(p) == "video":
            apri_video(file)
            if not self.get_visible() or (self.pagine.get_visible_child_name() == "galleria" and not self.elementi):
                GLib.idle_add(lambda: self.close() and False)
            return
        if len(file) > 1:
            elenco, i = file, 0
        else:
            elenco, i = L.fratelli(p, L.IMMAGINI)
        self.da_galleria = False
        self.mostra_vista(elenco, i)

    def mostra_vista(self, elenco, indice):
        self.salva_rotazione()
        self.elenco = elenco
        self.pagine.set_visible_child_name("vista")
        self.striscia.metti(elenco, indice)
        self.b_striscia.set_visible(len(elenco) > 1)
        self.striscia.set_visible(len(elenco) > 1 and self.b_striscia.get_active() and not self.schermo_intero)
        self.vai(indice, subito=True)
        self.visore.grab_focus()

    def corrente(self):
        return self.elenco[self.indice] if 0 <= self.indice < len(self.elenco) else None

    def vai(self, i, subito=False, dissolvi=False):
        if not self.elenco or not 0 <= i < len(self.elenco):
            return
        if not subito:
            self.salva_rotazione()
        self.indice = i
        p = self.elenco[i]
        self.visore.apri(p, dissolvi=dissolvi)
        self.striscia.vai(i)
        self.set_title("%s - %s" % (os.path.basename(p), T("Foto", "Photos")))
        self.v_nome.set_text(os.path.basename(p))
        self.aggiorna_cuore()
        self.f_sx.set_sensitive(i > 0)
        self.f_dx.set_sensitive(i < len(self.elenco) - 1)
        self.visore.prepara(self.elenco[i + 1] if i + 1 < len(self.elenco) else None)
        self.visore.prepara(self.elenco[i - 1] if i > 0 else None)
        if self.info.get_reveal_child():
            self.riempi_info()

    def foto_aperta(self, v):
        """la foto è pronta: misure, zoom, informazioni."""
        p = self.corrente()
        try:
            dim = os.path.getsize(p)
        except (OSError, TypeError):
            dim = 0
        w, h = v.misure_girate()
        parti = []
        if w and h:
            parti.append("%d × %d" % (w, h))
        if dim:
            parti.append(L.misura(dim))
        self.v_dati.set_text("  ·  ".join(parti))
        self.zoom_cambiato()

    def zoom_cambiato(self):
        self.v_zoom.set_text("%d%%" % self.visore.percento())

    def aggiorna_cuore(self):
        si = self.corrente() in self.preferiti
        C.metti_icona(self.b_cuore, "emblem-favorite-symbolic" if si else "non-starred-symbolic")
        ctx = self.b_cuore.get_style_context()
        (ctx.add_class if si else ctx.remove_class)("cuore")
        self.b_cuore.set_tooltip_text(T("Togli dai preferiti", "Remove from favorites") if si
                                      else T("Aggiungi ai preferiti", "Add to favorites"))

    def cuore(self):
        p = self.corrente()
        if p:
            self.cuore_di(p)
            self.aggiorna_cuore()

    # --- frecce e mouse ---
    def frecce_visibili(self, si):
        for f in (self.f_sx, self.f_dx):
            f.set_visible(si and len(self.elenco) > 1)

    def mouse_mosso(self):
        self.frecce_visibili(True)
        if self._t_frecce:
            GLib.source_remove(self._t_frecce)
        self._t_frecce = GLib.timeout_add(1800, self._frecce_via)
        if self.schermo_intero:
            w = self.get_window()
            if w:
                w.set_cursor(None)

    def _frecce_via(self):
        self._t_frecce = None
        self.frecce_visibili(False)
        if self.schermo_intero and self.get_window():
            self.get_window().set_cursor(Gdk.Cursor.new_from_name(self.get_display(), "none"))
        return False

    # --- azioni ---
    def ruota(self):
        if self.visore.sup is None:
            return
        self.visore.ruota()
        self.foto_aperta(self.visore)
        if self._t_salva:
            GLib.source_remove(self._t_salva)
        self._t_salva = GLib.timeout_add(2500, lambda: self.salva_rotazione() and False)

    def salva_rotazione(self):
        """la rotazione resta nel file: JPEG cambiando solo l'orientamento (niente qualità persa), PNG riscritta
        (senza perdite); gli altri formati restano girati solo qui."""
        if self._t_salva:
            GLib.source_remove(self._t_salva)
            self._t_salva = None
        v, p = self.visore, self.visore.percorso
        delta = (v.giro - getattr(v, "giro_salvato", 0)) % 360
        if not p or not delta:
            return True
        fmt = dati_file(p)[0]
        ok = False
        if fmt == "jpeg" and GExiv2:
            try:
                m = GExiv2.Metadata.new()
                m.open_path(p)
                ora = int(m.get_orientation())
                passi = {1: 0, 0: 0, 6: 90, 3: 180, 8: 270}
                if ora in passi:   # le orientazioni specchiate restano come sono
                    nuovo = {0: 1, 90: 6, 180: 3, 270: 8}[(passi[ora] + delta) % 360]
                    m.set_orientation(GExiv2.Orientation(nuovo))
                    m.save_file(p)
                    ok = True
            except GLib.Error:
                ok = False
        elif fmt == "png" and isinstance(v.pb, GdkPixbuf.Pixbuf) and v.pb.get_width() >= v.vero[0] - 1:
            try:   # v.pb è la foto com'era all'apertura: si gira di tutta la rotazione
                angolo = {90: GdkPixbuf.PixbufRotation.CLOCKWISE, 180: GdkPixbuf.PixbufRotation.UPSIDEDOWN,
                          270: GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE}.get(v.giro)
                (v.pb.rotate_simple(angolo) if angolo else v.pb).savev(p + ".tod-tmp", "png", [], [])
                os.replace(p + ".tod-tmp", p)
                ok = True
            except (GLib.Error, OSError, KeyError):
                ok = False
        if ok:
            v.giro_salvato = v.giro
            v.cache.pop(p, None)
            self.mini.dimentica(p)
        return True

    def elimina(self, p, vicino):
        if not p:
            return
        pop = Gtk.Popover()
        pop.set_relative_to(vicino)
        C.classe(pop, "menu-w11")
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(10))
        vb.set_margin_start(px(14))
        vb.set_margin_end(px(14))
        vb.set_margin_top(px(12))
        vb.set_margin_bottom(px(12))
        vb.pack_start(C.etichetta(T("Spostare questo file nel Cestino?", "Move this file to the Recycle Bin?"), "nome"),
                      False, False, 0)
        vb.pack_start(C.etichetta(os.path.basename(p), "tenue"), False, False, 0)
        hb = Gtk.Box(spacing=px(8), homogeneous=True)
        si = C.bottone(testo=T("Elimina", "Delete"), cls="accento")
        no = C.bottone(testo=T("Annulla", "Cancel"), cls=None)
        for b in (si, no):   # da tastiera: Invio elimina, Esc annulla
            b.set_can_focus(True)
        hb.pack_start(si, True, True, 0)
        hb.pack_start(no, True, True, 0)
        vb.pack_start(hb, False, False, 0)
        nota = C.etichetta("", "tenue", ellissi=False, righe=3, larga=34)
        vb.pack_start(nota, False, False, 0)
        no.connect("clicked", lambda *_: pop.popdown())
        stato = {"davvero": False}

        def conferma(*_):
            err = C.elimina_davvero(p) if stato["davvero"] else C.nel_cestino(p)
            if err and not stato["davvero"]:
                stato["davvero"] = True
                nota.set_text(T("Il Cestino non funziona per questa cartella. Eliminare definitivamente? "
                                "Non si potrà recuperare.",
                                "The Recycle Bin doesn't work for this folder. Delete permanently? "
                                "It can't be undone."))
                si.set_label(T("Elimina definitivamente", "Delete permanently"))
                C.classe(si, "rosso")
                nota.show()
                return
            pop.popdown()
            if not err:
                self.tolto(p)
        si.connect("clicked", conferma)
        vb.show_all()
        nota.hide()
        pop.add(vb)
        pop.popup()
        si.grab_focus()

    def tolto(self, p):
        self.preferiti.sposta(p, None)
        self.mini.dimentica(p)
        self.elementi = [e for e in self.elementi if e["percorso"] != p]
        if self.pagine.get_visible_child_name() == "galleria":
            self.mostra_sezione()
            return
        if p in self.elenco:
            i = self.elenco.index(p)
            self.elenco.pop(i)
            self.visore.cache.pop(p, None)
            if not self.elenco:
                self.mostra_galleria()
                return
            self.striscia.metti(self.elenco, min(i, len(self.elenco) - 1))
            self.b_striscia.set_visible(len(self.elenco) > 1)
            self.cambia_striscia()
            self.vai(min(i, len(self.elenco) - 1), subito=True)

    def mostra_info(self):
        si = self.b_info.get_active()
        self.info.set_reveal_child(si)
        if si:
            self.riempi_info()

    def riempi_info(self):
        for f in self.info_box.get_children():
            self.info_box.remove(f)
        p = self.corrente()
        if not p:
            return
        add = self.info_box.pack_start
        add(C.etichetta(T("Informazioni sul file", "File information"), "titolo2"), False, False, 0)
        try:
            st = os.stat(p)
        except OSError:
            st = None
        fmt, w, h = dati_file(p)
        quando = L.data_da_nome(p, st.st_mtime if st else 0)
        meta = {}
        if GExiv2 and fmt in ("jpeg", "png", "webp", "tiff", "heif", "avif"):
            try:
                m = GExiv2.Metadata.new()
                m.open_path(p)

                def tag(n):
                    try:
                        return m.get_tag_interpreted_string(n) if m.has_tag(n) else None
                    except GLib.Error:
                        return None
                meta["modello"] = " ".join(x for x in (tag("Exif.Image.Make"), tag("Exif.Image.Model")) if x)
                if (tag("Exif.Image.Model") or "").lower().startswith((tag("Exif.Image.Make") or "-").lower()):
                    meta["modello"] = tag("Exif.Image.Model")
                d = tag("Exif.Photo.DateTimeOriginal")
                if d:
                    import time as _t
                    try:
                        quando = _t.mktime(_t.strptime(d.strip(), "%Y:%m:%d %H:%M:%S"))
                    except ValueError:
                        pass
                for k, n in (("f", "Exif.Photo.FNumber"), ("t", "Exif.Photo.ExposureTime"),
                             ("iso", "Exif.Photo.ISOSpeedRatings"), ("mm", "Exif.Photo.FocalLength")):
                    meta[k] = tag(n)
            except GLib.Error:
                pass

        def riga(icona, titolo, *righe):
            hb = Gtk.Box(spacing=px(12))
            im = C.immagine_icona(icona, 16)
            im.set_valign(Gtk.Align.START)
            hb.pack_start(im, False, False, 0)
            vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(2))
            vb.pack_start(C.etichetta(titolo, "nome", ellissi=False, righe=3, larga=28), False, False, 0)
            for r in righe:
                if r:
                    vb.pack_start(C.etichetta(r, "tenue", ellissi=False, righe=3, larga=30), False, False, 0)
            hb.pack_start(vb, True, True, 0)
            add(hb, False, False, 0)
        riga("x-office-calendar-symbolic", T("Data acquisizione", "Date taken"), L.data_lunga(quando))
        mp = "%.1f MP" % (w * h / 1e6) if w and h else ""
        riga("image-x-generic-symbolic", os.path.basename(p),
             "  ·  ".join(x for x in ("%d × %d" % (w, h) if w else "", mp.replace(".", ",") if L.ITALIANO else mp,
                                       L.misura(st.st_size) if st else "") if x))
        if meta.get("modello") or any(meta.get(k) for k in ("f", "t", "iso", "mm")):
            dati = []
            if meta.get("f"):
                dati.append(meta["f"].replace("F", "f/") if meta["f"].startswith("F") else meta["f"])
            if meta.get("t"):
                dati.append(meta["t"])
            if meta.get("iso"):
                dati.append("ISO " + meta["iso"])
            if meta.get("mm"):
                mm = meta["mm"].replace(".0 mm", " mm")
                dati.append(mm.replace(".", ",") if L.ITALIANO else mm)
            riga("camera-photo-symbolic", meta.get("modello") or T("Fotocamera", "Camera"), "   ".join(dati))
        cart = os.path.dirname(p)
        riga("folder-symbolic", T("Percorso", "Location"), cart)
        b = C.bottone(testo=T("Apri cartella", "Open folder"), cls="lieve",
                      azione=lambda: C.mostra_nella_cartella(p))
        b.set_halign(Gtk.Align.START)
        b.set_margin_start(px(20))
        add(b, False, False, 0)
        self.info_box.show_all()

    def copia(self):
        pb = self.visore.pb
        if pb is not None:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_image(pb)

    def sfondo(self):
        p = self.corrente()
        if p:
            comando_tod("aspetto", "sfondo", p)
            C.avviso(self.sovra, T("Sfondo del desktop impostato", "Desktop background set"), secondi=3)

    def cambia_striscia(self):
        self.striscia.set_visible(self.b_striscia.get_active() and len(self.elenco) > 1 and not self.schermo_intero)

    # --- schermo intero e presentazione ---
    def cambia_intero(self):
        if self.schermo_intero:
            self.ferma_presentazione()
            self.unfullscreen()
        else:
            self.fullscreen()

    def stato_finestra(self, w, ev):
        intero = bool(ev.new_window_state & Gdk.WindowState.FULLSCREEN)
        if intero == self.schermo_intero:
            return False
        self.schermo_intero = intero
        self.alto.set_visible(not intero)
        self.basso.set_visible(not intero)
        self.cambia_striscia()
        C.metti_icona(self.b_intero, "view-restore-symbolic" if intero else "view-fullscreen-symbolic")
        ctx = self.pagine.get_style_context()
        (ctx.add_class if intero else ctx.remove_class)("nero")
        if not intero and self.get_window():
            self.get_window().set_cursor(None)
        return False

    def avvia_presentazione(self):
        if len(self.elenco) < 1:
            return
        self.info.set_reveal_child(False)
        self.b_info.set_active(False)
        self.fullscreen()
        self.ferma_presentazione()
        self.presentazione = GLib.timeout_add(4000, self.passo_presentazione)

    def passo_presentazione(self):
        if not self.elenco:
            self.presentazione = None
            return False
        self.vai((self.indice + 1) % len(self.elenco), dissolvi=True)
        return True

    def ferma_presentazione(self):
        if self.presentazione:
            GLib.source_remove(self.presentazione)
            self.presentazione = None

    # --- tastiera ---
    def tasto(self, w, ev):
        k = Gdk.keyval_name(ev.keyval)
        ctrl = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)
        if self.pagine.get_visible_child_name() == "galleria":
            if k == "F5" or (ctrl and k in ("r", "R")):
                self.scandisci()
                return True
            if k == "Escape" and self.elenco and not self.da_galleria:
                return False
            return False
        if isinstance(self.get_focus(), Gtk.Entry):
            return False
        if k in ("Left", "Page_Up", "BackSpace"):
            self.vai(self.indice - 1)
        elif k in ("Right", "Page_Down", "space"):
            self.vai(self.indice + 1)
        elif k == "Home":
            self.vai(0)
        elif k == "End":
            self.vai(len(self.elenco) - 1)
        elif k == "Escape":
            if self.presentazione or self.schermo_intero:
                self.ferma_presentazione()
                self.unfullscreen()
            elif self.da_galleria:
                self.mostra_galleria()
            else:
                return False
        elif k == "F11" or (k in ("f", "F") and not ctrl):
            self.cambia_intero()
        elif k == "F5":
            self.ferma_presentazione() if self.presentazione else self.avvia_presentazione()
        elif k == "Delete":
            self.elimina(self.corrente(), self.b_elimina)
        elif ctrl and k in ("r", "R"):
            self.ruota()
        elif ctrl and k in ("c", "C"):
            self.copia()
        elif k in ("plus", "KP_Add", "equal"):
            self.visore.zooma(self.visore.scala() * 1.25)
        elif k in ("minus", "KP_Subtract"):
            self.visore.zooma(self.visore.scala() / 1.25)
        elif k in ("0", "KP_0"):
            self.visore.adatta_finestra()
        elif k in ("1", "KP_1"):
            self.visore.zooma(1.0)
        elif k in ("i", "I") and not ctrl:
            self.b_info.set_active(not self.b_info.get_active())
        else:
            return False
        return True


if __name__ == "__main__":
    sys.exit(Foto().run(sys.argv))
