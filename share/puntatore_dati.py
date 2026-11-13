"""puntatore_dati.py — lo stile e la dimensione del puntatore per Impostazioni → Mouse (13 novembre 2026). Senza grafica.

Pietro: "da dove lo cambio lo stile del mouse?". Gli stili sono i due puntatori DeX OS (share/dexos/dexos-cursors.py):
corpo scuro (come macOS) e corpo chiaro (come Windows), nel colore d'accento; si cambiano con `tod aspetto puntatore
scuro|chiaro`, che tiene anche il puntatore delle app di Debian. Le anteprime si leggono dai file Xcursor veri.
"""
import os
import struct

HOME = os.environ.get("TOD_TERMUX_HOME") or os.path.expanduser("~")
ICONE = os.path.join(HOME, ".local/share/icons")
IMMAGINE = 0xFFFD0002   # tipo dei pezzi "immagine" nei file Xcursor
# dimensione: per cento della misura normale (24 pixel a 96 DPI, poi in proporzione allo schermo: lib/look.sh)
DIMENSIONI = (100, 300)
FORME = ("left_ptr", "pointer", "xterm", "watch")   # freccia, mano, testo, attesa
ALIAS = {"pointer": ("hand2", "hand1"), "xterm": ("text", "ibeam"), "left_ptr": ("default", "arrow"),
         "watch": ("wait", "progress", "left_ptr_watch")}


def stili(nomi_dexos):
    """[(chiave, nome del tema)] dai nomi DeX OS di tod (DEXOS_NAMES: il 6° è il puntatore scuro, il 7° il chiaro)"""
    n = (nomi_dexos or "").split()
    out = []
    for i, chiave in ((5, "scuro"), (6, "chiaro")):
        if len(n) > i and os.path.isfile(os.path.join(ICONE, n[i], "index.theme")):
            out.append((chiave, n[i]))
    return out


def file_forma(tema, forma, radice=ICONE):
    """il file Xcursor di una forma (o di un suo nome alternativo), o None"""
    for nome in (forma,) + ALIAS.get(forma, ()):
        p = os.path.join(radice, tema, "cursors", nome)
        if os.path.isfile(p):
            return p
    return None


def leggi(dati, misura):
    """il primo fotogramma più vicino a MISURA in un file Xcursor → (larghezza, altezza, x, y, RGBA) o None.
    I pixel Xcursor sono ARGB premoltiplicati (interi little-endian): qui RGBA normali, come vuole GdkPixbuf."""
    if len(dati) < 16 or dati[:4] != b"Xcur":
        return None
    _testa, _ver, n = struct.unpack_from("<III", dati, 4)
    pezzi = []
    for i in range(min(n, 4096)):
        o = 16 + 12 * i
        if o + 12 > len(dati):
            break
        tipo, sotto, pos = struct.unpack_from("<III", dati, o)
        if tipo == IMMAGINE:
            pezzi.append((abs(sotto - misura), i, pos))
    for _d, _i, pos in sorted(pezzi):
        if pos + 36 > len(dati):
            continue
        _h, tipo, _s, _v, w, h, xh, yh, _rit = struct.unpack_from("<9I", dati, pos)
        if tipo != IMMAGINE or not (0 < w <= 512 and 0 < h <= 512) or pos + 36 + 4 * w * h > len(dati):
            continue
        px = dati[pos + 36:pos + 36 + 4 * w * h]
        out = bytearray(4 * w * h)
        for k in range(0, len(px), 4):
            b, g, r, a = px[k], px[k + 1], px[k + 2], px[k + 3]
            if a:
                r, g, b = min(255, r * 255 // a), min(255, g * 255 // a), min(255, b * 255 // a)
            out[k:k + 4] = bytes((r, g, b, a))
        return w, h, xh, yh, bytes(out)
    return None


def misura_px(per_cento, dpi):
    """i pixel del puntatore: 24 a 96 DPI, in proporzione allo schermo e alla dimensione scelta (come lib/look.sh)"""
    base = 24 * max(DIMENSIONI[0], min(DIMENSIONI[1], per_cento)) // 100
    return (base * dpi + 48) // 96
