#!/usr/bin/env python3
"""Scanner: scansiona documenti e foto con lo scanner Wi-Fi o USB (eSCL/AirScan, share/stampa/escl.py).

A sinistra lo scanner e le impostazioni (vetro o alimentatore, colori o grigi, risoluzione, foglio), a destra le
pagine scansionate, una dopo l'altra; alla fine "Salva…" in un PDF (o in JPEG, una per file). Start → Scanner;
Impostazioni → Stampanti e scanner. Gira in Termux (PyGObject, GTK 3) con l'ambiente della sessione.

  tod-scanner.py [--scanner CHIAVE]       la finestra
  tod-scanner.py elenco [--json]          gli scanner trovati (Wi-Fi, USB, aggiunti con l'indirizzo)
  tod-scanner.py aggiungi INDIRIZZO       uno scanner con l'indirizzo (192.168.1.50, http://…/eSCL)
  tod-scanner.py togli CHIAVE
  tod-scanner.py scansiona FILE [--scanner CHIAVE|URL] [--alimentatore] [--fronte-retro] [--grigi] [--dpi N]
                                [--foglio a4|lettera|vetro]
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # share/: tod_avviso
import escl  # noqa: E402
from escl import T  # noqa: E402

FONTI = {"Platen": ("Vetro", "Flatbed"), "Feeder": ("Alimentatore", "Document feeder")}
COLORI = {"RGB24": ("Colori", "Color"), "Grayscale8": ("Scala di grigi", "Grayscale")}
FOGLI = (("a4", "A4", "A4"), ("lettera", "Lettera", "Letter"), ("vetro", "Tutto il vetro", "Whole glass"))
DOVE = {"wifi": "Wi-Fi", "usb": "USB", "indirizzo": T("Indirizzo", "Address")}
ALTEZZA = 260   # le miniature delle pagine

CSS = b"""
.side { background-color: alpha(@theme_fg_color, 0.035); border-right: 1px solid alpha(@theme_fg_color, 0.08); }
.pane-title { font-size: 15pt; font-weight: bold; }
.group-title { font-weight: bold; opacity: 0.75; margin-top: 10px; }
.hint { opacity: 0.65; font-size: 9pt; }
.warn { color: #d13438; }
.pagina { background-color: alpha(@theme_fg_color, 0.04); border: 1px solid alpha(@theme_fg_color, 0.08);
          border-radius: 10px; padding: 10px; }
.togli { min-width: 26px; min-height: 26px; padding: 0; border-radius: 13px; }
.scansiona { padding: 8px 14px; font-weight: bold; }
.empty-title { font-size: 15pt; font-weight: bold; }
"""


def scegli_scanner(elenco, chiave, conf):
    """Lo scanner da mostrare: quello chiesto, l'ultimo usato, il primo."""
    for k in (chiave, conf.get("ultimo")):
        for s in elenco:
            if k and s["chiave"] == k:
                return s
    return elenco[0] if elenco else None


def dpi_vicino(lista, voluto):
    return min(lista, key=lambda d: (abs(d - voluto), -d)) if lista else voluto


# --- la finestra ------------------------------------------------------------------------------------------------

def finestra(scelto=None):
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")   # senza, Termux carica Gdk 4 e GTK 3 non parte
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk
    import tod_avviso   # avvisi come macOS (share/tod_avviso.py)

    GLib.set_prgname("tod-scanner")   # la barra riconosce la finestra (tod-scanner.desktop)

    class Scanner(Gtk.Window):
        def __init__(self):
            super().__init__(title=T("Scanner", "Scanner"))
            self.set_default_size(1000, 660)
            self.set_position(Gtk.WindowPosition.CENTER)
            self.set_icon_name("scanner")
            css = Gtk.CssProvider()
            css.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css,
                                                     Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.conf = escl.carica_conf()
            self.elenco, self.scanner, self.cap = [], None, None
            self.pagine, self.miniature, self.da_salvare = [], [], False
            self.annulla, self.lavora = threading.Event(), False
            self.connect("delete-event", self.chiudi)

            outer = Gtk.Box()
            self.add(outer)
            side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=16)
            sf = Gtk.Box()
            sf.get_style_context().add_class("side")
            sf.set_size_request(310, -1)
            sf.pack_start(side, True, True, 0)
            outer.pack_start(sf, False, False, 0)

            t = Gtk.Label(label=T("Scanner", "Scanner"), xalign=0)
            t.get_style_context().add_class("pane-title")
            side.pack_start(t, False, False, 0)
            h = Gtk.Box(spacing=6)
            self.cb_scanner = Gtk.ComboBoxText(hexpand=True)
            self.cb_scanner.connect("changed", self.su_scanner)
            h.pack_start(self.cb_scanner, True, True, 0)
            b = Gtk.Button(tooltip_text=T("Cerca di nuovo", "Search again"))
            b.add(Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON))
            b.connect("clicked", lambda *_: self.cerca())
            self.bt_cerca = b
            h.pack_start(b, False, False, 0)
            side.pack_start(h, False, False, 0)
            self.lb_stato = Gtk.Label(xalign=0, wrap=True, max_width_chars=34)
            self.lb_stato.get_style_context().add_class("hint")
            side.pack_start(self.lb_stato, False, False, 0)

            t = Gtk.Label(label=T("Impostazioni", "Settings"), xalign=0)
            t.get_style_context().add_class("group-title")
            side.pack_start(t, False, False, 0)
            self.griglia = Gtk.Grid(column_spacing=10, row_spacing=8)
            side.pack_start(self.griglia, False, False, 0)
            self.cb = {}
            for i, (k, testo) in enumerate((("fonte", T("Origine", "Source")), ("colore", T("Colore", "Color")),
                                            ("dpi", T("Risoluzione", "Resolution")), ("foglio", T("Foglio", "Page size")))):
                self.griglia.attach(Gtk.Label(label=testo, xalign=0), 0, i, 1, 1)
                self.cb[k] = Gtk.ComboBoxText(hexpand=True)
                self.griglia.attach(self.cb[k], 1, i, 1, 1)
            self.cb["fonte"].connect("changed", self.su_fonte)

            self.bt_scansiona = Gtk.Button(margin_top=14)
            self.bt_scansiona.get_style_context().add_class("suggested-action")
            self.bt_scansiona.get_style_context().add_class("scansiona")
            bh = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
            self.sp_scansiona = Gtk.Spinner()
            bh.pack_start(self.sp_scansiona, False, False, 0)
            self.lb_scansiona = Gtk.Label(label=T("Scansiona", "Scan"))
            bh.pack_start(self.lb_scansiona, False, False, 0)
            self.bt_scansiona.add(bh)
            self.bt_scansiona.connect("clicked", lambda *_: self.scansiona())
            side.pack_start(self.bt_scansiona, False, False, 0)
            self.bt_annulla = Gtk.Button(label=T("Annulla", "Cancel"))
            self.bt_annulla.connect("clicked", lambda *_: self.annulla.set())
            side.pack_start(self.bt_annulla, False, False, 0)
            self.lb_errore = Gtk.Label(xalign=0, wrap=True, max_width_chars=34)
            self.lb_errore.get_style_context().add_class("warn")
            side.pack_start(self.lb_errore, False, False, 0)

            b = Gtk.Button(label=T("Aggiungi con l'indirizzo…", "Add by address…"), relief=Gtk.ReliefStyle.NONE,
                           halign=Gtk.Align.START)
            b.connect("clicked", lambda *_: self.con_indirizzo())
            side.pack_end(b, False, False, 0)

            destra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            outer.pack_start(destra, True, True, 0)
            barra = Gtk.Box(spacing=8, margin=14)
            self.lb_pagine = Gtk.Label(label=T("Pagine", "Pages"), xalign=0)
            self.lb_pagine.get_style_context().add_class("pane-title")
            barra.pack_start(self.lb_pagine, True, True, 0)
            self.bt_salva = Gtk.Button(label=T("Salva…", "Save…"))
            self.bt_salva.get_style_context().add_class("suggested-action")
            self.bt_salva.connect("clicked", lambda *_: self.salva())
            barra.pack_end(self.bt_salva, False, False, 0)
            self.bt_ricomincia = Gtk.Button(label=T("Ricomincia", "Start over"))
            self.bt_ricomincia.connect("clicked", lambda *_: self.ricomincia())
            barra.pack_end(self.bt_ricomincia, False, False, 0)
            destra.pack_start(barra, False, False, 0)
            self.avviso = Gtk.Box(spacing=8, margin_start=14, margin_end=14)
            self.lb_avviso = Gtk.Label(xalign=0, wrap=True)
            self.avviso.pack_start(self.lb_avviso, True, True, 0)
            self.bt_apri = Gtk.Button(label=T("Apri", "Open"))
            self.avviso.pack_end(self.bt_apri, False, False, 0)
            destra.pack_start(self.avviso, False, False, 0)

            self.pila = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
            destra.pack_start(self.pila, True, True, 0)
            vuoto = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, valign=Gtk.Align.CENTER,
                            halign=Gtk.Align.CENTER, margin=30)
            img = Gtk.Image.new_from_icon_name("scanner", Gtk.IconSize.DIALOG)
            img.set_pixel_size(96)
            img.set_opacity(0.6)
            vuoto.pack_start(img, False, False, 0)
            t = Gtk.Label(label=T("Metti il foglio sul vetro e premi Scansiona", "Put the page on the glass and press Scan"),
                          justify=Gtk.Justification.CENTER, wrap=True)
            t.get_style_context().add_class("empty-title")
            vuoto.pack_start(t, False, False, 0)
            t = Gtk.Label(label=T("Le pagine si aggiungono qui una dopo l'altra; alla fine le salvi tutte in un PDF.",
                                  "Pages are added here one after another; at the end you save them all in a PDF."),
                          justify=Gtk.Justification.CENTER, wrap=True, max_width_chars=50)
            t.get_style_context().add_class("hint")
            vuoto.pack_start(t, False, False, 0)
            self.pila.add_named(vuoto, "vuoto")
            self.sw = Gtk.ScrolledWindow()
            self.sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START, margin=14,
                                    column_spacing=14, row_spacing=14, homogeneous=False, max_children_per_line=8)
            self.sw.add(self.flow)
            self.pila.add_named(self.sw, "pagine")

            self.show_all()
            self.bt_annulla.hide()   # (non no_show_all: in GTK 3 lascerebbe nascosto anche il testo dentro)
            self.avviso.hide()
            self.mostra_pagine()
            self.pronto(False)
            self.cerca()

        # --- scanner e impostazioni ---
        def pronto(self, si):
            for w in list(self.cb.values()) + [self.bt_scansiona]:
                w.set_sensitive(si and not self.lavora)

        def cerca(self):
            self.lb_stato.set_text(T("Cerco gli scanner…", "Looking for scanners…"))
            self.bt_cerca.set_sensitive(False)
            self.cb_scanner.set_sensitive(False)

            def lavoro():
                try:
                    trovati = escl.cerca()
                except Exception as e:   # mai una finestra ferma su "Cerco…"
                    trovati, err = [], str(e)
                else:
                    err = ""
                GLib.idle_add(self.mostra_elenco, trovati, err)
            threading.Thread(target=lavoro, daemon=True).start()

        def mostra_elenco(self, trovati, err=""):
            self.bt_cerca.set_sensitive(True)
            vecchio = self.scanner["chiave"] if self.scanner else scelto
            self.elenco = trovati
            s = scegli_scanner(trovati, vecchio, self.conf)
            self.cb_scanner.handler_block_by_func(self.su_scanner)   # la scelta la fa questa funzione, una volta sola
            self.cb_scanner.remove_all()
            for x in trovati:
                self.cb_scanner.append(x["chiave"], x["nome"])
            if s:
                self.cb_scanner.set_active_id(s["chiave"])
            self.cb_scanner.handler_unblock_by_func(self.su_scanner)
            self.cb_scanner.set_sensitive(bool(trovati))
            if not s:
                self.scanner, self.cap = None, None
                self.pronto(False)
                self.lb_stato.set_text(err or T("Nessuno scanner trovato. Accendi lo scanner Wi-Fi (nella stessa rete del "
                                                "telefono) o collega la multifunzione USB, poi premi ⟳.",
                                                "No scanner found. Turn on your Wi-Fi scanner (on the same network as the "
                                                "phone) or plug in the USB multifunction printer, then press ⟳."))
                return False
            if self.scanner is None or self.scanner["chiave"] != s["chiave"] or self.cap is None:
                self.su_scanner(self.cb_scanner)
            else:
                self.scanner = s
            return False

        def su_scanner(self, cb):
            s = next((x for x in self.elenco if x["chiave"] == cb.get_active_id()), None)
            if not s:
                return
            self.scanner, self.cap = s, None
            self.pronto(False)
            self.lb_errore.set_text("")
            self.lb_stato.set_text(T("Collegamento…", "Connecting…"))

            def lavoro():
                try:
                    cap, err = escl.capacita(s["url"]), ""
                except escl.Errore as e:
                    cap, err = None, str(e)
                GLib.idle_add(self.mostra_capacita, s, cap, err)
            threading.Thread(target=lavoro, daemon=True).start()

        def mostra_capacita(self, s, cap, err):
            if self.scanner is not s:
                return False
            dove = " · ".join(x for x in (DOVE.get(s["dove"], ""), s.get("ip", "")) if x)
            if not cap:
                self.lb_stato.set_text(f"{dove}\n{err}")
                return False
            self.cap = cap
            self.lb_stato.set_text(" · ".join(x for x in (cap["modello"] if cap["modello"] != s["nome"] else "", dove) if x))
            voci = []
            for f in ("Platen", "Feeder"):
                if f in cap["fonti"]:
                    voci.append((f, T(*FONTI[f])))
                    if f == "Feeder" and cap["fonti"][f].get("fronte_retro"):
                        voci.append(("Duplex", T("Alimentatore, fronte e retro", "Document feeder, both sides")))
            self.sostituisci("fonte", voci, self.conf.get("fonte", "Platen"))
            self.su_fonte()
            self.pronto(True)
            return False

        def sostituisci(self, k, voci, attiva):
            c = self.cb[k]
            if k == "fonte":   # su_fonte la chiama chi riempie, una volta sola
                c.handler_block_by_func(self.su_fonte)
            c.remove_all()
            for i, testo in voci:
                c.append(i, testo)
            if not c.set_active_id(attiva) and voci:
                c.set_active(0)
            if k == "fonte":
                c.handler_unblock_by_func(self.su_fonte)

        def caps_fonte(self):
            f = self.cb["fonte"].get_active_id() or "Platen"
            return self.cap["fonti"].get("Feeder" if f == "Duplex" else f) if self.cap else None

        def su_fonte(self, *_):
            c = self.caps_fonte()
            if not c:
                return
            colori = [(k, T(*COLORI[k])) for k in ("RGB24", "Grayscale8") if k in c["colori"]] or \
                [(k, k) for k in c["colori"] if k != "BlackAndWhite1"] or [("RGB24", T(*COLORI["RGB24"]))]
            self.sostituisci("colore", colori, self.conf.get("colore", "RGB24"))
            lista = [d for d in c["dpi"] if d <= 1200]
            self.sostituisci("dpi", [(str(d), f"{d} dpi") for d in lista],
                             str(dpi_vicino(lista, int(self.conf.get("dpi", 300)))))
            fogli = [(k, T(it, en)) for k, it, en in FOGLI if k != "vetro" or self.cb["fonte"].get_active_id() == "Platen"]
            self.sostituisci("foglio", fogli, self.conf.get("foglio", "a4"))

        # --- scansione ---
        def scansiona(self):
            if not (self.scanner and self.cap) or self.lavora:
                return
            fonte_id = self.cb["fonte"].get_active_id() or "Platen"
            fonte = "Feeder" if fonte_id == "Duplex" else fonte_id
            colore = self.cb["colore"].get_active_id() or "RGB24"
            dpi = int(self.cb["dpi"].get_active_id() or 300)
            foglio = self.cb["foglio"].get_active_id() or "a4"
            self.conf = escl.carica_conf()
            self.conf.update(ultimo=self.scanner["chiave"], fonte=fonte_id, colore=colore, dpi=dpi, foglio=foglio)
            try:
                escl.salva_conf(self.conf)
            except OSError:
                pass
            xml = escl.impostazioni_xml(fonte, colore, dpi, *escl.area(self.caps_fonte(), foglio),
                                        versione=self.cap["versione"], fronte_retro=fonte_id == "Duplex")
            self.lavora = True
            self.annulla.clear()
            self.pronto(False)
            self.lb_errore.set_text("")
            self.sp_scansiona.start()
            self.lb_scansiona.set_text(T("Scansione…", "Scanning…"))
            self.bt_annulla.show()
            url = self.scanner["url"]

            def lavoro():
                err = ""
                try:
                    escl.scansiona(url, xml, lambda d: GLib.idle_add(self.aggiungi, d, dpi), annulla=self.annulla,
                                   una_sola=fonte == "Platen")
                except escl.Errore as e:
                    err = str(e)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                GLib.idle_add(self.finita, err)
            threading.Thread(target=lavoro, daemon=True).start()

        def finita(self, err):
            self.lavora = False
            self.sp_scansiona.stop()
            self.lb_scansiona.set_text(T("Scansiona", "Scan"))
            self.bt_annulla.hide()
            self.pronto(self.cap is not None)
            if err and not self.annulla.is_set():
                self.lb_errore.set_text(err)
            return False

        # --- pagine ---
        def miniatura(self, dati):
            caricatore = GdkPixbuf.PixbufLoader()   # il JPEG si decodifica già piccolo (veloce anche a 600 dpi)
            caricatore.connect("size-prepared", lambda c, l, a: c.set_size(max(1, l * ALTEZZA // max(a, 1)), ALTEZZA))
            try:
                caricatore.write(dati)
                caricatore.close()
                return caricatore.get_pixbuf()
            except GLib.Error:
                try:
                    caricatore.close()
                except GLib.Error:
                    pass
                return None

        def aggiungi(self, dati, dpi):
            self.pagine.append((dati, dpi))
            self.miniature.append(self.miniatura(dati))
            self.da_salvare = True
            self.avviso.hide()
            self.mostra_pagine()
            adj = self.sw.get_vadjustment()
            GLib.idle_add(lambda: adj.set_value(adj.get_upper()) and False)
            return False

        def mostra_pagine(self):
            for c in self.flow.get_children():
                self.flow.remove(c)
            for i, pb in enumerate(self.miniature):
                v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                v.get_style_context().add_class("pagina")
                o = Gtk.Overlay()
                img = Gtk.Image.new_from_pixbuf(pb) if pb else Gtk.Image.new_from_icon_name("image-x-generic", Gtk.IconSize.DIALOG)
                if not pb:
                    img.set_size_request(ALTEZZA * 7 // 10, ALTEZZA)
                o.add(img)
                b = Gtk.Button(tooltip_text=T("Togli questa pagina", "Remove this page"), halign=Gtk.Align.END,
                               valign=Gtk.Align.START, margin=4)
                b.get_style_context().add_class("togli")
                b.get_style_context().add_class("circular")
                b.add(Gtk.Image.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU))
                b.connect("clicked", lambda _b, n=i: self.togli(n))
                o.add_overlay(b)
                v.pack_start(o, False, False, 0)
                lb = Gtk.Label(label=T(f"Pagina {i + 1}", f"Page {i + 1}"))
                lb.get_style_context().add_class("hint")
                v.pack_start(lb, False, False, 0)
                self.flow.add(v)
            self.flow.show_all()
            n = len(self.pagine)
            if n == 0:
                self.lb_pagine.set_text(T("Pagine", "Pages"))
            elif n == 1:
                self.lb_pagine.set_text(T("1 pagina", "1 page"))
            else:
                self.lb_pagine.set_text(T(f"{n} pagine", f"{n} pages"))
            self.pila.set_visible_child_name("pagine" if n else "vuoto")
            self.bt_salva.set_sensitive(n > 0)
            self.bt_ricomincia.set_sensitive(n > 0)

        def togli(self, i):
            if 0 <= i < len(self.pagine):
                del self.pagine[i]
                del self.miniature[i]
                self.da_salvare = bool(self.pagine)
                self.mostra_pagine()

        def chiedi(self, testo, sotto, ok, distruttivo=True):
            d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                  buttons=Gtk.ButtonsType.NONE, text=testo)
            d.format_secondary_text(sotto)
            d.add_button(T("Annulla", "Cancel"), Gtk.ResponseType.CANCEL)
            b = d.add_button(ok, Gtk.ResponseType.OK)
            b.get_style_context().add_class("destructive-action" if distruttivo else "suggested-action")
            r = tod_avviso.come_macos(d).run() == Gtk.ResponseType.OK
            d.destroy()
            return r

        def ricomincia(self):
            if self.da_salvare and not self.chiedi(T("Togliere tutte le pagine?", "Remove all pages?"),
                                                   T("Non le hai ancora salvate.", "You haven't saved them yet."),
                                                   T("Togli tutte", "Remove all")):
                return
            self.pagine, self.miniature, self.da_salvare = [], [], False
            self.avviso.hide()
            self.mostra_pagine()

        def salva(self):
            if not self.pagine:
                return
            d = Gtk.FileChooserDialog(title=T("Salva la scansione", "Save the scan"), transient_for=self,
                                      action=Gtk.FileChooserAction.SAVE)
            d.add_button(T("Annulla", "Cancel"), Gtk.ResponseType.CANCEL)
            d.add_button(T("Salva", "Save"), Gtk.ResponseType.OK).get_style_context().add_class("suggested-action")
            d.set_do_overwrite_confirmation(True)
            filtri = {}
            for est, testo in (("pdf", T("PDF: tutte le pagine in un file", "PDF: all pages in one file")),
                               ("jpg", T("JPEG: un'immagine per pagina", "JPEG: one image per page"))):
                f = Gtk.FileFilter()
                f.set_name(testo)
                f.add_pattern(f"*.{est}")
                if est == "jpg":
                    f.add_pattern("*.jpeg")
                d.add_filter(f)
                filtri[est] = f
            cartella = self.conf.get("cartella") or GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS) \
                or GLib.get_home_dir()
            if os.path.isdir(cartella):
                d.set_current_folder(cartella)
            d.set_current_name(T("Scansione", "Scan") + time.strftime(" %Y-%m-%d %H.%M") + ".pdf")

            def su_filtro(*_):   # il tipo scelto cambia anche l'estensione del nome
                nome = d.get_current_name() or ""
                est = "jpg" if d.get_filter() is filtri["jpg"] else "pdf"
                base = os.path.splitext(nome)[0] if nome.lower().endswith((".pdf", ".jpg", ".jpeg")) else nome
                d.set_current_name(f"{base}.{est}")
            d.connect("notify::filter", su_filtro)
            percorso = d.get_filename() if d.run() == Gtk.ResponseType.OK else None
            jpeg = d.get_filter() is filtri["jpg"]
            d.destroy()
            if not percorso:
                return
            if jpeg and not percorso.lower().endswith((".jpg", ".jpeg")):
                percorso += ".jpg"
            try:
                scritti = escl.salva(self.pagine, percorso)
            except (OSError, escl.Errore) as e:
                self.mostra_avviso(T(f"Non salvata: {e}", f"Not saved: {e}"), None)
                return
            self.da_salvare = False
            self.conf = escl.carica_conf()
            self.conf["cartella"] = os.path.dirname(scritti[0])
            try:
                escl.salva_conf(self.conf)
            except OSError:
                pass
            testo = os.path.basename(scritti[0]) if len(scritti) == 1 else \
                T(f"{len(scritti)} immagini in {os.path.dirname(scritti[0])}", f"{len(scritti)} images in {os.path.dirname(scritti[0])}")
            self.mostra_avviso(T(f"Salvata: {testo}", f"Saved: {testo}"),
                               scritti[0] if len(scritti) == 1 else os.path.dirname(scritti[0]))

        def mostra_avviso(self, testo, apri):
            self.lb_avviso.set_text(testo)
            try:
                self.bt_apri.disconnect_by_func(self.apri)
            except TypeError:
                pass
            self.bt_apri.set_visible(bool(apri))
            if apri:
                self.bt_apri.connect("clicked", self.apri, apri)
            self.avviso.show()

        def apri(self, _b, percorso):
            try:
                Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(percorso).get_uri(), None)
            except GLib.Error as e:
                self.mostra_avviso(str(e), None)

        # --- scanner con l'indirizzo ---
        def con_indirizzo(self):
            d = Gtk.Dialog(title=T("Aggiungi uno scanner", "Add a scanner"), transient_for=self, modal=True)
            d.add_button(T("Annulla", "Cancel"), Gtk.ResponseType.CANCEL)
            ok = d.add_button(T("Aggiungi", "Add"), Gtk.ResponseType.OK)
            ok.get_style_context().add_class("suggested-action")
            d.set_default_response(Gtk.ResponseType.OK)
            box = d.get_content_area()
            box.set_spacing(10)
            box.set_border_width(18)
            box.pack_start(Gtk.Label(label=T("L'indirizzo dello scanner nella tua rete (lo trovi nel menu della stampante, "
                                             "alla voce Wi-Fi o Rete):", "The scanner's address on your network (see the "
                                             "printer's menu, under Wi-Fi or Network):"), xalign=0, wrap=True,
                                     max_width_chars=48), False, False, 0)
            voce = Gtk.Entry(placeholder_text="192.168.1.50", activates_default=True)
            box.pack_start(voce, False, False, 0)
            msg = Gtk.Label(xalign=0, wrap=True, max_width_chars=48)
            box.pack_start(msg, False, False, 0)
            vivo = [True]

            def fatto(nuovo, err):
                if not vivo[0]:
                    return False
                if nuovo:
                    vivo[0] = False
                    d.destroy()
                    self.conf["ultimo"] = nuovo["chiave"]   # (scegli_scanner: quello appena aggiunto)
                    self.scanner = None
                    self.mostra_elenco([s for s in self.elenco if s["url"] != nuovo["url"]] + [nuovo])
                    return False
                msg.get_style_context().add_class("warn")
                msg.set_text(err)
                d.set_response_sensitive(Gtk.ResponseType.OK, True)
                return False

            def risposta(_d, r):
                if r != Gtk.ResponseType.OK:
                    vivo[0] = False
                    d.destroy()
                    return
                msg.get_style_context().remove_class("warn")
                msg.set_text(T("Provo…", "Trying…"))
                d.set_response_sensitive(Gtk.ResponseType.OK, False)

                def lavoro(t=voce.get_text()):
                    try:
                        nuovo, err = escl.aggiungi_indirizzo(t), ""
                    except escl.Errore as e:
                        nuovo, err = None, str(e)
                    GLib.idle_add(fatto, nuovo, err)
                threading.Thread(target=lavoro, daemon=True).start()
            d.connect("response", risposta)
            d.show_all()

        def chiudi(self, *_):
            if self.da_salvare and not self.chiedi(T("Chiudere senza salvare?", "Close without saving?"),
                                                   T("Le pagine scansionate andranno perse.", "The scanned pages will be lost."),
                                                   T("Chiudi senza salvare", "Close without saving")):
                return True
            self.annulla.set()
            Gtk.main_quit()
            return False

    Scanner()
    Gtk.main()
    return 0


# --- i comandi --------------------------------------------------------------------------------------------------

def _opzione(args, nome, d=None):
    if nome in args:
        i = args.index(nome)
        if i + 1 < len(args):
            v = args[i + 1]
            del args[i:i + 2]
            return v
    return d


def _segno(args, nome):
    if nome in args:
        args.remove(nome)
        return True
    return False


def scansiona_cli(args):
    """tod scanner scansiona FILE …: una scansione dal terminale (e nelle prove)."""
    chi = _opzione(args, "--scanner")
    dpi = int(_opzione(args, "--dpi", "300"))
    foglio = _opzione(args, "--foglio", "a4")
    alimentatore, fronte_retro, grigi = _segno(args, "--alimentatore"), _segno(args, "--fronte-retro"), _segno(args, "--grigi")
    if not args:
        print(T("uso: tod scanner scansiona FILE.pdf|FILE.jpg [--scanner CHIAVE|URL] [--alimentatore] [--fronte-retro] "
                "[--grigi] [--dpi N] [--foglio a4|lettera|vetro]", "usage: tod scanner scansiona FILE.pdf|FILE.jpg …"),
              file=sys.stderr)
        return 2
    if chi and "://" in chi:
        url = chi
    else:
        s = scegli_scanner(escl.cerca(rete=not os.environ.get("TOD_SCANNER_SENZA_RETE")), chi, escl.carica_conf())
        if not s or (chi and s["chiave"] != chi):
            print(T("Nessuno scanner trovato", "No scanner found"), file=sys.stderr)
            return 1
        url = s["url"]
    try:
        cap = escl.capacita(url)
        fonte = "Feeder" if alimentatore or fronte_retro else "Platen"
        c = cap["fonti"].get(fonte)
        if not c:
            raise escl.Errore(T("Questo scanner non ha l'alimentatore" if fonte == "Feeder" else "Questo scanner non ha il vetro",
                                "This scanner has no document feeder" if fonte == "Feeder" else "This scanner has no flatbed"))
        dpi = dpi_vicino(c["dpi"], dpi)
        colore = "Grayscale8" if grigi and "Grayscale8" in c["colori"] else "RGB24"
        xml = escl.impostazioni_xml(fonte, colore, dpi, *escl.area(c, foglio), versione=cap["versione"],
                                    fronte_retro=fronte_retro and c.get("fronte_retro", False))
        pagine = []
        escl.scansiona(url, xml, lambda d: pagine.append((d, dpi)), una_sola=fonte == "Platen",
                       pausa=float(os.environ.get("TOD_SCANNER_PAUSA", "1")))
        for p in escl.salva(pagine, args[0]):
            print(p)
    except (escl.Errore, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def principale(argv):
    args = argv[1:]
    js = _segno(args, "--json")
    if not args or args[0].startswith("--"):
        return finestra(_opzione(args, "--scanner"))
    cmd, args = args[0], args[1:]
    if cmd in ("elenco", "cerca", "list"):
        trovati = escl.cerca(rete=not os.environ.get("TOD_SCANNER_SENZA_RETE"))
        if js:
            print(json.dumps(trovati, ensure_ascii=False))
        else:
            for s in trovati:
                print(f"{s['nome']} · {DOVE.get(s['dove'], s['dove'])} · {s['url']}  [{s['chiave']}]")
            if not trovati:
                print(T("nessuno scanner trovato", "no scanner found"))
        return 0
    if cmd in ("aggiungi", "add") and args:
        try:
            s = escl.aggiungi_indirizzo(args[0])
        except escl.Errore as e:
            print(e, file=sys.stderr)
            return 1
        print(json.dumps(s, ensure_ascii=False) if js else f"{s['nome']} · {s['url']}")
        return 0
    if cmd in ("togli", "remove") and args:
        return 0 if escl.togli(args[0]) else 1
    if cmd in ("scansiona", "scan"):
        return scansiona_cli(args)
    print("uso: tod scanner [elenco|aggiungi INDIRIZZO|togli CHIAVE|scansiona FILE …]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(principale(sys.argv))
