#!/data/data/com.termux/files/usr/bin/python3
"""tod-attivita.py — Gestione attività come quella di Windows 11 (12 novembre 2026).

Pietro: Cattura schermo e Gestione attività di XFCE "hanno l'aspetto Linux": le rifacciamo nello stile delle nostre app
(Foto, Lettore: share/media/comune.py, colori e accento del sistema). Due pagine:
  Processi     le app con una finestra (i loro processi dentro, aprendo la riga) e i processi in background, con
               processore e memoria; "Termina attività" (Canc). I pezzi che tengono in piedi il desktop non si terminano.
  Prestazioni  processore e memoria negli ultimi due minuti.
I dati: share/attivita_dati.py (senza grafica). Android mostra solo i processi del desktop, non le app del telefono.
"""
import os
import signal
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "media"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comune as C  # noqa: E402
from comune import Gdk, Gio, GLib, Gtk, Pango, px, T  # noqa: E402
import attivita_dati as D  # noqa: E402

GLib.set_prgname("tod-attivita")   # la barra riconosce la finestra (tod-attivita.desktop)
IT = C.L.ITALIAN if hasattr(C.L, "ITALIAN") else os.environ.get("LANGUAGE", os.environ.get("LANG", "it")).startswith("it")
OGNI = 2   # secondi tra un aggiornamento e l'altro
STORIA = 60   # punti dei grafici (due minuti)


def pannelli():
    """Catalogo delle app, finestre aperte e chiusura: le stesse funzioni del pannello Batteria (tod-pannelli.py)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("tod_pannelli", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                                "tod-pannelli.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


CSS = """
.w11 .att-testa { padding: 0; }
.w11 treeview.view { padding: 1px 0; }
.w11 treeview.view.gruppo-att { font-weight: 600; }
.w11 .att-scheda { background-color: CARTA; border: 1px solid LINEA; border-radius: 8px; padding: 16px 18px; }
.w11 .att-numero { font-size: 22pt; font-weight: 600; }
.w11 .att-nota { color: TENUE; font-size: 9pt; }
"""


class Grafico(Gtk.DrawingArea):
    """una linea con l'area sotto nell'accento, griglia leggera, valori da 0 a 100"""

    def __init__(self, finestra):
        super().__init__()
        self.finestra, self.valori = finestra, []
        self.set_size_request(-1, px(150))
        self.connect("draw", self.disegna)

    def metti(self, valori):
        self.valori = list(valori)
        self.queue_draw()

    def disegna(self, _w, cr):
        c = self.finestra.colori
        w, h = self.get_allocated_width(), self.get_allocated_height()
        r, g, b = C.rgb(c["ACCENTO"])
        t = C.rgb(c["TESTO"])
        cr.set_line_width(1)
        cr.set_source_rgba(*t, 0.08)
        for i in range(5):
            y = int(h * i / 4) + 0.5
            cr.move_to(0, y)
            cr.line_to(w, y)
        cr.stroke()
        cr.rectangle(0.5, 0.5, w - 1, h - 1)
        cr.set_source_rgba(*t, 0.14)
        cr.stroke()
        if len(self.valori) < 2:
            return False
        passo = w / (STORIA - 1)
        x0 = w - passo * (len(self.valori) - 1)
        punti = [(x0 + i * passo, h - 2 - (h - 4) * max(0, min(100, v)) / 100) for i, v in enumerate(self.valori)]
        cr.move_to(punti[0][0], h)
        for x, y in punti:
            cr.line_to(x, y)
        cr.line_to(punti[-1][0], h)
        cr.close_path()
        cr.set_source_rgba(r, g, b, 0.16)
        cr.fill()
        cr.set_line_width(px(1.5))
        cr.move_to(*punti[0])
        for x, y in punti[1:]:
            cr.line_to(x, y)
        cr.set_source_rgb(r, g, b)
        cr.stroke()
        return False


class Finestra(Gtk.ApplicationWindow):
    COL = ("icona", "nome", "cpu_t", "mem_t", "tipo", "chiave", "cpu", "kb", "pid")

    def __init__(self, app):
        super().__init__(application=app, title=T("Gestione attività", "Task Manager"))
        C.imposta_scala()
        w, h = px(980), px(660)
        mon = Gdk.Display.get_default().get_primary_monitor() or Gdk.Display.get_default().get_monitor(0)
        if mon:
            a = mon.get_workarea()
            w, h = min(w, int(a.width * 0.9)), min(h, int(a.height * 0.9))
        self.set_default_size(w, h)
        self.set_icon_name("utilities-system-monitor")
        C.classe(self, "w11")
        self.colori = C.colori()
        self.stile = C.Stile(CSS, self.tema_cambiato)
        self.P = pannelli()
        self.catalogo = self.P.Catalogo()
        self.prima = None
        self.ultimo = None          # (campione, cpu, app, resto, finestre)
        self.ordine, self.discendente = "kb", True
        self.aperte, self.scelta = set(), None
        self.filtro = ""
        self.storia_cpu, self.storia_mem = [], []
        self._in_lettura = False
        radice = Gtk.Box()
        self.add(radice)
        self.nav = C.Nav(self.scegli, larga=220)
        self.nav.voce("processi", T("Processi", "Processes"), "view-list-symbolic")
        self.nav.voce("prestazioni", T("Prestazioni", "Performance"), "utilities-system-monitor-symbolic")
        radice.pack_start(self.nav, False, False, 0)
        self.pagine = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        strato = C.classe(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), "strato")
        strato.pack_start(self.pagine, True, True, 0)
        radice.pack_start(strato, True, True, 0)
        self.pagine.add_named(self.costruisci_processi(), "processi")
        self.pagine.add_named(self.costruisci_prestazioni(), "prestazioni")
        self.connect("key-press-event", self.tasto)
        self.connect("destroy", lambda *_: GLib.source_remove(self._giro))
        self.show_all()
        self.scegli("processi")
        self._giro = GLib.timeout_add_seconds(OGNI, self.leggi)
        self.leggi()

    def tema_cambiato(self):
        self.colori = C.colori()
        self.g_cpu.queue_draw()
        self.g_mem.queue_draw()

    def scegli(self, k):
        self.nav.segna(k)
        self.pagine.set_visible_child_name(k)

    # ========================================================================================== processi
    def costruisci_processi(self):
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        testa = Gtk.Box(spacing=px(10))
        for m, n in (("start", 24), ("end", 16), ("top", 18), ("bottom", 10)):
            getattr(testa, f"set_margin_{m}")(px(n))
        testa.pack_start(C.etichetta(T("Processi", "Processes"), "titolo"), False, False, 0)
        self.cerca = Gtk.SearchEntry(placeholder_text=T("Cerca un'app o un processo", "Search for an app or process"))
        self.cerca.set_size_request(px(260), -1)
        self.cerca.set_valign(Gtk.Align.CENTER)
        self.cerca.connect("search-changed", lambda e: (setattr(self, "filtro", e.get_text().strip().lower()), self.riempi()))
        self.b_termina = C.bottone("process-stop-symbolic", T("Termina attività", "End task"), azione=self.termina,
                                   cls="lieve")
        self.b_termina.set_valign(Gtk.Align.CENTER)
        self.b_termina.set_sensitive(False)
        testa.pack_end(self.b_termina, False, False, 0)
        testa.pack_end(self.cerca, False, False, 0)
        v.pack_start(testa, False, False, 0)
        self.store = Gtk.TreeStore(str, str, str, str, str, str, float, int, int)
        self.vista = Gtk.TreeView(model=self.store, headers_visible=True, enable_search=False)
        self.vista.get_selection().connect("changed", self.selezione)
        self.vista.connect("row-expanded", lambda _v, it, _p: self.aperte.add(self.store[it][5]))
        self.vista.connect("row-collapsed", lambda _v, it, _p: self.aperte.discard(self.store[it][5]))
        c = Gtk.TreeViewColumn(T("Nome", "Name"))
        c.set_expand(True)
        ri = Gtk.CellRendererPixbuf()
        ri.set_property("stock-size", Gtk.IconSize.SMALL_TOOLBAR)
        ri.set_padding(px(4), 0)
        c.pack_start(ri, False)
        c.add_attribute(ri, "icon-name", 0)
        rt = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        rt.set_padding(px(6), px(2))
        c.pack_start(rt, True)
        c.add_attribute(rt, "text", 1)
        c.set_cell_data_func(rt, self._nome_cella)
        c.set_clickable(True)
        c.connect("clicked", lambda *_: self.ordina("nome"))
        self.vista.append_column(c)
        self.c_cpu = self.colonna_numero(T("Processore", "CPU"), 2, 6, "cpu", 60.0)
        self.c_mem = self.colonna_numero(T("Memoria", "Memory"), 3, 7, "kb", 1024 * 1024.0)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.add(self.vista)
        sw.set_margin_start(px(12))
        sw.set_margin_end(px(12))
        v.pack_start(sw, True, True, 0)
        nota = C.etichetta(T("Qui ci sono le app e i servizi del desktop. Le app del telefono le gestisce Android.",
                             "This shows desktop apps and services. Phone apps are managed by Android."), "att-nota")
        for m, n in (("start", 24), ("top", 8), ("bottom", 12)):
            getattr(nota, f"set_margin_{m}")(px(n))
        v.pack_start(nota, False, False, 0)
        return v

    def colonna_numero(self, titolo, testo, valore, chiave, pieno):
        r = Gtk.CellRendererText(xalign=1.0)
        r.set_padding(px(14), px(2))
        c = Gtk.TreeViewColumn(titolo, r, text=testo)
        c.set_min_width(px(130))
        c.set_alignment(1.0)
        c.set_clickable(True)
        c.connect("clicked", lambda *_: self.ordina(chiave))
        c.set_cell_data_func(r, self._numero_cella, (valore, pieno))
        self.vista.append_column(c)
        return c

    def _nome_cella(self, _c, cella, modello, it, _d):
        cella.set_property("weight", 600 if modello[it][4] == "gruppo" else 400)

    def _numero_cella(self, _c, cella, modello, it, dati):
        """come Windows: lo sfondo del numero si colora di più quanto più consuma (qui nell'accento)"""
        i, pieno = dati
        if modello[it][4] == "gruppo":
            cella.set_property("cell-background-set", False)
            return
        quota = min(1.0, modello[it][i] / pieno)
        r, g, b = C.rgb(self.colori["ACCENTO"])
        col = Gdk.RGBA(r, g, b, 0.04 + 0.30 * quota)
        cella.set_property("cell-background-rgba", col)
        cella.set_property("cell-background-set", True)

    def ordina(self, chiave):
        if self.ordine == chiave:
            self.discendente = not self.discendente
        else:
            self.ordine, self.discendente = chiave, chiave != "nome"
        self.riempi()

    # --- lettura (in un filo: xprop e /proc di cento processi) ------------------------------------------------------
    def leggi(self):
        if self._in_lettura:
            return True
        self._in_lettura = True

        def lavoro():
            try:
                camp = D.Campione()
                fin = self.P.finestre_aperte()
            except Exception as e:   # noqa: BLE001 - un giro andato storto non chiude la finestra
                print(f"tod-attivita: {type(e).__name__}: {e}", file=sys.stderr)
                camp, fin = None, []
            GLib.idle_add(lambda: self.letto(camp, fin) and False)
        threading.Thread(target=lavoro, daemon=True).start()
        return True

    def letto(self, camp, fin):
        self._in_lettura = False
        if camp is None:
            return
        uso = D.cpu(self.prima, camp)
        self.prima = camp
        radici, app = {}, {}
        for _xid, pid, ist, cl in fin:
            if pid not in camp.processi:
                continue
            info = self.catalogo.cerca(ist, cl)
            chiave = info.get_id() if info is not None else (cl or ist or str(pid)).lower()
            if chiave not in app:
                icona = info.get_icon().to_string() if info is not None and info.get_icon() else "application-x-executable"
                app[chiave] = {"nome": info.get_display_name() if info else (cl or ist), "icona": icona, "radici": []}
            app[chiave]["radici"].append(pid)
            radici.setdefault(pid, chiave)
        per_app, resto = D.assegna(camp.processi, radici)
        for k, a in app.items():
            a["pid"] = per_app.get(k, [])
        self.ultimo = (camp, uso, app, resto)
        self.riempi()
        tot_cpu = sum(uso.values())
        self.storia_cpu = (self.storia_cpu + [tot_cpu])[-STORIA:]
        usata = camp.mem_totale - camp.mem_libera
        self.storia_mem = (self.storia_mem + [usata * 100 / camp.mem_totale if camp.mem_totale else 0])[-STORIA:]
        self.aggiorna_prestazioni(camp, uso, tot_cpu, usata)

    # --- elenco -----------------------------------------------------------------------------------------------------
    def chiave_ordine(self, voce):
        if self.ordine == "nome":
            return voce["nome"].lower()
        return voce[self.ordine]

    def riempi(self):
        if self.ultimo is None:
            return
        camp, uso, app, resto = self.ultimo
        f = self.filtro
        voci_app = []
        for k, a in app.items():
            figli = [{"nome": D.nome_leggibile(camp.processi[p], IT), "pid": p, "cpu": uso.get(p, 0.0),
                      "kb": camp.processi[p].kb} for p in a["pid"] if p in camp.processi]
            voce = {"nome": a["nome"], "icona": a["icona"], "chiave": "app:" + k, "pid": 0, "figli": figli,
                    "cpu": sum(x["cpu"] for x in figli), "kb": sum(x["kb"] for x in figli)}
            if not f or f in voce["nome"].lower() or any(f in x["nome"].lower() for x in figli):
                voci_app.append(voce)
        voci_bg = []
        for p in resto:
            pr = camp.processi[p]
            nome = D.nome_leggibile(pr, IT)
            if f and f not in nome.lower() and f not in pr.nome.lower():
                continue
            voci_bg.append({"nome": nome, "icona": "application-x-executable", "chiave": f"pid:{p}", "pid": p, "figli": [],
                            "cpu": uso.get(p, 0.0), "kb": pr.kb})
        for elenco in (voci_app, voci_bg):
            elenco.sort(key=self.chiave_ordine, reverse=self.discendente)
        scelta = self.scelta
        adj = self.vista.get_parent().get_vadjustment() if self.vista.get_parent() else None
        pos = adj.get_value() if adj else 0
        self.vista.freeze_child_notify()
        self.store.clear()
        da_scegliere = None
        for titolo, elenco, chiave in ((T(f"App ({len(voci_app)})", f"Apps ({len(voci_app)})"), voci_app, "g:app"),
                                       (T(f"Processi in background ({len(voci_bg)})",
                                          f"Background processes ({len(voci_bg)})"), voci_bg, "g:bg")):
            if not elenco:
                continue
            g = self.store.append(None, ["", titolo, "", "", "gruppo", chiave, 0.0, 0, 0])
            for v in elenco:
                it = self.store.append(g, [v["icona"], v["nome"], D.percento(v["cpu"], IT), D.dimensione(v["kb"], IT),
                                           "app" if v["figli"] else "proc", v["chiave"], v["cpu"], v["kb"], v["pid"]])
                if v["chiave"] == scelta:
                    da_scegliere = it
                for x in sorted(v["figli"], key=lambda x: -x["kb"]) if len(v["figli"]) > 1 else []:
                    ci = self.store.append(it, ["", x["nome"], D.percento(x["cpu"], IT), D.dimensione(x["kb"], IT),
                                                "proc", f"pid:{x['pid']}", x["cpu"], x["kb"], x["pid"]])
                    if f"pid:{x['pid']}" == scelta:
                        da_scegliere = ci
        self.vista.thaw_child_notify()
        for riga in self.store:   # i gruppi sempre aperti, le app come le aveva lasciate l'utente
            self.vista.expand_row(riga.path, False)
            for r in riga.iterchildren():
                if r[5] in self.aperte:
                    self.vista.expand_row(r.path, False)
        if da_scegliere is not None:
            self.vista.get_selection().select_iter(da_scegliere)
        if adj:
            GLib.idle_add(lambda: adj.set_value(pos) and False)
        self.c_cpu.set_title(T("Processore", "CPU") + "\n" + D.percento(sum(uso.values()), IT))
        usata = camp.mem_totale - camp.mem_libera
        self.c_mem.set_title(T("Memoria", "Memory") + "\n" + (D.percento(usata * 100 / camp.mem_totale, IT)
                                                              if camp.mem_totale else ""))

    def selezione(self, sel):
        modello, it = sel.get_selected()
        if it is None or modello[it][4] == "gruppo":
            self.scelta = None
            self.b_termina.set_sensitive(False)
            return
        self.scelta = modello[it][5]
        self.b_termina.set_sensitive(not self.da_non_terminare(modello[it]))

    def da_non_terminare(self, riga):
        if self.ultimo is None:
            return True
        camp = self.ultimo[0]
        if riga[4] == "app":
            return False
        p = camp.processi.get(riga[8])
        return p is None or D.protetto(p)

    # --- termina ----------------------------------------------------------------------------------------------------
    def termina(self):
        sel = self.vista.get_selection()
        modello, it = sel.get_selected()
        if it is None or self.ultimo is None or self.da_non_terminare(modello[it]):
            return
        camp, _uso, app, _resto = self.ultimo
        riga = list(modello[it])
        if riga[4] == "app":
            k = riga[5][4:]
            pid = set()
            for r in app.get(k, {}).get("radici", []):
                pid.update(D.discendenti(camp.processi, r))
            self.uccidi(sorted(pid, key=lambda p: -len(D.discendenti(camp.processi, p))), camp)
            return
        # un processo in background: si chiede, come Windows per i processi di sistema
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                              buttons=Gtk.ButtonsType.NONE, text=T(f"Terminare «{riga[1]}»?", f"End “{riga[1]}”?"))
        d.format_secondary_text(T("Se è un servizio del desktop, la funzione che offre smette di funzionare finché "
                                  "non riavvii il desktop.", "If it is a desktop service, what it provides stops working "
                                  "until you restart the desktop."))
        d.add_button(T("Annulla", "Cancel"), Gtk.ResponseType.CANCEL)
        d.add_button(T("Termina", "End"), Gtk.ResponseType.OK)
        d.set_default_response(Gtk.ResponseType.CANCEL)
        try:
            import tod_avviso
            tod_avviso.come_macos(d)
        except Exception:   # noqa: BLE001 - senza il modulo resta la finestra normale
            pass
        r = d.run()
        d.destroy()
        if r == Gtk.ResponseType.OK:
            self.uccidi(D.discendenti(camp.processi, riga[8]), camp)

    def uccidi(self, pids, camp):
        """SIGTERM a tutti (i figli prima), e dopo 3 secondi SIGKILL a chi è ancora vivo con lo stesso avvio"""
        io = os.getpid()
        vivi = []
        for p in pids:
            pr = camp.processi.get(p)
            if pr is None or p == io or D.protetto(pr):
                continue
            try:
                os.kill(p, signal.SIGTERM)
                vivi.append((p, pr.tick))
            except OSError:
                pass

        def finisci():
            for p, _t in vivi:
                if D.leggi(p) is not None:
                    try:
                        os.kill(p, signal.SIGKILL)
                    except OSError:
                        pass
            self.leggi()
            return False
        GLib.timeout_add(3000, finisci)
        GLib.timeout_add(400, lambda: self.leggi() and False)

    # ======================================================================================== prestazioni
    def costruisci_prestazioni(self):
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(14))
        for m, n in (("start", 24), ("end", 24), ("top", 18), ("bottom", 18)):
            getattr(v, f"set_margin_{m}")(px(n))
        v.pack_start(C.etichetta(T("Prestazioni", "Performance"), "titolo"), False, False, 0)
        self.g_cpu, self.n_cpu, self.s_cpu = self.scheda(v, T("Processore", "CPU"))
        self.g_mem, self.n_mem, self.s_mem = self.scheda(v, T("Memoria", "Memory"))
        sw.add(v)
        return sw

    def scheda(self, dentro, titolo):
        s = C.classe(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(6)), "att-scheda")
        testa = Gtk.Box(spacing=px(10))
        testa.pack_start(C.etichetta(titolo, "titolo2"), False, False, 0)
        n = C.etichetta("", "att-numero", xalign=1.0)
        testa.pack_end(n, False, False, 0)
        s.pack_start(testa, False, False, 0)
        sotto = C.etichetta("", "sotto", ellissi=False)
        s.pack_start(sotto, False, False, 0)
        g = Grafico(self)
        g.set_margin_top(px(6))
        s.pack_start(g, False, False, 0)
        dentro.pack_start(s, False, False, 0)
        return g, n, sotto

    def aggiorna_prestazioni(self, camp, uso, tot_cpu, usata):
        self.n_cpu.set_text(D.percento(tot_cpu, IT))
        self.s_cpu.set_text(T(f"Usato dalle app e dai servizi del desktop · {D.CORE} core · {len(camp.processi)} processi",
                              f"Used by desktop apps and services · {D.CORE} cores · {len(camp.processi)} processes"))
        self.g_cpu.metti(self.storia_cpu)
        nostra = sum(p.kb for p in camp.processi.values())
        if camp.mem_totale:
            self.n_mem.set_text(f"{D.dimensione(usata, IT)} / {D.dimensione(camp.mem_totale, IT)}")
            self.s_mem.set_text(T(f"In uso sul telefono ({D.percento(usata * 100 / camp.mem_totale, IT)}) · "
                                  f"desktop e app: {D.dimensione(nostra, IT)}",
                                  f"In use on the phone ({D.percento(usata * 100 / camp.mem_totale, IT)}) · "
                                  f"desktop and apps: {D.dimensione(nostra, IT)}"))
        self.g_mem.metti(self.storia_mem)

    # --- tastiera ---------------------------------------------------------------------------------------------------
    def tasto(self, _w, ev):
        k = Gdk.keyval_name(ev.keyval)
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        if k == "Delete" and self.b_termina.get_sensitive() and not self.cerca.has_focus():
            self.termina()
        elif ctrl and k in ("f", "F"):
            self.scegli("processi")
            self.cerca.grab_focus()
        elif k == "Escape" and self.cerca.get_text():
            self.cerca.set_text("")
        elif k == "F5":
            self.leggi()
        else:
            return False
        return True


class Attivita(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.termuxondex.Attivita", flags=Gio.ApplicationFlags.DEFAULT_FLAGS
                         if hasattr(Gio.ApplicationFlags, "DEFAULT_FLAGS") else Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        w = self.get_active_window() or Finestra(self)
        w.present()


if __name__ == "__main__":
    sys.exit(Attivita().run(sys.argv))
