# shellcheck shell=bash
# =============================================================================
#  Store (prototipo, 9 novembre 2026): le app di Flathub per aarch64, senza la sandbox di Flatpak.
#  Flatpak vero su Android non va: bubblewrap vuole i namespace utente, che Android vieta alle app.
#   - Flatpak di Debian, estratto (dpkg-deb -x) nella cartella dello Store: niente installato nel
#     container. Gira senza proot come le app di Debian (lib/diretto.sh), con share/store/tod-nolink.c
#     (Android vieta i collegamenti fisici: OSTree ripiega sulle copie) e FLATPAK_SYSTEM_HELPER_ON_SESSION=1
#     (il controllo parentale cerca il D-Bus di sistema, che su Android non c'è). Scarica, verifica (GPG)
#     e aggiorna lui.
#   - L'avvio è nostro (share/store/tod-store.py): runtime su /usr e app su /app con collegamenti e
#     fakechroot, proot se senza non parte; GPU: zink di Flathub sopra il turnip KGSL del container.
#   - Una app, un solo sistema: se Termux o Debian hanno già l'app (share/tod-app-keys.awk), non si installa.
#  Caricato solo da "tod store" (tod_main), non a ogni comando.
# =============================================================================

STORE_FLATHUB=https://dl.flathub.org/repo/flathub.flatpakrepo

store_dir() { printf '%s' "${TOD_STORE_DIR:-${TOD_TERMUX_HOME:-$HOME}/.local/share/tod-store}"; }

store_pronto() { # flatpak estratto, libreria compilata (e non più vecchia del sorgente), Flathub aggiunto
  local s
  s=$(store_dir)
  if [[ -x $s/fp/usr/bin/flatpak && -s $s/bin/tod-nolink.so && -s $s/flatpak/repo/config &&
    ! $TOD_ROOT/share/store/tod-nolink.c -nt $s/bin/tod-nolink.so ]]; then return 0; fi
  return 1
}

store_lingua() { # la lingua del container (LANG di /etc/tod/locale.env)
  local root l=""
  if root=$(cx_rootfs) && [[ -f $root/etc/tod/locale.env ]]; then
    l=$(sed -n 's/^LANG=//p' "$root/etc/tod/locale.env")
  fi
  printf '%s' "${l:-C.UTF-8}"
}

store_fp() { # store_fp argomenti... → il flatpak di Debian, senza proot, sulla cartella dello Store
  local s root d bus
  s=$(store_dir)
  root=$(cx_rootfs) || return 1
  d=$(diretto_dir)
  bus=$(cx_session_bus)
  mkdir -p "$s/casa" "$s/vuota"
  local -a amb=(
    GLIBC_TUNABLES=glibc.pthread.rseq=0
    FAKECHROOT_BASE="$root" FAKECHROOT_ELFLOADER="$d/ld.so" FAKECHROOT_ELFLOADER_OPT_ARGV0=--argv0
    FAKECHROOT_EXCLUDE_PATH="$DIRETTO_FUORI"
    LD_PRELOAD="$s/bin/tod-nolink.so $d/tod-diretto.so $root$DIRETTO_FC"
    LD_LIBRARY_PATH="$(diretto_librerie "$root" "$d"):$s/fp/usr/lib/aarch64-linux-gnu"
    PATH=/usr/local/bin:/usr/bin:/bin
    HOME="$s/casa" XDG_CACHE_HOME="$s/cache" XDG_CONFIG_HOME="$s/config" XDG_DATA_HOME="$s/dati"
    TMPDIR="$TMPDIR" LANG="$(store_lingua)" TERM="${TERM:-xterm}"
    GCONV_PATH="$root/usr/lib/aarch64-linux-gnu/gconv"
    FLATPAK_USER_DIR="$s/flatpak" FLATPAK_TRIGGERSDIR="$s/vuota" FLATPAK_BWRAP=/bin/false
    FLATPAK_SYSTEM_HELPER_ON_SESSION=1
  )
  if [[ -n $bus ]]; then amb+=(DBUS_SESSION_BUS_ADDRESS="$bus"); fi
  env -i "${amb[@]}" "$d/ld.so" --argv0 flatpak "$s/fp/usr/bin/flatpak" "$@"
}

store_prepara() { # una volta: flatpak di Debian nella cartella dello Store, la libreria, Flathub
  cx_installed || die "$(t cx_missing)"
  diretto_prepara || die "$(t dir_no_prep)"   # caricatore corretto e libfakechroot del container
  local s root f
  s=$(store_dir)
  root=$(cx_rootfs)
  mkdir -p "$s/bin" "$s/deb"
  if [[ ! -x $s/fp/usr/bin/flatpak ]]; then
    info "$(t st_prepara)"
    # solo i pacchetti che il container non ha (apt-get -s: simulazione), scaricati come utente:
    # nel container non si installa niente
    rm -f "$s/deb"/*.deb
    # shellcheck disable=SC2016 # lo script gira nel container: $1 e $p sono suoi
    proot-distro login --user "$(cx_user)" --shared-tmp "$(cx_distro)" -- /bin/bash -c \
      'cd "$1" && p=$(apt-get -s install --no-install-recommends flatpak | sed -n "s/^Inst \([^ ]*\) .*/\1/p") &&
       apt-get download ${p:-flatpak}' _ "$s/deb" >>"$TOD_LOG" 2>&1 || die "$(t st_prep_ko "$TOD_LOG")"
    rm -rf "$s/fp.nuovo"
    for f in "$s/deb"/*.deb; do
      dpkg-deb -x "$f" "$s/fp.nuovo" >>"$TOD_LOG" 2>&1 || die "$(t st_prep_ko "$TOD_LOG")"
    done
    [[ -x $s/fp.nuovo/usr/bin/flatpak ]] || die "$(t st_prep_ko "$TOD_LOG")"
    rm -rf "$s/fp"
    mv "$s/fp.nuovo" "$s/fp"
  fi
  if [[ ! -s $s/bin/tod-nolink.so || $TOD_ROOT/share/store/tod-nolink.c -nt $s/bin/tod-nolink.so ]]; then
    if ! have clang; then pkg_install clang || return 1; fi
    clang --target=aarch64-linux-gnu -O2 -fPIC -shared -nostdlib -ffreestanding -fno-builtin -fuse-ld=lld \
      -o "$s/bin/tod-nolink.so.$$" "$TOD_ROOT/share/store/tod-nolink.c" "$root$DIRETTO_LIBC" >>"$TOD_LOG" 2>&1 ||
      die "$(t st_prep_ko "$TOD_LOG")"
    mv -f "$s/bin/tod-nolink.so.$$" "$s/bin/tod-nolink.so"
  fi
  if [[ ! -s $s/flatpak/repo/config ]] || ! grep -q '^\[remote "flathub"\]' "$s/flatpak/repo/config"; then
    store_fp remote-add --user --if-not-exists flathub "$STORE_FLATHUB" >>"$TOD_LOG" 2>&1 ||
      die "$(t st_prep_ko "$TOD_LOG")"
  fi
  # Mesa due volte (25.08 e 25.08-extra, ~460 MB l'uno): basta il primo, lo zink di Flathub va sul turnip
  # del container (tod-store.py gpu_env) e i codec in più stanno in codecs-extra
  local m
  m=$(store_fp mask --user 2>/dev/null) || m=""
  if [[ $m != *'GL.default//*-extra'* ]]; then
    store_fp mask --user 'org.freedesktop.Platform.GL.default//*-extra' >>"$TOD_LOG" 2>&1 || true
  fi
  log "store: pronto in $s"
}

store_assicura() {
  if ! store_pronto; then store_prepara; fi
}

store_py() { # store_py comando... → share/store/tod-store.py con l'ambiente che gli serve
  local root u casa proot=0
  root=$(cx_rootfs) || die "$(t cx_missing)"
  u=$(cx_user)
  casa=${TOD_STORE_CASA:-}
  if [[ -z $casa ]]; then casa=$(diretto_casa "$root" "$u"); fi   # la stessa casa delle app di Debian
  if ! diretto_on; then proot=1; fi
  TOD_STORE_DIR=$(store_dir) TOD_STORE_ROOTFS=$root TOD_STORE_DIRETTO="$(diretto_dir)/tod-diretto.so" \
    TOD_STORE_FC=$root$DIRETTO_FC TOD_STORE_CORREGGI=$TOD_ROOT/share/diretto/correggi.py \
    TOD_STORE_CASA=$casa TOD_STORE_UTENTE=$u TOD_STORE_TOD=$PREFIX/bin/tod TOD_STORE_LANG="$(store_lingua)" \
    TOD_STORE_TZ="$(prop persist.sys.timezone)" TOD_STORE_PROOT=${TOD_STORE_PROOT:-$proot} \
    DBUS_SESSION_BUS_ADDRESS="$(cx_session_bus)" python3 "$TOD_ROOT/share/store/tod-store.py" "$@"
}

store_catalogo() { # il catalogo di Flathub (AppStream) per cercare: scaricato al massimo una volta al giorno
  local a
  a=$(store_dir)/flatpak/appstream/flathub/aarch64/active/appstream.xml.gz
  if [[ -f $a && -z $(find "$a" -mmin +1440 2>/dev/null) ]]; then return 0; fi
  info "$(t st_catalogo)"
  store_fp update --user --appstream --noninteractive flathub >>"$TOD_LOG" 2>&1 || true
}

store_trova() { # store_trova nome → id di Flathub: "gimp" → org.gimp.GIMP (stessa chiave di tod-app-keys.awk)
  local n=$1 k ris a
  if [[ $n == *.*.* ]]; then printf '%s' "$n"; return 0; fi
  store_catalogo >&2
  k=$(app_key "$n")
  ris=$(store_fp search --user --columns=application "$n" 2>/dev/null) || ris=""
  while IFS= read -r a; do
    if [[ -n $a && $(app_key "$a") == "$k" ]]; then printf '%s' "$a"; return 0; fi
  done <<<"$ris"
  return 1
}

# --- da dove: prima Termux (nativa), poi Debian, Flathub solo per le app che lì non ci sono ---------------
# (lo stesso ordine di "tod apt"; ogni famiglia di app di Flathub porta con sé una base di qualche GB)

store_candidati() { # id di Flathub → nomi di pacchetto da provare: org.gimp.GIMP → gimp, org.telegram.desktop → telegram-desktop
  local id=$1 k u v
  k=$(app_key "$id")
  u=${id,,}
  v=${u%.*}
  v=${v##*.}-${u##*.}
  printf '%s\n%s\n' "$k" "$v"
  case $k in   # chiavi che tod-app-keys.awk riunisce → i nomi veri dei pacchetti
    obs) printf '%s\n' obs-studio ;;
    vscode) printf '%s\n' code-oss code ;;
  esac
}

store_dipendenze() { # store_dipendenze termux|debian pacchetto... → "pacchetto<TAB>Depends" (un record per pacchetto)
  local s=$1
  shift
  if [[ $s == termux ]]; then
    command apt-cache show "$@" 2>/dev/null
  else
    proot-distro login "$(cx_distro)" -- apt-cache show "$@" 2>/dev/null
  fi | awk '
    /^Package: / { p = ($2 in visti) ? "" : $2; visti[$2] = 1; d = "" }
    p != "" && /^(Pre-)?Depends: / { d = d ", " substr($0, index($0, ":") + 2) }
    /^$/ { if (p != "") print p "\t" d; p = "" }
    END { if (p != "") print p "\t" d }'
}

store_ha_finestra() { # store_ha_finestra pacchetto "Depends" → vero se è un'app con finestra: un pacchetto con lo stesso
  # nome ma senza librerie grafiche non è la stessa app (boxes di Debian disegna cornici nel terminale, GNOME Boxes
  # è un'altra cosa). Vale anche un metapacchetto che dipende dai suoi pezzi (libreoffice → libreoffice-calc)
  local p=$1 d=" $2"
  local re='[ ,|(](lib)?(gtk-?[0-9]|gir1\.2-gtk|adwaita|qt[56]|kf[56]|x11|sdl2|electron|wx|fltk|webkit|xfce)'
  if [[ $d =~ $re ]]; then return 0; fi
  if [[ $d == *" $p-"* ]]; then return 0; fi
  return 1
}

store_fonte() { # id di Flathub → "termux<TAB>pacchetto", "debian<TAB>pacchetto" oppure "flathub<TAB>id"
  local id=$1 c p d
  local -a cand=() te=() de=()
  mapfile -t cand < <(store_candidati "$id" | awk 'NF && !v[$0]++')
  for c in "${cand[@]}"; do
    if pkg_has "$c"; then te+=("$c"); fi
  done
  if ((${#te[@]})); then
    while IFS=$'\t' read -r p d; do
      if store_ha_finestra "$p" "$d"; then printf 'termux\t%s\n' "$p"; return 0; fi
    done < <(store_dipendenze termux "${te[@]}" || true)
  fi
  if cx_installed; then
    for c in "${cand[@]}"; do
      if cx_has "$c"; then de+=("$c"); fi
    done
    if ((${#de[@]})); then
      while IFS=$'\t' read -r p d; do
        if store_ha_finestra "$p" "$d"; then printf 'debian\t%s\n' "$p"; return 0; fi
      done < <(store_dipendenze debian "${de[@]}" || true)
    fi
  fi
  printf 'flathub\t%s\n' "$id"
}

store_installa() { # tod store installa [--flathub] <id o nome>
  local solo=${TOD_STORE_SOLO_FLATHUB:-0} n id owner dove pkg
  # --flathub (e TOD_STORE_SOLO_FLATHUB=1 nelle prove): niente Termux né Debian. Una prova nella cartella di
  # prova ha installato davvero KCalc in Termux (9 novembre): la scelta della fonte vale per tutto il sistema
  if [[ ${1:-} == --flathub ]]; then solo=1; shift; fi
  n=${1:-}
  [[ -n $n ]] || die "$(t st_uso)"
  store_assicura
  id=$(store_trova "$n") || die "$(t st_non_trovata "$n" "$n")"
  # una app, un solo sistema (share/tod-app-keys.awk: org.gimp.GIMP → gimp)
  owner=$(app_owner "$id")
  if [[ -n $owner ]]; then die "$(t apt_already "$id" "$owner")"; fi
  dove=flathub pkg=$id
  if [[ $solo != 1 ]]; then IFS=$'\t' read -r dove pkg < <(store_fonte "$id"); fi
  if [[ $dove == termux || $dove == debian ]]; then
    if [[ $dove == termux ]]; then info "$(t st_da_termux "$id" "$pkg")"; else info "$(t st_da_debian "$id" "$pkg")"; fi
    log "store: $id da $dove ($pkg)"
    # per la finestra: le righe di stato di apt (dlstatus/pmstatus con la percentuale)
    local -a stato=()
    if [[ ${TOD_STORE_AVANZAMENTO:-0} == 1 ]]; then stato=(-oAPT::Status-Fd=1); fi
    apt_cli install -y "${stato[@]}" "$pkg" || die "$(t st_inst_ko "$id" "$TOD_LOG")"
    ok "$(t st_installata_sis "$pkg")"
    return 0
  fi
  info "$(t st_installo "$id")"
  log "store: installo $id"
  # TOD_STORE_AVANZAMENTO=1 (la finestra, che legge da un terminale finto): la barra di flatpak con la
  # percentuale; --noninteractive la spegne
  local -a modo=(--noninteractive)
  if [[ ${TOD_STORE_AVANZAMENTO:-0} == 1 ]]; then modo=(); fi
  store_fp install --user -y "${modo[@]}" flathub "$id" || die "$(t st_inst_ko "$id" "$TOD_LOG")"
  store_py prepara "$id" >>"$TOD_LOG" 2>&1 || die "$(t st_inst_ko "$id" "$TOD_LOG")"
  store_menu "$id"
  ok "$(t st_installata "$id" "$id")"
}

store_menu() { # voci dello Start dell'app (Exec = tod store avvia <id>)
  local f
  if ! f=$(store_py menu "$1" 2>>"$TOD_LOG"); then
    log "store: nessuna voce per $1"
    return 0
  fi
  log "store: voci $f"
  if [[ -z ${TOD_STORE_MENU:-} ]]; then desktop_refresh_caches; fi
}

store_rimuovi() { # tod store rimuovi <id>
  local id=${1:-}
  [[ -n $id ]] || die "$(t st_uso)"
  store_assicura
  store_py togli-menu "$id" >>"$TOD_LOG" 2>&1 || true
  store_fp uninstall --user -y --noninteractive "$id" || die "$(t st_inst_ko "$id" "$TOD_LOG")"
  # i runtime che non servono più a nessuna app
  store_fp uninstall --user -y --noninteractive --unused >>"$TOD_LOG" 2>&1 || true
  rm -rf "$(store_dir)/radici/$id"
  if [[ -z ${TOD_STORE_MENU:-} ]]; then desktop_refresh_caches; fi
  ok "$(t st_rimossa "$id")"
}

store_aggiorna() { # tod store aggiorna [id...]: app e runtime (tutti, o quelli chiesti), poi radici e voci rifatte
  local id
  store_assicura
  store_fp update --user -y --noninteractive "$@" || die "$(t st_inst_ko flathub "$TOD_LOG")"
  while IFS=$'\t' read -r id _; do
    if [[ -z $id ]]; then continue; fi
    store_py prepara "$id" >>"$TOD_LOG" 2>&1 || warn "$(t st_inst_ko "$id" "$TOD_LOG")"
    store_menu "$id"
  done < <(store_py elenco)
  ok "$(t st_aggiornate)"
}

store_elenco() {
  local l
  if ! store_pronto; then
    info "$(t st_nessuna)"
    return 0
  fi
  l=$(store_py elenco)
  if [[ -z $l ]]; then
    info "$(t st_nessuna)"
    return 0
  fi
  awk -F '\t' '{ printf "%-28s %-12s %s\n", $2, $3, $1 }' <<<"$l"
}

store_aggiornamenti() { # per Aggiornamenti (share/tod-updates.py): "id<TAB>nome<TAB>da<TAB>a<TAB>icona" di ciò che
  # Flathub ha più nuovo (app e runtime; i runtime senza nome). --locale: senza rete, dall'ultimo elenco scaricato
  local -a cache=()
  if ! store_pronto; then return 0; fi
  if [[ ${1:-} == --locale ]]; then cache=(--cached); fi
  local up el
  up=$(store_fp remote-ls --user --updates "${cache[@]}" --columns=application,version flathub 2>>"$TOD_LOG") || up=""
  if [[ -z $up ]]; then return 0; fi
  el=$(store_py elenco 2>>"$TOD_LOG") || el=""
  awk -F '\t' 'NR == FNR { if (NF) { nome[$1] = $2; da[$1] = $3; ic[$1] = $4 } next }
    NF && $1 != "Application ID" { printf "%s\t%s\t%s\t%s\t%s\n", $1, nome[$1], da[$1], $2, ic[$1] }' \
    <(printf '%s\n' "$el") <(printf '%s\n' "$up")
}

store_finestra() { # la finestra dello Store (share/store/tod-store-gui.py), in sottofondo
  [[ -n ${DISPLAY:-} ]] || die "$(t st_uso)"
  (setsid python3 "$TOD_ROOT/share/store/tod-store-gui.py" "$@" </dev/null >>"$TOD_LOG" 2>&1 &)
}

store_cli() { # tod store [finestra|cerca|info|installa|avvia|elenco|rimuovi|aggiorna|prepara]
  local c=${1:-}
  if (($#)); then shift; fi
  if [[ -z $c ]]; then   # senza niente: la finestra nel desktop, l'elenco in un terminale senza schermo
    if [[ -n ${DISPLAY:-} ]]; then c=finestra; else c=elenco; fi
  fi
  case $c in
    finestra | apri | gui) store_finestra "$@" ;;
    _catalogo) # per la finestra: Store pronto e catalogo fresco, poi la cartella del catalogo
      store_assicura >&2
      store_catalogo >&2
      printf '%s\n' "$(store_dir)/flatpak/appstream/flathub/aarch64/active"
      ;;
    _fonte) # per la finestra: da dove arriverebbe l'app (termux|debian|flathub<TAB>pacchetto o id)
      [[ -n ${1:-} ]] || die "$(t st_uso)"
      store_fonte "$1"
      ;;
    _aggiornamenti) store_aggiornamenti "$@" ;;
    _voce) # la voce "Store" nello Start (la crea anche desktop_install_entries: installazione e aggiornamenti)
      desktop_store_entry
      desktop_refresh_caches
      ;;
    _chiavi) # per la finestra: "chiave<TAB>pacchetto" delle app di Termux e di Debian (una app, un solo sistema)
      app_keys_termux
      if cx_installed; then app_keys_cx; fi
      ;;
    cerca | search)
      [[ -n ${1:-} ]] || die "$(t st_uso)"
      store_assicura
      store_catalogo
      store_fp search --user --columns=application,name,description "$@"
      ;;
    info)
      [[ -n ${1:-} ]] || die "$(t st_uso)"
      store_assicura
      store_fp remote-info --user flathub "$(store_trova "$1" || printf '%s' "$1")"
      ;;
    installa | install) store_installa "$@" ;;
    avvia | run)
      [[ -n ${1:-} ]] || die "$(t st_uso)"
      store_py avvia "$@"
      ;;
    elenco | list) store_elenco ;;
    rimuovi | remove | uninstall) store_rimuovi "$@" ;;
    aggiorna | update) store_aggiorna "$@" ;;
    prepara) store_prepara ;;
    *) die "$(t st_uso)" ;;
  esac
}
