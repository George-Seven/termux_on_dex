#!/data/data/com.termux/files/usr/bin/python3
"""tod-adb.py — il collegamento ADB senza fili in stile DeX OS (15 novembre 2026).

Pietro: "prepariamo un'interfaccia di sistema in stile DexOS per il collegamento adb wireless quando mi collego ad una
rete wifi nuova". Avvisi come quelli di sistema (share/tod_avviso.py: icona in alto, testi centrati, pulsanti a pillola):
  1. domanda       «Collegare ADB senza fili?»  Collega · Non su questa rete · Non ora
  2. attesa        si aprono le Opzioni sviluppatore: si accende il Debug wireless, e appena il telefono annuncia la
                   porta ci si collega da soli
  3. associazione  solo la prima volta (o se Android l'ha dimenticata): il codice a 6 cifre, la porta si trova da sola
  4. fatto         «ADB è collegato»
La logica (adb, Bonjour, reti) sta in share/adb_dati.py.

  tod-adb.py          collega adesso (Impostazioni → Avanzate): niente domanda
  tod-adb.py --rete   dal servizio del telefono quando cambia la rete Wi-Fi: prima un tentativo in silenzio (Debug
                      wireless già acceso), poi la domanda; niente se è già collegato, se la rete è tra le "non su
                      questa rete" o se la domanda è spenta (ADB_CHIEDI=0)
"""
import fcntl
import os
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(1, QUI)
import adb_dati as A  # noqa: E402
import tod_avviso  # noqa: E402

GLib.set_prgname("tod-adb")
PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
ITALIAN = os.environ.get("LANGUAGE", os.environ.get("LANG", "it")).startswith("it")
ATTESA = 180   # secondi per accendere il Debug wireless
OGNI = 2


def T(it, en):
    return it if ITALIAN else en


def registro(testo):
    print(f"{time.strftime('%F %T')} tod-adb: {testo}", file=sys.stderr, flush=True)


def am(*argomenti):
    try:
        subprocess.run([os.path.join(PREFIX, "bin/am"), "start", "--user", "0", *argomenti], capture_output=True,
                       timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


def apri_debug_wireless():
    """le Opzioni sviluppatore con la voce del Debug wireless in evidenza (chiave della preferenza di Android: se il
    telefono non la conosce si apre solo la pagina). In DeX si apre in una finestra accanto al desktop."""
    threading.Thread(target=am, args=("-a", "android.settings.APPLICATION_DEVELOPMENT_SETTINGS",
                                      "--es", ":settings:fragment_args_key", "toggle_adb_wireless"), daemon=True).start()


class Collegamento:
    def __init__(self, modo, chiedi=True):
        self.modo, self.chiedi = modo, chiedi
        self.rete = A.rete_wifi()
        self.ip = self.rete["ip"] if self.rete else None
        self.d = None
        self.fermo = threading.Event()   # l'utente ha chiuso: i fili di lavoro smettono
        self.porta = None                # la porta di collegamento vista (per non provarci l'associazione)

    # --- finestre ---------------------------------------------------------------------------------------------------
    def finestra(self, icona, titolo, testo, pulsanti, dentro=(), risposta=None):
        """un avviso di sistema: PULSANTI [(testo, risposta, predefinito)]; DENTRO: widget sotto il testo"""
        if self.d is not None:
            self.d.destroy()
        d = Gtk.Dialog(title=T("ADB senza fili", "Wireless ADB"))
        d.set_keep_above(True)
        d.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        d.set_icon_name("smartphone")
        for etichetta, r, predefinito in pulsanti:
            b = d.add_button(etichetta, r)
            if predefinito:
                b.get_style_context().add_class("suggested-action")
                d.set_default_response(r)
        box = d.get_content_area()
        box.set_spacing(8)
        box.set_border_width(18)
        box.pack_start(tod_avviso.testa(icona, titolo, testo), False, False, 0)
        for w in dentro:
            box.pack_start(w, False, False, 0)
        d.connect("response", risposta or (lambda *_: self.fine()))
        d.connect("delete-event", lambda *_: (self.fine(), True)[1])
        tod_avviso.come_macos(d, libero=True)
        d.show_all()
        self.d = d
        return d

    def fine(self):
        self.fermo.set()
        if self.d is not None:
            self.d.destroy()
            self.d = None
        Gtk.main_quit()

    def domanda(self):
        nome = self.rete["nome"] if self.rete and self.rete["nome"] else None
        testo = T("Con ADB collegato la Gestione attività chiude le app del telefono con un clic. Serve il Debug "
                  "wireless di Android, che si spegne a ogni riavvio e quando cambi rete.",
                  "With ADB connected, Task Manager closes phone apps with one click. It needs Android's Wireless "
                  "debugging, which turns off at every restart and when you change network.")
        if nome:
            testo = T(f"Sei sulla rete «{nome}». ", f"You are on the “{nome}” network. ") + testo

        def risposta(_d, r):
            if r == Gtk.ResponseType.OK:
                self.attesa()
            elif r == 1 and self.rete:
                A.segna_mai(self.rete["chiave"])
                registro(f"non chiedere più sulla rete {self.rete['chiave']}")
                self.fine()
            else:
                self.fine()
        # in colonna l'ultimo aggiunto va in cima: Collega sopra, Non ora in fondo (come macOS)
        self.finestra("smartphone", T("Collegare ADB senza fili?", "Connect wireless ADB?"), testo,
                      [(T("Non ora", "Not now"), Gtk.ResponseType.CANCEL, False),
                       (T("Non su questa rete", "Not on this network"), 1, False),
                       (T("Collega", "Connect"), Gtk.ResponseType.OK, True)], risposta=risposta)

    def attesa(self):
        giro = Gtk.Spinner(active=True)
        giro.set_size_request(int(48 * tod_avviso.scala()), int(48 * tod_avviso.scala()))
        self.finestra(giro, T("Accendi il Debug wireless", "Turn on Wireless debugging"),
                      T("Nelle Opzioni sviluppatore che si stanno aprendo, accendi «Debug wireless» (più veloce: il "
                        "riquadro «Debug wireless» nella tendina rapida). Se Android chiede di consentirlo su questa rete, "
                        "tocca «Consenti». Al resto penso io.",
                        "In the Developer options that are opening, turn on “Wireless debugging” (quicker: the "
                        "“Wireless debugging” tile in Quick settings). If Android asks to allow it on this network, tap "
                        "“Allow”. I'll take care of the rest."),
                      [(T("Annulla", "Cancel"), Gtk.ResponseType.CANCEL, False)])
        apri_debug_wireless()
        threading.Thread(target=self.aspetta, daemon=True).start()

    def aspetta(self):
        """finché il Debug wireless non compare: Bonjour a ogni giro, la scansione delle porte ogni 10 s (se Bonjour
        tace). Una porta vista ma che non accetta = Termux non associato → il codice."""
        inizio, viste, ultima_scansione = time.time(), set(), 0.0
        while not self.fermo.is_set() and time.time() - inizio < ATTESA:
            if A.collegato():
                GLib.idle_add(self.fatto)
                return
            p = A.porte(self.ip)
            candidate = [p["connect"]] if "connect" in p else []
            if not candidate and time.time() - ultima_scansione >= 10:
                ultima_scansione = time.time()
                candidate = [x for x in A.porte_aperte() if x not in viste]
            for porta in candidate:
                if self.fermo.is_set():
                    return
                ok, out = A.collega(porta)
                registro(f"collegamento a {porta}: {'riuscito' if ok else out}")
                if ok:
                    GLib.idle_add(self.fatto)
                    return
                viste.add(porta)
                if "connect" in p:   # il telefono la annuncia ma non ci accetta: manca l'associazione
                    self.porta = porta
                    GLib.idle_add(self.associazione)
                    return
            if viste and time.time() - inizio > 30:   # porte trovate a mano, nessuna accetta: probabilmente lo stesso
                GLib.idle_add(self.associazione)
                return
            self.fermo.wait(OGNI)
        if not self.fermo.is_set():
            GLib.idle_add(self.non_visto)

    def associazione(self, errore=None):
        voce = Gtk.Entry(placeholder_text=T("Codice a 6 cifre", "6-digit code"), activates_default=True,
                         max_length=7, xalign=0.5, hexpand=True)
        voce.get_style_context().add_class("tod-avviso-codice")
        avviso = tod_avviso.etichetta(errore or "", "tod-avviso-errore")
        giro = Gtk.Spinner()
        dentro = [voce, avviso, giro]

        def risposta(d, r):
            if r != Gtk.ResponseType.OK:
                self.fine()
                return
            codice = A.codice_valido(voce.get_text())
            if not codice:
                return
            d.set_response_sensitive(Gtk.ResponseType.OK, False)
            voce.set_sensitive(False)
            avviso.set_text(T("Associo…", "Pairing…"))
            avviso.show()
            giro.show()
            giro.start()
            threading.Thread(target=self.associa, args=(codice,), daemon=True).start()
        d = self.finestra("dialog-password", T("Associa Termux, una volta sola", "Pair Termux, just once"),
                          T("Nella pagina del Debug wireless tocca «Associa dispositivo con codice di associazione» e "
                            "scrivi qui il codice a 6 cifre che compare.",
                            "On the Wireless debugging page tap “Pair device with pairing code” and type here the "
                            "6-digit code it shows."),
                          [(T("Annulla", "Cancel"), Gtk.ResponseType.CANCEL, False),
                           (T("Associa", "Pair"), Gtk.ResponseType.OK, True)], dentro, risposta)
        voce.connect("changed", lambda v: d.set_response_sensitive(Gtk.ResponseType.OK,
                                                                    A.codice_valido(v.get_text()) is not None))
        d.set_response_sensitive(Gtk.ResponseType.OK, False)
        avviso.set_visible(bool(errore))
        giro.hide()   # solo mentre si associa: senza, resta uno spazio vuoto sotto la casella
        voce.grab_focus()

    def associa(self, codice):
        """la porta di associazione (aperta solo mentre il telefono mostra il codice): Bonjour, sennò le porte in
        ascolto tranne quella di collegamento; poi il collegamento"""
        p = A.porte(self.ip)
        candidate = [p["pairing"]] if "pairing" in p else [x for x in A.porte_aperte() if x not in (self.porta, p.get("connect"))]
        riuscito = False
        for porta in candidate:
            riuscito, out = A.associa(porta, codice)
            registro(f"associazione su {porta}: {'riuscita' if riuscito else out}")
            if riuscito or self.fermo.is_set():
                break
        if self.fermo.is_set():
            return
        if not riuscito:
            GLib.idle_add(self.associazione, T("Il codice non è stato accettato, o la finestra del codice sul telefono si "
                                               "è chiusa. Riaprila e riprova.",
                                               "The code wasn't accepted, or the code window on the phone closed. Open "
                                               "it again and retry."))
            return
        for _ in range(10):   # appena associato il telefono accetta il collegamento
            p = A.porte(self.ip)
            for porta in [p.get("connect"), self.porta]:
                if porta and A.collega(porta)[0]:
                    GLib.idle_add(self.fatto)
                    return
            if self.fermo.wait(OGNI):
                return
        GLib.idle_add(self.attesa)   # associato: il collegamento lo ritenta l'attesa

    def fatto(self, gia=False):
        registro("ADB collegato" + (" (lo era già)" if gia else ""))
        self.finestra("emblem-default", T("ADB è collegato", "ADB is connected"),
                      T("La Gestione attività ora chiude le app del telefono con un clic. Il collegamento dura finché "
                        "il telefono non si riavvia o non cambi rete Wi-Fi.",
                        "Task Manager now closes phone apps with one click. The connection lasts until the phone "
                        "restarts or you change Wi-Fi network."),
                      [(T("OK", "OK"), Gtk.ResponseType.OK, True)])
        GLib.timeout_add_seconds(8, lambda: (self.fine() if self.d is not None else None) and False)

    def non_visto(self):
        def risposta(_d, r):
            if r == Gtk.ResponseType.OK:
                self.fermo.clear()
                self.attesa()
            else:
                self.fine()
        self.finestra("smartphone", T("Il Debug wireless non si è acceso", "Wireless debugging didn't turn on"),
                      T("Non vedo il Debug wireless del telefono. Controlla di essere sul Wi-Fi e che sia acceso nelle "
                        "Opzioni sviluppatore.", "I can't see the phone's Wireless debugging. Check that you are on "
                        "Wi-Fi and that it is on in Developer options."),
                      [(T("Chiudi", "Close"), Gtk.ResponseType.CANCEL, False),
                       (T("Riprova", "Retry"), Gtk.ResponseType.OK, True)], risposta=risposta)

    # --- avvio ------------------------------------------------------------------------------------------------------
    def avvia(self):
        if self.modo != "rete":
            if A.collegato():
                self.fatto(gia=True)
            else:
                self.attesa()
            return

        def prova():   # Debug wireless già acceso (l'utente l'ha acceso da sé): ci si collega senza chiedere niente,
            # anche sulle reti "non su questa rete" (lì solo niente domanda)
            p = A.porte(self.ip)
            if "connect" in p and A.collega(p["connect"])[0]:
                registro(f"ADB collegato da solo sulla rete {self.rete['chiave'] if self.rete else '?'}")
                GLib.idle_add(Gtk.main_quit)
                return
            GLib.idle_add(self.domanda if self.chiedi else Gtk.main_quit)
        threading.Thread(target=prova, daemon=True).start()


def main():
    modo = "rete" if "--rete" in sys.argv else "adesso"
    if not A.presente():
        registro("adb assente (pacchetto android-tools)")
        return 0 if modo == "rete" else 1
    blocco = open(os.path.join(A.TMP, "tod-adb.lock"), "w")
    try:   # una finestra sola: due cambi di rete di fila non ne aprono due
        fcntl.flock(blocco, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    chiedi = True
    if modo == "rete":
        rete = A.rete_wifi()
        if rete is None or A.collegato():
            return 0
        chiedi = A.da_chiedere(rete, None, A.reti_mai(), A.conf("ADB_CHIEDI", "1") != "0")
    Collegamento(modo, chiedi).avvia()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
