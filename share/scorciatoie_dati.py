"""scorciatoie_dati.py — le scorciatoie da tastiera per Impostazioni → Tastiera (13 novembre 2026). Senza grafica.

Pietro: "i pannelli di tastiera e mouse sono la cosa che al momento mi dà più fastidio di tutti". Al posto di quello di
XFCE (98 righe, molte doppie o inutili come le aree di lavoro) un elenco come quello di macOS: le azioni che contano,
raggruppate, con le combinazioni scritte in chiaro. Le scorciatoie restano quelle di XFCE (canale xfconf
xfce4-keyboard-shortcuts: /commands/custom/<tasti> = comando, /xfwm4/custom/<tasti> = azione delle finestre), che
xfsettingsd e xfwm4 applicano subito; quelle che qui non si vedono restano come sono.
"""
import os
import re

QUI = os.path.dirname(os.path.abspath(__file__))
PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")

# (gruppo, chiave, italiano, inglese, tipo, riconosce, comando da scrivere)
#   tipo "cmd": /commands/custom, riconosce(valore) → vero se il comando è quello dell'azione
#   tipo "wm":  /xfwm4/custom, il valore è il nome dell'azione di xfwm4
AZIONI = [
    ("sistema", "start", "Apri lo Start", "Open Start", "cmd", lambda v: v.endswith("tod-start"),
     f"{PREFIX}/bin/tod-start"),
    ("sistema", "esplora", "Esplora file", "File Explorer", "cmd", lambda v: v.split()[:1] == ["thunar"], "thunar"),
    ("sistema", "terminale", "Terminale", "Terminal", "cmd", lambda v: "--launch TerminalEmulator" in v,
     "exo-open --launch TerminalEmulator"),
    ("sistema", "browser", "Browser", "Web browser", "cmd", lambda v: "--launch WebBrowser" in v,
     "exo-open --launch WebBrowser"),
    ("sistema", "attivita", "Gestione attività", "Task Manager", "cmd", lambda v: "tod-attivita.py" in v,
     f"python3 {QUI}/tod-attivita.py"),
    ("sistema", "spegni", "Spegni", "Shut down", "cmd", lambda v: v.endswith("tod spegni"), f"{PREFIX}/bin/tod spegni"),
    ("schermata", "zona", "Cattura una zona", "Capture an area", "cmd",
     lambda v: "tod-cattura.py" in v and "--schermo" not in v and "--finestra" not in v, f"python3 {QUI}/tod-cattura.py"),
    ("schermata", "schermo", "Cattura tutto lo schermo", "Capture the whole screen", "cmd",
     lambda v: "tod-cattura.py --schermo" in v, f"python3 {QUI}/tod-cattura.py --schermo"),
    ("schermata", "finestra", "Cattura una finestra", "Capture a window", "cmd",
     lambda v: "tod-cattura.py --finestra" in v, f"python3 {QUI}/tod-cattura.py --finestra"),
    ("finestre", "close_window_key", "Chiudi la finestra", "Close window", "wm", None, None),
    ("finestre", "maximize_window_key", "Ingrandisci o ripristina", "Maximize or restore", "wm", None, None),
    ("finestre", "hide_window_key", "Riduci a icona", "Minimize", "wm", None, None),
    ("finestre", "fullscreen_key", "Schermo intero", "Full screen", "wm", None, None),
    ("finestre", "cycle_windows_key", "Passa alla finestra successiva", "Next window", "wm", None, None),
    ("finestre", "cycle_reverse_windows_key", "Passa alla finestra precedente", "Previous window", "wm", None, None),
    ("finestre", "show_desktop_key", "Mostra la scrivania", "Show desktop", "wm", None, None),
    ("finestre", "tile_left_key", "Affianca a sinistra", "Tile left", "wm", None, None),
    ("finestre", "tile_right_key", "Affianca a destra", "Tile right", "wm", None, None),
    ("finestre", "tile_up_key", "Affianca in alto", "Tile up", "wm", None, None),
    ("finestre", "tile_down_key", "Affianca in basso", "Tile down", "wm", None, None),
    ("finestre", "above_key", "Sempre in primo piano", "Always on top", "wm", None, None),
    ("finestre", "popup_menu_key", "Menu della finestra", "Window menu", "wm", None, None),
    ("finestre", "move_window_key", "Sposta con la tastiera", "Move with the keyboard", "wm", None, None),
    ("finestre", "resize_window_key", "Ridimensiona con la tastiera", "Resize with the keyboard", "wm", None, None),
]
GRUPPI = [("sistema", "Sistema e app", "System and apps"), ("schermata", "Schermata", "Screenshots"),
          ("finestre", "Finestre", "Windows")]

RADICE = {"cmd": "/commands/custom/", "wm": "/xfwm4/custom/"}
# i tasti "Maiusc + numero" visti come XFCE li confronta (dipende dalla tastiera): per noi sono lo stesso tasto
SIMBOLI = {"numbersign": "3", "sterling": "3", "dollar": "4", "percent": "5", "exclam": "1", "at": "2",
           "quotedbl": "2", "asciicircum": "6", "ampersand": "6", "slash": "7", "parenleft": "8", "asterisk": "8",
           "parenright": "9", "equal": "0"}
MOD = re.compile(r"<([A-Za-z0-9]+)>")


def leggi(testo):
    """le righe di `xfconf-query -c xfce4-keyboard-shortcuts -lv` → {percorso: valore}"""
    out = {}
    for riga in testo.splitlines():
        p, _, v = riga.partition(" ")
        if p.startswith("/"):
            out[p] = v.strip()
    return out


def divisa(tasti):
    """"<Super><Shift>4" → (["Super", "Shift"], "4")"""
    mods = MOD.findall(tasti)
    return mods, MOD.sub("", tasti)


def normale(tasti):
    """la forma per confrontare: modificatori in ordine, Control = Primary, lettere minuscole, simboli → numeri"""
    mods, k = divisa(tasti)
    mods = sorted({"Primary" if m in ("Control", "Ctrl") else m for m in mods})
    k = SIMBOLI.get(k, k)
    if len(k) == 1:
        k = k.lower()
    return "".join(f"<{m}>" for m in mods) + k


def di(azione, voci):
    """le combinazioni di un'azione [(percorso, tasti)], senza i doppioni che per l'utente sono lo stesso tasto"""
    _g, chiave, _it, _en, tipo, riconosce, _cmd = azione
    rad = RADICE[tipo]
    tutte = []
    for p, v in sorted(voci.items()):
        if not p.startswith(rad) or p.count("/") != 3 or p.endswith("/override"):
            continue
        if (tipo == "wm" and v == chiave) or (tipo == "cmd" and riconosce(v)):
            tutte.append((p, p[len(rad):]))
    visti, out = set(), []
    # prima le forme "pulite" (cifre, minuscole), così il doppione nascosto è la variante
    for p, t in sorted(tutte, key=lambda x: (divisa(x[1])[1] in SIMBOLI, divisa(x[1])[1] != divisa(x[1])[1].lower())):
        n = normale(t)
        if n not in visti:
            visti.add(n)
            out.append((p, t))
    return out


def tutte_di(azione, voci):
    """tutti i percorsi di un'azione, varianti comprese (per cambiarla o toglierla davvero)"""
    _g, chiave, _it, _en, tipo, riconosce, _cmd = azione
    rad = RADICE[tipo]
    return [p for p, v in voci.items() if p.startswith(rad) and p.count("/") == 3 and not p.endswith("/override")
            and ((tipo == "wm" and v == chiave) or (tipo == "cmd" and riconosce(v)))]


def comando_di(azione, voci):
    """il comando da scrivere per un'azione "cmd": quello che c'è già (stesso percorso), sennò il nostro"""
    for p in tutte_di(azione, voci):
        return voci[p]
    return azione[6]


def chi_usa(tasti, voci, salvo=None):
    """(azione o None, percorso) che usa già quei tasti (tra comandi e finestre); salvo = azione da non contare"""
    n = normale(tasti)
    for rad in RADICE.values():
        for p, v in voci.items():
            if p.startswith(rad) and p.count("/") == 3 and not p.endswith("/override") and normale(p[len(rad):]) == n:
                for a in AZIONI:
                    if a is salvo:
                        continue
                    if p in tutte_di(a, voci):
                        return a, p
                if salvo is None or p not in tutte_di(salvo, voci):
                    return None, p
    return None, None


TASTI = {"Print": ("Stamp", "Print Screen"), "Delete": ("Canc", "Delete"), "Escape": ("Esc", "Esc"),
         "space": ("Spazio", "Space"), "Return": ("Invio", "Enter"), "Tab": ("Tab", "Tab"), "Left": ("←", "←"),
         "Right": ("→", "→"), "Up": ("↑", "↑"), "Down": ("↓", "↓"), "Page_Up": ("Pag ↑", "Page Up"),
         "Page_Down": ("Pag ↓", "Page Down"), "Home": ("Inizio", "Home"), "End": ("Fine", "End"),
         "Insert": ("Ins", "Insert"), "BackSpace": ("⌫", "Backspace"), "Super_L": ("Win", "Win"),
         "Super_R": ("Win destro", "Right Win"), "numbersign": ("#", "#"), "sterling": ("£", "£"),
         "dollar": ("$", "$"), "percent": ("%", "%"),
         # tasti multimediali delle tastiere (XFCE li salva con o senza "XF86")
         "XF86WWW": ("Tasto Internet", "Internet key"), "XF86HomePage": ("Tasto Pagina iniziale", "Home page key"),
         "XF86PowerOff": ("Tasto di accensione", "Power key"), "XF86Mail": ("Tasto Posta", "Mail key"),
         "XF86Explorer": ("Tasto Esplora", "Explorer key"), "XF86Calculator": ("Tasto Calcolatrice", "Calculator key"),
         "XF86Search": ("Tasto Cerca", "Search key"), "XF86Display": ("Tasto Schermo", "Display key"),
         "XF86Terminal": ("Tasto Terminale", "Terminal key"), "XF86Sleep": ("Tasto Sospendi", "Sleep key")}
MODI = {"Primary": ("Ctrl", "Ctrl"), "Control": ("Ctrl", "Ctrl"), "Alt": ("Alt", "Alt"), "Shift": ("Maiusc", "Shift"),
        "Super": ("Win", "Win"), "Mod4": ("Win", "Win")}
ORDINE = ["Super", "Mod4", "Primary", "Control", "Alt", "Shift"]


def etichetta(tasti, italiano=True):
    """"<Super><Shift>4" → "Win + Maiusc + 4"; "<Primary><Alt>Delete" → "Ctrl + Alt + Canc" """
    i = 0 if italiano else 1
    mods, k = divisa(tasti)
    mods = sorted(set(mods), key=lambda m: ORDINE.index(m) if m in ORDINE else 9)
    parti = [MODI.get(m, (m, m))[i] for m in mods]
    if k in TASTI or "XF86" + k in TASTI:
        parti.append(TASTI.get(k, TASTI.get("XF86" + k))[i])
    elif k.startswith("XF86"):
        parti.append(k[4:])
    elif k.startswith("KP_"):
        nome = TASTI.get(k[3:], (k[3:], k[3:]))[i]
        parti.append(nome + (" (tastierino)" if italiano else " (keypad)"))
    else:
        parti.append(k.upper() if len(k) == 1 else k)
    return " + ".join(parti)
