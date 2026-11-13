#!/data/data/com.termux/files/usr/bin/python3
"""tod-archivio.py — Archiviazione, come quella di macOS (Impostazioni → Generali → Archiviazione), 13 novembre 2026.

Pietro: "fammi un applicativo tipo macOS che mi fa vedere la memoria occupata" (dopo i 62 GB di un registro di prova
dimenticato in $PREFIX/tmp). In alto il telefono con la barra a colori per categoria, sotto i Consigli (file
temporanei grandi che nessuno usa, Cestino, cache) e l'elenco delle categorie; un clic apre il dettaglio con le
cartelle che pesano di più e "Mostra in Esplora file". I conti: share/archivio_dati.py (senza grafica, nei test).
All'apertura si vede subito l'ultima misura salvata, e intanto se ne fa una nuova (du, circa un minuto).
"""
import os
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "media"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comune as C  # noqa: E402
from comune import Gdk, Gio, GLib, Gtk, px, T  # noqa: E402
import archivio_dati as D  # noqa: E402

GLib.set_prgname("tod-archivio")   # la barra riconosce la finestra (tod-archivio.desktop)
IT = os.environ.get("LANGUAGE", os.environ.get("LANG", "it")).startswith("it")
NOMI = {k: (it, en, col, icona) for k, it, en, col, icona in D.CATEGORIE}
SPIEGA = {
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
    "cestino": T("I file che hai buttato, finché non svuoti il Cestino.", "Files you threw away, until you empty the Trash."),
}

CSS = """
.w11 .arch-carta { background-color: CARTA; border: 1px solid LINEA; border-radius: 12px; padding: 18px 20px; }
.w11 .arch-elenco { background-color: CARTA; border: 1px solid LINEA; border-radius: 12px; }
.w11 .arch-elenco row { padding: 10px 16px; border-bottom: 1px solid LINEA; background: transparent; }
.w11 .arch-elenco row:last-child { border-bottom: none; }
.w11 .arch-elenco row:hover { background-color: PASSAGGIO; }
.w11 .arch-elenco row:first-child { border-top-left-radius: 12px; border-top-right-radius: 12px; }
.w11 .arch-elenco row:last-child { border-bottom-left-radius: 12px; border-bottom-right-radius: 12px; }
.w11 .arch-numero { font-size: 13pt; font-weight: 600; }
.w11 .arch-legenda { font-size: 9pt; color: TESTO2; }
.w11 .arch-sezione { font-size: 11pt; font-weight: 600; margin-top: 8px; }
.w11 .arch-misura { color: TESTO2; font-feature-settings: "tnum"; }
.w11 button.arch-indietro { color: ACCTESTO; padding: 4px 8px 4px 2px; }
"""


def gentile(p):
    """un percorso leggibile: ~ per la casa di Termux, «Debian:» per il container"""
    if p.startswith(D.ROOTFS):
        return "Debian: " + (p[len(D.ROOTFS):] or "/")
    if p.startswith(D.CASA):
        return "~" + p[len(D.CASA):]
    if p.startswith(D.PREFIX):
        return "Termux: /usr" + p[len(D.PREFIX):]
    return p


def modello():
    try:
        r = subprocess.run(["getprop", "ro.product.model"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class Pallino(Gtk.DrawingArea):
    """il quadratino tondo del colore della categoria (come i segni della barra di macOS)"""

    def __init__(self, colore, lato=12):
        super().__init__()
        self.colore = colore
        self.set_size_request(px(lato), px(lato))
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self.disegna)

    def disegna(self, w, cr):
        lato = min(w.get_allocated_width(), w.get_allocated_height())
        C.rettangolo_tondo(cr, 0, 0, lato, lato, lato * 0.3)
        cr.set_source_rgb(*C.rgb(self.colore))
        cr.fill()


class Barra(Gtk.DrawingArea):
    """la barra a colori dello spazio occupato; al passaggio del mouse dice cosa è quel pezzo"""

    def __init__(self, finestra):
        super().__init__()
        self.finestra, self.pezzi, self.totale = finestra, [], 0
        self.set_size_request(-1, px(26))
        self.set_has_tooltip(True)
        self.connect("draw", self.disegna)
        self.connect("query-tooltip", self.suggerimento)

    def metti(self, dati):
        cat = dati["categorie"]
        self.pezzi = [(k, cat[k]["byte"]) for k, *_ in D.CATEGORIE if cat[k]["byte"] > 0]
        self.totale = dati["totale"] or sum(n for _k, n in self.pezzi)
        self.queue_draw()

    def posizioni(self, w):
        x, out = 0.0, []
        for k, n in self.pezzi:
            l = w * n / self.totale if self.totale else 0
            out.append((k, n, x, l))
            x += l
        return out

    def disegna(self, w, cr):
        larga, alta = w.get_allocated_width(), w.get_allocated_height()
        c = self.finestra.colori
        C.rettangolo_tondo(cr, 0, 0, larga, alta, px(6))
        cr.save()
        cr.clip_preserve()
        cr.set_source_rgb(*C.rgb("#3a3a3c" if c["scuro"] else "#e3e3e6"))   # lo spazio libero
        cr.fill()
        for k, _n, x, l in self.posizioni(larga):
            if l < 0.5:
                continue
            cr.rectangle(x, 0, max(1.0, l), alta)
            cr.set_source_rgb(*C.rgb(NOMI[k][2]))
            cr.fill()
            if l > 3:   # una lineetta chiara tra un pezzo e l'altro, come macOS
                cr.rectangle(x + l - 1, 0, 1, alta)
                cr.set_source_rgba(1, 1, 1, 0.6)
                cr.fill()
        cr.restore()
        return False

    def suggerimento(self, w, x, _y, _tastiera, tip):
        for k, n, x0, l in self.posizioni(w.get_allocated_width()):
            if x0 <= x < x0 + l:
                tip.set_text(f"{NOMI[k][0] if IT else NOMI[k][1]}: {D.dimensione(n, IT)}")
                return True
        if self.totale:
            libero = self.finestra.dati["libero"] if self.finestra.dati else 0
            tip.set_text(T(f"Libero: {D.dimensione(libero, IT)}", f"Free: {D.dimensione(libero, IT)}"))
            return True
        return False


class Finestra(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=T("Archiviazione", "Storage"))
        C.imposta_scala()
        w, h = px(860), px(760)
        mon = Gdk.Display.get_default().get_primary_monitor() or Gdk.Display.get_default().get_monitor(0)
        if mon:
            a = mon.get_workarea()
            w, h = min(w, int(a.width * 0.9)), min(h, int(a.height * 0.9))
        self.set_default_size(w, h)
        self.set_icon_name("org.gnome.baobab")
        C.classe(self, "w11")
        self.colori = C.colori()
        self.stile = C.Stile(CSS, self.tema_cambiato)
        self.dati, self.temporanei, self.pulibili = None, [], []
        self.in_misura = False
        self.aperta = None   # la categoria del dettaglio
        self.pagine = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT, transition_duration=180)
        self.pagine.add_named(self.costruisci_principale(), "principale")
        self.dettaglio = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.add(self.dettaglio)
        self.pagine.add_named(sw, "dettaglio")
        strato = C.classe(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), "strato pieno")
        strato.pack_start(self.pagine, True, True, 0)
        self.add(strato)
        self.connect("key-press-event", self.tasto)
        self.show_all()
        vecchia = D.ultima()
        if vecchia:   # consigli subito (leggere /proc e due cartelle costa poco), le misure dopo
            self.mostra(vecchia, D.temporanei_grandi([os.path.join(D.PREFIX, "tmp"), os.path.join(D.ROOTFS, "tmp")]),
                        D.cache_pulibili(vecchia))
        self.misura()

    def tema_cambiato(self):
        self.colori = C.colori()
        self.barra.queue_draw()

    # ============================================================================================ pagina principale
    def costruisci_principale(self):
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(14))
        for m, n in (("start", 28), ("end", 28), ("top", 22), ("bottom", 24)):
            getattr(v, f"set_margin_{m}")(px(n))
        testa = Gtk.Box(spacing=px(10))
        testa.pack_start(C.etichetta(T("Archiviazione", "Storage"), "titolo"), False, False, 0)
        self.rotella = Gtk.Spinner()
        testa.pack_end(self.rotella, False, False, 0)
        self.b_aggiorna = C.bottone("view-refresh-symbolic", suggerimento=T("Misura di nuovo", "Measure again"),
                                    azione=self.misura)
        testa.pack_end(self.b_aggiorna, False, False, 0)
        self.stato = C.etichetta("", "tenue", xalign=1.0)
        testa.pack_end(self.stato, False, False, 0)
        v.pack_start(testa, False, False, 0)

        # il telefono e la barra
        carta = C.classe(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(12)), "arch-carta")
        riga = Gtk.Box(spacing=px(14))
        riga.pack_start(C.immagine_icona("drive-harddisk", 44), False, False, 0)
        nomi = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(2))
        nomi.set_valign(Gtk.Align.CENTER)
        nomi.pack_start(C.etichetta(T("Memoria del telefono", "Phone storage"), "titolo2"), False, False, 0)
        mod = modello()
        nomi.pack_start(C.etichetta(mod or T("Spazio interno", "Internal storage"), "sotto"), False, False, 0)
        riga.pack_start(nomi, True, True, 0)
        self.l_uso = C.etichetta("", "arch-numero", xalign=1.0)
        self.l_uso.set_valign(Gtk.Align.CENTER)
        riga.pack_end(self.l_uso, False, False, 0)
        carta.pack_start(riga, False, False, 0)
        self.barra = Barra(self)
        carta.pack_start(self.barra, False, False, 0)
        self.legenda = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=px(16), row_spacing=px(4),
                                   max_children_per_line=6, homogeneous=False)
        carta.pack_start(self.legenda, False, False, 0)
        v.pack_start(carta, False, False, 0)

        # consigli
        self.l_consigli = C.etichetta(T("Consigli", "Recommendations"), "arch-sezione")
        v.pack_start(self.l_consigli, False, False, 0)
        self.consigli = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(10))
        v.pack_start(self.consigli, False, False, 0)

        # categorie
        v.pack_start(C.etichetta(T("Cosa occupa lo spazio", "What uses the space"), "arch-sezione"), False, False, 0)
        self.elenco = C.classe(Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE), "arch-elenco")
        self.elenco.connect("row-activated", lambda _l, r: self.apri(r.chiave))
        v.pack_start(self.elenco, False, False, 0)
        nota = C.etichetta(T("Le misure contano lo spazio vero sul telefono. «Android e altre app» è il resto dello spazio "
                             "occupato: lo gestiscono le impostazioni del telefono.",
                             "Sizes count the real space on the phone. “Android and other apps” is the rest of the used "
                             "space: the phone's settings manage it."), "tenue", ellissi=False, righe=3)
        nota.set_line_wrap(True)
        v.pack_start(nota, False, False, 0)
        sw.add(v)
        return sw

    def svuota(self, box):
        for f in box.get_children():
            box.remove(f)
            f.destroy()

    def mostra(self, dati, temporanei, pulibili):
        self.dati, self.temporanei, self.pulibili = dati, temporanei, pulibili
        cat = dati["categorie"]
        tot = dati["totale"] or sum(v["byte"] for v in cat.values())
        self.l_uso.set_text(T(f"{D.dimensione(dati['usato'], IT)} di {D.dimensione(tot, IT)} usati",
                              f"{D.dimensione(dati['usato'], IT)} of {D.dimensione(tot, IT)} used"))
        self.barra.metti(dati)
        self.svuota(self.legenda)
        for k, it, en, col, _i in D.CATEGORIE:
            if cat[k]["byte"] > 0:
                self.legenda.add(self.voce_legenda(col, it if IT else en))
        libero = "#3a3a3c" if self.colori["scuro"] else "#e3e3e6"   # come la parte vuota della barra
        self.legenda.add(self.voce_legenda(libero, T(f"Libero · {D.dimensione(dati['libero'], IT)}",
                                                                   f"Free · {D.dimensione(dati['libero'], IT)}")))
        self.legenda.show_all()
        # consigli
        self.svuota(self.consigli)
        cons = D.consigli(dati, temporanei, pulibili)
        for k, n, extra in cons:
            self.consigli.pack_start(self.carta_consiglio(k, n, extra), False, False, 0)
        self.consigli.show_all()
        self.l_consigli.set_visible(bool(cons))
        self.consigli.set_visible(bool(cons))
        # categorie, dalla più grande
        self.svuota(self.elenco)
        for k, it, en, col, icona in sorted(D.CATEGORIE, key=lambda c: -cat[c[0]]["byte"]):
            if cat[k]["byte"] <= 0:
                continue
            r = Gtk.ListBoxRow()
            r.chiave = k
            b = Gtk.Box(spacing=px(12))
            b.pack_start(Pallino(col, 14), False, False, 0)
            b.pack_start(C.etichetta(it if IT else en), True, True, 0)
            b.pack_start(C.etichetta(D.dimensione(cat[k]["byte"], IT), "arch-misura", xalign=1.0), False, False, 0)
            b.pack_start(C.immagine_icona("go-next-symbolic", 14), False, False, 0)
            r.add(b)
            self.elenco.add(r)
        self.elenco.show_all()
        quando = dati.get("quando")
        if not self.in_misura:
            self.stato.set_text(T(f"Misurato alle {time.strftime('%H:%M', time.localtime(quando))}",
                                  f"Measured at {time.strftime('%H:%M', time.localtime(quando))}") if quando else "")
        if self.aperta and self.pagine.get_visible_child_name() == "dettaglio":
            self.apri(self.aperta, scorri=False)

    def voce_legenda(self, colore, testo):
        b = Gtk.Box(spacing=px(6))
        b.pack_start(Pallino(colore, 10), False, False, 0)
        b.pack_start(C.etichetta(testo, "arch-legenda", ellissi=False), False, False, 0)
        return b

    def carta_consiglio(self, k, n, extra):
        carta = C.classe(Gtk.Box(spacing=px(14)), "arch-carta")
        carta.pack_start(C.immagine_icona(NOMI[k][3], 32), False, False, 0)
        testi = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(3))
        if k == "temporanei":
            titolo = T("Elimina i file temporanei grandi", "Delete large temporary files")
            sotto = T(f"{len(extra)} file che nessun programma sta usando occupano {D.dimensione(n, IT)} "
                      f"(il più grande: {os.path.basename(extra[0][0])}).",
                      f"{len(extra)} files no program is using take {D.dimensione(n, IT)} "
                      f"(the largest: {os.path.basename(extra[0][0])}).")
            testo_b, azione = T("Elimina…", "Delete…"), self.elimina_temporanei
        elif k == "cestino":
            titolo = T("Svuota il Cestino", "Empty the Trash")
            sotto = T(f"Il Cestino occupa {D.dimensione(n, IT)}.", f"The Trash takes {D.dimensione(n, IT)}.")
            testo_b, azione = T("Svuota…", "Empty…"), self.svuota_cestino
        else:
            titolo = T("Pulisci la cache", "Clean the cache")
            sotto = T(f"{D.dimensione(n, IT)} di copie che le app ricreano quando servono (le app aperte non si toccano).",
                      f"{D.dimensione(n, IT)} of copies apps recreate when needed (open apps are left alone).")
            testo_b, azione = T("Pulisci…", "Clean…"), self.pulisci_cache
        testi.pack_start(C.etichetta(titolo, "nome"), False, False, 0)
        testi.pack_start(C.etichetta(sotto, "sotto", ellissi=False, righe=2), False, False, 0)
        carta.pack_start(testi, True, True, 0)
        b = C.bottone(testo=testo_b, azione=azione, cls=None)
        b.set_valign(Gtk.Align.CENTER)
        carta.pack_end(b, False, False, 0)
        return carta

    # ============================================================================================ dettaglio
    def apri(self, k, scorri=True):
        if not self.dati:
            return
        self.aperta = k
        it, en, col, _i = NOMI[k]
        v = self.dettaglio
        self.svuota(v)
        dentro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(14))
        for m, n in (("start", 28), ("end", 28), ("top", 14), ("bottom", 24)):
            getattr(dentro, f"set_margin_{m}")(px(n))
        indietro = C.bottone("go-previous-symbolic", T("Archiviazione", "Storage"), azione=self.chiudi_dettaglio,
                             cls="lieve arch-indietro")
        indietro.set_halign(Gtk.Align.START)
        dentro.pack_start(indietro, False, False, 0)
        cat = self.dati["categorie"][k]
        testa = Gtk.Box(spacing=px(12))
        testa.pack_start(Pallino(col, 20), False, False, 0)
        testa.pack_start(C.etichetta(it if IT else en, "titolo"), False, False, 0)
        testa.pack_end(C.etichetta(D.dimensione(cat["byte"], IT), "arch-numero", xalign=1.0), False, False, 0)
        dentro.pack_start(testa, False, False, 0)
        dentro.pack_start(C.etichetta(SPIEGA[k], "sotto", ellissi=False, righe=3), False, False, 0)
        azione = {"cestino": (T("Svuota il Cestino…", "Empty the Trash…"), self.svuota_cestino),
                  "cache": (T("Pulisci la cache…", "Clean the cache…"), self.pulisci_cache),
                  "temporanei": (T("Elimina i file grandi non in uso…", "Delete large unused files…"),
                                 self.elimina_temporanei),
                  "android": (T("Apri le impostazioni del telefono", "Open the phone's settings"),
                              self.impostazioni_telefono)}.get(k)
        if azione and (k != "temporanei" or any(not t[2] for t in self.temporanei)) and \
                (k != "cache" or self.pulibili):
            b = C.bottone(testo=azione[0], azione=azione[1], cls=None)
            b.set_halign(Gtk.Align.START)
            dentro.pack_start(b, False, False, 0)
        parti = [p for p in cat["parti"] if p[0]]
        if parti:
            lista = C.classe(Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE), "arch-elenco")
            massimo = parti[0][1] or 1
            for p, n in parti[:60]:
                lista.add(self.riga_parte(p, n, massimo, col))
            dentro.pack_start(lista, False, False, 0)
            if len(parti) > 60:
                resto = sum(n for _p, n in parti[60:])
                dentro.pack_start(C.etichetta(T(f"e altre {len(parti) - 60} cartelle: {D.dimensione(resto, IT)}",
                                                f"and {len(parti) - 60} more folders: {D.dimensione(resto, IT)}"),
                                              "tenue"), False, False, 0)
        v.pack_start(dentro, False, False, 0)
        v.show_all()
        self.pagine.set_visible_child_name("dettaglio")
        sw = v.get_ancestor(Gtk.ScrolledWindow)
        if scorri and sw is not None:
            sw.get_vadjustment().set_value(0)

    def riga_parte(self, percorso, n, massimo, colore):
        r = Gtk.ListBoxRow(activatable=False)
        b = Gtk.Box(spacing=px(12))
        testi = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(4))
        nome = os.path.basename(percorso.rstrip("/")) or percorso
        testi.pack_start(C.etichetta(nome, "nome"), False, False, 0)
        testi.pack_start(C.etichetta(gentile(percorso), "tenue"), False, False, 0)
        quota = Gtk.DrawingArea()
        quota.set_size_request(-1, px(4))

        def disegna(w, cr):
            larga = w.get_allocated_width()
            C.rettangolo_tondo(cr, 0, 0, larga, w.get_allocated_height(), px(2))
            cr.set_source_rgba(*C.rgb(self.colori["TESTO"]), 0.08)
            cr.fill()
            C.rettangolo_tondo(cr, 0, 0, max(px(3), larga * n / massimo), w.get_allocated_height(), px(2))
            cr.set_source_rgb(*C.rgb(colore))
            cr.fill()
        quota.connect("draw", disegna)
        testi.pack_start(quota, False, False, 0)
        b.pack_start(testi, True, True, 0)
        b.pack_start(C.etichetta(D.dimensione(n, IT), "arch-misura", xalign=1.0), False, False, 0)
        if os.path.exists(percorso):
            mostra = C.bottone("folder-open-symbolic", suggerimento=T("Mostra in Esplora file", "Show in File Explorer"),
                               azione=lambda: C.mostra_nella_cartella(percorso))
            mostra.set_valign(Gtk.Align.CENTER)
            b.pack_start(mostra, False, False, 0)
        r.add(b)
        return r

    def chiudi_dettaglio(self):
        self.aperta = None
        self.pagine.set_visible_child_name("principale")

    # ============================================================================================ misura e azioni
    def misura(self):
        if self.in_misura:
            return
        self.in_misura = True
        self.rotella.start()
        self.b_aggiorna.set_sensitive(False)
        self.stato.set_text(T("Misuro lo spazio…", "Measuring the space…"))

        def lavoro():
            try:
                dati = D.analizza(lambda n: GLib.idle_add(lambda: self.stato.set_text(
                    T(f"Misuro lo spazio… ({n} di 4)", f"Measuring the space… ({n} of 4)")) and False))
                cartelle = [os.path.join(D.PREFIX, "tmp"), os.path.join(D.ROOTFS, "tmp")]
                temp = D.temporanei_grandi(cartelle)
                pul = D.cache_pulibili(dati)
            except Exception as e:   # noqa: BLE001 - una misura andata male non chiude la finestra
                print(f"tod-archivio: {type(e).__name__}: {e}", file=sys.stderr)
                dati, temp, pul = None, [], []
            GLib.idle_add(lambda: self.misurato(dati, temp, pul) and False)
        threading.Thread(target=lavoro, daemon=True).start()

    def misurato(self, dati, temp, pul):
        self.in_misura = False
        self.rotella.stop()
        self.b_aggiorna.set_sensitive(True)
        if dati is None:
            self.stato.set_text(T("Misura non riuscita", "Measuring failed"))
            return
        self.mostra(dati, temp, pul)

    def chiedi(self, titolo, testo, sì, pericoloso=True):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                              buttons=Gtk.ButtonsType.NONE, text=titolo)
        d.format_secondary_text(testo)
        d.add_button(T("Annulla", "Cancel"), Gtk.ResponseType.CANCEL)
        b = d.add_button(sì, Gtk.ResponseType.OK)
        if pericoloso:
            b.get_style_context().add_class("destructive-action")
        d.set_default_response(Gtk.ResponseType.CANCEL)
        try:
            import tod_avviso
            tod_avviso.come_macos(d)
        except Exception:   # noqa: BLE001 - senza il modulo resta la finestra normale
            pass
        r = d.run()
        d.destroy()
        return r == Gtk.ResponseType.OK

    def dopo(self, lavoro):
        """un'azione in sottofondo, poi una misura nuova"""
        self.stato.set_text(T("Libero lo spazio…", "Freeing space…"))
        self.rotella.start()

        def giro():
            try:
                lavoro()
            except Exception as e:   # noqa: BLE001
                print(f"tod-archivio: {type(e).__name__}: {e}", file=sys.stderr)
            GLib.idle_add(lambda: (self.rotella.stop(), self.misura()) and False)
        threading.Thread(target=giro, daemon=True).start()

    def elimina_temporanei(self):
        liberi = [t for t in self.temporanei if not t[2]]
        if not liberi:
            return
        n = sum(t[1] for t in liberi)
        elenco = "\n".join(f"• {os.path.basename(p)} ({D.dimensione(b, IT)})" for p, b, _a in liberi[:6])
        if not self.chiedi(T(f"Eliminare {len(liberi)} file temporanei?", f"Delete {len(liberi)} temporary files?"),
                           T(f"Si libera {D.dimensione(n, IT)}. Nessun programma li sta usando; non vanno nel Cestino.\n\n",
                             f"This frees {D.dimensione(n, IT)}. No program is using them; they don't go to the Trash.\n\n")
                           + elenco, T("Elimina", "Delete")):
            return

        def lavoro():
            in_uso = D.aperti()
            for p, _b, _a in liberi:
                if p not in in_uso:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        self.dopo(lavoro)

    def svuota_cestino(self):
        n = self.dati["categorie"]["cestino"]["byte"] if self.dati else 0
        if not self.chiedi(T("Svuotare il Cestino?", "Empty the Trash?"),
                           T(f"I file nel Cestino ({D.dimensione(n, IT)}) vengono eliminati per sempre: non si possono "
                             "più recuperare.", f"The files in the Trash ({D.dimensione(n, IT)}) are deleted forever: "
                             "they can't be recovered."), T("Svuota il Cestino", "Empty Trash")):
            return

        def lavoro():
            try:
                subprocess.run(["gio", "trash", "--empty"], timeout=600, check=True, capture_output=True)
            except (OSError, subprocess.SubprocessError):
                base = os.path.join(D.CASA, ".local/share/Trash")
                for sotto in ("files", "info"):
                    shutil.rmtree(os.path.join(base, sotto), ignore_errors=True)
                    os.makedirs(os.path.join(base, sotto), exist_ok=True)
        self.dopo(lavoro)

    def pulisci_cache(self):
        if not self.pulibili:
            return
        n = sum(b for _p, b in self.pulibili)
        nomi = ", ".join(os.path.basename(p) for p, _b in self.pulibili[:6])
        if not self.chiedi(T("Pulire la cache?", "Clean the cache?"),
                           T(f"Si libera {D.dimensione(n, IT)} ({nomi}). Le app ricreano le copie che servono; "
                             "la prima apertura può essere un po' più lenta.",
                             f"This frees {D.dimensione(n, IT)} ({nomi}). Apps recreate the copies they need; "
                             "the first start may be a little slower."), T("Pulisci", "Clean"), pericoloso=False):
            return
        pulibili = list(self.pulibili)
        self.dopo(lambda: [shutil.rmtree(p, ignore_errors=True) for p, _b in pulibili])

    def impostazioni_telefono(self):
        try:
            subprocess.Popen(["am", "start", "--user", "0", "-a", "android.settings.INTERNAL_STORAGE_SETTINGS"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def tasto(self, _w, ev):
        k = Gdk.keyval_name(ev.keyval)
        if k in ("Escape", "BackSpace") and self.pagine.get_visible_child_name() == "dettaglio":
            self.chiudi_dettaglio()
        elif k == "F5" or (k in ("r", "R") and ev.state & Gdk.ModifierType.CONTROL_MASK):
            self.misura()
        else:
            return False
        return True


class Archivio(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.termuxondex.Archivio", flags=Gio.ApplicationFlags.DEFAULT_FLAGS
                         if hasattr(Gio.ApplicationFlags, "DEFAULT_FLAGS") else Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        w = self.get_active_window() or Finestra(self)
        w.present()


if __name__ == "__main__":
    sys.exit(Archivio().run(sys.argv))
