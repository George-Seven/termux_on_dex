# Uno Store di app Flathub per Termux On DeX — studio di fattibilità

9 novembre 2026, Galaxy S26 Ultra (Android 17, Adreno 840), container Debian 13. Tutte le prove in
`~/src/tod-store-prove` (cartella a parte): nulla installato nel container né nello Start.

## In breve

**Si può fare.** Flathub ha **3094 app per aarch64** (l'88% delle 3527 per x86_64). Il Flatpak di
Debian, estratto in una cartella (non installato), senza root, senza proot e senza la sua sandbox,
scarica e installa app e runtime da Flathub. L'avvio lo facciamo noi, come le app di Debian: senza
proot, con la GPU vera e la finestra dei file che funziona. Il prototipo è `tod store`.

## 1. Installazione da Flathub

| Cosa | Risultato |
|---|---|
| `flatpak --user` di Debian 1.16.6 (dpkg-deb -x) | funziona, con due accorgimenti (sotto) |
| GNOME 51 + Mesa + codec + GNOME Text Editor | 45 s |
| KDE 6.11 + KWrite, con `tod store installa kwrite` | 1 min 20 s |
| Preparazione dello Store da zero (`tod store prepara`) | 10,6 s |
| Catalogo AppStream di Flathub / una ricerca | 11 s (al massimo una volta al giorno) / ~2,7 s |

- **Android vieta i collegamenti fisici** (`linkat: Permission denied`): OSTree non creava nemmeno
  il deposito. `share/store/tod-nolink.c` (precaricata): `O_TMPFILE` → EOPNOTSUPP e `linkat` negato
  → EXDEV, così OSTree ripiega su file temporanei + rinomina e su copie.
- Il **controllo parentale** cerca il D-Bus di sistema (su Android non c'è) e bloccava l'app:
  `FLATPAK_SYSTEM_HELPER_ON_SESSION=1` lo salta (per le installazioni `--user` non cambia altro).
- bubblewrap per installare non serve. **`flatpak run` invece non va**, nemmeno con un finto bwrap:
  si ferma prima di chiamarlo (Android nega `open_tree`/`openat` sulle cartelle di sistema). Quindi
  l'avvio è nostro.
- La firma GPG di Flathub la verifica Flatpak come sempre.

## 2. Avvio delle app (runtime su /usr, app su /app)

Lanciatore nostro (`share/store/tod-store.py`): legge `metadata` di app e runtime (comando,
`[Environment]`, estensioni), "monta" le estensioni (Mesa, traduzioni, codec) con collegamenti
relativi e fa una radice per app (`usr` → runtime, `app` → app, `etc` del runtime con passwd, hosts,
resolv.conf nostri).

- **Con proot**: funziona al primo colpo.
- **Senza proot** (come `lib/diretto.sh`): il runtime ha il suo glibc (2.44), quindi caricatore e
  libc *del runtime* corretti con `share/diretto/correggi.py`; libfakechroot di Debian e
  `tod-diretto.so` vanno bene anche col glibc più nuovo. In più è servito:
  - 1543 collegamenti assoluti del runtime (quasi tutti licenze) resi relativi;
  - `LOCPATH`, `GCONV_PATH`, `TZDIR` col percorso vero: il glibc apre lingue e fusi orari da sé
    (fakechroot non lo vede) e l'app restava in inglese;
  - i RUNPATH assoluti (es. gvfs) in `LD_LIBRARY_PATH`;
  - `tod-nolink.so` anche nell'app: Qt salva le impostazioni con `QSaveFile` (`O_TMPFILE`), e
    KWrite dava "Disk full?".

Misure (schermo finto Xvfb, 3 avvii per modo, GNOME Text Editor):

| | finestra dopo | processore nei primi 7 s | memoria |
|---|---|---|---|
| proot | 1,27–2,30 s | 3,3–3,5 s | ~154 MB |
| **senza proot** | **0,69–1,12 s** | **2,8–2,9 s** | ~150 MB |

KWrite (Qt, runtime KDE) con il prototipo, senza proot: finestra in 1,0–1,4 s, ~120 MB. Entrambe in
italiano.

## 3. GPU

| Configurazione | Renderer | glxgears (senza vsync) |
|---|---|---|
| Mesa di Flathub così com'è | **llvmpipe** (software) | 300–470 fps |
| Zink di Flathub + turnip KGSL del container | **Adreno 840** (turnip) | **813–962 fps** |
| Per confronto: app Debian senza proot, ora | Adreno 840 (zink) | 735–822 fps |

- Il turnip di Flathub è compilato **senza KGSL** (non conosce `/dev/kgsl-3d0`), quello del
  container sì. Non serve un'estensione GL nostra: basta un file ICD di Vulkan che punta al
  `libvulkan_freedreno.so` del container. Lo zink di Flathub (Mesa 26.2.5) fa il resto.
- Vulkan diretto (giochi): `vulkaninfo` vede Adreno 840 con lo stesso ICD; senza, "no drivers".
- Il prototipo lo fa da solo (legge `/etc/tod/gpu.env` del container). Con il vsync acceso le app
  vanno a 30 fps come quelle di Debian.

## 4. File e portali

- Fuori dalla sandbox (niente `/.flatpak-info`) **GTK e Qt non chiedono il portale**: usano la loro
  finestra. GTK 4 (Text Editor) si apre in 0,25–0,58 s; Qt (KWrite) usa la finestra GTK 3 del
  runtime KDE in 0,36 s. In entrambi aperto davvero `~/Documenti/prova.txt`.
- Senza D-Bus di sessione è uguale: il `dbus-launch` del runtime fallisce e basta (niente portale
  di Debian che rifiuta tutto, la trappola delle app del container).
- Avvisi innocui nei registri: portale `org.freedesktop.portal.Desktop` assente (Qt chiede il tema
  chiaro/scuro), gvfsd assente, `/run/host/font-dirs.xml`.

## Costi e limiti

- **Spazio doppio**: senza collegamenti fisici ogni file sta due volte (deposito OSTree + copia
  installata). KDE 6.11 + KWrite = 3,4 GB (1,7 + 1,7); GNOME 51 + Text Editor 3,4 GB. Ogni runtime
  in più pesa così (GNOME e KDE ~1,2 GB installati, Mesa ~0,45 GB per versione). Il Mesa "-extra"
  installato due volte (~460 MB in più) ora si evita con `flatpak mask`.
- Nessuna sandbox: le app vedono la casa come le app di Debian (è lo scopo).
- App Electron di Flathub (zypak): provate con Boxy SVG, partono senza proot (vedi sotto). Il processo
  grafico di Chromium cade tre volte e Chromium passa da solo alla grafica software: da guardare.
- Non provati: giochi con estensioni (Wine, Steam), app che parlano solo con i portali (notifiche, apri
  link, screenshot).
- I messaggi di Flatpak (avanzamento) sono in inglese: i suoi cataloghi non sono nel container.

## Prototipo `tod store` (riga di comando)

```
tod store cerca <testo>        # solo app aarch64, dal catalogo di Flathub
tod store info <app>
tod store installa <app>       # "gimp" va bene quanto "org.gimp.GIMP"; --flathub: salta Termux e Debian
tod store avvia <app> [file]   # senza proot; proot da solo se senza non parte
tod store elenco | rimuovi <app> | aggiorna
```

- **Una app, un solo sistema**: prima di installare usa `app_owner` (`share/tod-app-keys.awk`);
  provato: `tod store installa gimp` → "org.gimp.GIMP: l'app è già installata (gimp)".
- Voce nello Start: `~/.local/share/applications/tod-store-<id>.desktop`, `Exec=tod store avvia <id>`,
  icona dell'app (percorso stabile anche dopo gli aggiornamenti), cursore di attesa.
- Dati: `~/.local/share/tod-store` (Flatpak estratto, deposito, radici, glibc corretti); i dati delle
  app in `~/.var/app/<id>` nella casa delle app di Debian (come Flatpak).
- `lib/store.sh` si carica solo con `tod store`; i comandi di sempre non cambiano.
- File: `lib/store.sh`, `share/store/tod-store.py`, `share/store/tod-store-gui.py`, `share/store/catalogo.py`,
  `share/store/tod-nolink.c`, `desktop_store_entry` in `lib/desktop.sh`, il gruppo "store" in `share/tod-updates.py`,
  chiavi `st_*` in `lib/i18n.sh`, nove test `test_store_*`.

### App che non si aprivano (Boxy SVG, 9 novembre)

- Il comando di molte app è uno **script** (`#!/bin/bash`): senza proot si avvia l'interprete del runtime con
  lo script (`lancio()` in `tod-store.py`), come fa `lib/diretto.sh` per Debian.
- **zypak** (la sandbox di Chromium/Electron fatta con Flatpak): al suo posto un aiutante nostro
  (`<store>/aiutanti/zypak-wrapper`, primo nel PATH) che avvia Electron con `--no-sandbox
  --disable-dev-shm-usage`; c'è anche `flatpak-spawn`.
- Chromium si fermava subito (SIGTRAP) perché `/proc/self/exe` diceva `/app/...` e i collegamenti risolti
  il percorso vero: ora i programmi partono dal percorso vero. Boxy SVG: finestra in 1,5 s, senza proot.
- Il ripiego su proot passava le librerie del runtime a proot stesso ("bad ELF magic"): ora l'ambiente
  dell'app va solo dentro proot (`env -i`).
- Una prova ha installato davvero KCalc e libmpc in Termux (la scelta della fonte vale per tutto il
  sistema, anche con la cartella di prova): tolti subito. Da allora le prove usano `--flathub` /
  `TOD_STORE_SOLO_FLATHUB=1` o un `tod` finto.

## Da dove arrivano le app: prima Termux, poi Debian, Flathub per il resto

Lo Store mostra tutto il catalogo di Flathub, ma "Installa" sceglie la strada più leggera, nello stesso
ordine di `tod apt`: se Termux ha la stessa app la installa da Termux (nativa), se no da Debian, e solo
le app che lì non ci sono arrivano da Flathub (con la loro base di qualche GB).

- Nomi provati (`store_candidati`): la chiave di `tod-app-keys.awk` (`org.gimp.GIMP` → `gimp`), fornitore-nome
  (`org.telegram.desktop` → `telegram-desktop`, `org.gnome.Boxes` → `gnome-boxes`) e pochi nomi noti
  (`obs-studio`, `code-oss`).
- Un pacchetto vale come "la stessa app" solo se dipende da una libreria grafica (GTK, Qt, KDE, SDL, X11,
  Electron, wx...) o è un metapacchetto dei suoi pezzi (libreoffice): `boxes`, che in Termux e Debian è un
  programma da terminale, viene scartato.
- Provato in sola lettura su 19 app: GIMP, Inkscape, Krita, Telegram, LibreOffice, VLC, OBS, VS Code
  (code-oss), Firefox, Thunderbird, Audacity, Kdenlive, Okular, Calcolatrice di GNOME, KCalc da Termux;
  GNOME Boxes, KWrite, Foliate, Shortwave da Debian. 0,3–0,6 s se è in Termux, 2–4 s se serve Debian.
- L'installazione passa da `tod apt install -y <pacchetto>`; poi l'app è "Nel sistema". Nella pagina
  dell'app la tessera "Da" dice Termux · nativa / Debian / Flathub (runtime "non serve" se dal sistema).
- Prova a vista con un `tod` finto (niente installato nel sistema): Krita "Da Termux", "Scarico i
  pacchetti (61.4 MB)", poi "Nel sistema" e "Installata da Termux: la trovi nello Start".

## La finestra dello Store, come l'App Store (rifatta il 9 novembre: "troppo Linux")

`tod store` nel desktop (o `tod store finestra [id]`) apre `share/store/tod-store-gui.py` (GTK 3 di Termux).

- Barra laterale come l'App Store: ricerca, Scopri, Crea, Lavora, Gioca, Sviluppa, Categorie, Installate;
  icone nell'accento, voce scelta in grigio. Contenuto su fondo pieno (bianco / #1e1e21).
- Scopri e le sezioni: due schede grandi fatte con la prima schermata dell'app (nome in bianco sopra),
  poi scaffali di app in due-tre colonne, senza riquadri, con un filo sotto ogni riga; "Vedi tutte".
- Pulsanti a capsula "Ottieni" / "Apri" (anche per le app che il sistema ha già: si apre quella).
- **Avanzamento**: la finestra legge `tod store installa` da un terminale finto (`TOD_STORE_AVANZAMENTO=1`):
  Flatpak dà la tabella delle misure e la percentuale, apt le righe di stato (`APT::Status-Fd`). Sul
  pulsante un anello che si riempie, nella pagina dell'app la barra e "Scarico 45% · 186 MB di 410 MB",
  "Installo 80%", poi "Installata: la trovi nello Start".
- Pagina dell'app: icona grande, nome, sottotitolo, sviluppatore; la striscia DA / CATEGORIA /
  SVILUPPATORE / LINGUA / VERSIONE / Verificata; schermate; descrizione con "altro"; Novità (note
  dell'ultima versione); Informazioni (fornitore, licenza, da dove arriva, base, compatibilità).
- Provata su schermo finto, chiaro e scuro: Planify installata davvero da Flathub (base GNOME 50, 806 MB)
  in 36 s con la percentuale; VLC "da Termux" con il `tod` finto (anello, barra, "Installo 80%").
- Catalogo: `share/store/catalogo.py` legge l'AppStream di Flathub (2932 app; 5,4 s la prima volta,
  0,31 s dall'indice in cache). Finestra aperta in 1,0–1,3 s, ~95 MB.
- Voce **Store** nello Start (`desktop_store_entry` in `lib/desktop.sh`): la crea `desktop_install_entries`,
  quindi arriva con l'installazione e con ogni aggiornamento di Termux On DeX (`self_after`); `tod store _voce` la
  crea subito. Lo Store si prepara da solo alla prima apertura (Flatpak di Debian e catalogo, circa un minuto).
- **Aggiornamenti** (`share/tod-updates.py`): nel controllo `tod store _aggiornamenti` elenca app e runtime di
  Flathub con una versione nuova (`flatpak remote-ls --updates`); le app compaiono con nome e icona, i runtime tra
  i componenti di sistema; "Aggiorna" chiama `tod store aggiorna [id]`, che rifà radici e voci dello Start.

## Prossimi passi (da decidere)

1. **Spazio**: (a) accettare il doppio; (b) dopo ogni installazione svuotare il deposito dei file
   (gli aggiornamenti scaricano poi i file interi, non le differenze); (c) un nostro scaricatore
   OSTree che scrive direttamente nella cartella installata e riusa i file uguali (più lavoro, il
   risultato migliore).
2. **apt al contrario**: oggi lo Store rifiuta le app che Termux o Debian hanno già, ma `tod apt`
   non sa delle app dello Store (poche righe in `app_owner`).
3. Electron con la GPU vera (oggi ripiega sul software); provare un gioco Vulkan.
