"""Foto, Video e Musica di Termux On DeX: la parte senza grafica (cartelle, date, coda, formati, impostazioni).

Niente GTK qui dentro: i test (tests/run.sh) la usano così com'è, senza schermo.
"""
import json
import os
import random
import re
import time

IMMAGINI = {".jpg", ".jpeg", ".jpe", ".png", ".gif", ".webp", ".heic", ".heif", ".avif", ".bmp", ".tif", ".tiff",
            ".jxl", ".svg", ".ico", ".tga", ".pnm", ".ppm"}
VIDEO = {".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".3gp", ".3g2", ".mpg", ".mpeg", ".ts", ".m2ts", ".mts",
         ".wmv", ".flv", ".ogv", ".vob"}
AUDIO = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav", ".wma", ".aiff", ".aif", ".ape", ".wv",
         ".mka", ".amr", ".mid", ".midi"}

LINGUA = (os.environ.get("LANGUAGE") or os.environ.get("LANG") or "it")[:2]
ITALIANO = LINGUA == "it"
MESI = {True: ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre",
               "ottobre", "novembre", "dicembre"],
        False: ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                "November", "December"]}


def T(it, en):
    return it if ITALIANO else en


def casa():
    return os.environ.get("HOME", "/data/data/com.termux/files/home")


def config_dir():
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(casa(), ".config"), "tod")


def stato_dir():
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(casa(), ".local", "state"), "tod")


def cache_dir():
    return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(casa(), ".cache"), "tod")


# --- cartelle ------------------------------------------------------------------------------------------------
def cartelle_utente(h=None):
    """XDG_*_DIR di ~/.config/user-dirs.dirs (la home dell'utente di tod le ha già: Immagini, Video, Musica...)."""
    h = h or casa()
    out = {}
    try:
        with open(os.path.join(h, ".config", "user-dirs.dirs"), encoding="utf-8") as f:
            for r in f:
                m = re.match(r'\s*XDG_([A-Z]+)_DIR="(.*)"', r)
                if m:
                    out[m.group(1)] = m.group(2).replace("$HOME", h)
    except OSError:
        pass
    ripieghi = {"PICTURES": ("Immagini", "Pictures"), "VIDEOS": ("Video", "Videos"), "MUSIC": ("Musica", "Music"),
                "DOWNLOAD": ("Download", "Downloads")}
    for k, nomi in ripieghi.items():
        if k not in out:
            for n in nomi:
                if os.path.isdir(os.path.join(h, n)):
                    out[k] = os.path.join(h, n)
                    break
    return out


TELEFONO = os.environ.get("TOD_TELEFONO_DIR", "/storage/emulated/0")


def _da_ambiente(var):
    v = os.environ.get(var)
    if v is None:
        return None
    return [(os.path.basename(p.rstrip("/")) or p, p, 6) for p in v.split(":") if p]


def _esistenti(voci):
    visti, out = set(), []
    for nome, p, prof in voci:
        try:
            vero = os.path.realpath(p)
        except OSError:
            continue
        if os.path.isdir(p) and vero not in visti:
            visti.add(vero)
            out.append((nome, p, prof))
    return out


def cartelle_foto():
    """(nome, cartella, profondità): dove l'app Foto cerca. Prima la fotocamera del telefono, poi Immagini."""
    v = _da_ambiente("TOD_FOTO_CARTELLE")
    if v is not None:
        return _esistenti(v)
    u = cartelle_utente()
    voci = [(T("Fotocamera", "Camera"), os.path.join(TELEFONO, "DCIM", "Camera"), 2),
            (T("Screenshot", "Screenshots"), os.path.join(TELEFONO, "DCIM", "Screenshots"), 1),
            (T("Screenshot", "Screenshots"), os.path.join(TELEFONO, "Pictures", "Screenshots"), 1)]
    if "PICTURES" in u:
        voci.append((T("Immagini", "Pictures"), u["PICTURES"], 4))
    if "DOWNLOAD" in u:
        voci.append(("Download", u["DOWNLOAD"], 1))
    return _esistenti(voci)


def cartelle_video():
    v = _da_ambiente("TOD_VIDEO_CARTELLE")
    if v is not None:
        return _esistenti(v)
    u = cartelle_utente()
    voci = []
    if "VIDEOS" in u:
        voci.append((T("Video", "Videos"), u["VIDEOS"], 4))
    voci.append((T("Fotocamera", "Camera"), os.path.join(TELEFONO, "DCIM", "Camera"), 2))
    if "DOWNLOAD" in u:
        voci.append(("Download", u["DOWNLOAD"], 1))
    return _esistenti(voci)


def cartelle_musica():
    v = _da_ambiente("TOD_MUSICA_CARTELLE")
    if v is not None:
        return _esistenti(v)
    u = cartelle_utente()
    voci = []
    if "MUSIC" in u:
        voci.append((T("Musica", "Music"), u["MUSIC"], 6))
    if "DOWNLOAD" in u:
        voci.append(("Download", u["DOWNLOAD"], 1))
    return _esistenti(voci)


def tipo_di(percorso):
    e = os.path.splitext(percorso)[1].lower()
    if e in IMMAGINI:
        return "immagine"
    if e in VIDEO:
        return "video"
    if e in AUDIO:
        return "audio"
    return None


def scansiona(cartella, estensioni, profondita=4):
    """[(percorso, mtime, dimensione)] dei file con quelle estensioni; niente cartelle nascoste né miniature."""
    out = []

    def giro(d, livello):
        try:
            voci = list(os.scandir(d))
        except OSError:
            return
        for e in voci:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    if livello < profondita:
                        giro(e.path, livello + 1)
                elif os.path.splitext(e.name)[1].lower() in estensioni:
                    st = e.stat()
                    if st.st_size > 0:
                        out.append((e.path, st.st_mtime, st.st_size))
            except OSError:
                continue
    giro(cartella, 1)
    return out


def fratelli(percorso, estensioni):
    """i file dello stesso tipo nella stessa cartella, in ordine di nome (come Esplora file), e il posto di questo."""
    d = os.path.dirname(os.path.abspath(percorso))
    try:
        nomi = sorted((n for n in os.listdir(d) if not n.startswith(".")
                       and os.path.splitext(n)[1].lower() in estensioni), key=chiave_naturale)
    except OSError:
        nomi = []
    lista = [os.path.join(d, n) for n in nomi]
    p = os.path.join(d, os.path.basename(percorso))
    if p not in lista:
        lista.append(p)
    return lista, lista.index(p)


def chiave_naturale(s):
    """"foto 2" prima di "foto 10", senza badare alle maiuscole."""
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s)]


# --- date ----------------------------------------------------------------------------------------------------
_DATA_NOME = re.compile(r"(?<!\d)(20\d{2}|19\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?([0-2]\d|3[01])(?:[-_. ]?([01]\d|2[0-3])[-_.]?([0-5]\d)[-_.]?([0-5]\d))?(?!\d{3})")


def data_da_nome(nome, mtime):
    """la data della foto: dal nome del file (IMG_20260601_183012.jpg, Screenshot_20261027-1012...: la fotocamera
    la scrive lì e la copia del file la conserva), se no la data del file."""
    m = _DATA_NOME.search(os.path.basename(nome))
    if m:
        try:
            a, me, g = int(m.group(1)), int(m.group(2)), int(m.group(3))
            o, mi, s = (int(m.group(i)) if m.group(i) else 0 for i in (4, 5, 6))
            t = time.mktime((a, me, g, o, mi, s, 0, 0, -1))
            if 0 < t <= time.time() + 86400:
                return t
        except (ValueError, OverflowError):
            pass
    return mtime


def titolo_mese(t):
    lt = time.localtime(t)
    return "%s %d" % (MESI[ITALIANO][lt.tm_mon - 1], lt.tm_year)


def per_mese(elementi, quando=lambda e: e["data"]):
    """[(“novembre 2026”, [elementi])], dal più recente."""
    ordinati = sorted(elementi, key=quando, reverse=True)
    gruppi, ultimo = [], None
    for e in ordinati:
        lt = time.localtime(quando(e))
        k = (lt.tm_year, lt.tm_mon)
        if k != ultimo:
            gruppi.append((titolo_mese(quando(e)), []))
            ultimo = k
        gruppi[-1][1].append(e)
    return gruppi


def data_lunga(t):
    lt = time.localtime(t)
    if ITALIANO:
        return "%d %s %d, %02d:%02d" % (lt.tm_mday, MESI[True][lt.tm_mon - 1], lt.tm_year, lt.tm_hour, lt.tm_min)
    return "%s %d, %d, %02d:%02d" % (MESI[False][lt.tm_mon - 1], lt.tm_mday, lt.tm_year, lt.tm_hour, lt.tm_min)


# --- formati -------------------------------------------------------------------------------------------------
def tempo(s):
    """secondi → 3:07 o 1:02:03"""
    if s is None or s != s or s < 0:
        s = 0
    s = int(s)
    o, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return "%d:%02d:%02d" % (o, m, s) if o else "%d:%02d" % (m, s)


def misura(b):
    """byte → "2,4 MB" (virgola in italiano)"""
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1000 or u == "TB":
            t = ("%.0f %s" if u == "B" or b >= 100 else "%.1f %s") % (b, u)
            return t.replace(".", ",") if ITALIANO else t
        b /= 1000.0
    return ""


def numero(n):
    """1234 → "1.234" in italiano"""
    t = "{:,}".format(int(n))
    return t.replace(",", ".") if ITALIANO else t


def elementi(n):
    return T("%s elemento", "%s item") % numero(n) if n == 1 else T("%s elementi", "%s items") % numero(n)


def brani(n):
    return T("%s brano", "%s song") % numero(n) if n == 1 else T("%s brani", "%s songs") % numero(n)


# --- impostazioni piccole (json) -----------------------------------------------------------------------------
def leggi_json(percorso, predefinito):
    try:
        with open(percorso, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, type(predefinito)) else predefinito
    except (OSError, ValueError):
        return predefinito


def scrivi_json(percorso, valore):
    try:
        os.makedirs(os.path.dirname(percorso), exist_ok=True)
        tmp = percorso + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(valore, f, ensure_ascii=False)
        os.replace(tmp, percorso)
        return True
    except OSError:
        return False


class Preferiti:
    """le foto col cuore (Foto): un elenco di percorsi in ~/.config/tod/foto-preferiti.json"""

    def __init__(self, percorso=None):
        self.percorso = percorso or os.path.join(config_dir(), "foto-preferiti.json")
        self.insieme = set(leggi_json(self.percorso, []))

    def __contains__(self, p):
        return p in self.insieme

    def cambia(self, p):
        if p in self.insieme:
            self.insieme.discard(p)
        else:
            self.insieme.add(p)
        scrivi_json(self.percorso, sorted(self.insieme))
        return p in self.insieme

    def sposta(self, vecchio, nuovo):
        if vecchio in self.insieme:
            self.insieme.discard(vecchio)
            if nuovo:
                self.insieme.add(nuovo)
            scrivi_json(self.percorso, sorted(self.insieme))


class Posizioni:
    """dove si era fermato ogni video (Lettore multimediale: "riprendi da"), al più 200 video."""
    MAX = 200

    def __init__(self, percorso=None):
        self.percorso = percorso or os.path.join(stato_dir(), "video-posizioni.json")
        self.d = leggi_json(self.percorso, {})

    def leggi(self, p, durata=None):
        v = self.d.get(p)
        if not isinstance(v, (int, float)) or v < 10:
            return 0
        if durata and v > durata - 15:
            return 0
        return v

    def scrivi(self, p, pos, durata):
        """dall'inizio (meno di 10 s) o quasi alla fine (ultimi 15 s o 95%): si dimentica."""
        if pos < 10 or (durata and (pos > durata - 15 or pos > durata * 0.95)):
            self.d.pop(p, None)
        else:
            self.d.pop(p, None)
            self.d[p] = round(pos, 1)
            while len(self.d) > self.MAX:
                self.d.pop(next(iter(self.d)))
        scrivi_json(self.percorso, self.d)


# --- coda di riproduzione ------------------------------------------------------------------------------------
class Coda:
    """la coda del lettore: elementi (percorsi), quello in riproduzione, casuale e ripeti (no, tutti, uno).

    In casuale l'ordine si mescola una volta (come Media Player): "successivo" segue l'ordine mescolato e il
    brano in corso resta il primo; spegnendo il casuale si torna all'ordine vero dal brano in corso."""
    NO, TUTTI, UNO = "no", "tutti", "uno"

    def __init__(self, elementi=None, rnd=None):
        self.elementi = list(elementi or [])
        self.indice = 0 if self.elementi else -1
        self.ripeti = self.NO
        self.casuale = False
        self.ordine = list(range(len(self.elementi)))
        self.rnd = rnd or random.Random()

    def __len__(self):
        return len(self.elementi)

    def corrente(self):
        return self.elementi[self.indice] if 0 <= self.indice < len(self.elementi) else None

    def imposta(self, elementi, inizio=0):
        self.elementi = list(elementi)
        self.indice = inizio if 0 <= inizio < len(self.elementi) else (0 if self.elementi else -1)
        self._rimescola()

    def _rimescola(self):
        n = len(self.elementi)
        if self.casuale and n:
            altri = [i for i in range(n) if i != self.indice]
            self.rnd.shuffle(altri)
            self.ordine = ([self.indice] if self.indice >= 0 else []) + altri
        else:
            self.ordine = list(range(n))

    def metti_casuale(self, si):
        self.casuale = bool(si)
        self._rimescola()

    def _pos(self):
        try:
            return self.ordine.index(self.indice)
        except ValueError:
            return -1

    def prossimo_indice(self, automatico=False):
        """quello che viene dopo; automatico = finito da solo (lì "ripeti uno" ripete). None = fine."""
        if not self.elementi:
            return None
        if automatico and self.ripeti == self.UNO:
            return self.indice
        p = self._pos() + 1
        if p >= len(self.ordine):
            if self.ripeti == self.NO and automatico:
                return None
            if self.ripeti == self.NO:
                return None
            p = 0
        return self.ordine[p]

    def avanti(self, automatico=False):
        i = self.prossimo_indice(automatico)
        if i is None:
            return None
        self.indice = i
        return self.corrente()

    def indietro(self):
        if not self.elementi:
            return None
        p = self._pos() - 1
        if p < 0:
            p = len(self.ordine) - 1 if self.ripeti == self.TUTTI else 0
        self.indice = self.ordine[p]
        return self.corrente()

    def vai(self, i):
        if 0 <= i < len(self.elementi):
            self.indice = i
            if self.casuale:
                self._rimescola()
        return self.corrente()

    def aggiungi(self, elementi, subito_dopo=False):
        n0 = len(self.elementi)
        nuovi = list(elementi)
        if subito_dopo and self.indice >= 0:
            dove = self.indice + 1
            self.elementi[dove:dove] = nuovi
            self.ordine = [i + len(nuovi) if i >= dove else i for i in self.ordine]
            p = self._pos() + 1
            self.ordine[p:p] = list(range(dove, dove + len(nuovi)))
        else:
            self.elementi.extend(nuovi)
            self.ordine.extend(range(n0, n0 + len(nuovi)))
        if self.indice < 0 and self.elementi:
            self.indice = 0

    def togli(self, i):
        if not 0 <= i < len(self.elementi):
            return
        del self.elementi[i]
        self.ordine = [j - 1 if j > i else j for j in self.ordine if j != i]
        if i < self.indice:
            self.indice -= 1
        elif i == self.indice and self.indice >= len(self.elementi):
            self.indice = len(self.elementi) - 1

    def svuota(self):
        self.elementi, self.ordine, self.indice = [], [], -1

    def successivi(self, n=10):
        """i prossimi n, nell'ordine in cui suoneranno (per "Successivi")."""
        p = self._pos()
        out = [self.ordine[j] for j in range(p + 1, min(len(self.ordine), p + 1 + n))]
        if self.ripeti == self.TUTTI and len(out) < n:
            out += [j for j in self.ordine[:max(0, p)]][:n - len(out)]
        return out

    def giro_ripeti(self):
        self.ripeti = {self.NO: self.TUTTI, self.TUTTI: self.UNO, self.UNO: self.NO}[self.ripeti]
        return self.ripeti


# --- musica: i tag -------------------------------------------------------------------------------------------
def brano_vuoto(percorso, mtime=0, dim=0):
    nome = os.path.splitext(os.path.basename(percorso))[0]
    m = re.match(r"^\s*(\d{1,3})[\s.\-_]+(.+)$", nome)
    return {"mtime": mtime, "dim": dim, "titolo": (m.group(2) if m else nome).strip(),
            "artista": "", "album": "", "artista_album": "", "traccia": int(m.group(1)) if m else 0, "disco": 0,
            "anno": 0, "genere": "", "durata": 0.0, "copertina": ""}


def artista_di(b):
    return b.get("artista_album") or b.get("artista") or T("Artista sconosciuto", "Unknown artist")


def album_di(b):
    return b.get("album") or T("Album sconosciuto", "Unknown album")


def chiave_album(b):
    """un album = titolo + artista dell'album (due "Greatest Hits" di artisti diversi restano due album)."""
    return (album_di(b).lower(), (b.get("artista_album") or b.get("artista") or "").lower())


def chiave_ordine(s):
    """artisti e album in ordine come Media Player: senza "The"/"Il"/"La" davanti, senza maiuscole."""
    s = (s or "").strip().lower()
    return re.sub(r"^(the|il|lo|la|i|gli|le|l')\s*", "", s) or s


def raggruppa_album(brani_d):
    """{chiave: {"titolo", "artista", "anno", "brani": [percorsi in ordine di disco e traccia], "copertina"}}"""
    album = {}
    for p, b in brani_d.items():
        k = chiave_album(b)
        a = album.setdefault(k, {"titolo": album_di(b), "artista": artista_di(b), "anno": 0, "brani": [],
                                 "copertina": "", "chiave": k})
        a["brani"].append(p)
        a["anno"] = max(a["anno"], b.get("anno") or 0)
        if not a["copertina"] and b.get("copertina"):
            a["copertina"] = b["copertina"]
    for a in album.values():
        a["brani"].sort(key=lambda p: (brani_d[p].get("disco") or 0, brani_d[p].get("traccia") or 0,
                                       chiave_naturale(os.path.basename(p))))
    return album


def raggruppa_artisti(brani_d):
    art = {}
    for p, b in brani_d.items():
        n = artista_di(b)
        a = art.setdefault(n.lower(), {"nome": n, "brani": [], "album": set(), "copertina": ""})
        a["brani"].append(p)
        a["album"].add(chiave_album(b))
        if not a["copertina"] and b.get("copertina"):
            a["copertina"] = b["copertina"]
    return art


def cerca(testo, *campi):
    t = (testo or "").strip().lower()
    return not t or all(any(parola in (c or "").lower() for c in campi) for parola in t.split())
