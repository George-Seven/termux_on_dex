"""tastiera_dati.py — lingue e layout della tastiera per Impostazioni → Tastiera (13 novembre 2026). Senza grafica.

Pietro: "e se voglio usare una lingua diversa o un layout diverso della tastiera?". In DeX, Termux:X11 manda a X le
lettere già fatte, secondo il layout scelto nel telefono (InputEventSender.java: sendTextEvent quando non ci sono
Ctrl/Alt/Win): il layout di X conta solo per le combinazioni. Per scegliere le lingue qui, come su macOS, Termux:X11
deve mandare i tasti veri (preferScancodes) e X usare i layout scelti (lib/x11.sh: x11_tastiera_applica). L'elenco dei
layout è quello di X (xkeyboard-config, rules/evdev.xml), con i nomi in italiano fatti qui (Termux non ha le traduzioni).
"""
import os
import re
import xml.etree.ElementTree as ET

PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
REGOLE = os.path.join(PREFIX, "share/X11/xkb/rules/evdev.xml")
MASSIMO = 4   # X tiene al massimo 4 layout insieme (gruppi XKB)

# come si passa da un layout all'altro (opzioni grp: di XKB). Alt + Maiusc prima: arriva sempre a X, mentre
# Ctrl + Spazio e Win + Spazio su certi telefoni li prende Android per cambiare il suo layout
CAMBI = [("grp:alt_shift_toggle", "Alt + Maiusc", "Alt + Shift"),
         ("grp:win_space_toggle", "Win + Spazio", "Win + Space"),
         ("grp:ctrl_space_toggle", "Ctrl + Spazio", "Ctrl + Space"),
         ("grp:shift_caps_toggle", "Maiusc + Bloc Maiusc", "Shift + Caps Lock"),
         ("grp:caps_toggle", "Bloc Maiusc", "Caps Lock")]
CAMBIO_PREDEFINITO = CAMBI[0][0]

LINGUE_IT = {
    "Albanian": "Albanese", "Amharic": "Amarico", "Armenian": "Armeno", "Arabic": "Arabo", "Azerbaijani": "Azero",
    "Bangla": "Bengalese", "Bengali": "Bengalese", "Belarusian": "Bielorusso", "Belgian": "Belga", "Berber": "Berbero",
    "Bosnian": "Bosniaco", "Bulgarian": "Bulgaro", "Burmese": "Birmano", "Chinese": "Cinese", "Croatian": "Croato",
    "Czech": "Ceco", "Danish": "Danese", "Dutch": "Olandese", "English": "Inglese", "Estonian": "Estone",
    "Faroese": "Faroese", "Filipino": "Filippino", "Finnish": "Finlandese", "French": "Francese",
    "Georgian": "Georgiano", "German": "Tedesco", "Greek": "Greco", "Hebrew": "Ebraico", "Hungarian": "Ungherese",
    "Icelandic": "Islandese", "Indian": "Indiano", "Indonesian": "Indonesiano", "Irish": "Irlandese",
    "Italian": "Italiano", "Japanese": "Giapponese", "Kazakh": "Kazako", "Korean": "Coreano", "Kyrgyz": "Kirghiso",
    "Latvian": "Lettone", "Lithuanian": "Lituano", "Macedonian": "Macedone", "Malay": "Malese", "Maltese": "Maltese",
    "Moldavian": "Moldavo", "Mongolian": "Mongolo", "Montenegrin": "Montenegrino", "Nepali": "Nepalese",
    "Norwegian": "Norvegese", "Persian": "Persiano", "Polish": "Polacco", "Portuguese": "Portoghese",
    "Romanian": "Rumeno", "Russian": "Russo", "Serbian": "Serbo", "Sinhala": "Singalese", "Slovak": "Slovacco",
    "Slovenian": "Sloveno", "Spanish": "Spagnolo", "Swedish": "Svedese", "Tajik": "Tagiko", "Thai": "Thailandese",
    "Turkmen": "Turkmeno", "Turkish": "Turco", "Ukrainian": "Ucraino", "Uzbek": "Uzbeko", "Vietnamese": "Vietnamita",
    "Kurdish": "Curdo", "Catalan": "Catalano", "Occitan": "Occitano", "Breton": "Bretone", "Welsh": "Gallese",
    "Tatar": "Tataro", "Ossetian": "Osseto", "Chuvash": "Ciuvascio", "Latin": "Latino", "Uyghur": "Uiguro",
    "Tibetan": "Tibetano", "Hawaiian": "Hawaiano", "Sanskrit": "Sanscrito", "Yakut": "Jacuto", "Swiss": "Svizzero",
    "Kabylian": "Cabilo", "Taiwanese": "Taiwanese", "Esperanto": "Esperanto", "Swahili": "Swahili",
    "Urdu": "Urdu", "Hindi": "Hindi", "Tamil": "Tamil", "Pashto": "Pashto", "Romani": "Romaní",
}
# le parole tra parentesi (paesi e varianti), dalla più lunga alla più corta
PAROLE_IT = [
    ("Democratic Republic of the Congo", "Rep. Dem. del Congo"), ("intl., with dead keys", "internazionale, con tasti morti"),
    ("with dead keys", "con tasti morti"), ("no dead keys", "senza tasti morti"), ("South Africa", "Sudafrica"),
    ("New Zealand", "Nuova Zelanda"), ("Latin American", "America Latina"), ("Switzerland", "Svizzera"),
    ("Belgium", "Belgio"), ("Brazil", "Brasile"), ("Egypt", "Egitto"), ("Morocco", "Marocco"), ("Syria", "Siria"),
    ("Cameroon", "Camerun"), ("Spain", "Spagna"), ("France", "Francia"), ("Germany", "Germania"),
    ("Italy", "Italia"), ("Ukraine", "Ucraina"), ("Turkey", "Turchia"), ("China", "Cina"), ("Japan", "Giappone"),
    ("Cambodia", "Cambogia"), ("Arabic Keyboard", "tastiera araba"), ("typewriter", "macchina da scrivere"),
    ("programmer", "programmatori"), ("ergonomic", "ergonomica"), ("phonetic", "fonetica"), ("legacy", "vecchia"),
    ("extended", "estesa"), ("Cyrillic", "cirillico"), ("Latin", "latino"), ("alt.", "alternativa"),
    ("intl.", "internazionale"), ("UK", "Regno Unito"), ("US", "USA"),
]


class Voce:
    """un layout di X (variante "" = quello di base)"""
    __slots__ = ("codice", "variante", "inglese", "breve", "lingue")

    def __init__(self, codice, variante, inglese, breve="", lingue=()):
        self.codice, self.variante, self.inglese, self.breve, self.lingue = codice, variante, inglese, breve, tuple(lingue)

    def chiave(self):
        return (self.codice, self.variante)

    def __repr__(self):
        return f"Voce({self.codice!r}, {self.variante!r})"


def carica(percorso=REGOLE):
    """tutti i layout e le varianti di evdev.xml, nell'ordine del file (prima il layout, poi le sue varianti)"""
    try:
        radice = ET.parse(percorso).getroot()
    except (OSError, ET.ParseError):
        return []
    out = []
    for lay in radice.iter("layout"):
        c = lay.find("configItem")
        if c is None or not c.findtext("name") or c.findtext("name") == "custom":
            continue
        codice = c.findtext("name")
        lingue = [x.text for x in c.iter("iso639Id") if x.text]
        out.append(Voce(codice, "", c.findtext("description") or codice, c.findtext("shortDescription") or codice, lingue))
        for v in lay.iter("variant"):
            cv = v.find("configItem")
            if cv is None or not cv.findtext("name"):
                continue
            lv = [x.text for x in cv.iter("iso639Id") if x.text] or lingue
            out.append(Voce(codice, cv.findtext("name"), cv.findtext("description") or cv.findtext("name"),
                            cv.findtext("shortDescription") or c.findtext("shortDescription") or codice, lv))
    return out


def italiano(testo):
    """"English (US, intl., with dead keys)" → "Inglese (USA, internazionale, con tasti morti)" """
    m = re.match(r"^([^(]+?)\s*(\((.*)\))?$", testo)
    if not m:
        return testo
    lingua = m.group(1)
    for en, it in LINGUE_IT.items():
        if lingua == en or lingua.startswith(en + " "):
            lingua = it + lingua[len(en):]
            break
    dentro = m.group(3)
    if dentro is None:
        return lingua
    for en, it in PAROLE_IT:
        dentro = re.sub(r"(?<![\w.])" + re.escape(en) + r"(?![\w])", it, dentro)
    return f"{lingua} ({dentro})"


def nome(voce, it=True):
    return italiano(voce.inglese) if it else voce.inglese


def trova(codice, variante, voci):
    for v in voci:
        if v.codice == codice and v.variante == variante:
            return v
    return None


def nome_di(codice, variante, voci, it=True):
    v = trova(codice, variante, voci)
    if v is not None:
        return nome(v, it)
    return codice + (f" ({variante})" if variante else "")


def sigla(codice):
    """la sigla per la barra: "IT", "US", "GB"… (il codice di X: la sigla della lingua direbbe EN per USA e Regno Unito)"""
    return codice[:3].upper()


def cerca(testo, voci, it=True):
    """le voci che contengono tutte le parole cercate (nome italiano o inglese, codice)"""
    parole = testo.lower().split()
    if not parole:
        return list(voci)
    out = []
    for v in voci:
        dove = " ".join((nome(v, it), v.inglese, v.codice, v.variante, " ".join(v.lingue))).lower()
        if all(p in dove for p in parole):
            out.append(v)
    # prima i layout di base, poi le varianti: "inglese usa" deve dare prima Inglese (USA)
    return sorted(out, key=lambda v: (v.variante != "", nome(v, it).lower()))


def scomponi(layout, varianti):
    """"it,us" e ",intl" → [("it", ""), ("us", "intl")]"""
    lay = [x.strip() for x in (layout or "").split(",")]
    var = [x.strip() for x in (varianti or "").split(",")]
    out = []
    for i, c in enumerate(lay):
        if c:
            out.append((c, var[i] if i < len(var) else ""))
    return out[:MASSIMO]


def componi(sorgenti):
    """[("it", ""), ("us", "intl")] → ("it,us", ",intl"), senza doppioni e al massimo 4"""
    visti, lista = set(), []
    for c, v in sorgenti:
        if c and (c, v) not in visti:
            visti.add((c, v))
            lista.append((c, v))
    lista = lista[:MASSIMO]
    return ",".join(c for c, _v in lista), ",".join(v for _c, v in lista)


def predefinito(lingua):
    """il primo layout per la lingua del desktop (LINGUA di tod: "it", "en"…)"""
    return {"en": "us", "da": "dk", "sv": "se", "cs": "cz", "el": "gr", "uk": "ua", "ja": "jp", "ko": "kr",
            "sl": "si", "et": "ee", "he": "il", "fa": "ir", "zh": "cn", "vi": "vn"}.get(lingua, lingua or "us")
