"""Anteprime delle finestre nella barra (tod-barra.py): i conti e le decisioni, senza grafica né X (li prova
tests/run.sh, test_barra_anteprime).

Le finestre ridotte a icona X non le disegna più, quindi la loro immagine va presa prima. Con il compositore di
xfwm4 ogni finestra ha la sua immagine fuori schermo (estensione Composite): la barra se ne tiene un riferimento
(XCompositeNameWindowPixmap: è la stessa immagine, nessuna copia) finché la finestra è sullo schermo. Quando sparisce
(ridotta a icona, mostra desktop, altro spazio di lavoro) il riferimento conserva l'ultima immagine: se ne fa la
miniatura, che resta in memoria (pochi KB), e il riferimento si libera. Nessuna cattura continua: le miniature delle
finestre visibili si prendono solo quando si apre l'anteprima (e una volta al secondo mentre è aperta).
"""
import collections

LARGHEZZA, ALTEZZA = 216, 128   # spazio della miniatura a 96 DPI (la barra lo scala con i DPI dello schermo)
ATTESA_MS = 250                 # dopo la sparizione: la finestra si è chiusa (niente foto) o è ridotta (foto)
VIVA_MS = 1000                  # anteprima aperta: le finestre visibili si rifotografano una volta al secondo
TETTO = 6 << 20                 # byte di miniature al più (~50 finestre): oltre, via le usate meno di recente


def misura(w, h, larg=LARGHEZZA, alt=ALTEZZA):
    """la miniatura che sta in larg×alt con le stesse proporzioni della finestra, mai ingrandita"""
    if w <= 0 or h <= 0 or larg <= 0 or alt <= 0:
        return 0, 0
    k = min(larg / w, alt / h, 1.0)
    return max(1, int(round(w * k))), max(1, int(round(h * k)))


def passi(w, h, dw, dh):
    """Riduzione da w×h a dw×dh fatta nel server X (XRender), che filtra solo in modo bilineare: da 1920 a 200
    in un colpo prenderebbe un pixel ogni 10 e il testo verrebbe a pezzi. Allora si dimezza finché si può (ogni
    pixel è la media esatta di 2×2) e l'ultimo passo, al più di 2 volte, va alla misura giusta.
    → [(larghezza, altezza, scala x, scala y)]"""
    out = []
    while w >= 2 * dw and h >= 2 * dh and w > 1 and h > 1:
        w, h = (w + 1) // 2, (h + 1) // 2
        out.append((w, h, 2.0, 2.0))
    if not out or (w, h) != (dw, dh):
        out.append((dw, dh, w / dw, h / dh))
    return out


def fisso(v):
    """numero di XRender (XFixed: 16 bit di intero, 16 di frazione)"""
    return int(round(v * 65536))


def trasformazione(sx, sy, ox=0.0, oy=0.0):
    """matrice 3×3 di XRender, dal punto della miniatura a quello dell'immagine (scala e spostamento)"""
    return [fisso(sx), 0, fisso(ox), 0, fisso(sy), fisso(oy), 0, 0, fisso(1)]


def ritaglio(cornice, cliente, ombra=None):
    """La parte da mostrare dell'immagine della cornice (la finestra di xfwm4 attorno all'app): l'app senza la
    barra del titolo di xfwm4 (il titolo c'è già sopra la miniatura) e senza l'ombra trasparente delle app con la
    barra propria (_GTK_FRAME_EXTENTS). Sempre dentro l'immagine.
    cornice = (larghezza, altezza), cliente = (x, y, larghezza, altezza) dentro la cornice,
    ombra = (sinistra, destra, alto, basso) → (x, y, larghezza, altezza)"""
    fw, fh = cornice
    x, y, w, h = cliente
    s, d, a, b = (tuple(ombra) + (0, 0, 0, 0))[:4] if ombra else (0, 0, 0, 0)
    if w - s - d >= 8 and h - a - b >= 8:   # un'ombra più grande della finestra non ha senso: si ignora
        x, y, w, h = x + s, y + a, w - s - d, h - a - b
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(fw, x + w), min(fh, y + h)
    if x1 - x0 < 1 or y1 - y0 < 1:
        return 0, 0, fw, fh
    return x0, y0, x1 - x0, y1 - y0


def quante(n, larg_scheda, spazio, disponibile):
    """quante schede affiancate stanno nella larghezza dello schermo (almeno una)"""
    k = n
    while k > 1 and k * larg_scheda + (k - 1) * spazio > disponibile:
        k -= 1
    return max(1, min(n, k)) if n else 0


class Cassetto:
    """Le miniature in memoria: finestra → (miniatura, peso in byte). Al più `tetto` byte: oltre, via quelle usate
    meno di recente. Quelle delle finestre chiuse si buttano subito (tieni_solo)."""

    def __init__(self, tetto=TETTO):
        self.tetto = tetto
        self.voci = collections.OrderedDict()

    def metti(self, xid, mini, peso):
        self.voci.pop(xid, None)
        self.voci[xid] = (mini, peso)
        while self.peso() > self.tetto and len(self.voci) > 1:
            self.voci.popitem(last=False)

    def prendi(self, xid):
        v = self.voci.get(xid)
        if v is None:
            return None
        self.voci.move_to_end(xid)
        return v[0]

    def togli(self, xid):
        self.voci.pop(xid, None)

    def tieni_solo(self, vive):
        for k in [k for k in self.voci if k not in vive]:
            del self.voci[k]

    def peso(self):
        return sum(p for _m, p in self.voci.values())


class Cornici:
    """Per ogni cornice: il riferimento all'immagine del compositore (pixmap) e la sua misura. I metodi dicono cosa
    fare ("nomina" = prendere un riferimento nuovo, "aspetta", "fotografa", "libera"); le chiamate a X le fa la barra.
    Il riferimento va rifatto quando la finestra ricompare (X le dà un'immagine nuova) e quando cambia misura (quella
    vecchia resterebbe in memoria e ferma)."""

    def __init__(self):
        self.rif = {}          # cornice → (pixmap, larghezza, altezza)
        self.mappate = set()

    def mappata(self, c):
        self.mappate.add(c)
        return "nomina"

    def cambiata(self, c, w, h):
        r = self.rif.get(c)
        if c in self.mappate and r is not None and (r[1], r[2]) != (w, h):
            return "nomina"
        return None

    def nominata(self, c, pix, w, h):
        """→ il riferimento vecchio da liberare (o None)"""
        vecchio = self.rif.get(c)
        self.rif[c] = (pix, w, h)
        return vecchio[0] if vecchio else None

    def smappata(self, c):
        self.mappate.discard(c)
        return "aspetta" if c in self.rif else None

    def dopo_attesa(self, c, viva):
        """sparita da ATTESA_MS: ridotta a icona (o altrove) → foto; chiusa → si libera e basta"""
        if c in self.mappate or c not in self.rif:   # già tornata sullo schermo, o già liberata
            return None
        return "fotografa" if viva else "libera"

    def libera(self, c):
        """→ il riferimento da liberare (o None)"""
        r = self.rif.pop(c, None)
        return r[0] if r else None

    def distrutta(self, c):
        self.mappate.discard(c)
        return self.libera(c)
