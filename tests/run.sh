#!/usr/bin/env bash
# =============================================================================
#  Test di Termux On DeX — girano su qualsiasi Linux (anche in CI), con mock di
#  getprop, adb, proot-distro, apt e un finto rootfs Debian.
#  Ogni test è in una subshell con `set -Eeuo pipefail`, come il codice vero:
#  un errore non gestito dentro una funzione fa fallire il test.
#
#  bash tests/run.sh            tutti i test
#  bash tests/run.sh gpu        solo quelli che contengono "gpu" nel nome
# =============================================================================
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
FILTER=${1:-}

export HOME="$WORK/home" PREFIX="$WORK/usr" TMPDIR="$WORK/usr/tmp" NO_COLOR=1
unset XDG_CONFIG_HOME XDG_STATE_HOME XDG_CACHE_HOME
mkdir -p "$HOME" "$PREFIX/bin" "$TMPDIR"

# --- isolamento dal sistema vero ---------------------------------------------
# I test possono girare sul telefono, nel proot accanto al desktop vero: lì
# pgrep/pkill vedono i processi di Termux (stesso UID) e il PATH può contenere
# i programmi veri di Termux (am, pulseaudio, termux-x11…). Un session_stop di
# prova ha chiuso così il desktop di Pietro. Quindi:
#  - via dal PATH ogni cartella di Termux: restano solo i mock dei test;
#  - pgrep/pkill vedono solo i processi nati dai test (TOD_TEST_RUN ereditata).
PATH=$(tr ':' '\n' <<<"$PATH" | grep -v '^/data/data/com\.termux/' | paste -sd:)
# strumenti innocui che nel proot esistono solo in Termux: questi sì
mkdir -p "$WORK/safe-bin"
for _t in curl jq python3 sha256sum unzip zip clang git; do
  if ! command -v "$_t" >/dev/null && [[ -x /data/data/com.termux/files/usr/bin/$_t ]]; then
    ln -s "/data/data/com.termux/files/usr/bin/$_t" "$WORK/safe-bin/$_t"
  fi
done
unset _t
PATH="$PATH:$WORK/safe-bin"
export PATH TOD_TEST_RUN=$WORK TOD_PM=false
unset DISPLAY PULSE_SERVER XDG_RUNTIME_DIR

_test_pids() { # pgrep vero, filtrato sui processi che hanno TOD_TEST_RUN=$WORK
  local p
  for p in $(command pgrep "$@" 2>/dev/null); do
    if tr '\0' '\n' <"/proc/$p/environ" 2>/dev/null | grep -qxF "TOD_TEST_RUN=$WORK"; then echo "$p"; fi
  done
}
pgrep() {
  local out
  out=$(_test_pids "$@")
  [[ -n $out ]] || return 1
  printf '%s\n' "$out"
}
pkill() {
  local sig=TERM p found=1
  if [[ ${1:-} =~ ^-([0-9]+|[A-Z]+)$ ]]; then sig=${1#-}; shift; fi
  for p in $(_test_pids "$@"); do kill -s "$sig" "$p" 2>/dev/null || kill "-$sig" "$p" 2>/dev/null || true; found=0; done
  return "$found"
}
export -f _test_pids pgrep pkill

getprop() {
  case $1 in
    persist.sys.locale) echo it-IT ;;
    ro.soc.model) echo SM8850 ;;
    ro.hardware.egl) echo adreno ;;
    *) echo "" ;;
  esac
}

TOD_ROOT=$ROOT
for m in core i18n detect pkgs x11 api usb stampa rete videochip gpu desktop session container diretto apt apps disinstalla games phantom shizuku lingua look files maint menu tui benvenuto installer; do
  # shellcheck source=/dev/null
  . "$ROOT/lib/$m.sh"
done
tod_init

fresh_env() { # HOME/PREFIX nuovi per ogni test
  export HOME="$WORK/h_$1" PREFIX="$WORK/p_$1"
  export TMPDIR="$PREFIX/tmp"
  mkdir -p "$HOME" "$PREFIX/bin" "$TMPDIR"
  TOD_CONF_DIR="$HOME/.config/tod" TOD_STATE_DIR="$HOME/.local/state/tod" TOD_CACHE_DIR="$HOME/.cache/tod"
  TOD_CONF="$TOD_CONF_DIR/tod.conf" TOD_GPU_ENV="$TOD_CONF_DIR/gpu.env" TOD_LOG="$TOD_STATE_DIR/tod.log"
  TOD_GPU_PROBE="$TOD_STATE_DIR/gpu-probe.txt" TOD_X11_APK="$TMPDIR/termux-x11.apk" TOD_X11_DEB="$TMPDIR/termux-x11-nightly.deb"
  LINGUA_SO="$PREFIX/lib/tod-gettext.so" LINGUA_DIR="$PREFIX/share/tod-locale"
  mkdir -p "$PREFIX/lib"
  TOD_YES=1
  tod_init
}

# --- asserzioni (ritornano 1 → con set -e il test fallisce) ------------------
eq() {
  if [[ $2 == "$3" ]]; then return 0; fi
  printf '  ✘ %s\n    atteso:   %q\n    ottenuto: %q\n' "$1" "$2" "$3"
  return 1
}
has() {
  if grep -qF -- "$2" <<<"$3"; then return 0; fi
  printf '  ✘ %s: manca %q in:\n%s\n' "$1" "$2" "$3"
  return 1
}
hasnt() {
  if ! grep -qF -- "$2" <<<"$3"; then return 0; fi
  printf '  ✘ %s: non doveva contenere %q:\n%s\n' "$1" "$2" "$3"
  return 1
}
fails() { # in una subshell: anche le funzioni che fanno die/exit si possono provare
  if ( "$@" ) >/dev/null 2>&1; then printf '  ✘ doveva fallire: %s\n' "$*"; return 1; fi
  return 0
}

# =============================================================================
test_i18n_keys_complete() {
  local k missing=""
  local keys
  keys=$(grep -ohE '(\$\(|&& |\|\| )t [a-z_0-9]+' "$ROOT"/tod "$ROOT"/lib/*.sh | awk '{print $NF}' | sort -u)
  for k in $keys; do
    [[ -n ${TOD_MSG_it[$k]-} ]] || missing+=" it:$k"
    [[ -n ${TOD_MSG_en[$k]-} ]] || missing+=" en:$k"
  done
  eq "chiavi usate nel codice ma non tradotte" "" "$missing"
}

test_i18n_same_keys_and_placeholders() {
  local k a b diffs=""
  eq "stesse chiavi it/en" "$(printf '%s\n' "${!TOD_MSG_it[@]}" | sort)" "$(printf '%s\n' "${!TOD_MSG_en[@]}" | sort)"
  for k in "${!TOD_MSG_it[@]}"; do
    a=$(awk -v s="${TOD_MSG_it[$k]}" 'BEGIN{print gsub(/%s/, "", s)}')
    b=$(awk -v s="${TOD_MSG_en[$k]-}" 'BEGIN{print gsub(/%s/, "", s)}')
    [[ $a == "$b" ]] || diffs+=" $k($a/$b)"
  done
  eq "numero di %s uguale in it/en" "" "$diffs"
}

test_i18n_t_and_lang() {
  TOD_LANG=it
  eq "it" "Desktop xfce" "$(t s_desktop xfce)"
  TOD_LANG=en
  eq "en" "xfce desktop" "$(t s_desktop xfce)"
  eq "chiave ignota → chiave" "chiave_che_non_esiste" "$(t chiave_che_non_esiste)"
  conf_set UI_LANG en
  i18n_init
  eq "UI_LANG forza la lingua" en "$TOD_LANG"
}

test_conf_roundtrip() {
  conf_set DE kde
  conf_set APPS "firefox libreoffice"
  conf_load
  eq "DE" kde "$(conf_get DE)"
  eq "valore con spazi" "firefox libreoffice" "$(conf_get APPS)"
  eq "predefinito" xyz "$(conf_get NON_ESISTE xyz)"
  conf_set DE xfce
  conf_load
  eq "sovrascrittura" xfce "$(conf_get DE)"
  eq "una riga per chiave" 2 "$(wc -l <"$TOD_CONF" | tr -d ' ')"
}

test_steps_resume() {
  fails step_done base
  step_mark base
  step_mark base
  step_done base
  eq "nessun duplicato" 1 "$(grep -c '^base$' "$TOD_STATE_DIR/steps")"
  step_reset
  fails step_done base
}

test_display_classify() {
  eq "S26U QHD+ orizzontale" "200 1 phone" "$(display_classify 3120 1440)"
  eq "S26U QHD+ verticale" "200 1 phone" "$(display_classify 1440 3120)"
  eq "S26U FHD+" "160 1 phone" "$(display_classify 2340 1080)"
  eq "monitor 1080p" "108 1 fhd" "$(display_classify 1920 1080)"
  eq "monitor 1440p" "120 1 qhd" "$(display_classify 2560 1440)"
  eq "ultrawide 3440x1440" "120 1 qhd" "$(display_classify 3440 1440)"
  eq "16:10 2560x1600" "144 1 hidpi" "$(display_classify 2560 1600)"
  eq "4K" "96 2 uhd" "$(display_classify 3840 2160)"
  eq "zero" "96 1 unknown" "$(display_classify 0 0)"
}

test_gpu_classify() {
  gpu_classify "Adreno840v2" adreno adreno SM8850 QTI
  eq "famiglia" adreno "$GPU_FAMILY"
  eq "modello" 840 "$GPU_ADRENO"
  eq "revisione" 2 "$GPU_REV"
  eq "etichetta" "Adreno 840 v2" "$GPU_LABEL"
  gpu_classify "" adreno "" SM8750 QTI
  eq "da SoC" 830 "$GPU_ADRENO"
  gpu_classify "Adreno (TM) 840" "" "" "" ""
  eq "nome con (TM)" 840 "$GPU_ADRENO"
  eq "senza revisione" "" "$GPU_REV"
  if [[ ! -e /dev/kgsl-3d0 ]]; then
    gpu_classify "" mali mali MT6989 Mediatek
    eq "mali" mali "$GPU_FAMILY"
    gpu_classify "" "" "" S5E9965 Samsung
    eq "exynos" xclipse "$GPU_FAMILY"
  fi
  eq "One UI" 8.5 "$(oneui_version 80500)"
  eq "One UI vuota" "" "$(oneui_version "")"
}

test_x11_prefs_filter() {
  local list out
  list=$'"fullscreen"="false"\n"keepScreenOn"="true"\ndisplayResolutionMode=scaled\n"dexMetaKeyCapture": "false"'
  out=$(x11_prefs_filter "$list" fullscreen=true keepScreenOn=true displayResolutionMode=native \
    dexMetaKeyCapture=true chiaveInventata=1 hideCutout=true)
  eq "solo le chiavi esistenti" $'fullscreen=true\nkeepScreenOn=true\ndisplayResolutionMode=native\ndexMetaKeyCapture=true' "$out"
}

test_gpu_profiles_env() {
  mkdir -p "$PREFIX/share/vulkan/icd.d"
  touch "$PREFIX/share/vulkan/icd.d/freedreno_icd.aarch64.json"
  local out
  out=$(gpu_profile_env zink-turnip)
  has "ICD fissato" "VK_ICD_FILENAMES=$PREFIX/share/vulkan/icd.d/freedreno_icd.aarch64.json" "$out"
  has "zink" "MESA_LOADER_DRIVER_OVERRIDE=zink" "$out"
  hasnt "senza WSI sw" "MESA_VK_WSI_DEBUG" "$out"
  has "variante sw" "MESA_VK_WSI_DEBUG=sw" "$(gpu_profile_env zink-turnip-sw)"
  eq "kgsl" "MESA_LOADER_DRIVER_OVERRIDE=kgsl" "$(gpu_profile_env kgsl)"
  fails gpu_profile_env zink-wrapper
  fails gpu_profile_env inventato
  touch "$PREFIX/share/vulkan/icd.d/wrapper_icd.aarch64.json"
  has "wrapper" "wrapper_icd.aarch64.json" "$(gpu_profile_env zink-wrapper)"
}

test_gpu_env_write_export_reset() {
  conf_set VSYNC off
  gpu_write_env kgsl
  export MESA_VK_WSI_DEBUG=sw GALLIUM_DRIVER=zink   # avanzi di un altro profilo
  gpu_env_export
  eq "profilo" kgsl "${TOD_GPU_PROFILE:-}"
  eq "override" kgsl "${MESA_LOADER_DRIVER_OVERRIDE:-}"
  eq "vsync off" 0 "${vblank_mode:-}"
  eq "avanzi ripuliti (WSI)" "" "${MESA_VK_WSI_DEBUG:-}"
  eq "avanzi ripuliti (gallium)" "" "${GALLIUM_DRIVER:-}"
  eq "config" kgsl "$(conf_get GPU_PROFILE)"
  conf_set VSYNC tearfree
  gpu_write_env llvmpipe
  has "tearfree" "MESA_VK_WSI_PRESENT_MODE=mailbox" "$(cat "$TOD_GPU_ENV")"
  local -a clean=()
  mapfile -t clean < <(gpu_unset_args)
  eq "coppie -u VAR" "-u MESA_LOADER_DRIVER_OVERRIDE" "${clean[0]} ${clean[1]}"
  eq "env -u funziona" "" "$(MESA_LOADER_DRIVER_OVERRIDE=x env "${clean[@]}" printenv MESA_LOADER_DRIVER_OVERRIDE || true)"
}

test_gpu_adreno8xx_avoids_glitchy_profiles() {
  mkdir -p "$TOD_STATE_DIR"
  GPU_ADRENO=840
  printf '%s\n' 'kgsl|ok|Adreno (TM) 840|' 'zink-turnip|ok|zink|' 'zink-turnip-sw|ok|zink|' 'llvmpipe|ok|llvmpipe|' >"$TOD_GPU_PROBE"
  eq "840: per rango vince la copia software" zink-turnip-sw "$(gpu_pick_best)"
  printf '%s\n' 'kgsl|ok|FD840|4753' 'zink-turnip|ok|zink|1434' 'zink-turnip-sw|ok|zink|897' 'llvmpipe|ok|llvmpipe|500' >"$TOD_GPU_PROBE"
  eq "840: il punteggio non premia chi sporca il desktop" zink-turnip-sw "$(gpu_pick_best)"
  printf '%s\n' 'kgsl|ok|FD840|' 'llvmpipe|ok|llvmpipe|' >"$TOD_GPU_PROBE"
  eq "840: meglio software pulito che GPU con i blocchi neri" llvmpipe "$(gpu_pick_best)"
  GPU_ADRENO=740
  printf '%s\n' 'kgsl|ok|FD740|' 'zink-turnip-sw|ok|zink|' >"$TOD_GPU_PROBE"
  eq "740: nessun cambiamento" kgsl "$(gpu_pick_best)"
  GPU_ADRENO=840
  eq "container 840: env zink-sw" "MESA_VK_WSI_DEBUG=sw" "$(cx_profile_env zink-sw | grep WSI)"
  hasnt "container: zink normale senza WSI software" "WSI" "$(cx_profile_env zink)"
}

test_gpu_adreno8xx_glamor_prefers_dri3() { # 30 ottobre: con glamor nel server X zink diretto è pulito e più fluido
  mkdir -p "$TOD_STATE_DIR"
  GPU_ADRENO=840
  conf_set X11_GLAMOR 1
  printf '%s\n' 'kgsl|ok|Adreno (TM) 840|' 'zink-turnip|ok|zink|' 'zink-turnip-sw|ok|zink|' 'llvmpipe|ok|llvmpipe|' >"$TOD_GPU_PROBE"
  eq "840 con glamor: vince la presentazione diretta" zink-turnip "$(gpu_pick_best)"
  eq "con glamor zink-turnip non sporca il desktop" no "$(if gpu_glitchy zink-turnip; then echo si; else echo no; fi)"
  eq "kgsl resta da evitare anche con glamor (non riprovato)" si "$(if gpu_glitchy kgsl; then echo si; else echo no; fi)"
  printf '%s\n' 'kgsl|ok|FD840|' 'zink-turnip-sw|ok|zink|' 'llvmpipe|ok|llvmpipe|' >"$TOD_GPU_PROBE"
  eq "840 con glamor senza zink-turnip: la copia software va ancora" zink-turnip-sw "$(gpu_pick_best)"
  conf_set X11_GLAMOR 0
  printf '%s\n' 'zink-turnip|ok|zink|' 'zink-turnip-sw|ok|zink|' >"$TOD_GPU_PROBE"
  eq "840 senza glamor: come prima" zink-turnip-sw "$(gpu_pick_best)"
}

test_gpu_pick_best() {
  mkdir -p "$TOD_STATE_DIR"
  printf '%s\n' 'kgsl|ko||' 'zink-turnip|ok|zink Vulkan 1.4(Turnip Adreno (TM) 840)|' 'llvmpipe|ok|llvmpipe|' >"$TOD_GPU_PROBE"
  eq "per rango" zink-turnip "$(gpu_pick_best)"
  printf '%s\n' 'kgsl|ok|FD840|900' 'zink-turnip|ok|zink|1400' 'virgl|ok|virgl|300' >"$TOD_GPU_PROBE"
  eq "per punteggio" zink-turnip "$(gpu_pick_best)"
  eq "punteggio registrato" 900 "$(gpu_score_of kgsl)"
  eq "punteggio assente" 0 "$(gpu_score_of virgl-inventato)"
  : >"$TOD_GPU_PROBE"
  eq "niente funziona" llvmpipe "$(gpu_pick_best)"
  rm -f "$TOD_GPU_PROBE"
  eq "file assente" llvmpipe "$(gpu_pick_best)"
}

test_gpu_candidates() {
  GPU_FAMILY=adreno
  mkdir -p "$PREFIX/share/vulkan/icd.d"
  touch "$PREFIX/share/vulkan/icd.d/freedreno_icd.aarch64.json"
  printf '#!/bin/sh\n' >"$PREFIX/bin/virgl_test_server_android"
  chmod +x "$PREFIX/bin/virgl_test_server_android"
  PATH="$PREFIX/bin:$PATH"
  gpu_set_stack official
  eq "official" "zink-turnip zink-turnip-sw virgl llvmpipe" "$(gpu_candidates | tr '\n' ' ' | sed 's/ $//')"
  gpu_set_stack lfdevs
  eq "lfdevs: kgsl per primo" "kgsl zink-turnip zink-turnip-sw virgl llvmpipe" "$(gpu_candidates | tr '\n' ' ' | sed 's/ $//')"
  GPU_FAMILY=mali
  eq "mali" "virgl llvmpipe" "$(gpu_candidates | tr '\n' ' ' | sed 's/ $//')"
}

# --- logica di gpu_setup con driver finti -------------------------------------
_gpu_mocks() { # $1 = risultato official, $2 = risultato lfdevs (righe probe separate da ;)
  OFF_RES=$1 LF_RES=$2
  mkdir -p "$PREFIX/share/vulkan/icd.d"
  touch "$PREFIX/share/vulkan/icd.d/freedreno_icd.aarch64.json"
  GPU_FAMILY=adreno
  gpu_install_base() { return 0; }
  x11_with_server() { "$@"; }
  gpu_install_lfdevs() { gpu_set_stack lfdevs; }
  gpu_restore_official() { gpu_set_stack official; }
  pkg_has() { return 1; }
  gpu_probe_all() {
    if [[ $(gpu_stack) == lfdevs ]]; then tr ';' '\n' <<<"$LF_RES" >"$TOD_GPU_PROBE"
    else tr ';' '\n' <<<"$OFF_RES" >"$TOD_GPU_PROBE"; fi
  }
}

test_gpu_setup_keeps_kgsl_when_better() {
  _gpu_mocks 'zink-turnip|ok|zink|' 'kgsl|ok|FD840|'
  gpu_setup
  eq "profilo" kgsl "$(conf_get GPU_PROFILE)"
  eq "stack" lfdevs "$(gpu_stack)"
}

test_gpu_setup_reverts_when_not_better() {
  _gpu_mocks 'zink-turnip|ok|zink|' 'kgsl|ko||;zink-turnip|ok|zink|'
  gpu_setup
  eq "profilo" zink-turnip "$(conf_get GPU_PROFILE)"
  eq "torna ai driver ufficiali" official "$(gpu_stack)"
  eq "risultati ripristinati" 'zink-turnip|ok|zink|' "$(cat "$TOD_GPU_PROBE")"
}

test_gpu_setup_respects_no_experimental() {
  _gpu_mocks 'zink-turnip|ok|zink|' 'kgsl|ok|FD840|'
  conf_set GPU_EXPERIMENTAL no
  gpu_setup
  eq "profilo" zink-turnip "$(conf_get GPU_PROFILE)"
  eq "stack" official "$(gpu_stack)"
}

test_gpu_setup_bench_compares_scores() {
  _gpu_mocks 'zink-turnip|ok|zink|1500' 'kgsl|ok|FD840|1200;zink-turnip|ok|zink|1100'
  gpu_setup --bench
  eq "vince il punteggio ufficiale" zink-turnip "$(conf_get GPU_PROFILE)"
  eq "stack" official "$(gpu_stack)"
}

test_gpu_setup_wrapper_last_resort() {
  _gpu_mocks 'llvmpipe|ok|llvmpipe|' 'kgsl|ko||;llvmpipe|ok|llvmpipe|'
  pkg_has() { [[ $1 == vulkan-wrapper-android ]]; }
  pkg_installed() { return 1; }
  apt-get() { touch "$PREFIX/share/vulkan/icd.d/wrapper_icd.aarch64.json"; WRAPPED=1; }
  gpu_probe_all() {
    if [[ -n ${WRAPPED:-} ]]; then echo 'zink-wrapper|ok|zink Vulkan (Adreno (TM) 840 (QUALCOMM_PROPRIETARY))|' >"$TOD_GPU_PROBE"
    else echo 'llvmpipe|ok|llvmpipe|' >"$TOD_GPU_PROBE"; fi
  }
  gpu_setup
  eq "Zink sul driver Qualcomm" zink-wrapper "$(conf_get GPU_PROFILE)"
}

test_gpu_probe_all_adreno8xx_order() { # 30 ottobre: senza benchmark con lfdevs vinceva sempre kgsl (sporca il desktop)
  mkdir -p "$TOD_STATE_DIR"
  GPU_FAMILY=adreno
  GPU_ADRENO=840
  gpu_stack() { echo lfdevs; }
  gpu_icd() { echo /x/freedreno_icd.json; }
  have() { [[ $1 != virgl_test_server_android ]] && command -v "$1" >/dev/null 2>&1; }
  gpu_probe_one() { echo "renderer $1"; }
  conf_set X11_GLAMOR 0
  gpu_probe_all
  eq "840 senza glamor: provato e scelto per primo zink-turnip-sw" "zink-turnip-sw|ok|renderer zink-turnip-sw|" "$(sed -n 1p "$TOD_GPU_PROBE")"
  eq "840 senza glamor: scelta" zink-turnip-sw "$(gpu_pick_best)"
  conf_set X11_GLAMOR 1
  gpu_probe_all
  eq "840 con glamor: scelta" zink-turnip "$(gpu_pick_best)"
  GPU_ADRENO=740
  gpu_probe_all
  eq "740: ordine di prima, vince kgsl" kgsl "$(gpu_pick_best)"
}

test_gpu_probe_one_rejects_fallbacks() {
  mkdir -p "$PREFIX/share/vulkan/icd.d"
  touch "$PREFIX/share/vulkan/icd.d/freedreno_icd.aarch64.json"
  # vulkaninfo/glxinfo finti come *programmi*: gpu_vk_ok li lancia tramite timeout(1)
  cat >"$PREFIX/bin/vulkaninfo" <<'EOF'
#!/bin/sh
if [ -f "$HOME/vk_fail" ]; then echo "TU: error: device (chip_id = 44050A21, gpu_id = 6860) is unsupported"; exit 1; fi
echo "deviceName = Adreno (TM) 840"
EOF
  chmod +x "$PREFIX/bin/vulkaninfo"
  # glxinfo finto: con kgsl risponde Freedreno, con zink ricade su llvmpipe
  cat >"$PREFIX/bin/glxinfo" <<'EOF'
#!/bin/sh
case "${MESA_LOADER_DRIVER_OVERRIDE:-}" in
  kgsl) echo "OpenGL renderer string: FD840" ;;
  zink) echo "OpenGL renderer string: llvmpipe (LLVM 19, 128 bits)" ;;
  *) echo "OpenGL renderer string: llvmpipe (LLVM 19, 128 bits)" ;;
esac
EOF
  chmod +x "$PREFIX/bin/glxinfo"
  PATH="$PREFIX/bin:$PATH"
  eq "kgsl ok" FD840 "$(gpu_probe_one kgsl)"
  fails gpu_probe_one zink-turnip
  eq "llvmpipe ammesso solo come software" "llvmpipe (LLVM 19, 128 bits)" "$(gpu_probe_one llvmpipe)"
  # chip non supportato (Adreno 840v2 con Turnip ufficiale)
  touch "$HOME/vk_fail"
  fails gpu_probe_one zink-turnip
  has "motivo nel log" "unsupported" "$(cat "$TOD_LOG")"
}

test_virgl_profile_needs_server_flag() {
  has "flag virgl" "TOD_NEEDS_VIRGL=1" "$(gpu_profile_env virgl)"
}

# --- rete e GitHub --------------------------------------------------------------
test_download_checksum() {
  echo ciao >"$WORK/sorgente.txt"
  local sha
  sha=$(sha256sum "$WORK/sorgente.txt" | cut -d' ' -f1)
  net_download "file://$WORK/sorgente.txt" "$TOD_CACHE_DIR/x/a.txt" "$sha"
  eq "contenuto" ciao "$(cat "$TOD_CACHE_DIR/x/a.txt")"
  sha256_ok "$TOD_CACHE_DIR/x/a.txt" "${sha^^}"   # maiuscole accettate
  fails net_download "file://$WORK/sorgente.txt" "$TOD_CACHE_DIR/x/b.txt" "$(printf '0%.0s' {1..64})"
  eq "file con checksum errato scartato" no "$([[ -e $TOD_CACHE_DIR/x/b.txt ]] && echo si || echo no)"
}

test_github_release_parsing() {
  if ! command -v jq >/dev/null; then echo "  (jq assente: salto)"; return 0; fi
  cat >"$WORK/rel.json" <<'EOF'
[
 {"tag_name":"turnip-weekly","draft":false,"published_at":"2026-10-01T00:00:00Z","body":"","assets":[]},
 {"tag_name":"freedreno-26.1.0-devel","draft":false,"published_at":"2026-05-01T00:00:00Z","body":"","assets":[]},
 {"tag_name":"freedreno-26.2.0-devel-20260709","draft":false,"published_at":"2026-07-12T18:01:00Z",
  "body":"## Checksums\r\n```\r\nf22d1258f28d71f38ce4e39e67f251565f1f1df57ccb4c046698e6e853aa5aca  mesa-vulkan-icd-freedreno_26.2.0-1_aarch64.deb\r\n5adfa7b3000bdce3ff660afb40e069e95508b0ab5c54c29dd9732111a548cfeb  mesa_26.2.0-1_aarch64.deb\r\n```",
  "assets":[
   {"name":"mesa_26.2.0-1_aarch64.deb","browser_download_url":"https://example.invalid/mesa.deb","digest":null},
   {"name":"mesa-vulkan-icd-freedreno_26.2.0-1_aarch64.deb","browser_download_url":"https://example.invalid/icd.deb","digest":"sha256:abc123"}]},
 {"tag_name":"freedreno-99-draft","draft":true,"published_at":"2026-12-01T00:00:00Z","body":"","assets":[]}
]
EOF
  net_get() { cat "$WORK/rel.json"; }
  local rel url sha name
  rel=$(gh_release lfdevs/termux-packages freedreno-)
  eq "release più recente (no draft, no altri prefissi)" freedreno-26.2.0-devel-20260709 "$(jq -r .tag_name <<<"$rel")"
  IFS='|' read -r url sha name <<<"$(gh_asset "$rel" '^mesa_.*_aarch64\.deb$')"
  eq "url" https://example.invalid/mesa.deb "$url"
  eq "digest assente → vuoto" "" "$sha"
  eq "nome" mesa_26.2.0-1_aarch64.deb "$name"
  eq "sha dalle note" 5adfa7b3000bdce3ff660afb40e069e95508b0ab5c54c29dd9732111a548cfeb "$(gh_body_sha "$rel" "$name")"
  IFS='|' read -r url sha name <<<"$(gh_asset "$rel" '^mesa-vulkan-icd-freedreno_.*\.deb$')"
  eq "digest di GitHub" abc123 "$sha"
  eq "nessun asset" "" "$(gh_asset "$rel" 'inesistente')"
  fails gh_release lfdevs/termux-packages prefisso-inesistente-
}

# --- container ------------------------------------------------------------------
_fake_rootfs() {
  R="$PREFIX/var/lib/proot-distro/installed-rootfs/debian"
  mkdir -p "$R/etc" "$R/usr/lib" "$R/usr/share/applications" "$R/usr/share/icons/hicolor/48x48/apps" \
    "$R/usr/share/pixmaps" "$R/var/lib/dpkg/info"
  printf 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\nVERSION_CODENAME=trixie\n' >"$R/usr/lib/os-release"
  ln -sf ../usr/lib/os-release "$R/etc/os-release"
  cat >"$R/usr/share/applications/libreoffice-writer.desktop" <<'EOF'
[Desktop Entry]
Name=LibreOffice Writer
TryExec=libreoffice
Exec=libreoffice --writer %U
Icon=libreoffice-writer
Type=Application
Actions=NewDocument;

[Desktop Action NewDocument]
Name=New Document
Exec=libreoffice --writer
EOF
  printf '[Desktop Entry]\nName=Hidden\nExec=x\nNoDisplay=true\n' >"$R/usr/share/applications/hidden.desktop"
  printf '[Desktop Entry]\nName=Lo\nExec=lo\nIcon=lo\n' >"$R/usr/share/applications/lo.desktop"
  echo PNG >"$R/usr/share/icons/hicolor/48x48/apps/real.png"
  ln -sf /usr/share/icons/hicolor/48x48/apps/real.png "$R/usr/share/icons/hicolor/48x48/apps/libreoffice-writer.png"
  echo XPM >"$R/usr/share/pixmaps/lo.xpm"
  printf '%s\n' /usr/share/applications/libreoffice-writer.desktop /usr/share/applications/hidden.desktop \
    /usr/share/icons/hicolor/48x48/apps/libreoffice-writer.png /usr/bin/libreoffice >"$R/var/lib/dpkg/info/libreoffice-writer.list"
  printf '%s\n' /usr/share/applications/lo.desktop /usr/share/pixmaps/lo.xpm >"$R/var/lib/dpkg/info/lo:arm64.list"
  printf 'Package: libreoffice-writer\nStatus: install ok installed\n\nPackage: lo\nStatus: install ok installed\n\nPackage: rimosso\nStatus: deinstall ok config-files\n' \
    >"$R/var/lib/dpkg/status"
}

test_cx_detection_and_suffix() {
  fails cx_installed
  _fake_rootfs
  cx_installed
  eq "os-release (link relativo)" "debian trixie" "$(cx_os)"
  eq "trixie" debian_trixie_arm64 "$(cx_mesa_suffix debian trixie)"
  eq "noble" ubuntu_noble_arm64 "$(cx_mesa_suffix ubuntu noble)"
  fails cx_mesa_suffix debian bookworm
  eq "pacchetti installati" $'libreoffice-writer\nlo' "$(cx_pkg_list)"
  eq "locale da Android" it_IT.UTF-8 "$(cx_locale)"
}

test_cx_export_and_prune() {
  _fake_rootfs
  local apps="$HOME/.local/share/applications" d
  cx_export_pkgs lo libreoffice-writer
  d=$(cat "$apps/tod-cx-libreoffice-writer.desktop")
  has "Exec riscritto" "Exec=$PREFIX/bin/tod cx run -- libreoffice --writer %U" "$d"
  has "azione riscritta" "Exec=$PREFIX/bin/tod cx run -- libreoffice --writer" "$d"
  hasnt "TryExec tolto" "TryExec" "$d"
  eq "marcatore subito dopo [Desktop Entry]" "X-TOD-Container=debian" "$(sed -n 2p "$apps/tod-cx-libreoffice-writer.desktop")"
  eq "voce nascosta non esportata" no "$([[ -e $apps/tod-cx-hidden.desktop ]] && echo si || echo no)"
  eq "icona (link assoluto risolto)" PNG "$(cat "$HOME/.local/share/icons/hicolor/48x48/apps/libreoffice-writer.png")"
  has "pixmap → percorso assoluto" "Icon=$HOME/.local/share/pixmaps/lo.xpm" "$(cat "$apps/tod-cx-lo.desktop")"
  # rimozione: il pacchetto sparisce dallo status → spariscono le sue voci
  sed -i 's/^Package: lo$/Package: lo-rimosso/' "$R/var/lib/dpkg/status"
  cx_prune_exports
  eq "voce rimossa" no "$([[ -e $apps/tod-cx-lo.desktop ]] && echo si || echo no)"
  eq "l'altra resta" si "$([[ -e $apps/tod-cx-libreoffice-writer.desktop ]] && echo si || echo no)"
}

test_cx_gpu_probe_choice() {
  _fake_rootfs
  GPU_FAMILY=adreno
  conf_set CX_MESA_TAG mesa-26.2.0-devel-20260709
  have() { [[ $1 != virgl_test_server_android ]] && command -v "$1" >/dev/null 2>&1; }
  cx_run() { # cx_run --raw env VAR=... timeout 30 glxinfo -B
    local a o=""
    for a in "$@"; do case $a in MESA_LOADER_DRIVER_OVERRIDE=*) o=${a#*=} ;; esac; done
    case $o in
      kgsl) echo "${KGSL_OUT:-OpenGL renderer string: FD840}" ;;
      zink) echo "OpenGL renderer string: zink Vulkan 1.4(Turnip Adreno (TM) 840 (MESA_TURNIP))" ;;
      *) echo "OpenGL renderer string: llvmpipe (LLVM 19)" ;;
    esac
  }
  cx_gpu_probe
  eq "kgsl nel container" kgsl "$(conf_get CX_GPU_PROFILE)"
  eq "gpu.env del container" MESA_LOADER_DRIVER_OVERRIDE=kgsl "$(cat "$R/etc/tod/gpu.env")"
  KGSL_OUT="OpenGL renderer string: llvmpipe (LLVM 19)"
  cx_gpu_probe
  eq "ripiego su zink" zink "$(conf_get CX_GPU_PROFILE)"
}

test_cx_gpu_probe_glamor_adreno8xx() { # con glamor il container passa a zink diretto (DRI3), senza resta zink-sw
  _fake_rootfs
  GPU_FAMILY=adreno
  GPU_ADRENO=840
  conf_set CX_MESA_TAG mesa-26.2.0-devel-20260709
  have() { [[ $1 != virgl_test_server_android ]] && command -v "$1" >/dev/null 2>&1; }
  cx_run() {
    local a o=""
    for a in "$@"; do case $a in MESA_LOADER_DRIVER_OVERRIDE=*) o=${a#*=} ;; esac; done
    case $o in
      kgsl) echo "OpenGL renderer string: FD840" ;;
      zink) echo "OpenGL renderer string: zink Vulkan 1.4(Turnip Adreno (TM) 840 (MESA_TURNIP))" ;;
      *) echo "OpenGL renderer string: llvmpipe (LLVM 19)" ;;
    esac
  }
  conf_set X11_GLAMOR 0
  cx_gpu_probe
  eq "senza glamor: copia software" zink-sw "$(conf_get CX_GPU_PROFILE)"
  has "senza glamor: WSI software" MESA_VK_WSI_DEBUG=sw "$(cat "$R/etc/tod/gpu.env")"
  conf_set X11_GLAMOR 1
  cx_gpu_probe
  eq "con glamor: zink diretto" zink "$(conf_get CX_GPU_PROFILE)"
  hasnt "con glamor: niente WSI software" MESA_VK_WSI_DEBUG "$(cat "$R/etc/tod/gpu.env")"
}

test_cx_aiuti_zenity() { # 31 ottobre: senza zenity le app Rust (rfd, PdfCraft) non aprivano la finestra dei file
  has "zenity tra gli strumenti del container" zenity "$CX_AIUTI"
  cx_root() { printf '%s\n' "$*" >"$WORK/cxa_args_$$"; cat >"$WORK/cxa_script_$$"; }
  cx_upgrade
  has "tod update: strumenti passati al container" zenity "$(cat "$WORK/cxa_args_$$")"
  has "tod update: installa quelli che mancano" 'install $manca' "$(cat "$WORK/cxa_script_$$")"
  # lo script vero, con dpkg e apt-get finti: zenity manca → si installa
  mkdir -p "$WORK/cxa_bin_$$"
  printf '#!/bin/sh\nexit 1\n' >"$WORK/cxa_bin_$$/dpkg"
  printf '#!/bin/sh\necho "apt-get $*" >>"%s"\n' "$WORK/cxa_apt_$$" >"$WORK/cxa_bin_$$/apt-get"
  chmod +x "$WORK/cxa_bin_$$/dpkg" "$WORK/cxa_bin_$$/apt-get"
  PATH="$WORK/cxa_bin_$$:$PATH" sh "$WORK/cxa_script_$$" "$CX_AIUTI"
  has "zenity installato" "install zenity" "$(cat "$WORK/cxa_apt_$$")"
}

test_cx_gpu_probe_virgl_mali() { # Mali: glxinfo con virpipe dà BadMatch, ma con una finestra vera virgl va
  _fake_rootfs
  GPU_FAMILY=mali
  conf_set CX_MESA_TAG ""
  have() { [[ $1 == virgl_test_server_android ]] || command -v "$1" >/dev/null 2>&1; }
  virgl_start() { return 0; }
  virgl_stop() { :; }
  cx_run() {
    local a v=0
    for a in "$@"; do [[ $a == GALLIUM_DRIVER=virpipe ]] && v=1; done
    if ((v)) && [[ " $* " == *" glxinfo "* ]]; then echo "X Error of failed request:  BadMatch"; return 1; fi
    if ((v)) && [[ " $* " == *" glxgears "* ]]; then echo "GL_RENDERER   = virgl (Mali-G77)"; return 124; fi
    echo "OpenGL renderer string: llvmpipe (LLVM 19)"
  }
  cx_gpu_probe
  eq "virgl scelto" virgl "$(conf_get CX_GPU_PROFILE)"
  has "percorso di virpipe forzato" LIBGL_ALWAYS_SOFTWARE=1 "$(cat "$R/etc/tod/gpu.env")"
}

test_cx_runner_script() {
  _fake_rootfs
  mkdir -p "$R/usr/bin"
  cx_install_runner
  local f="$R/usr/local/bin/tod-run"
  [[ -x $f ]]
  bash -n "$f"
  has "pulizia ambiente" "unset LD_PRELOAD LD_LIBRARY_PATH" "$(cat "$f")"
  # eseguito "fuori" dal container: deve almeno funzionare come sh POSIX
  eq "esegue il comando" ok "$(TOD_ETC=$R/etc/tod DISPLAY=:5 sh "$f" --raw sh -c 'echo ok')"
  eq "DISPLAY rispettato" :5 "$(TOD_ETC=$R/etc/tod DISPLAY=:5 sh "$f" --raw sh -c 'echo $DISPLAY')"
  eq "AppImage senza FUSE" 1 "$(TOD_ETC=$R/etc/tod sh "$f" --raw sh -c 'echo $APPIMAGE_EXTRACT_AND_RUN')"
  # pkexec: acceso di serie (le app grafiche che chiedono i permessi), spento solo se scelto
  local p="$R/usr/local/bin/pkexec"
  [[ -x $p ]]
  # ArtCraft Launcher 0.8.1 lo cerca solo in /usr/bin, /bin, /usr/sbin, /sbin (26 ottobre)
  # relativo: senza proot il kernel seguirebbe un /usr/local assoluto fuori dal container
  eq "anche in /usr/bin" ../local/bin/pkexec "$(readlink "$R/usr/bin/pkexec")"
  ln -sf /usr/local/bin/pkexec "$R/usr/bin/pkexec"
  cx_install_runner
  eq "il collegamento assoluto di prima si rifà" ../local/bin/pkexec "$(readlink "$R/usr/bin/pkexec")"
  conf_set CX_PKEXEC 0
  cx_install_runner
  eq "spento a richiesta: niente pkexec" no:no "$([[ -e $p ]] && echo si || echo no):$([[ -L $R/usr/bin/pkexec ]] && echo si || echo no)"
  printf '#!/bin/sh\n# polkit vero\n' >"$R/usr/bin/pkexec"
  conf_set CX_PKEXEC 1
  cx_install_runner
  has "un pkexec vero non si tocca" "polkit vero" "$(cat "$R/usr/bin/pkexec")"
  rm -f "$R/usr/bin/pkexec"
  [[ -x $p ]]
  sh -n "$p"
  eq "risponde a --version come polkit" "pkexec version 126" "$(sh "$p" --version)"
  mkdir -p "$WORK/fb_$$"
  printf '#!/bin/sh\nshift; [ "$1" = -- ] && shift; echo "sudo:$*"\n' >"$WORK/fb_$$/sudo"
  chmod +x "$WORK/fb_$$/sudo"
  eq "pkexec → sudo, opzioni di polkit tolte" "sudo:apt install ./x.deb" \
    "$(PATH="$WORK/fb_$$:$PATH" sh "$p" --disable-internal-agent apt install ./x.deb)"
  has "poi il menu" "tod-sync-menu" "$(cat "$p")"
  # ogni apt/dpkg del container aggiorna il menu Start (tranne quando lo fa già tod apt)
  has "menu: tod cx sync" "tod\" cx sync" "$(cat "$R/usr/local/bin/tod-sync-menu")"
  has "hook di apt" "tod-sync-menu" "$(cat "$R/etc/apt/apt.conf.d/99tod-menu")"
  eq "TOD_MENU_SYNC=0: niente doppio aggiornamento" "" "$(TOD_MENU_SYNC=0 PREFIX=/nessuno sh -x "$R/usr/local/bin/tod-sync-menu" 2>&1 | grep -c setsid | sed 's/^0$//')"
}

test_cx_sync_apps() {
  _fake_rootfs
  export TOD_TERMUX_HOME=$HOME
  mkdir -p "$R/var/lib/dpkg/info"
  printf '/usr\n/usr/share/applications/ai.storyteller.soundcraft.desktop\n' >"$R/var/lib/dpkg/info/soundcraft:arm64.list"
  printf '[Desktop Entry]\nName=SoundCraft\nExec=soundcraft %%F\nIcon=soundcraft\nStartupWMClass=soundcraft\n' >"$R/usr/share/applications/ai.storyteller.soundcraft.desktop"
  printf 'Package: soundcraft\nStatus: install ok installed\n\n' >>"$R/var/lib/dpkg/status"
  desktop_refresh_caches() { :; }
  cx_sync_apps
  has "app installata dal launcher: nel menu" "Exec=$PREFIX/bin/tod cx run -- soundcraft %F" \
    "$(cat "$HOME/.local/share/applications/tod-cx-ai.storyteller.soundcraft.desktop")"
  has "nome della finestra giusto (icona nella barra)" "StartupWMClass=ai.storyteller.soundcraft" \
    "$(cat "$HOME/.local/share/applications/tod-cx-ai.storyteller.soundcraft.desktop")"
  has "cursore di attesa" "StartupNotify=true" \
    "$(cat "$HOME/.local/share/applications/tod-cx-ai.storyteller.soundcraft.desktop")"
  # la voce sparita (tolto un AppImage con lo stesso id, 26 ottobre): si rifà anche se il pacchetto non è cambiato
  rm -f "$HOME/.local/share/applications/tod-cx-ai.storyteller.soundcraft.desktop"
  cx_sync_apps
  eq "voce cancellata: rifatta" si "$([[ -f $HOME/.local/share/applications/tod-cx-ai.storyteller.soundcraft.desktop ]] && echo si || echo no)"
  # app Electron: --no-sandbox in automatico (senza, in proot si chiudono subito)
  mkdir -p "$R/usr/lib/app-electron" "$R/usr/bin"
  : >"$R/usr/lib/app-electron/chrome-sandbox"
  ln -sf /usr/lib/app-electron/app-electron "$R/usr/bin/app-electron"
  printf '/usr/share/applications/org.esempio.App.desktop\n' >"$R/var/lib/dpkg/info/app-electron.list"
  printf '[Desktop Entry]\nName=App\nExec=app-electron %%U\n\n[Desktop Action NewChat]\nExec=app-electron "app://new"\n' \
    >"$R/usr/share/applications/org.esempio.App.desktop"
  printf 'Package: app-electron\nStatus: install ok installed\n\n' >>"$R/var/lib/dpkg/status"
  cx_sync_apps
  local cl="$HOME/.local/share/applications/tod-cx-org.esempio.App.desktop"
  eq "Electron: --no-sandbox su ogni Exec" 2 "$(grep -c "cx run -- app-electron --no-sandbox" "$cl")"
  cx_sync_apps
  eq "una volta sola" 0 "$(grep -c -- "--no-sandbox --no-sandbox" "$cl")"
  eq "le altre app no" 0 "$(grep -c -- "--no-sandbox" "$HOME/.local/share/applications/tod-cx-ai.storyteller.soundcraft.desktop")"
  # l'id di avvio arriva dentro il container (è lui che spegne il cursore di attesa)
  proot-distro() { printf '%s\n' "$@" >"$WORK/pd_$$"; }
  cx_installed() { return 0; }
  cx_startup_watch() { echo "watch:$1" >>"$WORK/pd_watch_$$"; }
  DESKTOP_STARTUP_ID=abc_TIME1 cx_run soundcraft
  has "DESKTOP_STARTUP_ID passato" "DESKTOP_STARTUP_ID=abc_TIME1" "$(cat "$WORK/pd_$$")"
  eq "e chi spegne il cursore alla prima finestra" "watch:abc_TIME1" "$(cat "$WORK/pd_watch_$$")"
}

test_apt_unified() { # un solo apt: prima Termux, poi il container; il menu si aggiorna
  pkg_has() { [[ $1 == gimp || $1 == htop ]]; }
  pkg_installed() { [[ $1 == htop ]]; }
  cx_installed() { return 0; }
  printf '%s\n' gparted gimp htop >"$WORK/names_$$"
  cx_pkgnames() { printf '%s' "$WORK/names_$$"; }
  cx_pkg_list() { printf '%s\n' gparted; }
  apt_native() { echo "termux:$*" >>"$WORK/apt_$$"; }
  cx_apt() { echo "debian:$*" >>"$WORK/apt_$$"; }
  cx_sync_apps() { echo "menu" >>"$WORK/apt_$$"; }
  cx_prune_exports() { echo "menu-" >>"$WORK/apt_$$"; }
  apt_cli install -y gimp gparted
  eq "gimp da Termux, gparted dal container" $'termux:install -y gimp\ndebian:install -y gparted\nmenu' "$(cat "$WORK/apt_$$")"
  : >"$WORK/apt_$$"
  apt_cli remove htop gparted
  eq "rimuove da dove sta" $'termux:remove htop\ndebian:remove gparted\nmenu-' "$(cat "$WORK/apt_$$")"
  : >"$WORK/apt_$$"
  local rc=0
  apt_cli install nonesiste 2>"$WORK/err_$$" || rc=$?
  eq "pacchetto inesistente: errore come apt" 100 "$rc"
  has "messaggio" "nonesiste" "$(cat "$WORK/err_$$")"
  eq "nessuna installazione" "" "$(cat "$WORK/apt_$$")"
  apt_cli update
  eq "update su entrambi" $'termux:update\ndebian:update' "$(cat "$WORK/apt_$$")"
  # il terminale del desktop carica tod-shell.sh una volta sola
  files_user_home() { printf '%s' "$HOME/u"; }
  mkdir -p "$HOME/u"
  apt_shell_setup; apt_shell_setup
  eq "riga in .bashrc" 1 "$(grep -c 'un solo apt' "$HOME/u/.bashrc")"
  has "funzione apt" 'tod" apt' "$(cat "$ROOT/share/tod-shell.sh")"
}

test_look_sfondo() { # sfondo DeX OS: famiglia nella variante del tema, oppure un'immagine dell'utente
  export TOD_TERMUX_HOME=$HOME
  local d="$HOME/.local/share/backgrounds/dexos"
  mkdir -p "$d"
  : >"$d/fiore-chiaro.jpg"; : >"$d/fiore-scuro.jpg"; : >"$d/vetro-chiaro.png"
  eq "di serie: fiore chiaro" "$d/fiore-chiaro.jpg" "$(look_sfondo_path)"
  conf_set TEMA scuro
  eq "tema scuro: la variante scura" "$d/fiore-scuro.jpg" "$(look_sfondo_path)"
  conf_set TEMA chiaro; conf_set SFONDO vetro
  eq "anche in PNG" "$d/vetro-chiaro.png" "$(look_sfondo_path)"
  : >"$HOME/mia.jpg"; conf_set SFONDO "$HOME/mia.jpg"
  eq "un'immagine dell'utente" "$HOME/mia.jpg" "$(look_sfondo_path)"
  rm -f "$HOME/mia.jpg"
  eq "immagine sparita: si torna al fiore" "$d/fiore-chiaro.jpg" "$(look_sfondo_path)"
  fails look_sfondo_cli arcobaleno
}

test_look_pulizia() { # via temi e icone di XFCE, GNOME, KDE, LXDE, v1 e Fluent; restano hicolor, Fluent-Light/Dark e i nostri
  pkg_installed() { return 1; }   # mai apt vero nei test
  local h=$HOME n
  export TOD_TERMUX_HOME=$HOME
  cx_installed() { return 1; }
  for n in Daloa Kokodi Adwaita Fluent Fluent-Light Fluent-Dark Default/xfwm4 Default/gtk-3.0 PiXflat; do mkdir -p "$PREFIX/share/themes/$n"; done
  for n in Adwaita breeze nuoveXT2 hicolor Fluent; do mkdir -p "$PREFIX/share/icons/$n"; done
  mkdir -p "$h/.icons/PiXflat" "$h/.themes/Windows-10-3.2.1" "$h/.local/share/icons/DeX-OS-Cursors-x-v2"
  : >"$h/.local/share/icons/DeX-OS-Cursors-x-v2/index.theme"
  conf_set DEXOS_NAMES "g gs w ws i DeX-OS-Cursors-x-v2 DeX-OS-Cursors-Light-x-v2"
  mkdir -p "$PREFIX/share/backgrounds/xfce"; : >"$PREFIX/share/backgrounds/wallpaper4.jpg"
  look_sfondi_ensure() { :; }
  look_pulizia
  eq "sfondi di XFCE e della v1 via" "" "$(ls "$PREFIX/share/backgrounds")"
  eq "temi rimasti" "Default Fluent-Dark Fluent-Light" "$(ls "$PREFIX/share/themes" | tr '\n' ' ' | sed 's/ $//')"
  eq "di Default resta solo la parte di GTK" gtk-3.0 "$(ls "$PREFIX/share/themes/Default")"
  eq "icone rimaste" "hicolor" "$(ls "$PREFIX/share/icons" | tr '\n' ' ' | sed 's/ $//')"
  eq "v1 via" "no no" "$([[ -e $h/.icons/PiXflat ]] && echo si || echo no) $([[ -e $h/.themes/Windows-10-3.2.1 ]] && echo si || echo no)"
  has "dpkg non li rimette" "path-exclude=$PREFIX/share/themes/Daloa/*" "$(cat "$PREFIX/etc/dpkg/dpkg.cfg.d/tod-pulizia")"
  has "nemmeno le cartelle vuote" "path-exclude=$PREFIX/share/themes/Daloa
" "$(cat "$PREFIX/etc/dpkg/dpkg.cfg.d/tod-pulizia")"
  has "puntatore di X: il nostro" "Inherits=DeX-OS-Puntatore" "$(cat "$h/.icons/default/index.theme")"
  eq "con nome fisso" DeX-OS-Cursors-x-v2 "$(readlink "$h/.local/share/icons/DeX-OS-Puntatore")"
  mkdir -p "$PREFIX/share/themes/Daloa"
  look_pulizia
  eq "una volta sola" yes "$([[ -d $PREFIX/share/themes/Daloa ]] && echo yes)"
}

test_dexos_puntatore() { # puntatore DeX OS: variante scura di serie, chiara a scelta, niente se manca il tema
  local i="$HOME/.local/share/icons" n
  conf_set DEXOS_NAMES "g gs w ws DeX-OS-Icons-x DeX-OS-Cursors-x-v1 DeX-OS-Cursors-Light-x-v1"
  eq "senza tema: niente" "" "$(look_dexos_cursor)"
  for n in DeX-OS-Cursors-x-v1 DeX-OS-Cursors-Light-x-v1; do mkdir -p "$i/$n"; : >"$i/$n/index.theme"; done
  eq "di serie: corpo scuro" DeX-OS-Cursors-x-v1 "$(look_dexos_cursor)"
  conf_set PUNTATORE chiaro
  eq "chiaro a scelta" DeX-OS-Cursors-Light-x-v1 "$(look_dexos_cursor)"
}

test_phantom_riprova() { # associazione ADB fallita (Annulla per sbaglio, codice scaduto): si riprova, mai avanti da soli
  local n="$WORK/ph_$$"
  echo 0 >"$n"
  priv_ready() { return 1; }
  adb_connect_local() { local k; k=$(($(cat "$n") + 1)); echo "$k" >"$n"; ((k >= 2)); }
  phantom_apply() { :; }
  phantom_verify() { :; }
  dev_settings_open() { :; }
  ui_msg() { :; }
  ui_menu() { if [[ $1 == "$(t ph_title)" && $2 == "$(t ph_intro)" ]]; then echo adb; else echo "${RISPOSTA:-retry}"; fi; }
  phantom_fix 2>/dev/null
  eq "riuscita al secondo tentativo" 2 "$(cat "$n")"
  eq "fatto con ADB" adb "$(conf_get PHANTOM_FIX)"
  echo -9 >"$n"
  if RISPOSTA=skip phantom_fix 2>/dev/null; then n=fatto; else n=saltato; fi
  eq "salta solo se lo sceglie l'utente" saltato "$n"
}

test_diretto_run() { # app di Debian senza proot: programma, script, Electron, ripiego su proot
  local r="$WORK/dr_root_$$" d="$WORK/dr_dir_$$" o="$WORK/dr_out_$$" out rc DBUS_SESSION_BUS_ADDRESS= XDG_CURRENT_DESKTOP=
  mkdir -p "$r/etc/tod" "$r/etc/ld.so.conf.d" "$r/usr/bin" "$r/usr/lib/editor" "$r/usr/lib/aarch64-linux-gnu" \
    "$r/usr/share/zoneinfo/Europe" "$d/lib"
  : >"$r/usr/share/zoneinfo/Europe/Rome"
  echo 'dex:x:1000:1000::/home/dex:/bin/bash' >"$r/etc/passwd"
  echo '/usr/local/lib' >"$r/etc/ld.so.conf.d/x.conf"
  printf 'GALLIUM_DRIVER=zink\n' >"$r/etc/tod/gpu.env"
  printf 'DISPLAY=:0.0\nPULSE_SERVER=127.0.0.1\n' >"$r/etc/tod/session.env"
  elf() { printf '\177ELF....../lib/ld-linux-aarch64.so.1\n' >"$1"; chmod +x "$1"; }
  elf "$r/usr/bin/app"; elf "$r/usr/bin/python3"; elf "$r/usr/lib/editor/editor"; : >"$r/usr/lib/editor/chrome-sandbox"
  ln -s ../lib/editor/editor "$r/usr/bin/editor"
  printf '#!/usr/bin/env python3\nprint(1)\n' >"$r/usr/bin/script"; chmod +x "$r/usr/bin/script"
  printf '\177ELF statico\n' >"$r/usr/bin/statico"; chmod +x "$r/usr/bin/statico"
  # il caricatore finto: --list risponde, il resto lo scrive ed esce con DR_RC
  {
    echo "#!$(command -v bash)"
    echo "if [[ \$1 == --list ]]; then [[ -f '$WORK/dr_manca_$$' ]] && echo 'libx.so => not found'; exit 0; fi"
    echo "{ echo \"argomenti: \$*\"; echo \"DISPLAY=\$DISPLAY\"; echo \"TZ=\$TZ\"; echo \"GPU=\$GALLIUM_DRIVER\"; echo \"LIB=\$LD_LIBRARY_PATH\"; echo \"HOME=\$HOME\"; echo \"BUS=\${DBUS_SESSION_BUS_ADDRESS:-nessuno}\"; echo \"DE=\${XDG_CURRENT_DESKTOP:-nessuno}\"; echo \"PRE=\$LD_PRELOAD\"; echo \"LIBVA=\${LIBVA_DRIVER_NAME:-nessuno} VAPROG=\${TOD_VA_PROG:-nessuno}\"; } >>'$o'"
    # (un'app che cade solo col video col chip)
    echo "if [[ -n \${LIBVA_DRIVER_NAME:-} && -f '$WORK/dr_rcva_$$' ]]; then exit \$(cat '$WORK/dr_rcva_$$'); fi"
    echo "exit \$(cat '$WORK/dr_rc_$$' 2>/dev/null || echo 0)"
  } >"$d/ld.so"
  chmod +x "$d/ld.so"
  # (i nomi r e d li usa anche diretto_run: qui i percorsi per intero)
  cx_rootfs() { printf '%s' "$WORK/dr_root_$$"; }
  diretto_dir() { printf '%s' "$WORK/dr_dir_$$"; }
  diretto_pronto() { return 0; }
  cx_user() { echo dex; }
  prop() { if [[ $1 == persist.sys.timezone ]]; then echo Europe/Rome; fi; }
  rm -f "$TOD_STATE_DIR/diretto-no"
  : >"$o"
  # il bus del desktop: un socket vero (python crea un socket unix), qui uno finto
  local bus="$WORK/dr_bus_$$"
  python3 -c 'import socket,sys; socket.socket(socket.AF_UNIX).bind(sys.argv[1])' "$bus"
  DISPLAY=:0 DBUS_SESSION_BUS_ADDRESS="unix:path=$bus,guid=1" diretto_run app uno "due tre"
  out=$(cat "$o"); : >"$o"
  # senza, le app si avviano un loro bus privato: niente icona nel vassoio né notifiche (26 ottobre)
  has "D-Bus del desktop" "BUS=unix:path=$bus,guid=1" "$out"
  DBUS_SESSION_BUS_ADDRESS="unix:path=$WORK/non-c-e,guid=1" diretto_run app
  has "socket che non c'è: niente indirizzo" "BUS=nessuno" "$(cat "$o")"; : >"$o"
  DBUS_SESSION_BUS_ADDRESS=disabled: diretto_run app
  has "bus spento (disabled:, da Electron): non si passa" "BUS=nessuno" "$(cat "$o")"; : >"$o"
  # il desktop: Electron sceglie da qui il portachiavi (senza, le app Electron non salvavano l'accesso: 28 ottobre)
  XDG_CURRENT_DESKTOP=XFCE diretto_run app
  has "desktop passato all'app" "DE=XFCE" "$(cat "$o")"; : >"$o"
  has "programma ELF dal caricatore, con il suo nome" "argomenti: --argv0 /usr/bin/app $r/usr/bin/app uno due tre" "$out"
  has "socket di X per intero" "DISPLAY=$TMPDIR/.X11-unix/X0" "$out"
  has "ora del telefono" "TZ=Europe/Rome" "$out"
  has "profilo GPU del container" "GPU=zink" "$out"
  # i programmi Bun e le app Rust (rustix) aprono i file senza il glibc: /home/dex non lo vedrebbero
  # vero e corto: i socket Unix stanno sotto i 108 caratteri (la copia unica di Electron, 28 ottobre)
  local casa
  casa="$PREFIX/.cx-dex"   # dentro $PREFIX: proot vede solo files/usr e files/home
  has "home col percorso vero e corto" "HOME=$casa" "$out"
  eq "porta alla home del container" "$r/home/dex" "$(readlink "$casa")"
  rm -f "$casa"; mkdir -p "$casa"
  : >"$o"; diretto_run app
  has "nome già occupato da altro: il percorso lungo" "HOME=$r/home/dex" "$(cat "$o")"
  rmdir "$casa"; : >"$o"
  has "librerie: copie nostre, poi ld.so.conf (solo cartelle che ci sono)" "LIB=$d/lib:$r/usr/lib/aarch64-linux-gnu:$r/usr/lib" "$out"
  diretto_run script x
  has "script: l'interprete del container, lo script come argomento" "--argv0 /usr/bin/python3 $r/usr/bin/python3 /usr/bin/script x" "$(cat "$o")"; : >"$o"
  diretto_run editor --no-sandbox
  has "Electron: niente /dev/shm" "$r/usr/lib/editor/editor --disable-dev-shm-usage --no-sandbox" "$(cat "$o")"; : >"$o"
  rc=0; diretto_run statico || rc=$?
  eq "programma statico: in proot" 97 "$rc"
  rc=0; diretto_run nonce || rc=$?
  eq "programma che non c'è: in proot (lo dice proot)" 97 "$rc"
  echo 127 >"$WORK/dr_rc_$$"; rc=0; diretto_run app || rc=$?
  eq "127 è dell'app (sh -c comando-che-non-c'è): non si riparte" 127 "$rc"
  eq "e non si segna" "" "$(cat "$TOD_STATE_DIR/diretto-no" 2>/dev/null)"
  rm -f "$WORK/dr_rc_$$"; : >"$WORK/dr_manca_$$"; rc=0; diretto_run app || rc=$?; rm -f "$WORK/dr_manca_$$"
  eq "librerie mancanti: in proot" 97 "$rc"
  rm -f "$TOD_STATE_DIR/diretto-no"
  # video col chip (lib/videochip.sh, 13 novembre): ogni app Chromium/Electron, solo con l'aiutante acceso; un'app che
  # cade col ponte si riapre subito senza e non finisce tra quelle "da proot" (Pietro: "come abbiamo la certezza che sui
  # dispositivi diversi le app non si bloccano?")
  printf 'VA_PRELOAD="/v/gbm.so /v/shim.so"\nVA_FLAGS="--use-angle=vulkan --x"\nLIBVA_DRIVER_NAME="tod"\n' >"$r/etc/tod/video.env"
  videochip_aiutante_vivo() { [[ -n ${VA_VIVO:-} ]]; }
  : >"$o"; diretto_run editor --no-sandbox
  out=$(cat "$o"); : >"$o"
  has "aiutante spento: l'app come sempre" "$r/usr/lib/editor/editor --disable-dev-shm-usage --no-sandbox" "$out"
  has "aiutante spento: niente driver" "LIBVA=nessuno" "$out"
  VA_VIVO=1 diretto_run editor --no-sandbox
  out=$(cat "$o"); : >"$o"
  has "app Electron: i flag del ponte" "$r/usr/lib/editor/editor --use-angle=vulkan --x --disable-dev-shm-usage --no-sandbox" "$out"
  has "app Electron: GBM e shim davanti" "PRE=/v/gbm.so /v/shim.so $d/tod-diretto.so" "$out"
  has "app Electron: driver tod e il suo percorso per lo shim" "LIBVA=tod VAPROG=/usr/lib/editor/editor" "$out"
  VA_VIVO=1 diretto_run app
  has "app qualunque: niente ponte" "LIBVA=nessuno" "$(cat "$o")"; : >"$o"
  echo 139 >"$WORK/dr_rcva_$$"; rc=0; VA_VIVO=1 diretto_run editor || rc=$?; rm -f "$WORK/dr_rcva_$$"
  out=$(cat "$o"); : >"$o"
  eq "caduta col ponte: riaperta subito senza (non 97)" 0 "$rc"
  has "prima col ponte" "LIBVA=tod" "$out"
  has "poi senza" "LIBVA=nessuno" "$out"
  has "segnata per il solo ponte" "/usr/lib/editor/editor" "$(cat "$r/etc/tod/video-no")"
  eq "e non tra quelle da proot" "" "$(cat "$TOD_STATE_DIR/diretto-no" 2>/dev/null)"
  VA_VIVO=1 diretto_run editor
  has "segnata: d'ora in poi senza ponte" "LIBVA=nessuno" "$(cat "$o")"; : >"$o"
  rm -f "$r/etc/tod/video.env" "$r/etc/tod/video-no"
  . "$ROOT/lib/videochip.sh"   # la funzione vera, per i test che seguono
  echo 139 >"$WORK/dr_rc_$$"; rc=0; diretto_run app || rc=$?; rm -f "$WORK/dr_rc_$$"
  eq "crash appena partito: in proot" 97 "$rc"
  has "e segnato" "/usr/bin/app" "$(cat "$TOD_STATE_DIR/diretto-no")"
  : >"$o"
  rc=0; diretto_run app || rc=$?
  eq "segnato: subito in proot, senza riprovare" "97:" "$rc:$(cat "$o")"
  rm -f "$TOD_STATE_DIR/diretto-no"
  # cx_run: prima senza proot; 97 → proot; --proot, --raw e la shell sempre in proot
  cx_installed() { return 0; }
  proot-distro() { echo "proot $*" >>"$o"; }
  diretto_run() { echo "diretto $*" >>"$o"; return "${DR_FINTO:-0}"; }
  : >"$o"
  cx_run app a; cx_run --proot -- app b; cx_run --raw glxinfo; cx_run; DR_FINTO=97 cx_run app c
  out=$(cat "$o")
  hasnt "senza bus del desktop: nessun --env" "DBUS_SESSION_BUS_ADDRESS" "$out"
  : >"$o"
  DBUS_SESSION_BUS_ADDRESS="unix:path=$bus" cx_run --proot -- app e
  has "proot: il D-Bus del desktop passa" "--env DBUS_SESSION_BUS_ADDRESS=unix:path=$bus" "$(cat "$o")"
  : >"$o"
  XDG_CURRENT_DESKTOP=XFCE cx_run --proot -- app f
  has "proot: anche il desktop" "--env XDG_CURRENT_DESKTOP=XFCE" "$(cat "$o")"
  rm -f "$bus"
  has "app: senza proot" "diretto app a" "$out"
  hasnt "app partita: niente proot" "tod-run app a" "$out"
  has "--proot: in proot" "tod-run app b" "$out"
  has "--raw (prove GPU): in proot" "tod-run --raw glxinfo" "$out"
  has "shell: in proot" "-- /usr/local/bin/tod-run" "$out"
  has "97: si riparte in proot" "tod-run app c" "$out"
  : >"$o"
  conf_get() { if [[ $1 == DIRETTO ]]; then echo 0; else echo "${2:-}"; fi; }
  cx_run app d
  hasnt "DIRETTO=0: tutto in proot" "diretto app d" "$(cat "$o")"
}

test_diretto_collegamenti() { # collegamenti assoluti → relativi: senza proot il kernel li seguirebbe su Android
  local r="$WORK/dl_$$"
  mkdir -p "$r/etc/ssl/certs" "$r/usr/lib/ssl" "$r/usr/bin" "$r/etc/alternatives" "$r/home/dex"
  ln -s usr/bin "$r/bin"
  echo cert >"$r/etc/ssl/certs/ca.crt"
  echo awk >"$r/usr/bin/mawk"
  ln -s /etc/ssl/certs/ca.crt "$r/usr/lib/ssl/cert.pem"
  ln -s /usr/bin/mawk "$r/etc/alternatives/awk"
  ln -s /etc/alternatives/awk "$r/usr/bin/awk"
  ln -s /data/data/com.termux/files/home/Documenti "$r/home/dex/Documenti"
  ln -s /proc/self/mounts "$r/etc/mtab"
  ln -s mawk "$r/usr/bin/nawk"
  python3 "$TOD_ROOT/share/diretto/collegamenti.py" "$r" >/dev/null
  eq "certificati" ../../../etc/ssl/certs/ca.crt "$(readlink "$r/usr/lib/ssl/cert.pem")"
  eq "stesso file" cert "$(cat "$r/usr/lib/ssl/cert.pem")"
  eq "alternative" ../../usr/bin/mawk "$(readlink "$r/etc/alternatives/awk")"
  eq "catena" awk "$(cat "$r/usr/bin/awk")"
  eq "verso Android: com'era" /proc/self/mounts "$(readlink "$r/etc/mtab")"
  eq "la home dell'utente non si tocca" /data/data/com.termux/files/home/Documenti "$(readlink "$r/home/dex/Documenti")"
  eq "relativi già giusti" mawk "$(readlink "$r/usr/bin/nawk")"
  has "un secondo giro non fa niente" ": 0" "$(python3 "$TOD_ROOT/share/diretto/collegamenti.py" "$r")"
}

test_store_prepara() { # Store (9 novembre): le estensioni di Flatpak "montate" con collegamenti, radice dell'app, menu
  local s="$WORK/st_$$" rt ap gl lo
  rt="$s/flatpak/runtime/org.prova.Platform/aarch64/1/c1/files"
  gl="$s/flatpak/runtime/org.prova.Platform.GL.default/aarch64/1.0/c2/files"
  lo="$s/flatpak/runtime/org.prova.Platform.Locale/aarch64/1/c3/files"
  ap="$s/flatpak/app/org.prova.App/aarch64/stable/c4/files"
  mkdir -p "$rt/etc/fonts" "$rt/lib/aarch64-linux-gnu/GL" "$rt/share/runtime/locale" "$rt/share/common" \
    "$rt/share/licenses" "$rt/bin" "$gl/lib" "$lo/it" "$ap/bin" "$ap/share/applications" \
    "$ap/share/icons/hicolor/scalable/apps"
  echo licenza >"$rt/share/common/LICENSE"
  ln -s /usr/share/common/LICENSE "$rt/share/licenses/prova"
  echo py >"$rt/bin/python3"
  ln -s /usr/bin/python3 "$rt/bin/python"
  echo gl >"$gl/lib/libGL.so"
  echo it >"$lo/it/x"
  echo app >"$ap/bin/app"
  echo '<svg/>' >"$ap/share/icons/hicolor/scalable/apps/org.prova.App.svg"
  printf '[Desktop Entry]\nName=Prova\nName[it]=Prova it\nExec=app %%U\nTryExec=app\nIcon=org.prova.App\n' \
    >"$ap/share/applications/org.prova.App.desktop"
  printf '[Runtime]\nname=org.prova.Platform\n\n[Environment]\nXDG_DATA_DIRS=/app/share:/usr/share\n\n' >"$rt/../metadata"
  printf '[Extension org.prova.Platform.GL]\nversions=1.0\ndirectory=lib/aarch64-linux-gnu/GL\nsubdirectories=true\nadd-ld-path=lib\n\n' >>"$rt/../metadata"
  printf '[Extension org.prova.Platform.Locale]\ndirectory=share/runtime/locale\n' >>"$rt/../metadata"
  printf '[Application]\nname=org.prova.App\nruntime=org.prova.Platform/aarch64/1\ncommand=app\n' >"$ap/../metadata"
  ln -s c1 "$s/flatpak/runtime/org.prova.Platform/aarch64/1/active"
  ln -s c2 "$s/flatpak/runtime/org.prova.Platform.GL.default/aarch64/1.0/active"
  ln -s c3 "$s/flatpak/runtime/org.prova.Platform.Locale/aarch64/1/active"
  ln -s c4 "$s/flatpak/app/org.prova.App/aarch64/stable/active"
  local py="$TOD_ROOT/share/store/tod-store.py"
  TOD_STORE_DIR=$s TOD_STORE_CASA=/casa/dex python3 "$py" prepara org.prova.App >/dev/null
  eq "Mesa dentro il runtime (GL/default)" gl "$(cat "$rt/lib/aarch64-linux-gnu/GL/default/lib/libGL.so")"
  has "collegamento relativo" "../" "$(readlink "$rt/lib/aarch64-linux-gnu/GL/default")"
  eq "traduzioni" it "$(cat "$rt/share/runtime/locale/it/x")"
  eq "assoluto → relativo" licenza "$(cat "$rt/share/licenses/prova")"
  eq "stessa cartella" python3 "$(readlink "$rt/bin/python")"
  local rad="$s/radici/org.prova.App"
  eq "radice: /usr = runtime" "$(cd "$rt" && pwd -P)" "$(readlink "$rad/usr")"
  eq "radice: /app = app" "$(cd "$ap" && pwd -P)" "$(readlink "$rad/app")"
  eq "radice: /etc del runtime" ../usr/etc/fonts "$(readlink "$rad/etc/fonts")"
  has "la casa vera nell'utente" ":/casa/dex:" "$(cat "$rad/etc/passwd")"
  has "add-ld-path dell'estensione" "/c2/files/lib" "$(cat "$rad/.ld-path")"
  TOD_STORE_DIR=$s TOD_STORE_MENU="$s/menu" TOD_STORE_TOD=/x/tod python3 "$py" menu org.prova.App >/dev/null
  local d
  d=$(cat "$s/menu/tod-store-org.prova.App.desktop")
  has "si avvia dallo Store" "Exec=/x/tod store avvia org.prova.App %U" "$d"
  has "segnata" "X-TOD-Store=flathub" "$d"
  has "icona dal percorso stabile (active)" "/stable/active/files/share/icons/hicolor/scalable/apps/org.prova.App.svg" "$d"
  if grep -q '^TryExec=' <<<"$d"; then eq "niente TryExec" "" "$d"; fi
  has "elenco" "org.prova.App	Prova it" "$(TOD_STORE_DIR=$s TOD_STORE_LANG=it_IT.UTF-8 python3 "$py" elenco)"
  TOD_STORE_DIR=$s TOD_STORE_MENU="$s/menu" TOD_STORE_TOD=/x/tod python3 "$py" togli-menu org.prova.App >/dev/null
  eq "voce tolta" "" "$(ls "$s/menu")"
}

test_store_lancio() { # Store senza proot (9 novembre, Boxy SVG non si apriva): script #!, percorso vero, zypak
  local s="$WORK/sl_$$" rad vero
  rad="$s/radici/org.prova.App"
  vero="$s/flatpak/app/files"
  mkdir -p "$vero/bin" "$vero/main" "$s/rt/bin" "$rad"
  ln -s "$vero" "$rad/app"
  ln -s "$s/rt" "$rad/usr"
  ln -s usr/bin "$rad/bin"
  printf '#!/bin/bash\nexec zypak-wrapper /app/main/prog "$@"\n' >"$vero/bin/avvio"
  printf '#!/usr/bin/env python3\n' >"$vero/bin/script-py"
  printf '\177ELF' >"$vero/main/prog"
  echo bash >"$s/rt/bin/bash"
  echo py >"$vero/bin/python3"
  local out
  out=$(cd "$TOD_ROOT/share/store" && TOD_STORE_DIR=$s python3 -c '
import importlib.util, os, sys
sp = importlib.util.spec_from_file_location("ts", "tod-store.py"); m = importlib.util.module_from_spec(sp)
sp.loader.exec_module(m)
r = sys.argv[1]
for p in ("/app/bin/avvio", "/app/bin/script-py", "/app/main/prog"):
    n, v, prima = m.lancio(r, p)
    print(n, v.replace(os.path.realpath(sys.argv[2]), "S"), " ".join(prima))
d = m.aiutanti()
z = open(os.path.join(d, "zypak-wrapper")).read()
print(os.access(os.path.join(d, "zypak-wrapper"), os.X_OK), "readlink -f" in z, "--no-sandbox" in z,
      os.access(os.path.join(d, "flatpak-spawn"), os.X_OK))' "$rad" "$s")
  has "script: l'interprete del runtime, col percorso vero" "/bin/bash S/rt/bin/bash /app/bin/avvio" "$out"
  has "#!/usr/bin/env: cerca prima in /app/bin" "/app/bin/python3 S/flatpak/app/files/bin/python3 /app/bin/script-py" "$out"
  has "programma: il percorso vero (Chromium confronta /proc/self/exe)" "/app/main/prog S/flatpak/app/files/main/prog" "$out"
  has "aiutanti di Flatpak" "True True True True" "$out"
}

test_store_catalogo() { # Store, finestra: il catalogo AppStream di Flathub → app con id vero, lingua, ricerca
  local s="$WORK/sc_$$" c
  c="$s/flatpak/appstream/flathub/aarch64/active"
  mkdir -p "$c/icons/128x128"
  echo png >"$c/icons/128x128/org.prova.Scrivi.png"
  python3 - "$c/appstream.xml.gz" <<'PY'
import gzip, sys
x = """<?xml version="1.0" encoding="UTF-8"?>
<components version="0.8" origin="flatpak">
 <component type="desktop-application"><id>org.prova.Scrivi.desktop</id>
  <name>Writer</name><name xml:lang="it">Scrivi</name>
  <summary>Write texts</summary><summary xml:lang="it">Scrivi testi</summary>
  <description><p>Plain editor.</p><ul><li>Fast</li></ul></description>
  <description xml:lang="it"><p>Editor semplice.</p><ul><li>Veloce</li></ul></description>
  <icon type="cached" width="128" height="128">org.prova.Scrivi.png</icon>
  <categories><category>Office</category></categories>
  <releases><release version="2.1" timestamp="1700000000"/></releases>
  <custom><value key="flathub::verification::verified">true</value></custom>
  <bundle type="flatpak" runtime="org.kde.Platform/aarch64/6.11">app/org.prova.Scrivi/aarch64/stable</bundle>
 </component>
 <component type="desktop-application"><id>org.prova.Note</id><name>Note di scrittura</name><summary>Appunti</summary>
  <bundle type="flatpak" runtime="org.gnome.Platform/aarch64/51">app/org.prova.Note/aarch64/stable</bundle></component>
 <component type="runtime"><id>org.kde.Platform</id><name>KDE</name>
  <bundle type="flatpak">runtime/org.kde.Platform/aarch64/6.11</bundle></component>
</components>"""
with gzip.open(sys.argv[1], "wt", encoding="utf-8") as f:
    f.write(x)
PY
  local out
  out=$(cd "$TOD_ROOT/share/store" && python3 -c '
import sys, catalogo
a = catalogo.leggi(sys.argv[1], "it")
s = [x for x in a if x["id"] == "org.prova.Scrivi"][0]
print(len(a), s["nome"], s["sommario"], s["runtime"], s["versione"], s["verificata"], bool(s["icona"]))
print(s["descrizione"].replace("\n", "|"))
print(",".join(x["id"] for x in catalogo.cerca(a, "scri")))
print(len(catalogo.leggi(sys.argv[1], "it")))' "$s")
  has "solo le app, id del bundle, testi in italiano" "2 Scrivi Scrivi testi org.kde.Platform/aarch64/6.11 2.1 True True" "$out"
  has "descrizione con elenco" "Editor semplice.||• Veloce" "$out"
  has "prima chi comincia così" "org.prova.Scrivi,org.prova.Note" "$out"
  [[ -s $s/cache/catalogo-it.json ]] || eq "indice in cache" si no
}

test_store_fonte() { # Store: prima Termux, poi Debian, Flathub solo per il resto; solo pacchetti con una finestra
  fresh_env store2
  # shellcheck source=/dev/null
  . "$ROOT/lib/store.sh"
  local out
  out=$(
    pkg_has() { [[ " gimp boxes telegram-desktop " == *" $1 "* ]]; }
    cx_installed() { return 0; }
    cx_has() { [[ " boxes gnome-boxes libreoffice " == *" $1 "* ]]; }
    store_dipendenze() { # come apt-cache show: Depends veri (ridotti)
      local p
      shift
      for p in "$@"; do
        case $p in
          gimp) printf 'gimp\t, gtk3, glib, babl\n' ;;
          boxes) printf 'boxes\t, libc6 (>= 2.38), libunistring5\n' ;;
          gnome-boxes) printf 'gnome-boxes\t, libc6, libgtk-4-1 (>= 4.14), libadwaita-1-0\n' ;;
          telegram-desktop) printf 'telegram-desktop\t, qt6-qtbase, ffmpeg\n' ;;
          libreoffice) printf 'libreoffice\t, libreoffice-core, libreoffice-writer, libreoffice-calc\n' ;;
        esac
      done
    }
    for i in org.gimp.GIMP org.gnome.Boxes org.telegram.desktop org.libreoffice.LibreOffice io.github.prova.Solo; do
      store_fonte "$i"
    done
  )
  eq "fonti" "$(printf 'termux\tgimp\ndebian\tgnome-boxes\ntermux\ttelegram-desktop\ndebian\tlibreoffice\nflathub\tio.github.prova.Solo')" "$out"
  out=$(
    store_assicura() { :; }
    store_trova() { printf 'org.gimp.GIMP'; }
    app_owner() { :; }
    store_fonte() { printf 'termux\tgimp\n'; }
    apt_cli() { printf 'apt %s | senza domande=%s\n' "$*" "${TOD_CX_SENZA_DOMANDE:-}"; }
    store_fp() { printf 'FLATPAK\n'; }
    store_installa gimp 2>&1
  )
  # 10 novembre (SuperTuxKart si fermava): i file interrotti si riprovano, e nessuna domanda di dpkg o debconf
  has "da Termux con tod apt" "apt install -y -oAcquire::Retries=3 -oDpkg::Options::=--force-confdef" "$out"
  has "il pacchetto" "--force-confold gimp | senza domande=1" "$out"
  has "lo dice" "la installo da Termux (pacchetto gimp)" "$out"
  if grep -q FLATPAK <<<"$out"; then eq "niente Flathub" "" "$out"; fi
}

test_store_link() { # dal browser (flathub.org, 10 novembre): flatpak+https, il file .flatpakref, appstream, la pagina
  local f="$WORK/slink_$$" out
  mkdir -p "$f"
  printf '[Flatpak Ref]\nName=net.supertuxkart.SuperTuxKart\nBranch=stable\nUrl=https://dl.flathub.org/repo/\n' \
    >"$f/scaricato (1).flatpakref"
  out=$(cd "$TOD_ROOT/share/store" && python3 -c '
import sys, catalogo
for x in sys.argv[1:]:
    print(catalogo.id_da_link(x))' \
    "flatpak+https://dl.flathub.org/repo/appstream/net.supertuxkart.SuperTuxKart.flatpakref" \
    "$f/scaricato (1).flatpakref" "file://$f/scaricato%20(1).flatpakref" \
    "appstream://org.kde.kwrite.desktop" "appstream:org.gnome.Calculator" \
    "https://flathub.org/it/apps/org.gimp.GIMP/install" "/sparito/io.github.prova.App.flatpakref" "org.kde.krita")
  eq "link → id" "$(printf '%s\n' net.supertuxkart.SuperTuxKart net.supertuxkart.SuperTuxKart \
    net.supertuxkart.SuperTuxKart org.kde.kwrite org.gnome.Calculator org.gimp.GIMP io.github.prova.App org.kde.krita)" "$out"
  has "la finestra usa i link" "catalogo.id_da_link(args[0])" "$(cat "$ROOT/share/store/tod-store-gui.py")"
}

test_store_cx_senza_domande() { # cx_apt: niente domande di debconf solo per lo Store, dal terminale apt chiede come sempre
  fresh_env store4
  local out
  out=$(
    proot-distro() { printf '%s\n' "$*"; }
    cx_apt install -y gioco
    TOD_CX_SENZA_DOMANDE=1 cx_apt install -y gioco
  )
  eq "dal terminale: come sempre" "login --shared-tmp debian -- env TOD_MENU_SYNC=0 apt install -y gioco" "$(sed -n 1p <<<"$out")"
  eq "dallo Store: senza domande" \
    "login --shared-tmp debian -- env TOD_MENU_SYNC=0 DEBIAN_FRONTEND=noninteractive apt install -y gioco" "$(sed -n 2p <<<"$out")"
}

test_store_aggiornamenti() { # Aggiornamenti: le app dello Store con nome e icona, i runtime tra i componenti di sistema
  local f="$WORK/sagg_$$" out
  mkdir -p "$f"
  printf '#!/bin/sh\necho "$*" >"%s/argomenti"\necho "Updating app/org.prova.App/aarch64/stable"\n' "$f" >"$f/tod-ok"
  printf '#!/bin/sh\necho "$*" >"%s/argomenti"\necho "error: niente rete"\nexit 1\n' "$f" >"$f/tod-ko"
  echo png >"$f/icona.png"
  out=$(LANGUAGE=it HOME="$f" python3 - "$ROOT/share/tod-updates.py" "$f" <<'EOF'
import importlib.util as u, sys
s = u.spec_from_file_location("up", sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)
d = sys.argv[2]
it = m.store_items("org.prova.App\tProva\t1.0\t1.1\t" + d + "/icona.png\norg.gnome.Platform\t\t\t50.3\t\n\nrumore\n")
print(len(it), it[0]["app"], it[0]["from"], it[0]["to"], it[0]["icon"] == d + "/icona.png", it[0]["where"])
print("app" in it[1], it[1]["name"])
print(m.summary({"tod": [], "termux": [], "debian": [], "store": it}))
m.TOD = d + "/tod-ok"
passi = []
print(m.install([it[0]], lambda f, t: passi.append(t)), "Aggiornamento di org.prova.App…" in passi)
print(open(d + "/argomenti").read().strip())
print(m.install(it, lambda *a: None), open(d + "/argomenti").read().strip())
m.TOD = d + "/tod-ko"
print(m.install(it, lambda *a: None))
EOF
)
  eq "voci: app con nome e icona" "2 Prova 1.0 1.1 True store" "$(sed -n 1p <<<"$out")"
  eq "runtime: componente di sistema" "False org.gnome.Platform" "$(sed -n 2p <<<"$out")"
  eq "nel riassunto" "Prova e un altro aggiornamento" "$(sed -n 3p <<<"$out")"
  eq "una sola app: solo lei" "(True, '') True" "$(sed -n 4p <<<"$out")"
  eq "tod store aggiorna con l'id" "store aggiorna org.prova.App" "$(sed -n 5p <<<"$out")"
  eq "con i runtime: tutto lo Store" "(True, '') store aggiorna" "$(sed -n 6p <<<"$out")"
  eq "errore: le ultime righe" "(False, 'error: niente rete')" "$(sed -n 7p <<<"$out")"
  has "il controllo chiede allo Store" '"_aggiornamenti"' "$(cat "$ROOT/share/tod-updates.py")"
}

test_store_voce() { # Store nello Start: con l'installazione e con ogni aggiornamento di tod (desktop_install_entries)
  fresh_env store3
  desktop_store_entry
  local d
  d=$(cat "$HOME/.local/share/applications/tod-store.desktop")
  has "apre la finestra (con il link del browser)" "Exec=python3 $TOD_ROOT/share/store/tod-store-gui.py %u" "$d"
  has "il pulsante Installa di flathub.org" \
    "MimeType=x-scheme-handler/flatpak+https;x-scheme-handler/appstream;application/vnd.flatpak.ref;" "$d"
  local m
  m=$(cat "$HOME/.config/mimeapps.list")
  has "flatpak+https allo Store" "x-scheme-handler/flatpak+https=tod-store.desktop" "$m"
  has "i file .flatpakref allo Store" "application/vnd.flatpak.ref=tod-store.desktop" "$m"
  has "nome" "Name[it]=Store" "$d"
  has "la barra la riconosce" "StartupWMClass=tod-store" "$d"
  has "tra le app di sistema" "Categories=System;PackageManager;" "$d"
  has "all'installazione e dopo gli aggiornamenti" "desktop_store_entry" "$(declare -f desktop_install_entries)"
}

test_store_nolink_compila() { # tod-nolink.c (senza intestazioni) compila senza avvisi, anche col glibc del CI
  local cc
  cc=$(command -v clang || command -v gcc) || { echo "  (nessun compilatore: salto)"; return 0; }
  "$cc" -shared -fPIC -O2 -Wall -Wextra -Werror -o "$WORK/nolink_$$.so" "$ROOT/share/store/tod-nolink.c"
  has "O_TMPFILE → EOPNOTSUPP" "EOPNOTSUPP" "$(cat "$ROOT/share/store/tod-nolink.c")"
  has "collegamento negato → EXDEV" "EXDEV" "$(cat "$ROOT/share/store/tod-nolink.c")"
}

test_store_una_app() { # Store: una app, un solo sistema (GIMP già in Debian → niente seconda copia da Flathub)
  fresh_env store1
  # shellcheck source=/dev/null
  . "$ROOT/lib/store.sh"
  local out rc=0
  out=$(
    store_assicura() { :; }
    store_trova() { printf 'org.gimp.GIMP'; }
    app_owner() { printf 'gimp'; }
    store_fp() { touch "$HOME/scaricato"; }
    store_installa gimp 2>&1
  ) || rc=$?
  eq "rifiutata" 1 "$rc"
  has "messaggio" "org.gimp.GIMP: l'app è già installata (gimp)" "$out"
  eq "niente scaricato" "" "$(ls "$HOME")"
}

test_media_voci() { # Foto, Lettore multimediale e Lettore musicale nello Start, e predefiniti per immagini, video,
  # audio (9 novembre: i .mp4 si aprivano con Ristretto). Ristretto lascia il posto; le scelte dell'utente restano
  fresh_env media1
  local apps="$HOME/.local/share/applications" f="$HOME/.config/mimeapps.list" d
  mkdir -p "$HOME/.config"
  printf '%s\n' "[Added Associations]" "image/png=org.xfce.ristretto.desktop;" "" "[Default Applications]" \
    "image/jpeg=org.xfce.ristretto.desktop" "video/mp4=org.xfce.ristretto.desktop" \
    "image/svg+xml=tod-cx-ai.storyteller.vectorcraft.desktop" "text/plain=org.xfce.mousepad.desktop" >"$f"
  desktop_media_entries
  d=$(cat "$apps/tod-foto.desktop")
  has "Foto: la finestra" "Exec=python3 $TOD_ROOT/share/media/tod-foto.py %F" "$d"
  has "Foto: nome" "Name[it]=Foto" "$d"
  has "Foto: la barra la riconosce" "StartupWMClass=tod-foto" "$d"
  has "Foto: apre le JPEG" "image/jpeg;" "$d"
  has "Foto: apre le HEIC del telefono" "image/heic;" "$d"
  d=$(cat "$apps/tod-video.desktop")
  has "video: modo video" "share/media/tod-lettore.py --video %F" "$d"
  has "video: nome" "Name[it]=Lettore multimediale" "$d"
  has "video: barra" "StartupWMClass=tod-video" "$d"
  has "video: mp4 e mkv" "video/mp4;video/x-m4v;video/x-matroska;" "$d"
  d=$(cat "$apps/tod-musica.desktop")
  has "musica: modo musica" "share/media/tod-lettore.py --musica %F" "$d"
  has "musica: nome" "Name[it]=Lettore musicale" "$d"
  has "musica: barra" "StartupWMClass=tod-musica" "$d"
  has "musica: mp3 e flac" "audio/mpeg;audio/mp3;audio/flac;" "$d"
  d=$(cat "$f")
  has "le JPEG non più a Ristretto" "image/jpeg=tod-foto.desktop" "$d"
  has "i video non più a Ristretto" "video/mp4=tod-video.desktop" "$d"
  has "tipo senza app: alla nostra" "image/heic=tod-foto.desktop" "$d"
  has "audio a Lettore musicale" "audio/mpeg=tod-musica.desktop" "$d"
  has "scelta dell'utente: resta" "image/svg+xml=tod-cx-ai.storyteller.vectorcraft.desktop" "$d"
  has "altri tipi: intatti" "text/plain=org.xfce.mousepad.desktop" "$d"
  has "le associazioni aggiunte non si toccano" "image/png=org.xfce.ristretto.desktop;" "$d"
  desktop_media_entries   # a ogni aggiornamento: nessun doppione
  eq "una riga per tipo" 1 "$(grep -c '^image/jpeg=' "$f")"
  eq "una voce sola nel file" 1 "$(grep -c '^MimeType=' "$apps/tod-foto.desktop")"
  # l'utente sceglie un'altra app per i video (Esplora file → Apri con): resta anche dopo l'aggiornamento
  sed -i 's|^video/mp4=.*|video/mp4=vlc.desktop|' "$f"
  desktop_media_entries
  has "VLC scelto dall'utente resta" "video/mp4=vlc.desktop" "$(cat "$f")"
  has "all'installazione e dopo gli aggiornamenti" "desktop_media_entries" "$(declare -f desktop_install_entries)"
  has "Ristretto non più nello Start" "org.xfce.ristretto.desktop" "$DESKTOP_USELESS"
  hasnt "Ristretto non più installato" "ristretto" "$(desktop_packages xfce)"
}

test_media_pacchetti() { # GStreamer e i caricatori di immagini: con l'installazione, gli Aggiornamenti e tod update
  pkg_has() { return 0; }
  pkg_installed() { [[ $1 != gst-libav && $1 != webp-pixbuf-loader ]]; }
  pkg_install() { echo "install $*" >>"$WORK/mpk_$$"; }
  pkg_install_avail() { echo "avail $*" >>"$WORK/mpk_$$"; }
  desktop_preconfigure() { :; }
  desktop_install_entries() { :; }
  files_setup() { :; }
  eq "mancano solo quelli non installati" "gst-libav webp-pixbuf-loader" "$(media_mancanti)"
  desktop_install xfce
  has "installazione: solo i mancanti" "avail gst-libav webp-pixbuf-loader" "$(cat "$WORK/mpk_$$")"
  : >"$WORK/mpk_$$"
  usb_on() { return 1; }
  rete_on() { return 1; }
  videochip_on() { return 1; }   # niente compilazioni vere del video col chip
  x11_glamor_da_aggiornare() { return 1; }
  self_after >/dev/null
  has "Aggiornamenti (tod _self_after): i mancanti" "avail gst-libav webp-pixbuf-loader" "$(cat "$WORK/mpk_$$")"
  has "tod update: anche lì" "media_install" "$(declare -f tod_update)"
  local p
  for p in gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad gst-libav webp-pixbuf-loader libheif gexiv2; do
    has "pacchetto $p" " $p " " $MEDIA_PKGS "
  done
  pkg_installed() { return 0; }
  : >"$WORK/mpk_$$"
  media_install
  eq "tutto installato: nessuna installazione" "" "$(cat "$WORK/mpk_$$")"
}

test_media_comandi() { # tod foto|video|musica: aprono l'app (staccata); senza desktop un messaggio chiaro
  fresh_env media3
  setsid() { echo "$*" >>"$WORK/mcmd_$$"; }
  local out rc=0
  out=$(unset DISPLAY; media_apri tod-foto.py x.jpg 2>&1) || rc=$?
  eq "senza desktop: errore" 1 "$rc"
  has "senza desktop: perché" "$(t md_no_schermo)" "$out"
  (
    export DISPLAY=:9
    cd "$WORK" && media_apri tod-lettore.py --video "film con spazi.mkv"
    wait
  )
  has "percorso completo, spazi compresi" "share/media/tod-lettore.py --video $WORK/film con spazi.mkv" "$(cat "$WORK/mcmd_$$")"
  pkg_installed() { [[ $1 != gexiv2 ]]; }
  has "stato: cosa manca" "gexiv2" "$(media_cli stato 2>&1)"
  for c in foto video musica media; do
    has "tod $c nel case di tod" "$c" "$(sed -n '/^tod_main() {/,/^}/p' "$ROOT/tod" | grep -E "^\s+${c}[ |)]")"
  done
}

test_media_libreria() { # Foto, Video, Musica senza finestre: date dai nomi, coda (casuale, ripeti), posizioni, album
  command -v python3 >/dev/null || return 0
  local out
  out=$(cd "$ROOT/share/media" && TZ=UTC LANGUAGE=it HOME="$WORK/mlib_$$" python3 - <<'PY'
import os, time, libreria as L
def eq(n, a, b):
    if a != b:
        print("✘", n, "atteso", repr(a), "ottenuto", repr(b))
t = L.data_da_nome("IMG_20260601_183012.jpg", 1)
eq("data dalla fotocamera", (2026, 6, 1, 18, 30), time.localtime(t)[:5])
eq("screenshot di Samsung", (2026, 10, 27), time.localtime(L.data_da_nome("Screenshot_20261027-101200_Chrome.jpg", 1))[:3])
eq("solo la data", (2026, 10, 29), time.localtime(L.data_da_nome("20261029_171043.jpg", 1))[:3])
eq("niente data nel nome: quella del file", 12345.0, L.data_da_nome("vacanze.jpg", 12345.0))
eq("numeri a caso non sono date", 7.0, L.data_da_nome("IMG_99999999.jpg", 7.0))
g = L.per_mese([{"data": L.data_da_nome("IMG_20261103_100000.jpg", 0)}, {"data": L.data_da_nome("IMG_20260601_100000.jpg", 0)},
                {"data": L.data_da_nome("IMG_20261101_100000.jpg", 0)}])
eq("mesi, dal più recente", ["novembre 2026", "giugno 2026"], [x[0] for x in g])
eq("due foto a novembre", 2, len(g[0][1]))
eq("tempo", ["0:00", "3:07", "1:02:03"], [L.tempo(0), L.tempo(187.9), L.tempo(3723)])
eq("misura con la virgola", "2,4 MB", L.misura(2400000))
eq("ordine naturale", ["foto 2.jpg", "foto 10.jpg"], sorted(["foto 10.jpg", "foto 2.jpg"], key=L.chiave_naturale))
c = L.Coda(["a", "b", "c"])
eq("avanti", "b", c.avanti())
eq("fine coda senza ripeti", None, (c.avanti(), c.avanti(automatico=True))[1])
c.ripeti = c.TUTTI
eq("ripeti tutti: si ricomincia", "a", c.avanti(automatico=True))
c.ripeti = c.UNO
eq("ripeti uno: da sola ripete", "a", c.avanti(automatico=True))
eq("ripeti uno: avanti a mano va avanti", "b", c.avanti())
c = L.Coda([str(i) for i in range(20)])
c.vai(5)
c.metti_casuale(True)
vis = [c.corrente()] + [c.avanti() for _ in range(19)]
eq("casuale: tutti una volta sola", sorted(str(i) for i in range(20)), sorted(vis))
eq("casuale: si parte da quello in corso", "5", vis[0])
c.metti_casuale(False)
eq("senza casuale: l'ordine vero dal brano in corso", str(int(c.corrente()) + 1) if c.corrente() != "19" else None, c.avanti())
c = L.Coda(["a", "b", "c"])
c.aggiungi(["x"], subito_dopo=True)
eq("riproduci dopo", ["a", "x", "b", "c"], c.elementi)
eq("e il prossimo è quello", "x", c.avanti())
c.togli(0)
eq("togliere prima del corrente", ("x", ["x", "b", "c"]), (c.corrente(), c.elementi))
p = L.Posizioni(os.path.join(os.environ["HOME"], "pos.json"))
p.scrivi("/v.mp4", 754.2, 3600)
eq("riprendi da", 754.2, L.Posizioni(p.percorso).leggi("/v.mp4", 3600))
p.scrivi("/v.mp4", 3590, 3600)
eq("finito: dall'inizio", 0, p.leggi("/v.mp4", 3600))
p.scrivi("/w.mp4", 4, 3600)
eq("appena iniziato: niente", 0, p.leggi("/w.mp4", 3600))
b = L.brano_vuoto("/m/03 - Pioggia sui tetti.flac")
eq("senza tag: titolo e traccia dal nome", ("Pioggia sui tetti", 3), (b["titolo"], b["traccia"]))
br = {"/m/2.mp3": dict(L.brano_vuoto("/m/2.mp3"), titolo="B", album="Uno", artista="X", traccia=2),
      "/m/1.mp3": dict(L.brano_vuoto("/m/1.mp3"), titolo="A", album="Uno", artista="X", traccia=1),
      "/n/1.mp3": dict(L.brano_vuoto("/n/1.mp3"), titolo="C", album="Uno", artista="Y", traccia=1)}
al = L.raggruppa_album(br)
eq("stesso titolo, artisti diversi: due album", 2, len(al))
eq("album in ordine di traccia", ["/m/1.mp3", "/m/2.mp3"], al[("uno", "x")]["brani"])
eq("ordine senza articolo", "beatles", L.chiave_ordine("The Beatles"))
eq("ricerca a parole", (True, False), (L.cerca("luce nord", "Luce del nord"), L.cerca("luce sud", "Luce del nord")))
os.makedirs(os.path.join(os.environ["HOME"], ".config"), exist_ok=True)
os.makedirs(os.path.join(os.environ["HOME"], "Immagini"), exist_ok=True)
open(os.path.join(os.environ["HOME"], ".config", "user-dirs.dirs"), "w").write('XDG_MUSIC_DIR="$HOME/Musica"\n')
u = L.cartelle_utente()
eq("cartelle da user-dirs", os.path.join(os.environ["HOME"], "Musica"), u.get("MUSIC"))
eq("e i ripieghi", os.path.join(os.environ["HOME"], "Immagini"), u.get("PICTURES"))
os.environ["TOD_FOTO_CARTELLE"] = os.path.join(os.environ["HOME"], "Immagini") + ":/non/esiste"
eq("cartelle di prova (TOD_FOTO_CARTELLE): solo quelle che esistono", 1, len(L.cartelle_foto()))
# Anteprima (barra spaziatrice): cosa mostrare, e con un file solo dalla scrivania le frecce sulla sua cartella
d = os.path.join(os.environ["HOME"], "ap")
os.makedirs(os.path.join(d, "zeta cartella"))
for n in ("b 10.txt", "b 2.txt", "A.pdf", ".nascosto", "foto.JPG", "suono", "film.mkv", "x.bin"):
    open(os.path.join(d, n), "w").write("x")
g = lambda n, m="", t=False: L.genere(os.path.join(d, n), m, t)
eq("generi dai nomi", ["cartella", "testo", "pdf", "immagine", "video", "altro"],
   [g("zeta cartella"), g("b 2.txt"), g("A.pdf"), g("foto.JPG"), g("film.mkv"), g("x.bin")])
eq("generi dal tipo (senza estensione)", ["audio", "testo", "testo", "pdf"],
   [g("suono", "audio/ogg"), g("suono", "text/x-log"), g("suono", "application/x-shellscript", True), g("suono", "application/pdf")])
lista, i = L.vicini(os.path.join(d, "b 2.txt"))
eq("vicini: prima le cartelle, poi i nomi (2 prima di 10), senza i nascosti",
   ["zeta cartella", "A.pdf", "b 2.txt", "b 10.txt", "film.mkv", "foto.JPG", "suono", "x.bin"], [os.path.basename(x) for x in lista])
eq("e il posto di quello scelto", 2, i)
eq("un nascosto scelto: anche i nascosti", True, ".nascosto" in [os.path.basename(x) for x in L.vicini(os.path.join(d, ".nascosto"))[0]])
print("fine")
PY
  )
  hasnt "nessun errore" "✘" "$out"
  has "arrivato in fondo" "fine" "$out"
}

test_media_finestre_compilano() { # le tre app: Python valido, prgname uguale alla voce dello Start, niente schermo
  command -v python3 >/dev/null || return 0
  python3 -m py_compile "$ROOT/share/media/libreria.py" "$ROOT/share/media/comune.py" "$ROOT/share/media/tod-foto.py" \
    "$ROOT/share/media/tod-lettore.py" "$ROOT/share/media/visore.py" "$ROOT/share/media/motore.py" \
    "$ROOT/share/media/tod-anteprima.py" || { echo "share/media non compila"; return 1; }
  has "Foto: prgname" 'GLib.set_prgname("tod-foto")' "$(cat "$ROOT/share/media/tod-foto.py")"
  has "lettore: prgname dei due modi" 'GLib.set_prgname("tod-video" if VIDEO else "tod-musica")' \
    "$(cat "$ROOT/share/media/tod-lettore.py")"
  # le prove a vista vanno su uno schermo finto: le cartelle vere si cambiano con queste variabili
  local v
  for v in TOD_FOTO_CARTELLE TOD_VIDEO_CARTELLE TOD_MUSICA_CARTELLE TOD_TELEFONO_DIR; do
    has "$v" "$v" "$(cat "$ROOT/share/media/libreria.py")"
  done
  has "uscita video scelta a mano" "TOD_VIDEO_USCITA" "$(cat "$ROOT/share/media/motore.py")"
  has "tod media prepara rifà le icone delle app nuove" "desktop_icone_app" "$(sed -n '/^media_cli() {/,/^}/p' "$ROOT/lib/desktop.sh")"
  has "anche le voci dello Start (installazione, aggiornamenti)" "desktop_icone_app" "$(sed -n '/^desktop_install_entries() {/,/^}/p' "$ROOT/lib/desktop.sh")"
  has "il Lettore usa il motore in comune" "from motore import" "$(cat "$ROOT/share/media/tod-lettore.py")"
  has "Foto usa il visore in comune" "from visore import" "$(cat "$ROOT/share/media/tod-foto.py")"
  has "icona DeX OS di Foto" "tod-foto" "$(cat "$ROOT/share/dexos/dexos-icons.py")"
  has "icona DeX OS del lettore" "multimedia-video-player" "$(cat "$ROOT/share/dexos/dexos-icons.py")"
}

test_media_video_verticali() { # 11 novembre: i video girati in verticale col telefono si vedevano di lato
  command -v python3 >/dev/null || return 0
  local m="$ROOT/share/media/motore.py" c="$ROOT/share/media/comune.py" out
  # le misure come si vedono (la finestra dell'Anteprima diventa alta): la funzione vera, presa dal file
  out=$(python3 - "$m" <<'PY'
import ast, sys
f = next(n for n in ast.parse(open(sys.argv[1]).read()).body if isinstance(n, ast.FunctionDef) and n.name == "gira_misure")
ns = {}
exec(compile(ast.Module(body=[f], type_ignores=[]), "motore.py", "exec"), ns)
class Tag:
    def __init__(self, o): self.o = o
    def get_string(self, k): return (self.o is not None and k == "image-orientation", self.o)
g = ns["gira_misure"]
print(g(1920, 1080, Tag("rotate-90")), g(1920, 1080, Tag("rotate-270")), g(1920, 1080, Tag("flip-rotate-90")),
      g(1920, 1080, Tag("rotate-180")), g(1920, 1080, Tag(None)), g(1920, 1080, None))
PY
  )
  eq "girati di 90 e 270: alti; 180 e senza indicazione: come sono" \
    "(1080, 1920) (1080, 1920) (1080, 1920) (1920, 1080) (1920, 1080) (1920, 1080)" "$out"
  has "il video si gira come chiede il file" 'GIRA = "videoflip video-direction=auto ! "' "$(cat "$m")"
  has "prima dei sottotitoli, con la GPU e con cairo" 'oc = GIRA + (' "$(sed -n '/    def metti_uscita(self):/,/    def collega_sub/p' "$m")"
  has "anche le miniature dei video" '"videoflip video-direction=auto ! videoconvertscale' "$(cat "$c")"
  has "le miniature vecchie (di lato) si rifanno" '== "Termux On DeX" and L.tipo_di(percorso) == "video"' "$(cat "$c")"
  has "quelle nuove hanno il segno nuovo" '"Termux On DeX 2"' "$(cat "$c")"
  has "l'Anteprima usa le misure girate" "self.M.gira_misure(" "$(cat "$ROOT/share/media/tod-anteprima.py")"
}

test_anteprima_spazio() { # barra spaziatrice in Esplora file e sulla scrivania: anteprima come Quick Look di macOS
  local src="$ROOT/share/tod-gettext.c" cc ap="$ROOT/share/media/tod-anteprima.py"
  has "aggancio prima che parta l'app" "int g_application_run(void *app, int argc, char **argv)" "$(cat "$src")"
  has "solo Thunar e xfdesktop" '!strcasecmp(n, "thunar") ? 1 : !strcasecmp(n, "xfdesktop") ? 2 : 0' "$(cat "$src")"
  has "compila anche con glibc (controllo su GitHub, Ubuntu): getprogname solo su Bionic" \
    "const char *n = program_invocation_short_name;" "$(sed -n '/^static int tod_ap_chi(void)/,/^}/p' "$src")"
  has "si può spegnere" "TOD_ANTEPRIMA_SPENTA" "$(cat "$src")"
  has "mai mentre si scrive (rinomina, indirizzo, ricerca)" 'tod_ap_e_un(fuoco, "GtkEditable") || tod_ap_e_un(fuoco, "GtkTextView")' "$(cat "$src")"
  has "spazio senza modificatori" "kv != 0x20" "$(cat "$src")"
  has "niente di scelto: lo spazio resta quello di sempre" "return n > 0;" "$(cat "$src")"
  has "con un file solo: la vista nel suo ordine" '"--indice"' "$(cat "$src")"
  has "lanciata dal PATH della sessione (share/bin)" 'file[0] = "tod-anteprima";' "$(cat "$src")"
  [[ -x $ROOT/share/bin/tod-anteprima ]] || { echo "share/bin/tod-anteprima non eseguibile"; return 1; }
  has "il lanciatore apre la finestra" "media/tod-anteprima.py" "$(cat "$ROOT/share/bin/tod-anteprima")"
  has "prgname dell'anteprima" 'GLib.set_prgname("tod-anteprima")' "$(cat "$ap")"
  has "di nuovo lo spazio sugli stessi file: si chiude" "(file, indice) == self.fin.richiesti" "$(cat "$ap")"
  has "spazio ed Esc chiudono" "if k in (Gdk.KEY_space, Gdk.KEY_Escape):" "$(cat "$ap")"
  has "il video aspetta il MapNotify vero (zink)" "self.fin.mappata and self.area_ok" "$(cat "$ap")"
  cc=$(command -v clang || command -v gcc) || { echo "  (nessun compilatore: salto la compilazione)"; return 0; }
  "$cc" -shared -fPIC -O2 -Wall -Wextra -Wno-unused-parameter -Werror -o "$WORK/ta_$$.so" "$src" -ldl
  eq "contiene l'aggancio" 1 "$(grep -ac g_application_run "$WORK/ta_$$.so" | sed 's/^[1-9][0-9]*$/1/')"
}

test_diretto_correggi() { # la copia del glibc: set_robust_list → -ENOSYS, file interni → /dev/fd/NN
  local f="$WORK/glibc_$$" out
  python3 -c '
import struct, sys
b = struct.pack("<IIII", 0x52800C68, 0xD503201F, 0xD4000001, 0xD4000001)   # mov w8,#99; nop; svc; svc
b += struct.pack("<II", 0x52800B08, 0xD4000001)                            # mov w8,#88; svc (altra chiamata)
b += b"\0/etc/hosts\0/etc/hosts.allow\0/etc/resolv.conf\0"
open(sys.argv[1], "wb").write(b)' "$f"
  python3 "$TOD_ROOT/share/diretto/correggi.py" "$f" "$f.ok" >/dev/null
  out=$(python3 -c '
import struct, sys
b = open(sys.argv[1], "rb").read()
w = struct.unpack_from("<6I", b)
print(hex(w[2]), hex(w[5]), [x for x in b[24:].split(b"\0") if x])' "$f.ok")
  eq "svc di set_robust_list → movn x0,#37; l'altra chiamata resta" "0x928004a0 0xd4000001" "$(cut -d' ' -f1,2 <<<"$out")"
  has "file interni sui descrittori, gli altri intatti" "[b'/dev/fd/96', b'/etc/hosts.allow', b'/dev/fd/97']" "$out"
  eq "stessa lunghezza" "$(stat -c %s "$f")" "$(stat -c %s "$f.ok")"
  # le azioni di posix_spawn le fa il glibc senza fakechroot: ArtCraft Launcher avviava le app in /usr/bin
  # e il figlio usciva con 127 (26 ottobre). tod-diretto.c non si compila qui: si controlla che ci siano
  has "cartella di partenza tradotta" "posix_spawn_file_actions_addchdir_np" "$(cat "$ROOT/share/diretto/tod-diretto.c")"
  has "file da aprire tradotti" "posix_spawn_file_actions_addopen" "$(cat "$ROOT/share/diretto/tod-diretto.c")"
  # elenchi di argomenti senza limite: con 1024 il collegatore di cargo perdeva metà dei file (26 ottobre)
  eq "nessun elenco fisso" 0 "$(grep -c '\[1024\]' "$ROOT/share/diretto/tod-diretto.c")"
  has "argomenti troppo lunghi per fakechroot: E2BIG come il kernel (rustc riprova con @file)" "EINVAL → E2BIG" "$(cat "$ROOT/share/diretto/tod-diretto.c")"
  # la "seconda copia" di Electron riconosce la prima da /proc/<pid>/exe (link di ritorno da Chromium, 28 ottobre)
  has "/proc/<pid>/exe delle altre app" "altro_exe(path, buf, len, vero(path, buf, len))" "$(cat "$ROOT/share/diretto/tod-diretto.c")"
  eq "readlink e readlinkat non si chiamano a vicenda (giro senza fine)" 0 "$(grep -c 'return readlink(' "$ROOT/share/diretto/tod-diretto.c")"
  # accept è vietata da Android (solo accept4): senza, Electron non accettava la seconda copia (28 ottobre)
  has "accept del glibc → accept4" "return accept4(fd, indirizzo, lunghezza, 0);" "$(cat "$ROOT/share/diretto/tod-diretto.c")"
  has "accept diretta al kernel → accept4" "syscall(NR_ACCEPT4, x[0], x[1], x[2], 0, 0, 0)" "$(cat "$ROOT/share/diretto/tod-diretto.c")"
}

test_diretto_usb_e_resti() { # app Electron dal menu (23 ottobre): dischi USB accesi e resti di un avvio caduto
  local r="$WORK/du_root_$$" d="$WORK/du_dir_$$" o="$WORK/du_out_$$" p="$WORK/du_proc_$$" out
  mkdir -p "$r/etc/tod" "$r/usr/bin" "$d/lib" "$TMPDIR/tod-usb" "$HOME/.local/share/tod-usb"
  echo 'dex:x:1000:1000::/home/dex:/bin/bash' >"$r/etc/passwd"
  printf '\177ELF....../lib/ld-linux-aarch64.so.1\n' >"$r/usr/bin/app"; chmod +x "$r/usr/bin/app"
  {
    echo "#!$(command -v bash)"
    echo 'if [[ $1 == --list ]]; then exit 0; fi'
    echo "{ echo \"PRE=\$LD_PRELOAD\"; echo \"FUORI=\$FAKECHROOT_EXCLUDE_PATH\"; } >>'$o'"
  } >"$d/ld.so"
  chmod +x "$d/ld.so"
  cx_rootfs() { printf '%s' "$WORK/du_root_$$"; }
  diretto_dir() { printf '%s' "$WORK/du_dir_$$"; }
  diretto_pronto() { return 0; }
  cx_user() { echo dex; }
  usb_data() { printf '%s' "$HOME/.local/share/tod-usb"; }
  : >"$TMPDIR/tod-usb/lkl-attivo"; : >"$HOME/.local/share/tod-usb/tod-fs-glibc.so"
  : >"$o"
  diretto_run app
  out=$(cat "$o")
  has "tod-fs dopo fakechroot (i suoi execvp saltavano fakechroot)" \
    "PRE=$d/tod-diretto.so $r$DIRETTO_FC $HOME/.local/share/tod-usb/tod-fs-glibc.so" "$out"
  has "/media/usb lo traduce tod-fs, non fakechroot" ":/media/usb" "$out"
  rm -f "$TMPDIR/tod-usb/lkl-attivo"; : >"$o"
  diretto_run app
  hasnt "senza dischi: niente tod-fs" "tod-fs-glibc" "$(cat "$o")"
  # resti: solo i processi partiti dal nostro caricatore con quel programma
  mkdir -p "$p/11" "$p/12" "$p/13" "$p/14"
  printf '%s\0--argv0\0/usr/bin/app\0%s\0' "$d/ld.so" "$r/usr/bin/app" >"$p/11/cmdline"
  printf '%s\0--argv0\0/usr/bin/app\0%s\0--type=gpu\0' "$d/ld.so" "$r/usr/bin/app" >"$p/12/cmdline"
  printf '%s\0--argv0\0/usr/bin/altra\0%s\0' "$d/ld.so" "$r/usr/bin/altra" >"$p/13/cmdline"
  printf 'proot\0%s\0' "$r/usr/bin/app" >"$p/14/cmdline"
  eq "resti: l'app e i suoi figli, non altre app né proot" "11 12" "$(diretto_resti "$d/ld.so" "$r/usr/bin/app" "$p" | sort | tr '\n' ' ' | sed 's/ $//')"
}

test_usb_lkl_accanto() { # il pacchetto dei dischi portato con il progetto (dist/): si usa, se l'impronta torna
  local r="$WORK/lklroot_$$"
  mkdir -p "$r/dist" "$TOD_CACHE_DIR"
  echo pacchetto >"$r/dist/tod-lkl-aarch64.tar.xz"
  net_download() { [[ -f $2 ]] && echo "in cache" || echo "da internet"; return 1; }
  local giusta
  giusta=$(sha256sum "$r/dist/tod-lkl-aarch64.tar.xz" | cut -d' ' -f1)
  eq "impronta giusta: copiato e usato" "in cache" "$(TOD_ROOT=$r LKL_SHA256=$giusta usb_lkl_scarica 2>/dev/null)"
  rm -f "$TOD_CACHE_DIR"/tod-lkl-*.tar.xz
  eq "impronta sbagliata: ignorato" "da internet" "$(TOD_ROOT=$r LKL_SHA256=0000 usb_lkl_scarica 2>/dev/null)"
}

test_usb_install() { # l'installazione: pacchetti, tod-lkl (scaricato o compilato), ntfs-3g nel container
  local d="$HOME/.local/share/tod-usb" f out
  export TOD_TERMUX_HOME=$HOME
  mkdir -p "$d"
  pkg_install() { echo "pkg $*" >>"$WORK/u_$$"; }
  net_download() { echo "scarica $1 $3" >>"$WORK/u_$$"; return "${SCARICA_RC:-0}"; }
  tar() { local f d=${!#}; for f in $LKL_FILE; do echo x >"$d/$f"; done; }   # l'ultimo argomento è la cartella
  bash() { echo "bash ${*: -1}" >>"$WORK/u_$$"; }
  cx_installed() { return 0; }
  cx_pkg_installed() { [[ $1 == gnome-disk-utility ]]; }
  cx_pkg_install() { echo "debian $*" >>"$WORK/u_$$"; }
  cx_root() { echo "debian via $*" >>"$WORK/u_$$"; }
  pkg_installed() { [[ $1 == qemu-utils ]]; }
  apt-get() { echo "termux $*" >>"$WORK/u_$$"; }
  desktop_dischi_entry() { echo "voce" >>"$WORK/u_$$"; }
  : >"$d/disco.img"; mkdir -p "$d/stato"; : >"$d/stato/memoria"
  usb_install
  out=$(cat "$WORK/u_$$")
  has "ncat, flock, libusb, termux-usb e gli strumenti dei dischi" "pkg nmap util-linux libusb termux-api fdisk dosfstools exfatprogs e2fsprogs" "$out"
  has "file compilati dalla release, con l'impronta" "scarica $LKL_URL $LKL_SHA256" "$out"
  for f in $LKL_FILE; do [[ -s $d/$f ]] || out+=" manca:$f"; done
  eq "file al loro posto" "" "$(grep -o 'manca:.*' <<<"$out")"
  has "NTFS dal container" "debian ntfs-3g" "$out"
  has "dischi in Esplora file già dal primo avvio" "bash monitor" "$out"
  has "voce Dischi" "voce" "$out"
  eq "la vecchia macchina via" "no" "$([[ -e $d/disco.img || -e $d/stato ]] && echo si || echo no)"
  has "GNOME Disks via dal container" "debian via gnome-disk-utility" "$out"
  has "QEMU via da Termux" "termux remove -y qemu-utils" "$out"
  : >"$WORK/u_$$"
  usb_install
  if grep -q '^scarica' "$WORK/u_$$"; then out=si; else out=no; fi
  eq "già pronti: niente download" no "$out"
  : >"$WORK/u_$$"; rm -f "$d/lkl-versione"
  SCARICA_RC=1 usb_install 2>/dev/null
  has "download fallito: si compila qui" "bash $ROOT/share/usb/lkl/costruisci.sh" "$(cat "$WORK/u_$$")"
  : >"$WORK/u_$$"
  conf_set USB_DISCHI 0
  usb_install
  eq "dischi spenti: niente" "" "$(cat "$WORK/u_$$")"
}

test_bv_answers() { # le scelte della schermata di benvenuto finiscono nella configurazione, controllate
  command -v jq >/dev/null || return 0
  BV_DIR="$WORK/bv_$$"; mkdir -p "$BV_DIR"
  bv_app_list >"$BV_DIR/app"
  has "catalogo per la schermata" "firefox|Firefox|on" "$(cat "$BV_DIR/app")"
  sleep 30 & BV_PID=$!; BV_ON=1
  printf '%s' '{"lingua":"es","nome":"Ada Rossi","utente":"ada","tema":"scuro","accento":"verde","app":["vlc","rm -rf","gimp"]}' >"$BV_DIR/risposte.json"
  bv_answers
  eq "utente" ada "$(conf_get USER_NAME)"
  eq "nome completo" "Ada Rossi" "$(conf_get USER_FULLNAME)"
  eq "lingua" es "$(conf_get LINGUA)"
  eq "tema" scuro "$(conf_get TEMA)"
  eq "accento in esadecimale" "#107c10" "$(conf_get ACCENTO)"
  eq "solo app del catalogo" "vlc gimp " "$(conf_get APPS)"
  eq "risposte segnate" 1 "$(conf_get BV_RISPOSTE)"
  conf_set BV_RISPOSTE 0
  printf '%s' '{"lingua":"xx-evil","nome":"","utente":"Root/../x","tema":"viola","accento":"nero","app":[]}' >"$BV_DIR/risposte.json"
  bv_answers
  eq "utente non valido ignorato" ada "$(conf_get USER_NAME)"
  eq "lingua non valida ignorata" es "$(conf_get LINGUA)"
  eq "tema non valido ignorato" scuro "$(conf_get TEMA)"
  kill "$BV_PID" 2>/dev/null; wait "$BV_PID" 2>/dev/null || true
  BV_ON=0 BV_PID=""
}

test_bv_note_e_comandi() { # stato per la schermata (solo l'id del passo) e Riprova/Esci da lì
  BV_DIR="$WORK/bvn_$$"; mkdir -p "$BV_DIR"; BV_LAST=""
  BV_ON=0; bv_note 10 desktop
  eq "spenta: niente stato" "" "$(cat "$BV_DIR/stato" 2>/dev/null)"
  BV_ON=1; bv_note 42 look
  has "percentuale" "pct=42" "$(cat "$BV_DIR/stato")"
  has "solo l'id del passo (i nomi li ha la schermata)" "passo=look" "$(cat "$BV_DIR/stato")"
  sleep 30 & BV_PID=$!
  ( sleep 1; echo riprova >"$BV_DIR/comando" ) &
  eq "Riprova dalla schermata" retry "$(bv_error_choice cx "Container debian" 50)"
  has "errore mostrato con l'id" "errore=cx" "$(cat "$BV_DIR/stato")"
  ( sleep 1; echo esci >"$BV_DIR/comando" ) &
  eq "Esci dalla schermata" quit "$(bv_error_choice cx "Container debian" 50)"
  kill "$BV_PID" 2>/dev/null; wait "$BV_PID" 2>/dev/null || true
  BV_ON=0 BV_PID=""
}

test_bv_nomi_dei_passi() { # la schermata ha un nome comprensibile per ogni passo dopo di lei (niente "v1", "xfce"...)
  local id ids
  ids=$(python3 -c 'import ast,sys; t=ast.parse(open(sys.argv[1]).read()); print(" ".join(k.value for n in ast.walk(t) if isinstance(n,ast.Assign) and getattr(n.targets[0],"id","")=="PASSI" for k in n.value.keys))' "$TOD_ROOT/share/tod-benvenuto.py")
  for id in desktop gpu cx cxgpu apps look lingua usbvm dischi integrate; do
    has "nome per $id" " $id " " $ids "
  done
}

test_bv_domande() { # con la schermata di benvenuto nel terminale si chiede solo ADB
  TUI_ON=1 TOD_YES=0 GPU_FAMILY=mali
  conf_set INSTALLED 0
  ui_yesno() { echo "si/no:$1" >>"$WORK/q_$$"; return 0; }
  ui_input() { echo "input:$1" >>"$WORK/q_$$"; echo utente; }
  ui_menu() { echo "menu:$1" >>"$WORK/q_$$"; echo xfce; }
  ui_checklist() { echo "lista:$1" >>"$WORK/q_$$"; }
  ui_msg() { :; }
  installer_questions
  eq "solo la domanda su ADB" "si/no:$(t q_ph_title)" "$(cat "$WORK/q_$$")"
  : >"$WORK/q_$$"; conf_set BENVENUTO 0
  installer_questions
  has "senza schermata: anche il nome utente" "input:$(t q_user_title)" "$(cat "$WORK/q_$$")"
  TUI_ON=0
}

test_preseed() { # primo avvio preparato: xfconfd privato, programmi di XFCE finti (niente barra sullo schermo)
  local f="$WORK/ps_$$"
  conf_set DE xfce
  have() { return 0; }
  x11_running() { return 1; }
  dbus-run-session() { { echo "PATH=$PATH"; command -v xfce4-panel; xfce4-panel -r && echo "finto ok"; echo "cmd ${*: -1}"; } >"$f"; }
  session_preseed
  has "comando interno" "cmd _preseed" "$(cat "$f")"
  has "xfce4-panel finto" "tod-preseed." "$(grep -v PATH= "$f")"
  has "e non fa niente" "finto ok" "$(cat "$f")"
  eq "cartella dei finti tolta" "" "$(compgen -G "$TMPDIR/tod-preseed.*" || true)"
  : >"$f"; conf_set DE kde
  session_preseed
  eq "KDE: niente" "" "$(cat "$f")"
}

test_cx_home_apps() { # app installate nella cartella personale del container (AppImage dei launcher)
  HR_ROOT="$WORK/hr_$$"; local h
  h="$HR_ROOT/home/dex"
  mkdir -p "$HR_ROOT/etc" "$h/.local/share/applications" "$h/.local/share/icons/hicolor/256x256/apps" "$h/Applications" "$HR_ROOT/usr/bin"
  cx_rootfs() { printf '%s' "$HR_ROOT"; }
  cx_distro() { echo debian; }
  desktop_refresh_caches() { :; }
  printf '[Desktop Entry]\nType=Application\nName=PDFCraft\nExec=/home/dex/Applications/pdfcraft.AppImage %%U\nIcon=pdfcraft\n' \
    >"$h/.local/share/applications/ai.storyteller.pdfcraft.desktop"
  printf '[Desktop Entry]\nType=Application\nName=Nostra\nExec=true\n' >"$h/.local/share/applications/tod-host-open.desktop"
  printf '[Desktop Entry]\nType=Application\nName=Fuori\nExec=/usr/bin/fuori\n' >"$h/.local/share/applications/fuori.desktop"
  : >"$h/.local/share/icons/hicolor/256x256/apps/pdfcraft.png"
  : >"$h/Applications/pdfcraft.AppImage"; : >"$HR_ROOT/usr/bin/fuori"
  cx_sync_home_apps
  local e="$HOME/.local/share/applications/tod-cx-ai.storyteller.pdfcraft.desktop"
  has "nello Start" "Exec=$PREFIX/bin/tod cx run -- /home/dex/Applications/pdfcraft.AppImage" "$(cat "$e")"
  has "con la sua icona" "Icon=$h/.local/share/icons/hicolor/256x256/apps/pdfcraft.png" "$(cat "$e")"
  eq "le voci nostre no" no "$([[ -e $HOME/.local/share/applications/tod-cx-tod-host-open.desktop ]] && echo si || echo no)"
  cx_remove_home_app ai.storyteller.pdfcraft
  eq "disinstallata: via l'AppImage" no "$([[ -e $h/Applications/pdfcraft.AppImage ]] && echo si || echo no)"
  eq "e la voce dello Start" no "$([[ -e $e ]] && echo si || echo no)"
  cx_remove_home_app fuori
  eq "mai file fuori dalla cartella personale" si "$([[ -e $HR_ROOT/usr/bin/fuori ]] && echo si || echo no)"
}

test_cartelle_dopo_risposte() { # nome utente scelto nella schermata dopo il desktop: barra laterale rifatta
  local bm
  cx_installed() { return 1; }
  conf_set USER_NAME ""
  files_setup
  bm="$(files_termux_home)/.config/gtk-3.0/bookmarks"
  eq "senza nome utente: niente cartelle" "" "$(cat "$bm" 2>/dev/null)"
  conf_set USER_NAME ada
  installer_user_folders
  has "Download nella barra laterale" "/ada/$(t fm_downloads)" "$(cat "$bm")"
  has "e in user-dirs" "XDG_DOWNLOAD_DIR=" "$(cat "$(files_termux_home)/.config/user-dirs.dirs")"
}

test_telefono_spente() { # app del telefono spente: il generatore toglie le voci e si ferma, "aggiorna" non le rifà
  local th="$WORK/tel_$$"
  mkdir -p "$th/.config/tod" "$th/.local/share/applications"
  printf 'ANDROID_APPS=0\n' >"$th/.config/tod/tod.conf"
  : >"$th/.local/share/applications/tod-android-com.esempio.desktop"
  : >"$th/.local/share/applications/firefox.desktop"
  TOD_TERMUX_HOME="$th" python3 "$ROOT/share/tod-android-apps.py"
  eq "voci del telefono tolte" no "$([[ -e $th/.local/share/applications/tod-android-com.esempio.desktop ]] && echo si || echo no)"
  eq "le altre restano" si "$([[ -e $th/.local/share/applications/firefox.desktop ]] && echo si || echo no)"
  conf_set ANDROID_APPS 0
  python3() { echo "generatore partito" >>"$WORK/tel_$$.log"; }
  android_apps_cli aggiorna
  eq "spente: aggiorna non le rifà" "" "$(cat "$WORK/tel_$$.log" 2>/dev/null)"
}

test_lavoro_in_sottofondo() { # il sottofondo non toglie fluidità: priorità minima, controlli meno frequenti
  eq "priorità minima" 19 "$(low_prio sh -c 'ps -o ni= -p $$' | tr -d ' ')"
  has "app del telefono" "low_prio setsid python3" "$(declare -f android_apps_sync)"
  has "stato del telefono" "low_prio setsid python3" "$(declare -f phone_status_start)"
  has "aggiornamenti" "low_prio setsid python3" "$(declare -f updates_start)"
  has "primo controllo aggiornamenti dopo 10 minuti" "FIRST_CHECK, EVERY = 600" "$(cat "$ROOT/share/tod-updates.py")"
}

test_prestazioni() { # tod prestazioni: resoconto leggibile in Download
  local out
  mkdir -p "$HOME/storage/downloads"
  out=$(TOD_TERMUX_HOME="$HOME" LANGUAGE=it python3 "$ROOT/share/tod-prestazioni.py" 1 2>&1)
  has "processore" "Processore usato da noi" "$out"
  has "memoria" "Memoria libera" "$out"
  eq "salvato in Download" 1 "$(ls "$HOME"/storage/downloads/tod-prestazioni-*.txt | wc -l)"
  # app di Debian senza proot: il nome vero, non il caricatore (prima tutte insieme sotto "ld.so")
  out=$(python3 - "$ROOT/share/tod-prestazioni.py" <<'PYEOF2'
import re, sys
src = open(sys.argv[1]).read()
def T(it, en): return it
exec(re.search(r"^def gruppo\(.*?(?=^\S)", src, re.S | re.M).group(0))
ld, r = "/h/.local/share/tod-diretto/ld.so", "/rootfs"
print(gruppo("\0".join([ld, "--argv0", "/usr/lib/app-electron/app-electron", r + "/usr/lib/app-electron/app-electron", "--type=zygote"]), "ld.so"))
print(gruppo("\0".join([ld, "--argv0", "/usr/bin/python3", r + "/usr/bin/python3", "/usr/bin/wordcraft"]), "ld.so"))
PYEOF2
)
  eq "senza proot: nomi veri" "app-electron|python3 wordcraft" "$(echo "$out" | paste -sd'|')"
}

test_apk_adb() { # con ADB collegato Termux:X11 e Termux:API si installano da sole, sennò a mano
  TOD_X11_APK="$WORK/x11_$$.apk"; TOD_X11_DEB="$WORK/x11_$$.deb"; TOD_API_APK="$WORK/api_$$.apk"
  have() { [[ $1 == adb ]] || command -v "$1" >/dev/null; }
  shizuku_ready() { return 1; }
  timeout() { shift; "$@"; }
  run_step() { shift; "$@"; }
  x11_variant() { echo shareduid; }
  x11_uid_status() { echo shared; }
  api_from_github() { return 0; }
  api_permissions() { :; }
  x11_app_download() { echo apk >"$TOD_X11_APK"; }
  api_app_download() { echo apk >"$TOD_API_APK"; }
  x11_app_installed() { [[ -f $WORK/inst_x11_$$ ]]; }
  api_app_installed() { [[ -f $WORK/inst_api_$$ ]]; }
  ui_msg() { echo "msg $2" >>"$WORK/a_$$"; }
  termux-open() { echo "apri" >>"$WORK/a_$$"; touch "$WORK/inst_x11_$$" "$WORK/inst_api_$$"; }
  am() { :; }
  adb() {
    case $1 in
      devices) printf 'List of devices attached\n127.0.0.1:40123\tdevice\n' ;;
      -s) case $3 in
            push) echo "${4##*/}" >"$WORK/push_$$" ;;
            shell) if [[ $4 == settings ]]; then
                     echo "impostazione $5 ${*:7}" >>"$WORK/set_$$"
                     if [[ $5 == get ]]; then echo 1; fi
                   elif [[ $4 == pm ]]; then
                     echo "adb install $(cat "$WORK/push_$$")" >>"$WORK/a_$$"
                     case $(cat "$WORK/push_$$") in x11*) touch "$WORK/inst_x11_$$" ;; api*) touch "$WORK/inst_api_$$" ;; esac
                     echo Success
                   fi ;;
          esac ;;
    esac
  }
  x11_app_ensure >/dev/null 2>&1; api_app_ensure >/dev/null 2>&1
  eq "con ADB: installate da sole, nessuna finestra" "adb install x11_$$.apk"$'\n'"adb install api_$$.apk" "$(cat "$WORK/a_$$")"
  eq "verifica di Play Protect spenta e poi rimessa com'era" "impostazione get verifier_verify_adb_installs"$'\n'"impostazione put verifier_verify_adb_installs 0"$'\n'"impostazione put verifier_verify_adb_installs 1" "$(sed -n 1,3p "$WORK/set_$$")"
  rm -f "$WORK/a_$$" "$WORK/inst_x11_$$" "$WORK/inst_api_$$"
  adb() { [[ $1 == devices ]] && echo "List of devices attached"; }
  x11_app_ensure >/dev/null 2>&1
  has "senza ADB: lo dice" "ADB non è collegato" "$(cat "$WORK/a_$$")"
  has "senza ADB: si apre l'installazione" "apri" "$(cat "$WORK/a_$$")"
}

test_api_app_download() { # Termux:API dalla release di GitHub, l'APK "github", controllato con lo sha256
  command -v jq >/dev/null && command -v sha256sum >/dev/null || return 0
  local sum
  net_get() {
    case $1 in
      *releases/latest) printf '%s' '{"tag_name":"v0.53.0","assets":[
        {"name":"termux-api-app_v0.53.0+fdroid.apk","browser_download_url":"https://x/fdroid.apk"},
        {"name":"termux-api-app_v0.53.0+github.debug.apk","browser_download_url":"https://x/termux-api-app_v0.53.0%2Bgithub.debug.apk"},
        {"name":"checksums-sha256.txt","browser_download_url":"https://x/sums"}]}' ;;
      */sums) printf '%s  termux-api-app_v0.53.0+github.debug.apk\n' "$sum" ;;
    esac
  }
  net_download() { echo "$1" >"$WORK/url_$$"; printf 'apk vero' >"$2"; }
  sum=$(printf 'apk vero' | sha256sum | cut -d' ' -f1)
  TOD_API_APK="$WORK/api_$$.apk"
  api_app_download
  eq "l'APK della build GitHub" "https://x/termux-api-app_v0.53.0%2Bgithub.debug.apk" "$(cat "$WORK/url_$$")"
  eq "checksum giusto: si tiene" "apk vero" "$(cat "$TOD_API_APK")"
  sum=0000
  local rc=0
  api_app_download 2>/dev/null || rc=$?
  eq "checksum sbagliato: errore" 1 "$rc"
  eq "e il file non resta" "" "$(ls "$TOD_API_APK" 2>/dev/null)"
}

test_desktop_pacchetti_nostri() { # i pacchetti delle parti nostre, chiesti per nome e con il nome giusto
  pkg_has() { return 0; }
  pkg_install() { echo "$*" >>"$WORK/pk_$$"; }
  pkg_install_avail() { echo "$*" >>"$WORK/pk_$$"; }
  desktop_preconfigure() { :; }
  desktop_install xfce
  local tutto; tutto=$(cat "$WORK/pk_$$")
  has "pygobject (Start, Impostazioni...)" "pygobject" "$tutto"
  has "gvfs (dischi in Esplora file)" "gvfs" "$tutto"
  has "termux-api (batteria, notifiche)" "termux-api" "$tutto"
  # in Termux "python-gobject" non esiste: chiederlo non installava niente, e su un telefono nuovo
  # le nostre finestre non partivano
  eq "nessuno chiede python-gobject" "" "$(grep -rn 'install[a-z_]* .*python-gobject' "$ROOT/lib" "$ROOT/tod" || true)"
  local base
  base=$(desktop_packages xfce)
  has "Fluent GTK: la base di DeX OS" "fluent-gtk-theme" "$(grep -A5 '^look_apply_windows11()' "$ROOT/lib/look.sh")"
  local p
  for p in xfce4-whiskermenu-plugin xfce4-pulseaudio-plugin xfce4-clipman-plugin xfce4-docklike-plugin \
    xfce4-genmon-plugin fluent-icon-theme; do   # la barra di XFCE non c'è più: niente plugin né icone Fluent
    hasnt "niente $p" "$p" "$base $(grep -A5 '^look_apply_windows11()' "$ROOT/lib/look.sh")"
  done
}

test_spegni_subito() { # "tod spegni --subito" (dalla finestrella dello spegnimento) spegne davvero,
  # non riapre la finestrella: prima "--subito" si cercava al posto sbagliato e le due si richiamavano
  eval "$(sed -n '/^tod_main() {/,/^}/p' "$ROOT/tod")"   # solo la funzione, senza eseguire tod
  setsid() { echo "$*" >>"$WORK/spegni_$$"; }
  have() { return 0; }
  local DISPLAY=:9
  # tod spegni lancia in sottofondo (&): si aspetta prima di leggere, sennò è una gara
  tod_main spegni --subito; wait
  # servizi e desktop, poi anche Termux (_spegni = session_stop + termux_exit, 4 novembre)
  has "--subito: servizi, desktop e Termux" "tod _spegni" "$(cat "$WORK/spegni_$$")"
  : >"$WORK/spegni_$$"
  TOD_SPEGNIMENTO=1 tod_main spegni; wait
  has "lanciato dalla finestrella: mai di nuovo la finestrella" "tod _spegni" "$(cat "$WORK/spegni_$$")"
  : >"$WORK/spegni_$$"
  tod_main spegni; wait
  has "dal desktop: prima le app" "tod-spegni.py" "$(cat "$WORK/spegni_$$")"
}

test_apt_strumenti_dischi() { # gli strumenti dei dischi sono pacchetti come gli altri: Termux, sennò il container
  pkg_has() { [[ $1 == e2fsprogs || $1 == htop ]]; }
  pkg_installed() { return 1; }
  cx_installed() { return 0; }
  cx_pkg_list() { :; }
  cx_has() { [[ $1 == ntfs-3g ]]; }
  apt_native() { echo "termux:$*" >>"$WORK/aptd_$$"; }
  cx_apt() { echo "debian:$*" >>"$WORK/aptd_$$"; }
  cx_sync_apps() { :; }
  : >"$WORK/aptd_$$"
  apt_cli install e2fsprogs ntfs-3g htop >/dev/null
  eq "Termux per quel che ha, NTFS dal container" $'termux:install e2fsprogs htop\ndebian:install ntfs-3g' "$(cat "$WORK/aptd_$$")"
}

test_app_one_system() { # la stessa app non si installa né compare in due sistemi
  command -v awk >/dev/null || return 0
  eq "chiavi" "firefox vlc vscode gimp obs libreoffice-writer" \
    "$(printf 'firefox-esr\nvlc-qt\ncode-oss\norg.gimp.GIMP.desktop\ncom.obsproject.Studio.desktop\nlibreoffice-writer\n' |
      awk -f "$ROOT/share/tod-app-keys.awk" -v mode=names | tr '\n' ' ' | sed 's/ $//')"
  # Termux finto: firefox installato, con la sua voce di menu; una nascosta non conta
  local info="$PREFIX/var/lib/dpkg/info" apps="$PREFIX/share/applications"
  mkdir -p "$info" "$apps"
  printf '%s\n' "$apps/firefox.desktop" >"$info/firefox.list"
  printf '[Desktop Entry]\nName=Firefox\n' >"$apps/firefox.desktop"
  printf '%s\n' "$apps/gcr-viewer.desktop" >"$info/gcr.list"
  printf '[Desktop Entry]\nName=Gcr\nNoDisplay=true\n' >"$apps/gcr-viewer.desktop"
  cx_installed() { return 1; }
  pkg_installed() { return 1; }
  eq "firefox-esr è già firefox" firefox "$(app_owner firefox-esr)"
  eq "componente nascosto: non è un'app" "" "$(app_owner gcr)"
  pkg_has() { return 0; }
  apt_native() { echo "termux:$*" >>"$WORK/one_$$"; }
  apt_cli install firefox-esr >"$WORK/one_err_$$" 2>&1
  eq "niente seconda copia" "" "$(cat "$WORK/one_$$" 2>/dev/null)"
  has "spiega perché" "già installata (firefox)" "$(cat "$WORK/one_err_$$")"
  # menu: la voce del container non compare se Termux ha già l'app
  _fake_rootfs
  export TOD_TERMUX_HOME=$HOME
  mkdir -p "$R/var/lib/dpkg/info" "$R/usr/share/applications"
  printf '/usr/share/applications/firefox-esr.desktop\n' >"$R/var/lib/dpkg/info/firefox-esr.list"
  printf '[Desktop Entry]\nName=Firefox ESR\nExec=firefox-esr\n' >"$R/usr/share/applications/firefox-esr.desktop"
  desktop_refresh_caches() { :; }
  cx_export_pkgs firefox-esr
  eq "niente doppione nello Start" no "$([[ -e $HOME/.local/share/applications/tod-cx-firefox-esr.desktop ]] && echo si || echo no)"
  # il controllo dentro apt di Debian
  conf_set CX_PKEXEC 0
  cx_install_runner
  local g="$R/usr/local/bin/tod-app-guard" rc=0
  sh -n "$g"
  printf 'VERSION 2\nCommandLine::AsString=apt-get%%20install%%20-o%%20Debug::X=1%%20-y%%20firefox-esr\n\nfirefox-esr - < 128.0 /var/cache/apt/archives/f.deb\nlibfoo - < 1.0 /x/l.deb\n' |
    sh "$g" 2>"$WORK/guard_$$" || rc=$?
  eq "apt di Debian si ferma" 1 "$rc"
  has "e lo dice" "firefox-esr" "$(cat "$WORK/guard_$$")"
  rc=0
  printf 'VERSION 2\nCommandLine::AsString=apt install foo\n\nfirefox-esr - < 128.0 /x/f.deb\nfoo - < 1.0 /x/foo.deb\n' | sh "$g" || rc=$?
  eq "le dipendenze passano" 0 "$rc"
  rc=0
  printf 'VERSION 2\nCommandLine::AsString=apt install firefox-esr\n\nfirefox-esr - < 128.0 /x/f.deb\n' | TOD_MENU_SYNC=0 sh "$g" || rc=$?
  eq "da tod apt: già controllato" 0 "$rc"
  # la stessa app, se Termux ce l'ha con un altro nome, viene da Termux
  rm -f "$info/firefox.list"
  pkg_has() { [[ $1 == firefox ]]; }
  : >"$WORK/one_$$"
  apt_cli install firefox-esr >/dev/null 2>&1
  eq "firefox-esr → firefox di Termux" "termux:install firefox" "$(cat "$WORK/one_$$")"
}

test_menu_only_useful() { # nello Start solo ciò che serve: niente demo, doppioni, dipendenze
  local sys="$PREFIX/share/applications" apps="$HOME/.local/share/applications"
  mkdir -p "$sys"
  printf '[Desktop Entry]\nName=Xarchiver\nExec=xarchiver %%f\nMimeType=application/zip;\n' >"$sys/xarchiver.desktop"
  printf '[Desktop Entry]\nName=Mousepad\n' >"$sys/org.xfce.mousepad.desktop"
  desktop_hide_useless
  has "Xarchiver nascosto" "NoDisplay=true" "$(cat "$apps/xarchiver.desktop")"
  has "ma apre ancora gli archivi" "MimeType=application/zip;" "$(cat "$apps/xarchiver.desktop")"
  eq "Mousepad resta" no "$([[ -e $apps/org.xfce.mousepad.desktop ]] && echo si || echo no)"
  printf '[Desktop Entry]\nName=Mio\n' >"$apps/thunar-settings.desktop"
  printf '[Desktop Entry]\nName=Thunar\n' >"$sys/thunar-settings.desktop"
  desktop_hide_useless
  eq "una scelta dell'utente non si tocca" "[Desktop Entry]
Name=Mio" "$(cat "$apps/thunar-settings.desktop")"
  # il resto del desktop: energia, posta, terminale in italiano, barra bloccata
  mkdir -p "$PREFIX/etc/xdg/autostart" "$PREFIX/share/Thunar/sendto" "$HOME/.config/Thunar"
  : >"$PREFIX/etc/xdg/autostart/xfce4-power-manager.desktop"
  : >"$PREFIX/share/Thunar/sendto/thunar-sendto-email.desktop"
  printf '<actions>\n<action>\n\t<name>Open Terminal Here</name>\n</action>\n</actions>\n' >"$HOME/.config/Thunar/uca.xml"
  desktop_hide_useless; desktop_hide_useless
  has "gestore dell'energia spento" "Hidden=true" "$(cat "$HOME/.config/autostart/xfce4-power-manager.desktop")"
  has "niente Invia a posta" "Hidden=true" "$(cat "$HOME/.local/share/Thunar/sendto/thunar-sendto-email.desktop")"
  eq "Apri terminale qui, una volta" 1 "$(grep -c '<name xml:lang="it">Apri terminale qui</name>' "$HOME/.config/Thunar/uca.xml")"
  has "rientro conservato" $'\t<name xml:lang="it">' "$(cat "$HOME/.config/Thunar/uca.xml")"
  desktop_tune_files
  has "barra bloccata" "CustomizePanel=NONE" "$(cat "$PREFIX/etc/xdg/xfce4/kiosk/kioskrc")"
  has "spegnimento ancora tolto" "Shutdown=NONE" "$(cat "$PREFIX/etc/xdg/xfce4/kiosk/kioskrc")"
  # container: le app arrivate come dipendenze non entrano nello Start
  _fake_rootfs
  export TOD_TERMUX_HOME=$HOME
  mkdir -p "$R/var/lib/dpkg/info" "$R/usr/share/applications" "$R/var/lib/apt"
  printf '/usr/share/applications/kdesystemsettings.desktop\n' >"$R/var/lib/dpkg/info/systemsettings.list"
  printf '[Desktop Entry]\nName=KDE\nExec=systemsettings\n' >"$R/usr/share/applications/kdesystemsettings.desktop"
  printf 'Package: systemsettings\nArchitecture: arm64\nAuto-Installed: 1\n\n' >"$R/var/lib/apt/extended_states"
  desktop_refresh_caches() { :; }
  cx_export_pkgs systemsettings
  eq "dipendenza: niente voce" no "$([[ -e $apps/tod-cx-kdesystemsettings.desktop ]] && echo si || echo no)"
  printf 'Package: systemsettings\nArchitecture: arm64\nAuto-Installed: 0\n\n' >"$R/var/lib/apt/extended_states"
  cx_export_pkgs systemsettings
  eq "scelta dall'utente: c'è" si "$([[ -e $apps/tod-cx-kdesystemsettings.desktop ]] && echo si || echo no)"
}

test_dev_tools() { # modalità sviluppo: ponte e ADB all'avvio, solo se accesa
  local calls="$WORK/dev_$$"
  # tools/ (strumenti di lavoro) non è nel repository pubblico: un ponte finto
  TOD_ROOT="$WORK/devroot_$$"; mkdir -p "$TOD_ROOT/tools"; : >"$TOD_ROOT/tools/termux-bridge"
  setsid() { echo "$*" >>"$calls"; }
  have() { [[ $1 == adb ]]; }
  conf_set SVILUPPO 0
  dev_tools_start
  eq "spenta: niente" "" "$(cat "$calls" 2>/dev/null)"
  conf_set SVILUPPO 1
  dev_tools_start
  has "ponte avviato" "tools/termux-bridge" "$(cat "$calls")"
  has "con la home di Termux" "HOME=" "$(cat "$calls")"
  has "ADB in sottofondo" "tod _dev_adb" "$(cat "$calls")"
  adb() { case $1 in devices) printf 'List of devices attached\n127.0.0.1:41234\tdevice\n' ;; esac; }
  has "ADB già collegato" "ADB collegato (127.0.0.1:41234)" "$(dev_adb_reconnect)"
  unset -f setsid have adb
}

test_open_host() { # le app di Debian aprono link e file con le app del desktop, mai comandi
  mkdir -p "$TMPDIR" "$WORK/oh_$$/bin" "$WORK/oh_$$/root/home/dex"
  printf '#!/bin/sh\necho "$1" >>"%s"\n' "$WORK/oh_$$/opened" >"$WORK/oh_$$/bin/xdg-open"
  chmod +x "$WORK/oh_$$/bin/xdg-open"
  : >"$WORK/oh_$$/root/home/dex/nota.txt"
  printf '#!/bin/sh\n' >"$WORK/oh_$$/root/home/dex/script.sh"; chmod +x "$WORK/oh_$$/root/home/dex/script.sh"
  TOD_OPEN_CMD="$WORK/oh_$$/bin/xdg-open" TOD_CX_ROOTFS="$WORK/oh_$$/root" bash "$ROOT/share/tod-open-host" >"$WORK/oh_$$/log" 2>&1 &
  local svc=$! i
  for ((i = 0; i < 50; i++)); do [[ -p $TMPDIR/tod-open.fifo ]] && break; sleep 0.1; done
  printf '%s\n' "https://accounts.google.com/x" "/home/dex/nota.txt" "/home/dex/script.sh" "rm -rf /" >"$TMPDIR/tod-open.fifo"
  for ((i = 0; i < 50; i++)); do [[ $(grep -c . "$WORK/oh_$$/opened" 2>/dev/null) -ge 2 ]] && break; sleep 0.1; done
  sleep 0.3
  kill "$svc" 2>/dev/null || true
  has "link al browser del desktop" "https://accounts.google.com/x" "$(cat "$WORK/oh_$$/opened")"
  has "file del container col percorso di Termux" "$WORK/oh_$$/root/home/dex/nota.txt" "$(cat "$WORK/oh_$$/opened")"
  eq "mai eseguibili né comandi" 2 "$(grep -c . "$WORK/oh_$$/opened")"
  has "rifiuti nel registro" "rifiutato" "$(cat "$WORK/oh_$$/log")"
  # xdg-open del desktop: le app del desktop (xdg-utils), non termux-open (app di Android)
  mkdir -p "$WORK/oh_$$/pfx/bin"
  printf '#!/bin/sh
echo "desktop $1"
' >"$WORK/oh_$$/pfx/bin/xdg-utils-xdg-open"
  printf '#!/bin/sh
echo "android $1"
' >"$WORK/oh_$$/pfx/bin/termux-open"
  chmod +x "$WORK/oh_$$/pfx/bin/"*
  eq "link alle app del desktop" "desktop app://x" "$(PREFIX="$WORK/oh_$$/pfx" sh "$ROOT/share/bin/xdg-open" app://x)"
  rm -f "$WORK/oh_$$/pfx/bin/xdg-utils-xdg-open"
  eq "senza xdg-utils: Android" "android app://x" "$(PREFIX="$WORK/oh_$$/pfx" sh "$ROOT/share/bin/xdg-open" app://x)"
  # nel container: xdg-open che passa dal desktop, e browser predefinito per GIO
  _fake_rootfs
  mkdir -p "$R/home/dex/.config"
  printf '[Default Applications]\nx-scheme-handler/app=org.esempio.App.desktop\nx-scheme-handler/https=firefox.desktop\n' \
    >"$R/home/dex/.config/mimeapps.list"
  conf_set CX_PKEXEC 0
  cx_install_runner; cx_install_runner
  sh -n "$R/usr/local/bin/xdg-open"
  has "xdg-open del container" "tod-open.fifo" "$(cat "$R/usr/local/bin/xdg-open")"
  # senza proot /tmp è quella del container: la FIFO di Termux arriva da TMPDIR (26 ottobre: il login di
  # un'app non apriva più il browser). Copia con un /tmp finto: mai scrivere nella FIFO vera del desktop
  mkdir -p "$WORK/xo_$$/tmp"
  sed "s|/tmp/tod-open.fifo|$WORK/xo_$$/nessuna.fifo|" "$R/usr/local/bin/xdg-open" >"$WORK/xo_$$/xdg-open"
  mkfifo "$WORK/xo_$$/tmp/tod-open.fifo"
  (cat "$WORK/xo_$$/tmp/tod-open.fifo" >"$WORK/xo_$$/letto") &
  TMPDIR="$WORK/xo_$$/tmp" sh "$WORK/xo_$$/xdg-open" https://example.com/login >/dev/null 2>&1 || true
  # se non ha scritto nella FIFO, il lettore resterebbe appeso: lo si sblocca
  timeout 2 sh -c ': >"$1"' _ "$WORK/xo_$$/tmp/tod-open.fifo" 2>/dev/null || true
  wait
  eq "senza proot: la FIFO da TMPDIR" "https://example.com/login" "$(cat "$WORK/xo_$$/letto")"
  eq "https al desktop, una volta" 1 "$(grep -c '^x-scheme-handler/https=tod-host-open.desktop' "$R/home/dex/.config/mimeapps.list")"
  has "gli altri link restano alla loro app" "x-scheme-handler/app=org.esempio.App.desktop" "$(cat "$R/home/dex/.config/mimeapps.list")"
  eq "il vecchio browser tolto" 0 "$(grep -c firefox "$R/home/dex/.config/mimeapps.list")"
}

test_updates_progress() { # avanzamento vero da apt: MB scaricati, velocità, installazione
  has "stato di apt" "APT::Status-Fd=1" "$(cat "$ROOT/share/tod-updates.py")"
  eq "dimensioni di apt in italiano e inglese" "123000000 1234000 1234000 12300000 12300000" "$(python3 -c "
import importlib.util
sp = importlib.util.spec_from_file_location('u', '$ROOT/share/tod-updates.py'); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
print(' '.join(str(int(m.parse_size(*a))) for a in (('123', 'MB'), ('1.234', 'kB'), ('1,234', 'kB'), ('12,3', 'MB'), ('12.3', 'MB'))))")"
}

test_usb_lkl_servizio() { # il servizio: chi serve una partizione, dischi da non richiedere, niente macchina
  local th="$HOME/thlkl" run="$HOME/runlkl" u="$ROOT/share/usb/tod-usb" f out
  mkdir -p "$th/.config/tod" "$th/.local/share/tod-usb" "$run"
  f=$(sed -n '/^lkl_di() {/,/^}/p; /^VISTI=/p; /^usb_nuovi() {/,/^}/p' "$u")
  local base=(env TH="$th" DATA="$th/.local/share/tod-usb" LKLBIN="$th/.local/share/tod-usb/tod-lkl" RUN="$run")
  printf 'DATI\tusb1\tChiavetta USB\tX\t1\tvfat\tDATI\tA-B\nFOTO\tusb1\tChiavetta USB\tX\t1\texfat\tFOTO\tC-D\n' >"$run/lkl-usb1.volumi"
  printf 'DATI 2\tusb2\tChiavetta USB\tX\t1\tvfat\tDATI\tA-B\n' >"$run/lkl-usb2.volumi"
  eq "partizione del primo disco" "usb1" "$("${base[@]}" bash -c "$f"$'\n''lkl_di FOTO')"
  eq "stesso nome con il numero: il secondo" "usb2" "$("${base[@]}" bash -c "$f"$'\n''lkl_di "DATI 2"')"
  eq "partizione sconosciuta: nessuno" "" "$("${base[@]}" bash -c "$f"$'\n''lkl_di ALTRO')"
  # dispositivi: già visti, espulsi e già serviti non si richiedono; un espulso scollegato si dimentica
  echo file:/a >"$run/espulsi"; echo file:/b >"$run/lkl-dev-usb3"
  out=$("${base[@]}" bash -c "is_storage() { return 0; }"$'\n'"$f"$'\n''VISTI=file:/c; usb_nuovi "$(printf "file:/a\nfile:/b\nfile:/c\nfile:/d")"')
  eq "solo il disco nuovo" "file:/d" "$out"
  "${base[@]}" bash -c "is_storage() { return 0; }"$'\n'"$f"$'\n''usb_nuovi file:/d' >/dev/null
  eq "espulso e scollegato: dimenticato" "" "$(cat "$run/espulsi")"
  hasnt "niente più macchina dei dischi" "qemu" "$(tr 'A-Z' 'a-z' <"$u" | grep -v 'qemu, fino al\|tolta il')"
  # i resti della macchina: il blocco in ~/.ssh/config e i canali vanno via, il resto di config resta
  mkdir -p "$th/.ssh"; printf 'Host mio\n# tod-usb inizio\nHost tod-usb\n  Port 2222\n# tod-usb fine\n' >"$th/.ssh/config"
  : >"$run/canale-1.sock"
  "${base[@]}" TMPDIR="$HOME" bash -c "$(sed -n '/^pulisci_macchina() {/,/^}/p' "$u")"$'\n''pulisci_macchina'
  eq "config di ssh ripulito" "Host mio" "$(cat "$th/.ssh/config")"
  eq "canali via" no "$([[ -e $run/canale-1.sock ]] && echo si || echo no)"
  has "tod-lkl smonta tutto alla fine della sessione" "lkl_ferma_tutti" "$(sed -n '/^ferma() {/,/^}/p' "$u")"
}

test_tod_disco_lkl() { # comandi da terminale con LKL: lsblk/blkid dai dati di tod-lkl, pausa per chi cambia il disco
  local run="$HOME/tmpdisco/tod-usb" bin="$HOME/bindisco" so="$HOME/disco.so" w="$ROOT/share/bin/tod-disco" out
  mkdir -p "$run" "$bin" "$PREFIX/bin"
  # una libreria vera qualsiasi al posto del disco finto (un file vuoto fa protestare il linker a ogni comando)
  local l; for l in /usr/lib/aarch64-linux-gnu/libm.so.6 /lib/*/libm.so.6 /system/lib64/libm.so; do [[ -f $l ]] && { cp "$l" "$so"; break; }; done
  [[ -f $so ]] || : >"$so"
  printf '0\t0\t8000000000\tChiavetta USB\tDataTraveler\n1\t1048576\t4000000000\tvfat\tDATI\tAB12-CD34\tDATI\n2\t4001048576\t3000000000\tvfat\t\tEF56-7890\t-\n' >"$run/lkl-usb3.parti"
  printf '#!/bin/sh\necho "$@" >>%s/ncat.log; echo ok\n' "$bin" >"$bin/ncat"
  printf '#!/bin/sh\necho "fdisk $*"\n' >"$PREFIX/bin/fdisk"
  printf '#!/bin/sh\necho "mkfs.vfat $* (preload: $LD_PRELOAD)"\n' >"$PREFIX/bin/mkfs.vfat"
  chmod +x "$bin/ncat" "$PREFIX/bin/fdisk" "$PREFIX/bin/mkfs.vfat"
  local e=(env PATH="$bin:$PATH" PREFIX="$PREFIX" TMPDIR="$HOME/tmpdisco" TOD_DISCO_SO="$so")
  out=$("${e[@]}" bash "$w" lsblk)
  has "il disco" "sda         7.5G    0 disk" "$out"
  has "partizione montata" "/media/usb/DATI" "$out"
  hasnt "non montata: niente cartella" "/media/usb/-" "$out"
  out=$("${e[@]}" bash "$w" blkid)
  has "etichetta e uuid" '/dev/sda1: LABEL="DATI" UUID="AB12-CD34" TYPE="vfat"' "$out"
  has "senza etichetta: i campi non scivolano" '/dev/sda2: UUID="EF56-7890" TYPE="vfat"' "$out"
  eq "fdisk -l: i nostri dischi" "fdisk -l /dev/sda" "$("${e[@]}" bash "$w" fdisk -l)"
  out=$("${e[@]}" bash "$w" mkfs.vfat -F 32 /dev/sda2)
  has "il programma vero col disco finto" "mkfs.vfat -F 32 /dev/sda2 (preload: $so" "$out"
  eq "chi cambia il disco: pausa e ripresa sul suo disco" "$(printf -- '-U %s\n-U %s' "$run/lkl-usb3.ctl" "$run/lkl-usb3.ctl")" "$(cat "$bin/ncat.log")"
  : >"$bin/ncat.log"; "${e[@]}" bash "$w" fdisk -l /dev/sda >/dev/null
  eq "fdisk -l solo legge: niente pausa" "" "$(cat "$bin/ncat.log")"
  out=$("${e[@]}" bash "$w" mkntfs /dev/sda2 2>&1 || true)
  has "programma che Termux non ha: come installarlo" "sudo apt install ntfs-3g" "$out"
}

test_tod_dischi_dati() { # Dischi: dischi dai file di tod-lkl (stesso ordine di tod-disco), spazio libero, azioni
  python3 -c "import gi; gi.require_version('Gtk', '3.0')" 2>/dev/null || return 0
  local run="$HOME/tmpdischi/tod-usb" out
  mkdir -p "$run"
  printf '0\t0\t1000000000\tScheda SD\tLettore\tdos\n1\t1048576\t400000000\texfat\tFOTO\tAB-CD\tFOTO\n' >"$run/lkl-usb10.parti"
  printf '1\t100000000\t399000000\n' >"$run/lkl-usb10.spazio"
  printf '0\t0\t2000000000\tChiavetta USB\tKingston\t\n' >"$run/lkl-usb2.parti"
  out=$(TMPDIR="$HOME/tmpdischi" LANGUAGE=it python3 - "$ROOT/share/tod-dischi.py" <<'EOF'
import importlib.util as u, sys
s = u.spec_from_file_location("d", sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)
dd = m.leggi_dischi()
print([(d["id"], d["dev"]) for d in dd])
k = dd[1]
print(k["parti"][0]["dev"], k["parti"][0]["spazio"], [(i, n // 1000000) for i, n in k["liberi"]])
print(dd[0]["liberi"], m.misura(1500000000))
EOF
)
  has "ordine come tod-disco: usb2 = sda, usb10 = sdb" "[('usb2', '/dev/sda'), ('usb10', '/dev/sdb')]" "$out"
  has "partizione, spazio usato e libero dopo (allineato a 1 MiB)" "/dev/sdb1 (100000000, 399000000) [(401604608, 597)]" "$out"
  has "senza tabella: niente spazio libero da usare" "[] 1,5 GB" "$out"
  has "Dischi è il nostro" "tod-dischi.py" "$(sed -n '/^dischi() {/,/^}/p' "$ROOT/share/usb/tod-usb")"
  # 1° novembre: con il desktop in inglese la finestra mostrava "Chiavetta USB" (nome interno di tod-lkl)
  out=$(LANGUAGE=en LANG=en_US.UTF-8 python3 - "$ROOT/share/tod-dischi.py" <<'EOF'
import importlib.util as u, sys
s = u.spec_from_file_location("d", sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)
print(m.tipo_testo("Chiavetta USB"), "|", m.tipo_testo("Scheda SD"), "|", m.tipo_testo("altro"))
EOF
)
  eq "tipo del disco tradotto in inglese" "USB stick | SD card | altro" "$out"
}

test_usb_notifiche_lingua() { # 1° novembre: le notifiche del servizio dei dischi erano solo in italiano
  eval "$(sed -n '/^msg() {/p' "$ROOT/share/usb/tod-usb")"
  eq "notifica in inglese" "You can unplug the drive." "$(LANGUAGE=en msg "Puoi scollegare il disco." "You can unplug the drive.")"
  eq "notifica in italiano" "Puoi scollegare il disco." "$(LANGUAGE=it_IT msg "Puoi scollegare il disco." "You can unplug the drive.")"
  eq "senza lingua: inglese" "B" "$(LANGUAGE='' LANG='' msg A B)"
  hasnt "nessuna notifica con il testo scritto fisso" 'notify "$name" "P' "$(cat "$ROOT/share/usb/tod-usb")"
}

test_usb_porte_senza_java() { # termux-usb -l (Java) solo quando l'elenco di /sys cambia, o ogni 60 s
  local u="$ROOT/share/usb/tod-usb" sys="$HOME/sysusb" f out
  mkdir -p "$sys/usb1" "$sys/1-1"
  ln -s /non/leggibile "$sys/1-1.2"   # come su Android: collegamenti verso cartelle vietate
  f=$(sed -n '/^usb_firma() {/,/^}/p; /^attendi() {/,/^}/p; /^da_chiedere() {/,/^}/p' "$u")
  out=$(TOD_USB_SYS="$sys" bash -c "$f"'
SECONDS=100; usb_firma; prima=$USB_FIRMA
da_chiedere "$prima" 90 && echo uguale-10s || echo no-uguale-10s
da_chiedere "$prima" 30 && echo uguale-70s || echo no-uguale-70s
mkdir "'"$sys"'/1-1.3"; usb_firma
da_chiedere "$prima" 99 && echo cambiato || echo no-cambiato
TOD_USB_SYS=/non/esiste; usb_firma
da_chiedere "" 98 && echo vuoto-2s || echo no-vuoto-2s
da_chiedere "" 95 && echo vuoto-5s || echo no-vuoto-5s
ATTESA_FD=""; t=$SECONDS; attendi 0.1; echo attesa-ok
TOD_USB_SYS="'"$sys"'"; usb_firma; prima=$USB_FIRMA
da_chiedere "$prima" 99 105 && echo dopo-cambio || echo no-dopo-cambio
da_chiedere "$prima" 99 0 && echo finito || echo no-finito')
  eq "porte uguali: niente Java per 60 s" "no-uguale-10s" "$(echo "$out" | sed -n 1p)"
  eq "dopo 60 s si richiede comunque" "uguale-70s" "$(echo "$out" | sed -n 2p)"
  eq "chiavetta collegata: si richiede subito" "cambiato" "$(echo "$out" | sed -n 3p)"
  eq "elenco non leggibile: come prima, non prima di 4 s" "no-vuoto-2s" "$(echo "$out" | sed -n 4p)"
  eq "elenco non leggibile: ogni 4 s" "vuoto-5s" "$(echo "$out" | sed -n 5p)"
  eq "attesa senza fifo" "attesa-ok" "$(echo "$out" | sed -n 6p)"
  has "i collegamenti verso cartelle vietate contano" "1-1.2 " "$(TOD_USB_SYS="$sys" bash -c "$f"$'\n''usb_firma; echo "$USB_FIRMA"')"
  eq "dopo un cambio si richiede finché Android non ha il dispositivo" "dopo-cambio" "$(echo "$out" | sed -n 7p)"
  eq "finita la finestra, di nuovo ogni 60 s" "no-finito" "$(echo "$out" | sed -n 8p)"
  has "riavvia prende l'ambiente dalla nostra barra" "tod-barra.py" "$(grep -n 'pan=' "$u")"
  # tra un controllo delle porte e l'altro: il giro completo solo se un disco aperto è cambiato
  local run="$HOME/runporte"
  mkdir -p "$run"
  f=$(sed -n '/^lkl_vivo() {/,/^}/p; /^giro_serve() {/,/^}/p' "$u")
  sleep 30 & local vivo=$!
  echo /dev/bus/usb/001/011 >"$run/lkl-dev-usb4"; echo "$vivo" >"$run/lkl-usb4.pid"
  : >"$run/lkl-usb4.volumi"; touch -d '1 minute ago' "$run/lkl-usb4.volumi"; : >"$run/volumi"
  out=$(RUN="$run" bash -c "$f"$'\n''giro_serve && echo serve || echo niente')
  eq "disco vivo, partizioni già lette: niente giro" "niente" "$out"
  touch -d '1 minute' "$run/lkl-usb4.volumi"
  out=$(RUN="$run" bash -c "$f"$'\n''giro_serve && echo serve || echo niente')
  eq "partizioni nuove: giro" "serve" "$out"
  touch -d '1 minute ago' "$run/lkl-usb4.volumi"; kill "$vivo"; wait "$vivo" 2>/dev/null || true
  out=$(RUN="$run" bash -c "$f"$'\n''giro_serve && echo serve || echo niente')
  eq "disco finito (espulso, scollegato): giro" "serve" "$out"
  hasnt "il controllo leggero non avvia processi" '$(' "$(sed -n '/^giro_serve() {/,/^}/p; /^lkl_vivo() {/,/^}/p' "$u")"
  # dopo ogni giro si ricorda cosa ha visto (sennò un disco in errore si riapriva all'infinito)
  eq "ogni giro aggiorna VISTI" "$(grep -c 'giro "\$devs"' "$u")" "$(grep -c 'giro "\$devs"; VISTI=\$devs' "$u")"
}

test_usb_volumi() { # dischi in Esplora file: un'unità per disco, un volume per partizione, nomi e icone
  python3 -c "import gi; gi.require_version('Gio', '2.0'); from gi.repository import Gio" 2>/dev/null || return 0
  local tmp="$HOME/tmpvol" out
  mkdir -p "$tmp/tod-usb"
  printf 'EMUELEC\tsda\tScheda SD\tUSB Storage\t805306368\tvfat\tEMUELEC\t1510-0052\nScheda SD 2\tsda\tScheda SD\tUSB Storage\t1\text4\t\tc1e9\nDATI\tsdb\tChiavetta USB\tFlash\t1\texfat\tDATI\tAB-CD\n' \
    >"$tmp/tod-usb/volumi"
  out=$(TMPDIR="$tmp" python3 - "$ROOT/share/usb/tod-volumi.py" <<'PY'
import importlib.util as u, sys
s = u.spec_from_file_location("v", sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)
from gi.repository import GLib
d, v = m.leggi()
GLib.Variant(f"(a{m.DRIVE}a{m.VOLUME}a{m.MOUNT})", ([m.drive_struct(x) for x in d.values()], [m.volume_struct(x) for x in v], [m.mount_struct(x) for x in v]))
print(" ".join(f"{x['nome']}:{len(x['volumi'])}:{m.drive_struct(x)[2]}" for x in d.values()))
print(" ".join(x["uri"] for x in v))
PY
)
  eq "unità e volumi" "EMUELEC:1:. GThemedIcon media-flash-sd media-flash-sd-mmc media-flash drive-removable-media Scheda SD 2:1:. GThemedIcon media-flash-sd media-flash-sd-mmc media-flash drive-removable-media DATI:1:. GThemedIcon drive-removable-media-usb drive-removable-media
file:///media/usb/EMUELEC file:///media/usb/Scheda%20SD%202 file:///media/usb/DATI" "$out"
}

test_usb_volumi_avviso_perso() { # disco già collegato all'accensione (3 novembre): l'elenco scritto mentre il
  # monitor partiva non arrivava mai a Esplora file e scrivania; il controllo periodico lo annuncia, una volta
  python3 -c "import gi; gi.require_version('Gio', '2.0'); from gi.repository import Gio" 2>/dev/null || return 0
  local tmp="$HOME/tmpvolp" out
  mkdir -p "$tmp/tod-usb" "$tmp/.local/state/tod"
  out=$(TMPDIR="$tmp" TOD_TERMUX_HOME="$tmp" python3 - "$ROOT/share/usb/tod-volumi.py" "$tmp/tod-usb/volumi" <<'PY'
import importlib.util as u, sys
s = u.spec_from_file_location("v", sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)
class Conn:
    segnali = []
    def register_object(self, *a): return 1
    def emit_signal(self, dest, path, iface, nome, params): self.segnali.append(f"{nome}:{params.unpack()[1]}")
c = Conn(); mon = m.Monitor(c)
print(len(mon.volumi), mon.aggiorna("controllo"))
open(sys.argv[2], "w").write("EMUELEC\tsda\tScheda SD\tUSB Storage\t805306368\tvfat\tEMUELEC\t1510-0052\n")
print(mon.aggiorna("controllo"), " ".join(c.segnali))
print(mon.aggiorna("controllo"), len(c.segnali))
class Risposta:
    def return_value(self, v): print("TodRicarica", v.unpack()[0], len(c.segnali))
open(sys.argv[2], "a").write("DATI\tsdb\tChiavetta USB\tFlash\t1\texfat\tDATI\tAB-CD\n")
mon.chiamata(None, ":1.5", None, None, "TodRicarica", None, Risposta())
PY
)
  eq "avviso perso: annunciato dal controllo, una volta sola; tod-usb lo fa rileggere subito" "0 False
True DriveConnected:tod-drive-emuelec VolumeAdded:tod-volume-emuelec MountAdded:tod-mount-emuelec
False 3
TodRicarica True 6" "$out"
  has "il registro dei dischi dice cosa è stato annunciato" "annunciati (controllo): +EMUELEC" "$(cat "$tmp/.local/state/tod/usb.log" 2>/dev/null)"
  local g
  g=$(sed -n '/^giro() {/,/^}/p' "$ROOT/share/usb/tod-usb")
  eq "il giro avvisa il monitor prima di aprire le finestre dei dischi nuovi" "avvisa_monitor voci" \
    "$(grep -o -E 'avvisa_monitor|^  voci' <<<"$g" | tr -d ' ' | tr '\n' ' ' | sed 's/ $//')"
}

test_usb_scalda() { # 4 novembre: la prima lettura della radice di una FAT32 grande costava 9 s a LKL appena partito e
  # la pagavano Esplora file e scrivania (finestra e doppio clic fermi). La fa tod-usb, una volta, prima dell'annuncio
  local run="$HOME/runsc" data="$HOME/datasc" f g
  mkdir -p "$run" "$data"
  : >"$data/tod-fs.so"
  f=$(sed -n '/^alias_of() {/,/^}/p; /^log() {/,/^}/p; /^scalda() {/,/^}/p' "$ROOT/share/usb/tod-usb")
  RUN="$run" DATA="$data" LOG="$run/log" bash -c "$f"$'\n''scalda "$1"' _ $'Scheda SD\nEMUELEC' 2>/dev/null
  eq "partizioni nuove: lette e segnate" "caldo-emuelec caldo-scheda-sd" "$(cd "$run" && echo caldo-*)"
  RUN="$run" DATA="$data" LOG="$run/log" bash -c "$f"$'\n''scalda "$1"' _ "EMUELEC" 2>/dev/null
  eq "tolta: la prossima volta si rilegge" "caldo-emuelec" "$(cd "$run" && echo caldo-*)"
  g=$(sed -n '/^giro() {/,/^}/p' "$ROOT/share/usb/tod-usb")
  eq "prima la lettura, poi l'annuncio al desktop" "scalda volumi.gio" "$(grep -o -E '^  scalda|volumi\.gio' <<<"$g" | sed -n '1,2p' | tr -d ' ' | tr '\n' ' ' | sed 's/ $//')"
  python3 -c "import gi; gi.require_version('Gio', '2.0'); from gi.repository import Gio" 2>/dev/null || return 0
  mkdir -p "$run/tod-usb"
  printf 'EMUELEC\tsda\tScheda SD\tUSB Storage\t1\tvfat\tEMUELEC\t1510-0052\nDATI\tsdb\tChiavetta USB\tFlash\t1\texfat\tDATI\tAB-CD\n' >"$run/tod-usb/volumi"
  eq "senza volumi.gio (tod-usb di prima): l'elenco di tod-fs" "EMUELEC DATI" "$(TMPDIR="$run" python3 - "$ROOT/share/usb/tod-volumi.py" <<'PY'
import importlib.util as u, sys
s = u.spec_from_file_location("v", sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)
print(" ".join(v["nome"] for v in m.leggi()[1]))
PY
)"
  head -1 "$run/tod-usb/volumi" >"$run/tod-usb/volumi.gio"
  eq "con volumi.gio: solo le partizioni già lette" "EMUELEC" "$(TMPDIR="$run" python3 - "$ROOT/share/usb/tod-volumi.py" <<'PY'
import importlib.util as u, sys
s = u.spec_from_file_location("v", sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)
print(" ".join(v["nome"] for v in m.leggi()[1]))
PY
)"
}

test_lkl_anticipo() { # 4 novembre: la prima apertura della Scheda SD (FAT32) costava 10 s: LKL legge le cartelle 512 byte
  # alla volta e ogni comando USB costa ~2 ms (4277 comandi). tod-lkl legge in anticipo quando una lettura prosegue l'altra
  local cc s="$ROOT/share/usb/lkl/tod-lkl.c"
  has "le scritture buttano la finestra" "if (scrivi) anticipo_butta(off, len);" "$(sed -n '/^static int usb_io/,/^}/p' "$s")"
  has "si può spegnere" 'getenv("TOD_LKL_ANTICIPO")' "$(cat "$s")"
  cc=$(command -v clang || command -v gcc) || { echo "  (nessun compilatore: salto)"; return 0; }
  { sed -n '/^\/\* lettura anticipata/,/^static int usb_io/{/^static int usb_io/!p}' "$s"
    sed -n '/^static int usb_leggi/,/^}/p' "$s"; } >"$WORK/ant_$$.c"
  cat >"$WORK/antp_$$.c" <<'C'
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static uint64_t blocchi = 2048;   /* disco finto da 1 MB */
static uint32_t blocco = 512;
static unsigned char disco[1 << 20];
static uint64_t guasto = UINT64_MAX;   /* settore illeggibile */
static int usb_io(uint64_t off, unsigned char *buf, uint64_t len, int scrivi);
#include ANT
static int usb_io(uint64_t off, unsigned char *buf, uint64_t len, int scrivi) {
    if (off + len > sizeof disco) { printf("oltre la fine\n"); return -1; }
    if (scrivi) anticipo_butta(off, len);
    comandi++;
    if (!scrivi && guasto * 512 >= off && guasto * 512 < off + len) return -1;
    if (scrivi) memcpy(disco + off, buf, len); else memcpy(buf, disco + off, len);
    return 0;
}
static int giusto(uint64_t s) { unsigned char b[512]; return !usb_leggi(s * 512, b, 512) && !memcmp(b, disco + s * 512, 512); }
int main(void) {
    int ok = 1;
    unsigned char nuovo[512];
    for (size_t i = 0; i < sizeof disco; i++) disco[i] = (unsigned char)(i * 7 + i / 512);
    for (uint64_t s = 0; s < 256; s++) ok &= giusto(s);
    printf("in fila %d %u\n", ok, comandi);
    comandi = 0; ok = 1;
    for (uint64_t s = 1000; s > 900; s -= 3) ok &= giusto(s);
    printf("sparse %d %u\n", ok, comandi);
    ok = giusto(1500) & giusto(1501);
    memset(nuovo, 0xab, sizeof nuovo);
    usb_io(1510 * 512, nuovo, 512, 1);
    for (uint64_t s = 1502; s <= 1512; s++) ok &= giusto(s);
    printf("dopo una scrittura %d\n", ok);
    ok = 1;
    for (uint64_t s = 2040; s < 2048; s++) ok &= giusto(s);
    printf("fine del disco %d\n", ok);
    guasto = 1700; ok = 1;
    for (uint64_t s = 1650; s < 1700; s++) ok &= giusto(s);
    printf("settore guasto dopo %d\n", ok);
    senza_anticipo = 1; comandi = 0; ok = 1;
    for (uint64_t s = 100; s < 164; s++) ok &= giusto(s);
    printf("spenta %d %u\n", ok, comandi);
    return 0;
}
C
  "$cc" -O2 -Wall -Werror -DANT="\"$WORK/ant_$$.c\"" -o "$WORK/antp_$$" "$WORK/antp_$$.c"
  eq "256 settori in fila: 3 comandi; salti, scritture, fine del disco, guasti: dati giusti" \
    "in fila 1 3|sparse 1 34|dopo una scrittura 1|fine del disco 1|settore guasto dopo 1|spenta 1 64" \
    "$("$WORK/antp_$$" | paste -sd'|')"
}

test_diretto_execvpe() { # 6 novembre: execvpe del glibc cerca nel PATH senza fakechroot, sul vero Android (i filtri di
  # stampa, gstoraster, non trovavano /usr/bin/gs). tod-diretto.so la fa come execvp, con l'ambiente del chiamante
  local s
  s=$(cat "$ROOT/share/diretto/tod-diretto.c")
  has "execvpe intercettata" "int execvpe(const char *file, char *const argv[], char *const envp[])" "$s"
  has "cercata nel PATH come execvp" 'if (f) return execve(f, argv, envp);' "$s"
}

_stampa_py() { # _stampa_py FILE: python con FILE caricato come modulo m; il codice della prova arriva da stdin
  python3 -c 'import importlib.util as u, sys
s = u.spec_from_file_location("m", sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)
exec(sys.stdin.read())' "$1"
}

test_stampa_indirizzo_vero() { # 11 novembre, Pietro con una stampante Wi-Fi vera: non trovata da sola, e aggiunta con
  # l'indirizzo "funziona anche con un IP a caso" ma non stampa (ipp://IP/ipp/print dato per buono, coda creata anche se
  # lpadmin falliva). Ora si chiede alla stampante chi è (share/stampa/ipp.py), il percorso si trova da solo, un
  # indirizzo muto dà un errore chiaro, la coda fallita si toglie, e si cerca anche indirizzo per indirizzo (solo Wi-Fi)
  command -v python3 >/dev/null || return 0
  local out
  out=$(TMPDIR="$WORK/si_tmp" TOD_TERMUX_HOME="$WORK/si_home" LANGUAGE=it _stampa_py "$ROOT/share/stampa/tod-stampa.py" <<'EOF'
import http.server, struct, threading, socket
ipp = m.ipp
def risposta(stato, attr):
    b = struct.pack(">BBHI", 2, 0, stato, 1) + b"\x04"
    for nome, tag, valori in attr:
        for i, v in enumerate(valori):
            n = nome.encode() if i == 0 else b""
            b += struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(v)) + v
    return b + b"\x03"
class Finta(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/ipp":   # una stampante vecchia: niente /ipp/print, solo PCL
            corpo = risposta(0, [("printer-make-and-model", 0x41, [b"Marca Laser 2000"]),
                                 ("document-format-supported", 0x49, [b"application/octet-stream", b"application/vnd.hp-PCL"]),
                                 ("printer-uuid", 0x45, [b"urn:uuid:ABC-1"]), ("printer-state", 0x23, [struct.pack(">i", 3)])])
        elif self.path == "/ipp/moderna":
            corpo = risposta(0, [("printer-make-and-model", 0x41, [b"Marca Getto 5"]),
                                 ("document-format-supported", 0x49, [b"image/urf", b"image/jpeg"])])
        else:
            corpo = risposta(0x0406, [])
        self.send_response(200); self.send_header("Content-Type", "application/ipp")
        self.send_header("Content-Length", str(len(corpo))); self.end_headers(); self.wfile.write(corpo)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Finta)
porta = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
st, mot = ipp.sonda("127.0.0.1", porte=((porta, False),))
print(st["uri"].replace(str(porta), "P"), st["modello"], st["senza_driver"], st["uuid"], st["formati"][-1], repr(mot))
a, _ = ipp.attributi("127.0.0.1", porta, "ipp/moderna")
print(ipp.descrivi(a, "ipp://x/ipp/moderna", "x")["senza_driver"], ipp.attributi("127.0.0.1", porta, "niente"))
libero = socket.socket(); libero.bind(("127.0.0.1", 0)); chiusa = libero.getsockname()[1]; libero.close()
print(ipp.sonda("127.0.0.1", porte=((chiusa, False),), attesa=0.5)[1] in ("spenta", "non-stampante"),
      ipp.porte_aperte(["127.0.0.1"], porta), ipp.porte_aperte(["127.0.0.1"], chiusa, 0.3))
print(ipp.rete_di_casa("192.168.0.207"), ipp.rete_di_casa("10.229.10.240"), ipp.rete_di_casa("10.0.0.5", "10.0.0.5"),
      ipp.rete_di_casa("192.168.0.207", "192.168.1.9"), ipp.rete_di_casa(""), len(ipp.indirizzi_vicini("192.168.0.207")),
      "192.168.0.207" in ipp.indirizzi_vicini("192.168.0.207"))
# l'aggiunta: lpadmin finto
m.log = lambda *a: None
fatte, esiste = [], set()
class R:
    def __init__(s, rc=0, out="", err=""): s.returncode, s.stdout, s.stderr = rc, out, err
def cups(*a, timeout=0):
    fatte.append(" ".join(a[:6]))
    if a[0] == "lpstat" and a[1] == "-v":
        return R(0, "".join(f"device for {n}: x\n" for n in sorted(esiste)))
    if a[0] == "lpadmin" and a[1] == "-p":
        esiste.add(a[2])   # come CUPS: la coda nasce prima del controllo del driver
        return R(1, "", "PPD creation failed") if a[a.index("-m") + 1] == "everywhere" and "rotta" in a[a.index("-v") + 1] else R(0)
    if a[0] == "lpadmin" and a[1] == "-x":
        esiste.discard(a[2]); return R(0)
    if a[0] == "lpinfo":
        return R(0, "drv:///sample.drv/generpcl.ppd Generic PCL Laser Printer\n" if "-m" in a and "--device-id" not in a and "--make-and-model" not in a else "")
    return R(0, "system default destination: x")
m.cups = cups
db = {"stampanti": {}, "tolte": [], "viste": {}}
m.ipp.sonda = lambda h, **k: (None, "spenta")
print(m.aggiungi_indirizzo(db, "192.168.0.222"), esiste, db["stampanti"])
m.ipp.sonda = lambda h, **k: (None, "non-stampante")
print(m.aggiungi_indirizzo(db, "192.168.0.1"))
print(m.aggiungi_indirizzo(db, "non un indirizzo!"))
m.ipp.sonda = lambda h, **k: (dict(st, uri="ipp://192.168.0.150:631/ipp", ip=h), "")
print(repr(m.aggiungi_indirizzo(db, "192.168.0.150")), sorted(esiste), [x for x in fatte if x.startswith("lpadmin -p")][-1],
      list(db["stampanti"]))
m.ipp.sonda = lambda h, **k: ({"uri": "ipp://rotta/ipp/print", "tipo": "ipp", "ip": h, "modello": "Rotta", "formati": ["image/urf"],
                                "device_id": "", "uuid": "", "senza_driver": True}, "")
print(m.aggiungi_indirizzo(db, "192.168.0.9"), sorted(esiste))
# 12 novembre, HP Envy di Pietro: aggiunta a mano e poi di nuovo dalla ricerca ("…_2"). Chi ha già una coda non si rifà
m.code = lambda: {"HP_Envy": "ipp://192.168.1.17:631/ipp/print"}
m.ipp.sonda = lambda h, **k: (dict(st, uri="ipp://192.168.1.17:631/ipp/print", ip=h, modello="HP"), "")
print(m.aggiungi_indirizzo(db, "192.168.1.17"), list(db["stampanti"]))
db2, prima = {"stampanti": {"uri:ipp://192.168.1.17:631/ipp/print": {"nome": "HP_Envy", "uri": "x", "tipo": "rete", "modello": "HP"}},
              "tolte": [], "viste": {}}, len(fatte)
r = m.aggiungi_rete(db2, {"chiave": "uuid-hp", "uri": "ipp://192.168.1.17:631/ipp/print", "modello": "HP", "ip": "192.168.1.17", "senza_driver": True})
print(repr(r), {k: v["nome"] for k, v in db2["stampanti"].items()}, m.host_di("ipp://192.168.1.17:631/ipp/print"),
      len([x for x in fatte[prima:] if x.startswith("lpadmin -p")]))
srv.shutdown()
EOF
)
  eq "percorso trovato, formati, errori chiari, coda fallita tolta, driver generico per PCL" "ipp://127.0.0.1:P/ipp Marca Laser 2000 False abc-1 application/vnd.hp-pcl ''
True (None, 'no')
True ['127.0.0.1'] []
True False True False False 253 False
Nessuna stampante risponde a 192.168.0.222. Controlla che sia accesa e sulla stessa rete Wi-Fi. set() {}
A 192.168.0.1 risponde un dispositivo, ma non è una stampante di rete.
«non un indirizzo!» non è un indirizzo valido.
'' ['Marca_Laser_2000'] lpadmin -p Marca_Laser_2000 -E -v ipp://192.168.0.150:631/ipp ['abc-1']
Rotta: non riesco a configurarla (PPD creation failed). ['Marca_Laser_2000']
HP c'è già tra le stampanti. ['abc-1']
'' {'uuid-hp': 'HP_Envy'} 192.168.1.17 0" "$out"
  # 12 novembre, Pietro: "quando una stampante è offline deve essere offline, non deve spuntare pronta"
  out=$(TMPDIR="$WORK/si_tmp" TOD_TERMUX_HOME="$WORK/si_home" _stampa_py "$ROOT/share/stampa/tod-stampa.py" <<'EOF'
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1); aperta = s.getsockname()[1]
c = socket.socket(); c.bind(("127.0.0.1", 0)); chiusa = c.getsockname()[1]; c.close()
print(m.porta_di("ipp://192.168.1.18/ipp/print"), m.porta_di("socket://10.0.0.9"), m.porta_di("ipps://stampante.local:8443/x"),
      m.porta_di("ipp://127.0.0.1:60001/ipp/print"), m.porta_di("usb://HP/x"))
m.porta_di = lambda u: ("127.0.0.1", int(u)) if u.isdigit() else None
print(m.raggiungibili([str(aperta), str(chiusa), "usb"], 0.5) == {str(aperta): True, str(chiusa): False})
EOF
)
  eq "ogni stampante di rete si controlla alla sua porta; le USB no" "('192.168.1.18', 631) ('10.0.0.9', 9100) ('stampante.local', 8443) None None
True" "$out"
  has "elenco: offline prima di pronta e di in pausa" 'if p["raggiungibile"] is False:   # prima di tutto' "$(cat "$ROOT/share/tod-settings.py")"
  has "scanner aggiunto a mano: si guarda se risponde" '"raggiungibile": ha_escl(m["url"], attesa=2)' "$(cat "$ROOT/share/stampa/escl.py")"
  has "la ricerca per indirizzi solo sul Wi-Fi di casa" "ipp.rete_di_casa(ip, wifi_del_telefono())" "$(cat "$ROOT/share/stampa/tod-stampa.py")"
  # "non riesco a vedere i processi di stampa": un documento per riga con il suo Annulla, il motivo del fermo, Riprendi
  has "coda: il nome del documento (lpq)" 'lav["titolo"] = titoli.get(' "$(cat "$ROOT/share/stampa/tod-stampa.py")"
  has "coda: una finestra per stampante, un documento per riga con il suo Annulla" 'self.comando("annulla", j)' "$(sed -n '/^class Coda(/,/^class AggiuntaGuidata(/p' "$ROOT/share/tod-settings.py")"
  has "coda: pausa e ripresa" 'self.comando("riprendi" if self.fermo else "pausa", self.nome)' "$(cat "$ROOT/share/tod-settings.py")"
  has "coda: gli ultimi documenti mandati" '"fatti": sorted(fatti.get(nome, [])' "$(cat "$ROOT/share/stampa/tod-stampa.py")"
  has "aggiunta guidata: trovate in rete, configurazione, pagina di prova" 'self.pile.add_named(p3, "esito")' "$(cat "$ROOT/share/tod-settings.py")"
  # 12 novembre, HP Envy 6100e: "arriva alla stampante ma non stampa" (Print job canceled at printer): l'Apple Raster
  # di Ghostscript ha l'intestazione povera; con rastertopwg di CUPS (pacchetto cups) il nostro filtro non si registra
  has "Apple Raster: il nostro filtro solo se manca quello di CUPS" 'usr/lib/cups/filter/rastertopwg' "$(sed -n '/^def convs(/,/^def scrivi_conf(/p' "$ROOT/share/stampa/tod-stampa.py")"
  has "stampante ferma: si può riprendere" '"riprendi", n, then=' "$(cat "$ROOT/share/tod-settings.py")"
}

test_scanner_delle_stampanti() { # 11 novembre, Pietro: la multifunzione Wi-Fi "non viene né rilevata né riesco ad aggiungerla
  # via IP". Le multifunzione che annunciano solo la stampa: si prova la scansione agli indirizzi delle stampanti
  # trovate; più porte nell'aggiunta per indirizzo; l'errore dice se è spenta o se non sa fare la scansione in rete
  command -v python3 >/dev/null || return 0
  local d="$WORK/sds_tmp" out
  mkdir -p "$d/tod-stampa"
  printf '[{"ip":"192.168.0.150","modello":"Marca Multi 3"},{"ip":"192.168.0.151","modello":"Solo stampa"},{"modello":"senza indirizzo"}]' \
    >"$d/tod-stampa/trovate.json"
  out=$(TMPDIR="$d" TOD_TERMUX_HOME="$d" LANGUAGE=it _stampa_py "$ROOT/share/stampa/escl.py" <<'EOF'
provati = []
def prova(url):
    provati.append(url)
    return url == "https://192.168.0.150/eSCL"
r = m.scanner_delle_stampanti(prova, ora=1000)
print([(s["nome"], s["url"], s["dove"]) for s in r], len(provati))
m.scanner_delle_stampanti(prova, ora=1100); print(len(provati))          # già guardate: non si ribussa
m.scanner_delle_stampanti(prova, ora=1000 + 700); print(len(provati))     # dopo 10 minuti sì
print(m.candidati_indirizzo("192.168.0.150"))
print(m.candidati_indirizzo("192.168.0.150:9095"), m.candidati_indirizzo("http://x/eSCL"))
m.candidati_indirizzo = lambda t: []
m.risponde_qualcuno = lambda t: False
try: m.aggiungi_indirizzo("192.168.0.222")
except m.Errore as e: print(e)
m.risponde_qualcuno = lambda t: True
try: m.aggiungi_indirizzo("192.168.0.150")
except m.Errore as e: print(e)
EOF
)
  eq "scanner agli indirizzi delle stampanti, porte dei vari marchi, errori chiari" "[('Marca Multi 3', 'https://192.168.0.150/eSCL', 'rete')] 4
4
8
['http://192.168.0.150/eSCL', 'https://192.168.0.150/eSCL', 'http://192.168.0.150:8080/eSCL', 'http://192.168.0.150:9095/eSCL', 'http://192.168.0.150:631/eSCL', 'https://192.168.0.150:8443/eSCL']
['http://192.168.0.150:9095/eSCL', 'https://192.168.0.150:9095/eSCL'] ['http://x/eSCL']
Nessun dispositivo risponde a «192.168.0.222». Controlla che sia acceso e sulla stessa rete Wi-Fi.
Il dispositivo a «192.168.0.150» risponde, ma non offre la scansione in rete (AirScan/eSCL)." "$out"
}

test_stampa_driver_ricoh() { # 12 novembre, la stampante vera di Pietro: Ricoh MP C2011 (da ufficio, non AirPrint). A CUPS
  # mancava cups-driverd ("lpinfo: cups-driverd failed to execute"): nessun driver, nemmeno i generici. Ora cups e
  # openprinting-ppds tra gli aiuti del container, e tra i tre driver del modello (PXL, PS, PDF) si sceglie il
  # linguaggio che la stampante dichiara, PXL per primo (PS e PDF sono opzioni che la macchina può non avere)
  command -v python3 >/dev/null || return 0
  local out
  has "cups-driverd e rastertopwg arrivano con il container" " cups " " $CX_AIUTI "
  has "i driver delle stampanti da ufficio" "openprinting-ppds" "$CX_AIUTI"
  has "il servizio non si porta dietro i consigliati di cups (avahi, colord, sane…)" "--no-install-recommends install \$manca" \
    "$(sed -n '/^cx_upgrade()/,/^}/p' "$ROOT/lib/container.sh")"
  out=$(TMPDIR="$WORK/sr_tmp" TOD_TERMUX_HOME="$WORK/sr_home" _stampa_py "$ROOT/share/stampa/tod-stampa.py" <<'EOF'
R = ["openprinting-ppds:0/ppd/openprinting/Ricoh/PDF/Ricoh-MP_C2011_PDF.ppd Ricoh MP C2011 PDF",
     "openprinting-ppds:0/ppd/openprinting/Ricoh/PS/Ricoh-MP_C2011_PS.ppd Ricoh MP C2011 PS",
     "openprinting-ppds:0/ppd/openprinting/Ricoh/PXL/Ricoh-MP_C2011_PXL.ppd Ricoh MP C2011 PXL",
     "everywhere IPP Everywhere"]
corto = lambda n: n.rsplit("/", 1)[-1]
print(corto(m.scegli_ppd(R)), corto(m.scegli_ppd(R, ["application/postscript"])),
      corto(m.scegli_ppd(R, ["application/vnd.hp-pcl", "application/postscript"])),
      corto(m.scegli_ppd(R, ["application/pdf"])), repr(m.scegli_ppd(["everywhere IPP Everywhere", "raw Raw"])),
      m.scegli_ppd(["gutenprint.5.3://x/expert Qualcosa"]))
chieste = []
class Ris:
    def __init__(s, out=""): s.returncode, s.stdout, s.stderr = 0, out, ""
def cups(*a, timeout=0):
    chieste.append(a[1:3])
    return Ris("\n".join(R) if a[1] == "--make-and-model" and "C2011" in a[2] else "")
m.cups = cups
st = {"senza_driver": False, "device_id": "", "formati": ["application/vnd.hp-pcl"], "modello": "RICOH MP C2011", "ip": "192.168.0.150"}
print(corto(m.modello_per(st)), chieste)
print(m.modello_per(dict(st, senza_driver=True)), repr(m.modello_per({"senza_driver": False, "modello": "192.168.0.9", "ip": "192.168.0.9"})))
print("printer" in m.ipp.PERCORSI)
EOF
)
  eq "PXL di serie, il linguaggio dichiarato quando si sa, mai everywhere/raw; dal nome del modello senza ID" \
"Ricoh-MP_C2011_PXL.ppd Ricoh-MP_C2011_PS.ppd Ricoh-MP_C2011_PXL.ppd Ricoh-MP_C2011_PDF.ppd '' gutenprint.5.3://x/expert
Ricoh-MP_C2011_PXL.ppd [('--make-and-model', 'RICOH MP C2011')]
everywhere ''
True" "$out"
}

test_video_chip_app() { # 13 novembre, Pietro: il video col chip "deve essere generale per tutti, tipo io installo una
  # nuova app e ha già questa miglioria". lib/videochip.sh compila driver VA-API, shim, GBM (glibc del container) e
  # l'aiutante (Termux); /etc/tod/video.env c'è solo col ponte pronto e acceso; diretto_run e tod-run lo danno a ogni
  # app Chromium/Electron riconosciuta dalla sua cartella, solo con l'aiutante acceso
  local d="$WORK/vchip_$$" out
  mkdir -p "$d/bin" "$d/casa" "$d/stato" "$d/conf" "$d/prefix/include/va" "$d/root/lib/aarch64-linux-gnu" "$d/root/etc"
  : >"$d/prefix/include/va/va_backend.h"
  : >"$d/root/lib/aarch64-linux-gnu/libc.so.6"
  cat >"$d/bin/clang" <<'EOF'
#!/bin/sh
echo "clang $*" >>"$VC_REG"
while [ $# -gt 0 ]; do [ "$1" = -o ] && { echo finto >"$2"; chmod +x "$2"; exit 0; }; shift; done
EOF
  printf '#!/bin/sh\necho "-lavcodec -lavutil"\n' >"$d/bin/pkg-config"
  chmod +x "$d/bin/clang" "$d/bin/pkg-config"
  out=$(PATH="$d/bin:$PATH" TOD_TERMUX_HOME="$d/casa" PREFIX="$d/prefix" VC_REG="$d/reg" TOD_STATE_DIR="$d/stato" \
    TOD_CONF="$d/conf/tod.conf" TOD_ROOT="$ROOT" VC_ROOT="$d/root" bash -c '
    source "$TOD_ROOT/lib/core.sh" 2>/dev/null; source "$TOD_ROOT/lib/videochip.sh"
    have() { command -v "$1" >/dev/null; }
    log() { echo "LOG $*"; }
    cx_rootfs() { echo "$VC_ROOT"; }
    videochip_esegui_prova() { echo x >>"$VC_REG.prova"; printf "dma_heap: ok\nvulkan: Finto (integrata)\n%s\n" "${VC_ESITO:-ESITO ok}"; }
    D=$TOD_TERMUX_HOME/.local/share/tod-va
    videochip_compila && ls "$D" | tr "\n" " "; echo
    readlink "$D/include/va"
    videochip_compila; wc -l <"$VC_REG"
    videochip_finto; cat "$D/finto/sys/dev/char/226:128/device/uevent" | sed -n 3p
    videochip_pronto && echo pronto
    videochip_env; [ -f "$VC_ROOT/etc/tod/video.env" ] || echo "senza prova: niente video.env"
    videochip_prova && echo adatto; videochip_prova; echo "prove: $(wc -l <"$VC_REG.prova")"
    videochip_env; cat "$VC_ROOT/etc/tod/video.env"
    conf_set VIDEO_CHIP 0; videochip_env; [ -f "$VC_ROOT/etc/tod/video.env" ] || echo "spento: niente video.env"
    conf_set VIDEO_CHIP 1; rm -f "$D/prova.txt"
    VC_ESITO="ESITO no: nessun Vulkan hardware" videochip_prova || echo "mali: $(videochip_motivo)"
    videochip_env; [ -f "$VC_ROOT/etc/tod/video.env" ] || echo "mali: niente video.env"
    rm -f "$D/prova.txt"; videochip_prova >/dev/null; rm -f "$D/tod-gbm.so"; videochip_env; [ -f "$VC_ROOT/etc/tod/video.env" ] || echo "incompleto: niente video.env"' 2>&1)
  local D="$d/casa/.local/share/tod-va"
  has "i pezzi compilati (con la prova reale)" "include tod-gbm.so tod-va-chip tod-va-prova tod-va-shim.so tod_drv_video.so" "$out"
  has "il driver per il glibc del container, con le sole intestazioni di libva" \
    "clang --target=aarch64-linux-gnu -O2 -fPIC -shared -nostdlib -ffreestanding -fno-builtin -fuse-ld=lld -I$D/include -o $D/tod_drv_video.so.nuovo $ROOT/share/video/tod_drv_video.c $d/root/lib/aarch64-linux-gnu/libc.so.6" \
    "$(cat "$d/reg")"
  has "intestazioni: un collegamento a quelle di libva di Termux" "$d/prefix/include/va" "$out"
  has "l'aiutante con ffmpeg" "-o $D/tod-va-chip.nuovo $ROOT/share/video/tod-va-chip.c -lavcodec -lavutil" "$(cat "$d/reg")"
  has "non ricompilati se i sorgenti non cambiano" $'\n5\n' "$out"
  has "scheda finta: un dispositivo PCI con i numeri veri (non \$VV)" "PCI_ID=5143:0840" "$out"
  has "pronto" "pronto" "$out"
  has "senza la prova reale niente ponte" "senza prova: niente video.env" "$out"
  has "prova passata (Adreno), e non rifatta finché il profilo grafico non cambia" $'adatto\nprove: 1' "$out"
  has "Mali (niente Vulkan hardware nel container): il motivo" "mali: nessun Vulkan hardware" "$out"
  has "Mali: le app partono come sempre" "mali: niente video.env" "$out"
  has "video.env: prima la GBM, poi lo shim" "VA_PRELOAD=\"$D/tod-gbm.so $D/tod-va-shim.so\"" "$out"
  has "video.env: ANGLE su Vulkan e il nodo finto" "--use-angle=vulkan --ignore-gpu-blocklist --render-node-override=/dev/dri/renderD128" "$out"
  has "video.env: il driver tod" "LIBVA_DRIVER_NAME=\"tod\"" "$out"
  has "spento: le app partono come sempre" "spento: niente video.env" "$out"
  has "un pezzo mancante: le app partono come sempre" "incompleto: niente video.env" "$out"
  # gli agganci: ogni app Chromium/Electron (anche nuova), solo con l'aiutante acceso; installazione, sessione, comando
  local s
  s=$(cat "$ROOT/lib/diretto.sh")
  has "diretto_run: video.env per le app Chromium/Electron, solo con l'aiutante acceso e se l'app non è caduta col ponte" \
    'videochip_aiutante_vivo && ! videochip_escluso "${real#"$root"}"; then' "$s"
  has "tod-run (proot): anche lì le app cadute col ponte partono senza" '! grep -qxF "$p" "$E/video-no"' "$(cat "$ROOT/lib/container.sh")"
  has "a fine sessione l'aiutante si ferma" "videochip_ferma" "$(sed -n '/^session_stop()/,/^}/p' "$ROOT/lib/session.sh")"
  s=$(cat "$ROOT/share/video/tod-va-chip.c")
  has "aiutante: cane da guardia (un giro bloccato oltre 20 s chiude quel video, non tutto)" "alarm(20);" "$s"
  has "aiutante: registro limitato" "#define RIGHE 300" "$s"
  has "aiutante: conferma l'apertura del chip" "rispondi(r < 0 ? SCARTATO : FATTO, 0);" "$s"
  has "aiutante: il pid lo scrive lui dopo l'ascolto" '!strcmp(argv[1], "--pid")' "$s"
  s=$(cat "$ROOT/share/video/tod_drv_video.c")
  has "driver: invii con limite di tempo (aiutante incantato)" "21 /* SO_SNDTIMEO */" "$s"
  has "driver: aspetta la conferma d'apertura al più 3 s" "poll(&p, 1, 3000) <= 0" "$s"
  has "driver: aiutante caduto a metà video → si ricollega (al più 3 volte), il fotogramma si salta" "if (++x->cadute > 3) x->rotto = 1;" "$s"
  s=$(cat "$ROOT/lib/diretto.sh")
  has "diretto_run: le nostre librerie davanti" 'LD_PRELOAD="${va_pre:+$va_pre }$pre"' "$s"
  has "diretto_run: lo shim riconosce i processi dell'app" 'TOD_VA_PROG="${real#"$root"}"' "$s"
  s=$(cat "$ROOT/lib/container.sh")
  has "tod-run (proot): lo stesso" 'export TOD_VA_PROG="$p" LD_PRELOAD="$VA_PRELOAD${LD_PRELOAD:+ $LD_PRELOAD}"' "$s"
  has "libva nel container" "libva2 libva-drm2" "$(sed -n 's/^CX_AIUTI=//p' "$ROOT/lib/container.sh")"
  has "a ogni avvio del desktop" "(videochip_start || true)" "$(cat "$ROOT/lib/session.sh")"
  has "nell'installazione" 'videochip_install' "$(cat "$ROOT/lib/installer.sh")"
  has "negli aggiornamenti" 'videochip_install || warn' "$(cat "$ROOT/lib/maint.sh")"
  has "tod videochip" "videochip | video-chip) videochip_cli" "$(cat "$ROOT/tod")"
  s=$(cat "$ROOT/share/video/tod_drv_video.c")
  has "driver: senza l'aiutante non parte (mai un video finto)" "aiutante tod-va-chip non raggiungibile: il driver non parte" "$s"
  has "driver: avvio per ogni versione di libva (Debian 2.22, Termux 2.24)" "AVVIO(22) AVVIO(23) AVVIO(24)" "$s"
  has "driver: niente motivo di prova se il chip non decodifica" "return VA_STATUS_ERROR_DECODING_ERROR;" "$s"
  has "shim: resta nei processi dell'app, si toglie dagli altri" 'getenv("TOD_VA_PROG")' "$(cat "$ROOT/share/video/tod-va-shim.c")"
  # VP9 (YouTube): il fotogramma intero al chip; i nascosti (alt-ref) senza superficie da aspettare. HEVC no: VA non passa
  # gli insiemi di riferimento della SPS, e il chip decodificherebbe con riferimenti sbagliati
  has "driver: VP9 Profile 0" "VAProfileVP9Profile0};" "$s"
  has "driver: fotogrammi VP9 nascosti senza attesa" "if (manda(x->sock, &h, d, mostra ? v->fd : -1) < 0) {" "$s"
  hasnt "driver: niente HEVC finché non passa dal chip" "VAProfileHEVCMain," "$(sed -n '/^static const VAProfile PROFILI/,/;/p' "$ROOT/share/video/tod_drv_video.c")"
  s=$(cat "$ROOT/share/video/tod-va-chip.c")
  has "aiutante: pacchetti senza superficie" "senza superficie (VP9: fotogramma nascosto, solo riferimento) si decodifica e basta" "$s"
  has "aiutante: attese troppo vecchie liberate (mai tutto scartato)" "h->token > att[i].token + 48" "$s"
}

test_video_col_chip() { # 12 novembre: il Lettore decodifica col chip del telefono (MediaCodec via ffmpeg) con
  # share/media/tod-chip.c. Si compila sul telefono solo se il sorgente è cambiato; se il chip rifiuta un file si torna
  # al processore. Trappola trovata: senza i fili binder (ABinderProcess_startThreadPool) il chip prende 8 pacchetti e
  # poi si ferma, dentro GStreamer (ffmpeg da solo li ha)
  local d="$WORK/chip_$$" out
  mkdir -p "$d/bin" "$d/casa" "$d/stato"
  cat >"$d/bin/clang" <<'EOF'
#!/bin/sh
echo "clang $*" >>"$CHIP_REG"
while [ $# -gt 0 ]; do [ "$1" = -o ] && { echo finto >"$2"; exit "${CHIP_RC:-0}"; }; shift; done
EOF
  printf '#!/bin/sh\necho "-I/x -lgstvideo-1.0 -lavcodec -lavutil"\n' >"$d/bin/pkg-config"
  chmod +x "$d/bin/clang" "$d/bin/pkg-config"
  out=$(PATH="$d/bin:$PATH" HOME="$d/casa" CHIP_REG="$d/reg" TOD_STATE_DIR="$d/stato" TOD_ROOT="$ROOT" bash -c '
    source "$TOD_ROOT/lib/core.sh" 2>/dev/null; source "$TOD_ROOT/lib/desktop.sh"
    have() { command -v "$1" >/dev/null; }
    log() { echo "LOG $*"; }
    media_chip_compila; ls "$MEDIA_CHIP_DIR"
    media_chip_compila; wc -l <"$CHIP_REG"
    rm -f "$MEDIA_CHIP_DIR/libgsttodchip.so"; CHIP_RC=1 media_chip_compila; ls "$MEDIA_CHIP_DIR" | wc -l' 2>&1)
  has "compilato al primo giro" "libgsttodchip.so" "$out"
  has "con GStreamer e libavcodec" "-lgstvideo-1.0 -lavcodec -lavutil -ldl" "$(cat "$d/reg")"
  has "non ricompilato se il sorgente non cambia" $'libgsttodchip.so\n1' "$out"
  has "una compilazione fallita non lascia niente" "compilazione non riuscita" "$out"
  has "nell'installazione e negli aggiornamenti del media" "media_chip_compila || true" "$(sed -n '/^media_install()/,/^}/p' "$ROOT/lib/desktop.sh")"
  has "a ogni avvio del desktop" "(media_chip_compila || true)" "$(cat "$ROOT/lib/session.sh")"
  has "il modulo: i fili binder prima di aprire il chip" "ABinderProcess_startThreadPool" "$(cat "$ROOT/share/media/tod-chip.c")"
  has "il modulo: tempi di presentazione veri al chip" "s->pkt->pts = tempo_di(f);" "$(cat "$ROOT/share/media/tod-chip.c")"
  has "Lettore: il chip davanti (prima di Gst.init)" "chip_video()" "$(sed -n '/^def gst():/,/^def /p' "$ROOT/share/media/comune.py")"
  has "Lettore: se il chip rifiuta, col processore dallo stesso punto" "C.chip_spento()" "$(cat "$ROOT/share/media/motore.py")"
  # comune.chip_video (GTK: il Python di Termux)
  local py=/data/data/com.termux/files/usr/bin/python3
  if [[ -x $py ]] && "$py" -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null; then
    mkdir -p "$d/casa/.local/share/tod-media" && echo x >"$d/casa/.local/share/tod-media/libgsttodchip.so"
    out=$(env -u LD_PRELOAD -u GST_PLUGIN_PATH -u GST_PLUGIN_FEATURE_RANK TOD_TERMUX_HOME="$d/casa" "$py" -c '
import os, sys
sys.path.insert(0, sys.argv[1])
import comune as C
print(C.chip_video(), os.environ["GST_PLUGIN_PATH"].endswith("tod-media"), os.environ["GST_PLUGIN_FEATURE_RANK"])
os.environ["TOD_VIDEO_CHIP"] = "0"
print(C.chip_video())' "$ROOT/share/media" 2>&1)
    eq "Lettore: cartella e rango del chip, spegnibile" "True True todchipdec:MAX
False" "$out"
  fi
}

test_archiviazione() { # 13 novembre, Pietro: "ho 120 GB su Termux, che abbiamo combinato?" (un registro di prova da
  # 62 GB in $PREFIX/tmp) → app Archiviazione come macOS. I conti (share/archivio_dati.py) su misure finte di du:
  # ogni byte in una sola categoria, il resto del disco ad Android, i consigli solo quando servono
  command -v python3 >/dev/null || return 0
  local d="$WORK/arch_$$" out
  mkdir -p "$d/tmp"
  head -c 10000 /dev/urandom >"$d/tmp/grande.log"
  printf 'x' >"$d/tmp/piccolo.log"
  out=$(TOD_TERMUX_HOME=/c PREFIX=/u TOD_CX_ROOTFS=/r ARCH_TMP="$d/tmp" _stampa_py "$ROOT/share/archivio_dati.py" <<'EOF'
import os
a = {"/c": 1000, "/c/.cache": 100, "/c/.cache/chromium": 60, "/c/.cache/tod": 10, "/c/.config": 50, "/c/.config/x": 50,
     "/c/.local": 300, "/c/.local/share": 290, "/c/.local/share/Trash": 200, "/c/.local/share/tod-store": 80,
     "/c/.local/share/altro": 10, "/c/.local/bin": 10, "/c/src": 400, "/c/src/prova": 400, "/c/Desktop": 100}
b = {"/u": 2000, "/u/tmp": 600, "/u/tmp/x": 100, "/u/var": 1100, "/u/var/lib": 1100, "/u/var/lib/proot-distro": 1000,
     "/u/lib": 300}
R = "/u/var/lib/proot-distro/containers/debian/rootfs"
c = {R: 900, R + "/usr": 500, R + "/home": 300, R + "/home/dex": 300, R + "/home/dex/.cache": 40,
     R + "/home/dex/.config": 200, R + "/tmp": 100}
dd = {"/c/.local/share/Trash/files": 190, "/c/.local/share/Trash/files/a.iso": 190}
r = m.calcola(a, b, c, dd, (10000, 8000, 2000), casa="/c", prefix="/u", rootfs=R, case_cx=[R + "/home/dex"])
print({k: r["categorie"][k]["byte"] for k, *_ in m.CATEGORIE}, r["nostro"], r["libero"])
print(r["categorie"]["cestino"]["parti"], r["categorie"]["temporanei"]["parti"][0])
t = [("/u/tmp/big.log", 62259920062, False), ("/u/tmp/in-uso.log", 10**9, True)]
print([(k, n) for k, n, _x in m.consigli(r, t, [("/c/.cache/chromium", 2 * 10**8)])], m.consigli(r, [], []))
print(m.cache_pulibili(r, ["/usr/bin/chromium --type=gpu"]), m.cache_pulibili(r, []))
print(m.dimensione(62259920062), m.dimensione(500), m.dimensione(1.5e6), m.dimensione(120e9), m.dimensione(1e9),
      m.dimensione(2.5e9, False))
print(m.leggi_du("4\t/a\nnon una riga\n12\t/a/b\n"), m.figli({"/a": 9, "/a/b": 1, "/a/c": 5, "/a/c/d": 5}, "/a"))
T = os.environ["ARCH_TMP"]
print([(os.path.basename(p), aperto) for p, _n, aperto in m.temporanei_grandi([T], 5000, {T + "/grande.log"})],
      m.temporanei_grandi([T], 5000, set())[0][2])
print(m.leggibile("/c/src/x"), m.leggibile("/r/usr/lib"), m.leggibile("/u/lib"), m.leggibile("/altro"))
EOF
)
  eq "categorie senza doppioni, Android = il resto, consigli, cache aperte salve, misure" "{'android': 5000, 'termux': 400, 'debian': 600, 'store': 80, 'dati': 330, 'documenti': 150, 'sviluppo': 400, 'cache': 140, 'temporanei': 700, 'cestino': 200} 3000 2000
[('/c/.local/share/Trash/files/a.iso', 190), ('/c/.local/share/Trash/info', 10)] ('/u/tmp', 500)
[('temporanei', 62259920062), ('cache', 200000000)] []
[] [('/c/.cache/chromium', 60)]
62,3 GB 500 byte 1,5 MB 120 GB 1 GB 2.5 GB
{'/a': 4096, '/a/b': 12288} [('/a/c', 5), ('/a/b', 1)]
[('grande.log', True)] False
~/src/x Debian: /usr/lib Termux: /usr/lib /altro" "$out"
  # Pietro: "lo volevo nelle impostazioni" → Impostazioni → Archiviazione (come macOS); niente app a parte nello Start
  local s
  s=$(cat "$ROOT/share/tod-settings.py")
  has "voce Archiviazione nelle Impostazioni" '("archiviazione", T("Archiviazione", "Storage")' "$s"
  has "il pannello" "def pane_archiviazione(self, box):" "$s"
  has "Informazioni → Gestisci… apre Archiviazione" 'self.open_pane("archiviazione")' "$s"
  local k
  for k in android termux debian store dati documenti sviluppo cache temporanei cestino; do
    has "spiegazione della categoria $k" "\"$k\": T(" "$(sed -n '/ARCH_SPIEGA = {/,/^    }/p' "$ROOT/share/tod-settings.py")"
  done
  has "la voce dell'app a parte si toglie" 'rm -f "$apps/tod-archivio.desktop"' "$(cat "$ROOT/lib/desktop.sh")"
  hasnt "nessuna app a parte" "tod-archivio.py" "$(cat "$ROOT/lib/desktop.sh")"
}

test_gestione_attivita() { # 12 novembre, Pietro: Gestione attività e Cattura di XFCE "hanno l'aspetto Linux": nostre.
  # I dati (share/attivita_dati.py) su un /proc finto: nomi leggibili, processore tra due letture, processi raggruppati
  # sotto l'app della finestra, i pezzi del desktop che non si terminano
  command -v python3 >/dev/null || return 0
  local d="$WORK/att_$$" out
  mkdir -p "$d"
  out=$(TOD_ATTIVITA_PROC="$d/proc" _stampa_py "$ROOT/share/attivita_dati.py" <<'EOF'
import os
P = os.environ["TOD_ATTIVITA_PROC"]
def proc(pid, gen, tick, kb, argv, comm):
    os.makedirs(f"{P}/{pid}", exist_ok=True)
    open(f"{P}/{pid}/stat", "w").write(f"{pid} ({comm}) S {gen} 1 1 0 -1 0 0 0 0 0 {tick} 0 0 0 20 0 1 0")
    open(f"{P}/{pid}/cmdline", "wb").write(b"\0".join(a.encode() for a in argv) + b"\0")
    open(f"{P}/{pid}/smaps_rollup", "w").write(f"Rss: 9 kB\nPss: {kb} kB\n")
os.makedirs(P, exist_ok=True)
open(f"{P}/meminfo", "w").write("MemTotal: 12000000 kB\nMemFree: 1 kB\nMemAvailable: 4000000 kB\n")
proc(10, 1, 100, 50000, ["/x/ld.so", "--argv0", "/usr/bin/code", "--no-sandbox"], "ld.so")
proc(11, 10, 50, 20000, ["/x/ld.so", "--argv0", "/usr/share/code/code", "--type=zygote"], "ld.so")
proc(12, 11, 0, 10000, ["python3", "-u", "/repo/share/media/tod-foto.py"], "python3")
proc(20, 1, 300, 30000, ["xfwm4", "--compositor=on"], "xfwm4")
proc(21, 1, 0, 5000, ["bash", "/repo/tod", "_hook"], "bash")
a = m.Campione(P, ora=100.0)
print(sorted((p.pid, p.nome, p.kb) for p in a.processi.values()), a.mem_totale, a.mem_libera)
proc(10, 1, 100 + 400, 50000, ["/x/ld.so", "--argv0", "/usr/bin/code"], "ld.so")   # 400 tick in 2 s
b = m.Campione(P, ora=102.0)
u = m.cpu(a, b, tick=100, core=8)
print({k: round(v, 1) for k, v in sorted(u.items())}, m.cpu(None, b) == {p: 0.0 for p in b.processi})
app, resto = m.assegna(b.processi, {10: "code"})
print({k: sorted(v) for k, v in app.items()}, sorted(resto), m.discendenti(b.processi, 10))
print(m.protetto(b.processi[20], io=1), m.protetto(b.processi[10], io=1), m.protetto(b.processi[12], io=12),
      m.nome_leggibile(b.processi[20]), m.nome_leggibile(b.processi[21], False))
print(m.dimensione(512), m.dimensione(3500), m.dimensione(250000), m.dimensione(2400000), m.percento(4.56), m.percento(37.2))
print(m.nome_processo([], "kworker"), m.nome_processo(["/usr/bin/node", "--x", "/app/main.js"]))
EOF
)
  eq "nomi, memoria, processore, app con i suoi processi, protetti" "[(10, 'code', 50000), (11, 'code', 20000), (12, 'tod-foto.py', 10000), (20, 'xfwm4', 30000), (21, 'tod', 5000)] 12000000 4000000
{10: 25.0, 11: 0.0, 12: 0.0, 20: 0.0, 21: 0.0} True
{'code': [10, 11, 12]} [20, 21] [12, 11, 10]
True False True Gestore delle finestre tod
512 KB 3,4 MB 244 MB 2,3 GB 4,6% 37%
kworker main.js" "$out"
  # la Cattura (GTK: il Python di Termux): nome come il telefono, finestre tagliate ai bordi, cartella delle prove
  local py=/data/data/com.termux/files/usr/bin/python3
  if [[ -x $py ]] && "$py" -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null; then
    out=$(env -u LD_PRELOAD TOD_CATTURA_CARTELLA="$d/foto" TOD_TERMUX_HOME="$d" "$py" -c 'import importlib.util as u, sys
s = u.spec_from_file_location("m", sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)
exec(sys.stdin.read())' "$ROOT/share/tod-cattura.py" 2>&1 <<'EOF'
import time
print(m.nome_file(time.mktime((2026, 11, 12, 18, 5, 12, 0, 0, -1))), m.cartella_screenshot().endswith("/foto"))
print(m.dentro((-20, 10, 300, 200), 1920, 1080), m.dentro((1800, 1000, 400, 300), 1920, 1080), m.dentro((2000, 0, 10, 10), 1920, 1080))
EOF
)
    has "Cattura: nome e taglio" "Screenshot_20261112-180512.png True
(0, 10, 280, 200) (1800, 1000, 120, 80) None" "$out"
  else
    echo "  (PyGObject di Termux assente: Cattura saltata)"
  fi
  # le scorciatoie: Stamp e Ctrl+Maiusc+Esc alle nostre, quella cambiata dall'utente resta
  mkdir -p "$d/bin" "$d/xf"
  cat >"$d/bin/xfconf-query" <<'EOF'
#!/bin/sh
k=$(printf '%s' "$4" | tr '/<>' '___')
case " $* " in *" -s "*) eval "v=\${$#}"; printf '%s' "$v" >"$XF/$k" ;; *) [ -f "$XF/$k" ] && cat "$XF/$k" ;; esac
EOF
  chmod +x "$d/bin/xfconf-query"
  printf 'xfce4-screenshooter' >"$d/xf/_commands_custom_Print"
  printf 'flameshot gui' >"$d/xf/_commands_custom__Shift_Print"
  printf 'xkill' >"$d/xf/_commands_custom__Primary__Alt_Escape"
  out=$(PATH="$d/bin:$PATH" XF="$d/xf" TOD_ROOT=/r bash -c 'source "$1/lib/core.sh" 2>/dev/null; source "$1/lib/desktop.sh"; desktop_sistema_scorciatoie
    for f in "$XF"/*; do printf "%s=%s\n" "${f##*/}" "$(cat "$f")"; done' _ "$ROOT")
  has "Stamp alla nostra" "_commands_custom_Print=python3 /r/share/tod-cattura.py" "$out"
  has "quella cambiata dall'utente resta" "_commands_custom__Shift_Print=flameshot gui" "$out"
  has "Alt+Stamp: la finestra" "_commands_custom__Alt_Print=python3 /r/share/tod-cattura.py --finestra" "$out"
  has "come macOS: Win+Maiusc+3 tutto lo schermo" "_commands_custom__Super__Shift_3=python3 /r/share/tod-cattura.py --schermo" "$out"
  has "Win+Maiusc+3 anche come lo scrive XFCE (tastiera italiana: £)" "_commands_custom__Super__Shift_sterling=python3 /r/share/tod-cattura.py --schermo" "$out"
  has "come macOS: Win+Maiusc+4 una zona" "_commands_custom__Super__Shift_4=python3 /r/share/tod-cattura.py" "$out"
  has "Ctrl+Alt+Esc: la Gestione attività, non xkill che non c'è" "_commands_custom__Primary__Alt_Escape=python3 /r/share/tod-attivita.py" "$out"
  has "Ctrl+Maiusc+Esc: la Gestione attività" "_commands_custom__Primary__Shift_Escape=python3 /r/share/tod-attivita.py" "$out"
  has "le voci di XFCE nascoste nello Start" "xfce4-screenshooter.desktop" "$DESKTOP_USELESS"
  has "tasto destro sulla barra: la nostra Gestione attività" '"tod-attivita.py")])' "$(cat "$ROOT/share/tod-barra.py")"
  has "pannello Batteria: la nostra Gestione attività" 'avvia("python3", os.path.join(QUI, "tod-attivita.py"))' "$(cat "$ROOT/share/tod-pannelli.py")"
  has "e le nostre al loro posto" 'desktop_sistema_entries' "$(sed -n '/^desktop_install_entries()/,/^}/p' "$ROOT/lib/desktop.sh")"
}

test_stampa_conf() { # i filtri di CUPS partono con l'ambiente del modo diretto: PassEnv sta in cups-files.conf. In
  # cupsd.conf CUPS 2.4 lo ignora, e filtri e collegamenti cadevano con il segnale 31 (6 novembre)
  command -v python3 >/dev/null || return 0
  local out
  out=$(TMPDIR="$WORK/sc_tmp" TOD_TERMUX_HOME="$WORK/sc_home" _stampa_py "$ROOT/share/stampa/tod-stampa.py" <<'EOF'
m.ROOTFS = m.os.path.join(m.TMP, "radice-con-cups")   # con rastertopwg di CUPS il nostro filtro non si registra
m.os.makedirs(m.os.path.join(m.ROOTFS, "usr/lib/cups/filter"), exist_ok=True)
open(m.os.path.join(m.ROOTFS, "usr/lib/cups/filter/rastertopwg"), "w").close()
assert "tod-pdftourf\n" not in m.convs() and m.convs().startswith("#"), m.convs()
m.ROOTFS = m.os.path.join(m.TMP, "radice-senza")       # (il resto della prova: un container senza il pacchetto cups)
m.scrivi_conf()
f = open(m.os.path.join(m.CONF, "cups-files.conf")).read()
c = open(m.os.path.join(m.CONF, "cupsd.conf")).read()
pe = [r for r in f.splitlines() if r.startswith("PassEnv ")]
print(len(pe), all(v in pe[0].split() for v in ("GLIBC_TUNABLES", "FAKECHROOT_BASE", "LD_PRELOAD", "FAKECHROOT_EXCLUDE_PATH")))
print("PassEnv" in c, "Listen " + m.SOCK in c, "ErrorPolicy retry-job" in c, "DefaultAuthType None" in c)
print(oct(m.os.stat(m.RUN).st_mode & 0o777))
print(open(m.os.path.join(m.CONF, "tod.convs")).read().splitlines()[-1].split())
EOF
)
  eq "PassEnv nel file giusto, CUPS sul nostro socket, cartella privata, conversione verso URF" \
    $'1 True\nFalse True True True\n0o700\n[\'application/vnd.cups-pdf\', \'image/urf\', \'100\', \'tod-pdftourf\']' "$out"
}

test_stampa_pdftourf() { # stampanti solo AirPrint: Ghostscript con urfrgb o urfgray, alla risoluzione della stampante
  local fk="$WORK/spu_bin" ppd="$WORK/spu.ppd" out
  mkdir -p "$fk"
  printf '#!/bin/sh\necho "gs $*"\n' >"$fk/gs"
  chmod +x "$fk/gs"
  printf '*DefaultResolution: 600dpi\n*cupsUrfSupported: "CP1,IS1-4-5-19,RS300-600,SRGB24,V1.4,W8"\n' >"$ppd"
  out=$(PATH="$fk:$PATH" PPD="$ppd" sh "$ROOT/share/stampa/tod-pdftourf" 1 pietro prova 1 "" 2>/dev/null)
  has "a colori, 600 dpi, dallo stdin" "-sDEVICE=urfrgb -r600 -sOutputFile=- -" "$out"
  out=$(PATH="$fk:$PATH" PPD="$ppd" sh "$ROOT/share/stampa/tod-pdftourf" 1 pietro prova 1 "print-color-mode=monochrome Resolution=300dpi" /x.pdf 2>/dev/null)
  has "in bianco e nero chiesto dall'utente, 300 dpi, dal file" "-sDEVICE=urfgray -r300 -sOutputFile=- /x.pdf" "$out"
  printf '*DefaultResolution: 300dpi\n*cupsUrfSupported: "CP1,RS300,V1.4,W8"\n' >"$ppd"
  out=$(PATH="$fk:$PATH" PPD="$ppd" sh "$ROOT/share/stampa/tod-pdftourf" 1 pietro prova 1 "" 2>/dev/null)
  has "stampante solo in grigio" "-sDEVICE=urfgray -r300" "$out"
}

test_stampa_bonjour() { # stampanti Wi-Fi: risposte mDNS (nomi compressi), ipp preferito a ipps, anche le solo AirPrint (URF)
  command -v python3 >/dev/null || return 0
  local out
  out=$(TMPDIR="$WORK/sb_tmp" TOD_TERMUX_HOME="$WORK/sb_home" _stampa_py "$ROOT/share/stampa/tod-stampa.py" <<'EOF'
import struct
class Msg:   # una risposta mDNS, con i nomi compressi come le stampanti vere
    def __init__(s):
        s.b, s.vis, s.n = bytearray(struct.pack(">HHHHHH", 0, 0x8400, 0, 0, 0, 0)), {}, 0
    def nome(s, n):
        parti = n.split(".")
        for i in range(len(parti)):
            suff = ".".join(parti[i:]).lower()
            if suff in s.vis:
                s.b += struct.pack(">H", 0xC000 | s.vis[suff]); return
            s.vis[suff] = len(s.b)
            s.b += bytes([len(parti[i].encode())]) + parti[i].encode()
        s.b += b"\0"
    def rr(s, n, tipo, scrivi):
        s.nome(n); s.b += struct.pack(">HHI", tipo, 0x8001, 120); pos = len(s.b); s.b += b"\0\0"
        scrivi(); s.b[pos:pos + 2] = struct.pack(">H", len(s.b) - pos - 2); s.n += 1
    def txt(s, *kv):
        for x in kv: s.b += bytes([len(x)]) + x.encode()
    def fine(s):
        s.b[6:8] = struct.pack(">H", s.n); return bytes(s.b)
p = Msg()
hp, canon = "HP LaserJet MFP M140w._ipp._tcp.local", "Canon X._ipp._tcp.local"
p.rr("_ipp._tcp.local", 12, lambda: p.nome(hp))
p.rr("_ipps._tcp.local", 12, lambda: p.nome("HP LaserJet MFP M140w._ipps._tcp.local"))
p.rr("_ipp._tcp.local", 12, lambda: p.nome(canon))
p.rr(hp, 33, lambda: (p.b.extend(struct.pack(">HHH", 0, 0, 631)), p.nome("hp-m140w.local")))
p.rr(hp, 16, lambda: p.txt("rp=ipp/print", "ty=HP LaserJet MFP M140w", "pdl=application/pdf,image/pwg-raster", "UUID=564E4333-4B12"))
p.rr("HP LaserJet MFP M140w._ipps._tcp.local", 33, lambda: (p.b.extend(struct.pack(">HHH", 0, 0, 443)), p.nome("hp-m140w.local")))
p.rr("HP LaserJet MFP M140w._ipps._tcp.local", 16, lambda: p.txt("rp=ipp/print", "ty=HP LaserJet MFP M140w", "UUID=564E4333-4B12"))
p.rr("hp-m140w.local", 1, lambda: p.b.extend(bytes([192, 168, 1, 50])))
p.rr(canon, 33, lambda: (p.b.extend(struct.pack(">HHH", 0, 0, 631)), p.nome("canon.local")))
p.rr(canon, 16, lambda: p.txt("rp=ipp/print", "ty=Canon X", "pdl=image/urf", "URF=W8,SRGB24"))
rr = m.analizza(p.fine())
for x in sorted(m.stampanti_da(rr, {canon.lower(): "192.168.1.60"}), key=lambda x: x["uri"]):
    print(x["uri"], x["modello"], x["chiave"], x["senza_driver"])
print(m.analizza(m.domanda([("_ipp._tcp.local", 12)])))
print(m.nome_coda("HP LaserJet MFP M140w (A4)"), m.nome_coda("HP LaserJet", {"HP_LaserJet"}), m.nome_coda("ÀÈ"))
EOF
)
  eq "due stampanti, indirizzi giusti, nomi delle code" "ipp://192.168.1.50:631/ipp/print HP LaserJet MFP M140w 564e4333-4b12 True
ipp://192.168.1.60:631/ipp/print Canon X 192.168.1.60/ipp/print True
[]
HP_LaserJet_MFP_M140w_A4 HP_LaserJet_2 Stampante" "$out"
}

test_stampa_giro_usb() { # stampanti USB: una coda per ponte (IPP Everywhere), la prima diventa predefinita, ricollegata
  # aggiorna l'indirizzo, senza driver lo segnala; tolta dall'utente mentre è collegata non torna da sola
  command -v python3 >/dev/null || return 0
  local fk="$WORK/sgu_bin" out
  mkdir -p "$fk"
  cat >"$fk/lpstat" <<'EOF'
#!/bin/sh
case "$1" in
  -v) if [ -f "$FAKE/code" ]; then while read -r n u; do echo "device for $n: $u"; done <"$FAKE/code"; fi ;;
  -d) if [ -s "$FAKE/pred" ]; then echo "system default destination: $(cat "$FAKE/pred")"; else echo "no system default destination"; fi ;;
esac
EOF
  cat >"$fk/lpadmin" <<'EOF'
#!/bin/sh
echo "$*" >>"$FAKE/lpadmin.log"
nome="" uri=""
while [ $# -gt 0 ]; do case "$1" in -p) nome=$2; shift ;; -v) uri=$2; shift ;; -d) echo "$2" >"$FAKE/pred"; shift ;; esac; shift; done
if [ -n "$nome" ] && [ -n "$uri" ]; then
  { grep -v "^$nome " "$FAKE/code" 2>/dev/null; echo "$nome $uri"; } >"$FAKE/code.n"; mv "$FAKE/code.n" "$FAKE/code"
fi
EOF
  printf '#!/bin/sh\nexit 0\n' >"$fk/lpinfo"
  printf '#!/bin/sh\necho "$*" >>"$FAKE/lpadmin.log"\n' >"$fk/cupsenable"
  chmod +x "$fk"/*
  out=$(PATH="$fk:$PATH" FAKE="$fk" TMPDIR="$WORK/sgu_tmp" TOD_TERMUX_HOME="$WORK/sgu_home" \
    _stampa_py "$ROOT/share/stampa/tod-stampa.py" <<'EOF'
import json, os
os.makedirs(m.USB, exist_ok=True)
def usb(ident, **kv):
    json.dump(dict(id=ident, **kv), open(os.path.join(m.USB, ident + ".json"), "w"))
usb("st1", porta=40001, tipo="ipp", nome="HP DeskJet 2700", seriale="CN123", vendor="03f0", product="112a")
usb("st2", porta=40002, tipo="raw", device_id="MFG:Acme;MDL:Vecchia 1;CMD:ESCPL2;", nome="Acme Vecchia 1",
    seriale="", vendor="1234", product="0001")
m.avvisa = m.log = lambda *a: None
db = m.carica_db()
c = m.giro_usb(db, set())
print(sorted(c))
print(json.dumps(db["stampanti"], sort_keys=True))
print(sorted(db.get("senza_driver", {})))
usb("st1", porta=40011, tipo="ipp", nome="HP DeskJet 2700", seriale="CN123", vendor="03f0", product="112a")
m.giro_usb(db, c)
print(db["stampanti"]["usb:cn123"]["uri"])
db["tolte"].append("usb:cn123")
del db["stampanti"]["usb:cn123"]
m.giro_usb(db, c)
print("usb:cn123" in db["stampanti"])
EOF
)
  eq "code create e aggiornate" "['usb:1234:0001', 'usb:cn123']
{\"usb:cn123\": {\"modello\": \"HP DeskJet 2700\", \"nome\": \"HP_DeskJet_2700\", \"tipo\": \"usb\", \"uri\": \"ipp://127.0.0.1:40001/ipp/print\"}}
['usb:1234:0001']
ipp://127.0.0.1:40011/ipp/print
False" "$out"
  has "IPP Everywhere, senza driver" "-m everywhere" "$(sed -n 1p "$fk/lpadmin.log")"
  has "la prima è la predefinita" "-d HP_DeskJet_2700" "$(cat "$fk/lpadmin.log")"
}

test_stampa_usb_ponte() { # il ponte USB: interfacce IPP via USB (7/1/4) preferite, nome dall'ID 1284, byte nei due sensi
  # e quello che la stampante manda dopo la chiusura buttato (sennò rovinerebbe il collegamento dopo)
  command -v python3 >/dev/null || return 0
  local out
  out=$(TMPDIR="$WORK/sup_tmp" _stampa_py "$ROOT/share/stampa/tod-stampante-usb.py" <<'EOF'
import socket, threading, time
ep = lambda a: bytes([7, 5, a, 2, 0, 2, 0])
intf = lambda num, alt, proto, o, i: bytes([9, 4, num, alt, 2, 7, 1, proto, 0]) + ep(o) + ep(i)
dev = bytes([18, 1, 0, 2, 0, 0, 0, 64, 0xf0, 0x03, 0x2a, 0x11, 0, 1, 1, 2, 3, 1])
corpo = intf(0, 0, 2, 0x01, 0x82) + intf(1, 0, 4, 0x03, 0x84) + intf(2, 0, 2, 0x05, 0x86) + intf(2, 1, 4, 0x07, 0x88)
conf = bytes([9, 2, 9 + len(corpo), 0, 3, 1, 0, 0xc0, 50]) + corpo
d, i = m.descrittori(dev + conf)
tipo, canali = m.scegli(i)
print(d["vendor"], d["product"], tipo, [(c["num"], c["alt"], c["out"], c["in"]) for c in canali])
print(m.scegli([x for x in i if x["proto"] != 4])[0], m.scegli([])[0])
print(m.nome_di("MFG:HP;MDL:HP DeskJet 2700;CMD:PCL3GUI;", ""), "|", m.nome_di("MFG:Brother;MDL:HL-L2350DW;", ""), "|",
      m.nome_di("", "Stampante"))
class Finta:
    def __init__(s):
        s.scritto, s.da_leggere = b"", [b"HTTP/1.1 200 OK\r\n", b"Content-Length: 2\r\n\r\nok"]
    def scrivi(s, e, x):
        s.scritto += x
    def leggi(s, e, n, ms):
        time.sleep(0.01)
        return s.da_leggere.pop(0) if s.da_leggere and b"\r\n\r\n" in s.scritto else b""
a, b = socket.socketpair()
f = Finta()
t = threading.Thread(target=m.pompa, args=(b, f, 1, 0x82, 50))
t.start()
a.sendall(b"POST /ipp/print HTTP/1.1\r\nHost: x\r\n\r\n")
r = b""
while not r.endswith(b"ok"):
    r += a.recv(100)
f.da_leggere.append(b"avanzo")
a.close()
t.join(5)
print(f.scritto.split(b"\r\n")[0].decode(), "|", r.split(b"\r\n")[0].decode(), f.da_leggere, t.is_alive())
class Staccata(Finta):
    def leggi(s, e, n, ms):
        raise m.Staccata()
a, b = socket.socketpair()
try:
    m.pompa(b, Staccata(), 1, 0x82, 50)
    print("niente")
except m.Staccata:
    print("staccata")
EOF
)
  eq "interfacce, nome, byte" "03f0 112a ipp [(1, 0, 3, 132), (2, 1, 7, 136)]
raw None
HP DeskJet 2700 | Brother HL-L2350DW | Stampante
POST /ipp/print HTTP/1.1 | HTTP/1.1 200 OK [] False
staccata" "$out"
}

test_usb_stampanti() { # 6 novembre: una stampante USB non è un "non disco": va al ponte del servizio di stampa, anche
  # una multifunzione con il lettore di schede (disco e stampante insieme)
  local bin="$HOME/binst" run="$HOME/runst" f out i
  mkdir -p "$bin" "$run" "$TMPDIR/tod-stampa"
  f=$(sed -n '/^log() {/p;/^is_storage() {/,/^}/p;/^stampanti_collega() {/,/^}/p' "$ROOT/share/usb/tod-usb")
  printf '#!/bin/sh\necho %s\n' "'{\"classes\": [7], \"storage\": false, \"printer\": true}'" >"$bin/termux-usb"
  chmod +x "$bin/termux-usb"
  PATH="$bin:$PATH" RUN="$run" HERE=/x LOG="$run/log" bash -c "$f"$'\n''is_storage /dev/bus/usb/001/007' || true
  eq "per il servizio di stampa" "/dev/bus/usb/001/007" "$(cat "$run/stampanti")"
  eq "non tra i non dischi" "" "$(cat "$run/nondischi" 2>/dev/null)"
  printf '#!/bin/sh\necho %s\n' "'{\"classes\": [7, 8], \"storage\": true, \"printer\": true}'" >"$bin/termux-usb"
  out=$(PATH="$bin:$PATH" RUN="$run" HERE=/x LOG="$run/log" bash -c "$f"$'\n''is_storage /dev/bus/usb/001/008 && echo disco')
  eq "multifunzione: disco" "disco" "$out"
  eq "multifunzione: anche stampante" $'/dev/bus/usb/001/007\n/dev/bus/usb/001/008' "$(cat "$run/stampanti")"
  printf '#!/bin/sh\necho "$*" >>"%s"\n' "$run/lanci" >"$bin/termux-usb"
  printf '/dev/bus/usb/001/007\n' >"$run/stampanti"
  PATH="$bin:$PATH" RUN="$run" HERE="$ROOT/share/usb" LOG="$run/log" bash -c "$f"$'\n''stampanti_collega'
  for i in $(seq 1 50); do if [[ -s $run/lanci ]]; then break; fi; sleep 0.1; done
  has "ponte lanciato" "share/usb/../stampa/tod-stampante-usb.py st1 /dev/bus/usb/001/007" "$(cat "$run/lanci" 2>/dev/null)"
  eq "segnata come sua" "/dev/bus/usb/001/007" "$(cat "$run/stampa-dev-st1")"
  eq "elenco svuotato" "" "$(cat "$run/stampanti" 2>/dev/null)"
}

test_stampa_client_conf() { # le app di Termux e del container trovano il nostro CUPS da client.conf; senza CUPS nel
  # container il servizio non parte
  _fake_rootfs
  local c="$PREFIX/etc/cups/client.conf"
  stampa_client_conf
  has "Termux" "ServerName $TMPDIR/tod-stampa/cups.sock" "$(cat "$c")"
  has "container" "ServerName $TMPDIR/tod-stampa/cups.sock" "$(cat "$R/etc/cups/client.conf")"
  has "CUPS tra gli aiuti del container" "cups-daemon cups-filters" "$CX_AIUTI"
  if stampa_pronta; then echo "  ✗ CUPS dato per installato" >&2; return 1; fi
  setsid() { echo "setsid $*" >>"$WORK/scc_py_$$"; }   # mai un servizio vero dai test
  stampa_start
  eq "senza CUPS: il servizio non parte" "" "$(cat "$WORK/scc_py_$$" 2>/dev/null)"
  mkdir -p "$R/usr/sbin" "$R/usr/lib/cups/filter"
  printf '#!/bin/sh\n' >"$R/usr/sbin/cupsd"; cp "$R/usr/sbin/cupsd" "$R/usr/lib/cups/filter/pdftopdf"
  chmod +x "$R/usr/sbin/cupsd" "$R/usr/lib/cups/filter/pdftopdf"
  stampa_start
  for _ in 1 2 3 4 5 6 7 8 9 10; do if [[ -s $WORK/scc_py_$$ ]]; then break; fi; sleep 0.1; done
  has "con CUPS: il servizio parte" "share/stampa/tod-stampa.py sorveglia" "$(cat "$WORK/scc_py_$$" 2>/dev/null)"
  if cmp -s "$ROOT/share/stampa/tod-pdftourf" "$R/usr/lib/cups/filter/tod-pdftourf" && [[ -x $R/usr/lib/cups/filter/tod-pdftourf ]]; then :
  else echo "  ✗ filtro URF non installato nel container" >&2; return 1; fi
}

_scanner_py() { # _scanner_py: share/stampa/escl.py caricato come m (FINTO: tests/escl_finto.py); il codice da stdin
  FINTO="$ROOT/tests/escl_finto.py" TMPDIR="$WORK/scanner_tmp" TOD_TERMUX_HOME="$WORK/scanner_home" \
    _stampa_py "$ROOT/share/stampa/escl.py"
}

test_scanner_bonjour() { # scanner Wi-Fi (8 novembre, "stampanti e scanner"): Bonjour _uscan/_uscans, HTTP preferito a
  # HTTPS, uno per UUID, il percorso da rs; chi non ha indirizzo resta fuori
  command -v python3 >/dev/null || return 0
  local out
  out=$(_scanner_py <<'EOF'
rec = [("_uscan._tcp.local", 12, "HP OfficeJet [AB12]._uscan._tcp.local"),
       ("HP OfficeJet [AB12]._uscan._tcp.local", 33, (80, "hp.local")),
       ("HP OfficeJet [AB12]._uscan._tcp.local", 16, {"rs": "eSCL", "ty": "HP OfficeJet Pro 9010", "uuid": "ABC-1"}),
       ("_uscans._tcp.local", 12, "HP OfficeJet [AB12]._uscans._tcp.local"),
       ("HP OfficeJet [AB12]._uscans._tcp.local", 33, (443, "hp.local")),
       ("HP OfficeJet [AB12]._uscans._tcp.local", 16, {"rs": "/eSCL/", "ty": "HP OfficeJet Pro 9010", "uuid": "abc-1"}),
       ("hp.local", 1, "192.168.1.50"),
       ("_uscans._tcp.local", 12, "Canon TS5300._uscans._tcp.local"),
       ("Canon TS5300._uscans._tcp.local", 33, (443, "canon.local")),
       ("Canon TS5300._uscans._tcp.local", 16, {"rs": "eSCL"}),
       ("_uscan._tcp.local", 12, "Senza indirizzo._uscan._tcp.local"),
       ("Senza indirizzo._uscan._tcp.local", 33, (80, "boh.local"))]
for s in m.scanner_da(rec, {"canon ts5300._uscans._tcp.local": "192.168.1.60"}):
    print(s["chiave"], "|", s["nome"], "|", s["url"], "|", s["dove"])
EOF
)
  eq "HP in HTTP (una volta), Canon in HTTPS" "https://192.168.1.60:443/escl | Canon TS5300 | https://192.168.1.60:443/eSCL | wifi
abc-1 | HP OfficeJet Pro 9010 | http://192.168.1.50:80/eSCL | wifi" "$out"
}

test_scanner_capacita() { # cosa sa fare lo scanner (vetro, alimentatore fronte-retro, risoluzioni elencate o a
  # intervallo) e le impostazioni di una scansione: il foglio mai più grande dello scanner
  command -v python3 >/dev/null || return 0
  local out
  out=$(_scanner_py <<'EOF'
import importlib.util as u, os
s = u.spec_from_file_location("finto", os.environ["FINTO"]); f = u.module_from_spec(s); s.loader.exec_module(f)
c = m.analizza_capacita(f.CAPACITA.encode())
p, a = c["fonti"]["Platen"], c["fonti"]["Feeder"]
print(c["modello"], c["versione"], sorted(c["fonti"]))
print(p["colori"], p["dpi"], p["max_l"], p["max_a"])
print(a["dpi"], a["fronte_retro"], a["max_a"])
print(m.area(p, "a4"), m.area(p, "vetro"), m.area(a, "lettera"), m.area({"max_l": 2000, "max_a": 3000}, "a4"))
x = m.impostazioni_xml("Feeder", "Grayscale8", 150, 2480, 3508, versione="2.63", fronte_retro=True)
print([r.strip() for r in x.splitlines() if any(k in r for k in ("InputSource", "ColorMode", "Resolution", "Duplex", "FormatExt", "Width"))])
x = m.impostazioni_xml("Platen", "RGB24", 300, 2550, 3508, versione="2.0")
print("FormatExt" in x, "Duplex" in x, "<pwg:InputSource>Platen</pwg:InputSource>" in x)
try:
    m.analizza_capacita(b"<x")
except m.Errore:
    print("risposta rotta: errore")
EOF
)
  eq "capacità e impostazioni" "Multifunzione finta MF-100 2.63 ['Feeder', 'Platen']
['BlackAndWhite1', 'Grayscale8', 'RGB24'] [75, 150, 300, 600] 2550 3508
[100, 150, 200, 300] True 4200
(2480, 3508) (2550, 3508) (2550, 3300) (2000, 3000)
['<pwg:Width>2480</pwg:Width>', '<pwg:InputSource>Feeder</pwg:InputSource>', '<scan:ColorMode>Grayscale8</scan:ColorMode>', '<scan:XResolution>150</scan:XResolution>', '<scan:YResolution>150</scan:YResolution>', '<scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt>', '<scan:Duplex>true</scan:Duplex>']
False False True
risposta rotta: errore" "$out"
}

test_scanner_pdf() { # il PDF senza librerie: i JPEG dello scanner così come sono (colori e grigi), pagine grandi
  # quanto il foglio (il dpi del JPEG, sennò quello chiesto), indice (xref) giusto
  command -v python3 >/dev/null || return 0
  local out
  out=$(_scanner_py <<'EOF'
import importlib.util as u, os, re
s = u.spec_from_file_location("finto", os.environ["FINTO"]); f = u.module_from_spec(s); s.loader.exec_module(f)
j1, j2 = f.jpeg_finto(1240, 1754, 3, 150), f.jpeg_finto(600, 800, 1, 0)
print(m.misure_jpeg(j1), m.misure_jpeg(j2), m.misure_jpeg(b"non un jpeg"))
d = m.pdf([(j1, 300), (j2, 100)])
print(d[:8], d.count(b"/Type /Page "), re.findall(rb"/MediaBox \[([^\]]*)\]", d), re.findall(rb"/ColorSpace (/\w+)", d))
x = int(d.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
voci = d[x:].split(b"\n")
n = int(voci[1].split()[1])
print(n, all(d[int(voci[2 + i][:10]):].startswith(b"%d 0 obj" % i) for i in range(1, n)), d.endswith(b"%%EOF\n"))
EOF
)
  eq "PDF da due JPEG" "(1240, 1754, 3, 150) (600, 800, 1, 0) None
b'%PDF-1.4' 2 [b'0 0 595.20 841.92', b'0 0 432.00 576.00'] [b'/DeviceRGB', b'/DeviceGray']
9 True True" "$out"
}

test_scanner_flusso() { # una scansione vera con lo scanner finto (tests/escl_finto.py): alimentatore fronte-retro in
  # grigi (si scalda: 503, poi 3 pagine in un PDF), vetro in JPEG; Location "localhost" del ponte USB; Host: localhost;
  # ogni lavoro cancellato alla fine. Poi gli scanner USB (ponte IPP via USB), quelli con l'indirizzo, e nessuno
  command -v python3 >/dev/null || return 0
  local d="$WORK/scanner_flusso" porta pid out u
  mkdir -p "$d"
  _sc() { TMPDIR="$WORK/scanner_tmp" TOD_TERMUX_HOME="$WORK/scanner_home" TOD_SCANNER_SENZA_RETE=1 TOD_SCANNER_PAUSA=0.05 \
    LANGUAGE=it python3 "$ROOT/share/stampa/tod-scanner.py" "$@"; }
  out=$(_sc scansiona "$d/niente.pdf" 2>&1) && { echo "  ✗ scansione senza scanner" >&2; return 1; }
  eq "senza scanner" "Nessuno scanner trovato" "$out"
  # (niente uscita del test aperta e 60 s al massimo: se un controllo fallisce prima del kill, non resta appeso)
  python3 "$ROOT/tests/escl_finto.py" --pagine 3 --occupato 1 --registro "$d/registro" --per 60 \
    </dev/null >"$d/porta" 2>"$d/errori" &
  pid=$!
  for _ in $(seq 50); do if [[ -s $d/porta ]]; then break; fi; sleep 0.1; done
  porta=$(cat "$d/porta")
  u="http://127.0.0.1:$porta/eSCL"
  out=$(_sc scansiona "$d/a.pdf" --scanner "$u" --alimentatore --fronte-retro --grigi --dpi 200 2>&1)
  eq "alimentatore: il PDF" "$d/a.pdf" "$out"
  eq "tre pagine" 3 "$(grep -ac '/Type /Page ' "$d/a.pdf")"
  out=$(_sc scansiona "$d/b.jpg" --scanner "$u" --foglio vetro 2>&1)
  eq "vetro: un JPEG" "$d/b.jpg" "$out"
  [[ ! -e "$d/b (2).jpg" ]] || { echo "  ✗ dal vetro più di una pagina" >&2; return 1; }
  out=$(cat "$d/registro")
  has "fronte-retro in grigi" $'<pwg:InputSource>Feeder</pwg:InputSource>\n  <scan:ColorMode>Grayscale8</scan:ColorMode>\n  <scan:XResolution>200</scan:XResolution>' "$out"
  has "fronte-retro chiesto" "<scan:Duplex>true</scan:Duplex>" "$out"
  has "tutto il vetro, a colori" $'<pwg:Width>2550</pwg:Width>\n      <pwg:Height>3508</pwg:Height>' "$out"
  has "vetro a colori" $'<pwg:InputSource>Platen</pwg:InputSource>\n  <scan:ColorMode>RGB24</scan:ColorMode>' "$out"
  eq "lavori cancellati alla fine" "DELETE /eSCL/ScanJobs/1 Host=localhost
DELETE /eSCL/ScanJobs/2 Host=localhost" "$(grep '^DELETE' <<<"$out")"
  eq "sempre Host: localhost" "" "$(grep -E '^(GET|POST|DELETE) ' <<<"$out" | grep -v 'Host=localhost$')"
  local usb="$WORK/scanner_tmp/tod-stampa/usb"
  mkdir -p "$usb"
  printf '{"id":"001","tipo":"ipp","porta":%s,"nome":"Multifunzione USB","seriale":"SN1"}' "$porta" >"$usb/001.json"
  printf '{"id":"002","tipo":"raw","porta":1,"nome":"Solo stampa"}' >"$usb/002.json"
  out=$(_sc aggiungi "127.0.0.1:1" 2>&1) && { echo "  ✗ aggiunto uno scanner che non c'è" >&2; return 1; }
  # (spento, oppure acceso ma senza scansione in rete: dipende da cosa gira su questa macchina; mai aggiunto)
  has "indirizzo sbagliato: un errore che nomina l'indirizzo" "«127.0.0.1:1»" "$out"
  eq "con l'indirizzo" "Multifunzione finta MF-100 · http://localhost:$porta/eSCL" "$(_sc aggiungi "localhost:$porta" 2>&1)"
  out=$(_sc elenco --json | python3 -c 'import json, sys
for s in json.load(sys.stdin):
    print(s["chiave"], s["nome"], s["url"], s["dove"])')
  eq "USB (IPP via USB) e con l'indirizzo; la solo stampa no" "usb:sn1 Multifunzione USB $u usb
indirizzo:http://localhost:$porta/escl Multifunzione finta MF-100 http://localhost:$porta/eSCL indirizzo" "$out"
  _sc togli "indirizzo:http://localhost:$porta/escl"
  eq "tolto" "usb:sn1" "$(_sc elenco --json | python3 -c 'import json, sys; print(" ".join(s["chiave"] for s in json.load(sys.stdin)))')"
  out=$(_sc scansiona "$d/c.pdf" 2>&1)
  eq "senza --scanner: quello che c'è (USB)" "$d/c.pdf" "$out"
  kill "$pid" 2>/dev/null || true
}

test_scanner_voce() { # "Scanner" nello Start solo con la stampa accesa; tod scanner arriva a tod-scanner.py; la pagina
  # delle Impostazioni ha il gruppo Scanner
  local f="$HOME/.local/share/applications/tod-scanner.desktop"
  desktop_scanner_entry
  has "voce nello Start" "Exec=python3 $ROOT/share/stampa/tod-scanner.py" "$(cat "$f")"
  has "riconosciuta dalla barra" "StartupWMClass=tod-scanner" "$(cat "$f")"
  conf_set STAMPA 0
  desktop_scanner_entry
  conf_set STAMPA 1
  [[ ! -e $f ]] || { echo "  ✗ voce rimasta con la stampa spenta" >&2; return 1; }
  has "tod scanner" "scanner | scan) scanner_cli" "$(cat "$ROOT/tod")"
  has "Impostazioni: gruppo Scanner" "self.fill_scanner()" "$(sed -n '/def pane_stampanti/,/def fill_stampanti/p' "$ROOT/share/tod-settings.py")"
  command -v python3 >/dev/null || return 0
  python3 -m py_compile "$ROOT/share/stampa/tod-scanner.py" "$ROOT/share/stampa/escl.py" "$ROOT/share/tod-settings.py"
}

test_scoperta_wsd_netbios() { # i PC Windows: la risposta WS-Discovery (Computer sì, stampanti no) e il nome NetBIOS
  command -v python3 >/dev/null || return 0
  local out
  out=$(_stampa_py "$ROOT/share/scoperta.py" <<'EOF'
import struct
pc = b'<soap:Envelope><soap:Body><wsd:ProbeMatches><wsd:ProbeMatch><wsd:Types>wsdp:Device  pub:Computer</wsd:Types>' \
     b'<wsd:XAddrs>http://192.168.1.23:5357/abc/</wsd:XAddrs></wsd:ProbeMatch></wsd:ProbeMatches></soap:Body></soap:Envelope>'
st = b'<wsd:Types>wsdp:Device wprt:PrintDeviceType</wsd:Types>'
print(m.analizza_wsd(pc), "Computer" in m.analizza_wsd(st)[0])
q = m.pacchetto_nbstat()
print(len(q), q[12], q[13:15], q[-4:].hex())
nomi = [(b"WORKGROUP", 0x00, 0x8400), (b"PC-PIETRO", 0x00, 0x0400), (b"PC-PIETRO", 0x20, 0x0400)]
r = struct.pack(">HHHHHH", 0x746f, 0x8400, 0, 1, 0, 0) + b"\xc0\x0c" + struct.pack(">HHIH", 0x21, 1, 0, 1 + 18 * 3) + bytes([3])
for n, s, f in nomi:
    r += n.ljust(15) + bytes([s]) + struct.pack(">H", f)
print(m.analizza_nbstat(r), repr(m.analizza_nbstat(b"corto")))
EOF
)
  eq "WS-Discovery e NetBIOS" "('wsdp:Device pub:Computer', ['http://192.168.1.23:5357/abc/']) False
50 32 b'CK' 00210001
PC-PIETRO ''" "$out"
}

_rete_py() { # _rete_py: tod-rete.py caricato come m, con case e cartelle di prova; il codice da stdin
  TMPDIR="$WORK/rete_tmp" TOD_TERMUX_HOME="$WORK/rete_home" TOD_SMB_BIN="${TOD_SMB_BIN:-/nessuno}" \
    _stampa_py "$ROOT/share/rete/tod-rete.py"
}

test_volume_del_telefono() { # 11 novembre, Pietro: "non possiamo aumentare o diminuire il volume del telefono?". Il cursore
  # della barra regolava solo PulseAudio: col telefono a 5 su 15 il 100% restava basso. Ora un volume solo
  # (share/tod_volume.py → termux-volume music N), l'uscita di PulseAudio al 100%
  command -v python3 >/dev/null || return 0
  local d="$WORK/volume_$$" out
  mkdir -p "$d/bin"
  cat >"$d/bin/termux-volume" <<'EOF'
#!/bin/sh
# finto: il volume in un file, i comandi ricevuti in un registro
[ -f "$TOD_FINTO" ] || echo 5 >"$TOD_FINTO"
if [ $# -eq 0 ]; then
  echo "[{\"stream\":\"call\",\"volume\":9,\"max_volume\":15},{\"stream\":\"music\",\"volume\":$(cat "$TOD_FINTO"),\"max_volume\":15}]"
else
  sleep 0.2; echo "$1 $2" >>"$TOD_FINTO.log"; echo "$2" >"$TOD_FINTO"
fi
EOF
  chmod +x "$d/bin/termux-volume"
  out=$(PATH="$d/bin:$PATH" TOD_FINTO="$d/finto" TOD_VOLUME_STATO="$d/stato.json" TMPDIR="$d" python3 - "$ROOT/share" <<'EOF'
import os, sys
sys.path.insert(0, sys.argv[1])
import tod_volume as v
v._pulse_fatto = True   # niente pactl nelle prove
finto = os.environ["TOD_FINTO"]
print(v.telefono(), v.stato(), v.leggi_telefono(), v.stato())
for p in (40, 60, 80, 100):   # il cursore trascinato
    v.metti(p)
print(v.stato())
v.aspetta()
r = open(finto + ".log").read().split()
print(len(r) // 2 <= 2, r[-2:], open(finto).read().strip())
v.muto(True); v.aspetta(); print(v.stato(), open(finto).read().strip())
v.muto(False); v.aspetta(); print(v.stato())
open(finto, "w").write("3")   # i tasti del telefono, subito dopo un nostro comando: non si torna indietro...
print(v.leggi_telefono())
d = v._carica(); d["messo"] = 0; v._salva(d)   # ... e più tardi sì
print(v.leggi_telefono(), v.gradini(1, 15), v.gradini(0, 15), v.gradini(100, 15))
os.environ["TOD_VOLUME_INTERNO"] = "1"
print(v.telefono())
EOF
)
  eq "letto, messo (solo l'ultimo valore al telefono), muto e ritorno, tasti del telefono" "True None (33, False) (33, False)
(100, False)
True ['music', '15'] 15
(0, True) 0
(100, False)
(100, False)
(20, False) 1 0 15
False" "$out"
  has "barra: l'icona dallo stato del telefono" "tv.stato()" "$(sed -n '/elif nome == "volume":/,/return/p' "$ROOT/share/tod-barra.py")"
  has "pannello: il cursore muove il telefono" "lambda v: tv.metti(v)" "$(cat "$ROOT/share/tod-pannelli.py")"
  has "Impostazioni → Suono" "tv.metti(int(s.get_value()))" "$(cat "$ROOT/share/tod-settings.py")"
  has "il demone rilegge il volume (tasti, pannello di Android)" "tod_volume.leggi_telefono()" "$(cat "$ROOT/share/tod-phone-status.py")"
  has "PulseAudio pieno all'avvio" 'audio_pactl set-sink-volume "$m" 100%' "$(cat "$ROOT/lib/session.sh")"
  has "Lettore: volume dell'app pieno di serie" 'd.get("volume", 100)' "$(cat "$ROOT/share/media/tod-lettore.py")"
}

test_rete_alias() { # il nome del socket di un PC è lo stesso in tod-rete.py, tod-fs.c e tod-usb (byte per byte)
  command -v python3 >/dev/null || return 0
  local nomi=("PC di Pietro" "NAS/Casa" "UNITÀ USB" "Mac.local") py sh="" n f
  py=$(_rete_py <<'EOF'
for n in ["PC di Pietro", "NAS/Casa", "UNITÀ USB", "Mac.local"]:
    print(m.alias_di(n))
EOF
)
  f=$(sed -n '/^alias_of() {/p' "$ROOT/share/usb/tod-usb")
  for n in "${nomi[@]}"; do sh+=$(bash -c "$f"$'\n''alias_of "$1"' _ "$n")$'\n'; done
  eq "come tod-usb (e quindi tod-fs.c)" "${sh%$'\n'}" "$py"
  eq "nomi" $'pc-di-pietro\nnas_casa\nunit---usb\nmac.local' "$py"
}

test_rete_nomi() { # 8 novembre, la FRITZ!Box di Pietro: due annunci Bonjour ("fritz-box" e "192-168-178-1"), "fritz.box"
  # con due indirizzi (192.168.178.96 non è un PC): il nome vero e l'indirizzo di chi ha risposto; il PC Windows da WS-Discovery
  command -v python3 >/dev/null || return 0
  local out
  out=$(_rete_py <<'EOF'
rec = [("_smb._tcp.local", 12, "fritz-box._smb._tcp.local"), ("fritz-box._smb._tcp.local", 33, (445, "fritz.box")),
       ("_smb._tcp.local", 12, "192-168-178-1._smb._tcp.local"), ("192-168-178-1._smb._tcp.local", 33, (445, "fritz.box")),
       ("fritz.box", 1, "192.168.178.96"), ("fritz.box", 1, "192.168.178.1")]
sorg = {"fritz-box._smb._tcp.local": "192.168.178.1", "192-168-178-1._smb._tcp.local": "192.168.178.1"}
c = m.candidati(rec, sorg, {"192.168.178.1": "pub:Computer", "192.168.178.46": "pub:Computer"})
print(sorted((ip, v["nomi"], v["porta"], v["da"]) for ip, v in c.items()))
print(m.nome_migliore(c["192.168.178.1"]["nomi"], "192.168.178.1"), "|", m.nome_migliore(["", "192-168-178-1", "fritz"], "x"),
      "|", m.nome_migliore(["192-168-178-1", ""], "192.168.178.1"))
EOF
)
  eq "FRITZ!Box e PC Windows" "[('192.168.178.1', ['fritz-box', '192-168-178-1'], 445, 'bonjour'), ('192.168.178.46', [], 445, 'wsd')]
fritz-box | fritz | PC 192.168.178.1" "$out"
}

test_rete_password() { # la password: prima da ospite, poi la finestra; ricordata nel portachiavi; quella rifiutata si
  # richiede con l'avviso; con una password in memoria nessuna finestra
  command -v python3 >/dev/null || return 0
  local out
  out=$(_rete_py <<'EOF'
chieste, salvate = [], []
m.log = lambda *a: None
m.password_di = lambda pc, u: None
m.ricorda_password = lambda pc, u, p: salvate.append((pc, u, p))
s = m.Servizio()
def chiedi(nome, utente, dominio, errore):
    chieste.append((nome, utente, errore))
    return {"utente": "pietro", "password": "a\tb%c", "dominio": "", "ricorda": True}
s.risposta("ENTRA\tPC-PIETRO\n", None)   # l'utente ha aperto il PC
print(s.risposta("AUTH\tPC-PIETRO\t192.168.1.23\tIPC$\tprimo\n", chiedi).strip())
print(s.risposta("AUTH\tPC-PIETRO\t192.168.1.23\tIPC$\tospite\n", chiedi).strip())
print(chieste, salvate, m.carica_conf()["credenziali"])
print(s.risposta("AUTH\tPC-PIETRO\t192.168.1.23\tDocumenti\tprimo\n", chiedi).strip())
print(s.risposta("AUTH\tPC-PIETRO\t192.168.1.23\tDocumenti\trifiutate\n", chiedi).strip(), chieste[-1])
print(s.risposta("sbagliata", chiedi).strip())
EOF
)
  eq "ospite, finestra, ricordata, rifiutata" $'NO\nOK\t-\tpietro\ta%09b%25c\n'"[('PC-PIETRO', '', False)] [('PC-PIETRO', 'pietro', 'a\\tb%c')] {'PC-PIETRO': {'utente': 'pietro', 'dominio': ''}}"$'\nOK\t-\tpietro\ta%09b%25c\nOK\t-\tpietro\ta%09b%25c'" ('PC-PIETRO', 'pietro', True)"$'\nNO' "$out"
}

test_rete_solo_se_aperto() { # 11 novembre, Pietro: aprendo la Rete compariva subito la richiesta di password, per PC che
  # non aveva scelto, ed Esplora file restava bloccato (da chiudere a forza). Thunar guarda dentro ogni sottocartella
  # dal filo della finestra: sulla Rete quel controllo riceve subito un errore; la password si chiede solo per il PC
  # che l'utente ha aperto ("ENTRA" da Esplora file e dalla finestra Apri/Salva), e solo per un po'
  command -v python3 >/dev/null || return 0
  local out g
  out=$(_rete_py <<'EOF'
chieste, righe = [], []
m.log = lambda *a: righe.append(a[0])
m.password_di = lambda pc, u: None
s = m.Servizio()
def chiedi(nome, utente, dominio, errore):
    chieste.append(nome)
    return {"utente": "pietro", "password": "x", "dominio": ""}
r = [s.risposta(f"AUTH\tPC-UNO\t192.168.0.2\tIPC$\t{x}\n", chiedi).strip() for x in ("primo", "ospite", "ospite")]
print(r, chieste, len([x for x in righe if "non la chiedo" in x]))
print(repr(s.risposta("ENTRA\tPC-UNO\n", None)), s.risposta("AUTH\tPC-DUE\t192.168.0.3\tIPC$\tospite\n", chiedi).strip(),
      s.risposta("AUTH\tPC-UNO\t192.168.0.2\tIPC$\tospite\n", chiedi).strip(), chieste)
s.memoria.clear()
s.entrate["PC-UNO"] -= m.INTENTO + 1
print(s.risposta("AUTH\tPC-UNO\t192.168.0.2\tIPC$\tospite\n", chiedi).strip(), chieste)
EOF
)
  eq "senza apertura niente finestra (una riga sola nel registro); dopo ENTRA solo quel PC; poi scade" "['NO', 'NO', 'NO'] [] 1
'OK\n' NO OK	-	pietro	x ['PC-UNO']
NO ['PC-UNO']" "$out"
  g=$(sed -n '/^void \*g_file_enumerate_children(/,/^}/p' "$ROOT/share/tod-gettext.c")
  has "Esplora file: il controllo dal filo della finestra non tocca la Rete" 'if ((long)getpid() == syscall(SYS_gettid)) {' "$g"
  has "… e riceve un errore vero (mai NULL senza errore)" 'G_IO_ERROR_NOT_SUPPORTED' "$g"
  has "l'apertura vera di un PC lo dice al servizio" 'tod_rete_entra(p);' "$g"
  has "il messaggio" 'ENTRA\t%.*s\n' "$(sed -n '/^static void tod_rete_entra(/,/^}/p' "$ROOT/share/tod-gettext.c")"
  has "anche la finestra Apri/Salva" 'rete_entra(percorso)' "$(sed -n '/    def vai(/,/self.riempi()/p' "$ROOT/share/tod_apri_salva.py")"
}

test_rete_annulla() { # 8 novembre: "Annulla" sulla FRITZ!Box e la finestra tornava 20 volte in 20 secondi (Esplora file
  # riprova). Dopo "Annulla" quel PC tace per PAUSA secondi, gli altri no; poi la finestra torna
  command -v python3 >/dev/null || return 0
  local out
  out=$(_rete_py <<'EOF'
chieste = []
m.log = lambda *a: None
m.password_di = lambda pc, u: None
s = m.Servizio()
def chiedi(nome, utente, dominio, errore):
    chieste.append(nome)
    return None
s.risposta("ENTRA\tfritz-box\n", None)
s.risposta("ENTRA\tPC-PIETRO\n", None)
r = [s.risposta(f"AUTH\tfritz-box\t192.168.178.1\tIPC$\t{x}\n", chiedi).strip() for x in ("ospite", "ospite", "rifiutate", "ospite")]
r.append(s.risposta("AUTH\tPC-PIETRO\t192.168.178.43\tIPC$\tospite\n", chiedi).strip())
s.annullate["fritz-box"] -= m.PAUSA
r.append(s.risposta("AUTH\tfritz-box\t192.168.178.1\tIPC$\tospite\n", chiedi).strip())
print(r, chieste)
EOF
)
  eq "una finestra sola per PC, poi di nuovo" "['NO', 'NO', 'NO', 'NO', 'NO', 'NO'] ['fritz-box', 'PC-PIETRO', 'fritz-box']" "$out"
}

test_rete_ricordati() { # 8 novembre: DESKTOP-I78AUDA risponde alla ricerca 3 volte su 4, e dopo l'avvio compariva dopo un
  # minuto. I PC con la password salvata si provano subito al loro ultimo indirizzo (se lì risponde ancora lo stesso PC);
  # l'indirizzo si ricorda con la password e segue il PC
  command -v python3 >/dev/null || return 0
  local out
  out=$(TMPDIR="$WORK/rete_tmp_ric" TOD_TERMUX_HOME="$WORK/rete_home_ric" TOD_SMB_BIN=/nessuno \
    _stampa_py "$ROOT/share/rete/tod-rete.py" <<'EOF'
import os
os.makedirs(m.RUN, exist_ok=True)
m.log = lambda *a: None
m.password_di = lambda pc, u: None
m.ricorda_password = lambda pc, u, p: True
m.scoperta.chiedi_bonjour = lambda tipi, attesa: ([], {}, None)
m.scoperta.chiedi_wsd = lambda attesa: {}
aperte = {"192.168.178.43", "192.168.178.50", "192.168.178.60", "192.168.178.61"}
m.porta_aperta = lambda ip, porta, attesa=1.0: ip in aperte
nb = {"192.168.178.43": "DESKTOP-I78AUDA", "192.168.178.50": "ALTRO-PC", "192.168.178.61": "PC-DI-PIETRO-ST"}
m.scoperta.nome_netbios = lambda ip, attesa=1.0: nb.get(ip, "")
def c(ip):
    return {"utente": "Pietro", "dominio": "", "indirizzo": ip, "porta": 445}
m.salva_conf({"pc": [{"nome": "MANUALE", "indirizzo": "192.168.178.90", "porta": 445}],
              "credenziali": {"DESKTOP-I78AUDA": c("192.168.178.43"), "PC-VECCHIO": c("192.168.178.50"),
                              "NAS": c("192.168.178.60"), "PC-DI-PIETRO-STUDIO": c("192.168.178.61"),
                              "SPENTO": c("192.168.178.70"), "MANUALE": c("192.168.178.90"),
                              "SENZA": {"utente": "x", "dominio": ""}}})
r = m.pc_ricordati(m.carica_conf())
print(sorted(r))
print([(p["nome"], p["ip"], p["da"]) for p in m.cerca_pc(ricordati=r)])
m.scoperta.chiedi_wsd = lambda attesa: {"192.168.178.43": "pub:Computer"}
print([(p["nome"], p["da"]) for p in m.cerca_pc(ricordati=r)][:1])
s = m.Servizio()
s.aggiorna([{"nome": "DESKTOP-I78AUDA", "ip": "192.168.178.44", "porta": 445, "da": "wsd"},
            {"nome": "PC-NUOVO", "ip": "192.168.178.80", "porta": 445, "da": "wsd"}])
print(m.carica_conf()["credenziali"]["DESKTOP-I78AUDA"]["indirizzo"], "PC-NUOVO" in m.carica_conf()["credenziali"])
chiedi = lambda nome, u, d, e: {"utente": "pietro", "password": "x", "dominio": "", "ricorda": True}
s.risposta("ENTRA\tPC-NUOVO\n", None)
s.risposta("AUTH\tPC-NUOVO\t192.168.178.80\tIPC$\tospite\n", chiedi)
print(m.carica_conf()["credenziali"]["PC-NUOVO"])
s.chiudi()
EOF
)
  eq "ricordati: subito, solo se è lo stesso PC; l'indirizzo segue" "['DESKTOP-I78AUDA', 'NAS', 'PC-DI-PIETRO-STUDIO', 'PC-VECCHIO', 'SPENTO']
[('DESKTOP-I78AUDA', '192.168.178.43', 'ricordato'), ('NAS', '192.168.178.60', 'ricordato'), ('PC-DI-PIETRO-STUDIO', '192.168.178.61', 'ricordato')]
[('DESKTOP-I78AUDA', 'wsd')]
192.168.178.44 False
{'utente': 'pietro', 'dominio': '', 'indirizzo': '192.168.178.80', 'porta': 445}" "$out"
}

test_rete_elenco_pc() { # i PC trovati e quelli aggiunti a mano: elenco per tod-fs, un tod-smb per PC, i PC spariti via
  command -v python3 >/dev/null || return 0
  local smb="$WORK/rete_smb_finto" out
  printf '#!/bin/sh\necho "$*" >>"%s"\nexec sleep 30\n' "$WORK/rete_smb_args" >"$smb"
  chmod +x "$smb"
  out=$(TOD_SMB_BIN="$smb" _rete_py <<'EOF'
import os, time
os.makedirs(m.RUN, exist_ok=True)
m.log = lambda *a: None
s = m.Servizio()
m.principale(["x", "aggiungi", "smb://192.168.1.9:4450/", "NAS di casa"])
s.aggiorna(m.tutti_i_nomi([{"nome": "PC-PIETRO", "ip": "192.168.1.23", "porta": 445},
                           {"nome": "PC-PIETRO", "ip": "192.168.1.24", "porta": 445}]))
print(open(m.VOLUMI).read().strip())
time.sleep(0.5)
print(sorted(open(os.environ["TMPDIR"] + "/../rete_smb_args").read().split("\n")))
s.pc["PC-PIETRO"]["visto"] -= 3600
s.aggiorna([])
print(open(m.VOLUMI).read().strip())
print(sorted(n for n, p in s.proc.items() if p.poll() is None))
m.principale(["x", "togli", "NAS di casa"])
s.aggiorna([])
print(repr(open(m.VOLUMI).read()), m.carica_conf()["pc"])
s.chiudi()
EOF
)
  local r="$WORK/rete_tmp/tod-rete"
  eq "elenco e tod-smb" $'NAS di casa\t192.168.1.9\nPC-PIETRO\t192.168.1.23\nPC-PIETRO (192.168.1.24)\t192.168.1.24\n'"['', '$r nas-di-casa 192.168.1.9:4450 NAS di casa', '$r pc-pietro 192.168.1.23 PC-PIETRO', '$r pc-pietro--192.168.1.24- 192.168.1.24 PC-PIETRO (192.168.1.24)']"$'\nNAS di casa\t192.168.1.9\nPC-PIETRO (192.168.1.24)\t192.168.1.24\n'"['NAS di casa', 'PC-PIETRO (192.168.1.24)']"$'\n'"'PC-PIETRO (192.168.1.24)\\t192.168.1.24\\n' []" "$out"
}

test_rete_cambio_rete() { # 9 novembre, Pietro: "metti che cambio rete in corso d'opera". Rete nuova: i PC della vecchia
  # spariscono subito con i loro tod-smb (niente resta appeso), quelli a mano restano con un tod-smb nuovo, e si cerca più
  # volte; senza rete per poco non succede niente, per più di PERSA sì; una ricerca della rete di prima si butta
  command -v python3 >/dev/null || return 0
  local smb="$WORK/rete_cambio_smb" out
  printf '#!/bin/sh\nexec sleep 30\n' >"$smb"
  chmod +x "$smb"
  out=$(TMPDIR="$WORK/rete_tmp_cambio" TOD_TERMUX_HOME="$WORK/rete_home_cambio" TOD_SMB_BIN="$smb" \
    _stampa_py "$ROOT/share/rete/tod-rete.py" <<'EOF'
import os, time
os.makedirs(m.RUN, exist_ok=True)
registro = []
m.log = registro.append
rete = ["192.168.178.20"]
m.rete_attuale = lambda: rete[0]
vol = lambda: open(m.VOLUMI).read().strip().replace("\t", " ").replace("\n", ", ")
s = m.Servizio()
m.principale(["x", "aggiungi", "192.168.178.90", "NAS"])
print(s.controlla_rete(0), s.rete)
s.aggiorna([{"nome": "DESKTOP-I78AUDA", "ip": "192.168.178.43", "porta": 445}])
prima = dict(s.proc)
print(vol(), "|", s.controlla_rete(1))
rete[0] = "192.168.1.10"
print(s.controlla_rete(2), s.raffica, vol())
time.sleep(0.3)
print(sorted(n for n, p in prima.items() if p.poll() is None), sorted(s.proc), s.proc["NAS"].pid != prima["NAS"].pid)
s.aggiorna([{"nome": "MacBook Pro di Pietro", "ip": "192.168.1.9", "porta": 445}])
rete[0] = ""
print(s.controlla_rete(10), s.controlla_rete(15), vol())
rete[0] = "192.168.1.10"
print(s.controlla_rete(16), s.persa_da)
rete[0] = ""
print(s.controlla_rete(20), s.controlla_rete(26), s.raffica, vol())
rete[0] = s.rete = "192.168.178.20"
def cerca_pc(attesa=2.0, ricordati=None):
    rete[0] = "10.20.30.40"   # il telefono passa ai dati mobili mentre si cerca
    return [{"nome": "fritz-box", "ip": "192.168.178.1", "porta": 445, "da": "bonjour"}]
m.cerca_pc = cerca_pc
s.cerca()
print(vol(), s.giri)
m.cerca_pc = lambda attesa=2.0, ricordati=None: [{"nome": "fritz-box", "ip": "192.168.178.1", "porta": 445, "da": "bonjour"}]
s.rete = rete[0]
s.cerca()
print(vol(), s.giri)
s.chiudi()
print("\n".join(registro))
EOF
)
  eq "cambio di rete (e nel registro)" "False 192.168.178.20
DESKTOP-I78AUDA 192.168.178.43, NAS 192.168.178.90 | False
True [6, 14, 32] NAS 192.168.178.90
[] ['NAS'] True
False False MacBook Pro di Pietro 192.168.1.9, NAS 192.168.178.90
False None
False True [] NAS 192.168.178.90
NAS 192.168.178.90 1
fritz-box 192.168.178.1, NAS 192.168.178.90 2
PC comparso: DESKTOP-I78AUDA (192.168.178.43)
PC comparso: NAS (192.168.178.90)
rete cambiata: 192.168.178.20 → 192.168.1.10
PC sparito: DESKTOP-I78AUDA (192.168.178.43)
PC comparso: MacBook Pro di Pietro (192.168.1.9)
rete cambiata: 192.168.1.10 → nessuna
PC sparito: MacBook Pro di Pietro (192.168.1.9)
rete cambiata durante la ricerca: risultato scartato
PC comparso: fritz-box (192.168.178.1)" "$out"
}

test_rete_battito() { # 9 novembre, Pietro: "per i dispositivi di rete ci vuole un controllo costante". Ogni BATTITO si bussa
  # alla porta dei PC: chi risponde resta anche se la ricerca non l'ha visto; chi non risponde da SCADE secondi sparisce
  # (prima dopo 10 minuti); un battito mancato da solo non basta; quelli aggiunti a mano non si toccano
  command -v python3 >/dev/null || return 0
  local out
  out=$(TMPDIR="$WORK/rete_tmp_batt" TOD_TERMUX_HOME="$WORK/rete_home_batt" TOD_SMB_BIN=/nessuno \
    _stampa_py "$ROOT/share/rete/tod-rete.py" <<'EOF'
import os
os.makedirs(m.RUN, exist_ok=True)
m.log = lambda *a: None
aperte = {"192.168.178.43"}
bussate = []
def porta_aperta(ip, porta, attesa=1.0):
    bussate.append(ip)
    return ip in aperte
m.porta_aperta = porta_aperta
vol = lambda: open(m.VOLUMI).read().strip().replace("\t", " ").replace("\n", ", ")
s = m.Servizio()
m.principale(["x", "aggiungi", "192.168.178.90", "NAS"])
s.aggiorna([{"nome": "DESKTOP-I78AUDA", "ip": "192.168.178.43", "porta": 445},
            {"nome": "fritz-box", "ip": "192.168.178.1", "porta": 445}])
for n in s.pc:
    s.pc[n]["visto"] -= m.BATTITO
s.battito()
print(vol(), sorted(bussate))
for n in s.pc:
    s.pc[n]["visto"] -= m.SCADE
s.battito()
print(vol(), m.BATTITO * 3 >= m.SCADE > m.BATTITO * 2)
s.chiudi()
EOF
)
  eq "battito" "DESKTOP-I78AUDA 192.168.178.43, fritz-box 192.168.178.1, NAS 192.168.178.90 ['192.168.178.1', '192.168.178.43']
DESKTOP-I78AUDA 192.168.178.43, NAS 192.168.178.90 True" "$out"
}

test_rete_aggiorna() { # "Aggiorna" (Esplora file, Impostazioni, tod rete aggiorna): "CERCA" al servizio → una ricerca subito,
  # la risposta quando è finita; anche dal socket vero e dalla riga di comando
  command -v python3 >/dev/null || return 0
  local out
  out=$(TMPDIR="$WORK/rete_tmp_agg" TOD_TERMUX_HOME="$WORK/rete_home_agg" TOD_SMB_BIN=/nessuno LANGUAGE=it \
    _stampa_py "$ROOT/share/rete/tod-rete.py" 2>&1 <<'EOF'
import os, sys, threading, time
os.makedirs(m.RUN, exist_ok=True)
m.log = lambda *a: None
m.rete_attuale = lambda: "192.168.1.10"
m.porta_aperta = lambda ip, porta, attesa=1.0: True
cercate = []
def cerca_pc(attesa=2.0, ricordati=None):
    cercate.append(1)
    return [{"nome": "MacBook Pro di Pietro", "ip": "192.168.1.9", "porta": 445, "da": "bonjour"}][:len(cercate) - 1]
m.cerca_pc = cerca_pc
m.OGNI = m.BATTITO = 3600
s = m.Servizio()
threading.Thread(target=s.giro_ricerca, daemon=True).start()
threading.Thread(target=s.giro_porta, args=(None,), daemon=True).start()
fine = time.time() + 10
while (s.giri < 1 or not os.path.exists(m.SOCK)) and time.time() < fine:
    time.sleep(0.05)
print(len(cercate), repr(s.risposta("CERCA\n", None)), len(cercate))
print(repr(m.al_servizio("CERCA\n")), len(cercate))
m.principale(["x", "aggiorna"])
s.fine = True
s.subito.set()
s.chiudi()
sys.stdout.flush()
print(m.principale(["x", "aggiorna"]), file=sys.stderr)
EOF
)
  eq "Aggiorna" "1 'OK\t1\n' 2
'OK\t1\n' 3
rete: acceso
  MacBook Pro di Pietro (192.168.1.9)
il servizio della rete è spento (tod rete on)
1" "$out"
}

test_rete_ricarica() { # 9 novembre, Pietro: "un pulsante aggiorna". In Esplora file il pulsante Ricarica (come Windows, accanto
  # all'indirizzo) che sulla Rete fa cercare subito i PC (tod-gettext.c → "CERCA"); nelle Impostazioni "Aggiorna"; tod rete
  # aggiorna. Su un PC che non risponde tod-smb dà errore dopo 10 s, non 20
  local g
  g=$(sed -n '/^void \*g_file_enumerate_children(/,/^}/p' "$ROOT/share/tod-gettext.c")
  has "Esplora file: CERCA quando elenca la Rete" 'if (p && !strcmp(p, TOD_RETE_RADICE)) tod_rete_cerca();' "$g"
  has "solo in Esplora file" 'tod_file_manager()' "$g"
  has "il messaggio per il servizio" 'send(s, "CERCA\n", 6, MSG_NOSIGNAL)' "$(sed -n '/^static void tod_rete_cerca(/,/^}/p' "$ROOT/share/tod-gettext.c")"
  has "nuove installazioni: Ricarica prima dell'indirizzo" "open-parent:1,reload:1,location-bar:1" "$FILES_TOOLBAR"
  has "Impostazioni: Aggiorna" 'self.rete_aggiorna(cerca)' "$(sed -n '/def rete_pc/,/def rete_cmd/p' "$ROOT/share/tod-settings.py")"
  has "Impostazioni: tod-rete.py aggiorna" '[sys.executable, RETE, "aggiorna"]' "$(sed -n '/def rete_aggiorna/,/def rete_cmd/p' "$ROOT/share/tod-settings.py")"
  has "tod rete aggiorna" "cerca | aggiorna | elenco" "$(sed -n '/^rete_cli()/,/^}/p' "$ROOT/lib/rete.sh")"
  has "tod-smb: 10 s" "smbc_setTimeout(ctx, 10000);" "$(cat "$ROOT/share/rete/tod-smb.c")"
  # le installazioni di prima: Ricarica acceso una volta sola, prima della barra dell'indirizzo
  local x="$WORK/rete_xfconf_ric"
  mkdir -p "$x"
  # shellcheck disable=SC2016 # $5 e $* sono dello script finto
  printf '#!/bin/sh\nif [ "$5" = "" ]; then echo "back:1,open-parent:1,location-bar:1,search:1,reload:0,zoom-in:0"; else echo "$*" >>"%s/set"; fi\n' "$x" >"$x/xfconf-query"
  chmod +x "$x/xfconf-query"
  PATH="$x:$PATH" rete_mostra_ricarica
  PATH="$x:$PATH" rete_mostra_ricarica
  eq "Ricarica accanto all'indirizzo, una volta" "-c thunar -p /last-toolbar-items -n -t string -s back:1,open-parent:1,reload:1,location-bar:1,search:1,zoom-in:0" "$(cat "$x/set")"
  eq "segnato" "1" "$(conf_get RETE_RICARICA 0)"
  command -v python3 >/dev/null || return 0
  python3 -m py_compile "$ROOT/share/rete/tod-rete.py" "$ROOT/share/tod-settings.py"
}

test_rete_esplora() { # 8 novembre, Pietro: "su Esplora rete non c'è niente, hai messo tutto su media/rete". La Rete è la voce
  # "Esplora la rete" di Esplora file (tod-gettext.c la porta su /media/rete), visibile; niente voce "Rete" doppia
  has "Esplora la rete apre /media/rete" 'per_percorso("/media/rete")' "$(sed -n '/^void \*g_file_new_for_uri(/,/^}/p' "$ROOT/share/tod-gettext.c")"
  local d f
  d=$(usb_data)
  mkdir -p "$d" "${TOD_TERMUX_HOME:-$HOME}/.local/share/tod-rete"
  printf '#!/bin/sh\n' >"$(rete_bin)"; chmod +x "$(rete_bin)"
  printf 'x/media/rete\0y' >"$d/tod-fs.so"
  files_user_home() { echo "$HOME/utente"; }
  files_user_folders() { :; }
  files_sdcards() { :; }
  f="$(files_termux_home)/.config/gtk-3.0/bookmarks"
  mkdir -p "${f%/*}"
  printf 'file:///media/rete Rete\nfile:///altro Altro\n' >"$f"
  files_bookmarks
  eq "la vecchia voce Rete via, le altre restano" "file:///altro Altro" "$(cat "$f")"
  local x="$WORK/sgn_xfconf"
  mkdir -p "$x"
  printf '#!/bin/sh\nif [ "$5" = "" ]; then printf "Value is an array with 2 items:\\n\\ncomputer:///\\nnetwork:///\\n"; else echo "$*" >>"%s/set"; fi\n' "$x" >"$x/xfconf-query"
  chmod +x "$x/xfconf-query"
  PATH="$x:$PATH" rete_mostra_esplora
  eq "network:/// non più nascosta, computer:/// sì" "-c thunar -p /hidden-bookmarks -n -a -t string -s computer:///" "$(cat "$x/set")"
}

test_usb_non_dischi() { # un errore nel chiedere al dispositivo non deve farlo ignorare per sempre
  local bin="$HOME/binnd" run="$HOME/runnd" out
  mkdir -p "$bin" "$run"
  local f; f=$(sed -n '/^is_storage() {/,/^}/p' "$ROOT/share/usb/tod-usb")
  printf '#!/bin/sh\necho "Permission denied"\n' >"$bin/termux-usb"; chmod +x "$bin/termux-usb"
  PATH="$bin:$PATH" RUN="$run" HERE=/x bash -c "$f"$'\n''is_storage /dev/bus/usb/002/004' || true
  eq "errore: nessun segno" "" "$(cat "$run/nondischi" 2>/dev/null)"
  printf '#!/bin/sh\necho %s\n' "'{\"classes\": [3], \"storage\": false}'" >"$bin/termux-usb"
  PATH="$bin:$PATH" RUN="$run" HERE=/x bash -c "$f"$'\n''is_storage /dev/bus/usb/001/006' || true
  eq "tastiera: segnata" "/dev/bus/usb/001/006" "$(cat "$run/nondischi" 2>/dev/null)"
  printf '#!/bin/sh\necho %s\n' "'{\"classes\": [8], \"storage\": true}'" >"$bin/termux-usb"
  out=$(PATH="$bin:$PATH" RUN="$run" HERE=/x bash -c "$f"$'\n''is_storage /dev/bus/usb/002/004 && echo disco')
  eq "lettore di schede: disco" "disco" "$out"
}

test_updates_center() { # Aggiornamenti: elenco da apt, app riconosciute, mai i pacchetti protetti
  command -v python3 >/dev/null || return 0
  local info="$PREFIX/var/lib/dpkg/info" apps="$PREFIX/share/applications" out
  mkdir -p "$info" "$apps"
  printf '%s\n' "$apps/gimp.desktop" >"$info/gimp.list"
  printf '[Desktop Entry]\nName=GNU Image Manipulation Program\nName[it]=GIMP\nIcon=gimp\nExec=gimp-3.2 %%U\n' >"$apps/gimp.desktop"
  out=$(PREFIX="$PREFIX" HOME="$HOME" TOD_TERMUX_HOME="$HOME" LANGUAGE=it python3 - "$ROOT/share/tod-updates.py" <<'EOF'
import importlib.util as u, sys, json
s = u.spec_from_file_location("up", sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)
lst = """Listing...
gimp/x11 3.2.6-1 aarch64 [upgradable from: 3.2.6]
mesa/x11 26.1 aarch64 [upgradable from: 26.0]
termux-x11-nightly/x11 1.03 all [aggiornabile da: 1.02]
curl/stable 8.23.0 aarch64 [aggiornabile da: 8.22.0]
"""
def fake(cmd, timeout=900):
    if cmd[:2] == ["apt-mark", "showhold"]: return 0, "mesa\n"
    if cmd[:2] == ["apt", "list"]: return 0, lst
    return 0, ""
m.run = fake
m.rootfs = lambda: None
st = m.check(refresh=False)
print(" ".join(x["name"] for x in st["termux"]))
print(st["termux"][0].get("app"), st["termux"][0]["from"], st["termux"][0]["to"])
print(m.summary(st))
# aggiornare una sola app: solo quel pacchetto, nel sistema giusto
calls = []
class P:
    def __init__(self, cmd, **k): calls.append(cmd); self.stdout = iter(["Setting up gimp (3.2.6-1) ...\n"])
    def wait(self): return 0
m.subprocess.Popen = P
m.install([st["termux"][0]], lambda *a: None)
m.install([{"name": "app-electron", "where": "debian"}, {"name": "curl", "where": "termux"}], lambda *a: None)
print(" | ".join(" ".join(c[c.index("--only-upgrade") + 1:]) + (" (debian)" if "proot-distro" in c else "") for c in calls))
EOF
)
  eq "solo ciò che si può aggiornare (niente driver bloccati né Termux:X11)" "gimp curl" "$(sed -n 1p <<<"$out")"
  eq "l'app con il suo nome" "GIMP 3.2.6 3.2.6-1" "$(sed -n 2p <<<"$out")"
  eq "riassunto" "GIMP e un altro aggiornamento" "$(sed -n 3p <<<"$out")"
  eq "una alla volta, o tutte: ognuna dove sta" "gimp | curl | app-electron (debian)" "$(sed -n 4p <<<"$out")"
  has "voce in Impostazioni" "X-XFCE-SettingsDialog" "$(desktop_install_entries >/dev/null 2>&1; cat "$HOME/.local/share/applications/tod-updates.desktop")"
}

test_deb_installer() { # installer grafico dei .deb: legge nome, icona e versione; avvisa solo se serve
  command -v dpkg-deb >/dev/null && command -v python3 >/dev/null || { echo "  (dpkg-deb assente: salto)"; return 0; }
  local d="$WORK/deb_$$" out
  mkdir -p "$d/pkg/DEBIAN" "$d/pkg/usr/share/applications" "$d/pkg/usr/share/icons/hicolor/48x48/apps" \
    "$d/pkg/usr/share/icons/hicolor/256x256/apps"
  printf 'Package: provaapp\nVersion: 2.0\nArchitecture: arm64\nMaintainer: Io <io@x>\nInstalled-Size: 2048\nDescription: Una prova\n Descrizione lunga.\n' >"$d/pkg/DEBIAN/control"
  printf '[Desktop Entry]\nName=Prova App\nName[it]=App di prova\nIcon=provaapp\nExec=provaapp\n' >"$d/pkg/usr/share/applications/org.prova.App.desktop"
  printf 'piccola' >"$d/pkg/usr/share/icons/hicolor/48x48/apps/provaapp.png"
  printf 'grande' >"$d/pkg/usr/share/icons/hicolor/256x256/apps/provaapp.png"
  dpkg-deb --build --root-owner-group "$d/pkg" "$d/provaapp.deb" >/dev/null 2>&1 || dpkg-deb --build "$d/pkg" "$d/provaapp.deb" >/dev/null
  out=$(LANGUAGE=it python3 - "$ROOT/share/tod-installer.py" "$d/provaapp.deb" <<'EOF'
import sys, types, importlib.util as u, tempfile
gi = types.ModuleType("gi"); gi.require_version = lambda *a: None
repo = types.ModuleType("gi.repository")
class _W: pass
class _Gtk: Window = _W
for n in ("Gdk", "GdkPixbuf", "Gio", "GLib", "Pango"): setattr(repo, n, types.SimpleNamespace(set_prgname=lambda *a: None))
repo.Gtk = _Gtk
sys.modules["gi"] = gi; sys.modules["gi.repository"] = repo
s = u.spec_from_file_location("inst", sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)
deb = sys.argv[2]; tmp = tempfile.mkdtemp()
files = m.contents(deb)
a = m.app_info(deb, files, tmp)
print(a["name"], a["desktop"])
print(open(a["icon"]).read())
print(m.field(deb, "Version"), m.human(m.field(deb, "Installed-Size")))
EOF
)
  eq "nome dell'app in italiano" "App di prova org.prova.App.desktop" "$(sed -n 1p <<<"$out")"
  eq "l'icona più grande" "grande" "$(sed -n 2p <<<"$out")"
  eq "versione e spazio" "2.0 2,0 MB" "$(sed -n 3p <<<"$out")"
  # i controlli di tod: dove va, se c'è già, se è più vecchio
  pkg_installed() { return 1; }
  cx_installed() { return 0; }
  cx_pkg_installed() { [[ $1 == provaapp ]]; }
  _fake_rootfs
  printf 'Package: provaapp\nStatus: install ok installed\nVersion: 3.0\n\n' >>"$R/var/lib/dpkg/status"
  out=$(deb_check "$d/provaapp.deb")
  has "va in Debian" "target=container" "$out"
  has "installata una più nuova" "installed=3.0" "$out"
  has "questa è più vecchia" "cmp=older" "$out"
  cx_pkg_installed() { return 1; }
  app_owner() { [[ $1 == provaapp ]] && echo "provaapp-termux"; }
  has "la stessa app in un altro sistema" "owner=provaapp-termux" "$(deb_check "$d/provaapp.deb")"
}

test_theme_dark() { # Impostazioni → Aspetto: chiaro/scuro subito, anche barra e Start
  _fake_xfconf
  conf_set TEMA chiaro
  eq "chiaro" Fluent-Light "$(look_w11_gtk)"
  DISPLAY=:9 look_theme_cli scuro
  eq "scelta salvata" scuro "$(conf_get TEMA)"
  eq "tema GTK scuro" Fluent-Dark "$(xfconf-query -c xsettings -p /Net/ThemeName)"
  eq "xfce4-panel mai chiamato" 0 "$(cat "$HOME/panel_calls" 2>/dev/null | wc -l)"
  has "la barra segue il tema da sola" "notify::gtk-theme-name" "$(cat "$ROOT/share/tod-barra.py")"
  has "la barra ha la sua versione scura" "True: dict(FONDO=" "$(cat "$ROOT/share/tod-barra.py")"
  DISPLAY=:9 look_theme_cli chiaro
  eq "di nuovo chiaro" Fluent-Light "$(xfconf-query -c xsettings -p /Net/ThemeName)"
  has "lo Start ha la sua versione scura" "CSS_DARK" "$(cat "$ROOT/share/tod-start.py")"
  has "Impostazioni nello Start" "tod-settings.desktop" "$(cat "$ROOT/share/tod-start.py")"
}

test_tema_coerente_all_avvio() { # 9 novembre: DeX-OS-Dark rimasto nei file di XFCE con TEMA=chiaro, a ogni avvio
  _fake_xfconf
  local th="$HOME/.local/share/themes"
  mkdir -p "$th/DeX-OS-0067c0/gtk-3.0" "$th/DeX-OS-WM-Dark-1.125/xfwm4"
  : >"$th/DeX-OS-0067c0/gtk-3.0/gtk.css"; : >"$th/DeX-OS-WM-Dark-1.125/xfwm4/themerc"
  conf_set DEXOS_NAMES "DeX-OS-0067c0 DeX-OS-Dark-0067c0 DeX-OS-WM-1.125 DeX-OS-WM-Dark-1.125"
  look_sfondo_apply() { :; }
  conf_set TEMA chiaro
  xfconf-query -c xsettings -p /Net/ThemeName -n -t string -s DeX-OS-Dark-0067c0
  look_tema_coerente
  eq "rimesso il chiaro scelto nelle Impostazioni" DeX-OS-0067c0 "$(xfconf-query -c xsettings -p /Net/ThemeName)"
  eq "anche la cornice" DeX-OS-WM-1.125 "$(xfconf-query -c xfwm4 -p /general/theme)"
  has "scritto nel registro" "aspetto: tema DeX-OS-Dark-0067c0 con TEMA=chiaro: rimesso DeX-OS-0067c0" "$(cat "$TOD_LOG")"
  # un tema che non è DeX OS (scelto a mano) non si tocca
  xfconf-query -c xsettings -p /Net/ThemeName -n -t string -s Adwaita-dark
  look_tema_coerente
  eq "tema non DeX OS lasciato com'è" Adwaita-dark "$(xfconf-query -c xsettings -p /Net/ThemeName)"
  has "a ogni avvio, prima del controllo di versione" "look_tema_coerente || true" "$(sed -n '/^look_session_apply()/,/^}/p' "$ROOT/lib/look.sh")"
}

test_old_settings_redirect() { # tasto destro "Impostazioni della scrivania" & co. → nuove Impostazioni
  local d="$WORK/oldset_$$"
  mkdir -p "$d"
  printf '#!/bin/sh\necho "$*"\n' >"$d/python3"
  chmod +x "$d/python3"
  eq "sfondo → Aspetto" aspetto "$(PATH="$d:$PATH" sh "$ROOT/share/bin/xfdesktop-settings" | awk '{print $NF}')"
  eq "aspetto → Aspetto" aspetto "$(PATH="$d:$PATH" sh "$ROOT/share/bin/xfce4-appearance-settings" | awk '{print $NF}')"
  eq "schermo → Schermo" schermo "$(PATH="$d:$PATH" sh "$ROOT/share/bin/xfce4-display-settings" | awk '{print $NF}')"
  eq "nuovo schermo collegato: niente finestra" "" "$(PATH="$d:$PATH" sh "$ROOT/share/bin/xfce4-display-settings" --minimal)"
  has "gestore → Impostazioni" "tod-settings.py" "$(PATH="$d:$PATH" sh "$ROOT/share/bin/xfce4-settings-manager")"
  eq "campanello → Notifiche" notifiche "$(PATH="$d:$PATH" sh "$ROOT/share/bin/xfce4-notifyd-config" | awk '{print $NF}')"
  has "il pannello vero per posizione e durata" 'bin/xfce4-notifyd-config' "$(cat "$ROOT/share/tod-settings.py")"
  has "da Avanzate si arriva ancora a quelle di XFCE (senza giro)" 'bin/xfce4-settings-manager' "$(cat "$ROOT/share/tod-settings.py")"
}

test_dexos_theme() { # tema DeX OS: il blu di Fluent diventa l'accento scelto, anche nei PNG
  command -v python3 >/dev/null || return 0
  local out
  out=$(python3 - "$ROOT/share/dexos/dexos-theme.py" "$WORK" <<'EOF'
import importlib.util as u, sys, os
s = u.spec_from_file_location("dx", sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)
rc = m.Recolor("#8a5cf5")
print(m.rgb_hex(*rc.rgb(0x1A, 0x73, 0xE8)))            # il blu di Fluent → viola
print(m.rgb_hex(*rc.rgb(0xF2, 0xF2, 0xF2)), m.rgb_hex(*rc.rgb(0xD9, 0x30, 0x25)))   # grigio e rosso intatti
print(rc.css("a { color: #1A73E8; background: rgba(26, 115, 232, 0.3); border: #ffffff; }"))
p = os.path.join(sys.argv[2], "t.png")
m.png_write(p, 2, 1, 4, [bytearray([0x1A, 0x73, 0xE8, 255, 10, 10, 10, 128])])
m.recolor_png(p, p + ".2", rc)
w, h, bpp, rows = m.png_read(p + ".2")
print(w, h, m.rgb_hex(*rows[0][0:3]), rows[0][3], m.rgb_hex(*rows[0][4:7]), rows[0][7])
EOF
)
  eq "accento" "#895bf5" "$(sed -n 1p <<<"$out")"
  eq "gli altri colori no" "#f2f2f2 #d93025" "$(sed -n 2p <<<"$out")"
  eq "anche rgba()" "a { color: #895bf5; background: rgba(137, 91, 245, 0.3); border: #ffffff; }" "$(sed -n 3p <<<"$out")"
  eq "PNG ricolorato, trasparenza intatta" "2 1 #895bf5 255 #0a0a0a 128" "$(sed -n 4p <<<"$out")"
  # desktop: tema DeX OS quando c'è, chiaro/scuro, accento per nome
  local th="$HOME/.local/share/themes"
  mkdir -p "$th/DeX-OS-0067c0/gtk-3.0" "$th/DeX-OS-WM-Dark-1.125/xfwm4"
  : >"$th/DeX-OS-0067c0/gtk-3.0/gtk.css"; : >"$th/DeX-OS-WM-Dark-1.125/xfwm4/themerc"
  conf_set DEXOS_NAMES "DeX-OS-0067c0 DeX-OS-Dark-0067c0 DeX-OS-WM-1.125 DeX-OS-WM-Dark-1.125"
  conf_set TEMA scuro
  eq "tema scuro DeX OS, col nome dell'accento" DeX-OS-Dark-0067c0 "$(look_w11_gtk)"
  eq "cornice DeX OS della scala" DeX-OS-WM-Dark-1.125 "$(look_w11_wm)"
  # un nome per accento e per scala: GTK ricorda i temi per nome (sennò resta il colore di prima)
  local nm
  nm=$(python3 -c "
import importlib.util as u
s = u.spec_from_file_location('dx', '$ROOT/share/dexos/dexos-theme.py'); m = u.module_from_spec(s); s.loader.exec_module(m)
print(' '.join(m.names('#8A5CF5', 1.125).values()), '|', ' '.join(m.names('#0067c0', 1.0).values()))")
  eq "nomi" "DeX-OS-8a5cf5-v15 DeX-OS-Dark-8a5cf5-v15 DeX-OS-WM-1.125-v15 DeX-OS-WM-Dark-1.125-v15 | DeX-OS-0067c0-v15 DeX-OS-Dark-0067c0-v15 DeX-OS-WM-1-v15 DeX-OS-WM-Dark-1-v15" "$nm"
  # menu, popup e suggerimenti nello stile di DeX OS: fondi pieni (niente vetro), voce attiva nell'accento
  local menu
  menu=$(python3 -c "
import importlib.util as u
s = u.spec_from_file_location('dx', '$ROOT/share/dexos/dexos-theme.py'); m = u.module_from_spec(s); s.loader.exec_module(m)
print(m.vetro_css('light', '#8a5cf5'))")
  has "voce del menu nell'accento" "background-color: #8a5cf5; color: #ffffff;" "$menu"
  eq "menu con fondo pieno" 0 "$(grep -A3 '^menu, .menu, .context-menu {' <<<"$menu" | grep -c 'rgba(2')"
  # casella spuntata vuota (7 novembre): la spunta di Fluent è un ritaglio nell'icona colorata con color, sopra un
  # quadratino bianco; con color bianco (e un fondo nell'accento) restava un quadrato bianco col bordo blu
  has "casella spuntata nell'accento" "check:checked, radio:checked, check:indeterminate, radio:indeterminate { color: #8a5cf5; }" "$menu"
  eq "mai bianca né con un fondo" 0 "$(sed -n '/^check:checked/,/}/{p;/}/q}' <<<"$menu" | grep -c 'background\|#ffffff')"
  has "nel menu, sulla voce nell'accento, bianca anche spuntata" \
    "menu menuitem:hover > radio:checked, menu menuitem:hover > radio:indeterminate { color: #ffffff; }" "$menu"
  eq "niente più passaggio da Adwaita/Default (il lampo di XFCE puro)" 0 "$(declare -f look_theme_reload | grep -c 'Adwaita\|Default')"
  conf_set TEMA chiaro
  DISPLAY= look_accent_cli viola
  eq "accento per nome" "#8a5cf5" "$(conf_get ACCENTO)"
  has "lo Start usa l'accento" 'replace(b"#005fb8", a)' "$(cat "$ROOT/share/tod-start.py")"
}

test_dexos_icons() { # icone DeX OS: tutte nostre (niente Fluent), complete, simboliche solo pieni, chiare e scure
  command -v python3 >/dev/null || return 0
  local out="$WORK/icons_$$" name t d
  name=$(HOME="$WORK/icons_home_$$" python3 "$ROOT/share/dexos/dexos-icons.py" build --accent "#8a5cf5" --out "$out")
  eq "nome per accento (e versione del disegno)" DeX-OS-Icons-8a5cf5-v10 "$name"
  t="$out/$name" d="$out/DeX-OS-Icons-Dark-8a5cf5-v10"
  # GIMP (tema Legacy → hicolor) si chiudeva senza image-missing da nessuna parte (26 ottobre)
  [[ -s $out/hicolor/scalable/status/image-missing.svg && -s $t/scalable/status/image-missing.svg ]]
  has "GIMP (Legacy → Color) arriva alle icone DeX OS" "Inherits=$name,hicolor" "$(cat "$out/Color/index.theme")"
  has "niente Fluent né Adwaita sotto" "Inherits=hicolor" "$(cat "$t/index.theme")"
  has "il tema scuro eredita il chiaro" "Inherits=$name,hicolor" "$(cat "$d/index.theme")"
  local f n mancano=0
  for f in folder user-home folder-download user-trash-full start-here application-pdf \
    application-vnd.debian.binary-package image-png text-plain inode-directory; do
    [[ -n $(find "$t/scalable" -name "$f.svg" -print -quit) ]] || { echo "manca $f"; return 1; }
  done
  # ogni nome dell'elenco ha la sua icona, a colori o simbolo, e la versione simbolica
  while IFS=$'\t' read -r _ n; do
    [[ -e $(find "$t" -path "*/$n.svg" -print -quit) || -e $(find "$t" -path "*/apps/$n.svg" -print -quit) ]] || mancano=$((mancano + 1))
  done < <(grep -v '^#' "$ROOT/share/dexos/icone/nomi.txt" | sed -n '1~97p')
  eq "nessun quadratino (campione dei nomi)" 0 "$mancano"
  eq "le simboliche solo con riempimenti (GTK non ricolora i tratti)" 0 "$(grep -rl 'stroke' "$t"/symbolic | wc -l)"
  [[ -e $t/symbolic/status/battery-level-50-symbolic.svg || -e $t/symbolic/panel/battery-level-50-symbolic.svg ]] ||
    { echo "manca la batteria"; return 1; }
  has "pulsanti grigio scuro nel chiaro" "#3a4150" "$(cat "$t/scalable/actions/edit-copy.svg")"
  has "e chiari nello scuro" "#e4e7ee" "$(cat "$d/scalable/actions/edit-copy.svg")"
  eq "go-home è un pulsante, non una cartella" 1 "$(grep -c '#3a4150' "$t/scalable/actions/go-home.svg")"
  has "logo dello Start" "rect" "$(cat "$t/scalable/apps/tod-start.svg")"
  eq "rifarlo non costa nulla" "$name" "$(HOME="$WORK/icons_home_$$" python3 "$ROOT/share/dexos/dexos-icons.py" build --accent "#8a5cf5" --out "$out")"
  # le simboliche vengono dai glifi già convertiti: contorni.json allineato a glifi.py
  eq "contorni aggiornati" "" "$(cd "$ROOT/share/dexos/icone" && python3 -c "
import json, glifi
c = json.load(open('contorni.json'))
print(' '.join(sorted(set(glifi.GLIFI) ^ set(c))))")"
}

test_session_log_cap() { # session.log con un tetto, anche mentre un programma ci scrive in coda
  local f="$TOD_STATE_DIR/session.log"
  mkdir -p "$TOD_STATE_DIR"
  python3 -c 'import sys; f = open(sys.argv[1], "w"); [f.write(f"riga {i}\n") for i in range(200000)]' "$f"
  exec 9>>"$f"
  SESSION_LOG_MAX=100000 session_log_cap
  echo "dopo il taglio" >&9
  exec 9>&-
  local n
  n=$(stat -c %s "$f")
  if ((n > 100000)); then echo "  ✘ registro ancora di $n byte"; return 1; fi
  has "segno del taglio" "registro accorciato" "$(sed -n 1p "$f")"
  has "le righe più recenti restano" "riga 199999" "$(tail -n 2 "$f")"
  eq "chi scrive in coda continua dopo il taglio" "dopo il taglio" "$(tail -n 1 "$f")"
  eq "niente buchi (file sparso)" 0 "$(tr -d '\n -~àèéìòù' <"$f" | wc -c)"
  SESSION_LOG_MAX=100000000 session_log_cap
  has "sotto il tetto non si tocca" "riga 199999" "$(tail -n 2 "$f")"
  head -c 300000 /dev/zero | tr '\0' 'x' >"$TOD_STATE_DIR/start.log"
  SESSION_LOG_MAX=100000 session_log_cap
  n=$(stat -c %s "$TOD_STATE_DIR/start.log")
  if ((n > 100000)); then echo "  ✘ start.log ancora di $n byte"; return 1; fi
}

test_barra_icona_finestra() { # 1° novembre (S20+): app senza voce di menu con l'icona enorme nella barra
  has "ripiego: l'icona della finestra alla misura della barra" "get_icon(px(self.dim()[1]))" \
    "$(sed -n '/def aggiorna_pulsante/,/ev.img.set_pixel_size/p' "$ROOT/share/tod-barra.py")"
  python3 -c "import gi; gi.require_version('GdkPixbuf', '2.0')" 2>/dev/null || return 0
  local out
  out=$(python3 - "$ROOT/share/tod-barra.py" <<'PYEOF'
import re, sys
import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib
src = open(sys.argv[1]).read()
exec(re.search(r"^    def get_icon\(.*?(?=^    def )", src, re.S | re.M).group(0).replace("\n    ", "\n")[4:])
class P:   # _NET_WM_ICON finto: un'icona da 48x48, come quella che PhotoCraft mette sulla finestra
    def interi(self, xid, nome): return [48, 48] + [0xff3366cc] * (48 * 48)
class F: p, xid = P(), 1
pb = get_icon(F(), 26)
print(pb.get_width(), pb.get_height())
PYEOF
)
  eq "l'icona esce alla misura chiesta" "26 26" "$out"
}

test_pannelli() { # pannelli della barra: il modulo si carica, le icone della barra aprono il pannello giusto
  command -v python3 >/dev/null || return 0
  python3 -m py_compile "$ROOT/share/tod-pannelli.py" || { echo "tod-pannelli.py non compila"; return 1; }
  local out
  # le icone a destra le calcola la barra da sé (senza avviare programmi): funzioni provate senza GTK
  out=$(python3 - "$ROOT/share/tod-barra.py" "$ROOT/share/tod-phone-status.py" <<'PYEOF2'
import os, re, sys, time
src = open(sys.argv[1]).read()
ITALIAN = True
QUI = "/q"
def T(it, en): return it
exec(re.search(r"^PANNELLO = .*?(?=^def riga_volume)", src, re.S | re.M).group(0))
for nome in ("riga_volume", "riga_notifiche", "riga_orologio", "leggi_volume"):
    exec(re.search(rf"^def {nome}\(.*?(?=^\S)", src, re.S | re.M).group(0))
print("vol", riga_volume(*leggi_volume("Volume: front-left: 30000 /  46% / -20 dB\nMute: no")))
print("muto", riga_volume(*leggi_volume("Volume: front-left: 30000 /  46% / -20 dB\nMuto: sì")))
print("senza", riga_volume(*leggi_volume("")))
print("notif", riga_notifiche(False, True), riga_notifiche(True, True))
print("ora", riga_orologio(time.strptime("2026-10-23 15:07", "%Y-%m-%d %H:%M")))
ns = {"__file__": sys.argv[2], "__name__": "tod_phone_status"}
exec(compile(open(sys.argv[2]).read(), sys.argv[2], "exec"), ns)
print("batt", ns["battery"]({"battery": {"percentage": 52, "status": "DISCHARGING", "plugged": "UNPLUGGED"}}))
print("rete", ns["network"]({"wifi": {"supplicant_state": "COMPLETED", "rssi": -50, "ssid": "casa"}}))
PYEOF2
)
  has "volume: icona del livello" "audio-volume-medium-symbolic" "$out"
  has "volume: clic → pannello" "/q/tod-pannello volume</iconclick>" "$out"
  has "volume muto (pactl in italiano)" "muto <icon>audio-volume-muted-symbolic" "$out"
  has "audio assente" "Audio non disponibile" "$out"
  has "notifiche nuove, clic → pannello" "notification-new-symbolic</icon><tool>Notifiche nuove</tool><iconclick>/q/tod-pannello notifiche" "$out"
  has "non disturbare vince" "notification-disabled-symbolic" "$out"
  has "orologio: ora e data, giorno in italiano" "<span>15:07</span>" "$out"
  has "orologio: suggerimento" "<tool>venerdì 23 ottobre 2026</tool>" "$out"
  has "il clic sull'orologio apre il calendario" "/q/tod-pannello calendario</txtclick>" "$out"
  has "batteria dalla funzione (niente Python nuovo)" "batt <icon>battery-level-50-symbolic</icon>" "$out"
  has "batteria: clic → pannello" "tod-pannello batteria</iconclick>" "$out"
  has "rete dalla funzione" "rete <icon>network-wireless-signal-excellent-symbolic</icon><tool>Wi-Fi: casa" "$out"
  eq "la barra non avvia più tod-tray né tod-phone-status.py per le icone" 0 "$(grep -c '"tod-tray"\|"tod-phone-status.py"), "' "$ROOT/share/tod-barra.py")"
  has "colori: solo l'accento di sistema, nessun blu fisso" "ACCENTO" "$(sed -n '/^CSS = /,/^"""/p' "$ROOT/share/tod-pannelli.py")"
  eq "nessun colore d'accento scritto a mano" 0 "$(sed -n '/^CSS = /,/^CSS_SCURO/p' "$ROOT/share/tod-pannelli.py" | grep -ci '#0067c0\|#005fb8\|#0a84ff')"
}

test_trust_launchers() { # collegamenti fidati: solo le copie di app installate
  local d="$WORK/trust_$$" calls="$WORK/trust_calls_$$"
  mkdir -p "$d/bin" "$HOME/Desktop" "$HOME/.local/share/applications"
  printf '#!/bin/sh\n[ "$1" = set ] && echo "$*" >>"%s"\nexit 0\n' "$calls" >"$d/bin/gio"; chmod +x "$d/bin/gio"
  printf '[Desktop Entry]\nName=Craft\n' | tee "$HOME/Desktop/tod-cx-craft.desktop" >"$HOME/.local/share/applications/tod-cx-craft.desktop"
  printf '[Desktop Entry]\nName=Sconosciuto\nExec=rm -rf ~\n' >"$HOME/Desktop/strano.desktop"
  PATH="$d/bin:$PATH" files_trust_launchers
  has "app installata: fidata" "tod-cx-craft.desktop metadata::xfce-exe-checksum" "$(cat "$calls")"
  eq "file sconosciuto: mai" 0 "$(grep -c strano "$calls")"
}

test_cestino_telefono() { # file del telefono: nel Cestino (non eliminati per sempre), spostati (non copiati)
  local src="$ROOT/share/tod-gettext.c" cc
  has "cestino: g_file_trash intercettata" "int g_file_trash(void *file" "$(cat "$src")"
  has "cartelle tra dischi: ripiego solo su WOULD_RECURSE (25)" "matches(err, quark(), 25)" "$(cat "$src")"
  has "mai sopra qualcosa che c'è già" 'lstat(a, &st) != 0' "$(cat "$src")"
  has "mv -T: mai dentro a una cartella che esiste" '"mv", "-f", "-T", "--"' "$(cat "$src")"
  has "si guarda la cartella che contiene, non il file (~/Telefono non è il telefono intero)" 'const char *barra = strrchr(path, '"'"'/'"'"');' "$(cat "$src")"
  has "stesso disco e cestinabili solo in Esplora file e scrivania" '!strcasecmp(n, "thunar") || !strcasecmp(n, "xfdesktop")' "$(cat "$src")"
  has "si può spegnere" "TOD_CESTINO_CLASSICO" "$(cat "$src")"
  has "/storage/emulated: ENOENT solo per una cartella che esiste" "e == ENOENT && (stat(name, &st) != 0" "$(cat "$src")"
  has "scrivania: niente Elimina nel tasto destro" '"<Actions>/XfdesktopFileIconManager/delete"' "$(cat "$src")"
  has "Sposta nel cestino chiede conferma" "misc-confirm-move-to-trash -n -t bool -s true" "$(cat "$ROOT/lib/files.sh")"
  has "la voce Elimina si salta quando xfdesktop la aggiunge da sé" '"tod-fuori-dal-menu")) return;' "$(cat "$src")"
  has "titolo di Thunar senza \"- Thunar\"" "FOLDER_NAME_WITHOUT_THUNAR_SUFFIX" "$(cat "$ROOT/lib/files.sh")"
  cc=$(command -v clang || command -v gcc) || { echo "  (nessun compilatore: salto la compilazione)"; return 0; }
  "$cc" -shared -fPIC -O2 -Wall -Wextra -Wno-unused-parameter -Werror -o "$WORK/tc_$$.so" "$src" -ldl
  eq "contiene g_file_trash" 1 "$(grep -ac g_file_trash "$WORK/tc_$$.so" | sed 's/^[1-9][0-9]*$/1/')"
  eq "contiene g_file_move" 1 "$(grep -ac g_file_move "$WORK/tc_$$.so" | sed 's/^[1-9][0-9]*$/1/')"
}

test_inline_rename() { # rinomina in linea: il modulo compila, intercetta gtk_dialog_run, solo in Thunar
  local cc
  cc=$(command -v clang || command -v gcc) || { echo "  (nessun compilatore: salto)"; return 0; }
  "$cc" -shared -fPIC -O2 -Wall -Werror -o "$WORK/tg_$$.so" "$ROOT/share/tod-gettext.c" -ldl
  eq "intercetta la finestra" 1 "$(grep -ac gtk_dialog_run "$WORK/tg_$$.so" | sed 's/^[1-9][0-9]*$/1/')"
  has "solo in Thunar (D-Bus lo avvia come \"Thunar\")" 'strcasecmp(prg, "thunar")' "$(cat "$ROOT/share/tod-gettext.c")"
  has "si può tornare al pannello classico" "TOD_RINOMINA_CLASSICA" "$(cat "$ROOT/share/tod-gettext.c")"
  eq "scrivania: intercetta la richiesta a Thunar" 1 "$(grep -ac g_dbus_proxy_call "$WORK/tg_$$.so" | sed 's/^[1-9][0-9]*$/1/')"
  has "scrivania: solo in xfdesktop e solo RenameFile" '!strcmp(method, "RenameFile") && !strcmp(prg, "xfdesktop")' "$(cat "$ROOT/share/tod-gettext.c")"
  has "senza genitore: si cerca la finestra di Thunar attiva" "tod_active_window(dialog)" "$(cat "$ROOT/share/tod-gettext.c")"
  has "scrivania: i lanciatori cambiano il nome mostrato e rinnovano il sigillo" "tod_desk_launcher_save" "$(cat "$ROOT/share/tod-gettext.c")"
  files_rename_css
  has "stile della casella" "window.tod-rename" "$(cat "$HOME/.config/gtk-3.0/gtk.css")"
  # 8 novembre, come macOS: sotto il campo niente etichetta vera, Nuova cartella senza finestra
  local src
  src=$(cat "$ROOT/share/tod-gettext.c")
  has "l'etichetta vera non si disegna mentre si scrive" "*slot = tod_render_hook;" "$src"
  has "solo se GtkCellRendererClass è quella di GTK 3" "cr.class_size != obj.class_size + 16 * sizeof(void *)" "$src"
  has "scrivania: il campo va a capo come l'etichetta" "3 /* GTK_WRAP_WORD_CHAR */" "$src"
  has "Nuova cartella: niente finestra" "if (tod_create_inline(dialog, &response)) return response;" "$src"
  has "Nuova cartella non viene scambiata per una rinomina" "if (tod_create_entry(dialog)) return 0;" "$src"
  has "creazione riconosciuta dall'icona del tipo (la rinomina ha quella del file)" "!= 6 /* GTK_IMAGE_GICON */" "$src"
  has "Thunar: poi la sua azione Rinomina" '"<Actions>/ThunarStandardView/rename"' "$src"
  has "campo di un elemento appena creato: la vista si risistema (prima 1x1, invisibile)" "f_gtk_widget_size_allocate(view, a);" "$src"
  has "menu come macOS" '"Nuova carte_lla"' "$src"
  has "si può tornare alla finestra di creazione" "TOD_NUOVO_CLASSICO" "$src"
}

test_settings_uninstall() { # Impostazioni → App: disinstallare ogni tipo di app, mai i pezzi del desktop
  local f="$ROOT/share/tod-settings.py"
  has "app del telefono: conferma di Android" "android.intent.action.DELETE" "$(cat "$f")"
  has "le altre: il disinstallatore" '"tod-disinstalla.py"), dove, nome_pkg, name' "$(cat "$f")"
  has "AppImage del container" 'dove, nome_pkg = "home"' "$(cat "$f")"
  eq "protetti Thunar, XFCE, Termux" "thunar xfce4-panel xfwm4 termux-x11-nightly" "$(python3 -c "
import re
src = open('$f').read()
rx = re.compile(re.search(r'PROTECTED = re.compile\(r\"(.*?)\"\)', src).group(1))
print(' '.join(p for p in ('thunar', 'gimp', 'xfce4-panel', 'xfwm4', 'termux-x11-nightly', 'cadcraft') if rx.match(p)))")"
}

test_disinstalla() { # disinstallazione profonda: componenti orfani solo per colpa dell'app, dati nel Cestino
  local l="$PREFIX/var/lib/dpkg/info" a="$PREFIX/share/applications" c="$HOME/.config" out
  mkdir -p "$l" "$a" "$c/GIMP/3.0" "$c/tod" "$HOME/.cache/gimp" "$HOME/.config/gtk-3.0" "$HOME/Documenti/gimp"
  printf '%s\n' "$a/gimp.desktop" "$PREFIX/bin/gimp-3.2" >"$l/gimp.list"
  printf '[Desktop Entry]\nName=GIMP\nExec=gimp-3.2 %%U\nIcon=gimp\nStartupWMClass=gimp-3.2\n' >"$a/gimp.desktop"
  printf 'Package: gimp\nStatus: install ok installed\nInstalled-Size: 1000\n\nPackage: libgimp\nInstalled-Size: 300\n\n' \
    >"$PREFIX/var/lib/dpkg/status"
  echo dati >"$c/GIMP/3.0/gimprc"
  # apt simulato: "vecchio" era orfano già prima, python-x è un pezzo del desktop
  apt-get() {
    case "$*" in
      "-s autoremove") echo "Remv vecchio [1]" ;;
      "-s purge --autoremove gimp") printf 'Purg gimp [1]\nPurg vecchio [1]\nPurg libgimp:aarch64 [1]\nPurg python-x [1]\n' ;;
      purge*) echo "apt-get $*" >>"$WORK/dis_$$" ;;
    esac
  }
  gio() { echo "cestino ${*: -1}" >>"$WORK/dis_$$"; }
  desktop_refresh_caches() { :; }
  out=$(app_piano termux gimp)
  has "il programma" $'programma\tgimp\t1000' "$out"
  has "componente orfano per colpa sua" $'componente\tlibgimp\t300' "$out"
  hasnt "già orfano prima: non si tocca" "vecchio" "$out"
  hasnt "pezzi del desktop mai" "python-x" "$out"
  has "impostazioni (nome senza maiuscole)" $'dati\t'"$c/GIMP" "$out"
  has "cache" "$HOME/.cache/gimp" "$out"
  hasnt "cartelle comuni: no" "gtk-3.0" "$out"
  hasnt "i documenti mai" "Documenti" "$out"
  has "voce del menu" $'voce\tgimp.desktop' "$out"
  eq "Thunar non si disinstalla" $'protetta\t1' "$(app_piano termux thunar)"
  printf 'thunar.desktop\ngimp.desktop\n' >"$c/tod/start-aggiunte"
  printf '[Default Applications]\nimage/png=gimp.desktop;ristretto.desktop;\nimage/x-xcf=gimp.desktop\n' >"$c/mimeapps.list"
  printf '{"fissate": ["thunar.desktop", "gimp.desktop"]}' >"$c/tod/barra.json"
  app_rimuovi termux gimp 1 >/dev/null
  out=$(cat "$WORK/dis_$$")
  has "purge del programma e dei suoi orfani, non di altro" "apt-get purge -y gimp libgimp" "$out"
  hasnt "solo quelli" "vecchio" "$out"
  has "impostazioni nel Cestino" "cestino $c/GIMP" "$out"
  has "cache nel Cestino" "cestino $HOME/.cache/gimp" "$out"
  eq "Start" "thunar.desktop" "$(cat "$c/tod/start-aggiunte")"
  eq "associazioni" $'[Default Applications]\nimage/png=ristretto.desktop;' "$(cat "$c/mimeapps.list")"
  has "barra" '"thunar.desktop"' "$(cat "$c/tod/barra.json")"
  hasnt "barra senza GIMP" "gimp" "$(cat "$c/tod/barra.json")"
  : >"$WORK/dis_$$"
  app_rimuovi termux gimp 0 >/dev/null
  hasnt "senza la spunta i dati restano" "cestino" "$(cat "$WORK/dis_$$")"
}

# --- app, desktop, UI -------------------------------------------------------------
test_apps_routing() {
  pkg_has() { [[ $1 == firefox || $1 == gimp ]]; }
  pkg_install() { echo "nativo:$*" >>"$WORK/calls_$$"; }
  cx_installed() { return 0; }
  cx_pkg_install() { echo "cx:$*" >>"$WORK/calls_$$"; }
  conf_set APPS "firefox libreoffice code-oss gimp"
  apps_install_selected 2>"$WORK/warn_$$"
  eq "chiamate" $'nativo:firefox gimp\ncx:libreoffice libreoffice-gtk3' "$(cat "$WORK/calls_$$")"
  has "code-oss senza alternativa nel container" "code-oss" "$(cat "$WORK/warn_$$")"
}

test_ini_set() {
  local f="$HOME/k.ini"
  have() { [[ $1 != kwriteconfig6 ]] && command -v "$1" >/dev/null 2>&1; }
  _ini_set "$f" Daemon Autolock false
  _ini_set "$f" Daemon LockOnResume false
  _ini_set "$f" Daemon Autolock true
  _ini_set "$f" Altro Chiave 1
  eq "ini" $'\n[Daemon]\nLockOnResume=false\nAutolock=true\n\n[Altro]\nChiave=1' "$(cat "$f")"
}

test_desktop_entries() {
  mkdir -p "$PREFIX/share/applications"
  printf '%s\n' "[Desktop Entry]" "Name=Chromium" "Exec=@@desktop_exec %U" "Icon=@@desktop_icon" >"$PREFIX/share/applications/chromium.desktop"
  desktop_install_entries
  has "Chromium riparato: si avvia" "Exec=$PREFIX/bin/chromium-browser %U" "$(cat "$HOME/.local/share/applications/chromium.desktop")"
  has "Chromium riparato: icona" "Icon=chromium" "$(cat "$HOME/.local/share/applications/chromium.desktop")"
  local c="$HOME/.local/share/applications/tod-control.desktop" h="$HOME/.config/autostart/tod-hook.desktop"
  has "centro di controllo" "Exec=$PREFIX/bin/tod menu" "$(cat "$c")"
  has "terminale" "Terminal=true" "$(cat "$c")"
  has "hook nascosto" "NoDisplay=true" "$(cat "$h")"
  has "hook" "Exec=$PREFIX/bin/tod _hook" "$(cat "$h")"
}

test_ui_noninteractive_defaults() {
  TOD_YES=1
  eq "menu → predefinito" kde "$(ui_menu T X kde xfce XFCE kde KDE)"
  eq "checklist → voci on" $'a\nc' "$(ui_checklist T X a A on b B off c C on)"
  ui_yesno T X y
  fails ui_yesno T X n
  eq "input → predefinito" 42 "$(ui_input T X 42)"
}

test_run_step_status_and_conf_reload() {
  f_ok() { echo "dentro"; conf_set DA_STEP si; }
  run_step "passo ok" f_ok >/dev/null
  eq "config ricaricata dopo la subshell" si "$(conf_get DA_STEP)"
  has "output nel log" "dentro" "$(cat "$TOD_LOG")"
  f_ko() { echo "qualcosa"; return 7; }
  local rc=0
  run_step "passo ko" f_ko >/dev/null 2>&1 || rc=$?
  eq "codice d'uscita propagato" 7 "$rc"
  # stile dei passi: errori controllati esplicitamente (set -e non è affidabile qui)
  f_explicit() { false || return 3; echo "non deve arrivare qui"; }
  rc=0
  run_step "errore esplicito" f_explicit >/dev/null 2>&1 || rc=$?
  eq "ritorno esplicito propagato" 3 "$rc"
  hasnt "si ferma al primo errore" "non deve arrivare qui" "$(cat "$TOD_LOG")"
  # die dentro un passo: il passo fallisce, lo script principale no
  f_die() { die "esco"; }
  rc=0
  run_step "die" f_die >/dev/null 2>&1 || rc=$?
  eq "die → passo fallito" 1 "$rc"
}

test_step_retry_is_bounded() {
  local rc=0
  ( TOD_YES=1; _step prova "passo che fallisce" false ) >/dev/null 2>&1 || rc=$?
  eq "con --yes si arrende" 1 "$rc"
  fails step_done prova
  ( TOD_YES=1; _step prova2 "passo ok" true ) >/dev/null 2>&1
  step_done prova2
}

test_retry_backoff() {
  N=0
  flaky() { N=$((N + 1)); ((N >= 3)); }
  retry 3 0 flaky
  eq "tentativi" 3 "$N"
  N=0
  fails retry 2 0 flaky
}

test_phantom_adb_parsing() {
  # adb finto come programma: il codice lo lancia anche tramite timeout(1)
  cat >"$PREFIX/bin/adb" <<'EOF'
#!/bin/bash
case "$*" in
  "mdns services") printf 'List of discovered mdns services\nadb-R5CX123-AbC\t_adb-tls-connect._tcp\t192.168.1.7:37123\n' ;;
  "connect 127.0.0.1:37123") echo "connected to 127.0.0.1:37123" ;;
  "devices") printf 'List of devices attached\n127.0.0.1:37123\tdevice\nemulator-5554\tdevice\n' ;;
  *"settings get global settings_enable_monitor_phantom_procs") printf 'false\r\n' ;;
  *"device_config get activity_manager max_phantom_processes") printf '2147483647\r\n' ;;
  *) exit 1 ;;
esac
EOF
  chmod +x "$PREFIX/bin/adb"
  PATH="$PREFIX/bin:$PATH"
  eq "mDNS → porta di connessione" 127.0.0.1:37123 "$(adb_try_mdns)"
  eq "dispositivo locale" 127.0.0.1:37123 "$(adb_local_device)"
  ADB_SERIAL=127.0.0.1:37123
  phantom_verify
  eq "monitor" false "$PH_MONITOR"
}

# --- sessione con un finto Termux:X11 (socket UNIX vero) --------------------------
_fake_x11() {
  # server finto: crea il socket X0 e resta vivo; argv[0]=app_process e la classe
  # nella riga di comando lo rendono riconoscibile a x11_server_pids, come quello vero
  cat >"$PREFIX/bin/termux-x11" <<'EOF'
#!/bin/bash
echo "$@" >"$HOME/x11_args"
exec -a app_process python3 -c '
import os, socket, sys, time
d = os.environ["TMPDIR"] + "/.X11-unix"
os.makedirs(d, exist_ok=True)
s = socket.socket(socket.AF_UNIX); s.bind(d + "/X0"); s.listen(1)
time.sleep(60)
' com.termux.x11.CmdEntryPoint "$@"
EOF
  printf '#!/bin/sh\necho "$@" >>"$HOME/am_calls"\n' >"$PREFIX/bin/am"
  cat >"$PREFIX/bin/termux-x11-preference" <<'EOF'
#!/bin/sh
if [ "$1" = list ]; then
  printf '"fullscreen"="false"\n"keepScreenOn"="false"\n"displayResolutionMode"="scaled"\n"dexMetaKeyCapture"="false"\n"showAdditionalKbd"="true"\n'
else
  echo "$@" >"$HOME/prefs_set"
fi
EOF
  chmod +x "$PREFIX/bin/termux-x11" "$PREFIX/bin/am" "$PREFIX/bin/termux-x11-preference"
  PATH="$PREFIX/bin:$PATH"
}

test_x11_prefs_una_per_volta() { # tutte insieme rifiutate: una per volta, e si sa quale non va
  local b="$WORK/xp_$$"; mkdir -p "$b"
  cat >"$b/termux-x11-preference" <<'EOF'
#!/bin/sh
case "$1" in
  list) printf '"fullscreen"="false"\n"displayResolutionMode"="scaled"\n"hideCutout"="false"\n' ;;
  *) [ $# -gt 1 ] && exit 1; case "$1" in *=*) echo "termux-x11-preference [list] {key:value}"; exit 0 ;; hideCutout:*) echo "chiave non valida"; exit 1 ;; esac; echo "$1" >>"$HOME/xp_ok" ;;
esac
EOF
  chmod +x "$b/termux-x11-preference"
  PATH="$b:$PATH"
  have() { [[ $1 == termux-x11-preference ]] || command -v "$1" >/dev/null; }
  x11_apply_prefs
  # chiave:valore come vuole lo strumento vero (con chiave=valore stampava l'uso e non applicava niente)
  has "schermo intero applicato da solo" "fullscreen:true" "$(cat "$HOME/xp_ok")"
  has "risoluzione applicata da sola" "displayResolutionMode:native" "$(cat "$HOME/xp_ok")"
  has "nel registro quale non va e perché" "hideCutout:true (chiave non valida)" "$(cat "$TOD_LOG")"
}

test_session_start_stop() {
  command -v python3 >/dev/null || { echo "  (python3 assente: salto)"; return 0; }
  _fake_x11
  conf_set INSTALLED 1
  session_start >/dev/null
  [[ -S $(x11_sock) ]]
  x11_running
  has "argomenti del server" "-ac -dpi 96 -xstartup $PREFIX/bin/tod _xstartup" "$(cat "$HOME/x11_args")"
  has "activity aperta" "com.termux.x11/com.termux.x11.MainActivity" "$(cat "$HOME/am_calls")"
  eq "preferenze DeX (solo chiavi esistenti)" \
    "fullscreen:true keepScreenOn:true displayResolutionMode:native dexMetaKeyCapture:true showAdditionalKbd:false" \
    "$(cat "$HOME/prefs_set")"
  eq "preferenze applicate una volta" "$TOD_VERSION" "$(conf_get X11_PREFS_APPLIED)"
  has "seconda chiamata: già avviato" "$(t session_already)" "$(session_start)"
  session_stop >/dev/null
  fails x11_running
  eq "socket rimosso" no "$([[ -e $(x11_sock) ]] && echo si || echo no)"
  has "stop chiesto all'app" "com.termux.x11.ACTION_STOP" "$(cat "$HOME/am_calls")"
}

test_x11_with_server_temporary() {
  command -v python3 >/dev/null || { echo "  (python3 assente: salto)"; return 0; }
  _fake_x11
  _show() { echo "DISPLAY=$DISPLAY"; [[ -S $(x11_sock) ]] && echo socket; }
  eq "server temporaneo per il comando" $'DISPLAY=:0\nsocket' "$(x11_with_server _show)"
  fails x11_running
  # codice d'uscita del comando propagato
  local rc=0
  x11_with_server sh -c 'exit 4' || rc=$?
  eq "rc" 4 "$rc"
}

test_display_tune_xfce() {
  cat >"$PREFIX/bin/xrandr" <<'EOF'
#!/bin/sh
echo "Screen 0: minimum 320 x 200, current $(cat "$HOME/res") x $(cat "$HOME/res_h"), maximum 8192 x 8192"
EOF
  cat >"$PREFIX/bin/xfconf-query" <<'EOF'
#!/bin/sh
case "$*" in
  *"-p /general/theme"*"-s"*) echo "$@" >>"$HOME/xfconf"; echo "${@: -1}" ;;
esac
case "$*" in
  *"-s "*) echo "$@" >>"$HOME/xfconf" ;;
  *"-p /general/theme") cat "$HOME/theme" 2>/dev/null ;;
esac
EOF
  chmod +x "$PREFIX/bin/xrandr" "$PREFIX/bin/xfconf-query"
  PATH="$PREFIX/bin:$PATH"
  echo 3840 >"$HOME/res"; echo 2160 >"$HOME/res_h"
  display_tune
  has "4K: DPI" "-c xsettings -p /Xft/DPI -n -t int -s 96" "$(cat "$HOME/xfconf")"
  has "4K: scala 2" "-p /Gdk/WindowScalingFactor -n -t int -s 2" "$(cat "$HOME/xfconf")"
  has "4K: tema xhdpi" "-s Default-xhdpi" "$(cat "$HOME/xfconf")"
  : >"$HOME/xfconf"; echo Default-xhdpi >"$HOME/theme"
  echo 3120 >"$HOME/res"; echo 1440 >"$HOME/res_h"
  display_tune
  has "telefono: DPI 200" "-p /Xft/DPI -n -t int -s 200" "$(cat "$HOME/xfconf")"
  has "torna al tema normale" "-p /general/theme -s Default" "$(cat "$HOME/xfconf")"
  : >"$HOME/xfconf"
  conf_set DPI 132
  display_tune
  has "DPI manuale" "-s 132" "$(cat "$HOME/xfconf")"
}

_fake_proot() { # finto proot-distro sopra il finto rootfs
  _fake_rootfs
  cat >"$PREFIX/bin/proot-distro" <<'EOF'
#!/bin/bash
# finto proot-distro: esegue il comando dopo "--" sull'host, mappando /tmp e /usr/local/bin
echo "$*" >>"$HOME/pd_calls"
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
shift
args=()
for a in "$@"; do
  case $a in
    /tmp/*) a="$TMPDIR/${a#/tmp/}" ;;
    /usr/local/bin/*) a="$ROOTFS$a" ;;
  esac
  args+=("$a")
done
exec "${args[@]}"
EOF
  chmod +x "$PREFIX/bin/proot-distro"
  PATH="$PREFIX/bin:$PATH"
  export ROOTFS="$R"
}

test_cx_plumbing_with_fake_proot() {
  _fake_proot
  local out
  out=$(cx_root uno "due tre" <<'EOF'
echo "argomenti: [$1] [$2]"
echo 'apici singoli: ok dentro lo script'
EOF
)
  has "argomenti al root script" "argomenti: [uno] [due tre]" "$out"
  has "apici negli script" "apici singoli: ok" "$out"
  eq "file temporaneo rimosso" "" "$(find "$TMPDIR" -name 'tod-cx-*' | tr -d '\n')"
  has "login come root con /tmp condiviso" "login --shared-tmp debian --" "$(cat "$HOME/pd_calls")"
  cx_install_runner
  eq "cx_run → tod-run" "ciao" "$(cx_run --raw echo ciao)"
  has "login come utente" "login --user dex --shared-tmp --bind $HOME:$HOME debian -- /usr/local/bin/tod-run --raw echo ciao" "$(cat "$HOME/pd_calls")"
  # GPU del container: scritta nel rootfs (dentro proot diventa /etc/tod/gpu.env)
  printf 'TOD_TEST_GPU=kgsl\n' | cx_write /etc/tod/gpu.env 644
  eq "gpu.env nel rootfs" "TOD_TEST_GPU=kgsl" "$(cat "$R/etc/tod/gpu.env")"
  eq "permessi" 644 "$(stat -c %a "$R/etc/tod/gpu.env")"
}

# --- Termux:X11 sharedUid e ponte GameNative ----------------------------------------
_fake_pm() { # pm finto: $1 = uid di Termux:X11 (vuoto = non installata), $2 = GameNative installato (1/0)
  cat >"$PREFIX/bin/pm" <<EOF
#!/bin/sh
case "\$*" in
  "list packages -U com.termux.x11") [ -n "$1" ] && echo "package:com.termux.x11 uid:$1" ;;
  "path com.termux.x11") [ -n "$1" ] && echo "package:/data/app/~~x/com.termux.x11/base.apk" ;;
  "path app.gamenative") [ "$2" = 1 ] && echo "package:/data/app/~~y/app.gamenative/base.apk" ;;
esac
exit 0
EOF
  chmod +x "$PREFIX/bin/pm"
  export TOD_PM="$PREFIX/bin/pm"
}

test_x11_variant_and_uid() {
  TERMUX_APP__APK_RELEASE=GITHUB
  eq "Termux da GitHub" shareduid "$(x11_variant)"
  TERMUX_APP__APK_RELEASE=F_DROID
  eq "Termux da F-Droid" regular "$(x11_variant)"
  unset TERMUX_APP__APK_RELEASE
  TERMUX_APK_RELEASE=GITHUB
  eq "Termux 0.118 (vecchio nome)" shareduid "$(x11_variant)"
  _fake_pm "$(id -u)" 0
  eq "stesso UID" shared "$(x11_uid_status)"
  _fake_pm 10999 0
  eq "UID diverso" separate "$(x11_uid_status)"
  _fake_pm "" 0
  eq "non installata" unknown "$(x11_uid_status)"
  fails x11_app_installed
}

test_x11_asset_selection() {
  if ! command -v jq >/dev/null; then echo "  (jq assente: salto)"; return 0; fi
  local rel='{"assets":[
    {"name":"termux-x11-nightly-1.03.01-0-all.deb","browser_download_url":"u/deb"},
    {"name":"termux-x11-universal-sharedUid-debug.apk","browser_download_url":"u/shared"},
    {"name":"termux-x11-universal-debug.apk","browser_download_url":"u/universal"},
    {"name":"termux-x11-arm64-v8a-debug.apk","browser_download_url":"u/arm64"}]}'
  eq "sharedUid" u/shared "$(jq -r "$(x11_asset_query shareduid)" <<<"$rel")"
  eq "normale: universal, mai sharedUid" u/universal "$(jq -r "$(x11_asset_query regular)" <<<"$rel")"
  rel='{"assets":[{"name":"termux-x11-universal-sharedUid-debug.apk","browser_download_url":"u/shared"},
                  {"name":"app-arm64-v8a-debug.apk","browser_download_url":"u/arm64"}]}'
  eq "normale: ripiego arm64" u/arm64 "$(jq -r "$(x11_asset_query regular)" <<<"$rel")"
}

test_x11_glamor_apk() { # 30 ottobre: la Termux:X11 di Termux On DeX (glamor) al posto della nightly, con ripiego
  if ! command -v jq >/dev/null; then echo "  (jq assente: salto)"; return 0; fi
  TOD_X11_APK="$WORK/x11g_$$.apk"; TOD_X11_DEB="$WORK/x11g_$$.deb"
  local TOD_ROOT="$WORK/x11g_radice_$$"   # mai la dist/ vera del repository
  mkdir -p "$TOD_ROOT"
  # la release finta in un file: x11_app_download ha una sua variabile locale "rel"
  echo '{"assets":[{"name":"termux-x11-nightly-1.03.01-0-all.deb","browser_download_url":"u/deb"},
    {"name":"termux-x11-universal-sharedUid-debug.apk","browser_download_url":"u/shared"},
    {"name":"termux-x11-universal-debug.apk","browser_download_url":"u/universal"}]}' >"$WORK/x11g_rel_$$"
  net_get() { cat "$WORK/x11g_rel_$$"; }
  net_download() { # url dest [sha]: "rotto" = la release con glamor non risponde
    echo "$1" >>"$WORK/x11g_dl_$$"
    if [[ $1 == *x11-glamor-* && -f $WORK/x11g_rotto_$$ ]]; then return 1; fi
    echo "da $1" >"$2"
  }
  x11_app_download shareduid
  eq "sharedUid: la nostra dalla release" 1 "$(conf_get X11_GLAMOR)"
  eq "si ricorda quale versione" "$X11_GLAMOR_VERSIONE" "$(conf_get X11_GLAMOR_INSTALLATA)"
  has "scaricata dalla release" "releases/download/x11-glamor-$X11_GLAMOR_VERSIONE/termux-x11-glamor-shareduid.apk" "$(cat "$TOD_X11_APK")"
  eq "niente pacchetto della nightly" no "$([[ -e $TOD_X11_DEB ]] && echo si || echo no)"
  touch "$WORK/x11g_rotto_$$"
  x11_app_download shareduid
  eq "release irraggiungibile: nightly ufficiale" "da u/shared" "$(cat "$TOD_X11_APK")"
  eq "release irraggiungibile: glamor spento" 0 "$(conf_get X11_GLAMOR)"
  rm -f "$WORK/x11g_rotto_$$"
  x11_app_download regular
  eq "variante normale: sempre l'ufficiale" "da u/universal" "$(cat "$TOD_X11_APK")"
  eq "variante normale: glamor spento" 0 "$(conf_get X11_GLAMOR)"
  conf_set X11_GLAMOR_APK 0
  x11_app_download shareduid
  eq "X11_GLAMOR_APK=0: l'ufficiale" "da u/shared" "$(cat "$TOD_X11_APK")"
  conf_set X11_GLAMOR_APK 1
  mkdir -p "$TOD_ROOT/dist"
  echo locale >"$TOD_ROOT/dist/termux-x11-glamor-shareduid.apk"
  X11_GLAMOR_SHA256=$(sha256sum "$TOD_ROOT/dist/termux-x11-glamor-shareduid.apk" | cut -d' ' -f1)
  : >"$WORK/x11g_dl_$$"
  x11_app_download shareduid
  eq "copia in dist/ con l'impronta giusta: niente rete" locale "$(cat "$TOD_X11_APK")"
  eq "copia in dist/: nessuno scaricamento" "" "$(cat "$WORK/x11g_dl_$$")"
}

test_self_update_da_github() { # 1° novembre: Termux On DeX si aggiorna anche dalla finestra Aggiornamenti
  have git || { echo "  (git assente: salto)"; return 0; }
  local o="$WORK/self_$$/origine" out
  local HOME="$WORK/self_$$/casa"
  local c="$HOME/.local/share/tod"
  g() { git -c user.name=prova -c user.email=prova@example.com "$@"; }
  mkdir -p "$o" "$HOME/.local/share"
  g -C "$o" init -q && g -C "$o" checkout -q -b v2
  echo uno >"$o/tod" && chmod 644 "$o/tod"   # anche senza permesso di esecuzione nel repository
  g -C "$o" add tod && g -C "$o" commit -q -m "primo"
  git clone -q --depth 1 -b v2 "file://$o" "$c"   # come install.sh
  local TOD_ROOT=$c
  eq "appena installato: niente di nuovo" "stato=aggiornato" "$(self_check)"
  echo due >"$o/tod" && g -C "$o" commit -qam "Barra: icona giusta"
  echo tre >>"$o/tod" && g -C "$o" commit -qam "Dischi in inglese"
  out=$(self_check)
  has "novità su GitHub" "stato=nuovo" "$out"
  has "contate" "novita=2" "$out"
  eq "con i titoli, la più nuova prima" "titolo=Dischi in inglese" "$(sed -n '/^titolo=/{p;q}' <<<"$out")"
  self_update
  eq "codice nuovo" "$(git -C "$o" rev-parse HEAD)" "$(git -C "$c" rev-parse HEAD)"
  eq "poi niente di nuovo" "stato=aggiornato" "$(self_check)"
  # la storia riscritta su GitHub (commit unico + push --force): git pull --ff-only falliva. Qui la copia
  # non è più "leggera" (il controllo ha scaricato tutti i commit): si riconosce dal posto
  g -C "$o" checkout -q --orphan nuovo && g -C "$o" commit -q -m "Termux On DeX v2" && g -C "$o" branch -q -M nuovo v2
  out=$(self_check)
  has "storia riscritta: c'è la novità" "stato=nuovo" "$out"
  hasnt "storia riscritta: novità non contate" "novita=" "$out"
  self_update
  eq "storia riscritta: aggiornato lo stesso" "$(git -C "$o" rev-parse HEAD)" "$(git -C "$c" rev-parse HEAD)"
  # mai sopra modifiche fatte a mano
  echo mio >>"$c/tod"
  g -C "$o" commit -q --allow-empty -m "altro"
  fails self_update
  eq "modifiche fatte a mano: restano" 1 "$(grep -c mio "$c/tod")"
  # una copia di sviluppo (clone completo altrove): solo avanti, mai allineata a forza
  local d="$WORK/self_$$/sviluppo"
  git clone -q -b v2 "file://$o" "$d"
  local TOD_ROOT=$d
  echo quattro >"$o/tod" && g -C "$o" commit -qam "avanti"
  self_update
  eq "sviluppo: avanti" "$(git -C "$o" rev-parse HEAD)" "$(git -C "$d" rev-parse HEAD)"
  g -C "$o" checkout -q --orphan nuovo2 && g -C "$o" commit -q -m "riscritta" && g -C "$o" branch -q -M nuovo2 v2
  fails self_update
  eq "sviluppo, storia riscritta: niente reset" "avanti" "$(git -C "$d" log -1 --format=%s)"
  # copia senza ramo su GitHub (quella di Pietro) o senza git: nessuna voce
  local TOD_ROOT="$WORK/self_$$/locale"
  mkdir -p "$TOD_ROOT" && git -C "$TOD_ROOT" init -q
  eq "copia senza ramo remoto: nessuna voce" "stato=nessuno" "$(self_check)"
  local TOD_ROOT="$WORK/self_$$/senza-git"
  mkdir -p "$TOD_ROOT"
  eq "senza git: nessuna voce" "stato=nessuno" "$(self_check)"
}

test_aggiornamenti_voce_tod() { # la voce "Termux On DeX" nella finestra Aggiornamenti e la sua installazione
  local f="$WORK/upd_$$" out
  mkdir -p "$f"
  printf '#!/bin/sh\necho "uscita di prova"\necho x11=nuova\n' >"$f/tod-ok"
  # gli errori di tod vanno su stderr, seguiti dal rimando al registro
  printf '#!/bin/sh\necho "✘ errore finto" >&2\necho "  Dettagli nel registro: /x/tod.log" >&2\nexit 1\n' >"$f/tod-ko"
  out=$(LANGUAGE=it HOME="$f" python3 - "$ROOT/share/tod-updates.py" "$f" <<'EOF'
import importlib.util as u, sys
s = u.spec_from_file_location("up", sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)
print(m.tod_item("stato=aggiornato"), m.tod_item("stato=nessuno"), m.tod_item("stato=errore"))
it = m.tod_item("stato=nuovo\nda=aaaaaaa\na=bbbbbbb\nnovita=2\ntitolo=Barra: icona giusta\ntitolo=Dischi in inglese\n")
print(it["app"], "|", it["desc"], "|", it["where"], it["from"], it["to"])
print(m.tod_item("stato=nuovo\nda=a\na=b\ntitolo=Termux On DeX v2")["desc"])
print(m.summary({"tod": [it], "termux": [{"name": "curl", "to": "1"}], "debian": []}))
m.TOD = sys.argv[2] + "/tod-ok"
ok, nota = m.install([it], lambda *a: None)
print(ok, nota)
m.TOD = sys.argv[2] + "/tod-ko"
print(m.install([it], lambda *a: None))
EOF
)
  eq "niente di nuovo: nessuna voce" "None None None" "$(sed -n 1p <<<"$out")"
  eq "la voce: nome, novità e la modifica più nuova" "Termux On DeX | 2 novità · Barra: icona giusta | tod aaaaaaa bbbbbbb" "$(sed -n 2p <<<"$out")"
  eq "storia riscritta: nuova versione" "Nuova versione · Termux On DeX v2" "$(sed -n 3p <<<"$out")"
  eq "nel riassunto per prima" "Termux On DeX e un altro aggiornamento" "$(sed -n 4p <<<"$out")"
  has "installata dalla finestra: cosa cambia e quando" "True Termux On DeX è aggiornato: barra, Start e finestre cambiano al prossimo avvio" "$(sed -n 5p <<<"$out")"
  has "Termux:X11 nuova: da Termux, non da qui" "installala da Termux con tod update" "$(sed -n 5p <<<"$out")"
  eq "errore: l'ultima riga di tod" "(False, 'errore finto')" "$(sed -n 6p <<<"$out")"
  has "la finestra chiama tod" '"_self_update"' "$(cat "$ROOT/share/tod-updates.py")"
}

test_x11_glamor_nuova_versione() { # la Termux:X11 nuova si propone solo da tod update (installarla chiude Termux)
  conf_set X11_GLAMOR 1
  if x11_glamor_da_aggiornare; then echo "  ✘ installazione di prima del 1° novembre (versione 1): niente da fare"; return 1; fi
  local X11_GLAMOR_VERSIONE=2
  x11_glamor_da_aggiornare || { echo "  ✘ versione 2 uscita: da aggiornare"; return 1; }
  usb_on() { return 1; }
  rete_on() { return 1; }   # niente apt veri dai test (samba)
  videochip_on() { return 1; }   # né libva e compilazioni del video col chip
  media_mancanti() { :; }   # né GStreamer
  desktop_install_entries() { :; }
  eq "dopo il codice nuovo: avviso per la finestra" "x11=nuova" "$(self_after)"
  conf_set X11_GLAMOR_INSTALLATA 2
  eq "già la nuova: niente" "" "$(self_after)"
  conf_set X11_GLAMOR 0
  conf_set X11_GLAMOR_INSTALLATA 1
  eq "con la Termux:X11 ufficiale: mai" "" "$(self_after)"
}

test_install_sh_full_upgrade_first() { # 31 ottobre: "CANNOT LINK EXECUTABLE" su un Termux appena installato
  local d="$WORK/inst_$$" b="$WORK/inst_$$/bin"
  mkdir -p "$d/com.termux/files/usr/bin" "$b" "$d/script"
  cp "$ROOT/install.sh" "$d/script/"   # lontano dal repository: niente "copia locale"
  printf '#!/bin/sh\necho "apt-get $*" >>%s/log\n' "$d" >"$b/apt-get"
  printf '#!/bin/sh\nexit 0\n' >"$b/dpkg"
  printf '#!/bin/sh\necho "tod $*" >>%s/log\n' "$d" >"$d/tod-finto"
  printf '#!/bin/sh\nfor a; do dest=$a; done\nmkdir -p "$dest" && cp %s/tod-finto "$dest/tod"\n' "$d" >"$b/git"
  printf '#!/bin/sh\necho "pkg $*" >>%s/log\n' "$d" >"$b/pkg"
  printf '#!/bin/sh\nexit 0\n' >"$b/apt-cache"   # gli elenchi dei pacchetti ci sono
  chmod +x "$b"/* "$d/tod-finto"
  PATH="$b:$PATH" PREFIX="$d/com.termux/files/usr" TOD_HOME="$d/tod" bash "$d/script/install.sh" --yes </dev/null >/dev/null 2>&1
  eq "elenchi da pkg (sceglie il mirror), aggiornamento completo, poi i pacchetti e tod" \
    "$(printf '%s\n' 'pkg update' full-upgrade 'install git dialog jq curl ca-certificates' 'tod install --yes')" \
    "$(sed -e 's/^apt-get -y -q -o [^ ]* -o [^ ]* //' "$d/log")"
  # 1° novembre (S20+): nessun server raggiungibile, il DNS del telefono li manda a 127.0.0.1
  : >"$d/log"
  printf '#!/bin/sh\nexit 100\n' >"$b/apt-cache"
  printf '#!/bin/sh\nprintf 127.0.0.1\n' >"$b/curl"
  chmod +x "$b"/*
  local out rc=0
  out=$(PATH="$b:$PATH" PREFIX="$d/com.termux/files/usr" TOD_HOME="$d/tod" bash "$d/script/install.sh" --yes </dev/null 2>&1) || rc=$?
  eq "senza elenchi si ferma" 1 "$rc"
  hasnt "senza elenchi: niente install alla cieca" "install git" "$(cat "$d/log")"
  has "il perché: il DNS del telefono" "DNS privato" "$out"
  printf '#!/bin/sh\nprintf 104.16.1.1\n' >"$b/curl"
  out=$(PATH="$b:$PATH" PREFIX="$d/com.termux/files/usr" TOD_HOME="$d/tod" bash "$d/script/install.sh" --yes </dev/null 2>&1) || true
  has "rete giù: controllare la connessione" "Controlla la connessione" "$out"
}

test_pkg_refresh_altro_mirror() { # un server dei pacchetti irraggiungibile: pkg di Termux sceglie un altro mirror
  apt_run() { echo "apt $*" >>"$HOME/apt"; [[ $* != *--error-on=any* ]]; }
  printf '#!/bin/sh\necho "pkg $*" >>"$HOME/pkg"\n' >"$PREFIX/bin/pkg"
  chmod +x "$PREFIX/bin/pkg"
  PATH="$PREFIX/bin:$PATH"
  pkg_refresh
  eq "server giù: pkg update" "pkg update" "$(cat "$HOME/pkg")"
  eq "poi gli elenchi dal mirror nuovo" 2 "$(grep -c update "$HOME/apt")"
  rm -f "$HOME/pkg" "$HOME/apt"
  apt_run() { echo "apt $*" >>"$HOME/apt"; }
  pkg_refresh
  eq "tutto a posto: niente pkg" "" "$(cat "$HOME/pkg" 2>/dev/null)"
  eq "una volta sola" 1 "$(grep -c update "$HOME/apt")"
}

test_games_bridge() {
  _fake_pm "" 1
  printf '#!/bin/sh\necho "$@" >>"$HOME/am_calls"\n' >"$PREFIX/bin/am"
  chmod +x "$PREFIX/bin/am"
  PATH="$PREFIX/bin:$PATH"
  game_add 1091500 "Cyberpunk 2077" >/dev/null
  local f="$HOME/.local/share/applications/tod-game-1091500.desktop"
  has "voce nel menu" "Exec=$PREFIX/bin/tod game run 1091500" "$(cat "$f")"
  has "categoria" "Categories=Game;" "$(cat "$f")"
  has "elenco" "Cyberpunk 2077" "$(game_list)"
  game_run 1091500
  eq "intent di GameNative" "start -n app.gamenative/app.gamenative.MainActivity -a app.gamenative.LAUNCH_GAME --ei app_id 1091500" \
    "$(cat "$HOME/am_calls")"
  fails game_add "cyberpunk"
  fails game_run 12ab
  games_entries
  has "voce GameNative" "Exec=$PREFIX/bin/tod game open" "$(cat "$HOME/.local/share/applications/tod-gamenative.desktop")"
  game_remove 1091500 >/dev/null
  eq "rimosso" no "$([[ -e $f ]] && echo si || echo no)"
  _fake_pm "" 0
  fails game_run 1091500
  games_entries
  eq "senza GameNative niente voce" no "$([[ -e $HOME/.local/share/applications/tod-gamenative.desktop ]] && echo si || echo no)"
}

# --- signal 9: associazione ADB (il caso reale del Galaxy S26) -----------------------
test_phantom_input_parsing() {
  eq "porta semplice" 41235 "$(ph_port_from 41235)"
  eq "indirizzo completo" 41235 "$(ph_port_from "192.168.1.5:41235")"
  eq "con spazi" 41235 "$(ph_port_from " 41235 ")"
  fails ph_port_from "abc"
  fails ph_port_from ""
  eq "codice" 123456 "$(ph_code_from 123456)"
  eq "codice con spazio" 123456 "$(ph_code_from "123 456")"
  fails ph_code_from 12345
}

_fake_adb() { # adb finto: l'associazione riesce solo se $HOME/pair_ok esiste
  cat >"$PREFIX/bin/adb" <<'EOF'
#!/bin/bash
echo "adb $*" >>"$HOME/adb_calls"
case "$1" in
  start-server) exit 0 ;;
  devices) echo "List of devices attached" ;;
  mdns) echo "List of discovered mdns services" ;;
  pair)
    if [ -f "$HOME/pair_ok" ]; then touch "$HOME/paired"; echo "Successfully paired to $2 [guid=adb-R5CX-abc]"
    else echo "Failed: Wrong password or connection was dropped."; fi
    exit 0 ;;   # come il vero adb: esce con 0 anche quando fallisce
  connect)
    if [ -f "$HOME/paired" ] && [ "$2" = "127.0.0.1:37999" ]; then echo "connected to $2"
    else echo "failed to authenticate to $2"; fi ;;
esac
EOF
  cat >"$PREFIX/bin/nmap" <<'EOF'
#!/bin/sh
printf 'Host: 127.0.0.1 ()\tPorts: 37999/open/tcp//unknown///, 41235/open/tcp//unknown///\n'
EOF
  printf '#!/bin/sh\nexit 0\n' >"$PREFIX/bin/am"
  chmod +x "$PREFIX/bin/adb" "$PREFIX/bin/nmap" "$PREFIX/bin/am"
  PATH="$PREFIX/bin:$PATH"
  # risposte dell'utente, una per chiamata (le chiamate avvengono in subshell: contatore su file)
  ui_input() {
    local n
    n=$(cat "$HOME/ui_n" 2>/dev/null || echo 0)
    n=$((n + 1))
    echo "$n" >"$HOME/ui_n"
    sed -n "${n}p" "$HOME/ui_answers"
  }
}

test_phantom_pair_failure_is_not_success() {
  _fake_adb
  printf '%s\n' "192.168.1.5:41235" "123456" 41235 123456 41235 123456 >"$HOME/ui_answers"
  local rc=0
  adb_connect_local >"$HOME/out" 2>&1 || rc=$?
  eq "si arrende dopo 3 tentativi" 1 "$rc"
  hasnt "nessun tentativo di connessione dopo un'associazione fallita" "connect 127.0.0.1:41235" \
    "$(grep -v '^adb connect 127.0.0.1:37999' "$HOME/adb_calls" || true)"
  has "spiega perché" "popup" "$(cat "$HOME/out")"
  eq "3 associazioni tentate" 3 "$(grep -c '^adb pair' "$HOME/adb_calls")"
}

test_phantom_pair_success_finds_connect_port() {
  _fake_adb
  touch "$HOME/pair_ok"
  printf '%s\n' "192.168.1.5:41235" "123 456" >"$HOME/ui_answers"
  adb_connect_local >"$HOME/out" 2>&1
  eq "porta di connessione trovata da sola" 127.0.0.1:37999 "$ADB_SERIAL"
  eq "all'utente chiesti solo porta e codice del popup" 2 "$(cat "$HOME/ui_n")"
  has "associazione con l'indirizzo ripulito" "adb pair 127.0.0.1:41235 123456" "$(cat "$HOME/adb_calls")"
}

# --- interfaccia grafica dell'installazione -------------------------------------------
_fake_dialog() { # dialog finto: registra quello che riceve la barra, risponde ai popup da file
  cat >"$PREFIX/bin/dialog" <<'EOF'
#!/bin/bash
for a in "$@"; do
  case $a in
    --gauge) cat >>"$HOME/gauge_feed"; exit 0 ;;
    --yesno)
      n=$(cat "$HOME/yesno_n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" >"$HOME/yesno_n"
      echo yesno >>"$HOME/dialog_calls"
      rc=$(sed -n "${n}p" "$HOME/yesno_rcs" 2>/dev/null); exit "${rc:-0}" ;;
    --textbox | --infobox | --msgbox) echo "$a" >>"$HOME/dialog_calls"; exit 0 ;;
  esac
done
exit 0
EOF
  chmod +x "$PREFIX/bin/dialog"
  PATH="$PREFIX/bin:$PATH"
  TUI_TTY=/dev/null TUI_ON=1 TUI_BACKTITLE=test
  export TOD_PROGRESS_FILE="$TMPDIR/tod-progress"
  : >"$TOD_PROGRESS_FILE"
}

test_tui_parse_status() {
  export TOD_PROGRESS_FILE="$TMPDIR/p"
  : >"$TOD_PROGRESS_FILE"
  eq "vuoto" "|" "$(tui_parse_status)"
  echo "dlstatus:3:12.5:Retrieving file 3 of 25" >>"$TOD_PROGRESS_FILE"
  eq "download (0-30%)" "3|Download dei pacchetti: 3 di 25" "$(tui_parse_status)"
  echo "pmstatus:xfce4-panel:50.0:Installing xfce4-panel" >>"$TOD_PROGRESS_FILE"
  eq "installazione (30-100%)" "65|Installazione: xfce4-panel" "$(tui_parse_status)"
  echo "tod:40:Test del profilo GPU kgsl" >>"$TOD_PROGRESS_FILE"
  eq "nota con percentuale" "40|Test del profilo GPU kgsl" "$(tui_parse_status)"
  echo "tod:-:Download: mesa.deb" >>"$TOD_PROGRESS_FILE"
  eq "nota senza percentuale" "|Download: mesa.deb" "$(tui_parse_status)"
  echo "pmerror:libfoo:10:dpkg error" >>"$TOD_PROGRESS_FILE"
  has "errore di dpkg" "Errore nel pacchetto libfoo" "$(tui_parse_status)"
}

test_tui_overall_and_render() {
  tui_register a "Alfa" 10
  tui_register b "Beta" 30
  tui_register c "Gamma" 60
  TUI_STATE[a]=done
  TUI_STATE[b]=run
  eq "avanzamento totale pesato" 25 "$(tui_overall b 50)"
  local r
  r=$(tui_render "dettaglio")
  has "intestazione" "Passo 2 di 3" "$r"
  has "passo fatto" '\Z2✔\Zn Alfa' "$r"
  has "passo in corso" '\Z4▶\Zn \ZbBeta\ZB' "$r"
  has "passo in attesa" '· Gamma' "$r"
  has "dettaglio" "dettaglio" "$r"
}

test_tui_run_progress() {
  _fake_dialog
  tui_register a "Primo" 50
  tui_register b "Secondo" 50
  TUI_STATE[a]=done
  step_b() { for i in 20 60 90; do printf 'pmstatus:pkg%s:%s.0:Installing\n' "$i" "$i" >>"$TOD_PROGRESS_FILE"; sleep 0.4; done; }
  tui_run b step_b
  tui_gauge_close
  eq "passo completato" done "${TUI_STATE[b]}"
  step_done b
  local feed pcts
  feed=$(cat "$HOME/gauge_feed")
  has "pacchetto mostrato" "Installazione: pkg60" "$feed"
  pcts=$(awk 'prev == "XXX" && /^[0-9]+$/ {print} {prev = $0}' "$HOME/gauge_feed")
  eq "arriva al 100%" 100 "$(tail -n 1 <<<"$pcts")"
  eq "la barra non torna mai indietro" "$(sort -n <<<"$pcts")" "$pcts"
}

test_tui_close_with_surviving_process() {
  _fake_dialog
  tui_register d "Passo con un demone" 100
  # come il server X dei test GPU: un processo che sopravvive al passo
  with_daemon() { sleep 47 & echo $! >"$HOME/daemon_pid"; }
  tui_run d with_daemon
  local t0=$SECONDS
  tui_gauge_close
  eq "la barra si chiude subito (il passo non ha ereditato la FIFO)" yes "$( (( SECONDS - t0 <= 2 )) && echo yes || echo no)"
  kill "$(cat "$HOME/daemon_pid")" 2>/dev/null || true
  # e anche se un processo la tiene davvero aperta, l'attesa è limitata
  tui_gauge_open
  sleep 43 &
  local holder=$!
  t0=$SECONDS
  tui_gauge_close
  eq "attesa limitata" yes "$( (( SECONDS - t0 <= 4 )) && echo yes || echo no)"
  kill "$holder" 2>/dev/null || true
  eq "intestazione finale" "Completato" "$(tui_render "" | sed -n 1p | cut -d' ' -f1)"
}

test_tui_run_retry_and_log() {
  _fake_dialog
  printf '%s\n' 3 0 >"$HOME/yesno_rcs"   # prima "Mostra log", poi "Riprova"
  tui_register x "Passo instabile" 100
  flaky() { if [[ -f $HOME/second ]]; then return 0; fi; touch "$HOME/second"; echo "errore finto"; return 5; }
  tui_run x flaky
  tui_gauge_close
  eq "riuscito al secondo tentativo" done "${TUI_STATE[x]}"
  eq "popup: log mostrato e poi riprova" $'yesno\n--textbox\nyesno' "$(cat "$HOME/dialog_calls")"
}

test_tui_run_quit() {
  _fake_dialog
  printf '%s\n' 1 >"$HOME/yesno_rcs"   # "Esci"
  tui_register y "Passo rotto" 100
  broken() { return 2; }
  local rc=0
  ( tui_run y broken ) >/dev/null 2>&1 || rc=$?
  eq "uscita con errore" 1 "$rc"
  fails step_done y
}

test_apt_run_status_fd() {
  cat >"$PREFIX/bin/apt-get" <<'EOF'
#!/bin/bash
for a; do if [ "$a" = "APT::Status-Fd=3" ]; then echo "pmstatus:foo:42.0:Installing foo" >&3; fi; done
echo "apt-get $*" >>"$HOME/apt_args"
EOF
  chmod +x "$PREFIX/bin/apt-get"
  PATH="$PREFIX/bin:$PATH"
  export TOD_PROGRESS_FILE="$TMPDIR/p"
  : >"$TOD_PROGRESS_FILE"
  apt_run -y install foo
  has "Status-Fd attivo" "-o APT::Status-Fd=3" "$(cat "$HOME/apt_args")"
  eq "riga di stato scritta" "pmstatus:foo:42.0:Installing foo" "$(cat "$TOD_PROGRESS_FILE")"
  unset TOD_PROGRESS_FILE
  : >"$HOME/apt_args"
  apt_run -y install foo
  hasnt "senza interfaccia niente Status-Fd" "Status-Fd" "$(cat "$HOME/apt_args")"
}

# --- casi reali dal log del Galaxy S26 Ultra ---------------------------------------
test_audio_restarts_broken_daemon() {
  _fake_pulse
  local st="$HOME/pulse_state"
  touch "$st/running" "$st/broken"
  printf '0\tmodule-null-sink\t\n' >"$st/modules"
  audio_start
  has "demone riavviato" "pulseaudio --kill" "$(cat "$st/calls")"
  eq "uscita vera dopo il riavvio" AAudio_sink "$(cat "$st/default")"
  has "motivo nel log" "nessuna uscita vera, riavvio PulseAudio" "$(cat "$TOD_LOG")"
}

test_audio_start_fallito_si_capisce() { # 9 novembre: "PulseAudio non è partito" senza motivo e senza volume
  _fake_pulse
  local st="$HOME/pulse_state"
  touch "$st/fail_start"
  audio_start
  has "il messaggio di PulseAudio finisce nel log" "audio: PulseAudio non parte (tentativo 1): E: [pulseaudio] main.c: Daemon startup failed." "$(cat "$TOD_LOG")"
  eq "secondo tentativo riuscito: c'è l'uscita" AAudio_sink "$(cat "$st/default")"
  eq "nessun file lasciato" "" "$(ls "$TMPDIR" | grep tod-pulse-avvio || true)"
}

test_audio_status_unreachable() {
  _fake_pulse
  touch "$HOME/pulse_state/running"
  audio_pactl() { return 1; }   # PulseAudio vivo ma non risponde (es. da dentro il proot)
  local out
  out=$(audio_status 2>&1)
  hasnt "niente Errore inatteso" "Errore inatteso" "$out"
  has "flussi a zero" "0" "$out"
}

test_audio_test_reaches_container() {
  _fake_proot
  # pacat finto: conta i byte ricevuti (da file o da stdin)
  cat >"$PREFIX/bin/pacat" <<'EOF'
#!/bin/bash
f=/dev/stdin
for a in "$@"; do case $a in -*) ;; *) f=$a ;; esac; done
if [[ $f == /dev/stdin ]]; then wc -c <"$f" >"$HOME/pacat_native"; else wc -c <"$f" >"$HOME/pacat_cx"; fi
EOF
  chmod +x "$PREFIX/bin/pacat"
  audio_start() { :; }
  audio_test >/dev/null 2>&1
  eq "nota nativa" 16000 "$(cat "$HOME/pacat_native" 2>/dev/null)"
  eq "nota arrivata nel container" 16000 "$(cat "$HOME/pacat_cx" 2>/dev/null)"
  has "esito nel log" "nativo=ok container=ok" "$(cat "$TOD_LOG")"
}

test_cx_rootfs_proot_distro_5() {
  local base="$PREFIX/var/lib/proot-distro"
  fails cx_rootfs
  mkdir -p "$base/installed-rootfs/debian/etc"
  eq "proot-distro 4" "$base/installed-rootfs/debian" "$(cx_rootfs)"
  mkdir -p "$base/containers/debian/rootfs/etc"
  eq "proot-distro 5 ha la precedenza" "$base/containers/debian/rootfs" "$(cx_rootfs)"
  rm -rf "$base/installed-rootfs"
  eq "solo proot-distro 5" "$base/containers/debian/rootfs" "$(cx_rootfs)"
  # con la 5 niente --bind della home (è già montata): niente avvisi a ogni avvio di un'app
  printf '#!/bin/sh\necho "$@" >>"$HOME/pd_calls"\n' >"$PREFIX/bin/proot-distro"
  chmod +x "$PREFIX/bin/proot-distro"
  PATH="$PREFIX/bin:$PATH"
  cx_run --raw true
  hasnt "nessun bind doppio con la 5" "--bind" "$(cat "$HOME/pd_calls")"
}

test_x11_server_restart_retry() {
  command -v python3 >/dev/null || { echo "  (python3 assente: salto)"; return 0; }
  # il primo avvio non crea il socket (come il 2° avvio ravvicinato sul telefono), il secondo sì
  cat >"$PREFIX/bin/termux-x11" <<'EOF'
#!/bin/bash
n=$(cat "$HOME/x11_n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" >"$HOME/x11_n"
if [ "$n" -eq 1 ]; then echo "errore finto del server"; exit 1; fi
exec -a app_process python3 -c '
import os, socket, time
d = os.environ["TMPDIR"] + "/.X11-unix"; os.makedirs(d, exist_ok=True)
s = socket.socket(socket.AF_UNIX); s.bind(d + "/X0"); s.listen(1); time.sleep(30)' com.termux.x11.CmdEntryPoint "$@"
EOF
  printf '#!/bin/sh\nexit 0\n' >"$PREFIX/bin/am"
  chmod +x "$PREFIX/bin/termux-x11" "$PREFIX/bin/am"
  PATH="$PREFIX/bin:$PATH"
  TOD_X11_WAIT=2 TOD_X11_RETRY_PAUSE=0 TOD_X11_SETTLE=0
  eq "riesce al secondo tentativo" ok "$(x11_with_server sh -c 'echo ok')"
  eq "due avvii" 2 "$(cat "$HOME/x11_n")"
  has "il motivo finisce nel log" "errore finto del server" "$(cat "$TOD_LOG")"
  fails x11_running
}

test_gpu_setup_lfdevs_x11_failure() {
  _gpu_mocks 'zink-turnip|ok|zink Vulkan 1.4(Adreno (TM) 840 (MESA_TURNIP))|' 'kgsl|ok|FD840|'
  # come sul telefono: il primo server X parte, il secondo (dopo i driver sperimentali) no
  x11_with_server() {
    local n
    n=$(cat "$HOME/xws_n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" >"$HOME/xws_n"
    if (( n >= 2 )); then return 1; fi
    "$@"
  }
  gpu_setup >/dev/null 2>&1
  eq "resta sui driver ufficiali" official "$(gpu_stack)"
  eq "profilo dal test vero" zink-turnip "$(conf_get GPU_PROFILE)"
  has "risultati ufficiali ripristinati" "MESA_TURNIP" "$(cat "$TOD_GPU_PROBE")"
  has "spiegato nel log" "server X non si avvia" "$(cat "$TOD_LOG")"
}

test_x11_server_senza_lock() { # server della sessione vivo ma senza file di lock (dopo un arresto con l'app aperta)
  local d="$WORK/srv_$$"
  mkdir -p "$TMPDIR/.X11-unix" "$d"
  rm -f "$TMPDIR/.X0-lock"
  : >"$d/com.termux.x11"
  : >"$d/:0"
  # un processo con la stessa riga di comando del server vero ("termux-x11 com.termux.x11 :0 ...")
  (cd "$d" && exec -a termux-x11 tail com.termux.x11 :0 -f -s 1 >/dev/null 2>&1) &
  local srv=$!
  sleep 0.3
  rm -f "$(x11_sock)"
  python3 -c "import socket, sys; socket.socket(socket.AF_UNIX).bind(sys.argv[1])" "$(x11_sock)"
  has "riconosciuto dalla riga di comando" "$srv" "$(x11_server_pids)"
  eq "server acceso" si "$(x11_running && echo si || echo no)"
  kill "$srv" 2>/dev/null || true
}

test_x11_lock_file_detection() {
  TOD_X11_SETTLE=0
  printf '#!/bin/sh\nexit 0\n' >"$PREFIX/bin/am"
  chmod +x "$PREFIX/bin/am"
  PATH="$PREFIX/bin:$PATH"
  mkdir -p "$TMPDIR/.X11-unix"
  # server rimasto vivo, con un nome che i vecchi controlli non riconoscevano
  bash -c 'exec -a termux-x11 sleep 41' &
  local srv=$!
  sleep 0.2
  printf '%10d\n' "$srv" >"$TMPDIR/.X0-lock"
  touch "$(x11_sock)"
  has "riconosciuto dal file di lock" "$srv" "$(x11_server_pids)"
  x11_cleanup_stale
  eq "socket di un server vivo non cancellato" si "$([[ -e $(x11_sock) ]] && echo si || echo no)"
  x11_kill
  eq "server chiuso" no "$(kill -0 "$srv" 2>/dev/null && echo si || echo no)"
  eq "lock rimosso dopo la chiusura" no "$([[ -e $TMPDIR/.X0-lock ]] && echo si || echo no)"
  # PID riciclato da un processo qualunque: non è un server X, non va toccato
  sleep 42 &
  local other=$!
  printf '%10d\n' "$other" >"$TMPDIR/.X0-lock"
  fails x11_lock_pid
  kill "$other" 2>/dev/null || true
}

test_session_start_retry_logs_server_error() {
  command -v python3 >/dev/null || { echo "  (python3 assente: salto)"; return 0; }
  cat >"$PREFIX/bin/termux-x11" <<'EOF'
#!/bin/bash
n=$(cat "$HOME/x11_n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" >"$HOME/x11_n"
if [ "$n" -eq 1 ]; then echo "Fatal server error: Server is already active for display 0"; exit 1; fi
exec -a app_process python3 -c '
import os, socket, time
d = os.environ["TMPDIR"] + "/.X11-unix"; os.makedirs(d, exist_ok=True)
s = socket.socket(socket.AF_UNIX); s.bind(d + "/X0"); s.listen(1); time.sleep(30)' com.termux.x11.CmdEntryPoint "$@"
EOF
  printf '#!/bin/sh\nexit 0\n' >"$PREFIX/bin/am"
  printf '#!/bin/sh\nexit 0\n' >"$PREFIX/bin/termux-x11-preference"
  chmod +x "$PREFIX/bin/termux-x11" "$PREFIX/bin/am" "$PREFIX/bin/termux-x11-preference"
  PATH="$PREFIX/bin:$PATH"
  TOD_X11_WAIT=2 TOD_X11_SETTLE=0
  conf_set INSTALLED 1
  session_start >/dev/null 2>&1
  x11_running
  has "errore del server nel log" "Server is already active for display 0" "$(cat "$TOD_LOG")"
  session_stop >/dev/null 2>&1
  fails x11_running
}

# --- lingua, uscita dal menu, disinstallazione ----------------------------------

_fake_xfconf() { # xfconf-query finto che conserva le proprietà (anche gli array) su file
  cat >"$PREFIX/bin/xfconf-query" <<'EOF'
#!/bin/bash
c="" p="" list=0 arr=0 reset=0; vals=()
while (($#)); do
  case $1 in
    -c) c=$2; shift ;; -p) p=$2; shift ;; -l) list=1 ;; -a) arr=1 ;; -r) reset=1 ;; -t) shift ;; -s) vals+=("$2"); shift ;;
  esac
  shift
done
d="$HOME/xfconf/$c"; mkdir -p "$d"
f="$d/$(printf '%s' "$p" | base64 -w0 | tr '/+' '_-')"
if ((list)); then for x in "$d"/*.p; do [[ -f $x ]] && cat "$x"; done | sort; exit 0; fi
if ((reset)); then
  for x in "$d"/*.p; do
    [[ -f $x ]] || continue
    case $(cat "$x") in "$p" | "$p"/*) rm -f "${x%.p}".* ;; esac
  done
  exit 0
fi
if ((${#vals[@]})); then
  printf '%s\n' "$p" >"$f.p"; printf '%s\n' "${vals[@]}" >"$f.v"
  if ((arr || ${#vals[@]} > 1)); then touch "$f.a"; else rm -f "$f.a"; fi
  exit 0
fi
[[ -f $f.v ]] || exit 1
if [[ -f $f.a ]]; then printf 'Value is an array with %s items:\n\n' "$(wc -l <"$f.v")"; fi
cat "$f.v"
EOF
  printf '#!/bin/sh\necho "$*" >>"$HOME/panel_calls"\n' >"$PREFIX/bin/xfce4-panel"
  chmod +x "$PREFIX/bin/xfconf-query" "$PREFIX/bin/xfce4-panel"
  PATH="$PREFIX/bin:$PATH"
}

test_pannelli_avvisano_la_barra() { # volume/notifiche cambiati: SIGUSR2 alla nostra barra, mai xfce4-panel (errore D-Bus)
  command -v python3 >/dev/null || return 0
  local d="$WORK/pav_$$"
  mkdir -p "$d/bin" "$d/finta"
  printf '#!/bin/sh\necho "$*" >>"%s/chiamate"\n' "$d" >"$d/bin/xfce4-panel"
  chmod +x "$d/bin/xfce4-panel"
  # una "barra" finta: si chiama tod-barra.py e scrive quando riceve SIGUSR2
  printf '%s\n' 'import signal, sys, time' \
    'signal.signal(signal.SIGUSR2, lambda *a: open(sys.argv[1] + "/segnale", "w").write("ok"))' \
    'open(sys.argv[1] + "/pronta", "w").write("ok")' 'time.sleep(20)' >"$d/finta/tod-barra.py"
  python3 "$d/finta/tod-barra.py" "$d" &
  local pid=$! i
  for i in $(seq 50); do if [[ -f $d/pronta ]]; then break; fi; sleep 0.1; done
  echo "$pid" >"$d/tod-barra.pid"
  # solo la funzione, senza caricare GTK: il file si legge e si esegue aggiorna_barra
  TMPDIR="$d" PATH="$d/bin:$PATH" python3 -c '
import os, re, signal, subprocess, sys
src = open(sys.argv[1]).read()
f = re.search(r"^def aggiorna_barra\(\):.*?(?=^\S)", src, re.S | re.M).group(0)
exec(f)
aggiorna_barra()' "$ROOT/share/tod-pannelli.py"
  for i in $(seq 30); do if [[ -f $d/segnale ]]; then break; fi; sleep 0.1; done
  kill "$pid" 2>/dev/null || true
  eq "la barra riceve il segnale" ok "$(cat "$d/segnale" 2>/dev/null)"
  eq "xfce4-panel mai chiamato" 0 "$(cat "$d/chiamate" 2>/dev/null | wc -l)"
}

test_pannelli_batteria_memoria() { # durata della batteria e memoria raggruppata per app (senza GTK)
  command -v python3 >/dev/null || return 0
  local out
  out=$(python3 - "$ROOT/share/tod-pannelli.py" <<'PYEOF'
import os, re, sys, tempfile
src = open(sys.argv[1]).read()
ITALIAN = True
def TT(it, en): return it
for nome in ("in_carica", "corrente_ua", "stima_ore", "durata", "in_proot", "raggruppa", "banda", "qualita"):
    exec(re.search(rf"^def {nome}\(.*?(?=^\S)", src, re.S | re.M).group(0))
# scarica: 2 645 000 µAh a -738 281 µA medi → 3,58 h
b = {"percentage": 52, "status": "DISCHARGING", "plugged": "UNPLUGGED", "current_average": -738281, "charge_counter": 2645000}
print("scarica", durata(stima_ore(b)))
# in carica, in mA e mAh: piena (100%) da 50% con 2000 mAh a 1000 mA → 2 h
print("carica", durata(stima_ore({"percentage": 50, "status": "CHARGING", "current_average": 1000, "charge_counter": 2000})))
print("senza corrente", stima_ore({"percentage": 50, "status": "DISCHARGING", "current_average": 0, "charge_counter": 1}))
class Info:
    def __init__(s, i, n): s.i, s.n = i, n
    def get_id(s): return s.i
    def get_display_name(s): return s.n
class Cat:
    def cerca(s, ist, cl): return Info("editor.desktop", "Editor") if cl == "Editor" else None
# 10 = finestra dell'editor (Electron); 11 e 12 suoi figli (renderer, terminale); 20 = X; 30 = finestra di Mousepad
procs = {10: (1, 300), 11: (10, 500), 12: (11, 200), 20: (1, 60), 30: (1, 25)}
app, sistema = raggruppa(procs, [(0x1, 10, "editor", "Editor"), (0x2, 10, "editor", "Editor"), (0x3, 30, "mousepad", "Mousepad")], Cat())
print("app", [(a["nome"], a["kb"], len(a["finestre"])) for a in app], "sistema", sistema)
# Mousepad aperto con proot, l'editor senza
app, _ = raggruppa(procs, [(0x1, 10, "editor", "Editor"), (0x3, 30, "mousepad", "Mousepad")], Cat(), lambda pid: pid == 30)
print("proot", sorted((a["nome"], a["proot"]) for a in app))
# /proc finto: 30 seguito da proot (40), 10 senza tracciatore, 50 seguito da un debugger
d = tempfile.mkdtemp()
for pid, tracer, argv0 in ((30, 40, None), (40, 0, b"/data/data/com.termux/files/usr/bin/proot"), (10, 0, None),
                           (50, 60, None), (60, 0, b"/usr/bin/gdb")):
    os.makedirs(f"{d}/{pid}")
    open(f"{d}/{pid}/status", "w").write(f"Name:\tx\nTracerPid:\t{tracer}\n")
    if argv0:
        open(f"{d}/{pid}/cmdline", "wb").write(argv0 + b"\0-r\0/rootfs\0")
print("in_proot", in_proot(30, d), in_proot(10, d), in_proot(50, d), in_proot(99, d))
print("banda", banda(2412), banda(5200), banda(6115))
print("qualita", qualita(-50)[1], qualita(-80)[1])
PYEOF
)
  has "durata in scarica" "scarica 3 h 35 min" "$out"
  has "durata in carica (mA e mAh)" "carica 2 h 0 min" "$out"
  has "senza corrente: nessuna stima" "senza corrente None" "$out"
  has "l'editor con i suoi processi, due finestre" "('Editor', 1000, 2)" "$out"
  has "Mousepad a parte" "('Mousepad', 25, 1)" "$out"
  has "il resto è il sistema" "sistema 60" "$out"
  has "segno proot solo per l'app in proot" "proot [('Editor', False), ('Mousepad', True)]" "$out"
  has "proot riconosciuto dal tracciatore" "in_proot True False False False" "$out"
  has "bande" "banda 2,4 GHz 5 GHz 6 GHz" "$out"
  has "segnale" "qualita excellent weak" "$out"
  local ps
  ps=$(cat "$ROOT/share/tod-phone-status.py")
  has "il clic apre i nostri pannelli" '"tod-pannello")' "$ps"
  has "clic sull'icona della batteria" '{PANNELLO} batteria' "$ps"
  has "clic sull'icona della rete" '{PANNELLO} rete' "$ps"
}

test_app_cambiate_una_volta() { # 230 app del telefono tolte insieme (S20+): Start e barra rifanno l'elenco una volta
  has "Start: i cambi passano dall'attesa" 'connect("changed", lambda *a: self._app_cambiate())' "$(cat "$ROOT/share/tod-start.py")"
  has "Start: attesa prima di ricostruire" "self._ricarica_dopo()" "$(sed -n '/def _app_cambiate/,/^$/p' "$ROOT/share/tod-start.py")"
  hasnt "Start: mai a ogni cambio" 'connect("changed", lambda *a: self.reload())' "$(cat "$ROOT/share/tod-start.py")"
  has "barra: attesa anche lei" "_ricarica_dopo())" "$(cat "$ROOT/share/tod-barra.py")"
  # sessione: barra, Start e pannelli prima di aspetto e cartelle
  local h
  h=$(sed -n '/^session_hook() {/,/^}/p' "$ROOT/lib/session.sh")
  eq "barra prima dell'aspetto" 1 "$(( $(grep -n look_barra_avvia <<<"$h" | cut -d: -f1) < $(grep -n look_session_apply <<<"$h" | cut -d: -f1) ))"
}

test_barra_nostra() { # la barra di DeX OS al posto di xfce4-panel: la sessione parte senza (niente attesa di 16 s)
  _fake_xfconf
  command -v python3 >/dev/null && python3 -m py_compile "$ROOT/share/tod-barra.py" || { echo "tod-barra.py non compila"; return 1; }
  look_barra_sessione
  eq "sessione: 4 programmi" 4 "$(xfconf-query -c xfce4-session -p /sessions/Failsafe/Count)"
  local tutti="" i
  for i in 0 1 2 3; do tutti+="$(xfconf-query -c xfce4-session -p /sessions/Failsafe/Client${i}_Command | tail -n +1 | tr '\n' ' ')"; done
  eq "niente xfce4-panel nella sessione" 0 "$(grep -c xfce4-panel <<<"$tutti")"
  has "la scrivania c'è" xfdesktop "$tutti"
  has "Thunar in sottofondo" "--daemon" "$tutti"
  # la prima volta la barra fissa le app installate, non legge più la barra di XFCE
  hasnt "niente docklike" "docklike" "$(cat "$ROOT/share/tod-barra.py")"
  has "app fissate predefinite" "PRIME = [" "$(cat "$ROOT/share/tod-barra.py")"
}

test_xfce_single_power_button() { # un solo Spegni: Start, Ctrl+Alt+Canc e il tasto di accensione
  _fake_xfconf
  local q=xfconf-query
  desktop_install_entries
  xfce_power_button
  has "nome italiano" "Name[it]=Spegni" "$(cat "$HOME/.local/share/applications/tod-stop.desktop")"
  eq "Ctrl+Alt+Canc" "$PREFIX/bin/tod spegni" "$($q -c xfce4-keyboard-shortcuts -p "/commands/custom/<Primary><Alt>Delete")"
  eq "tasto di accensione" "$PREFIX/bin/tod spegni" "$($q -c xfce4-keyboard-shortcuts -p /commands/custom/XF86PowerOff)"
  eq "xfce4-panel mai chiamato" 0 "$(cat "$HOME/panel_calls" 2>/dev/null | wc -l)"
  # la voce "Esci" di XFCE nascosta dal menu
  mkdir -p "$PREFIX/share/applications" && echo x >"$PREFIX/share/applications/xfce4-session-logout.desktop"
  desktop_tune_files
  has "Esci nascosto" "NoDisplay=true" "$(cat "$HOME/.local/share/applications/xfce4-session-logout.desktop")"
}

test_tod_help_question_mark() {
  local out
  out=$(bash "$ROOT/tod" "?" 2>&1) || true
  has "tod ? mostra l'aiuto" "tod" "$out"
  hasnt "niente errore" "$(t unknown_cmd "?")" "$out"
}

_fake_deb() { # _fake_deb file nome architettura [percorso dei file]
  local d="$WORK/deb_$$_$2_$3"
  mkdir -p "$d/DEBIAN" "$d${4:-/usr/share/doc/$2}"
  printf 'Package: %s\nVersion: 1.0\nArchitecture: %s\nMaintainer: test <t@t>\nDescription: App di prova %s\n' "$2" "$3" "$2" >"$d/DEBIAN/control"
  echo x >"$d${4:-/usr/share/doc/$2}/file"
  dpkg-deb --root-owner-group -b "$d" "$1" >/dev/null 2>&1 || dpkg-deb -b "$d" "$1" >/dev/null
}

test_deb_install_routing() {
  command -v dpkg-deb >/dev/null || { echo "  (dpkg-deb assente: salto)"; return 0; }
  mkdir -p "$HOME/Downloads" "$HOME/lavoro"
  _fake_deb "$HOME/Downloads/photocraft-0.7.0-linux-aarch64.deb" photocraft arm64
  _fake_deb "$HOME/lavoro/strumento_aarch64.deb" strumento aarch64
  _fake_deb "$HOME/lavoro/giocopc_amd64.deb" giocopc amd64
  _fake_deb "$HOME/lavoro/temi_all.deb" temi all /data/data/com.termux/files/usr/share/temi
  # i tentativi di Pietro dell'11 ottobre: nome nudo, ./nome, percorso completo
  eq "nome nudo: cercato nei Download" "$HOME/Downloads/photocraft-0.7.0-linux-aarch64.deb" "$(cd "$HOME" && deb_resolve photocraft-0.7.0-linux-aarch64.deb)"
  eq "percorso relativo" "$HOME/lavoro/strumento_aarch64.deb" "$(cd "$HOME/lavoro" && deb_resolve ./strumento_aarch64.deb)"
  fails deb_resolve inesistente.deb
  mkdir -p "$HOME/Downloads/App ArtCraft" && cp "$HOME/lavoro/temi_all.deb" "$HOME/Downloads/App ArtCraft/spostato.deb"
  eq "nome nudo: anche nelle sottocartelle dei Download" "$HOME/Downloads/App ArtCraft/spostato.deb" "$(cd / && deb_resolve spostato.deb)"
  eq "Debian → container" container "$(deb_target "$HOME/Downloads/photocraft-0.7.0-linux-aarch64.deb")"
  eq "Termux → termux" termux "$(deb_target "$HOME/lavoro/strumento_aarch64.deb")"
  eq "all con percorsi di Termux" termux "$(deb_target "$HOME/lavoro/temi_all.deb")"
  eq "per PC" non-arm:amd64 "$(deb_target "$HOME/lavoro/giocopc_amd64.deb")"
  pkg_install() { echo "pkg $*" >>"$HOME/inst_calls"; }
  cx_installed() { return 0; }
  cx_pkg_install() { echo "cx $* export=${CX_EXPORT_NAMES:-} file=$([[ -f $TMPDIR/${1#/tmp/} ]] && echo si)" >>"$HOME/inst_calls"; }
  (cd "$HOME/lavoro" && apps_add ./strumento_aarch64.deb) >/dev/null 2>&1
  (cd "$HOME" && apps_add photocraft-0.7.0-linux-aarch64.deb) >/dev/null 2>&1
  local c
  c=$(cat "$HOME/inst_calls")
  has "Termux: apt con il percorso assoluto" "pkg $HOME/lavoro/strumento_aarch64.deb" "$c"
  has "Debian: nel container dalla /tmp condivisa" "cx /tmp/tod-deb-" "$c"
  has "Debian: voci di menu del pacchetto giusto" "export=photocraft file=si" "$c"
  eq "file temporaneo rimosso" "" "$(find "$TMPDIR" -name 'tod-deb-*' | tr -d '\n')"
  fails apps_add "$HOME/lavoro/giocopc_amd64.deb"
  has "spiegazione per i pacchetti da PC" "amd64" "$( (apps_add "$HOME/lavoro/giocopc_amd64.deb") 2>&1)"
  # doppio clic: finestre zenity
  cat >"$PREFIX/bin/zenity" <<'EOF'
#!/bin/bash
echo "zenity $*" >>"$HOME/zenity_calls"
if [[ $* == *--progress* ]]; then cat >/dev/null; fi
EOF
  chmod +x "$PREFIX/bin/zenity"
  PATH="$PREFIX/bin:$PATH"
  # senza PyGObject: dove c'è (il CI su Ubuntu) deb_gui farebbe exec dell'installer grafico al posto del test
  python3() { if [[ $* == "-c import gi" ]]; then return 1; fi; command python3 "$@"; }
  : >"$HOME/inst_calls"
  DISPLAY=:0 deb_gui "$HOME/Downloads/photocraft-0.7.0-linux-aarch64.deb" >/dev/null 2>&1
  c=$(cat "$HOME/zenity_calls")
  has "conferma con nome e versione" "Installare <b>photocraft</b> 1.0?" "$c"
  has "dice dove va" "installato in <b>Debian</b>" "$c"
  has "barra d'attesa" "--progress --pulsate" "$c"
  has "esito" "photocraft è installato" "$c"
  has "installato davvero" "cx /tmp/tod-deb-" "$(cat "$HOME/inst_calls")"
  : >"$HOME/zenity_calls"
  DISPLAY=:0 deb_gui "$HOME/lavoro/giocopc_amd64.deb" >/dev/null 2>&1 || true
  has "pacchetto da PC: errore chiaro" "--error" "$(cat "$HOME/zenity_calls")"
  # associazione del doppio clic
  desktop_install_entries
  has "aperto con tod installa" "Exec=$PREFIX/bin/tod installa %f" "$(cat "$HOME/.local/share/applications/tod-deb.desktop")"
  has ".deb predefinito" "application/vnd.debian.binary-package=tod-deb.desktop" "$(cat "$HOME/.config/mimeapps.list")"
}

test_files_user_home() {
  _fake_rootfs
  mkdir -p "$R/home/dex"
  export TOD_TERMUX_HOME=$HOME
  FILES_PHONE="$WORK/phone_$$"
  mkdir -p "$FILES_PHONE"/{Documents,Download,Pictures,Music,Movies,DCIM/Camera} "$HOME/Progetti" "$HOME/Desktop" \
    "$HOME/.config/gtk-3.0" "$HOME/.local/share" "$HOME/storage"
  echo cfg >"$HOME/.config/prova"
  # com'era sul telefono di Pietro: collegamenti Telefono/Downloads e segnalibri vecchi
  ln -s "$FILES_PHONE" "$HOME/Telefono"
  ln -s "$FILES_PHONE/Download" "$HOME/Downloads"
  printf '%s\n' "file://$HOME/Telefono Telefono" "file://$HOME/Telefono/DCIM/Camera Fotocamera" "file://$R/home/dex Debian" \
    "file://$HOME/Downloads" "file://$HOME/Progetti Progetti" >"$HOME/.config/gtk-3.0/bookmarks"
  conf_set USER_NAME pietro
  files_setup
  local uh b="$HOME/.config/gtk-3.0/bookmarks"
  uh="$(dirname "$HOME")/pietro"
  eq "home dell'utente" "$uh" "$(files_user_home)"
  eq "Documenti = quelli del telefono" "$(readlink -f "$FILES_PHONE/Documents")" "$(readlink -f "$uh/Documenti")"
  eq "Video = Movies del telefono" "$(readlink -f "$FILES_PHONE/Movies")" "$(readlink -f "$uh/Video")"
  eq "Desktop = quello di Termux (lanciatori)" "$(readlink -f "$HOME/Desktop")" "$(readlink -f "$uh/Desktop")"
  eq "impostazioni condivise" cfg "$(cat "$uh/.config/prova")"
  eq "nella home solo le cartelle dell'utente" "Desktop Documenti Download Immagini Musica Video" "$(ls "$uh" | sort | tr '\n' ' ' | sed 's/ $//')"
  eq "collegamento Telefono tolto" no "$([[ -e $HOME/Telefono || -L $HOME/Telefono ]] && echo si || echo no)"
  has "cartelle XDG nella home dell'utente" "XDG_DOWNLOAD_DIR=\"$uh/Download\"" "$(cat "$HOME/.config/user-dirs.dirs")"
  eq "barra laterale: prima le cartelle dell'utente" "file://$uh/Documenti" "$(sed -n 1p "$b")"
  hasnt "niente Telefono" "Telefono" "$(cat "$b")"
  hasnt "niente Debian" "Debian" "$(cat "$b")"
  hasnt "niente Fotocamera" "Fotocamera" "$(cat "$b")"
  hasnt "niente doppione di Download" "file://$HOME/Downloads" "$(cat "$b")"
  has "segnalibro dell'utente conservato" "file://$HOME/Progetti Progetti" "$(cat "$b")"
  has "niente frecce sui collegamenti" "<svg" "$(cat "$HOME/.local/share/icons/hicolor/22x22/emblems/emblem-symbolic-link.svg")"
  has "niente X rosse" "<svg" "$(cat "$HOME/.local/share/icons/hicolor/22x22/emblems/emblem-unreadable.svg")"
  eq "niente più tema Fluent-tod (le icone DeX OS hanno gli emblemi vuoti)" no "$([[ -e $HOME/.local/share/icons/Fluent-tod ]] && echo si || echo no)"
  eq "Android/data nascosto nel telefono" Android "$(cat "$FILES_PHONE/.hidden")"
  local app
  app=$(dirname "$(dirname "$HOME")")
  eq "/data/data → solo la cartella di Termux" "${app##*/}" "$(ls "$PREFIX/share/tod-fs$(dirname "$app")")"
  has "nella cartella di Termux solo files" code_cache "$(cat "$app/.hidden")"
  has "sopra la home: la home tecnica di Termux nascosta" "${HOME##*/}" "$(cat "$(dirname "$HOME")/.hidden" 2>/dev/null)"
  local n
  n=$(wc -l <"$b")
  files_setup
  eq "seconda volta: niente doppioni" "$n" "$(wc -l <"$b")"
  # container: stesse cartelle
  mkdir -p "$R/home/dex/Documenti" "$R/home/dex/Musica"
  touch "$R/home/dex/Musica/mio.mp3"
  ln -s "$FILES_PHONE/Download" "$R/home/dex/Downloads"
  ln -s "$WORK" "$R/home/dex/storage"
  files_cx_user_dirs
  eq "doppione Downloads tolto" no "$([[ -L $R/home/dex/Downloads ]] && echo si || echo no)"
  eq "storage nascosto" storage "$(cat "$R/home/dex/.hidden")"
  eq "Documenti nel container: collegamento, non un disco montato" "$(readlink -f "$FILES_PHONE/Documents")" "$(readlink "$R/home/dex/Documenti")"
  eq "Desktop nel container" "$(readlink -f "$HOME/Desktop")" "$(readlink "$R/home/dex/Desktop")"
  eq "una cartella vera con file non si tocca" mio.mp3 "$(ls "$R/home/dex/Musica")"
  has "Salva con nome nel container" 'XDG_DOCUMENTS_DIR="$HOME/Documenti"' "$(cat "$R/home/dex/.config/user-dirs.dirs")"
  # la sessione gira come pietro
  (
    session_user_env
    eq "HOME della sessione" "$uh" "$HOME"
    eq "utente" pietro "$TOD_USER"
    eq "mappa delle cartelle di sistema" "$PREFIX/share/tod-fs" "$TOD_FS_MAP"
    eq "nome completo" Pietro "$TOD_USER_FULLNAME"
    eq "tod trova ancora i suoi file" "$(readlink -f "$TOD_TERMUX_HOME/.config/tod/tod.conf")" "$(readlink -f "$HOME/.config/tod/tod.conf")"
  )
  # Thunar come Esplora file
  _fake_xfconf
  files_session
  eq "niente barra dei menu" false "$(xfconf-query -c thunar -p /last-menubar-visible)"
  eq "barra del titolo uguale alle altre finestre" false "$(xfconf-query -c thunar -p /misc-use-csd)"
  eq "niente emblemi nella barra laterale" false "$(xfconf-query -c thunar -p /shortcuts-icon-emblems)"
  has "stile Esplora file" ".thunar .path-bar" "$(cat "$HOME/.config/gtk-3.0/gtk.css")"
  printf 'mio { color: red; }\n' >>"$HOME/.config/gtk-3.0/gtk.css"
  files_gtk_css
  eq "stile scritto una volta sola" 1 "$(grep -cF "Termux On DeX: Esplora file" "$HOME/.config/gtk-3.0/gtk.css")"
  has "le regole dell'utente restano" "mio { color: red; }" "$(cat "$HOME/.config/gtk-3.0/gtk.css")"
  # 9 novembre: col tema scuro la barra laterale restava bianca (#f9f9f9 fisso): solo colori del tema in uso
  local blocco
  blocco=$(sed -n '/Termux On DeX: Esplora file/,/<<< Termux On DeX/p' "$HOME/.config/gtk-3.0/gtk.css")
  has "barra laterale col colore del tema" "mix(@theme_bg_color, @theme_base_color, 0.6)" "$blocco"
  eq "nessun colore fisso nello stile di Thunar" 0 "$(grep -cE '#[0-9a-fA-F]{3,6}\b|rgba?\(' <<<"$blocco")"
  has "riscritto a ogni avvio della sessione" "files_gtk_css || true" "$(sed -n '/^session_hook()/,/^}/p' "$ROOT/lib/session.sh")"
  eq "niente barra di stato" false "$(xfconf-query -c thunar -p /last-statusbar-visible)"
  eq "i dischi USB collegati sulla scrivania, come macOS" true "$(xfconf-query -c xfce4-desktop -p /desktop-icons/file-icons/show-removable)"
  eq "anche i volumi di tipo device (tod-volumi)" true "$(xfconf-query -c xfce4-desktop -p /desktop-icons/file-icons/show-device-removable)"
  eq "niente file system sulla scrivania" false "$(xfconf-query -c xfce4-desktop -p /desktop-icons/file-icons/show-filesystem)"
  eq "niente Computer, File System, Rete" "computer:/// file:/// network:///" "$(xfconf_values thunar /hidden-bookmarks | tr '\n' ' ' | sed 's/ $//')"
  # nome non valido: niente home
  conf_set USER_NAME "Pietro Cav"
  fails files_user_home
  fails files_user_valid usr
}

test_i18n_follows_desktop_language() {
  getprop() { echo ""; }
  conf_set LINGUA it
  LANG=en_US.UTF-8 i18n_init
  eq "senza getprop: la lingua del desktop" it "$TOD_LANG"
  conf_set UI_LANG en
  i18n_init
  eq "UI_LANG vince" en "$TOD_LANG"
}

test_gettext_shim_program_domain() {
  local cc
  cc=$(command -v cc || command -v clang || true)
  if [[ -z $cc ]] || ! command -v python3 >/dev/null; then echo "  (compilatore o python3 assente: salto)"; return 0; fi
  local d="$WORK/shim_$$"
  # su Android il linker carica librerie solo sotto /data: lì, se c'è
  # (non usr/tmp: sotto proot diventa /tmp, che il linker rifiuta)
  if [[ -d /data/data/com.termux/cache && -w /data/data/com.termux/cache ]]; then d=$(mktemp -d /data/data/com.termux/cache/tod-shimtest-XXXXXX); fi
  mkdir -p "$d/loc/it/LC_MESSAGES"
  "$cc" -shared -fPIC -O2 -o "$d/shim.so" "$ROOT/share/tod-gettext.c" -ldl
  # catalogo .mo minimo per il dominio "provaprog" (formato GNU)
  python3 - "$d/loc/it/LC_MESSAGES/provaprog.mo" <<'EOF'
import struct, sys
msgs = {b"": b"Content-Type: text/plain; charset=UTF-8\nPlural-Forms: nplurals=2; plural=(n != 1);\n", b"_Edit": b"_Modifica"}
keys = sorted(msgs); ids = b""; strs = b""; o = []; t = []
for k in keys:
    o.append((len(k), len(ids))); ids += k + b"\0"
    t.append((len(msgs[k]), len(strs))); strs += msgs[k] + b"\0"
n = len(keys); start = 7 * 4 + 16 * n
out = struct.pack("Iiiiiii", 0x950412de, 0, n, 7 * 4, 7 * 4 + 8 * n, 0, 0)
out += b"".join(struct.pack("ii", l, start + p) for l, p in o)
out += b"".join(struct.pack("ii", l, start + len(ids) + p) for l, p in t)
open(sys.argv[1], "wb").write(out + ids + strs)
EOF
  cat >"$d/provaprog.c" <<'EOF'
#include <stdio.h>
char *dgettext(const char *, const char *);
char *textdomain(const char *);
int main(int argc, char **argv) {
  if (argc > 1) textdomain(argv[1]);
  printf("%s\n", dgettext(NULL, "_Edit"));
  return 0;
}
EOF
  # su Android dgettext non è in libc: si collega alla libreria stessa
  "$cc" -o "$d/provaprog" "$d/provaprog.c" 2>/dev/null ||
    "$cc" -o "$d/provaprog" "$d/provaprog.c" "$d/shim.so" -Wl,-rpath,"$d"
  local run=(env TOD_GETTEXT_LANG=it TOD_GETTEXT_DIR="$d/loc" LD_PRELOAD="$d/shim.so" "$d/provaprog")
  eq "senza textdomain: il dominio è il nome del programma (Thunar su Termux)" _Modifica "$("${run[@]}")"
  eq "textdomain esplicito vince" _Edit "$("${run[@]}" altrodominio)"
  eq "senza lingua: niente traduzione" _Edit "$(env TOD_GETTEXT_DIR="$d/loc" LD_PRELOAD="$d/shim.so" "$d/provaprog")"
  # GObject caricata dopo l'avvio (Python con gi): RTLD_NEXT dava NULL e g_object_add_weak_pointer non
  # registrava niente → puntatori appesi a widget liberati, barra e Start in crash (22 ottobre)
  cat >"$d/debole.c" <<'EOF'
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
int main(void) {
  void *h = dlopen("libgobject-2.0.so.0", RTLD_NOW | RTLD_LOCAL);
  if (!h) h = dlopen("libgobject-2.0.so", RTLD_NOW | RTLD_LOCAL);
  if (!h) { puts("salta"); return 0; }
  void *(*nuovo)(unsigned long, const char *, ...) = (void *(*)(unsigned long, const char *, ...))dlsym(h, "g_object_new");
  void (*unref)(void *) = (void (*)(void *))dlsym(h, "g_object_unref");
  /* GTK la chiama per nome: arriva a quella intercettata */
  void (*debole)(void *, void **) = (void (*)(void *, void **))dlsym(RTLD_DEFAULT, "g_object_add_weak_pointer");
  if (!debole) debole = (void (*)(void *, void **))dlsym(h, "g_object_add_weak_pointer");
  void *o = nuovo(80, NULL), *p = o;   /* 80 = G_TYPE_OBJECT */
  debole(o, &p);
  unref(o);
  puts(p ? "appeso" : "azzerato");
  return 0;
}
EOF
  if "$cc" -o "$d/debole" "$d/debole.c" -ldl 2>/dev/null; then
    local esito
    esito=$(LD_PRELOAD="$d/shim.so" "$d/debole")
    if [[ $esito != salta ]]; then eq "GObject caricata dopo: il puntatore debole si azzera" azzerato "$esito"; fi
  fi
  # nome utente (getpwuid/getpwnam): "pietro" invece di u0_a715
  cat >"$d/chi.c" <<'EOF'
#include <stdio.h>
#include <pwd.h>
#include <unistd.h>
int main(void) {
  struct passwd *p = getpwuid(getuid());
  struct passwd *q = getpwnam("pietro");
  printf("%s|%s\n", p ? p->pw_name : "?", q ? q->pw_name : "-");
  return 0;
}
EOF
  "$cc" -o "$d/chi" "$d/chi.c"
  eq "utente della sessione" "pietro|pietro" "$(env TOD_USER=pietro LD_PRELOAD="$d/shim.so" "$d/chi")"
  hasnt "fuori dalla sessione niente cambia" "pietro" "$(env LD_PRELOAD="$d/shim.so" "$d/chi")"
  # cartelle di sistema che Android non lascia leggere (/data/data): elencate dalla mappa
  cat >"$d/elenca.c" <<'EOF'
#include <dirent.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
  DIR *d = opendir(argv[1]);
  if (!d) { printf("errore\n"); return 0; }
  struct dirent *e;
  while ((e = readdir(d))) if (e->d_name[0] != '.') printf("%s ", e->d_name);
  printf("| %s\n", access(argv[1], R_OK) == 0 ? "leggibile" : "no");
  closedir(d);
  return 0;
}
EOF
  "$cc" -o "$d/elenca" "$d/elenca.c"
  mkdir -p "$d/chiusa/com.termux" "$d/chiusa/altra_app" "$d/mappa$d/chiusa/com.termux"
  chmod 000 "$d/chiusa"
  if ! ls "$d/chiusa" >/dev/null 2>&1; then
    eq "senza mappa: errore come prima" errore "$(env LD_PRELOAD="$d/shim.so" "$d/elenca" "$d/chiusa")"
    eq "con la mappa: solo la strada verso Termux" "com.termux | leggibile" \
      "$(env TOD_FS_MAP="$d/mappa" LD_PRELOAD="$d/shim.so" "$d/elenca" "$d/chiusa/")"
  fi
  # 9 novembre, nel Terminale: "cd /storage" e poi "ls" apre "." (cartella attraversabile ma non leggibile)
  chmod 111 "$d/chiusa"
  if ! ls "$d/chiusa" >/dev/null 2>&1; then
    eq "cd nella cartella e ls: la mappa vale anche per \".\"" "com.termux | leggibile" \
      "$(cd "$d/chiusa" && env TOD_FS_MAP="$d/mappa" LD_PRELOAD="$d/shim.so" "$d/elenca" .)"
    eq "anche un percorso relativo con .." "com.termux | leggibile" \
      "$(cd "$d/chiusa" && env TOD_FS_MAP="$d/mappa" LD_PRELOAD="$d/shim.so" "$d/elenca" ../chiusa)"
  fi
  chmod 755 "$d/chiusa"
  has "cartelle leggibili: quelle vere" altra_app "$(env TOD_FS_MAP="$d/mappa" LD_PRELOAD="$d/shim.so" "$d/elenca" "$d/chiusa")"
  rm -rf "$d"
}

test_look_windows11() { # DeX OS: tema, tasto Windows, registro delle notifiche, proporzioni; niente xfce4-panel
  pkg_install_avail() { echo "$*" >>"$HOME/pkg_calls"; }
  cx_installed() { return 1; }
  mkdir -p "$PREFIX/share/applications"
  desktop_install_entries
  look_apply_windows11 >/dev/null 2>&1
  has "Fluent GTK (la base di DeX OS)" "fluent-gtk-theme" "$(cat "$HOME/pkg_calls")"
  hasnt "niente icone Fluent né plugin della barra" "fluent-icon-theme" "$(cat "$HOME/pkg_calls")"
  hasnt "niente docklike" "docklike" "$(cat "$HOME/pkg_calls")"
  eq "nessuna configurazione della barra di XFCE" no "$([[ -e $HOME/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml || -e $HOME/.config/xfce4/panel ]] && echo si || echo no)"
  eq "comando tod-start" "$TOD_ROOT/share/tod-start" "$(readlink "$PREFIX/bin/tod-start")"
  eq "aspetto" windows11 "$(conf_get ASPETTO)"
  # nella sessione
  _fake_xfconf
  look_session_apply
  local q=xfconf-query
  eq "tema" Fluent-Light "$($q -c xsettings -p /Net/ThemeName)"
  eq "finestre" Fluent-Light "$($q -c xfwm4 -p /general/theme)"
  eq "pulsanti a destra" "O|HMC" "$($q -c xfwm4 -p /general/button_layout)"
  eq "tasto Windows" "$PREFIX/bin/tod-start" "$($q -c xfce4-keyboard-shortcuts -p /commands/custom/Super_L)"
  eq "registro delle notifiche acceso (per il pannello)" true "$($q -c xfce4-notifyd -p /notification-log)"
  eq "notifiche in basso a destra" bottom-right "$($q -c xfce4-notifyd -p /notify-location)"
  eq "xfce4-panel mai chiamato" 0 "$(cat "$HOME/panel_calls" 2>/dev/null | wc -l)"
  # proporzioni: tutto dallo stesso DPI
  look_scale_apply 108
  eq "icone del desktop in proporzione" 54 "$($q -c xfce4-desktop -p /desktop-icons/icon-size)"
  eq "puntatore in proporzione" 27 "$($q -c xsettings -p /Gtk/CursorThemeSize)"
  eq "senza Inter niente font Inter" 1 "$($q -c xsettings -p /Gtk/FontName >/dev/null 2>&1 && echo 0 || echo 1)"
  $q -c xsettings -p /Net/ThemeName -n -t string -s Altro
  look_session_apply
  eq "una volta sola" Altro "$($q -c xsettings -p /Net/ThemeName)"
  # XFCE di base non c'è più: solo DeX OS
  fails look_cli xfce
  fails look_cli v1
}

test_look_via_barra_xfce() { # aggiornando: via plugin della barra di XFCE, icone Fluent, copie vecchie
  local h=$HOME
  export TOD_TERMUX_HOME=$h
  mkdir -p "$h/.local/share/icons/Fluent-tod" "$h/.config/xfce4/panel" "$h/.config/xfce4.prima-di-windows11" \
    "$h/.local/share/icons/hicolor/scalable/apps" "$h/.config/xfce4/xfconf/xfce-perchannel-xml"
  echo x >"$h/.local/share/icons/hicolor/scalable/apps/tod-start.svg"
  echo x >"$h/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml"
  pkg_installed() { [[ $1 == xfce4-docklike-plugin || $1 == fluent-icon-theme ]]; }
  apt_run() { echo "$*" >>"$h/apt_calls"; }
  look_via_barra_xfce
  has "apt toglie solo quelli installati" "remove fluent-icon-theme xfce4-docklike-plugin" "$(cat "$h/apt_calls")"
  hasnt "e non gli altri" "whiskermenu" "$(cat "$h/apt_calls")"
  local f
  for f in .local/share/icons/Fluent-tod .config/xfce4/panel .config/xfce4.prima-di-windows11 \
    .local/share/icons/hicolor/scalable/apps/tod-start.svg .config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml; do
    eq "via $f" no "$([[ -e $h/$f ]] && echo si || echo no)"
  done
  # DeX OS: le versioni superate del disegno via, gli altri colori restano
  local t="$h/.local/share/themes" i="$h/.local/share/icons"
  mkdir -p "$t/DeX-OS-0067c0-v10" "$t/DeX-OS-0067c0-v11" "$t/DeX-OS-WM-1.333-v10" "$t/DeX-OS-WM-1.333-v11" "$t/DeX-OS-8a5cf5-v11" \
    "$i/DeX-OS-Icons-0067c0-v8" "$i/DeX-OS-Icons-0067c0-v9" "$i/DeX-OS-Icons-Dark-0067c0-v8" "$i/DeX-OS-Cursors-0067c0-v2"
  conf_set DEXOS_NAMES "DeX-OS-0067c0-v11 DeX-OS-Dark-0067c0-v11 DeX-OS-WM-1.333-v11 DeX-OS-WM-Dark-1.333-v11 DeX-OS-Icons-0067c0-v9 DeX-OS-Cursors-0067c0-v2"
  look_dexos_vecchi
  eq "temi e icone superati via, attuali e altri colori restano" \
    "DeX-OS-0067c0-v11 DeX-OS-8a5cf5-v11 DeX-OS-WM-1.333-v11 | DeX-OS-Cursors-0067c0-v2 DeX-OS-Icons-0067c0-v9" \
    "$(ls "$t" | tr '\n' ' ')| $(ls "$i" | grep DeX | tr '\n' ' ' | sed 's/ $//')"
}

test_cx_sync_look() {
  _fake_rootfs
  export TOD_TERMUX_HOME=$HOME
  mkdir -p "$PREFIX/share/themes/Fluent-Light/gtk-3.0" "$PREFIX/share/icons/Fluent" "$PREFIX/share/fonts/tod-inter" \
    "$HOME/.local/share/icons/Fluent-tod" "$HOME/.local/share/icons/hicolor" "$HOME/.local/share/icons/DeX-OS-Cursors-x-v2" "$R/usr/share/icons/Adwaita"
  cx_sync_look
  eq "tema Fluent nel container" "$PREFIX/share/themes/Fluent-Light" "$(readlink "$R/usr/share/themes/Fluent-Light")"
  eq "icone senza emblemi nel container" "$HOME/.local/share/icons/Fluent-tod" "$(readlink "$R/usr/share/icons/Fluent-tod")"
  eq "puntatore DeX OS" "$HOME/.local/share/icons/DeX-OS-Cursors-x-v2" "$(readlink "$R/usr/share/icons/DeX-OS-Cursors-x-v2")"
  eq "font Inter" "$PREFIX/share/fonts/tod-inter" "$(readlink "$R/usr/local/share/fonts/tod-inter")"
  eq "hicolor del container intatto" no "$([[ -L $R/usr/share/icons/hicolor ]] && echo si || echo no)"
}

test_shizuku() {
  command -v zip >/dev/null || { echo "  (zip assente: salto)"; return 0; }
  local d="$WORK/sz_$$"
  mkdir -p "$d/assets"
  printf '#!/system/bin/sh\n[ -z "$RISH_APPLICATION_ID" ] && export RISH_APPLICATION_ID="PKG"\n' >"$d/assets/rish"
  echo dex >"$d/assets/rish_shizuku.dex"
  (cd "$d" && zip -q -r base.apk assets)
  printf '#!/bin/sh\n[ "$1" = path ] && echo "package:%s/base.apk"\n' "$d" >"$PREFIX/bin/pm"
  chmod +x "$PREFIX/bin/pm"
  PATH="$PREFIX/bin:$PATH"
  shizuku_rish_install
  has "rish per Termux" 'RISH_APPLICATION_ID="com.termux"' "$(cat "$PREFIX/bin/rish")"
  eq "dex di sola lettura (Android 14+)" 400 "$(stat -c %a "$(shizuku_dir)/rish_shizuku.dex")"
  # Shizuku attivo: i comandi "da ADB" passano da rish
  printf '#!/bin/sh\n[ "$1" = -c ] && shift; case "$1" in "echo tod-ok") echo tod-ok ;; *) echo "rish:$1" ;; esac\n' >"$(shizuku_dir)/rish"
  SHIZUKU_OK=""
  shizuku_ready
  eq "comando via Shizuku" "rish:settings get global x " "$(priv_sh settings get global x)"
  eq "stato" "$(t sz_ok)" "$(shizuku_status)"
  # Shizuku spento: si torna ad ADB
  printf '#!/bin/sh\nexit 1\n' >"$(shizuku_dir)/rish"
  printf '#!/bin/sh\necho "adb:$*"\n' >"$PREFIX/bin/adb"
  chmod +x "$PREFIX/bin/adb"
  SHIZUKU_OK="" ADB_SERIAL=127.0.0.1:5555
  eq "senza Shizuku: ADB" "adb:-s 127.0.0.1:5555 shell settings get global x" "$(priv_sh settings get global x)"
}

test_start_unblocks_usr1() { # Android blocca SIGUSR1 e i figli lo ereditano: il menu deve sbloccarlo
  has "sblocco di SIGUSR1" "pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGUSR1})" "$(cat "$ROOT/share/tod-start.py")"
  command -v python3 >/dev/null || return 0
  eq "con il segnale bloccato dal padre, arriva lo stesso" ricevuto "$(timeout 10 python3 -c '
import signal, subprocess, sys, time
signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})   # come fa Android: il figlio lo eredita
child = "import signal,sys\nsignal.pthread_sigmask(signal.SIG_UNBLOCK,{signal.SIGUSR1})\n" \
        "signal.signal(signal.SIGUSR1, lambda *a: (print(\"ricevuto\"), sys.exit(0)))\nsignal.pause()"
p = subprocess.Popen([sys.executable, "-c", child])
time.sleep(0.5); p.send_signal(signal.SIGUSR1); p.wait(5)
')"
}

test_start_a_sinistra() { # 10 novembre, Pietro: "se sposto il bottone a sinistra si apre sempre al centro"
  command -v python3 >/dev/null || return 0
  local out
  mkdir -p "$WORK/sx_$$"
  out=$(python3 - "$ROOT/share/tod-start.py" "$WORK/sx_$$" <<'PY'
import ast, json, os, sys
src = open(sys.argv[1]).read()
cls = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ClassDef) and any(
    isinstance(f, ast.FunctionDef) and f.name == "a_sinistra" for f in n.body))
ns = {"json": json, "os": os, "CONF": sys.argv[2], "px": lambda n: n}
for f in cls.body:
    if isinstance(f, ast.FunctionDef) and f.name in ("a_sinistra", "x_menu"):
        exec(compile(ast.Module(body=[f], type_ignores=[]), "tod-start.py", "exec"), ns)
class WA:
    x, width = 100, 1000
class S:
    pass
s = S()
def x(allineamento):
    if allineamento is not None:
        json.dump({"allineamento": allineamento}, open(os.path.join(sys.argv[2], "barra.json"), "w"))
    s.sinistra = ns["a_sinistra"](s)
    return ns["x_menu"](s, WA, 400)
print(x(None), x("sinistra"), x("centro"))
PY
  )
  eq "senza barra.json al centro, barra a sinistra a sinistra, al centro al centro" "400 112 400" "$out"
  has "si sceglie a ogni apertura" "self.sinistra = self.a_sinistra()" "$(sed -n '/    def place(self):/,/^    def /p' "$ROOT/share/tod-start.py")"
  has "anche quando la finestra cambia misura" "x, y = self.x_menu(wa, alloc.width)" "$(cat "$ROOT/share/tod-start.py")"
}

test_start_wrapper() {
  local w="$ROOT/share/tod-start" log="$WORK/start_$$"
  mkdir -p "$TMPDIR" "$WORK/fakepy_$$"
  printf '#!/bin/sh\necho "python3 $*" >>"%s"\n' "$log" >"$WORK/fakepy_$$/python3"
  chmod +x "$WORK/fakepy_$$/python3"
  # nessun menu in memoria: si avvia e si mostra
  PATH="$WORK/fakepy_$$:$PATH" sh "$w"
  has "senza menu: avvio con --show" "tod-start.py --show" "$(cat "$log")"
  # menu vivo ma bloccato (non risponde a SIGUSR1): ucciso e riavviato
  bash -c 'trap "" USR1; sleep 20; :' tod-start.py &   # nel cmdline come il menu vero
  local hung=$!
  echo "$hung" >"$TMPDIR/tod-start.pid"
  : >"$log"
  PATH="$WORK/fakepy_$$:$PATH" sh "$w" 2>/dev/null
  sleep 0.2
  eq "menu bloccato: ucciso" no "$(kill -0 "$hung" 2>/dev/null && echo si || echo no)"
  has "e riavviato" "tod-start.py --show" "$(cat "$log")"
  # dopo un riavvio il vecchio pid può essere di un altro processo: non è il menu, non si tocca
  sleep 20 &
  local other=$!
  echo "$other" >"$TMPDIR/tod-start.pid"
  : >"$log"
  PATH="$WORK/fakepy_$$:$PATH" sh "$w" --preload 2>/dev/null
  has "pid di un altro processo: menu precaricato lo stesso" "tod-start.py" "$(cat "$log")"
  eq "e l'altro processo resta vivo" si "$(kill -0 "$other" 2>/dev/null && echo si || echo no)"
  kill "$other" 2>/dev/null || true
}

test_android_apps() {
  command -v python3 >/dev/null || { echo "  (python3 assente: salto)"; return 0; }
  local out
  out=$(python3 - "$ROOT/share/tod-android-apps.py" <<'EOF'
import importlib.util, sys
sp = importlib.util.spec_from_file_location("a", sys.argv[1]); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
print(round(m.inset_fraction("(type 0x6)0x147ae30"), 3))
tree = """N: android=http://schemas.android.com/apk/res/android
  E: vector (line=1)
    A: http://schemas.android.com/apk/res/android:viewportWidth(0x01010402)=(type 0x4)0x42d80000
    A: http://schemas.android.com/apk/res/android:viewportHeight(0x01010403)=108.0
    E: path (line=2)
      A: http://schemas.android.com/apk/res/android:fillColor(0x010101a5)=#ff25d366
      A: http://schemas.android.com/apk/res/android:pathData(0x01010405)="M0,0h108v108h-108z" (Raw: "M0,0h108v108h-108z")
"""
class F:   # APK finto: nessuna risorsa
    def resolve(self, r): return None
w, h, body = m.vector_svg(F(), m.xml_nodes(tree))
print(h, body)
print(m.FRAMEWORK_COLORS["0x0106000b"][0])
EOF
)
  has "frazione di inset di Android" "0.01" "$out"
  has "vettore → SVG con il colore" '<path d="M0,0h108v108h-108z" fill="#25d366"' "$out"
  has "colore di Android (bianco)" "#ffffff" "$out"
  # sincronizzazione: mai senza gli strumenti, mai se spenta
  android_apps_ready() { return 1; }
  android_apps_sync
  conf_set ANDROID_APPS 0
  android_apps_ready() { return 0; }
  android_apps_sync
  eq "spenta: niente scansione" no "$([[ -e $TOD_STATE_DIR/android-apps.log ]] && echo si || echo no)"
}

test_phone_status() {
  command -v python3 >/dev/null || { echo "  (python3 assente: salto)"; return 0; }
  mkdir -p "$TMPDIR"
  local ps="$ROOT/share/tod-phone-status.py"
  printf '%s' '{"battery":{"percentage":51,"status":"DISCHARGING","plugged":"UNPLUGGED","temperature":35.0},"wifi":{"supplicant_state":"COMPLETED","rssi":-60,"ssid":"\"Casa\"","ip":"192.168.1.17"}}' >"$TMPDIR/tod-phone.json"
  has "batteria al 50%" "<icon>battery-level-50-symbolic</icon>" "$(LANGUAGE=it python3 "$ps" batteria)"
  has "percentuale nel suggerimento" "51%" "$(LANGUAGE=it python3 "$ps" batteria)"
  has "Wi-Fi con il nome della rete" "Wi-Fi: Casa" "$(LANGUAGE=it python3 "$ps" rete)"
  has "segnale buono" "network-wireless-signal-good-symbolic" "$(LANGUAGE=it python3 "$ps" rete)"
  printf '%s' '{"battery":{"percentage":100,"status":"CHARGING","plugged":"AC"},"wifi":{"error":"Please grant the following permission to use this command: android.permission.ACCESS_FINE_LOCATION"}}' >"$TMPDIR/tod-phone.json"
  has "in carica" "battery-level-100-charged-symbolic" "$(LANGUAGE=it python3 "$ps" batteria)"
  has "manca il permesso Posizione" "permesso Posizione" "$(LANGUAGE=it python3 "$ps" rete)"
  # nella nostra barra: rete, volume, batteria, notifiche, orologio, in quest'ordine
  has "ordine delle icone di stato" '"rete"' "$(grep -A2 'VOCI = \[' "$ROOT/share/tod-barra.py")"
  # notifiche: solo le nuove, niente Termux/sistema, niente doppioni; cambia il testo = nuova
  local py
  py=$(
    cd "$ROOT/share" && python3 -c '
import importlib.util as u
s = u.spec_from_file_location("ps", "tod-phone-status.py"); m = u.module_from_spec(s); s.loader.exec_module(m)
seen = {}
a = [{"key": "k1", "packageName": "com.whatsapp", "title": "Anna", "content": "ciao"},
     {"key": "k2", "packageName": "com.termux", "title": "Termux", "content": "1 session"}]
m.fresh(a, seen)
b = a + [{"key": "k3", "packageName": "com.google.android.gm", "title": "Posta", "content": "x"},
         {"key": "k4", "packageName": "com.google.android.gm", "title": "Posta", "content": "x"}]
b[0] = dict(b[0], content="ciao, ci sei?")
print(",".join(n["key"] for n in m.fresh(b, seen)))
print(",".join(n["key"] for n in m.fresh(b[1:], seen)), len(seen))
'
  )
  eq "solo le notifiche nuove" "k1,k3
 3" "$py"
}

test_session_old_daemons() { # "Esci" e subito l'icona (3 novembre): i demoni delle accensioni di prima restavano
  # vivi (tre del telefono, tre tod-open-host, tre ponti): l'hook li chiude, per argomenti esatti
  local p="$HOME/procfinto" out
  mkdir -p "$p"/{101,102,103,104,105,106}
  printf '%s\0' python3 "$TOD_ROOT/share/tod-phone-status.py" demone >"$p/101/cmdline"
  printf '%s\0' bash "$TOD_ROOT/share/tod-open-host" >"$p/102/cmdline"
  printf '%s\0' python3 /altrove/share/tod-phone-status.py demone >"$p/103/cmdline"      # un'altra copia: no
  printf '%s\0' nano "$TOD_ROOT/share/tod-open-host" x >"$p/104/cmdline"                 # un testo uguale: no
  printf '%s\0' python3 "$TOD_ROOT/share/tod-updates.py" controlla --ciclo >"$p/105/cmdline"
  printf '%s\0' bash "$TOD_ROOT/tools/termux-bridge" >"$p/106/cmdline"
  out=$(kill() { echo "$2"; }; TOD_PROC=$p session_old_daemons | tr '\n' ' ')
  eq "chiusi solo i demoni di tod" "101 102 105 106 " "$out"
  eq "nei test, senza /proc finto, nessun processo vero" "" "$(kill() { echo "$2"; }; session_old_daemons)"
  has "l'hook li chiude per primi" "session_old_daemons" "$(sed -n '/^session_hook() {/,/^}/p' "$ROOT/lib/session.sh" | sed -n 2,3p)"
  # il demone del telefono, da sé: tutti gli altri demoni, non solo quello del file pid
  mkdir -p "$p/201" "$p/202"
  printf '%s\0' python3 "$TOD_ROOT/share/tod-phone-status.py" demone >"$p/201/cmdline"
  printf '%s\0' python3 "$TOD_ROOT/share/tod-phone-status.py" batteria >"$p/202/cmdline"
  out=$(cd "$ROOT/share" && python3 -c '
import importlib.util as u, sys
s = u.spec_from_file_location("ps", "tod-phone-status.py"); m = u.module_from_spec(s); s.loader.exec_module(m)
print(m.altri_demoni(sys.argv[1]))' "$p")
  eq "demoni del telefono da chiudere" "[101, 103, 201]" "$out"
}

test_session_shell() { # sessione avviata da Termux in sottofondo (icona della Termux:X11): niente SHELL, e il
  # terminale del desktop mostrava il benvenuto di Termux (login). La sessione ha sempre bash
  eq "senza SHELL: bash" "$PREFIX/bin/bash" "$(unset SHELL; session_user_env; echo "$SHELL")"
  eq "SHELL=login di Termux: bash" "$PREFIX/bin/bash" "$(SHELL=$PREFIX/bin/login; session_user_env; echo "$SHELL")"
  eq "una shell scelta resta" "/x/zsh" "$(SHELL=/x/zsh; session_user_env; echo "$SHELL")"
}

test_termux_exit() { # Spegni chiude anche Termux (4 novembre), "Esci" e tod restart no
  local b="$HOME/binam" out
  mkdir -p "$b"
  printf '#!/bin/sh\necho "am $*" >>"%s/am.log"\n' "$b" >"$b/am"
  chmod +x "$b/am"
  PATH="$b:$PATH" termux_exit
  eq "Termux:X11 e poi Termux" "am broadcast --user 0 -a com.termux.x11.ACTION_STOP -p com.termux.x11
am startservice --user 0 -n com.termux/com.termux.app.TermuxService -a com.termux.service_stop" "$(cat "$b/am.log")"
  rm -f "$b/am.log"
  conf_set SPEGNI_TERMUX 0
  PATH="$b:$PATH" termux_exit
  eq "SPEGNI_TERMUX=0: Termux resta aperto" "" "$(cat "$b/am.log" 2>/dev/null)"
  conf_set SPEGNI_TERMUX 1
  has "tod spegni passa da _spegni" '"$PREFIX/bin/tod" _spegni' "$(cat "$ROOT/tod")"
  has "_spegni: prima il desktop, poi Termux" "_spegni) session_stop; termux_exit ;;" "$(cat "$ROOT/tod")"
  hasnt "tod stop non chiude Termux" "termux_exit" "$(sed -n '/^session_stop() {/,/^}/p' "$ROOT/lib/session.sh")"
}

test_lingua_code_and_env() {
  eq "dalla lingua di Android" it "$(lingua_code)"
  conf_set LINGUA en
  eq "scelta dell'utente" en "$(lingua_code)"
  conf_set LINGUA it
  fails lingua_active
  # 1° novembre (S20+): passo di traduzione fallito → anche Start, barra e Impostazioni in inglese
  eq "senza traduzione di XFCE: la lingua per le nostre finestre, niente modulo" "it||" \
    "$(unset LANGUAGE TOD_GETTEXT_LANG LD_PRELOAD; lingua_env; echo "${LANGUAGE:-}|${TOD_GETTEXT_LANG:-}|${LD_PRELOAD:-}")"
  eq "in inglese: niente" "|" "$(unset LANGUAGE TOD_GETTEXT_LANG; LINGUA=x; conf_set LINGUA en; lingua_env; echo "${LANGUAGE:-}|${TOD_GETTEXT_LANG:-}"; conf_set LINGUA it)"
  conf_set LINGUA it
  touch "$LINGUA_SO"
  mkdir -p "$LINGUA_DIR/it/LC_MESSAGES"
  fails lingua_active
  touch "$LINGUA_DIR/it/LC_MESSAGES/xfce4-panel.mo"
  lingua_active
  LD_PRELOAD=/termux-exec.so lingua_env
  eq "libreria davanti a termux-exec" "$LINGUA_SO:/termux-exec.so" "$LD_PRELOAD"
  eq "lingua" it "$TOD_GETTEXT_LANG"
  eq "LANGUAGE per i menu di glib" it "$LANGUAGE"
  lingua_env
  eq "non aggiunta due volte" "$LINGUA_SO:/termux-exec.so" "$LD_PRELOAD"
  mkdir -p "$WORK/mo_$$" && touch "$WORK/mo_$$/a.mo" "$WORK/mo_$$/gimp30.mo"
  eq "cataloghi installati" 2 "$(lingua_install_mo it "$WORK/mo_$$" | tail -n 1)"
  # i plug-in di GIMP vogliono la cartella compilata $PREFIX/share/locale (26 ottobre)
  eq "share/locale porta alle nostre traduzioni" tod-locale "$(readlink "$PREFIX/share/locale")"
  # cataloghi delle app solo se l'app c'è
  have() { [[ $1 == gimp ]]; }
  eq "solo GIMP installato" "gimp-data libgimp-3.0-0" "$(lingua_app_pkgs | tr '\n' ' ' | sed 's/ $//')"
  have() { command -v "$1" >/dev/null 2>&1; }
}

test_tod_gettext_compila_su_termux_nuovo() { # 1° novembre: spawn.h (solo con libandroid-spawn) → modulo non compilato
  # tod-gettext.c si compila sul telefono di chi installa: solo intestazioni di ndk-sysroot, che arriva con clang
  local ok=" assert.h ctype.h dirent.h dlfcn.h errno.h fcntl.h inttypes.h limits.h locale.h math.h poll.h pthread.h
    pwd.h signal.h stdarg.h stdbool.h stddef.h stdint.h stdio.h stdlib.h string.h strings.h sys/mman.h sys/socket.h
    sys/stat.h sys/syscall.h sys/time.h sys/types.h sys/un.h sys/wait.h termios.h time.h unistd.h wchar.h " h fuori=""
  ok=$(tr -s ' \n' ' ' <<<"$ok")
  for h in $(grep -o '^#include <[^>]*>' "$ROOT/share/tod-gettext.c" | sed 's/#include <//; s/>//'); do
    [[ $ok == *" $h "* ]] || fuori+="$h "
  done
  eq "intestazioni di un Termux nuovo (verificare con dpkg -S che vengano da ndk-sysroot)" "" "$fuori"
}

test_start_menu_e_icone_app_nuove() { # 1° novembre (S20+): app appena installata senza icona, Start bloccato
  local f="$ROOT/share/tod-start.py"
  # il menu del tasto destro sulla finestra, non sulla tessera che una ricostruzione distrugge
  has "menu agganciato alla finestra" "menu.attach_to_widget(self, None)" "$(sed -n '/def on_app_button/,/return True/p' "$f")"
  hasnt "mai alla tessera" "attach_to_widget(widget" "$(cat "$f")"
  has "niente ricostruzione sotto un menu aperto" "self._ricarica_rimandata = True" "$(sed -n '/def _ricarica_ora/,/return False/p' "$f")"
  has "si rifà alla chiusura del menu" "_ricarica_dopo(300)" "$(sed -n '/def _menu_chiuso/,/^$/p' "$f")"
  # icone arrivate dopo la voce: si ricercano, e intanto quella generica
  has "icone ricercate a ogni ricostruzione" "rescan_if_needed()" "$(sed -n '/def reload/,/seen = set/p' "$f")"
  has "e a ogni apertura" "rescan_if_needed()" "$(sed -n '/def show_menu/,/home_apps_check/p' "$f")"
  has "senza icona: quella generica" '"application-x-executable"' "$(sed -n '/^def app_icon/,/return img/p' "$f")"
  has "e si riprova per un po'" "self._giri_icone < 6" "$(cat "$f")"
}

test_lingua_ripara() { # la traduzione mancante si installa da sola all'avvio del desktop, una volta al giorno
  conf_set LINGUA it
  lingua_setup() { echo chiamata >>"$HOME/ripara"; touch "$LINGUA_SO"; mkdir -p "$LINGUA_DIR/it/LC_MESSAGES"
    touch "$LINGUA_DIR/it/LC_MESSAGES/thunar.mo"; }
  printf '#!/bin/sh\necho "$@" >>"$HOME/notifiche"\n' >"$PREFIX/bin/notify-send"
  chmod +x "$PREFIX/bin/notify-send"
  PATH="$PREFIX/bin:$PATH"
  has "tod doctor: mancante, e cosa succederà" "traduzione di XFCE mancante" "$(lingua_doctor)"
  lingua_ripara
  wait
  eq "installata in sottofondo" chiamata "$(cat "$HOME/ripara")"
  has "avviso quando è pronta" "Traduzione pronta" "$(cat "$HOME/notifiche")"
  eq "tod doctor: pronta" "it, desktop tradotto" "$(lingua_doctor)"
  rm -f "$LINGUA_SO" "$HOME/ripara"
  lingua_ripara
  wait
  eq "fallita oggi: si riprova domani, non a ogni avvio" "" "$(cat "$HOME/ripara" 2>/dev/null)"
  echo "2000-01-01 $(cksum <"$TOD_ROOT/share/tod-gettext.c" | cut -d' ' -f1)" >"$TOD_STATE_DIR/lingua-tentativo"
  lingua_ripara
  wait
  eq "un altro giorno: si riprova" chiamata "$(cat "$HOME/ripara")"
  rm -f "$LINGUA_SO" "$HOME/ripara"
  echo "$(date +%F) 12345" >"$TOD_STATE_DIR/lingua-tentativo"
  lingua_ripara
  wait
  eq "stesso giorno ma modulo cambiato (aggiornamento): si riprova subito" chiamata "$(cat "$HOME/ripara")"
  conf_set LINGUA en
  rm -f "$HOME/ripara" "$TOD_STATE_DIR/lingua-tentativo"
  lingua_ripara
  wait
  eq "in inglese: niente da installare" "" "$(cat "$HOME/ripara" 2>/dev/null)"
  has "chiamata dall'hook della sessione" "lingua_ripara" "$(sed -n '/^session_hook() {/,/^}/p' "$ROOT/lib/session.sh")"
}

test_x11_glamor_solo_adreno() { # 1° novembre: sul S20+ (Mali) la Termux:X11 con glamor si chiudeva appena aperta
  conf_set X11_GLAMOR 1
  local GPU_FAMILY=adreno
  x11_glamor_ok || { echo "  ✘ Adreno: glamor acceso"; return 1; }
  GPU_FAMILY=mali
  fails x11_glamor_ok
  GPU_FAMILY=xclipse
  fails x11_glamor_ok
  GPU_FAMILY=adreno
  conf_set X11_GLAMOR_NO 1
  fails x11_glamor_ok
  conf_set X11_GLAMOR_NO 0
  conf_set X11_GLAMOR 0
  fails x11_glamor_ok
}

test_session_start_glamor_ripiego() { # Termux:X11 con glamor che si chiude subito: si riparte senza, per sempre
  command -v python3 >/dev/null || { echo "  (python3 assente: salto)"; return 0; }
  cat >"$PREFIX/bin/termux-x11" <<'EOF'
#!/bin/bash
echo "${TERMUX_X11_GLAMOR:-?}" >>"$HOME/x11_glamor"
exec -a app_process python3 -c '
import os, socket, time
d = os.environ["TMPDIR"] + "/.X11-unix"; os.makedirs(d, exist_ok=True)
p = d + "/X0"
if os.path.exists(p): os.unlink(p)
s = socket.socket(socket.AF_UNIX); s.bind(p); s.listen(1)
time.sleep(1 if os.environ.get("TERMUX_X11_GLAMOR") == "1" else 30)' com.termux.x11.CmdEntryPoint "$@"
EOF
  printf '#!/bin/sh\nexit 0\n' >"$PREFIX/bin/am"
  printf '#!/bin/sh\nexit 0\n' >"$PREFIX/bin/termux-x11-preference"
  chmod +x "$PREFIX/bin/termux-x11" "$PREFIX/bin/am" "$PREFIX/bin/termux-x11-preference"
  PATH="$PREFIX/bin:$PATH"
  TOD_X11_WAIT=3 TOD_X11_SETTLE=0 TOD_X11_VIVO=2
  local GPU_FAMILY=adreno
  conf_set INSTALLED 1
  conf_set X11_GLAMOR 1
  session_start >/dev/null 2>&1
  x11_running || { echo "  ✘ il desktop deve partire senza glamor"; return 1; }
  eq "prima con glamor, poi senza" "1 0" "$(tr '\n' ' ' <"$HOME/x11_glamor" | sed 's/ $//')"
  eq "ricordato per questo telefono" 1 "$(conf_get X11_GLAMOR_NO)"
  has "nel registro il perché" "si è chiusa appena aperta" "$(cat "$TOD_LOG")"
  session_stop >/dev/null 2>&1
  fails x11_running
  : >"$HOME/x11_glamor"
  session_start >/dev/null 2>&1
  eq "la volta dopo: subito senza glamor" "0" "$(cat "$HOME/x11_glamor")"
  session_stop >/dev/null 2>&1
}

test_xstartup_server_nuovo_niente_arresto() { # la sessione del server morto non spegne quello ripartito
  printf '#!/bin/sh\nexit 0\n' >"$PREFIX/bin/dbus-launch"
  printf '#!/bin/sh\necho "$@" >"$HOME/tod_called"\n' >"$PREFIX/bin/tod"
  chmod +x "$PREFIX/bin/dbus-launch" "$PREFIX/bin/tod"
  PATH="$PREFIX/bin:$PATH"
  x11_server_pids() { if [[ -f $HOME/srv_n ]]; then echo 222; else touch "$HOME/srv_n"; echo 111; fi; }
  session_xstartup >/dev/null 2>&1
  sleep 1
  eq "server nuovo: niente arresto" "" "$(cat "$HOME/tod_called" 2>/dev/null)"
  has "nel registro" "il server X è un altro" "$(cat "$TOD_LOG")"
}

test_logout_stops_everything() {
  # "Esci" dal menu: quando la sessione XFCE finisce, parte "tod stop" staccato
  printf '#!/bin/sh\nexit 0\n' >"$PREFIX/bin/dbus-launch"
  printf '#!/bin/sh\necho "$@" >"$HOME/tod_called"\n' >"$PREFIX/bin/tod"
  chmod +x "$PREFIX/bin/dbus-launch" "$PREFIX/bin/tod"
  PATH="$PREFIX/bin:$PATH"
  session_xstartup >/dev/null 2>&1
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do [[ -f $HOME/tod_called ]] && break; sleep 0.2; done
  eq "arresto completo dopo l'uscita" stop "$(cat "$HOME/tod_called" 2>/dev/null)"
}

test_phantom_restore_defaults() {
  cat >"$PREFIX/bin/adb" <<'EOF'
#!/bin/bash
echo "$*" >>"$HOME/adb_calls"
case "$*" in
  *"settings get global settings_enable_monitor_phantom_procs") printf 'null\r\n' ;;
  *"device_config get activity_manager max_phantom_processes") printf 'null\r\n' ;;
esac
exit 0
EOF
  chmod +x "$PREFIX/bin/adb"
  PATH="$PREFIX/bin:$PATH"
  ADB_SERIAL=127.0.0.1:37999
  phantom_unapply
  phantom_verify_default
  has "monitor rimesso come di fabbrica" "settings delete global settings_enable_monitor_phantom_procs" "$(cat "$HOME/adb_calls")"
  has "limite dei processi rimesso" "device_config delete activity_manager max_phantom_processes" "$(cat "$HOME/adb_calls")"
  has "ottimizzazione batteria rimessa" "deviceidle whitelist -com.termux" "$(cat "$HOME/adb_calls")"
}

test_uninstall_package_list_and_safety() {
  mkdir -p "$PREFIX/var/log"
  cat >"$PREFIX/var/log/dpkg.log" <<'EOF'
2026-10-11 18:20:00 install unzip:aarch64 <none> 6.0-9
2026-10-11 18:34:47 install x11-repo:all <none> 8.4-1
2026-10-11 18:35:00 upgrade bash:aarch64 5.2-1 5.3-1
2026-10-11 18:39:00 install xfce4:all <none> 4.20
2026-10-11 18:40:00 install rimosso:aarch64 <none> 1.0
EOF
  pkg_installed() { [[ $1 != rimosso ]]; }
  eq "da Termux On DeX in poi" $'x11-repo\nxfce4' "$(tod_installed_packages '2026-10-11 18:34:33')"
  eq "tutti (Termux come nuovo)" $'unzip\nx11-repo\nxfce4' "$(tod_installed_packages '')"
  # prova generale che toccherebbe un pacchetto vitale: la rimozione si blocca
  apt-mark() { :; }
  apt-get() { if [[ $1 == -s ]]; then printf 'Purg xfce4 [4.20]\nPurg bash [5.3-1]\n'; fi; }
  apt_run() { echo "NON DOVEVA ARRIVARE QUI" >"$HOME/removed"; }
  conf_set UNINSTALL_SINCE '2026-10-11 18:34:33'
  fails uninstall_packages
  eq "niente rimosso" no "$([[ -e $HOME/removed ]] && echo si || echo no)"
}

test_uninstall_packages_success() {
  mkdir -p "$PREFIX/var/log"
  printf '%s\n' "2026-10-11 18:34:47 install x11-repo:all <none> 8.4-1" \
    "2026-10-11 18:39:00 install xfce4:all <none> 4.20" >"$PREFIX/var/log/dpkg.log"
  pkg_installed() { return 0; }
  apt-mark() { echo "apt-mark $*" >>"$HOME/calls"; }
  apt-get() { if [[ $1 == -s ]]; then printf 'Purg xfce4 [4.20]\nPurg x11-repo [8.4-1]\n'; fi; }
  apt_run() { echo "apt $*" >>"$HOME/calls"; }
  conf_set UNINSTALL_SINCE '2026-10-11 18:34:33'
  uninstall_packages
  has "segnati come automatici" "apt-mark auto x11-repo xfce4" "$(cat "$HOME/calls")"
  has "tolti solo quelli che non servono ad altro" "autoremove --purge" "$(cat "$HOME/calls")"
}

_fake_pulse() { # PulseAudio finto con moduli veri (stato su file)
  local st="$HOME/pulse_state"
  mkdir -p "$st"
  printf '0\tmodule-aaudio-sink\t\n1\tmodule-suspend-on-idle\t\n' >"$st/modules"
  cat >"$PREFIX/bin/pulseaudio" <<'EOF'
#!/bin/bash
st="$HOME/pulse_state"
echo "pulseaudio $* XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-}" >>"$st/calls"
case $1 in
  --check) [[ -f $st/running && ${XDG_RUNTIME_DIR:-} == "$TMPDIR" ]] ;;
  --start)
    if [[ -f $st/fail_start ]]; then rm -f "$st/fail_start"; echo "E: [pulseaudio] main.c: Daemon startup failed." >&2; exit 1; fi
    touch "$st/running"; rm -f "$st/broken"; printf '0\tmodule-native-protocol-unix\t\n' >"$st/modules" ;;
  --kill) rm -f "$st/running" ;;
esac
EOF
  cat >"$PREFIX/bin/pactl" <<'EOF'
#!/bin/bash
st="$HOME/pulse_state"
echo "pactl $* PULSE_SERVER=${PULSE_SERVER:-}" >>"$st/calls"
what="$1"; [[ $1 == list && ${2:-} == short ]] && what="list ${3:-}"
case $what in
  info) [[ -f $st/running ]] ;;
  "list modules") cat "$st/modules" ;;
  "list sinks")
    if grep -q module-aaudio-sink "$st/modules"; then printf '0\tAAudio_sink\tmodule-aaudio-sink.c\n'; fi
    if grep -q module-sles-sink "$st/modules"; then printf '1\tOpenSL_ES_sink\tmodule-sles-sink.c\n'; fi ;;
  "list sink-inputs") : ;;
  load-module)
    # demone "rotto" (visto su Android 17): le uscite verso Android vengono rifiutate
    if [[ -f $st/broken && $2 =~ ^module-(aaudio|sles)-sink$ ]]; then echo "Failure: Module initialization failed" >&2; exit 1; fi
    n=$(( $(wc -l <"$st/modules") + 5 )); printf '%s\t%s\t\n' "$n" "$2" >>"$st/modules"; echo "$n" ;;
  unload-module) awk -v id="$2" '$1 != id' "$st/modules" >"$st/m2" && mv "$st/m2" "$st/modules" ;;
  set-default-sink) echo "$2" >"$st/default" ;;
esac
EOF
  chmod +x "$PREFIX/bin/pulseaudio" "$PREFIX/bin/pactl"
  PATH="$PREFIX/bin:$PATH"
}

test_audio_start_and_ensure() {
  _fake_pulse
  local st="$HOME/pulse_state"
  audio_start
  has "demone nella cartella della sessione" "pulseaudio --start --exit-idle-time=-1 XDG_RUNTIME_DIR=$TMPDIR" "$(cat "$st/calls")"
  has "modulo TCP per il container" "module-native-protocol-tcp" "$(cat "$st/modules")"
  hasnt "niente sospensione dell'uscita" "module-suspend-on-idle" "$(cat "$st/modules")"
  eq "uscita predefinita" AAudio_sink "$(cat "$st/default")"
  has "pactl parla al socket del desktop" "PULSE_SERVER=unix:$TMPDIR/pulse/native" "$(cat "$st/calls")"
  # scelta di OpenSL ES
  conf_set AUDIO_SINK sles
  audio_ensure
  hasnt "AAudio tolto" "module-aaudio-sink" "$(cat "$st/modules")"
  has "OpenSL ES caricato" "module-sles-sink" "$(cat "$st/modules")"
  eq "uscita OpenSL ES" OpenSL_ES_sink "$(cat "$st/default")"
  # nessuna uscita: si prova prima AAudio
  conf_set AUDIO_SINK auto
  printf '0\tmodule-native-protocol-tcp\t\n' >"$st/modules"
  audio_ensure
  has "uscita creata" "module-aaudio-sink" "$(cat "$st/modules")"
  # il TCP non viene caricato due volte
  eq "un solo modulo TCP" 1 "$(grep -c module-native-protocol-tcp "$st/modules")"
}

test_session_uses_audio_socket() {
  _fake_pulse
  printf '#!/bin/sh\necho "$PULSE_SERVER" >"$HOME/pulse_env"\n' >"$PREFIX/bin/dbus-launch"
  printf '#!/bin/sh\nexit 0\n' >"$PREFIX/bin/tod"
  chmod +x "$PREFIX/bin/dbus-launch" "$PREFIX/bin/tod"
  session_xstartup >/dev/null 2>&1
  eq "le app del desktop usano il socket giusto" "unix:$TMPDIR/pulse/native" "$(cat "$HOME/pulse_env")"
}

# --- CLI end-to-end (processo separato, come sul telefono) ----------------------
test_cli_smoke() {
  local out rc=0
  out=$(bash "$ROOT/tod" version)
  has "version" "Termux On DeX" "$out"
  has "help" "install" "$(bash "$ROOT/tod" help)"
  bash "$ROOT/tod" comando-inesistente >/dev/null 2>&1 || rc=$?
  eq "comando ignoto → 2" 2 "$rc"
  has "status" "Desktop" "$(bash "$ROOT/tod" status)"
  has "install --help" "--no-container" "$(bash "$ROOT/tod" install --help)"
  bash "$ROOT/tod" tweak vsync off >/dev/null
  has "tweak scrive il profilo" "vblank_mode=0" "$(cat "$HOME/.config/tod/gpu.env")"
  has "doctor" "GPU" "$(bash "$ROOT/tod" doctor 2>&1)"
  has "gpu status" "stack=official" "$(bash "$ROOT/tod" gpu status)"
  has "x11 status" "Termux:X11" "$(bash "$ROOT/tod" x11 status)"
  hasnt "niente giochi nella guida" "game add" "$(bash "$ROOT/tod" help)"
  has "guida: aspetto e lingua" "aspetto [tema|accento|puntatore]" "$(bash "$ROOT/tod" help)"
  rc=0
  bash "$ROOT/tod" gpu use profilo-finto >/dev/null 2>&1 || rc=$?
  eq "profilo inesistente rifiutato" 1 "$rc"
  rc=0
  bash "$ROOT/tod" start >/dev/null 2>&1 || rc=$?
  eq "start senza installazione" 1 "$rc"
}

test_xsync_wait() { # xfwm4: conferma del ridisegno data da tod, al massimo ~25 cambi di misura al secondo
  local cc
  cc=$(command -v clang || command -v gcc) || { echo "  (nessun compilatore: salto)"; return 0; }
  "$cc" -shared -fPIC -O2 -Wall -Werror -o "$WORK/tx_$$.so" "$ROOT/share/tod-gettext.c" -ldl
  eq "intercetta la richiesta di ridisegno" 1 "$(grep -ac "XSendEvent" "$WORK/tx_$$.so" | sed 's/^[1-9][0-9]*$/1/')"
  has "solo dentro xfwm4" 'strcmp(n, "xfwm4")' "$(cat "$ROOT/share/tod-gettext.c")"
  has "conferma con il contatore della finestra" "XSyncSetCounter" "$(cat "$ROOT/share/tod-gettext.c")"
  has "conferma quando l'app ha disegnato davvero (DAMAGE)" "XDamageCreate" "$(cat "$ROOT/share/tod-gettext.c")"
  has "mai sopra una conferma vera dell'app" "if (cur < a->value)" "$(cat "$ROOT/share/tod-gettext.c")"
  has "si può spegnere" "TOD_XSYNC_CLASSICO" "$(cat "$ROOT/share/tod-gettext.c")"
}

test_resize_edges() { # cornice da 1 px elegante, ma si ridimensiona senza mirare (bordi invisibili)
  python3 -c "import ast; ast.parse(open('$ROOT/share/tod-edges.py').read())"
  has "avviato dalla sessione" "share/tod-edges.py" "$(cat "$ROOT/lib/session.sh")"
  has "zone che non disegnano" "InputOnly" "$(cat "$ROOT/share/tod-edges.py")"
  has "il ridimensionamento lo fa xfwm4" "_NET_WM_MOVERESIZE" "$(cat "$ROOT/share/tod-edges.py")"
  has "mai sopra la barra delle app" "_NET_WORKAREA" "$(cat "$ROOT/share/tod-edges.py")"
  has "xfwm4 accetta la presa del mouse (ora aggiornata)" "_TOD_EDGES_TIME" "$(cat "$ROOT/share/tod-edges.py")"
  has "bordo da una riga" 'save(f"left-{sfx}", B, S(8)' "$(cat "$ROOT/share/dexos/dexos-theme.py")"
  has "angoli in basso larghi (sennò l'arrotondamento in alto sparisce)" "cw = R + S(10)" "$(cat "$ROOT/share/dexos/dexos-theme.py")"
  has "niente striscia in alto" "frame_border_top -n -t int -s 0" "$(cat "$ROOT/lib/look.sh")"
}

test_avvisi_macos() { # 10 novembre, Pietro: "niente grafica linux, i popup di sistema in stile macOS"
  local src cc f n m
  src=$(cat "$ROOT/share/tod-gettext.c")
  # quando: prima che la finestrella compaia, una volta sola, solo con il tema DeX OS
  has "domande ed errori (gtk_dialog_run)" "else tod_avviso(dialog);   /* le domande e gli errori" "$src"
  has "anche le finestre mostrate senza run (copia in corso, password)" "void gtk_window_present(void *w) {" "$src"
  has "e con gtk_widget_show" "void gtk_widget_show(void *w) {" "$src"
  has "una volta sola, mai su una finestra già sullo schermo" "if (f_g_object_get_data(w, TOD_AVVISO_FATTO) || f_gtk_widget_get_realized(w)) return 0;" "$src"
  has "solo con il tema DeX OS (è lui che disegna la classe tod-avviso)" 'int si = tema && !strncmp(tema, "DeX-OS", 6);' "$src"
  has "si può spegnere" "TOD_AVVISI_CLASSICI" "$src"
  has "rinomina e Nuova cartella restano come sono" '!tod_find_type(corpo, "GtkEntry")) forma = 2;' "$src"
  # forma: niente barra di Linux, icona dell'app in alto, testi centrati, pulsanti a pillola
  has "cornice di GTK (angoli tondi, ombra) al posto della barra di xfwm4" "f_gtk_window_set_titlebar(win, testa);" "$src"
  has "sempre al centro della finestra da cui viene, o dello schermo" "f_gtk_window_set_position(win, madre ? 4 /* CENTER_ON_PARENT */ : 1 /* CENTER */);" "$src"
  has "anche i dialoghi nati \"al centro della madre\" senza madre" "!(pos == 4 /* CENTER_ON_PARENT */ && !madre)" "$src"
  has "si sposta trascinandola da un punto qualsiasi" "f_gtk_window_begin_move_drag(win, 1, (int)x, (int)y" "$src"
  has "l'icona dell'app prima di quella della finestra" "prova[n++] = f_gtk_window_get_default_icon_name();" "$src"
  has "triangolo sull'icona per avvertimenti ed errori" '(tipo == 1 || tipo == 3) && strncmp(nome, "dialog-", 7)' "$src"
  has "testi a capo anche nei percorsi lunghi" "2 /* PANGO_WRAP_WORD_CHAR */" "$src"
  has "due pulsanti affiancati, di più uno sotto l'altro" "int colonna = n > 2;" "$src"
  has "senza predefinito: quello a destra, in colonna in cima" 'if (!scelto) tod_classe(ordine[n - 1], "tod-predefinito");' "$src"
  has "le caselle con il nome dentro" "f_gtk_entry_set_placeholder_text(in[1]->w, buf);" "$src"
  has "un file in una scheda" 'tod_classe(scheda, "tod-scheda");' "$src"
  has "copia in corso: anche i lavori aggiunti dopo" '"add", (void *)tod_avanzamento_aggiunta' "$src"
  has "password tradotta (gnome-keyring di Termux è senza traduzioni)" '{"gnome-keyring", "gcr", NULL}' "$src"
  has "GTK caricata da Python: la si cerca nelle librerie già caricate" 'if (!f_##name) f_##name = (ret (*)(__VA_ARGS__))tod_next(#name);' "$src"
  # le nostre app Python: ogni Gtk.MessageDialog passa da come_macos (GI non passa da LD_PRELOAD)
  for f in "$ROOT"/share/*.py "$ROOT"/share/*/*.py; do
    n=$(grep -c 'Gtk\.MessageDialog(' "$f" || true)
    if ((n == 0)); then continue; fi
    m=$(grep 'tod_avviso\.come_macos(' "$f" | grep -vc 'libero=True' || true)   # le libere sono le Gtk.Dialog nostre
    eq "avvisi come macOS in ${f#"$ROOT"/}" "$n" "$m"
  done
  # le finestrelle fatte da noi: contenuto disposto come macOS (testa), cornice e pulsanti da tod_avviso_libero
  has "finestrelle nostre: cornice, pulsanti e margini" "int tod_avviso_libero(void *w) {" "$src"
  eq "Dischi: formatta, rinomina, lavoro in corso" 3 "$(grep -c 'tod_avviso.come_macos(dl, libero=True)' "$ROOT/share/tod-dischi.py")"
  eq "Rete: la password" 1 "$(grep -c 'tod_avviso.come_macos(d, libero=True)' "$ROOT/share/rete/tod-rete.py")"
  eq "Scanner: aggiungi per indirizzo" 1 "$(grep -c 'tod_avviso.come_macos(d, libero=True)' "$ROOT/share/stampa/tod-scanner.py")"
  has "Rete: il nome dentro le caselle (come macOS)" 'placeholder_text=etichetta)' "$(cat "$ROOT/share/rete/tod-rete.py")"
  if command -v python3 >/dev/null; then
    eq "fuori dal desktop (modulo non caricato) non fa niente" True \
      "$(LD_PRELOAD= python3 -c "import sys; sys.path.insert(0, '$ROOT/share'); import tod_avviso; o = object(); print(tod_avviso.come_macos(o) is o)")"
  fi
  cc=$(command -v clang || command -v gcc) || { echo "  (nessun compilatore: salto la compilazione)"; return 0; }
  "$cc" -shared -fPIC -O2 -Wall -Wextra -Wno-unused-parameter -Werror -o "$WORK/ta_$$.so" "$ROOT/share/tod-gettext.c" -ldl
  eq "la funzione per le app Python c'è" 1 "$(grep -ac tod_avviso "$WORK/ta_$$.so" | sed 's/^[1-9][0-9]*$/1/')"
}

test_avvisi_macos_gtk() { # gli avvisi come macOS con GTK vera, su uno schermo virtuale (Xvfb e PyGObject di Termux)
  local T=/data/data/com.termux/files/usr d cc py out x pid
  py=$T/bin/python3
  if [[ ! -x $T/bin/Xvfb || ! -x $py ]] || ! "$py" -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null; then
    echo "  (Xvfb o PyGObject di Termux assenti: salto)"; return 0
  fi
  cc=$(command -v clang || command -v gcc) || { echo "  (nessun compilatore: salto)"; return 0; }
  # il linker di Android carica librerie solo sotto /data (come test_gettext_shim_program_domain)
  [[ -d /data/data/com.termux/cache && -w /data/data/com.termux/cache ]] || { echo "  (niente cartella sotto /data: salto)"; return 0; }
  d=$(mktemp -d /data/data/com.termux/cache/tod-avvisi-XXXXXX)
  "$cc" -shared -fPIC -O2 -o "$d/ta.so" "$ROOT/share/tod-gettext.c" -ldl
  x=$((150 + RANDOM % 100))
  while [[ -e $T/tmp/.X11-unix/X$x ]]; do x=$((x + 1)); done
  "$T/bin/Xvfb" ":$x" -screen 0 800x600x24 -nolisten tcp >/dev/null 2>&1 &
  pid=$!
  for _ in $(seq 50); do [[ -S $T/tmp/.X11-unix/X$x ]] && break; sleep 0.1; done
  cat >"$d/prova.py" <<'PYEOF'
import ctypes, sys
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk
f = ctypes.CDLL(sys.argv[1]).tod_avviso
f.argtypes, f.restype = [ctypes.c_void_p], ctypes.c_int
cap = ctypes.pythonapi.PyCapsule_GetPointer
cap.restype, cap.argtypes = ctypes.c_void_p, [ctypes.py_object, ctypes.c_char_p]
def avviso(w): return f(cap(w.__gpointer__, None))
def classi(w): return " ".join(sorted(c for c in w.get_style_context().list_classes() if c.startswith("tod-") or c == "default"))
def etichette(box): return [b.get_label() for b in box.get_children()]
Gtk.Settings.get_default().set_property("gtk-theme-name", "DeX-OS-0067c0-v13")
d = Gtk.MessageDialog(message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.NONE, text='Eliminare per sempre "a.txt"?')
d.format_secondary_text("Il file non si potrà recuperare.")
for i, n in enumerate(("Annulla", "Salta", "Elimina")):
    d.add_button(n, i + 1)
d.set_default_response(3)
print("messaggio", avviso(d), classi(d))
print("barra", classi(d.get_titlebar()))
k = d.get_message_area().get_children()
print("icona in alto", type(k[0]).__name__, classi(k[0]))
print("testi", classi(k[1]), "|", classi(k[2]), "| selezionabile", k[1].get_selectable())
print("virgolette", k[1].get_label())
aa = d.get_action_area()
print("colonna", aa.get_orientation() == Gtk.Orientation.VERTICAL, etichette(aa))
print("predefinito", classi(aa.get_children()[0]))
print("di nuovo", avviso(d))
print("posto", repr(d.get_property("window-position").value_nick))
d2 = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.NONE, text="Disinstallare?")
d2.add_button("Annulla", 1)
d2.add_button("Disinstalla", 2)
avviso(d2)
print("affiancati", d2.get_action_area().get_orientation() == Gtk.Orientation.HORIZONTAL, etichette(d2.get_action_area()))
r = Gtk.Dialog()   # come la rinomina: una casella → non si tocca
r.get_content_area().add(Gtk.Entry())
r.add_button("Rinomina", 1)
print("rinomina", avviso(r), r.get_titlebar())
s = Gtk.Dialog()   # come "Sostituire il file?" di Thunar: griglia con l'icona in alto a sinistra
g = Gtk.Grid()
g.attach(Gtk.Image.new_from_icon_name("dialog-question", Gtk.IconSize.BUTTON), 0, 0, 1, 1)
g.attach(Gtk.Label(label="Questa cartella contiene già un file."), 1, 0, 2, 1)
g.attach(Gtk.Image.new_from_icon_name("text-x-generic", Gtk.IconSize.DIALOG), 1, 2, 1, 1)
g.attach(Gtk.Label(label="Dimensione: 8 byte"), 2, 2, 1, 1)
g.attach(Gtk.CheckButton(label="Applica a tutti"), 0, 5, 3, 1)
s.get_content_area().add(g)
for i, n in enumerate(("Annulla", "Salta", "Sostituisci")):
    s.add_button(n, i + 1)
s.get_content_area().show_all()
print("sostituisci", avviso(s), type(s.get_content_area().get_children()[0]).__name__)
print("scheda", sum("tod-scheda" in classi(w) for w in g.get_children()))
print("principale", etichette(s.get_action_area())[0], classi(s.get_action_area().get_children()[0]))
sys.path.insert(0, sys.argv[2])
import tod_avviso
lib = ctypes.CDLL(sys.argv[1]).tod_avviso_libero
lib.argtypes, lib.restype = [ctypes.c_void_p], ctypes.c_int
p = Gtk.Dialog()   # una finestrella nostra (come la password della Rete)
p.add_button("Annulla", 1)
p.add_button("Connetti", 2).get_style_context().add_class("suggested-action")
t = tod_avviso.testa("network-server", "Connetti a «PC»", "Nome utente e password.")
p.get_content_area().add(t)
print("libera", lib(cap(p.__gpointer__, None)), classi(p), classi(p.get_titlebar()))
print("testa", [classi(w) for w in t.get_children()], t.get_size_request()[0] >= 268)
print("libera pulsanti", etichette(p.get_action_area()), classi(p.get_action_area().get_children()[1]))
Gtk.Settings.get_default().set_property("gtk-theme-name", "Adwaita")
a = Gtk.MessageDialog(text="Altro tema")
print("altro tema", avviso(a), a.get_titlebar())
PYEOF
  mkdir -p "$d/loc"   # lingua italiana senza cataloghi: solo per le virgolette
  out=$(env -u LD_PRELOAD DISPLAY=":$x" HOME="$HOME" TOD_GETTEXT_LANG=it TOD_GETTEXT_DIR="$d/loc" \
    timeout 60 "$py" "$d/prova.py" "$d/ta.so" "$ROOT/share" 2>&1) || true
  # zenity delle app di Debian → servizio del desktop → avviso vero (che si chiude da solo: --timeout → uscita 5)
  local av zout
  # (senza timeout: è un programma di Debian e il modulo di Termux nel suo LD_PRELOAD lo fermerebbe)
  env DISPLAY=":$x" HOME="$HOME" TOD_AVVISI_SOCK="$d/av.sock" LD_PRELOAD="$d/ta.so" \
    "$py" "$ROOT/share/tod-avvisi-host.py" >"$d/av.log" 2>&1 &
  av=$!
  for _ in $(seq 80); do [[ -S $d/av.sock ]] && break; sleep 0.1; done
  zout=$(TOD_AVVISI_SOCK="$d/av.sock" TOD_ZENITY_VERO=/bin/false timeout 30 "$py" "$ROOT/share/tod-zenity" \
    --warning --title Prova --text "Avviso di prova" --timeout 1; echo "uscita=$?")
  kill "$av" 2>/dev/null || true
  out+=$'\n'"zenity $zout"$'\n'"$(cat "$d/av.log")"
  kill "$pid" 2>/dev/null || true
  rm -rf "$d"
  has "messaggio trasformato" "messaggio 1 tod-avviso tod-avviso-messaggio" "$out"
  has "niente barra di xfwm4: quella di GTK, vuota" "barra tod-avviso-testa" "$out"
  has "icona dell'app in alto" "icona in alto Overlay tod-avviso-icona" "$out"
  has "titolo e testo, non selezionabili (si trascina la finestra)" "testi tod-avviso-titolo | tod-avviso-testo | selezionabile False" "$out"
  has "tre pulsanti: in colonna, il predefinito in cima" "colonna True ['Elimina', 'Salta', 'Annulla']" "$out"
  has "il predefinito resta tale (accento nel tema)" "predefinito default" "$out"
  has "una volta sola" "di nuovo 0" "$out"
  has "senza finestra madre sullo schermo: al centro dello schermo" "posto 'center'" "$out"
  has "virgolette diritte di Thunar → caporali, come il resto del desktop" "virgolette Eliminare per sempre «a.txt»?" "$out"
  has "due pulsanti affiancati, nell'ordine di prima" "affiancati True ['Annulla', 'Disinstalla']" "$out"
  has "la rinomina non si tocca" "rinomina 0 None" "$out"
  has "Sostituire il file?: l'icona in alto" "sostituisci 1 Overlay" "$out"
  has "il file in una scheda" "scheda 1" "$out"
  has "senza predefinito: quello a destra, in cima e nell'accento" "principale Sostituisci tod-predefinito" "$out"
  has "finestrella nostra: cornice e classi" "libera 1 tod-avviso tod-avviso-finestrella tod-avviso-testa" "$out"
  has "testa(): icona, titolo, testo, larga come un avviso" "testa ['tod-avviso-icona', 'tod-avviso-titolo', 'tod-avviso-testo'] True" "$out"
  has "finestrella nostra: pulsanti affiancati, il principale nell'accento" "libera pulsanti ['Annulla', 'Connetti']" "$out"
  has "con un altro tema niente" "altro tema 0 None" "$out"
  has "zenity delle app di Debian: l'avviso lo mostra il desktop, la scadenza torna all'app" "zenity uscita=5" "$out"
  has "e ha la forma di macOS" "tod-avviso: GtkMessageDialog come macOS (forma 1)" "$out"
}

test_avvisi_macos_tema() { # tema DeX OS: avvisi come macOS, chiaro e scuro, solo l'accento; ritocchi a notifiche e suggerimenti
  command -v python3 >/dev/null || return 0
  local out
  out=$(python3 - "$ROOT/share/dexos/dexos-theme.py" "$WORK/notifiche_$$" <<'PYEOF'
import importlib.util as u, os, sys
s = u.spec_from_file_location("dx", sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)
print("CHIARO", m.avvisi_css("light", "#8a5cf5"))
print("SCURO", m.avvisi_css("dark", "#8a5cf5"))
m.notify(sys.argv[2], "light", "#8a5cf5")
print("NOTIFICHE", open(os.path.join(sys.argv[2], "gtk.css")).read())
print("MENU", m.vetro_css("light", "#8a5cf5"))
PYEOF
)
  local chiaro scuro
  chiaro=$(sed -n '/^CHIARO/,/^SCURO/p' <<<"$out")
  scuro=$(sed -n '/^SCURO/,/^NOTIFICHE/p' <<<"$out")
  has "fondo pieno del pannello (chiaro)" "background-color: #f2f2f5; background-image: none; color: #1d1d1f; border-radius: 16px;" "$chiaro"
  has "fondo pieno del pannello (scuro)" "background-color: #242428; background-image: none; color: #f5f5f7; border-radius: 16px;" "$scuro"
  has "schede bianche (chiaro)" ".tod-scheda { background-color: #ffffff; border-radius: 12px;" "$chiaro"
  has "schede (scuro)" ".tod-scheda { background-color: #2e2e33; border-radius: 12px;" "$scuro"
  has "pulsanti a pillola" "border-radius: 999px; border: none; background-image: none;" "$chiaro"
  has "il predefinito nell'accento del sistema" "background-color: #8a5cf5; color: #ffffff;" "$chiaro"
  has "anche quello scelto da tod-gettext.c" "button.tod-predefinito {" "$chiaro"
  has "più forte dei pulsanti piatti di Fluent" ".tod-avviso.csd .tod-avviso-pulsanti button:first-child" "$chiaro"
  has "testo secondario" "color: #6e6e73;" "$chiaro"
  has "copia in corso: pulsanti tondi" ".tod-avanzamento-vista button.tod-tondo {" "$chiaro"
  has "copia in corso: icona visibile anche nel tema scuro" ".tod-avanzamento-vista image { -gtk-icon-style: symbolic; color: #f5f5f7; }" "$scuro"
  eq "mai un blu fisso" 0 "$(grep -ci '#0067c0\|#005fb8\|#0a84ff\|#1a73e8' <<<"$chiaro$scuro")"
  has "nel tema generato" "f.write(avvisi_css(mode, accent))" "$(cat "$ROOT/share/dexos/dexos-theme.py")"
  has "notifiche: angoli dei banner di macOS" "border-radius: 18px;" "$out"
  has "notifiche: al passaggio appena più scure, niente bordo colorato" "#XfceNotifyWindow:hover { background-color: #f6f6f8; }" "$out"
  has "suggerimenti più compatti" "tooltip.csd decoration { border-radius: 7px;" "$out"
  has "voci di menu" "padding: 4px 10px; min-height: 18px; border-radius: 6px;" "$out"
  # 11 novembre: Fluent spostava di 6 px (con una riga sopra) l'ultimo riquadro della griglia: la seconda scheda di
  # "Sostituire il file?" non era allineata alla prima. La nostra regola deve battere la sua (più classi)
  has "schede tutte allineate, anche l'ultima della griglia" \
    "dialog.background.tod-avviso.csd > box.dialog-vbox.vertical > grid.horizontal > box.horizontal.tod-scheda:last-child {
  margin: 0; border: none;" "$chiaro"
  has "anche il testo dentro l'ultima scheda (Fluent gli dava 8 px in più)" \
    "box.horizontal.tod-scheda:last-child > label {
  margin: 0;" "$chiaro"
  has "testo d'errore delle finestrelle nostre" "label.tod-avviso-errore { font-size: 9pt; color: #c42b1c; }" "$chiaro"
  has "ripiego senza il modulo (app del container): pulsanti a pillola" "messagedialog:not(.tod-avviso) .dialog-action-area button," "$chiaro"
  # un collegamento rotto tra i temi vecchi non deve fermare il generatore (11 novembre, nella cartella di prova)
  local temi="$WORK/temi_$$"
  mkdir -p "$temi"
  eq "temi già fatti, collegamento rotto accanto: nessun errore" ok "$(python3 - "$ROOT/share/dexos/dexos-theme.py" "$temi" <<'PYEOF'
import importlib.util as u, os, sys
s = u.spec_from_file_location("dx", sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)
for n in m.names("#8a5cf5", 1.0).values():
    os.makedirs(os.path.join(sys.argv[2], n)); open(os.path.join(sys.argv[2], n, "index.theme"), "w").close()
os.symlink("/non/esiste", os.path.join(sys.argv[2], "DeX-OS-0067c0-v12"))
m.build("#8a5cf5", 1.0, sys.argv[2])
print("ok")
PYEOF
)"
}

test_puntini_come_macos() { # i tre puntini dei cataloghi diventano "…" (stessi 3 byte: sul posto), quattro restano
  local cc d
  cc=$(command -v cc || command -v clang || true)
  if [[ -z $cc ]] || ! command -v python3 >/dev/null; then echo "  (compilatore o python3 assente: salto)"; return 0; fi
  d="$WORK/puntini_$$"
  if [[ -d /data/data/com.termux/cache && -w /data/data/com.termux/cache ]]; then d=$(mktemp -d /data/data/com.termux/cache/tod-puntini-XXXXXX); fi
  mkdir -p "$d/loc/it/LC_MESSAGES"
  "$cc" -shared -fPIC -O2 -o "$d/shim.so" "$ROOT/share/tod-gettext.c" -ldl
  python3 - "$d/loc/it/LC_MESSAGES/prova.mo" <<'PYEOF'
import struct, sys
msgs = {b"": b"Content-Type: text/plain; charset=UTF-8\n", b"Properties...": "Proprietà...".encode(),
        b"Wait....": b"Attendere....", b"A...B...": b"Uno...due...", b"Done": b"Fatto"}
keys = sorted(msgs); ids = b""; strs = b""; o = []; t = []
for k in keys:
    o.append((len(k), len(ids))); ids += k + b"\0"
    t.append((len(msgs[k]), len(strs))); strs += msgs[k] + b"\0"
n = len(keys); start = 7 * 4 + 16 * n
out = struct.pack("Iiiiiii", 0x950412de, 0, n, 7 * 4, 7 * 4 + 8 * n, 0, 0)
out += b"".join(struct.pack("ii", l, start + p) for l, p in o)
out += b"".join(struct.pack("ii", l, start + len(ids) + p) for l, p in t)
open(sys.argv[1], "wb").write(out + ids + strs)
PYEOF
  cat >"$d/puntini.c" <<'CEOF'
#include <stdio.h>
char *dgettext(const char *, const char *);
int main(int argc, char **argv) {
  for (int i = 1; i < argc; i++) printf("%s|", dgettext("prova", argv[i]));
  printf("\n");
  return 0;
}
CEOF
  "$cc" -o "$d/puntini" "$d/puntini.c" 2>/dev/null || "$cc" -o "$d/puntini" "$d/puntini.c" "$d/shim.so" -Wl,-rpath,"$d"
  eq "tre puntini → … ; quattro no; più volte nella stessa frase" "Proprietà…|Attendere....|Uno…due…|Fatto|" \
    "$(env TOD_GETTEXT_LANG=it TOD_GETTEXT_DIR="$d/loc" LD_PRELOAD="$d/shim.so" "$d/puntini" "Properties..." "Wait...." "A...B..." "Done")"
  rm -rf "$d"
}

test_lingua_portachiavi() { # la password del portachiavi in italiano: i cataloghi di gcr e gnome-keyring
  has "cataloghi di gcr e del portachiavi" "gcr gnome-keyring" "$LINGUA_DEB_PKGS"
  has "nome del portachiavi (libsecret) ed errori di sistema (libc)" "libsecret-common libc-l10n" "$LINGUA_DEB_PKGS"
  conf_set LINGUA it
  lingua_setup() { echo chiamata >>"$HOME/ripara"; }
  touch "$LINGUA_SO"
  mkdir -p "$LINGUA_DIR/it/LC_MESSAGES"
  touch "$LINGUA_DIR/it/LC_MESSAGES/thunar.mo"
  lingua_ripara
  wait
  eq "traduzione di prima, senza gcr: i cataloghi si riscaricano da soli" chiamata "$(cat "$HOME/ripara" 2>/dev/null)"
  rm -f "$HOME/ripara" "$TOD_STATE_DIR/lingua-tentativo"
  touch "$LINGUA_DIR/it/LC_MESSAGES/gcr.mo" "$LINGUA_DIR/it/LC_MESSAGES/gnome-keyring.mo"
  lingua_ripara
  wait
  eq "manca ancora libc (errori di sistema): si riscarica" chiamata "$(cat "$HOME/ripara" 2>/dev/null)"
  rm -f "$HOME/ripara" "$TOD_STATE_DIR/lingua-tentativo"
  touch "$LINGUA_DIR/it/LC_MESSAGES/libc.mo"
  lingua_ripara
  wait
  eq "completa: niente da fare" "" "$(cat "$HOME/ripara" 2>/dev/null)"
}

test_glib_in_italiano() { # 11 novembre: le frasi di GIO ("Error opening file…: Permission denied", "8 bytes") in italiano
  local cc d out
  cc=$(command -v cc || command -v clang || true)
  if [[ -z $cc ]] || ! command -v python3 >/dev/null; then echo "  (compilatore o python3 assente: salto)"; return 0; fi
  d="$WORK/glib_$$"
  if [[ -d /data/data/com.termux/cache && -w /data/data/com.termux/cache ]]; then d=$(mktemp -d /data/data/com.termux/cache/tod-glib-XXXXXX); fi
  mkdir -p "$d/loc/it/LC_MESSAGES"
  "$cc" -shared -fPIC -O2 -o "$d/shim.so" "$ROOT/share/tod-gettext.c" -ldl
  python3 - "$d/loc/it/LC_MESSAGES" <<'PYEOF'
import struct, sys
def mo(path, msgs, plurale=False):
    keys = sorted(msgs); ids = b""; strs = b""; o = []; t = []
    for k in keys:
        o.append((len(k), len(ids))); ids += k + b"\0"
        t.append((len(msgs[k]), len(strs))); strs += msgs[k] + b"\0"
    n = len(keys); start = 7 * 4 + 16 * n
    out = struct.pack("Iiiiiii", 0x950412de, 0, n, 7 * 4, 7 * 4 + 8 * n, 0, 0)
    out += b"".join(struct.pack("ii", l, start + p) for l, p in o)
    out += b"".join(struct.pack("ii", l, start + len(ids) + p) for l, p in t)
    open(path, "wb").write(out + ids + strs)
testa = b"Content-Type: text/plain; charset=UTF-8\nPlural-Forms: nplurals=2; plural=(n != 1);\n"
mo(sys.argv[1] + "/glib20.mo", {b"": testa, "Error opening file “%s”: %s".encode(): "Errore nell'aprire il file «%s»: %s".encode(),
                                 b"%s byte\0%s bytes": b"%s byte\0%s byte"})
mo(sys.argv[1] + "/libc.mo", {b"": testa, b"Permission denied": b"Permesso negato"})
PYEOF
  cat >"$d/glib.c" <<'CEOF'
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <errno.h>
int main(void) {
  void *glib = dlopen("libglib-2.0.so.0", RTLD_NOW | RTLD_GLOBAL);
  if (!glib) glib = dlopen("libglib-2.0.so", RTLD_NOW | RTLD_GLOBAL);
  const char *(*gt)(const char *) = (const char *(*)(const char *))dlsym(RTLD_DEFAULT, "glib_gettext");
  const char *(*se)(int) = (const char *(*)(int))dlsym(RTLD_DEFAULT, "g_strerror");
  char *(*fs)(unsigned long long, int) = (char *(*)(unsigned long long, int))dlsym(RTLD_DEFAULT, "g_format_size_full");
  printf("gio|%s\n", gt("Error opening file “%s”: %s"));
  printf("errore|%s\n", se(EACCES));
  if (!glib) { puts("dimensioni|salta"); return 0; }
  printf("dimensioni|%s|%s|%s\n", fs(8, 0), fs(167303987ULL, 2), fs(1234, 1));
  return 0;
}
CEOF
  "$cc" -o "$d/glib" "$d/glib.c" -ldl 2>/dev/null || "$cc" -o "$d/glib" "$d/glib.c"
  out=$(env TOD_GETTEXT_LANG=it TOD_GETTEXT_DIR="$d/loc" LD_PRELOAD="$d/shim.so" "$d/glib" 2>&1 | sed 's/\xc2\xa0/ /g')
  rm -rf "$d"
  has "le frasi di GIO dal catalogo glib20" "gio|Errore nell'aprire il file «%s»: %s" "$out"
  has "gli errori di sistema dal catalogo libc" "errore|Permesso negato" "$out"
  if grep -q "dimensioni|salta" <<<"$out"; then echo "  (GLib non caricabile: salto le dimensioni)"; return 0; fi
  has "byte al singolare e plurale italiani, decimali con la virgola, anche nella forma lunga" \
    "dimensioni|8 byte|159,6 MiB|1,2 kB (1234 byte)" "$out"
}

test_avvisi_debian_zenity() { # 11 novembre: domande e avvisi delle app di Debian (zenity) come gli altri popup del desktop
  command -v python3 >/dev/null || return 0
  local z="$ROOT/share/tod-zenity" d="$WORK/zen_$$" out
  mkdir -p "$d/app"
  # come PhotoCraft (rfd): livello, titolo, testo, poi --question con le etichette; e le forme --opzione=valore
  out=$(python3 - "$z" <<'PYEOF'
import importlib.machinery as m, importlib.util as u, sys
s = u.spec_from_loader("z", m.SourceFileLoader("z", sys.argv[1])); z = u.module_from_spec(s); s.loader.exec_module(z)
r = z.leggi(["--warning", "--title", "PhotoCraft", "--text", "Salvare?", "--question", "--ok-label", "Salva",
             "--cancel-label", "Cancel", "--extra-button", "Non salvare"])
print("rfd", r["tipo"], r["livello"], r["titolo"], r["ok"], r["annulla"], r["extra"])
r = z.leggi(["--info", "--text=Fatto\\nsotto", "--timeout=7", "--no-markup", "--icon-name=gimp"])
print("info", r["tipo"], repr(r["testo"]), r["scadenza"], r["markup"], r["icona"])
print("file", z.leggi(["--file-selection", "--title", "Apri"])["tipo"])
print("sconosciuta", z.leggi(["--question", "--text", "x", "--forms"]))
print("solo-domanda", z.leggi(["--question", "--text", "x"])["livello"])
PYEOF
)
  has "rfd: domanda con il livello d'avvertimento e le sue etichette" "rfd question warning PhotoCraft Salva Cancel ['Non salvare']" "$out"
  has "forme --opzione=valore, \\n, scadenza, icona" "info info 'Fatto\nsotto' 7 False gimp" "$out"
  has "anche la scelta dei file la fa il desktop (finestra Apri/Salva nostra)" "file file" "$out"
  has "un'opzione sconosciuta: zenity vero" "sconosciuta None" "$out"
  has "domanda semplice: niente triangolo" "solo-domanda None" "$out"
  # il giro intero: un finto servizio del desktop risponde "Non salvare" → stampa e uscita 1 come zenity
  printf '#!/bin/sh\necho "vero $*"\nexit 3\n' >"$d/zenity-vero"
  chmod +x "$d/zenity-vero"
  python3 - "$d/s.sock" "$d/richiesta" <<'PYEOF' &
import json, os, socket, sys
s = socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(1)
c, _ = s.accept()
open(sys.argv[2], "w").write(c.makefile("r").readline())
c.sendall(b'{"codice": 1, "stampa": "Non salvare\\n"}\n'); c.close()
PYEOF
  local srv=$!
  for _ in $(seq 50); do [[ -S $d/s.sock ]] && break; sleep 0.1; done
  out=$(TOD_AVVISI_SOCK="$d/s.sock" TOD_ZENITY_VERO="$d/zenity-vero" python3 "$z" --question --text "Salvare?" \
    --extra-button "Non salvare"; echo "uscita=$?")
  wait "$srv" || true
  eq "la risposta del desktop arriva all'app come da zenity" "Non salvare
uscita=1" "$out"
  has "chi chiede: il servizio ne trova nome e icona" '"chi": [' "$(cat "$d/richiesta")"
  out=$(TOD_AVVISI_SOCK="$d/niente.sock" TOD_ZENITY_VERO="$d/zenity-vero" python3 "$z" --question --text "x"; echo "uscita=$?")
  eq "servizio spento: zenity vero, stessi argomenti" "vero --question --text x
uscita=3" "$out"
  # il servizio: nome e icona dall'app che chiede, testi e pulsanti come macOS
  printf '[Desktop Entry]\nName=PhotoCraft\nExec=/x/bin/tod cx run -- photocraft %%U\nIcon=ai.storyteller.photocraft\n' \
    >"$d/app/tod-cx-ai.storyteller.photocraft.desktop"
  printf '[Desktop Entry]\nName=Altro\nExec=/x/bin/tod cx run -- altro\nIcon=altro\n[Desktop Action n]\nExec=photocraft\n' \
    >"$d/app/tod-cx-altro.desktop"
  out=$(LANGUAGE=it python3 - "$ROOT/share/tod-avvisi-host.py" "$d/app" <<'PYEOF'
import importlib.util as u, sys
s = u.spec_from_file_location("h", sys.argv[1]); h = u.module_from_spec(s); s.loader.exec_module(h)
print("app", h.app_di(["/home/x/.local/share/tod-diretto/ld.so", "--argv0", "/usr/bin/photocraft", "/opt/pc/photocraft"], [sys.argv[2]]))
print("nessuna", h.app_di(["/usr/bin/sconosciuto"], [sys.argv[2]]))
print("titolo=app", h.testi("PhotoCraft", "Salvare le modifiche?\nAltrimenti vanno perse.", "PhotoCraft"))
print("titolo vero", h.testi("Esporta", "Il file c'è già.", "PhotoCraft"))
print("pulsanti", h.pulsanti({"tipo": "question", "ok": "Ok", "annulla": "Cancel", "extra": ["No"]}))
print("predefinito annulla", h.pulsanti({"tipo": "question", "predefinito_annulla": True})[1])
print("avviso", h.pulsanti({"tipo": "error"}))
PYEOF
)
  has "nome e icona dell'app che chiede (anche senza proot: caricatore e --argv0)" "app ('PhotoCraft', 'ai.storyteller.photocraft')" "$out"
  has "le azioni delle voci ([Desktop Action]) non contano" "nessuna (None, None)" "$out"
  has "titolo = nome dell'app: in grassetto la prima riga del testo" "titolo=app ('Salvare le modifiche?', 'Altrimenti vanno perse.')" "$out"
  has "un titolo vero resta il titolo" "titolo vero ('Esporta', \"Il file c'è già.\")" "$out"
  has "le etichette inglesi di rfd nella lingua del desktop; l'OK a destra e predefinito" \
    "pulsanti ([('No', ('extra', 'No')), ('Annulla', 'annulla'), ('OK', 'ok')], 'ok')" "$out"
  has "--default-cancel" "predefinito annulla annulla" "$out"
  has "errore: solo OK" "avviso ([('OK', 'ok')], 'ok')" "$out"
  # nel container solo con python3 (sennò zenity non partirebbe più), via con AVVISI_DEBIAN=0; il servizio dall'hook
  local zr="$WORK/zroot_$$"
  mkdir -p "$zr/usr/bin" "$zr/usr/local/bin" "$zr/home/dex"
  cx_rootfs() { echo "$zr"; }
  cx_user() { echo dex; }
  cx_install_runner
  [[ ! -e $zr/usr/local/bin/zenity ]] || { echo "  ✘ zenity nostro senza python3 nel container"; return 1; }
  printf '#!/bin/sh\n' >"$zr/usr/bin/python3"
  chmod +x "$zr/usr/bin/python3"
  cx_install_runner
  has "zenity nostro nel container" "generato da Termux On DeX (share/tod-zenity" "$(cat "$zr/usr/local/bin/zenity")"
  [[ -x $zr/usr/local/bin/zenity ]] || { echo "  ✘ zenity nostro non eseguibile"; return 1; }
  conf_set AVVISI_DEBIAN 0
  cx_install_runner
  [[ ! -e $zr/usr/local/bin/zenity ]] || { echo "  ✘ AVVISI_DEBIAN=0: il zenity nostro doveva sparire"; return 1; }
  has "il servizio parte con la sessione, sapendo dov'è il container (percorsi di Apri/Salva)" \
    'TOD_CX_ROOTFS="$(cx_rootfs)" python3 "$TOD_ROOT/share/tod-avvisi-host.py"' \
    "$(sed -n '/^session_hook() {/,/^}/p' "$ROOT/lib/session.sh")"
}

test_portale_dei_file() { # 11 novembre: le app Electron (Boxy SVG…) mostravano i percorsi del container e non allegavano
  local T=/data/data/com.termux/files/usr py out
  has "il servizio del desktop offre il portale" "tod_portale.avvia(app_di)" "$(cat "$ROOT/share/tod-avvisi-host.py")"
  has "la finestra restituisce anche i percorsi veri" "def scegli_veri(r):" "$(cat "$ROOT/share/tod_apri_salva.py")"
  py=$T/bin/python3
  if [[ ! -x $py ]] || ! "$py" -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null; then
    echo "  (PyGObject di Termux assente: salto)"; return 0
  fi
  out=$(env -u LD_PRELOAD timeout 60 "$py" - "$ROOT/share" <<'PYEOF' 2>&1
import sys
sys.path.insert(0, sys.argv[1])
import tod_portale as p
print("richiesta", p.percorso_richiesta(":1.42", {"handle_token": "abc"}))
print("filtri", p.filtri([("Immagini", [(0, "*.svg"), (1, "image/png")]), ("Tutti", [(0, "*")]), ("Strano", [(1, "x-tod/mai-visto")])]))
r = p.richiesta("OpenFile", "Apri", {"multiple": True, "current_folder": list(b"/home/dex/Lavori\0")})
print("apri", r["modo"], r["multipli"], r["cartella"], r["titolo"])
print("cartella", p.richiesta("OpenFile", "", {"directory": True})["modo"])
r = p.richiesta("SaveFile", "Salva", {"current_name": "disegno.svg", "current_folder": list(b"/storage/emulated/0/Documents\0")})
print("salva", r["modo"], r["nome"], r["cartella"], r["conferma"])
r = p.richiesta("SaveFile", "Salva", {"current_file": list(b"/home/dex/a/b.txt\0")})
print("salva2", r["nome"], r["cartella"])
print("versione", p.VERSIONE >= 3, p.NOME)
PYEOF
  )
  has "la richiesta ha il percorso che le app si aspettano" "richiesta /org/freedesktop/portal/desktop/request/1_42/abc" "$out"
  has "filtri: motivi e tipi MIME, '*' e tipi sconosciuti = tutto" "filtri [['Immagini', ['*.svg', '*.png']], ['Tutti', []], ['Strano', []]]" "$out"
  has "Apri: più file e cartella di partenza" "apri apri True /home/dex/Lavori Apri" "$out"
  has "scelta di una cartella" "cartella cartella" "$out"
  has "Salva: nome proposto, cartella, conferma se esiste" "salva salva disegno.svg /storage/emulated/0/Documents True" "$out"
  has "Salva su un file esistente" "salva2 b.txt /home/dex/a" "$out"
  has "versione che Chromium ed Electron accettano (almeno 3)" "versione True org.freedesktop.portal.Desktop" "$out"
}

test_apri_salva_come_macos() { # 11 novembre: la finestra Apri/Salva delle app di Debian (zenity --file-selection) come macOS
  command -v python3 >/dev/null || return 0
  local out d="$WORK/apri_$$"
  mkdir -p "$d"
  # gli argomenti che passa rfd (le app Craft) per aprire, scegliere più file, cartelle, salvare
  out=$(python3 - "$ROOT/share/tod-zenity" <<'PYEOF'
import importlib.machinery as m, importlib.util as u, sys
s = u.spec_from_loader("z", m.SourceFileLoader("z", sys.argv[1])); z = u.module_from_spec(s); s.loader.exec_module(z)
r = z.leggi(["--file-selection", "--file-filter", "PDF | *.pdf", "--filename", "/home/dex/Documenti/", "--title", "Apri"])
print("apri", r["tipo"], r["file"]["modo"], r["file"]["cartella"], r["file"]["filtri"], r["titolo"])
r = z.leggi(["--file-selection", "--multiple", "--separator=:"])
print("multipli", r["file"]["multipli"], r["file"]["separatore"])
print("cartella", z.leggi(["--file-selection", "--directory"])["file"]["modo"])
r = z.leggi(["--file-selection", "--save", "--confirm-overwrite", "--filename", "/home/dex/Relazione.pdf"])
print("salva", r["file"]["modo"], r["file"]["cartella"], r["file"]["nome"], r["file"]["conferma"])
PYEOF
)
  has "Apri: cartella, formati e titolo" "apri file apri /home/dex/Documenti/ ['PDF | *.pdf'] Apri" "$out"
  has "più file, con il separatore" "multipli True :" "$out"
  has "una cartella" "cartella cartella" "$out"
  has "Salva: cartella e nome, sempre con la conferma" "salva salva /home/dex Relazione.pdf True" "$out"
  has "il servizio passa la scelta dei file alla finestra nostra" "import tod_apri_salva" "$(cat "$ROOT/share/tod-avvisi-host.py")"
  # le parti senza schermo, con il Python di Termux (PyGObject); il resto con GTK vera su Xvfb
  local T=/data/data/com.termux/files/usr py x pid
  py=$T/bin/python3
  if [[ ! -x $py ]] || ! "$py" -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null; then
    echo "  (PyGObject di Termux assente: salto)"; return 0
  fi
  mkdir -p "$d/radice/home/dex/Lavori/Sotto" "$d/casa/.config" "$d/casa/Documenti/Fatture" "$d/telefono/Documents" "$d/tmp"
  echo x >"$d/radice/home/dex/Lavori/x.txt"
  ln -s "$d/telefono/Documents" "$d/casa/Documenti2"
  printf 'XDG_DOCUMENTS_DIR="$HOME/Documenti"\n' >"$d/casa/.config/user-dirs.dirs"
  for f in "Relazione.pdf" "Note.txt" "Foto.png"; do echo x >"$d/casa/Documenti/$f"; done
  x=$((350 + RANDOM % 100))
  while [[ -e $T/tmp/.X11-unix/X$x ]]; do x=$((x + 1)); done
  "$T/bin/Xvfb" ":$x" -screen 0 1200x800x24 -nolisten tcp >/dev/null 2>&1 &
  pid=$!
  for _ in $(seq 50); do [[ -S $T/tmp/.X11-unix/X$x ]] && break; sleep 0.1; done
  out=$(env -u LD_PRELOAD DISPLAY=":$x" HOME="$d/casa" TMPDIR="$d/tmp" TOD_CX_ROOTFS="$d/radice" LANGUAGE=it \
    TOD_TELEFONO_DIR="$d/telefono" timeout 60 "$py" - "$ROOT/share" "$d" <<'PYEOF' 2>&1
import os, sys, time
sys.path.insert(0, sys.argv[1])
d = sys.argv[2]
import tod_apri_salva as a
from gi.repository import Gtk
print("dal", a.dal_container("/home/dex/x.pdf") == d + "/radice/home/dex/x.pdf", a.dal_container("/storage/emulated/0/a"),
      a.dal_container("/tmp/b") == d + "/tmp/b")
print("al", a.al_container(d + "/radice/home/dex/x.pdf"), a.al_container(d + "/casa/Documenti2/n.pdf") == d + "/telefono/Documents/n.pdf",
      a.al_container(d + "/tmp/c"))
print("filtri", a.filtri_zenity(["Immagini | *.png *.JPG", "*.txt"]), a.corrisponde("A.jpg", ["*.JPG"]), a.corrisponde("a.pdf", ["*.png"]))
print("dimensioni", a.dimensione(6), a.dimensione(12300), a.dimensione(1234567), a.dimensione(0))
print("nome", a.nome_senza_estensione("Relazione finale.pdf"), a.nome_senza_estensione("Archivio"))
now = time.mktime((2026, 11, 11, 15, 0, 0, 0, 0, -1))
print("date", a.data_breve(now - 600, now), "|", a.data_breve(now - 86400, now), "|", a.data_breve(now - 86400 * 30, now))
print("posti", [(s, n) for s, n, _i, _p in a.posti()])
a.carica_css()
esiti = []
p = a.Pannello({"modo": "apri", "cartella": "/home/dex/", "filtri": [["PDF", ["*.pdf"]]]}, esiti.append)
p.vai(d + "/casa/Documenti")
righe = {r[a.Pannello.NOME]: r for r in p.ordinato}
print("righe", list(righe), "attive", [n for n, r in righe.items() if r[a.Pannello.ATTIVA]])
sel = p.elenco.get_selection()
for n in ("Note.txt", "Relazione.pdf"):
    sel.unselect_all()
    sel.select_path(righe[n].path)
    print("scelto", n, p.scelti() and os.path.basename(p.scelti()[0]), p.principale.get_sensitive())
p.conferma()
print("apri ->", [os.path.basename(x) for x in esiti[0]])
esiti = []
s = a.Pannello({"modo": "salva", "nome": "Nuovo.pdf", "cartella": "/home/dex/Lavori/", "conferma": False}, esiti.append)
print("salva nome", s.nome.get_text(), s.principale.get_sensitive(), os.path.basename(s.qui))
s.conferma()
print("salva ->", a.al_container(esiti[0][0]))
esiti = []
c = a.Pannello({"modo": "cartella", "cartella": "/home/dex/Lavori/"}, esiti.append)
righe = {r[a.Pannello.NOME]: r for r in c.ordinato}
print("cartella attive", [n for n, r in righe.items() if r[a.Pannello.ATTIVA]])
c.conferma()
print("cartella ->", a.al_container(esiti[0][0]))
PYEOF
)
  kill "$pid" 2>/dev/null || true
  rm -rf "$d"
  has "percorsi del container → veri (container, Android, /tmp)" "dal True /storage/emulated/0/a True" "$out"
  has "veri → del container (radice tolta, collegamenti risolti, /tmp)" "al /home/dex/x.pdf True /tmp/c" "$out"
  has "formati di zenity, maiuscole indifferenti" "filtri [['Immagini', ['*.png', '*.JPG']], ['*.txt', ['*.txt']]] True False" "$out"
  has "dimensioni come il Finder, in italiano" "dimensioni 6 byte 12 KB 1,2 MB 0 byte" "$out"
  has "Salva: il nome si sceglie senza estensione" "nome 16 8" "$out"
  has "date come il Finder" "date oggi, 14:50 | ieri, 15:00 | 12 ott 2026" "$out"
  has "barra laterale: Recenti, la casa, le cartelle dell'utente, il Telefono" "('pref', 'Recenti')" "$out"
  has "cartelle prima, poi i file per nome" "righe ['Fatture', 'Foto.png', 'Note.txt', 'Relazione.pdf']" "$out"
  has "il formato esclude gli altri file (le cartelle restano)" "attive ['Fatture', 'Relazione.pdf']" "$out"
  has "un file escluso non si sceglie, Apri spento" "scelto Note.txt [] False" "$out"
  has "un PDF si sceglie, Apri acceso" "scelto Relazione.pdf Relazione.pdf True" "$out"
  has "Apri restituisce il file" "apri -> ['Relazione.pdf']" "$out"
  has "Salva: nome proposto, cartella data dall'app (percorso del container)" "salva nome Nuovo.pdf True Lavori" "$out"
  has "Salva restituisce cartella e nome, come li vede l'app" "salva -> /home/dex/Lavori/Nuovo.pdf" "$out"
  has "Cartella: solo le cartelle" "cartella attive ['Sotto']" "$out"
  has "Cartella: senza scelta, quella aperta" "cartella -> /home/dex/Lavori" "$out"
}

# =============================================================================
pass=0 fail=0 failed=()
for fn in $(declare -F | awk '{print $3}' | grep '^test_' | grep -- "${FILTER}" || true); do
  out=$( ( set -Eeuo pipefail; fresh_env "$fn"; "$fn" ) 2>&1 )
  rc=$?
  if [[ $rc == 0 ]]; then
    pass=$((pass + 1))
    printf 'ok   %s\n' "$fn"
    if [[ -n $out ]]; then printf '%s\n' "$out"; fi
  else
    fail=$((fail + 1))
    failed+=("$fn")
    printf 'FAIL %s (rc=%s)\n%s\n' "$fn" "$rc" "$out"
  fi
done
printf '\n%d superati, %d falliti\n' "$pass" "$fail"
if ((fail)); then printf '  - %s\n' "${failed[@]}"; exit 1; fi
