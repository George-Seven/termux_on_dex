"""Gli avvisi delle nostre app Python come quelli di macOS, uguali alle finestrelle di sistema (10 novembre).

tod-gettext.c (in LD_PRELOAD nel desktop) trasforma le finestrelle GTK dei programmi nativi prima che compaiano:
niente barra di Linux, icona dell'app in alto, testi centrati, pulsanti a pillola. Le app Python però chiamano GTK
con GI, che non passa da LD_PRELOAD: per loro c'è come_macos(finestra), da chiamare prima di run() o show().
Fuori dal desktop (il modulo non è caricato) o con TOD_AVVISI_CLASSICI=1 non fa niente.
"""
import ctypes
import os

_libreria = None


def _funzione(nome):
    """Una funzione del modulo già caricato nel processo (RTLD_NOLOAD: mai caricarlo apposta), o None."""
    global _libreria
    if _libreria is None:
        _libreria = False
        so = os.path.join(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"), "lib", "tod-gettext.so")
        for nome_lib in os.environ.get("LD_PRELOAD", "").replace(":", " ").split() + [so]:
            try:
                lib = ctypes.CDLL(nome_lib, mode=os.RTLD_NOLOAD | os.RTLD_GLOBAL)
                lib.tod_avviso
            except (OSError, AttributeError):   # non caricato, o un'altra libreria del LD_PRELOAD
                continue
            _libreria = lib
            break
    if not _libreria:
        return None
    try:
        f = getattr(_libreria, nome)
    except AttributeError:
        return None
    f.argtypes = [ctypes.c_void_p]
    f.restype = ctypes.c_int
    return f


def _tod_avviso():   # (compatibilità con le prove)
    return _funzione("tod_avviso")


def puntatore(oggetto):
    """L'indirizzo dell'oggetto GTK dentro un oggetto di PyGObject."""
    capsula = ctypes.pythonapi.PyCapsule_GetPointer
    capsula.restype = ctypes.c_void_p
    capsula.argtypes = [ctypes.py_object, ctypes.c_char_p]
    return capsula(oggetto.__gpointer__, None)


def come_macos(finestra, libero=False):
    """Prima che compaia: la finestrella prende la forma degli avvisi di macOS. Restituisce la finestra.
    libero=True per le finestrelle fatte da noi (contenuto già disposto con testa()/etichetta()): solo cornice,
    pulsanti e margini."""
    f = _funzione("tod_avviso_libero" if libero else "tod_avviso")
    if f:
        try:
            f(puntatore(finestra))
        except (AttributeError, TypeError, ValueError):
            pass
    return finestra


def scala():
    """I DPI del desktop rispetto a 96 (come tod-gettext.c)."""
    from gi.repository import Gdk
    s = Gdk.Screen.get_default()
    dpi = s.get_resolution() if s else -1
    return dpi / 96 if dpi and dpi > 0 else 1.0


def etichetta(testo, classe="tod-avviso-testo", markup=False):
    """Un testo dell'avviso: centrato, a capo alla larghezza della finestra (anche dentro i percorsi lunghi)."""
    from gi.repository import Gtk, Pango
    lab = Gtk.Label(wrap=True, xalign=0.5, justify=Gtk.Justification.CENTER, max_width_chars=1, hexpand=True)
    lab.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if markup:
        lab.set_markup(testo)
    else:
        lab.set_text(testo)
    lab.get_style_context().add_class(classe)
    return lab


def testa(icona, titolo, testo=None, larghezza=268):
    """L'inizio di un avviso di macOS: l'icona in alto, il titolo in grassetto, il testo sotto, tutto centrato,
    largo come gli avvisi di sistema. icona: un nome del tema, o un widget già pronto (es. Gtk.Spinner)."""
    from gi.repository import Gtk
    sc = scala()
    v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=int(6 * sc + 0.5))
    if isinstance(icona, str):
        img = Gtk.Image.new_from_icon_name(icona, Gtk.IconSize.DIALOG)
        img.set_pixel_size(int(58 * sc + 0.5))
    else:
        img = icona
    img.set_halign(Gtk.Align.CENTER)
    img.get_style_context().add_class("tod-avviso-icona")
    v.pack_start(img, False, False, int(4 * sc + 0.5))
    v.pack_start(etichetta(titolo, "tod-avviso-titolo"), False, False, 0)
    if testo:
        v.pack_start(etichetta(testo), False, False, 0)
    v.set_size_request(int(larghezza * sc + 0.5), -1)
    return v
