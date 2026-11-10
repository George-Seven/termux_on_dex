"""Gli avvisi delle nostre app Python come quelli di macOS, uguali alle finestrelle di sistema (10 novembre).

tod-gettext.c (in LD_PRELOAD nel desktop) trasforma le finestrelle GTK dei programmi nativi prima che compaiano:
niente barra di Linux, icona dell'app in alto, testi centrati, pulsanti a pillola. Le app Python però chiamano GTK
con GI, che non passa da LD_PRELOAD: per loro c'è come_macos(finestra), da chiamare prima di run() o show().
Fuori dal desktop (il modulo non è caricato) o con TOD_AVVISI_CLASSICI=1 non fa niente.
"""
import ctypes
import os

_funzione = None


def _tod_avviso():
    """tod_avviso() del modulo già caricato nel processo (RTLD_NOLOAD: mai caricarlo apposta), o False."""
    global _funzione
    if _funzione is None:
        _funzione = False
        so = os.path.join(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"), "lib", "tod-gettext.so")
        for nome in os.environ.get("LD_PRELOAD", "").replace(":", " ").split() + [so]:
            try:
                lib = ctypes.CDLL(nome, mode=os.RTLD_NOLOAD | os.RTLD_GLOBAL)
                f = lib.tod_avviso
            except (OSError, AttributeError):   # non caricato, o un'altra libreria del LD_PRELOAD
                continue
            f.argtypes = [ctypes.c_void_p]
            f.restype = ctypes.c_int
            _funzione = f
            break
    return _funzione


def puntatore(oggetto):
    """L'indirizzo dell'oggetto GTK dentro un oggetto di PyGObject."""
    capsula = ctypes.pythonapi.PyCapsule_GetPointer
    capsula.restype = ctypes.c_void_p
    capsula.argtypes = [ctypes.py_object, ctypes.c_char_p]
    return capsula(oggetto.__gpointer__, None)


def come_macos(finestra):
    """Prima che compaia: la finestrella prende la forma degli avvisi di macOS. Restituisce la finestra."""
    f = _tod_avviso()
    if f:
        try:
            f(puntatore(finestra))
        except (AttributeError, TypeError, ValueError):
            pass
    return finestra
