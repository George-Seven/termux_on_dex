#!/usr/bin/env python3
"""Aggancio ai bordi come Windows 11 (Aero Snap), 15 novembre 2026.

Trascinando una finestra per la barra del titolo:
  bordo sinistro o destro → metà schermo, angoli → un quarto, bordo in alto → ingrandita.
Mentre il puntatore sta sul bordo si vede l'anteprima del posto (vetro nel colore d'accento, sotto la
finestra trascinata); la finestra si aggancia solo lasciando il tasto. Staccando dal bordo una finestra
agganciata, torna alla misura di prima, sotto il puntatore nella stessa proporzione (come Windows).
Il posto è sempre dentro l'area di lavoro: mai sotto la barra delle app.

Lo usa share/tod-edges.py, che segue già la finestra attiva (una connessione X sola, un processo solo).
Qui: la parte senza X (zone, posti, area di lavoro, conti della cornice: la prova tests/run.sh) e la
classe Aggancio con ctypes (libX11, libcairo; libXext se c'è).

xfwm4 4.20 ha un aggancio suo (tile_on_move), ma sposta la finestra subito, a metà trascinamento e senza
anteprima, e con wrap_windows acceso non aggancia affatto (era il caso di Pietro): lib/desktop.sh
xfwm_aggancio lo spegne quando c'è questo. Lo spostamento resta di xfwm4: qui si guarda solo dove va il
puntatore (XQueryPointer, 60 volte al secondo e solo mentre si trascina) e, lasciato il tasto, si chiede
il posto con _NET_MOVERESIZE_WINDOW (o _NET_WM_STATE per ingrandire). Solo dopo lo stacco di una finestra
agganciata la si muove da qui (vedi Aggancio.stacca).
"""
import ctypes
import ctypes.util
import math
import os
import sys
import time

PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
CASA = os.environ.get("TOD_TERMUX_HOME", os.path.expanduser("~"))
DEBUG = bool(os.environ.get("TOD_EDGES_DEBUG"))

ZONE = ("sinistra", "destra", "alto", "alto-sx", "alto-dx", "basso-sx", "basso-dx")


def log(*a):
    print("tod-aggancio:", *a, file=sys.stderr, flush=True)


def dbg(*a):
    if DEBUG:
        log(*a)


# --- parte senza X ----------------------------------------------------------------------------------------

def zona(px, py, mon, scala=1.0):
    """La zona di aggancio sotto il puntatore, o None. mon = (x, y, w, h) dello schermo.
    Basta toccare il bordo (il puntatore non va oltre); gli angoli sono un tratto di bordo lungo 1/7 del lato
    corto, così si prendono anche al volo. In basso, fuori dagli angoli, niente: lì c'è la barra."""
    mx, my, mw, mh = mon
    x, y = px - mx, py - my
    if not (0 <= x < mw and 0 <= y < mh):
        return None
    e = max(2, int(round(4 * scala)))
    c = max(int(round(48 * scala)), min(mw, mh) // 7)
    sx, dx = x <= e, x >= mw - 1 - e
    su, giu = y <= e, y >= mh - 1 - e
    if (sx and y < c) or (su and x < c):
        return "alto-sx"
    if (dx and y < c) or (su and x >= mw - c):
        return "alto-dx"
    if (sx and y >= mh - c) or (giu and x < c):
        return "basso-sx"
    if (dx and y >= mh - c) or (giu and x >= mw - c):
        return "basso-dx"
    if sx:
        return "sinistra"
    if dx:
        return "destra"
    if su:
        return "alto"
    return None


def area_lavoro(schermo, workarea=None, strut=()):
    """Dove vanno le finestre: lo schermo (W, H) stretto da _NET_WORKAREA e dalle richieste di spazio delle barre
    (_NET_WM_STRUT_PARTIAL / _NET_WM_STRUT: sinistra, destra, alto, basso). Le richieste si contano anche qui
    perché xfwm4, quando lo schermo cambia grandezza (telefono → DeX), a volte rifà l'area di lavoro senza la
    barra (vedi Barra._verifica_area in tod-barra.py): il posto non deve mai finire sotto la barra."""
    W, H = schermo
    x0, y0, x1, y1 = 0, 0, W, H
    if workarea and len(workarea) >= 4 and workarea[2] > 0 and workarea[3] > 0:
        x0, y0 = max(x0, workarea[0]), max(y0, workarea[1])
        x1, y1 = min(x1, workarea[0] + workarea[2]), min(y1, workarea[1] + workarea[3])
    for s in strut or ():
        s = list(s)[:4] + [0] * (4 - len(s[:4]))
        sx, dx, su, giu = s
        if 0 < sx < W // 2:
            x0 = max(x0, sx)
        if 0 < dx < W // 2:
            x1 = min(x1, W - dx)
        if 0 < su < H // 2:
            y0 = max(y0, su)
        if 0 < giu < H // 2:
            y1 = min(y1, H - giu)
    if x1 - x0 < 100 or y1 - y0 < 100:   # numeri senza senso: tutto lo schermo
        return (0, 0, W, H)
    return (x0, y0, x1 - x0, y1 - y0)


def posto(z, area):
    """Il rettangolo (visibile) della zona dentro l'area di lavoro."""
    x, y, w, h = area
    w2, h2 = w // 2, h // 2
    return {"sinistra": (x, y, w2, h), "destra": (x + w2, y, w - w2, h), "alto": (x, y, w, h),
            "alto-sx": (x, y, w2, h2), "alto-dx": (x + w2, y, w - w2, h2),
            "basso-sx": (x, y + h2, w2, h - h2), "basso-dx": (x + w2, y + h2, w - w2, h - h2)}.get(z)


def cornice_per(visibile, ombra):
    """Dalla parte visibile al rettangolo della cornice: le app con la barra propria (GTK con l'intestazione,
    _GTK_FRAME_EXTENTS) disegnano l'ombra dentro la loro finestra, che quindi è più grande di quanto si vede."""
    x, y, w, h = visibile
    sx, dx, su, giu = (list(ombra or ()) + [0, 0, 0, 0])[:4]
    return (x - sx, y - su, w + sx + dx, h + su + giu)


def cliente(cornice, estensioni):
    """Dal rettangolo della cornice a quello della finestra dell'app (_NET_FRAME_EXTENTS di xfwm4: bordo e titolo),
    da chiedere con _NET_MOVERESIZE_WINDOW e StaticGravity (x, y = posizione della finestra dell'app)."""
    x, y, w, h = cornice
    sx, dx, su, giu = (list(estensioni or ()) + [0, 0, 0, 0])[:4]
    return (x + sx, y + su, max(1, w - sx - dx), max(1, h - su - giu))


def staccata(ora, misura, puntatore):
    """Cornice di una finestra agganciata appena staccata dal bordo: la misura di prima, con il puntatore nella
    stessa proporzione in orizzontale e alla stessa distanza dal bordo in alto (resta sul titolo)."""
    fx, fy, fw, fh = ora
    w, h = misura
    px, py = puntatore
    rx = min(max((px - fx) / fw, 0.0), 1.0) if fw > 0 else 0.5
    dy = min(max(py - fy, 0), max(0, h - 1))
    return (px - int(round(rx * w)), py - dy, w, h)


def rientro(r, m):
    x, y, w, h = r
    if w > 4 * m and h > 4 * m:
        return (x + m, y + m, w - 2 * m, h - 2 * m)
    return r


def unione(a, b):
    """Il rettangolo più piccolo che contiene tutti e due (x, y, larghezza, altezza)."""
    x, y = min(a[0], b[0]), min(a[1], b[1])
    return (x, y, max(a[0] + a[2], b[0] + b[2]) - x, max(a[1] + a[3], b[1] + b[3]) - y)


def verso(da, a, t):
    """Da un rettangolo all'altro, con frenata finale (t da 0 a 1)."""
    k = 1 - (1 - min(max(t, 0.0), 1.0)) ** 3
    return tuple(int(round(p + (q - p) * k)) for p, q in zip(da, a))


def preferenze(conf=None):
    """Colore d'accento e tema scuro da tod.conf (ACCENTO, TEMA): si rileggono a ogni trascinamento."""
    accento, scuro = "#0067c0", False
    try:
        for r in open(conf or os.path.join(CASA, ".config", "tod", "tod.conf")):
            k, _, v = r.strip().partition("=")
            v = v.strip().strip('"')
            if k == "ACCENTO" and len(v) == 7 and v.startswith("#"):
                try:
                    int(v[1:], 16)
                    accento = v
                except ValueError:
                    pass
            elif k == "TEMA":
                scuro = v == "scuro"
    except OSError:
        pass
    return accento, scuro


def colori(accento, scuro):
    """Vetro del tema DeX OS: fondo come i pannelli (#f2f2f5 / #242428) velato, tinta e bordo nell'accento,
    filo interno chiaro (come il bordo dei pannelli)."""
    r, g, b = (int(accento[i:i + 2], 16) / 255 for i in (1, 3, 5))
    if scuro:
        return {"fondo": (0.141, 0.141, 0.157, 0.34), "tinta": (r, g, b, 0.28), "bordo": (r, g, b, 0.95),
                "filo": (1, 1, 1, 0.12), "pixel": (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)}
    return {"fondo": (0.949, 0.949, 0.961, 0.40), "tinta": (r, g, b, 0.24), "bordo": (r, g, b, 0.90),
            "filo": (1, 1, 1, 0.55), "pixel": (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)}


# --- parte con X ------------------------------------------------------------------------------------------

Button1Mask = 1 << 8
Expose, ExposureMask = 12, 1 << 15
InputOutput, TrueColor = 1, 4
CWBackPixel, CWBorderPixel, CWOverrideRedirect, CWEventMask, CWColormap = 1 << 1, 1 << 3, 1 << 9, 1 << 11, 1 << 13
CWSibling, CWStackMode, Below = 1 << 5, 1 << 6, 1
ClientMessage = 33
SubstructureNotifyMask, SubstructureRedirectMask = 1 << 19, 1 << 20
MOVERESIZE_CANCEL = 11
StaticGravity = 10


class Attributi(ctypes.Structure):   # XSetWindowAttributes
    _fields_ = [("background_pixmap", ctypes.c_ulong), ("background_pixel", ctypes.c_ulong),
                ("border_pixmap", ctypes.c_ulong), ("border_pixel", ctypes.c_ulong),
                ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int),
                ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong),
                ("save_under", ctypes.c_int), ("event_mask", ctypes.c_long),
                ("do_not_propagate_mask", ctypes.c_long), ("override_redirect", ctypes.c_int),
                ("colormap", ctypes.c_ulong), ("cursor", ctypes.c_ulong)]


class InfoVisual(ctypes.Structure):   # XVisualInfo
    _fields_ = [("visual", ctypes.c_void_p), ("visualid", ctypes.c_ulong), ("screen", ctypes.c_int),
                ("depth", ctypes.c_int), ("c_class", ctypes.c_int), ("red_mask", ctypes.c_ulong),
                ("green_mask", ctypes.c_ulong), ("blue_mask", ctypes.c_ulong), ("colormap_size", ctypes.c_int),
                ("bits_per_rgb", ctypes.c_int)]


class Cambi(ctypes.Structure):   # XWindowChanges
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int), ("height", ctypes.c_int),
                ("border_width", ctypes.c_int), ("sibling", ctypes.c_ulong), ("stack_mode", ctypes.c_int)]


class Messaggio(ctypes.Structure):   # XClientMessageEvent, dentro un XEvent da 24 long
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int), ("data", ctypes.c_long * 5), ("pad", ctypes.c_long * 12)]


def _carica(nomi):
    for n in nomi:
        if n:
            try:
                return ctypes.CDLL(n)
            except OSError:
                pass
    return None


class Anteprima:
    """Il posto dove andrà la finestra. Con il compositore di xfwm4: una finestra ARGB disegnata con cairo
    (vetro velato nell'accento, angoli tondi, bordo e filo interno), che cresce dal puntatore fino al posto.
    Senza compositore la trasparenza non c'è: un contorno pieno nell'accento (4 strisce)."""
    DURATA = 0.14
    PASSI_CAIRO = {"cairo_xlib_surface_create": (ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                                                      ctypes.c_int, ctypes.c_int]),
                   "cairo_xlib_surface_set_size": (None, [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]),
                   "cairo_create": (ctypes.c_void_p, [ctypes.c_void_p]),
                   "cairo_set_operator": (None, [ctypes.c_void_p, ctypes.c_int]),
                   "cairo_set_source_rgba": (None, [ctypes.c_void_p] + [ctypes.c_double] * 4),
                   "cairo_paint": (None, [ctypes.c_void_p]), "cairo_new_sub_path": (None, [ctypes.c_void_p]),
                   "cairo_arc": (None, [ctypes.c_void_p] + [ctypes.c_double] * 5),
                   "cairo_close_path": (None, [ctypes.c_void_p]), "cairo_fill": (None, [ctypes.c_void_p]),
                   "cairo_fill_preserve": (None, [ctypes.c_void_p]), "cairo_stroke": (None, [ctypes.c_void_p]),
                   "cairo_set_line_width": (None, [ctypes.c_void_p, ctypes.c_double]),
                   "cairo_push_group": (None, [ctypes.c_void_p]),
                   "cairo_pop_group_to_source": (None, [ctypes.c_void_p]),
                   "cairo_destroy": (None, [ctypes.c_void_p]), "cairo_surface_flush": (None, [ctypes.c_void_p])}

    def __init__(self, a):
        self.a, self.x, self.dpy = a, a.x, a.dpy
        self.c = _carica([os.path.join(PREFIX, "lib/libcairo.so"), ctypes.util.find_library("cairo"),
                          "libcairo.so.2"])
        if self.c:
            try:
                for nome, (res, args) in self.PASSI_CAIRO.items():
                    f = getattr(self.c, nome)
                    f.restype, f.argtypes = res, args
            except AttributeError:   # cairo senza xlib
                self.c = None
        self.vetro = self.sup = 0
        self.strisce = []
        self.modo = None
        self.rett = self.da = self.a_rett = None
        self.tela = None   # il rettangolo della finestra ARGB: tutta l'animazione ci sta dentro
        self.t0 = 0.0
        self.sotto = 0
        self.col = colori("#0067c0", False)
        self.raggio = max(8, int(round(12 * a.s)))

    def finestre(self):
        return ([self.vetro] if self.vetro else []) + self.strisce

    def _vetro(self):
        """La finestra ARGB, creata la prima volta che serve. False se non si può (niente cairo o visual a 32 bit)."""
        if self.vetro:
            return True
        if not self.c:
            return False
        x, dpy = self.x, self.dpy
        vi = InfoVisual()
        if not x.XMatchVisualInfo(dpy, x.XDefaultScreen(dpy), 32, TrueColor, ctypes.byref(vi)):
            return False
        at = Attributi()
        at.override_redirect = 1
        at.colormap = x.XCreateColormap(dpy, self.a.root, vi.visual, 0)
        at.border_pixel = at.background_pixel = 0
        at.event_mask = ExposureMask
        w = x.XCreateWindow(dpy, self.a.root, 0, 0, 1, 1, 0, 32, InputOutput, vi.visual,
                            CWOverrideRedirect | CWColormap | CWBorderPixel | CWBackPixel | CWEventMask,
                            ctypes.byref(at))
        if not w:
            return False
        x.XStoreName(dpy, w, b"tod-aggancio")
        self.a.senza_input(w)
        self.sup = self.c.cairo_xlib_surface_create(dpy, w, vi.visual, 1, 1)
        self.vetro = w
        return True

    def _strisce(self):
        if not self.strisce:
            for _ in range(4):
                at = Attributi()
                at.override_redirect = 1
                w = self.x.XCreateWindow(self.dpy, self.a.root, 0, 0, 1, 1, 0, 0, InputOutput, None,
                                         CWOverrideRedirect | CWBackPixel, ctypes.byref(at))
                self.x.XStoreName(self.dpy, w, b"tod-aggancio")
                self.a.senza_input(w)
                self.strisce.append(w)
        for w in self.strisce:
            self.x.XSetWindowBackground(self.dpy, w, self.col["pixel"])
            self.x.XClearWindow(self.dpy, w)

    def mostra(self, meta, punto, sotto):
        """Anteprima verso il rettangolo meta; se non c'era, cresce da un rettangolo piccolo attorno al punto."""
        modo = "vetro" if self.a.compositore() and self._vetro() else "contorno"
        if self.modo and modo != self.modo:
            self.nascondi()
        if self.rett is None:
            x, y, w, h = meta
            pw, ph = max(1, w * 3 // 10), max(1, h * 3 // 10)
            px = min(max(punto[0] - pw // 2, x), x + w - pw)
            py = min(max(punto[1] - ph // 2, y), y + h - ph)
            inizio, nuova = (px, py, pw, ph), True
        else:
            inizio, nuova = self.rett, False
        self.modo, self.sotto = modo, sotto
        self.da, self.a_rett, self.t0 = inizio, meta, time.monotonic()
        if modo == "contorno":
            self._strisce()
        else:
            # La finestra prende UNA volta la misura che contiene tutta l'animazione, e dentro si disegna soltanto.
            # Ridimensionarla a ogni fotogramma la svuotava ogni volta (il server azzera la finestra, il compositore
            # la mostra vuota, poi arriva il disegno): sul S20+, senza la via veloce, l'anteprima sfarfallava.
            tela = unione(inizio, meta)
            if tela != self.tela:
                self.tela = tela
                self.x.XMoveResizeWindow(self.dpy, self.vetro, tela[0], tela[1], max(1, tela[2]), max(1, tela[3]))
                self.c.cairo_xlib_surface_set_size(self.sup, max(1, tela[2]), max(1, tela[3]))
        self._disponi(inizio, nuova)

    def anima(self):
        if self.modo is None or self.a_rett is None or self.rett == self.a_rett:
            return
        t = (time.monotonic() - self.t0) / self.DURATA
        self._disponi(self.a_rett if t >= 1 else verso(self.da, self.a_rett, t))

    def _sotto(self, w):
        """Subito sotto la finestra trascinata, come Windows (la si vede sopra l'anteprima)."""
        if self.sotto:
            ch = Cambi(sibling=self.sotto, stack_mode=Below)
            self.x.XConfigureWindow(self.dpy, w, CWSibling | CWStackMode, ctypes.byref(ch))

    def _disponi(self, r, mappa=False):
        x, dpy = self.x, self.dpy
        self.rett = r
        rx, ry, rw, rh = r
        if self.modo == "vetro":
            if mappa:
                x.XMapWindow(dpy, self.vetro)
                self._sotto(self.vetro)
            self.disegna()
        else:
            t = max(2, int(round(3 * self.a.s)))
            pezzi = [(rx, ry, rw, t), (rx, ry + rh - t, rw, t), (rx, ry + t, t, rh - 2 * t), (rx + rw - t, ry + t, t, rh - 2 * t)]
            for w, (px, py, pw, ph) in zip(self.strisce, pezzi):
                x.XMoveResizeWindow(dpy, w, px, py, max(1, pw), max(1, ph))
                if mappa:
                    x.XMapWindow(dpy, w)
                    self._sotto(w)
        x.XFlush(dpy)

    def _tondo(self, cr, x, y, w, h, r):
        c = self.c
        r = max(0.0, min(r, w / 2, h / 2))
        c.cairo_new_sub_path(cr)
        c.cairo_arc(cr, x + w - r, y + r, r, -math.pi / 2, 0)
        c.cairo_arc(cr, x + w - r, y + h - r, r, 0, math.pi / 2)
        c.cairo_arc(cr, x + r, y + h - r, r, math.pi / 2, math.pi)
        c.cairo_arc(cr, x + r, y + r, r, math.pi, 3 * math.pi / 2)
        c.cairo_close_path(cr)

    def disegna(self):
        if self.modo != "vetro" or not self.rett or not self.tela:
            return
        c, col = self.c, self.col
        w, h = max(1, self.rett[2]), max(1, self.rett[3])
        ox, oy = self.rett[0] - self.tela[0], self.rett[1] - self.tela[1]
        cr = c.cairo_create(self.sup)
        # il fotogramma si compone fuori dalla finestra e ci arriva con una copia sola: il compositore non vede
        # mai la finestra svuotata o disegnata a metà
        c.cairo_push_group(cr)
        c.cairo_set_operator(cr, 1)   # SOURCE: si parte da trasparente
        c.cairo_set_source_rgba(cr, 0, 0, 0, 0)
        c.cairo_paint(cr)
        c.cairo_set_operator(cr, 2)   # OVER
        self._tondo(cr, ox + 0.5, oy + 0.5, w - 1, h - 1, self.raggio)
        c.cairo_set_source_rgba(cr, *col["fondo"])
        c.cairo_fill_preserve(cr)
        c.cairo_set_source_rgba(cr, *col["tinta"])
        c.cairo_fill_preserve(cr)
        c.cairo_set_line_width(cr, 1.0)
        c.cairo_set_source_rgba(cr, *col["bordo"])
        c.cairo_stroke(cr)
        self._tondo(cr, ox + 1.5, oy + 1.5, w - 3, h - 3, self.raggio - 1)
        c.cairo_set_source_rgba(cr, *col["filo"])
        c.cairo_stroke(cr)
        c.cairo_pop_group_to_source(cr)
        c.cairo_set_operator(cr, 1)   # SOURCE: sostituisce tutto in un colpo
        c.cairo_paint(cr)
        c.cairo_destroy(cr)
        c.cairo_surface_flush(self.sup)
        self.x.XFlush(self.dpy)

    def nascondi(self):
        if self.modo is None:
            return
        for w in self.finestre():
            self.x.XUnmapWindow(self.dpy, w)
        self.x.XFlush(self.dpy)
        self.modo = self.rett = self.da = self.a_rett = None
        self.tela = None


class Aggancio:
    """Segue i trascinamenti della finestra attiva per conto di tod-edges.py (Edges: x, dpy, root, atom, prop,
    geometry, tocca_radice). tod-edges chiama: vista() quando cambia finestra attiva, cornice_mossa() a ogni
    ConfigureNotify della sua cornice, attesa()/passo() nel ciclo degli eventi, esposta() per i suoi Expose."""
    PASSO = 0.016

    def __init__(self, e, scala=1.0):
        # una copia nostra delle funzioni di libX11 (stessa libreria, stessa connessione): così le firme qui
        # sotto non cambiano quelle di tod-edges.py (XCreateWindow e XSendEvent hanno lì i loro tipi)
        self.e, self.x, self.dpy, self.root = e, ctypes.CDLL(e.x._name), e.dpy, e.root
        self.s = scala
        P, U, I, V = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_uint
        sig = {
            "XMoveResizeWindow": (I, [P, U, I, I, V, V]), "XUnmapWindow": (I, [P, U]), "XFlush": (I, [P]),
            "XQueryPointer": (I, [P, U, ctypes.POINTER(U), ctypes.POINTER(U), ctypes.POINTER(I), ctypes.POINTER(I),
                                  ctypes.POINTER(I), ctypes.POINTER(I), ctypes.POINTER(V)]),
            "XGetSelectionOwner": (U, [P, U]), "XDefaultScreen": (I, [P]),
            "XMatchVisualInfo": (I, [P, I, I, I, ctypes.POINTER(InfoVisual)]),
            "XCreateColormap": (U, [P, U, P, I]), "XStoreName": (I, [P, U, ctypes.c_char_p]),
            "XConfigureWindow": (I, [P, U, V, ctypes.POINTER(Cambi)]),
            "XDeleteProperty": (I, [P, U, U]), "XMapWindow": (I, [P, U]),
            "XSetWindowBackground": (I, [P, U, U]), "XClearWindow": (I, [P, U]),
            "XSendEvent": (I, [P, U, I, ctypes.c_long, P]),
            "XChangeProperty": (I, [P, U, U, U, I, I, ctypes.POINTER(ctypes.c_long), I]),
            "XCreateWindow": (U, [P, U, I, I, V, V, V, I, V, P, U, ctypes.POINTER(Attributi)]),
        }
        for nome, (res, args) in sig.items():
            f = getattr(self.x, nome)
            f.restype, f.argtypes = res, args
        self.xext = _carica([os.path.join(PREFIX, "lib/libXext.so"), ctypes.util.find_library("Xext"), "libXext.so.6"])
        if self.xext:
            self.xext.XShapeCombineRectangles.restype = I
            self.xext.XShapeCombineRectangles.argtypes = [P, U, I, I, I, P, I, I, I]
        self.anteprima = Anteprima(self)
        self.tr = None          # trascinamento in corso
        self.compiti = []       # [(quando, funzione)]
        self.ultima = (0, None)  # (cornice, geometria) vista per ultima
        self.prossimo = 0.0

    # --- aiuti X
    def senza_input(self, w):
        """I clic passano attraverso (forma d'ingresso vuota), se c'è libXext."""
        if self.xext:
            self.xext.XShapeCombineRectangles(self.dpy, w, 2, 0, 0, None, 0, 0, 0)   # ShapeInput, ShapeSet

    def compositore(self):
        scr = self.x.XDefaultScreen(self.dpy)
        return bool(self.x.XGetSelectionOwner(self.dpy, self.e.atom(f"_NET_WM_CM_S{scr}")))

    def puntatore(self):
        r, c = ctypes.c_ulong(), ctypes.c_ulong()
        x, y, wx, wy, m = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_uint()
        if not self.x.XQueryPointer(self.dpy, self.root, ctypes.byref(r), ctypes.byref(c), ctypes.byref(x),
                                    ctypes.byref(y), ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(m)):
            return None
        return x.value, y.value, m.value

    def schermo(self):
        g = self.e.geometry(self.root)
        return (g[2], g[3]) if g else (1, 1)

    def area(self, schermo):
        e = self.e
        wa = e.prop(self.root, "_NET_WORKAREA") or []
        cur = (e.prop(self.root, "_NET_CURRENT_DESKTOP") or [0])[0]
        if len(wa) >= 4 * (cur + 1):
            wa = wa[4 * cur:4 * cur + 4]
        strut = []
        for w in e.prop(self.root, "_NET_CLIENT_LIST") or []:
            s = e.prop(w, "_NET_WM_STRUT_PARTIAL") or e.prop(w, "_NET_WM_STRUT")
            if s:
                strut.append(s[:4])
        return area_lavoro(schermo, wa, strut)

    def estensioni(self, w, nome):
        v = self.e.prop(w, nome) or []
        return list(v[:4]) if len(v) >= 4 else [0, 0, 0, 0]

    def manda(self, w, tipo, dati):
        m = Messaggio()
        m.type, m.window, m.format = ClientMessage, w, 32
        m.message_type = self.e.atom(tipo)
        for i, v in enumerate(dati[:5]):
            m.data[i] = v
        self.x.XSendEvent(self.dpy, self.root, 0, SubstructureRedirectMask | SubstructureNotifyMask, ctypes.byref(m))
        self.x.XFlush(self.dpy)

    def sposta(self, w, r):
        """_NET_MOVERESIZE_WINDOW con x, y, larghezza e altezza; StaticGravity: x, y della finestra dell'app."""
        x, y, ww, hh = r
        self.manda(w, "_NET_MOVERESIZE_WINDOW", [StaticGravity | (0xF << 8) | (2 << 12), x, y, ww, hh])

    def salvata(self, w):
        v = self.e.prop(w, "_TOD_AGGANCIO")
        return list(v[:4]) if v and len(v) >= 4 else None

    def segna(self, w, valori):
        arr = (ctypes.c_long * 4)(*valori)
        self.x.XChangeProperty(self.dpy, w, self.e.atom("_TOD_AGGANCIO"), 6, 32, 0, arr, 4)   # CARDINAL
        self.x.XFlush(self.dpy)

    def dimentica(self, w):
        self.x.XDeleteProperty(self.dpy, w, self.e.atom("_TOD_AGGANCIO"))

    def agganciabile(self, w):
        """Finestre normali e dialoghi ridimensionabili, non a schermo intero (come Windows)."""
        e = self.e
        tipi = e.prop(w, "_NET_WM_WINDOW_TYPE") or []
        if tipi and not ({e.atom("_NET_WM_WINDOW_TYPE_NORMAL"), e.atom("_NET_WM_WINDOW_TYPE_DIALOG")} & set(tipi)):
            return False
        if e.atom("_NET_WM_STATE_FULLSCREEN") in set(e.prop(w, "_NET_WM_STATE") or []):
            return False
        hints = e.prop(w, "WM_NORMAL_HINTS") or []   # XSizeHints: flags, ..., min (5,6), max (7,8)
        if len(hints) > 8 and hints[0] & 16 and hints[0] & 32 and hints[5:7] == hints[7:9]:
            return False
        return True

    def compito(self, dopo, f):
        self.compiti.append((time.monotonic() + dopo, f))

    # --- chiamate da tod-edges
    def vista(self, frame):
        self.ultima = (frame, self.e.geometry(frame) if frame else None)

    def cornice_mossa(self, client, frame):
        """ConfigureNotify della cornice attiva: se si è spostata senza cambiare misura con il tasto sinistro giù,
        xfwm4 la sta trascinando."""
        geo = self.e.geometry(frame) if frame else None
        prima, self.ultima = self.ultima, (frame, geo)
        if self.tr or not client or not geo or prima[0] != frame or not prima[1]:
            return
        if prima[1][2:] != geo[2:] or prima[1][:2] == geo[:2]:
            return
        p = self.puntatore()
        if not p or not p[2] & Button1Mask or not self.agganciabile(client):
            return
        sch = self.schermo()
        t = {"client": client, "frame": frame, "zona": None, "persa": False, "fermo": 0, "ripresa": False,
             "px0": p[0], "prima": prima[1], "schermo": sch, "area": self.area(sch)}
        self.tr = t
        self.anteprima.col = colori(*preferenze())
        dbg("trascinamento di", hex(client), "da", prima[1], "area", t["area"])
        s = self.salvata(client)
        if s and abs(geo[2] - s[2]) <= 2 and abs(geo[3] - s[3]) <= 2 and (s[0], s[1]) != (geo[2], geo[3]):
            self.stacca(t, (s[0], s[1]), p)
        elif s:
            self.dimentica(client)   # ridimensionata a mano dopo l'aggancio: la misura vecchia non vale più

    def attesa(self):
        """Quanto può dormire il ciclo degli eventi: None = finché non arriva un evento X."""
        quando = [q for q, _f in self.compiti]
        if self.tr:
            quando.append(self.prossimo)
        if not quando:
            return None
        return max(0.0, min(quando) - time.monotonic())

    def passo(self):
        ora = time.monotonic()
        pronti = [f for q, f in self.compiti if q <= ora]
        self.compiti = [(q, f) for q, f in self.compiti if q > ora]
        for f in pronti:
            f()
        if self.tr and ora >= self.prossimo:
            self.prossimo = ora + self.PASSO
            self.segui()

    def esposta(self, w):
        if w == self.anteprima.vetro:
            self.anteprima.disegna()

    # --- il trascinamento
    def segui(self):
        t = self.tr
        p = self.puntatore()
        if p is None or not p[2] & Button1Mask:
            self.lasciata(t)
            return
        if t["ripresa"] or t["persa"]:
            return
        px, py = p[0], p[1]
        if t.get("guida"):   # dopo lo stacco la finestra la muoviamo noi
            ox, oy, cw, ch = t["guida"]
            nx, ny = px - ox, py - oy
            if (nx, ny) != t.get("pos"):
                t["pos"] = (nx, ny)
                self.sposta(t["client"], (nx + t["est"][0], ny + t["est"][2], cw, ch))
            geo = (nx, ny, 0, 0)
        else:
            geo = self.e.geometry(t["frame"])
        if geo is None:
            self.tr = None
            self.anteprima.nascondi()
            return
        # Esc durante lo spostamento: xfwm4 rimette la finestra esattamente dov'era prima e non la muove più.
        # Se resta lì per qualche passo con il puntatore lontano, il trascinamento è finito: niente aggancio.
        # Solo in orizzontale: in alto xfwm4 ferma la cornice al bordo mentre il puntatore sale ancora
        s = self.s
        a_casa = not t.get("guida") and abs(geo[0] - t["prima"][0]) <= 1 and abs(geo[1] - t["prima"][1]) <= 1
        if a_casa and abs(px - t["px0"]) > 32 * s:
            t["fermo"] += 1
            if t["fermo"] >= 4:
                log("la finestra è tornata al suo posto (Esc?): niente aggancio")
                t["persa"] = True
                self.anteprima.nascondi()
                return
        else:
            t["fermo"] = 0
        z = zona(px, py, (0, 0) + tuple(t["schermo"]), s)
        if z != t["zona"]:
            t["zona"] = z
            dbg("zona", z, "puntatore", (px, py))
            if z:
                self.anteprima.mostra(rientro(posto(z, t["area"]), int(round(8 * s))), (px, py), t["frame"])
            else:
                self.anteprima.nascondi()
        self.anteprima.anima()

    def lasciata(self, t):
        self.tr = None
        self.anteprima.nascondi()
        if t["persa"] or t["ripresa"] or not t["zona"]:
            return
        self.aggancia(t)

    def aggancia(self, t):
        client, z = t["client"], t["zona"]
        if z == "alto":
            self.dimentica(client)
            e = self.e
            self.manda(client, "_NET_WM_STATE", [1, e.atom("_NET_WM_STATE_MAXIMIZED_VERT"),
                                                 e.atom("_NET_WM_STATE_MAXIMIZED_HORZ"), 2, 0])
            self.compito(0.08, lambda: self.ingrandita(t, 6))
            log("ingrandita", hex(client))
            return
        meta = cornice_per(posto(z, t["area"]), self.estensioni(client, "_GTK_FRAME_EXTENTS"))
        cl = cliente(meta, self.estensioni(client, "_NET_FRAME_EXTENTS"))
        self.sposta(client, cl)
        self.compito(0.06, lambda: self.verifica(t, meta, cl, 6))

    def verifica(self, t, meta, cl, n):
        """xfwm4 rifiuta lo spostamento finché non ha chiuso il suo (il rilascio del tasto lo vede dopo di noi):
        si riprova per qualche decimo di secondo. Poi si ricorda la misura di prima per lo stacco."""
        geo = self.e.geometry(t["frame"])
        if geo and abs(geo[0] - meta[0]) <= 2 and abs(geo[1] - meta[1]) <= 2:
            self.segna(t["client"], [t["prima"][2], t["prima"][3], geo[2], geo[3]])
            log(t["zona"], hex(t["client"]), "→", geo, "(prima", t["prima"][2:], ")")
            return
        if n > 0 and self.tr is None:
            self.sposta(t["client"], cl)
            self.compito(0.06, lambda: self.verifica(t, meta, cl, n - 1))
        elif n <= 0:
            log("xfwm4 non ha spostato", hex(t["client"]), "in", meta, ": è", geo)

    def ingrandita(self, t, n):
        e = self.e
        stato = set(e.prop(t["client"], "_NET_WM_STATE") or [])
        if e.atom("_NET_WM_STATE_MAXIMIZED_HORZ") in stato or n <= 0 or self.tr is not None:
            return
        self.manda(t["client"], "_NET_WM_STATE", [1, e.atom("_NET_WM_STATE_MAXIMIZED_VERT"),
                                                   e.atom("_NET_WM_STATE_MAXIMIZED_HORZ"), 2, 0])
        self.compito(0.08, lambda: self.ingrandita(t, n - 1))

    def stacca(self, t, misura, p):
        """Finestra agganciata presa dal titolo: lo spostamento di xfwm4 si chiude (_NET_WM_MOVERESIZE_CANCEL),
        la finestra torna alla misura di prima sotto il puntatore, e da lì fino al rilascio la muoviamo noi
        (_NET_MOVERESIZE_WINDOW a ogni passo). xfwm4 esce dal suo ciclo al primo evento dopo l'annullamento (la
        proprietà cambiata sulla radice glielo dà subito) e poi aspetta il rilascio del tasto tenendo il mouse.
        Non si può fargli riprendere lo spostamento (_NET_WM_MOVERESIZE_MOVE): il nuovo spostamento si prende
        il rilascio, quello vecchio resta ad aspettarlo per sempre e il trascinamento dopo non funziona più
        (provato il 15 novembre su xfwm4 4.20)."""
        t["ripresa"] = True
        self.manda(t["client"], "_NET_WM_MOVERESIZE", [p[0], p[1], MOVERESIZE_CANCEL, 1, 1])
        self.e.tocca_radice()
        self.compito(0.03, lambda: self.riprendi(t, misura))

    def riprendi(self, t, misura):
        client = t["client"]
        p = self.puntatore()
        geo = self.e.geometry(t["frame"])
        if not p or not geo:
            self.tr = None
            return
        nuova = staccata(geo, misura, p[:2])
        est = self.estensioni(client, "_NET_FRAME_EXTENTS")
        cl = cliente(nuova, est)
        self.dimentica(client)
        self.sposta(client, cl)
        if self.tr is t and p[2] & Button1Mask:
            t.update(ripresa=False, px0=p[0], prima=nuova, fermo=0, est=est, pos=nuova[:2],
                     guida=(p[0] - nuova[0], p[1] - nuova[1], cl[2], cl[3]))
        elif self.tr is t:
            self.tr = None
        log("staccata", hex(client), "→ misura di prima", misura)
