#!/data/data/com.termux/files/usr/bin/python3
"""tod-attivita.py — Gestione attività come quella di Windows 11 (12 novembre 2026).

Pietro: Cattura schermo e Gestione attività di XFCE "hanno l'aspetto Linux": le rifacciamo nello stile delle nostre app
(Foto, Lettore: share/media/comune.py, colori e accento del sistema). Due pagine:
  Processi     le app con una finestra (i loro processi dentro, aprendo la riga) e i processi in background, con
               processore e memoria; "Termina attività" (Canc). I pezzi che tengono in piedi il desktop non si terminano.
  Prestazioni  processore e memoria negli ultimi due minuti, e il chip video (13 novembre: come "Video Decode" della GPU
               in Windows): chi lo usa, formato, fotogrammi al secondo, totali della sessione; nei Processi l'app che lo
               sta usando ha accanto "chip video".
I dati: share/attivita_dati.py (senza grafica). Android mostra a Termux solo i processi del desktop.
App del telefono (15 novembre, Pietro: "mi serve che gestione attività mi permetta di gestire le app android aperte"):
un gruppo a parte nei Processi, dall'aiutante share/android/AppAndroid.java (serve l'"Accesso ai dati di utilizzo"
per Termux: se manca, una riga e un link lo aprono). Stato (in primo piano, in background, sospesa), doppio clic =
passa all'app, "Termina attività" = Forza arresto con ADB o Shizuku, sennò la pagina dell'app nelle impostazioni del
telefono (Android non lascia chiudere le altre app da sé). Processore e memoria di quelle app Android non li dice.
"""
import os
import signal
import subprocess
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


def modulo(nome, file):
    import importlib.util
    spec = importlib.util.spec_from_file_location(nome, os.path.join(os.path.dirname(os.path.abspath(__file__)), file))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def pannelli():
    """Catalogo delle app, finestre aperte e chiusura: le stesse funzioni del pannello Batteria (tod-pannelli.py)."""
    return modulo("tod_pannelli", "tod-pannelli.py")


PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")


def am(*argomenti):
    """am start di Termux (in DeX la schermata si apre in una finestra) → l'uscita, per capire se Android l'ha aperta"""
    try:
        r = subprocess.run([os.path.join(PREFIX, "bin/am"), "start", "--user", "0", *argomenti], capture_output=True,
                           text=True, timeout=20, stdin=subprocess.DEVNULL)
        return r.stdout + r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return f"Error: {e}"


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
        self.alta = px(24)   # tutte le righe alte uguali (15 novembre: alcune erano il doppio delle altre e cambiavano)
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
        self.storia_cpu, self.storia_mem, self.storia_chip = [], [], []
        self.chip_prima, self.chip_ora, self.chip_app = None, 0.0, set()
        self._in_lettura = False
        # le app del telefono: l'aiutante resta acceso finché la finestra è aperta (avviarlo costa ~1 s)
        self.telefono = D.Telefono()
        self.tel_stato, self.tel_app = "assente", []   # assente | permesso | ok | errore
        self.tel_info = {}       # pacchetto → (nome, icona PNG, componente) o None (non si apre dal launcher)
        self._AA = None          # tod-android-apps.py, caricato alla prima app del telefono
        self._icone = {}
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
        self._chiusa = False
        self.connect("delete-event", lambda *_: (self.hide(), False)[1])   # sparisce subito, il resto dopo
        self.connect("destroy", lambda *_: (setattr(self, "_chiusa", True), GLib.source_remove(self._giro),
                                            self.telefono.ferma()))
        self.show_all()
        self.scegli("processi")
        self._giro = GLib.timeout_add_seconds(OGNI, self.leggi)
        self.leggi()

    def tema_cambiato(self):
        self.colori = C.colori()
        self.g_cpu.queue_draw()
        self.g_mem.queue_draw()
        self.g_chip.queue_draw()

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
        # le righe aperte dall'utente; non quelle che GTK chiude da sé mentre si aggiorna (un'app rimasta con un
        # processo solo perde le righe figlie e si chiude)
        self._in_sinc = False
        self.vista.connect("row-expanded", lambda _v, it, _p: self._in_sinc or self.aperte.add(self.store[it][5]))
        self.vista.connect("row-collapsed", lambda _v, it, _p: self._in_sinc or self.aperte.discard(self.store[it][5]))
        self.vista.connect("row-activated", self.attivata)
        self.vista.connect("button-press-event", self.clic)
        c = Gtk.TreeViewColumn(T("Nome", "Name"))
        c.set_expand(True)
        ri = Gtk.CellRendererPixbuf()
        ri.set_property("stock-size", Gtk.IconSize.SMALL_TOOLBAR)
        ri.set_padding(px(4), 0)
        ri.set_fixed_size(-1, self.alta)
        c.pack_start(ri, False)
        c.set_cell_data_func(ri, self._icona_cella)
        rt = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        rt.set_padding(px(6), px(2))
        rt.set_fixed_size(-1, self.alta)
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
        self.nota = C.etichetta("", "att-nota", ellissi=False)
        self.nota.set_line_wrap(True)
        self.nota.connect("activate-link", lambda _l, uri: (self.info_android("com.termux") if uri == "termux"
                                                            else self.apri_permesso(), True)[1])
        self.metti_nota()
        for m, n in (("start", 24), ("end", 24), ("top", 8), ("bottom", 12)):
            getattr(self.nota, f"set_margin_{m}")(px(n))
        v.pack_start(self.nota, False, False, 0)
        return v

    def metti_nota(self):
        if self.tel_stato == "permesso":
            # Termux installato da file (GitHub, F-Droid): Android blocca l'interruttore come "impostazione con
            # restrizioni" (15 novembre, sul telefono di Pietro) finché non lo si consente dalla pagina di Termux
            self.nota.set_markup(T("Per vedere anche le app del telefono, Termux ha bisogno dell'«Accesso ai dati di "
                                   "utilizzo»: <a href=\"permesso\">consentilo nelle impostazioni del telefono</a>. "
                                   "Se Android lo nega («impostazioni con restrizioni»): <a href=\"termux\">Info app di "
                                   "Termux</a> → ⋮ in alto a destra → «Consenti impostazioni con restrizioni», poi riprova.",
                                   "To also show phone apps, Termux needs “Usage access”: "
                                   "<a href=\"permesso\">allow it in the phone settings</a>. If Android refuses "
                                   "(“restricted settings”): <a href=\"termux\">Termux app info</a> → ⋮ at the top "
                                   "right → “Allow restricted settings”, then try again."))
        elif self.tel_stato == "ok":
            self.nota.set_text(T("App del telefono: doppio clic per passare all'app. Android non dice quanto processore e "
                                 "memoria usano.", "Phone apps: double-click to switch to the app. Android doesn't tell "
                                 "how much CPU and memory they use."))
        else:
            self.nota.set_text(T("Qui ci sono le app e i servizi del desktop. Le app del telefono le gestisce Android.",
                                 "This shows desktop apps and services. Phone apps are managed by Android."))

    def colonna_numero(self, titolo, testo, valore, chiave, pieno):
        r = Gtk.CellRendererText(xalign=1.0)
        r.set_padding(px(14), px(2))
        r.set_fixed_size(-1, self.alta)
        c = Gtk.TreeViewColumn(titolo, r, text=testo)
        c.set_min_width(px(130))
        c.set_alignment(1.0)
        c.set_clickable(True)
        c.connect("clicked", lambda *_: self.ordina(chiave))
        c.set_cell_data_func(r, self._numero_cella, (valore, pieno))
        self.vista.append_column(c)
        return c

    def _icona_cella(self, _c, cella, modello, it, _d):
        """nomi del tema e file (le icone delle app del telefono sono PNG fatte dal generatore dello Start)"""
        s = modello[it][0]
        g = self._icone.get(s) if s else None
        if s and g is None:
            try:
                g = Gio.Icon.new_for_string(s)
            except GLib.Error:
                g = Gio.ThemedIcon.new("application-x-executable")
            self._icone[s] = g
        cella.set_property("gicon", g)

    def _nome_cella(self, _c, cella, modello, it, _d):
        cella.set_property("weight", 600 if modello[it][4] == "gruppo" else 400)

    def _numero_cella(self, _c, cella, modello, it, dati):
        """come Windows: lo sfondo del numero si colora di più quanto più consuma (qui nell'accento)"""
        i, pieno = dati
        if modello[it][4] in ("gruppo", "android", "permesso"):   # Android non dice quanto consumano
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
            chip = ([], (0, 0)), ("in_preparazione", "")
            tel = None
            try:
                camp = D.Campione()
                fin = self.P.finestre_aperte()
                chip = D.chip_video(), D.chip_stato()
            except Exception as e:   # noqa: BLE001 - un giro andato storto non chiude la finestra
                print(f"tod-attivita: {type(e).__name__}: {e}", file=sys.stderr)
                camp, fin = None, []
            try:
                tel = self.telefono.chiedi()
                if tel and tel.get("ok"):
                    self.conosci([r[0] for r in tel.get("app", []) if r])
            except Exception as e:   # noqa: BLE001 - senza le app del telefono la finestra resta utile
                print(f"tod-attivita: telefono: {type(e).__name__}: {e}", file=sys.stderr)
            GLib.idle_add(lambda: self.letto(camp, fin, chip, tel) and False)
        threading.Thread(target=lavoro, daemon=True).start()
        return True

    def conosci(self, pacchetti):
        """nome, icona e componente delle app del telefono mai viste in questa sessione (dal filo di lettura: la prima
        volta per un'app senza icona in cache servono aapt2 e rsvg-convert, qualche secondo)"""
        nuovi = [p for p in pacchetti if p not in self.tel_info and not p.startswith(D.NOSTRE)]
        if not nuovi:
            return
        if self._AA is None:
            self._AA = modulo("tod_android_apps", "tod-android-apps.py")
        comps = self._AA.launcher_activities()
        for p in nuovi:
            try:
                self.tel_info[p] = self._AA.info_app(p, comps)
            except Exception as e:   # noqa: BLE001 - un APK strano non ferma le altre
                print(f"tod-attivita: {p}: {type(e).__name__}: {e}", file=sys.stderr)
                self.tel_info[p] = None

    def letto(self, camp, fin, chip=(([], (0, 0)), ("in_preparazione", "")), tel=None):
        self._in_lettura = False
        if self._chiusa:   # un giro finito dopo la chiusura
            return
        if tel is None:
            stato = "assente"
        elif tel.get("ok"):
            stato = "ok"
            self.tel_app = D.app_android(tel.get("app"), {p for p, i in self.tel_info.items() if i},
                                         {a["pkg"] for a in self.tel_app})
        else:
            stato = "permesso" if tel.get("errore") == "permesso" else "errore"
            if stato == "errore" and self.tel_stato != "errore":
                print(f"tod-attivita: telefono: {tel.get('errore')}", file=sys.stderr)
        if stato != "ok":
            self.tel_app = []
        if stato != self.tel_stato:
            self.tel_stato = stato
            self.metti_nota()
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
        self.chip_letto(camp, app, chip)
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
            nome = a["nome"] + (T("  ·  chip video", "  ·  video chip") if k in self.chip_app else "")
            voce = {"nome": nome, "icona": a["icona"], "chiave": "app:" + k, "pid": 0, "figli": figli,
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
        voci_tel = []
        for a in self.tel_app:
            info = self.tel_info.get(a["pkg"])
            if not info:
                continue
            nome = info[0] + "  ·  " + T(*D.STATI_ANDROID[a["stato"]])
            if f and f not in nome.lower() and f not in a["pkg"].lower():
                continue
            voci_tel.append({"nome": nome, "icona": info[1], "chiave": "android:" + a["pkg"], "pid": 0, "figli": [],
                             "cpu": 0.0, "kb": 0, "ultimo": a["ultimo"], "tipo": "android"})
        if self.tel_stato == "permesso" and not f:
            voci_tel.append({"nome": T("Consenti l'«Accesso ai dati di utilizzo» per vederle (doppio clic)",
                                       "Allow “Usage access” to see them (double-click)"),
                             "icona": "dialog-information-symbolic", "chiave": "permesso", "pid": 0, "figli": [],
                             "cpu": 0.0, "kb": 0, "tipo": "permesso"})
        for elenco in (voci_app, voci_bg):
            elenco.sort(key=self.chiave_ordine, reverse=self.discendente)
        # processore e memoria non si sanno: per nome, sennò la più usata di recente in cima
        voci_tel.sort(key=(lambda v: v["nome"].lower()) if self.ordine == "nome" else (lambda v: -v.get("ultimo", 0)),
                      reverse=self.ordine == "nome" and self.discendente)
        n_tel = sum(1 for v in voci_tel if v["tipo"] == "android")
        t_tel = (T("App del telefono", "Phone apps") if self.tel_stato == "permesso"
                 else T(f"App del telefono ({n_tel})", f"Phone apps ({n_tel})"))
        albero = []   # [(valori della riga, [figli])]
        for titolo, elenco, chiave in ((T(f"App ({len(voci_app)})", f"Apps ({len(voci_app)})"), voci_app, "g:app"),
                                       (t_tel, voci_tel, "g:android"),
                                       (T(f"Processi in background ({len(voci_bg)})",
                                          f"Background processes ({len(voci_bg)})"), voci_bg, "g:bg")):
            if not elenco:
                continue
            righe = []
            for v in elenco:
                if "tipo" in v:   # app del telefono: i numeri Android non li dà
                    vuoto = "—" if v["tipo"] == "android" else ""
                    righe.append(([v["icona"], v["nome"], vuoto, vuoto, v["tipo"], v["chiave"], 0.0, 0, 0], []))
                    continue
                figli = [(["", x["nome"], D.percento(x["cpu"], IT), D.dimensione(x["kb"], IT), "proc", f"pid:{x['pid']}",
                           x["cpu"], x["kb"], x["pid"]], [])
                         for x in (sorted(v["figli"], key=lambda x: -x["kb"]) if len(v["figli"]) > 1 else [])]
                righe.append(([v["icona"], v["nome"], D.percento(v["cpu"], IT), D.dimensione(v["kb"], IT),
                               "app" if v["figli"] else "proc", v["chiave"], v["cpu"], v["kb"], v["pid"]], figli))
            albero.append((["", titolo, "", "", "gruppo", chiave, 0.0, 0, 0], righe))
        # sul posto, non svuotando e riempiendo (15 novembre, Pietro: "le voci sfarfallano"): restano selezione,
        # posizione dello scorrimento e righe aperte, e GTK ridisegna solo i numeri cambiati
        self._in_sinc = True
        try:
            self.sincronizza(None, albero)
            for riga in self.store:   # i gruppi sempre aperti, le app come le aveva lasciate l'utente
                if not self.vista.row_expanded(riga.path):
                    self.vista.expand_row(riga.path, False)
                for r in riga.iterchildren():
                    if r[5] in self.aperte and self.store.iter_has_child(r.iter) and not self.vista.row_expanded(r.path):
                        self.vista.expand_row(r.path, False)
        finally:
            self._in_sinc = False
        usata = camp.mem_totale - camp.mem_libera
        for c, t in ((self.c_cpu, T("Processore", "CPU") + "\n" + D.percento(sum(uso.values()), IT)),
                     (self.c_mem, T("Memoria", "Memory") + "\n" + (D.percento(usata * 100 / camp.mem_totale, IT)
                                                                   if camp.mem_totale else ""))):
            if c.get_title() != t:   # un titolo nuovo rifà l'intestazione: solo se cambia
                c.set_title(t)

    def sincronizza(self, genitore, righe):
        """i figli di GENITORE (None = la radice) diventano RIGHE [(valori, figli)], riconosciute dalla chiave
        (colonna 5): quelle che restano si aggiornano solo nei valori cambiati, le nuove si aggiungono, le sparite si
        tolgono, poi si rimettono nell'ordine voluto. Gli iter di un TreeStore restano validi tra una modifica e l'altra."""
        esistenti = {}
        it = self.store.iter_children(genitore)
        while it is not None:
            esistenti[self.store.get_value(it, 5)] = it
            it = self.store.iter_next(it)
        volute = []
        for valori, figli in righe:
            it = esistenti.pop(valori[5], None)
            if it is None:
                it = self.store.append(genitore, valori)
            else:
                vecchi = self.store[it]
                cambiati = [x for i, v in enumerate(valori) if vecchi[i] != v for x in (i, v)]
                if cambiati:
                    self.store.set(it, *cambiati)
            volute.append(it)
            self.sincronizza(it, figli)
        for it in esistenti.values():
            self.store.remove(it)
        posto = [self.store.get_path(it).get_indices()[-1] for it in volute]
        if posto != list(range(len(posto))):   # reorder() in PyGObject non c'è: una riga dietro l'altra
            prima = None
            for it in volute:
                self.store.move_after(it, prima)   # None = in cima
                prima = it

    def selezione(self, sel):
        modello, it = sel.get_selected()
        if it is None or modello[it][4] == "gruppo":
            self.scelta = None
            self.b_termina.set_sensitive(False)
            self.b_termina.set_tooltip_text(None)
            return
        self.scelta = modello[it][5]
        self.b_termina.set_sensitive(not self.da_non_terminare(modello[it]))
        self.b_termina.set_tooltip_text(T("Con ADB o Shizuku la chiude subito (Forza arresto); senza, si apre la sua pagina "
                                          "nelle impostazioni del telefono: tocca «Forza arresto».",
                                          "With ADB or Shizuku it is closed right away (Force stop); without, its page "
                                          "in the phone settings opens: tap “Force stop”.")
                                        if modello[it][4] == "android" else None)

    def da_non_terminare(self, riga):
        if riga[4] == "android":
            return False
        if self.ultimo is None or riga[4] == "permesso":
            return True
        camp = self.ultimo[0]
        if riga[4] == "app":
            return False
        p = camp.processi.get(riga[8])
        return p is None or D.protetto(p)

    # --- app del telefono ---------------------------------------------------------------------------------------------
    def in_filo(self, lavoro):
        threading.Thread(target=lavoro, daemon=True).start()

    def passa_a(self, pkg):
        """come dal launcher: l'app già aperta torna davanti (in DeX la sua finestra), senza aprirne un'altra"""
        info = self.tel_info.get(pkg)
        if info:
            self.in_filo(lambda: am("-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER",
                                    "-f", "0x10200000", "-n", info[2]))   # NEW_TASK | RESET_TASK_IF_NEEDED

    def info_android(self, pkg):
        self.in_filo(lambda: am("-a", "android.settings.APPLICATION_DETAILS_SETTINGS", "-d", f"package:{pkg}"))

    def apri_permesso(self):
        """Impostazioni → Accesso ai dati di utilizzo, direttamente sull'interruttore di Termux se il telefono lo fa"""
        def lavoro():
            if "Error" in am("-a", "android.settings.USAGE_ACCESS_SETTINGS", "-d", "package:com.termux"):
                am("-a", "android.settings.USAGE_ACCESS_SETTINGS")
        self.in_filo(lavoro)

    def termina_android(self, pkg):
        """Forza arresto con ADB o Shizuku (tod _telefono_chiudi); senza, Android lo lascia fare solo dalle sue
        impostazioni: si apre la pagina dell'app (un tocco su «Forza arresto»)"""
        def lavoro():
            try:
                rc = subprocess.run(["tod", "_telefono_chiudi", pkg], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=60).returncode
            except (OSError, subprocess.SubprocessError):
                rc = 1
            if rc == 0:
                GLib.timeout_add(500, lambda: self.leggi() and False)
            else:
                am("-a", "android.settings.APPLICATION_DETAILS_SETTINGS", "-d", f"package:{pkg}")
        self.in_filo(lavoro)

    def attivata(self, _v, percorso, _c):
        riga = self.store[percorso]
        if riga[4] == "android":
            self.passa_a(riga[5][len("android:"):])
        elif riga[4] == "permesso":
            self.apri_permesso()

    def clic(self, _v, ev):
        """tasto destro: le azioni della riga (per le app del telefono anche Passa all'app e Informazioni)"""
        if ev.type != Gdk.EventType.BUTTON_PRESS or ev.button != 3:
            return False
        r = self.vista.get_path_at_pos(int(ev.x), int(ev.y))
        if r is None:
            return False
        self.vista.get_selection().select_path(r[0])
        riga = self.store[r[0]]
        if riga[4] in ("gruppo", "permesso"):
            return True
        menu = Gtk.Menu()

        def voce(testo, azione, attiva=True):
            m = Gtk.MenuItem(label=testo)
            m.set_sensitive(attiva)
            m.connect("activate", lambda *_: azione())
            menu.append(m)
        if riga[4] == "android":
            pkg = riga[5][len("android:"):]
            voce(T("Passa all'app", "Switch to"), lambda: self.passa_a(pkg))
            voce(T("Termina attività", "End task"), self.termina)
            voce(T("Informazioni sull'app", "App info"), lambda: self.info_android(pkg))
        else:
            voce(T("Termina attività", "End task"), self.termina, not self.da_non_terminare(riga))
        menu.show_all()
        menu.attach_to_widget(self.vista, None)
        menu.connect("selection-done", lambda m: GLib.idle_add(m.destroy))
        menu.popup_at_pointer(ev)
        return True

    # --- termina ----------------------------------------------------------------------------------------------------
    def termina(self):
        sel = self.vista.get_selection()
        modello, it = sel.get_selected()
        if it is None or self.da_non_terminare(modello[it]):
            return
        if modello[it][4] == "android":
            self.termina_android(modello[it][5][len("android:"):])
            return
        if self.ultimo is None:
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
        self.g_chip, self.n_chip, self.s_chip = self.scheda(v, T("Chip video", "Video chip"))
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

    # --- il chip video -----------------------------------------------------------------------------------------------
    def chip_letto(self, camp, app, chip):
        (attivi, (video, fotogrammi)), (stato, motivo) = chip
        ora = camp.ora if hasattr(camp, "ora") else 0.0
        fps = D.chip_al_secondo(self.chip_prima, attivi, ora - self.chip_ora) if self.chip_prima is not None else {}
        self.chip_prima, self.chip_ora = attivi, ora
        di_app = {p: k for k, a in app.items() for p in a.get("pid", [])}
        self.chip_app = set()
        righe = []
        for v in attivi:
            k = di_app.get(int(v.get("pid", 0)))
            if k:
                self.chip_app.add(k)
                chi = app[k]["nome"]
            else:
                p = camp.processi.get(int(v.get("pid", 0)))
                chi = D.nome_leggibile(p, IT) if p else T("un'app", "an app")
            al_s = fps.get(v["id"], 0.0)
            righe.append(f"{chi} · {D.CODEC.get(v.get('codec'), v.get('codec', '?'))} {v.get('w')}×{v.get('h')} · "
                         + T(f"{al_s:.0f} fotogrammi/s", f"{al_s:.0f} frames/s"))
        tot = sum(fps.values())
        self.storia_chip = (self.storia_chip + [min(100.0, tot * 100 / 60)])[-STORIA:]   # 60 fotogrammi/s = pieno
        if stato == "spento":
            self.n_chip.set_text(T("Spento", "Off"))
            sotto = T("Le app decodificano i video col processore. Si accende con «tod videochip on».",
                      "Apps decode video with the processor. Turn it on with “tod videochip on”.")
        elif stato == "non_disponibile":
            self.n_chip.set_text(T("Non disponibile", "Not available"))
            sotto = T(f"Su questo telefono le app usano il processore: {motivo}.", f"On this phone apps use the processor: {motivo}.")
        elif stato == "in_preparazione":
            self.n_chip.set_text(T("In preparazione", "Getting ready"))
            sotto = T("Sarà pronto al prossimo avvio del desktop.", "It will be ready at the next desktop start.")
        else:
            self.n_chip.set_text(T(f"{tot:.0f} fotogrammi/s", f"{tot:.0f} frames/s") if attivi else T("Inattivo", "Idle"))
            sotto = "\n".join(righe) if righe else T("Nessun video in questo momento.", "No video right now.")
            n = f"{fotogrammi:,}".replace(",", ".") if IT else f"{fotogrammi:,}"   # 12.345 in italiano
            sotto += "\n" + T(f"In questa sessione: {video} video, {n} fotogrammi decodificati dal chip invece che dal "
                               "processore.", f"This session: {video} videos, {n} frames decoded by the chip instead of "
                               "the processor.")
        self.s_chip.set_text(sotto)
        self.g_chip.metti(self.storia_chip)

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
