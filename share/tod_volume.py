"""tod_volume.py — il volume del desktop È il volume del telefono (11 novembre 2026).

Pietro: "vorrei una gestione migliore del volume... non possiamo aumentare o diminuire il volume del telefono?".
Prima il cursore della barra regolava solo l'uscita di PulseAudio, che Android poi abbassa col suo volume "media":
col telefono a 5 su 15 il nostro 100% restava basso, e i tasti del volume (che muovono quello del telefono) non
spostavano il nostro cursore. Ora c'è un volume solo, come su un PC: il cursore muove il volume media di Android
(`termux-volume music N`, Termux:API) e l'uscita di PulseAudio resta al 100%.

Senza GTK. Lo usano la barra (icona), tod-pannelli.py (cursore), le Impostazioni → Suono e tod-phone-status.py
(che rilegge il volume ogni pochi secondi: tasti del telefono, tastiera, pannello di Android).
  telefono()          vero se si può comandare il volume del telefono (sennò resta quello di PulseAudio)
  stato()             (percentuale, muto) dall'ultimo valore noto, None se non si sa ancora
  leggi_telefono()    lo chiede ad Android (lento: ~1,5 s), aggiorna lo stato; → (percentuale, muto) o None
  metti(percento)     lo imposta (subito nello stato, ad Android in sottofondo: vale l'ultimo valore chiesto)
  muto(si)            0, oppure il valore di prima
`termux-volume` costa ~1,5 s a chiamata: mai nel filo della grafica.
"""
import json
import os
import shutil
import signal
import subprocess
import threading
import time

TMP = os.environ.get("TMPDIR", "/data/data/com.termux/files/usr/tmp")
STATO = os.environ.get("TOD_VOLUME_STATO") or os.path.join(TMP, "tod-volume.json")
COMANDO = os.environ.get("TOD_VOLUME_COMANDO") or "termux-volume"
FLUSSO = "music"
FERMO = 4.0   # dopo un nostro comando le letture non riscrivono lo stato per questi secondi (Android è in ritardo)

_lucchetto = threading.Lock()
_voluto = None      # gradini chiesti e non ancora mandati
_filo = None
_pulse_fatto = False


def telefono():
    """Si comanda il volume del telefono? (Termux:API installata; TOD_VOLUME_INTERNO=1 nell'ambiente lo spegne)"""
    return not os.environ.get("TOD_VOLUME_INTERNO") and shutil.which(COMANDO) is not None


def _carica():
    try:
        with open(STATO) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _salva(d):
    tmp = f"{STATO}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, STATO)
    except OSError:
        pass


def percento(gradini, massimo):
    return int(round(gradini * 100.0 / massimo)) if massimo else 0


def gradini(p, massimo):
    """La percentuale del cursore → i gradini di Android (15 sul Galaxy); sopra lo zero almeno uno"""
    p = max(0, min(100, p))
    g = int(round(p * massimo / 100.0))
    return 1 if p > 0 and g == 0 else g


def stato():
    d = _carica()
    if "volume" not in d or not d.get("max"):
        return None
    return percento(d["volume"], d["max"]), d["volume"] == 0


def avvisa_barra():
    """SIGUSR2 alla barra: l'icona si ridisegna subito"""
    try:
        with open(os.path.join(TMP, "tod-barra.pid")) as f:
            pid = int(f.read().strip())
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            if b"tod-barra.py" in f.read():
                os.kill(pid, signal.SIGUSR2)
    except (OSError, ValueError):
        pass


def leggi_telefono(forza=False):
    """Il volume media di Android → stato. Subito dopo un nostro comando non si riscrive (FORZA lo fa comunque)."""
    try:
        out = subprocess.run([COMANDO], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL).stdout
        voce = next(x for x in json.loads(out) if x.get("stream") == FLUSSO)
        vol, massimo = int(voce["volume"]), int(voce["max_volume"])
    except (OSError, ValueError, TypeError, KeyError, StopIteration, subprocess.TimeoutExpired):
        return None
    d = _carica()
    if not forza and time.time() - d.get("messo", 0) < FERMO:
        return stato()
    if d.get("volume") != vol or d.get("max") != massimo:
        if vol > 0:
            d["prima"] = vol
        d.update(volume=vol, max=massimo)
        _salva(d)
        avvisa_barra()
    return percento(vol, massimo), vol == 0


def _pulse_pieno():
    """L'uscita di PulseAudio al 100% e non muta: il volume lo fa Android (una volta per processo)"""
    global _pulse_fatto
    if _pulse_fatto:
        return
    _pulse_fatto = True
    for a in (["set-sink-volume", "@DEFAULT_SINK@", "100%"], ["set-sink-mute", "@DEFAULT_SINK@", "0"]):
        try:
            subprocess.run(["pactl"] + a, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=4)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _manda():
    global _voluto, _filo
    _pulse_pieno()
    while True:
        with _lucchetto:
            g, _voluto = _voluto, None
            if g is None:
                _filo = None
                return
        try:
            subprocess.run([COMANDO, FLUSSO, str(g)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass


def metti(p):
    """Il volume al P per cento. Lo stato cambia subito (icona, altri cursori); ad Android va in sottofondo, e se
    intanto arriva un altro valore (cursore trascinato) si manda solo l'ultimo."""
    global _voluto, _filo
    d = _carica()
    massimo = d.get("max")
    if not massimo:
        if leggi_telefono(forza=True) is None:
            return False
        d = _carica()
        massimo = d["max"]
    g = gradini(p, massimo)
    if g > 0:
        d["prima"] = g
    cambiato = d.get("volume") != g
    d.update(volume=g, messo=time.time())
    _salva(d)
    if cambiato:
        avvisa_barra()
    with _lucchetto:
        _voluto = g
        if _filo is None:
            _filo = threading.Thread(target=_manda)   # non "daemon": il programma aspetta che il comando parta
            _filo.start()
    return True


def muto(si):
    d = _carica()
    massimo = d.get("max") or 15
    if si:
        return metti(0)
    return metti(percento(d.get("prima") or max(1, massimo // 3), massimo))


def aspetta():
    """(per i comandi e i test) finché l'ultimo valore è arrivato ad Android"""
    f = _filo
    if f is not None:
        f.join()
