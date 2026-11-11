#!/data/data/com.termux/files/usr/bin/python3
"""tod-avvisi-host.py — gli avvisi delle app di Debian come tutti gli altri popup del desktop (11 novembre 2026).

Pietro: "non voglio più nulla della grafica linux". Le app del container (PhotoCraft e le altre della suite Craft, con la
libreria rfd) mostrano domande e avvisi con zenity, che in Debian 13 è GTK 4 + libadwaita: aspetto di GNOME, che il
tema DeX OS non tocca. Nel container /usr/local/bin/zenity (share/tod-zenity, scritto da cx_install_runner) manda le
domande, gli avvisi e gli errori a questo servizio, che gira in Termux nel desktop e li mostra con GTK 3 e
share/tod_avviso.py: la stessa finestrella in stile macOS di Thunar, della scrivania e delle nostre app, con l'icona
dell'app che ha chiesto. Il resto di zenity (scelta dei file, elenchi...) resta a quello vero.

  tod-avvisi-host.py               il servizio (dall'hook della sessione; uno per sessione)
Protocollo: socket Unix $TMPDIR/tod-avvisi.sock (TOD_AVVISI_SOCK), una riga JSON per richiesta
  {"tipo": "question|info|warning|error", "titolo", "testo", "ok", "annulla", "extra": [...], "markup": bool,
   "predefinito_annulla": bool, "scadenza": secondi, "icona": nome, "chi": [argomenti del programma che chiede]}
e una riga di risposta {"codice": uscita di zenity, "stampa": testo per lo standard output}.
"""
import json
import os
import signal
import socket
import sys
import threading

TMP = os.environ.get("TMPDIR", "/data/data/com.termux/files/usr/tmp")
SOCK = os.environ.get("TOD_AVVISI_SOCK") or os.path.join(TMP, "tod-avvisi.sock")
PIDF = SOCK + ".pid"
ITALIANO = os.environ.get("LANGUAGE", os.environ.get("LANG", "it")).startswith("it")
QUI = os.path.dirname(os.path.abspath(__file__))


def T(it, en):
    return it if ITALIANO else en


# le etichette inglesi che rfd passa da sé ("Ok", "Cancel"...): nella lingua del desktop
ETICHETTE = {"ok": T("OK", "OK"), "cancel": T("Annulla", "Cancel"), "yes": T("Sì", "Yes"), "no": T("No", "No"),
             "close": T("Chiudi", "Close")}
# titoli che zenity mette da sé quando l'app non ne dà uno: non dicono niente
TITOLI_VUOTI = {"", "zenity", "question", "information", "warning", "error", "domanda", "informazione",
                "informazioni", "attenzione", "avviso", "errore"}


def etichetta(testo, predefinita):
    if not testo:
        return predefinita
    return ETICHETTE.get(testo.strip().strip("_").lower(), testo)


def cartelle_app():
    casa = os.environ.get("TOD_TERMUX_HOME") or os.path.expanduser("~")
    prefisso = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    return [os.path.join(casa, ".local/share/applications"), os.path.join(prefisso, "share/applications")]


def app_di(chi, cartelle=None):
    """Nome e icona dell'app che chiede, dalle voci del menu delle app del container (tod-cx-*.desktop: Exec con
    "tod cx run -- programma"). chi: gli argomenti del processo che ha lanciato zenity (senza proot il primo è il
    caricatore di Debian, poi --argv0 e il programma). → (nome, icona) o (None, None)."""
    nomi = set()
    for a in chi or []:
        b = os.path.basename(str(a))
        if b and not b.startswith("-") and b not in ("ld.so", "ld-linux-aarch64.so.1", "env", "sh", "bash"):
            nomi.add(b)
    if not nomi:
        return None, None
    for cartella in cartelle or cartelle_app():
        try:
            voci = sorted(f for f in os.listdir(cartella) if f.startswith("tod-cx-") and f.endswith(".desktop"))
        except OSError:
            continue
        for f in voci:
            nome = icona = esegue = None
            try:
                with open(os.path.join(cartella, f), encoding="utf-8", errors="replace") as fh:
                    for riga in fh:
                        riga = riga.strip()
                        if riga.startswith("[") and riga != "[Desktop Entry]":
                            break   # le azioni ([Desktop Action ...]) no
                        k, _, v = riga.partition("=")
                        if k == "Name" and nome is None:
                            nome = v
                        elif k == "Name[it]" and ITALIANO:
                            nome = v
                        elif k == "Icon":
                            icona = v
                        elif k == "Exec":
                            esegue = v
            except OSError:
                continue
            if not esegue:
                continue
            parti = esegue.split()
            dopo = parti[parti.index("--") + 1:] if "--" in parti else parti
            if dopo and os.path.basename(dopo[0]) in nomi:
                return nome, icona
    return None, None


def testi(titolo, testo, nome_app):
    """Come un avviso di macOS: in grassetto la domanda, sotto la spiegazione. Il titolo della finestra di zenity spesso
    è solo il nome dell'app (l'icona lo dice già): allora in grassetto va la prima riga del testo. → (titolo, testo)"""
    titolo = (titolo or "").strip()
    testo = (testo or "").strip()
    if titolo.lower() in TITOLI_VUOTI or (nome_app and titolo.lower() == nome_app.lower()) or titolo == testo:
        if "\n" in testo:
            primo, _, resto = testo.partition("\n")
            return primo.strip(), resto.strip()
        return testo, ""
    return titolo, testo


def pulsanti(r):
    """I pulsanti da sinistra a destra [(etichetta, risposta)], e la risposta predefinita. Risposte: "ok" (uscita 0),
    "annulla" (1), ("extra", etichetta) (1, e l'etichetta stampata, come zenity)."""
    if r.get("tipo") == "question":
        lista = [(e, ("extra", e)) for e in r.get("extra") or []]
        lista.append((etichetta(r.get("annulla"), T("No", "No")), "annulla"))
        lista.append((etichetta(r.get("ok"), T("Sì", "Yes")), "ok"))
        return lista, "annulla" if r.get("predefinito_annulla") else "ok"
    lista = [(e, ("extra", e)) for e in r.get("extra") or []]
    lista.append((etichetta(r.get("ok"), "OK"), "ok"))
    return lista, "ok"


def mostra(Gtk, r):
    """La finestrella (nel filo di GTK). → (codice, stampa)"""
    sys.path.insert(1, QUI)
    import tod_avviso
    from gi.repository import GLib
    nome_app, icona_app = app_di(r.get("chi"))
    tipo = r.get("tipo", "info")
    livello = r.get("livello") or tipo
    tipi = {"question": Gtk.MessageType.QUESTION, "info": Gtk.MessageType.INFO,
            "warning": Gtk.MessageType.WARNING, "error": Gtk.MessageType.ERROR}
    titolo, testo = testi(r.get("titolo"), r.get("testo"), nome_app)
    d = Gtk.MessageDialog(message_type=tipi.get(livello, Gtk.MessageType.INFO), buttons=Gtk.ButtonsType.NONE)
    d.set_title(r.get("titolo") or nome_app or "")
    d.set_keep_above(True)
    d.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
    icona = r.get("icona") or icona_app
    if icona:
        d.set_icon_name(icona)   # tod_avviso la mette in alto, con il triangolo se è un avvertimento
    markup = r.get("markup", True)
    d.set_markup(GLib.markup_escape_text(titolo))
    if testo:
        try:
            if markup:
                d.format_secondary_markup(testo)
            else:
                d.format_secondary_text(testo)
        except GLib.Error:
            d.format_secondary_text(testo)
    lista, predefinita = pulsanti(r)
    risposte = {}
    for i, (e, risposta) in enumerate(lista):
        rid = 100 + i
        risposte[rid] = risposta
        b = d.add_button(e, rid)
        if risposta == predefinita:
            d.set_default_response(rid)
            b.get_style_context().add_class("suggested-action")
    scaduto = []
    if r.get("scadenza"):
        def basta():
            scaduto.append(1)
            d.response(Gtk.ResponseType.DELETE_EVENT)
            return False
        GLib.timeout_add_seconds(int(r["scadenza"]), basta)
    tod_avviso.come_macos(d)
    d.show_all()
    d.present()
    rid = d.run()
    d.destroy()
    if scaduto:
        return 5, ""
    risposta = risposte.get(rid, "annulla")
    if risposta == "ok":
        return 0, ""
    if isinstance(risposta, tuple):
        return 1, risposta[1] + "\n"
    return 1, ""


def servizio():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import GLib, Gtk
    GLib.set_prgname("tod-avvisi")
    if not Gtk.init_check(sys.argv)[0]:
        print("tod-avvisi: niente schermo", file=sys.stderr)
        return 1
    # uno per sessione: quello di una sessione vecchia si chiude
    try:
        vecchio = int(open(PIDF).read())
        if vecchio != os.getpid() and b"tod-avvisi-host" in open(f"/proc/{vecchio}/cmdline", "rb").read():
            os.kill(vecchio, signal.SIGTERM)
    except (OSError, ValueError):
        pass
    with open(PIDF, "w") as f:
        f.write(str(os.getpid()))
    try:
        os.unlink(SOCK)
    except OSError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    vecchia = os.umask(0o177)   # solo l'utente: niente finestre chieste da altri
    s.bind(SOCK)
    os.umask(vecchia)
    s.listen(8)

    def uno(c):
        try:
            c.settimeout(10)
            riga = c.makefile("r", encoding="utf-8", errors="replace").readline()
            r = json.loads(riga)
            c.settimeout(None)
            esito, fatto = {}, threading.Event()

            def nel_filo():
                try:
                    esito["r"] = mostra(Gtk, r)
                except Exception as e:   # mai appesi: zenity dice "annullato"
                    print(f"tod-avvisi: {type(e).__name__}: {e}", file=sys.stderr)
                    esito["r"] = (1, "")
                fatto.set()
                return False
            GLib.idle_add(nel_filo)
            fatto.wait()
            codice, stampa = esito["r"]
            c.sendall((json.dumps({"codice": codice, "stampa": stampa}) + "\n").encode())
        except (OSError, ValueError) as e:
            print(f"tod-avvisi: richiesta non valida: {e}", file=sys.stderr)
        finally:
            c.close()

    def ascolta():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=uno, args=(c,), daemon=True).start()

    def fine(*_):
        try:
            if int(open(PIDF).read()) == os.getpid():
                os.unlink(SOCK)
                os.unlink(PIDF)
        except (OSError, ValueError):
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, fine)
    threading.Thread(target=ascolta, daemon=True).start()
    print(f"tod-avvisi: in ascolto su {SOCK}", file=sys.stderr)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(servizio())
