"""cattura_dati.py — l'anteprima della Cattura senza grafica (15 novembre 2026): la usano tod-cattura.py e i test.

Pietro: "quando faccio uno screen, nei pochi secondi in cui l'anteprima compare di lato, la possibilità di trascinarla
dove voglio ed eventualmente allegarla". Come macOS: l'anteprima si trascina in Esplora file, sulla scrivania, in una
chat o nel campo "allega" di Chromium e delle app di Debian (Electron, GTK). Si passa il file come lo passa Esplora
file (text/uri-list) e, per chi vuole l'immagine e non il file, i byte del PNG (image/png).

  uri_del_file(percorso)      file:// con il percorso VERO (collegamenti risolti): le app del container, con e senza
                              proot, vedono /storage, /sdcard, /data e /media come Termux; la casa del desktop no
  leggibile_dal_container(p)  vero se il percorso vero sta in uno di quei posti
  BERSAGLI, dati_per(f, p)    i formati offerti (in ordine di preferenza) e i byte da dare a chi riceve
  misura_icona, punto_presa   l'immagine che segue il puntatore e il punto in cui la si tiene
  Tempi                       quando l'anteprima sparisce: dopo 7 s, 3 s dopo che il puntatore è uscito, mai durante
                              un trascinamento; un trascinamento riuscito la chiude, uno annullato la lascia lì
"""
import os
import urllib.parse

# i percorsi che le app del container vedono uguali (come COMUNI di tod_apri_salva.py)
COMUNI = ("/data/", "/storage/", "/sdcard/", "/media/")
BERSAGLI = ("text/uri-list", "image/png")


def uri_del_file(percorso):
    """come GLib.filename_to_uri, ma sul percorso vero: Immagini della casa del desktop → /storage/emulated/0/Pictures"""
    return "file://" + urllib.parse.quote(os.path.realpath(percorso), safe="/!$&'()*+,=:@~")


def leggibile_dal_container(percorso):
    return os.path.realpath(percorso).startswith(COMUNI)


def dati_per(formato, percorso):
    """i byte per il formato chiesto da chi riceve; None se non è uno dei nostri"""
    if formato == "text/uri-list":
        return (uri_del_file(percorso) + "\r\n").encode()
    if formato == "image/png":
        with open(percorso, "rb") as f:
            return f.read()
    return None


def misura_icona(w, h, lato_max):
    """l'immagine che segue il puntatore: la miniatura rimpicciolita, il lato lungo al più lato_max"""
    sc = min(1.0, lato_max / max(w, h, 1))
    return max(1, round(w * sc)), max(1, round(h * sc))


def punto_presa(x, y, w_area, w_mini, h_mini, w_icona, h_icona):
    """dove si è preso la miniatura (x, y nell'area, che la centra in orizzontale) → lo stesso punto sull'icona"""
    x0 = (w_area - w_mini) / 2
    fx = min(max((x - x0) / max(w_mini, 1), 0.0), 1.0)
    fy = min(max(y / max(h_mini, 1), 0.0), 1.0)
    return round(fx * (w_icona - 1)), round(fy * (h_icona - 1))


def dentro(punto, rett):
    (px, py), (x, y, w, h) = punto, rett
    return x <= px < x + w and y <= py < y + h


class Tempi:
    PRIMA = 7.0   # secondi dalla comparsa
    DOPO = 3.0    # dall'ultima volta che il puntatore era sopra

    def __init__(self, ora):
        self.scade = ora + self.PRIMA
        self.trascina = False
        self.finita = False

    def sopra(self, ora):
        self.scade = max(self.scade, ora + self.DOPO)

    def inizio(self):
        self.trascina = True

    def fine(self, riuscito, ora):
        """riuscito: il file è arrivato da qualche parte → l'anteprima ha finito; se no torna come prima"""
        self.trascina = False
        if riuscito:
            self.finita = True
        else:
            self.sopra(ora)
        return not riuscito

    def resta(self, ora, puntatore_sopra=False):
        if self.finita:
            return False
        if self.trascina:
            return True
        if puntatore_sopra:
            self.sopra(ora)
        return ora < self.scade
