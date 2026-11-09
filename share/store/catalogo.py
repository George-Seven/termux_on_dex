#!/usr/bin/env python3
"""Il catalogo di Flathub (AppStream) per lo Store: legge appstream.xml.gz una volta e tiene un indice JSON.

  catalogo.py <cartella dello Store> [testo]    prova: quante app, e le prime che corrispondono al testo

Solo le app con finestra (desktop-application) per aarch64, con l'id VERO da installare (quello del
<bundle>: il componente di KWrite si chiama org.kde.kwrite.desktop) e il runtime. Testi nella lingua
del desktop se Flathub li ha, se no in inglese. Niente GTK: lo usa share/store/tod-store-gui.py.
"""
import gzip
import html
import json
import os
import sys
import time
import xml.etree.ElementTree as ET

LANG = "{http://www.w3.org/XML/1998/namespace}lang"
VERSIONE = 3   # cambia → l'indice si rifà


def cartella(store):
    return os.path.join(store, "flatpak", "appstream", "flathub", "aarch64", "active")


def _lingua(el, lingua):
    """tra gli elementi uguali (name, summary...) quello nella lingua giusta, se no quello senza lingua."""
    scelto, base = None, None
    for e in el:
        l = e.get(LANG)
        if l is None:
            base = base if base is not None else e
        elif l == lingua or l.split("_")[0] == lingua:
            scelto = e
    return scelto if scelto is not None else base


def _testo(e):
    return " ".join("".join(e.itertext()).split()) if e is not None else ""


def descrizione(d, lingua):
    """<description> → testo semplice con paragrafi ed elenchi (lo Store lo mostra com'è, niente markup)."""
    if d is None:
        return ""
    parti = []
    for e in d:
        l = e.get(LANG)
        if l is not None and l != lingua and l.split("_")[0] != lingua:
            continue
        if e.tag == "p":
            parti.append(_testo(e))
        elif e.tag in ("ul", "ol"):
            parti.append("\n".join("• " + _testo(li) for li in e if li.get(LANG) in (None, lingua)))
    return "\n\n".join(p for p in parti if p)


def _componente(c, lingua, icone):
    b = c.find("bundle")
    if b is None or not (b.text or "").startswith("app/"):
        return None
    appid = b.text.split("/")[1]
    nome = _testo(_lingua(c.findall("name"), lingua))
    if not nome:
        return None
    descs = c.findall("description")
    desc = None
    for d in descs:   # una <description> per lingua, oppure una sola con i <p xml:lang> dentro
        if d.get(LANG) in (lingua,) or (d.get(LANG) or "").split("_")[0] == lingua:
            desc = d
    if desc is None:
        desc = next((d for d in descs if d.get(LANG) is None), None)
    sviluppatore = _testo(_lingua(c.findall("developer/name"), lingua)) or \
        _testo(_lingua(c.findall("developer_name"), lingua))
    icona = ""
    for e in c.findall("icon"):
        if e.get("type") == "cached" and e.text:
            for d in ("128x128", "64x64"):
                p = os.path.join(icone, d, e.text)
                if os.path.exists(p):
                    icona = p
                    break
        if icona:
            break
    schermate = []
    for s in c.findall("screenshots/screenshot"):
        mini, grande = "", ""
        for im in s.findall("image"):
            w = int(im.get("width") or 0)
            if im.get("type") == "thumbnail" and 500 <= w <= 1300 and not mini:
                mini = (im.text or "").strip()
            elif im.get("type") == "source":
                grande = (im.text or "").strip()
        if mini or grande:
            schermate.append(mini or grande)
    versione, data, novita = "", 0, ""
    r = c.find("releases/release")
    if r is not None:
        versione = r.get("version") or ""
        rd = r.findall("description")   # le note dell'ultima versione ("Novità"), nella lingua giusta se c'è
        scelta = next((d for d in rd if (d.get(LANG) or "").split("_")[0] == lingua), None)
        if scelta is None:
            scelta = next((d for d in rd if d.get(LANG) is None), None)
        novita = descrizione(scelta, lingua)
        try:
            data = int(r.get("timestamp") or 0)
        except ValueError:
            data = 0
    verificata = any(v.get("key") == "flathub::verification::verified" and v.text == "true"
                     for v in c.findall("custom/value"))
    sito = next((u.text for u in c.findall("url") if u.get("type") == "homepage"), "") or ""
    return {
        "id": appid, "nome": nome, "sommario": _testo(_lingua(c.findall("summary"), lingua)),
        "descrizione": descrizione(desc, lingua), "categorie": [_testo(x) for x in c.findall("categories/category")],
        "parole": [_testo(x) for x in c.findall("keywords/keyword") if x.get(LANG) in (None, lingua)],
        "sviluppatore": sviluppatore, "licenza": _testo(c.find("project_license")), "sito": sito.strip(),
        "icona": icona, "schermate": schermate[:6], "versione": versione, "data": data, "novita": novita, "verificata": verificata,
        "runtime": b.get("runtime") or "",
        "tradotta": any(x.text == lingua and int(x.get("percentage") or 0) >= 50 for x in c.findall("languages/lang")),
    }


def leggi(store, lingua="it"):
    """Tutte le app (lista di dizionari), dall'indice se il catalogo non è cambiato."""
    cart = cartella(store)
    xml = os.path.join(cart, "appstream.xml.gz")
    st = os.stat(xml)
    indice = os.path.join(store, "cache", f"catalogo-{lingua}.json")
    firma = [VERSIONE, st.st_size, int(st.st_mtime)]
    try:
        with open(indice) as f:
            dati = json.load(f)
        if dati.get("firma") == firma:
            return dati["app"]
    except (OSError, ValueError):
        pass
    app, visti = [], set()
    icone = os.path.join(cart, "icons")
    with gzip.open(xml) as f:
        for ev, el in ET.iterparse(f, events=("end",)):
            if el.tag != "component":
                continue
            if el.get("type") in ("desktop-application", "desktop"):
                a = _componente(el, lingua, icone)
                if a and a["id"] not in visti:
                    visti.add(a["id"])
                    app.append(a)
            el.clear()
    os.makedirs(os.path.dirname(indice), exist_ok=True)
    with open(indice + ".tmp", "w") as f:
        json.dump({"firma": firma, "app": app}, f, ensure_ascii=False)
    os.replace(indice + ".tmp", indice)
    return app


def punteggio(a, adesso=None):
    """per ordinare senza statistiche di Flathub: verificate, tradotte, con schermate, aggiornate di recente."""
    adesso = adesso or time.time()
    return (2 if a["verificata"] else 0) + (1 if a["tradotta"] else 0) + (1 if a["schermate"] else 0) + \
        (1 if a["data"] and adesso - a["data"] < 365 * 86400 else 0)


def _piega(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def cerca(app, testo):
    """per nome (prima chi comincia così), poi id, parole chiave e descrizione breve."""
    t = _piega(testo.strip())
    if not t:
        return []
    ris = []
    for a in app:
        n = _piega(a["nome"])
        if n.startswith(t):
            p = 0
        elif t in n:
            p = 1
        elif t in a["id"].lower() or any(t in _piega(k) for k in a["parole"]):
            p = 2
        elif t in _piega(a["sommario"]):
            p = 3
        else:
            continue
        ris.append((p, -punteggio(a), n, a))
    ris.sort(key=lambda x: x[:3])
    return [x[3] for x in ris]


def pango(s):
    return html.escape(s, quote=False)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    t0 = time.time()
    tutte = leggi(sys.argv[1])
    print(f"{len(tutte)} app in {time.time() - t0:.2f} s")
    for a in cerca(tutte, sys.argv[2])[:8] if len(sys.argv) > 2 else []:
        print(a["id"], "|", a["nome"], "|", a["sommario"], "|", a["runtime"], "|", bool(a["icona"]), len(a["schermate"]))
