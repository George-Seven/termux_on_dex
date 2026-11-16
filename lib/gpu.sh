# shellcheck shell=bash
# =============================================================================
#  Accelerazione grafica
#
#  Due "stack" di driver lato Termux:
#    official → mesa + Turnip (mesa-vulkan-icd-freedreno) dei repository Termux
#    lfdevs   → mesa con backend Freedreno-KGSL (OpenGL nativo su Adreno, senza
#               Zink) dai pacchetti di lfdevs/termux-packages, bloccati con apt-mark
#  Profili (variabili d'ambiente) provati in ordine, il primo che funziona vince:
#    kgsl > zink-turnip > zink-turnip-sw > zink-wrapper > virgl > virgl-angle > llvmpipe
#  Il test è reale (glxinfo su un server X vero, poi un disegno riletto: gpu_disegna),
#  quindi gestisce da solo casi come le Adreno 840 "v2" che Turnip non riconosce
#  (VK_ERROR_INCOMPATIBLE_DRIVER) e i driver che si aprono ma non disegnano.
#  Senza Adreno (Mali, Xclipse, PowerVR) non c'è un driver Mesa: OpenGL passa da virgl,
#  cioè dal driver del telefono: "virgl" col suo OpenGL ES, "virgl-angle" con ANGLE sul
#  suo Vulkan (per i telefoni dove l'OpenGL ES di sistema non basta a virgl).
# =============================================================================

declare -gA GPU_RANK=([kgsl]=1 [zink-turnip]=2 [zink-turnip-sw]=3 [zink-wrapper]=4 [virgl]=5 [virgl-angle]=6 [llvmpipe]=9)

LFDEVS_TERMUX_REPO=lfdevs/termux-packages
LFDEVS_CX_REPO=lfdevs/mesa-for-android-container
# Release di riserva se l'API GitHub non risponde (checksum dalle note di rilascio)
LFDEVS_TERMUX_FALLBACK_TAG=freedreno-26.2.0-devel-20260709
declare -gA LFDEVS_TERMUX_FALLBACK=(
  [mesa_26.2.0-1_aarch64.deb]=5adfa7b3000bdce3ff660afb40e069e95508b0ab5c54c29dd9732111a548cfeb
  [mesa-vulkan-icd-freedreno_26.2.0-1_aarch64.deb]=f22d1258f28d71f38ce4e39e67f251565f1f1df57ccb4c046698e6e853aa5aca
)
LFDEVS_CX_FALLBACK_TAG=mesa-26.2.0-devel-20260709
declare -gA LFDEVS_CX_FALLBACK=(
  [debian_trixie_arm64]=4ee525b4844fb189f3762ff75ef77f5b80eee8f6cdab2e5588731b5c9f8168bb
  [ubuntu_noble_arm64]=89adc0d7b7eaa26e619cd6c41114894c152fa1680d0ff1969947e94e151f660f
  [ubuntu_questing_arm64]=344dffe6f06ccf8cf67f7f679a697eb55be47a79a09e369a42f36cbac24e39a0
  [ubuntu_resolute_arm64]=ed98eaeb85c1f84288759ddcc6936730a2ecd6c76d6a870b3a474797b2c68fad
)

TOD_GPU_PROBE="$TOD_STATE_DIR/gpu-probe.txt"

# variabili che un profilo può impostare: vanno tolte prima di provarne un altro,
# altrimenti (es. lanciando "tod gpu probe" dal terminale del desktop) si mescolano
GPU_VARS=(MESA_LOADER_DRIVER_OVERRIDE GALLIUM_DRIVER VK_ICD_FILENAMES VK_DRIVER_FILES TU_DEBUG
  MESA_VK_WSI_DEBUG MESA_GL_VERSION_OVERRIDE MESA_GLSL_VERSION_OVERRIDE LIBGL_ALWAYS_SOFTWARE
  TOD_NEEDS_VIRGL TOD_GPU_PROFILE vblank_mode MESA_VK_WSI_PRESENT_MODE VTEST_SOCKET_NAME)

gpu_env_reset() { unset "${GPU_VARS[@]}"; }

gpu_unset_args() { # → argomenti "-u VAR" per env(1)
  local v
  for v in "${GPU_VARS[@]}"; do printf -- '-u\n%s\n' "$v"; done
}

gpu_stack() { cat "$TOD_STATE_DIR/gpu-stack" 2>/dev/null || echo official; }
gpu_set_stack() { echo "$1" >"$TOD_STATE_DIR/gpu-stack"; }

gpu_icd() { # gpu_icd schema → percorso del JSON ICD Vulkan (es. freedreno, wrapper)
  local f
  for f in "$PREFIX"/share/vulkan/icd.d/*"$1"*.json; do
    if [[ -f $f ]]; then printf '%s' "$f"; return 0; fi
  done
  return 1
}

# gpu_profile_env profilo → righe VAR=valore (l'ICD è fissato: con più driver
# installati Zink potrebbe altrimenti scegliere lavapipe o il driver sbagliato)
gpu_profile_env() {
  local icd
  case $1 in
    kgsl)
      echo MESA_LOADER_DRIVER_OVERRIDE=kgsl
      ;;
    zink-turnip | zink-turnip-sw)
      icd=$(gpu_icd freedreno) || return 1
      printf '%s\n' MESA_LOADER_DRIVER_OVERRIDE=zink GALLIUM_DRIVER=zink \
        "VK_ICD_FILENAMES=$icd" "VK_DRIVER_FILES=$icd" TU_DEBUG=noconform
      if [[ $1 == zink-turnip-sw ]]; then echo MESA_VK_WSI_DEBUG=sw; fi
      ;;
    zink-wrapper) # Zink sopra il driver Vulkan proprietario di Android
      icd=$(gpu_icd wrapper) || return 1
      printf '%s\n' MESA_LOADER_DRIVER_OVERRIDE=zink GALLIUM_DRIVER=zink "VK_ICD_FILENAMES=$icd" "VK_DRIVER_FILES=$icd"
      ;;
    virgl | virgl-angle) # OpenGL tramite virgl_test_server_android (driver di sistema): va su qualsiasi GPU.
      # Le variabili sono le stesse: cambia come parte il server (VIRGL_MODO in tod.conf, vedi gpu_write_env)
      printf '%s\n' GALLIUM_DRIVER=virpipe MESA_GL_VERSION_OVERRIDE=4.0 MESA_GLSL_VERSION_OVERRIDE=400 TOD_NEEDS_VIRGL=1
      ;;
    llvmpipe)
      printf '%s\n' GALLIUM_DRIVER=llvmpipe LIBGL_ALWAYS_SOFTWARE=1
      ;;
    *) return 1 ;;
  esac
}

gpu_env_export() { # carica il profilo attivo nell'ambiente corrente
  gpu_env_reset
  [[ -f $TOD_GPU_ENV ]] || return 0
  set -a
  # shellcheck source=/dev/null
  . "$TOD_GPU_ENV"
  set +a
}

gpu_write_env() { # gpu_write_env profilo
  local p=$1 spec
  spec=$(gpu_profile_env "$p") || return 1
  {
    echo "# Generato da Termux On DeX $TOD_VERSION - profilo GPU: $p ($(date '+%F %T'))"
    echo "# Cambia con: tod gpu use <profilo>  |  nuovo test: tod gpu probe"
    echo "TOD_GPU_PROFILE=$p"
    printf '%s\n' "$spec"
    case $(conf_get VSYNC on) in
      off) echo "vblank_mode=0" ;;
      tearfree) printf '%s\n' "vblank_mode=3" "MESA_VK_WSI_PRESENT_MODE=mailbox" ;;
    esac
  } >"$TOD_GPU_ENV"
  conf_set GPU_PROFILE "$p"
  # un solo server virgl per desktop e container: come parte lo decide il profilo del desktop, se è virgl
  if [[ $p == virgl* ]]; then conf_set VIRGL_MODO "$(virgl_modo_di "$p")"; fi
}

# ----------------------------------------------------------------------------
#  virgl (server lato Termux, usato anche dal container come ripiego)
#
#  Su Mali, Xclipse e PowerVR tutto l'OpenGL del desktop passa da questo processo: se si ferma, ogni app
#  che disegna con la GPU si chiude. Quindi (16 novembre): lo si riconosce dal pid e dal nome del processo
#  (mai `pgrep -f`/`pkill -f`: prendono anche le shell che hanno quel testo nel comando), le prove dei
#  profili usano un server a parte su un socket loro (prima `tod gpu probe` spegneva quello del desktop
#  acceso), e il guardiano della sessione lo riaccende se cade (virgl_riaccendi).
# ----------------------------------------------------------------------------
VIRGL_NOME=virgl_test_serv   # il nome del processo come lo dà il kernel (15 lettere)

# ----------------------------------------------------------------------------
#  Venus: il Vulkan del telefono per le app del container, sui telefoni senza Adreno (16 novembre)
#
#  Il server virgl sa passare anche Vulkan (--venus), ma nel pacchetto virglrenderer-android di Termux la
#  parte per Android è rimasta fuori dalla compilazione: sul Mali si ferma alla prima immagine. Il nostro
#  (share/venus: stessi sorgenti e stesse modifiche di Termux, più la parte per Android accesa) regge
#  insieme OpenGL e Vulkan, quindi prende il posto dell'altro. Si scarica dalla release venus-N del
#  progetto in ~/.local/share/tod-venus; finché VENUS_SHA256 è vuoto (nessuna release) non succede niente.
#  Le app ricevono il Vulkan (/etc/tod/vulkan.env del container) solo dopo una prova vera (cx_venus_prova)
#  e solo mentre il server nostro è acceso ($TMPDIR/tod-virgl.venus). VENUS=0 spegne tutto.
# ----------------------------------------------------------------------------
VENUS_VERSIONE=1
VENUS_SHA256=c41973461ea6665a6f3e026c4c13064e01e1155141af8000090524fe1e9875a6
VENUS_URL=${TOD_VENUS_URL:-"https://github.com/PietroCav501/termux_on_dex/releases/download/venus-$VENUS_VERSIONE/tod-venus-aarch64.tar.xz"}
VENUS_FILE="bin/virgl_test_server lib/libvirglrenderer.so lib/libepoxy.so libexec/virgl_render_server"

venus_on() { [[ $(conf_get VENUS 1) == 1 ]]; }

venus_dir() { printf '%s/.local/share/tod-venus' "${TOD_TERMUX_HOME:-$HOME}"; }

venus_pronto() { # i file del server nostro ci sono, sono di questa versione e la prova non l'ha bocciato
  local d f
  d=$(venus_dir)
  if [[ -f $d/guasto ]]; then return 1; fi
  for f in $VENUS_FILE; do [[ -s $d/$f ]] || return 1; done
  [[ $(cat "$d/versione" 2>/dev/null) == "$VENUS_VERSIONE" ]]
}

venus_scarica() { # l'archivio della release, controllato con l'impronta scritta qui
  local d a t f
  d=$(venus_dir)
  a="$TOD_CACHE_DIR/tod-venus-$VENUS_VERSIONE.tar.xz"
  # già accanto al progetto (portato a mano, prima della release): si usa quello, se l'impronta torna
  if [[ ! -f $a && -f $TOD_ROOT/dist/tod-venus-aarch64.tar.xz ]] && sha256_ok "$TOD_ROOT/dist/tod-venus-aarch64.tar.xz" "$VENUS_SHA256"; then
    mkdir -p "$TOD_CACHE_DIR"
    cp -f "$TOD_ROOT/dist/tod-venus-aarch64.tar.xz" "$a"
  fi
  net_download "$VENUS_URL" "$a" "$VENUS_SHA256" || return 1
  mkdir -p "${d%/*}"
  t=$(mktemp -d "$d.XXXXXX") || return 1
  if ! tar -xJf "$a" -C "$t"; then rm -rf "$t"; return 1; fi
  for f in $VENUS_FILE; do [[ -s $t/$f ]] || { rm -rf "$t"; return 1; }; done
  chmod 755 "$t/bin/virgl_test_server" "$t/libexec/virgl_render_server"
  echo "$VENUS_VERSIONE" >"$t/versione"
  rm -rf "$d"
  mv "$t" "$d"
  venus_collega
}

# Il server deve usare la grafica di ANDROID. I nomi che cerca (libEGL.so, libGLESv2.so, libvulkan.so) nella
# sua cartella, che virgl_comando mette per prima: senza, il caricatore trova prima quelle di Termux, cioè Mesa,
# e tutto l'OpenGL del desktop finisce disegnato dal processore (S20+, 16 novembre: "virgl (LLVMPIPE)" e schermo
# nero all'avvio). A ogni avvio, così si aggiusta da sola anche un'installazione fatta prima di questa correzione.
venus_collega() {
  local d n
  d=$(venus_dir)
  [[ -d $d/lib ]] || return 0
  for n in libEGL.so libGLESv2.so libvulkan.so libvulkan.so.1; do
    if [[ $(readlink "$d/lib/$n" 2>/dev/null) != "/system/lib64/${n%.1}" ]]; then
      ln -sf "/system/lib64/${n%.1}" "$d/lib/$n" 2>/dev/null || true
    fi
  done
  return 0
}

# Prima di fidarsi: col server nostro l'OpenGL deve arrivare dal chip e disegnare (la stessa prova dei profili:
# server a parte, glxinfo, triangolo). Se no si segna "guasto" e resta il server di Termux, com'era prima.
venus_verifica() {
  local d r
  d=$(venus_dir)
  if r=$(gpu_probe_one virgl); then
    log "venus: OpenGL col server nostro: $r"
    return 0
  fi
  log "venus: col server nostro l'OpenGL non arriva dal chip (${r:-niente}): resta il server di Termux"
  echo "$VENUS_VERSIONE" >"$d/guasto"
  return 1
}

venus_install() { # passo dell'installazione e di ogni aggiornamento: solo senza Adreno, solo se c'è una release
  venus_on || return 0
  [[ -n $VENUS_SHA256 ]] || return 0
  if [[ -z ${GPU_FAMILY:-} ]]; then detect_device; fi
  if [[ $GPU_FAMILY == adreno ]]; then return 0; fi
  # già scaricato e bocciato dalla prova su questo telefono: non si riprova a ogni aggiornamento
  if [[ $(cat "$(venus_dir)/guasto" 2>/dev/null) == "$VENUS_VERSIONE" ]]; then return 0; fi
  if ! venus_pronto; then
    venus_scarica || return 1
    log "venus: server grafico nostro installato (versione $VENUS_VERSIONE)"
    x11_with_server venus_verifica || return 0
  fi
  # il Vulkan nel container: deciso da una prova vera, su un server a parte
  if cx_installed; then x11_with_server cx_venus_prova || true; fi
  return 0
}

venus_passo() { # per l'installazione guidata: senza Venus il desktop va lo stesso
  venus_install || warn "$(t venus_no)"
}

virgl_comando() { # il server da lanciare, un pezzo per riga: il nostro (con Venus) se c'è, sennò quello di Termux
  local d
  d=$(venus_dir)
  if [[ -z ${VENUS_SALTA:-} ]] && venus_on && venus_pronto; then
    venus_collega
    printf '%s\n' env "LD_LIBRARY_PATH=$d/lib" "RENDER_SERVER_EXEC_PATH=$d/libexec/virgl_render_server" \
      "$d/bin/virgl_test_server" --venus
  else
    echo virgl_test_server_android
  fi
}

venus_cli() { # tod venus [stato|prepara|on|off]
  case ${1:-stato} in
    on | si | sì) conf_set VENUS 1 ;;
    off | no)
      conf_set VENUS 0
      if cx_installed; then rm -f "$(cx_rootfs)/etc/tod/vulkan.env"; fi
      ;;
    prepara)
      conf_set VENUS 1
      rm -f "$(venus_dir)/guasto"   # chiesto a mano: si riprova
      [[ -n $VENUS_SHA256 ]] || die "$(t venus_no_release)"
      venus_install || die "$(t venus_no)"
      ;;
    stato | status) ;;
    *) die "tod venus stato|prepara|on|off" ;;
  esac
  local s v
  if ! venus_on; then s=$(t venus_spento); elif venus_pronto; then s=$(t venus_pronto); else s=$(t venus_assente); fi
  if cx_installed && [[ -f $(cx_rootfs)/etc/tod/vulkan.env ]]; then v=$(t venus_cx_si); else v=$(t venus_cx_no); fi
  ok "$(t venus_stato "$s" "$v")"
}

virgl_modo_di() { # profilo → opzione del server ("" = OpenGL ES di sistema)
  case $1 in
    virgl-angle) echo angle-vulkan ;;
    *) echo "" ;;
  esac
}

virgl_pid() { # il pid del server virgl del desktop, se è vivo
  local p n
  p=$(cat "$TMPDIR/tod-virgl.pid" 2>/dev/null) || p=""
  if [[ $p =~ ^[0-9]+$ ]]; then
    n=$(cat "/proc/$p/comm" 2>/dev/null) || n=""
    if [[ $n == "$VIRGL_NOME" ]]; then printf '%s' "$p"; return 0; fi
  fi
  return 1
}

# virgl_pids: tutti i processi del server (anche i figli, uno per app collegata, e quelli avviati senza file pid),
# letti da /proc/<pid>/comm. Non con `pgrep -x`: quello di Termux (procps-ng 4) per i nomi più lunghi di 15 lettere
# confronta il nome intero, quello di Debian le prime 15: sul S20+ (16 novembre) `pgrep -x virgl_test_serv` non
# trovava il server acceso. Nei test solo i processi nati dal test (mai un server vero sul telefono).
virgl_pids() {
  local f n p
  for f in /proc/[0-9]*/comm; do
    n=""
    read -r n 2>/dev/null <"$f" || true
    [[ $n == "$VIRGL_NOME" ]] || continue
    p=${f%/comm}
    p=${p##*/}
    if [[ -n ${TOD_TEST_RUN:-} ]] && ! tr '\0' '\n' <"/proc/$p/environ" 2>/dev/null | grep -qxF "TOD_TEST_RUN=$TOD_TEST_RUN"; then
      continue
    fi
    printf '%s\n' "$p"
  done
  return 0
}

virgl_running() { virgl_pid >/dev/null || [[ -n $(virgl_pids) ]]; }

virgl_start() { # virgl_start [modo]: senza argomento il modo scelto dalla prova (VIRGL_MODO)
  have virgl_test_server_android || return 1
  local modo p
  if (($#)); then modo=$1; else modo=$(conf_get VIRGL_MODO); fi
  if virgl_running; then
    # già acceso nel modo giusto (o acceso da una versione di prima, senza traccia del modo): resta
    if [[ ! -f $TMPDIR/tod-virgl.modo || $(cat "$TMPDIR/tod-virgl.modo" 2>/dev/null) == "$modo" ]]; then return 0; fi
    virgl_stop
  fi
  local -a arg=() avvio=(nohup) srv=()
  mapfile -t srv < <(virgl_comando)
  if [[ -n $modo ]]; then arg=("--$modo"); fi
  # nuova sessione di processo, come il server X: chiudere la scheda di Termux non lo spegne
  if have setsid; then avvio=(setsid nohup); fi
  # azzerato e poi aperto in coda (>>): così session_log_cap può accorciarlo mentre il server scrive
  : >"$TOD_STATE_DIR/virgl.log"
  "${avvio[@]}" "${srv[@]}" "${arg[@]}" >>"$TOD_STATE_DIR/virgl.log" 2>&1 </dev/null &
  p=$!
  printf '%s\n' "$p" >"$TMPDIR/tod-virgl.pid"
  printf '%s\n' "$modo" >"$TMPDIR/tod-virgl.modo"
  sleep 1
  if ! virgl_pid >/dev/null; then
    # setsid può aver cambiato pid: si cerca per nome
    p=$(virgl_pids | sort -n | tail -n 1)
    if [[ -n $p ]]; then printf '%s\n' "$p" >"$TMPDIR/tod-virgl.pid"; fi
  fi
  if virgl_running; then
    # col server nostro le app del container ricevono anche il Vulkan (diretto_run, tod-run)
    if ((${#srv[@]} > 1)); then : >"$TMPDIR/tod-virgl.venus"; else rm -f "$TMPDIR/tod-virgl.venus"; fi
    return 0
  fi
  log "virgl: il server non è partito (modo: ${modo:-sistema}): $(tail -n 3 "$TOD_STATE_DIR/virgl.log" 2>/dev/null | tr '\n' ' ')"
  rm -f "$TMPDIR/tod-virgl.pid" "$TMPDIR/tod-virgl.modo" "$TMPDIR/tod-virgl.venus"
  # il server nostro non parte su questo telefono: quello di Termux, com'era prima (l'OpenGL prima di tutto)
  if ((${#srv[@]} > 1)); then
    log "venus: il server nostro non parte: torno a quello di Termux"
    VENUS_SALTA=1
    virgl_start "$modo"
    return
  fi
  return 1
}

virgl_stop() {
  local p
  if p=$(virgl_pid); then kill "$p" 2>/dev/null || true; fi
  for p in $(virgl_pids); do kill "$p" 2>/dev/null || true; done   # anche i figli e i server di prima
  rm -f "$TMPDIR/tod-virgl.pid" "$TMPDIR/tod-virgl.modo" "$TMPDIR/tod-virgl.venus"
}

# Le prove dei profili: un server a parte, su un socket suo. Quello del desktop non si tocca mai.
virgl_prova_sock() { printf '%s/tod-virgl-prova.sock' "$TMPDIR"; }

virgl_prova_start() { # virgl_prova_start [modo] → 0 se il server di prova risponde
  have virgl_test_server_android || return 1
  local i sock
  local -a arg=() srv=()
  mapfile -t srv < <(virgl_comando)
  sock=$(virgl_prova_sock)
  virgl_prova_stop
  if [[ -n ${1:-} ]]; then arg=("--$1"); fi
  nohup "${srv[@]}" "${arg[@]}" --socket-path "$sock" >"$TOD_STATE_DIR/virgl-prova.log" 2>&1 </dev/null &
  VIRGL_PROVA_PID=$!
  for ((i = 0; i < 20; i++)); do
    if [[ -S $sock ]]; then return 0; fi
    kill -0 "$VIRGL_PROVA_PID" 2>/dev/null || break
    sleep 0.25
  done
  log "virgl: il server di prova non è partito (modo: ${1:-sistema}): $(tail -n 3 "$TOD_STATE_DIR/virgl-prova.log" 2>/dev/null | tr '\n' ' ')"
  virgl_prova_stop
  return 1
}

virgl_prova_stop() {
  if [[ -n ${VIRGL_PROVA_PID:-} ]]; then
    kill "$VIRGL_PROVA_PID" 2>/dev/null || true
    wait "$VIRGL_PROVA_PID" 2>/dev/null || true
  fi
  VIRGL_PROVA_PID=""
  rm -f "$(virgl_prova_sock)"
}

virgl_serve() { # il desktop o il container usano virgl?
  [[ $(conf_get GPU_PROFILE) == virgl* || $(conf_get CX_GPU_PROFILE) == virgl* ]]
}

# virgl_riaccendi: un passo del guardiano della sessione (session_guardia, lib/session.sh). Se il server è caduto
# (Android lo chiude quando manca memoria, o un'app lo fa cadere) lo riaccende e lo dice: le app aperte che
# disegnavano con la GPU vanno riaperte, ma quelle nuove partono di nuovo. Al più 5 volte per sessione.
VIRGL_RIAVVII=0
virgl_riaccendi() {
  if ! virgl_serve || ! have virgl_test_server_android || virgl_running; then return 0; fi
  if ((VIRGL_RIAVVII >= 5)); then return 0; fi
  VIRGL_RIAVVII=$((VIRGL_RIAVVII + 1))
  log "virgl: il server non c'è più, lo riaccendo (volta $VIRGL_RIAVVII)"
  if virgl_start && have notify-send; then
    notify-send -a "Termux On DeX" -i video-display "$(t virgl_riavviato_titolo)" "$(t virgl_riavviato)" 2>/dev/null || true
  fi
  return 0
}

# ----------------------------------------------------------------------------
#  Installazione degli stack
# ----------------------------------------------------------------------------
gpu_install_base() {
  pkg_install mesa mesa-demos || return 1
  pkg_install_avail vulkan-tools vulkan-loader-generic virglrenderer-android glmark2 || true
  if [[ $GPU_FAMILY == adreno ]]; then pkg_install_avail mesa-vulkan-icd-freedreno || true; fi
}

gpu_install_lfdevs() { # Freedreno-KGSL per Termux nativo
  have jq || pkg_install jq || return 1
  local dir="$TOD_CACHE_DIR/lfdevs-termux" rel tag rx line url sha name
  local -a debs=()
  mkdir -p "$dir"
  if rel=$(gh_release "$LFDEVS_TERMUX_REPO" freedreno-); then
    tag=$(jq -r .tag_name <<<"$rel")
    for rx in '^mesa_.*_aarch64\.deb$' '^mesa-vulkan-icd-freedreno_.*_aarch64\.deb$'; do
      line=$(gh_asset "$rel" "$rx")
      if [[ -z $line ]]; then err "$(t gpu_asset_missing "$rx" "$tag")"; return 1; fi
      IFS='|' read -r url sha name <<<"$line"
      if [[ -z $sha ]]; then sha=$(gh_body_sha "$rel" "$name"); fi
      net_download "$url" "$dir/$name" "$sha" || return 1
      debs+=("$dir/$name")
    done
  else
    tag=$LFDEVS_TERMUX_FALLBACK_TAG
    warn "$(t gh_fallback "$tag")"
    for name in "${!LFDEVS_TERMUX_FALLBACK[@]}"; do
      net_download "https://github.com/$LFDEVS_TERMUX_REPO/releases/download/$tag/$name" \
        "$dir/$name" "${LFDEVS_TERMUX_FALLBACK[$name]}" || return 1
      debs+=("$dir/$name")
    done
  fi
  pkg_unhold mesa mesa-vulkan-icd-freedreno
  if ! apt_run "${APT_FLAGS[@]}" install --allow-downgrades --allow-change-held-packages "${debs[@]}"; then
    pkg_fix
    gpu_set_stack lfdevs        # forza il ripristino anche se dpkg si è fermato a metà
    gpu_restore_official || true
    return 1
  fi
  pkg_hold mesa mesa-vulkan-icd-freedreno   # `pkg upgrade` non deve sovrascriverli
  gpu_set_stack lfdevs
  conf_set GPU_LFDEVS_TAG "$tag"
  log "stack lfdevs installato: $tag"
}

gpu_restore_official() { # torna ai driver dei repository Termux
  [[ $(gpu_stack) == lfdevs ]] || return 0
  local v vi
  local -a want=()
  pkg_unhold mesa mesa-vulkan-icd-freedreno
  pkg_refresh || true
  v=$(pkg_repo_version mesa)
  if [[ -n $v ]]; then want+=("mesa=$v"); fi
  if pkg_installed mesa-vulkan-icd-freedreno; then
    vi=$(pkg_repo_version mesa-vulkan-icd-freedreno)
    if [[ -n $vi ]]; then want+=("mesa-vulkan-icd-freedreno=$vi"); fi
  fi
  if ((${#want[@]})); then
    apt_run "${APT_FLAGS[@]}" install --reinstall --allow-downgrades "${want[@]}" || return 1
  fi
  gpu_set_stack official
  log "stack official ripristinato"
}

# ----------------------------------------------------------------------------
#  Test dei profili (richiedono DISPLAY con un server X attivo)
# ----------------------------------------------------------------------------
gpu_vk_ok() { # gpu_vk_ok icd.json → 0 se vulkaninfo trova un dispositivo
  have vulkaninfo || return 0
  local out
  if ! out=$(VK_ICD_FILENAMES=$1 VK_DRIVER_FILES=$1 timeout 20 vulkaninfo --summary 2>&1 </dev/null); then
    log "vulkaninfo KO ($1): $(grep -m1 -Ei 'unsupported|incompatible|error' <<<"$out" || true)"
    return 1
  fi
  grep -q 'deviceName' <<<"$out"
}

# gpu_disegna [-u VAR | VAR=valore]...: il profilo disegna davvero? 1 solo se la prova dice di no; una prova che
# non si può fare (niente python3, niente glmark2, nessuno schermo) non boccia nessuno. Due prove, perché
# sbagliano in modi diversi (16 novembre): share/tod-gl-prova.py disegna su una finestra vera e scambia i
# fotogrammi (zink senza presentazione: "could not create swapchain"); una scena breve di glmark2 fuori schermo
# usa shader da app vera (virgl su ANGLE-GL: glxinfo e il triangolo passano, glmark2 no).
gpu_disegna() {
  local out rc
  if [[ ${TOD_GPU_DISEGNO:-1} != 1 ]]; then return 0; fi
  if have python3 && [[ -f $TOD_ROOT/share/tod-gl-prova.py ]]; then
    if out=$(env "$@" timeout 25 python3 "$TOD_ROOT/share/tod-gl-prova.py" 2>&1 </dev/null); then rc=0; else rc=$?; fi
    log "disegno: rc=$rc $(grep -E '^(TEXTURE|FINESTRA|MOTIVO|ESITO)=' <<<"$out" | tr '\n' ' ')"
    # 0 disegna, 2 non si è potuto provare; il resto (1, tempo scaduto, caduta) = non disegna
    if [[ $rc != 0 && $rc != 2 ]]; then return 1; fi
  fi
  if have glmark2; then
    if out=$(env "$@" timeout 40 glmark2 --off-screen -s 128x128 -b build:use-vbo=true:duration=1 \
      -b shading:shading=phong:duration=1 2>&1 </dev/null); then rc=0; else rc=$?; fi
    if grep -qiE 'unrecognized option|invalid option' <<<"$out"; then return 0; fi   # glmark2 senza --off-screen
    if ! grep -qE 'glmark2 Score:[[:space:]]*[1-9]' <<<"$out"; then
      log "disegno: glmark2 non arriva in fondo (rc=$rc): $(tail -n 2 <<<"$out" | tr '\n' ' ')"
      return 1
    fi
  fi
  return 0
}

gpu_probe_one() { # gpu_probe_one profilo → stampa il renderer OpenGL; 0 = funziona
  local p=$1 spec out renderer icd rc=0
  local -a envs=() clean=()
  spec=$(gpu_profile_env "$p") || return 2
  mapfile -t envs <<<"$spec"
  mapfile -t clean < <(gpu_unset_args)
  case $p in
    zink-*)
      icd=$(sed -n 's/^VK_ICD_FILENAMES=//p' <<<"$spec")
      gpu_vk_ok "$icd" || return 1
      ;;
  esac
  if [[ $p == virgl* ]]; then
    virgl_prova_start "$(virgl_modo_di "$p")" || return 1
    envs+=("VTEST_SOCKET_NAME=$(virgl_prova_sock)")
  fi
  if out=$(env "${clean[@]}" "${envs[@]}" timeout 25 glxinfo -B 2>&1 </dev/null); then rc=0; else rc=$?; fi
  renderer=$(sed -n 's/^[[:space:]]*OpenGL renderer string:[[:space:]]*//p' <<<"$out" | sed -n 1p)
  log "probe $p: rc=$rc renderer=${renderer:-nessuno}"
  if [[ $rc != 0 || -z $renderer ]]; then
    log "probe $p output: $(tail -n 3 <<<"$out" | tr '\n' ' ')"
    virgl_prova_stop
    return 1
  fi
  if [[ $p != llvmpipe && ${renderer,,} =~ llvmpipe|softpipe|swrast ]]; then virgl_prova_stop; return 1; fi
  if [[ $p == zink-* && ${renderer,,} != *zink* ]]; then return 1; fi
  # il software è l'ultima spiaggia: non c'è niente sotto, inutile provarlo
  if [[ $p != llvmpipe ]] && ! gpu_disegna "${clean[@]}" "${envs[@]}"; then
    log "probe $p: il driver risponde ma non disegna: scartato"
    virgl_prova_stop
    return 1
  fi
  virgl_prova_stop
  printf '%s' "$renderer"
}

gpu_bench_one() { # gpu_bench_one profilo → punteggio glmark2 (0 se fallisce)
  local spec out s=""
  local -a envs=() clean=()
  if ! have glmark2 || ! spec=$(gpu_profile_env "$1"); then echo 0; return 0; fi
  mapfile -t envs <<<"$spec"
  mapfile -t clean < <(gpu_unset_args)
  if [[ $1 == virgl* ]]; then
    if ! virgl_prova_start "$(virgl_modo_di "$1")"; then echo 0; return 0; fi
    envs+=("VTEST_SOCKET_NAME=$(virgl_prova_sock)")
  fi
  if out=$(env "${clean[@]}" "${envs[@]}" vblank_mode=0 timeout 150 glmark2 -s 800x600 \
    -b build:use-vbo=true:duration=4 -b texture:texture-filter=mipmap:duration=4 \
    -b shading:shading=phong:duration=4 -b bump:bump-render=high-poly:duration=4 2>&1 </dev/null); then :; fi
  virgl_prova_stop
  s=$(sed -n 's/.*glmark2 Score:[[:space:]]*\([0-9][0-9]*\).*/\1/p' <<<"$out" | sed -n 1p)
  printf '%s' "${s:-0}"
}

# Adreno 8xx (verificato su 840, Galaxy S26 Ultra, Mesa 26.3): con kgsl e con zink-turnip
# Chromium ed Electron lasciano blocchi neri e superfici incomplete (menu, popup,
# ridisegni parziali), anche senza DRI3 e senza UBWC. glmark2 invece è pulito: il
# problema è nelle app con tante superfici. zink con WSI software è pulito.
# Questi profili restano usabili a mano (tod gpu run kgsl -- gioco), non per il desktop.
gpu_glitchy() { # gpu_glitchy profilo → vero se sporca il desktop su questa GPU
  [[ ${GPU_ADRENO:-} =~ ^8[0-9][0-9]$ ]] || return 1
  [[ $1 == kgsl ]] && return 0
  [[ $1 == zink-turnip ]] && ! gpu_glamor
}

# Server X con glamor (X11_GLAMOR=1, Termux:X11 con la composizione sulla GPU): la presentazione Vulkan
# diretta (DRI3) di zink-turnip è pulita anche con Chromium ed Electron (30 ottobre, Adreno 840: nessun
# blocco nero né fotogramma rovinato con menu e popup) ed è molto più fluida della copia software
# (mirino di CADCraft: 42 → 92 aggiornamenti al secondo, ritardo 33 → 17 ms). kgsl non è stato riprovato.
gpu_glamor() { x11_glamor_ok; }   # la composizione con la GPU nel server X è davvero accesa

gpu_rank() { # posizione nella preferenza per il desktop (più basso = meglio)
  if gpu_glitchy "$1"; then
    echo $((${GPU_RANK[$1]:-50} + 20))
  elif [[ ${GPU_ADRENO:-} =~ ^8[0-9][0-9]$ ]] && { { gpu_glamor && [[ $1 == zink-turnip ]]; } ||
    { ! gpu_glamor && [[ $1 == zink-turnip-sw ]]; }; }; then
    echo 0
  else
    echo "${GPU_RANK[$1]:-50}"
  fi
}

gpu_candidates() {
  local -a c=()
  if [[ $GPU_FAMILY == adreno ]]; then
    if [[ $(gpu_stack) == lfdevs ]]; then c+=(kgsl); fi
    if gpu_icd freedreno >/dev/null; then c+=(zink-turnip zink-turnip-sw); fi
  fi
  if gpu_icd wrapper >/dev/null; then c+=(zink-wrapper); fi
  if have virgl_test_server_android; then c+=(virgl virgl-angle); fi
  c+=(llvmpipe)
  printf '%s\n' "${c[@]}"
}

gpu_probe_all() { # [--bench]  → risultati in $TOD_GPU_PROBE (profilo|ok/ko|renderer|punteggio)
  local bench=${1:-} p r s
  local -a cands=()
  mapfile -t cands < <(gpu_candidates)
  if [[ $bench != --bench ]]; then
    # senza benchmark vince il primo che funziona: si provano in ordine di preferenza (gpu_rank), sennò su
    # Adreno 8xx con lo stack lfdevs vinceva sempre kgsl, che sporca il desktop (30 ottobre)
    mapfile -t cands < <(for p in "${cands[@]}"; do printf '%s %s\n' "$(gpu_rank "$p")" "$p"; done |
      sort -s -n -k1,1 | cut -d' ' -f2)
  fi
  : >"$TOD_GPU_PROBE"
  local k=0
  for p in "${cands[@]}"; do
    k=$((k + 1))
    progress_note "$((k * 90 / ${#cands[@]}))" "$(t tui_gpu_test "$p")"
    if r=$(gpu_probe_one "$p"); then
      s=""
      if [[ $bench == --bench && $p != llvmpipe ]]; then s=$(gpu_bench_one "$p"); fi
      printf '%s|ok|%s|%s\n' "$p" "$r" "$s" >>"$TOD_GPU_PROBE"
      # senza benchmark basta il primo che funziona (l'ordine è già per qualità)
      if [[ $bench != --bench ]]; then break; fi
    else
      printf '%s|ko||\n' "$p" >>"$TOD_GPU_PROBE"
    fi
  done
}

gpu_pick_best() { # dal file dei risultati → profilo migliore
  local file=${1:-$TOD_GPU_PROBE} best="" bestrank=99 bestscore=0 p st r s rank
  [[ -f $file ]] || { echo llvmpipe; return 0; }
  while IFS='|' read -r p st r s; do
    [[ -n $p && $st == ok ]] || continue
    rank=$(gpu_rank "$p")
    # col benchmark vince il punteggio; un profilo che sporca il desktop vale il minimo
    if [[ $s =~ ^[0-9]+$ ]] && (( s > 0 )) && gpu_glitchy "$p"; then s=1; fi
    if [[ $s =~ ^[0-9]+$ ]] && (( s > 0 )); then
      if (( s > bestscore )); then best=$p bestscore=$s bestrank=0; fi
    elif (( bestscore == 0 && rank < bestrank )); then
      best=$p bestrank=$rank
    fi
  done <"$file"
  printf '%s' "${best:-llvmpipe}"
}

gpu_score_of() { # gpu_score_of profilo [file] → punteggio registrato (0 se assente)
  local s
  s=$(awk -F'|' -v p="$1" '$1 == p && $2 == "ok" {print $4}' "${2:-$TOD_GPU_PROBE}" 2>/dev/null | sed -n 1p) || s=""
  num "$s"
}

# gpu_setup [--bench] — installa, prova, sceglie. Pensato per run_step (non interattivo).
gpu_setup() {
  local bench=${1:-} best_off best off_file="$TOD_STATE_DIR/gpu-probe.official.txt"
  gpu_install_base || return 1
  if [[ $(gpu_stack) == lfdevs ]]; then gpu_restore_official || true; fi
  x11_with_server gpu_probe_all "$bench" || return 1
  cp -f "$TOD_GPU_PROBE" "$off_file"
  best_off=$(gpu_pick_best "$off_file")
  best=$best_off
  log "gpu: migliore con stack official = $best_off"

  if [[ $GPU_FAMILY == adreno && $(conf_get GPU_EXPERIMENTAL yes) == yes ]]; then
    if gpu_install_lfdevs; then
      : >"$TOD_GPU_PROBE"   # niente decisioni su risultati del test precedente
      if x11_with_server gpu_probe_all "$bench"; then
        best=$(gpu_pick_best)
      else
        warn "$(t gpu_lfdevs_x11)"
        best=llvmpipe        # equivale a "peggiore": si torna ai driver ufficiali
      fi
      log "gpu: migliore con stack lfdevs = $best"
      # resta solo se è strettamente migliore: a parità i driver ufficiali sono più curati
      local worse=0
      if [[ $bench == --bench ]]; then
        if (( $(gpu_score_of "$best") <= $(gpu_score_of "$best_off" "$off_file") )); then worse=1; fi
      elif (( $(gpu_rank "$best") >= $(gpu_rank "$best_off") )); then
        worse=1
      fi
      if (( worse )); then
        log "gpu: lo stack sperimentale non migliora, torno a quello ufficiale"
        gpu_restore_official || return 1
        cp -f "$off_file" "$TOD_GPU_PROBE"
        best=$best_off
      fi
    else
      warn "$(t gpu_lfdevs_fail)"
    fi
  fi

  # Ultima spiaggia prima del software: Zink sul driver Vulkan di Android
  if [[ $best == llvmpipe || $best == virgl* ]] && pkg_has vulkan-wrapper-android && ! pkg_installed vulkan-wrapper-android; then
    if apt_run "${APT_FLAGS[@]}" install --no-remove vulkan-wrapper-android; then
      : >"$TOD_GPU_PROBE"
      x11_with_server gpu_probe_all "$bench" || true
      local w; w=$(gpu_pick_best)
      if (( $(gpu_rank "$w") < $(gpu_rank "$best") )); then best=$w; fi
    fi
  fi
  gpu_write_env "$best"
  log "gpu: profilo scelto = $best"
}

gpu_reprobe() { # nuovo test senza reinstallare nulla
  x11_with_server gpu_probe_all "${1:-}" || return 1
  gpu_write_env "$(gpu_pick_best)"
}

gpu_show() {
  local p st r s cur
  cur=$(conf_get GPU_PROFILE "?")
  printf '%s%s%s  stack=%s  %s=%s\n' "$C_BOLD" "$(t gpu_title)" "$C_0" "$(gpu_stack)" "$(t gpu_active)" "$cur"
  if [[ -f $TOD_GPU_PROBE ]]; then
    while IFS='|' read -r p st r s; do
      if [[ $st == ok ]]; then
        printf '  %s✔%s %-15s %s%s\n' "$C_G" "$C_0" "$p" "$r" "${s:+  (glmark2: $s)}"
      else
        printf '  %s✘%s %-15s\n' "$C_R" "$C_0" "$p"
      fi
    done <"$TOD_GPU_PROBE"
  fi
}

gpu_cli() { # tod gpu [status|probe [--bench]|bench|use <profilo>|stack official|lfdevs|run <profilo> -- cmd]
  local sub=${1:-status}
  if (($#)); then shift; fi
  detect_device
  case $sub in
    status | "") gpu_show ;;
    probe) run_step "$(t s_gpu_probe)" gpu_reprobe "${1:-}" && gpu_show ;;
    bench) run_step "$(t s_gpu_bench)" gpu_reprobe --bench && gpu_show ;;
    use)
      local p=${1:-}
      [[ -n $p && -n ${GPU_RANK[$p]:-} ]] || die "$(t gpu_unknown_profile "$p" "${!GPU_RANK[*]}")"
      gpu_write_env "$p" || die "$(t gpu_profile_unavailable "$p")"
      ok "$(t gpu_profile_set "$p")"
      ;;
    stack)
      case ${1:-} in
        official) run_step "$(t s_gpu_official)" gpu_restore_official && run_step "$(t s_gpu_probe)" gpu_reprobe ;;
        lfdevs) run_step "$(t s_gpu_lfdevs)" gpu_install_lfdevs && run_step "$(t s_gpu_probe)" gpu_reprobe ;;
        *) die "tod gpu stack official|lfdevs" ;;
      esac
      gpu_show
      ;;
    run) # tod gpu run <profilo> -- comando...   (un'app con un profilo diverso dal desktop)
      local p=${1:-} spec
      shift || true
      if [[ ${1:-} == -- ]]; then shift; fi
      spec=$(gpu_profile_env "$p") || die "$(t gpu_profile_unavailable "$p")"
      local -a envs=() clean=()
      mapfile -t envs <<<"$spec"
      mapfile -t clean < <(gpu_unset_args)
      if [[ $p == virgl* ]]; then virgl_start "$(virgl_modo_di "$p")" || true; fi
      exec env "${clean[@]}" "${envs[@]}" "$@"
      ;;
    *) die "tod gpu [status|probe|bench|use <profilo>|stack official|lfdevs|run <profilo> -- cmd]" ;;
  esac
}
