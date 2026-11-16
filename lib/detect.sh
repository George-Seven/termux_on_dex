# shellcheck shell=bash
# =============================================================================
#  Rilevamento: dispositivo, Android/One UI, GPU, Termux — e controlli iniziali
# =============================================================================

prop() { getprop "$1" 2>/dev/null || true; }

oneui_version() { # "80500" → "8.5"
  local v=${1:-}
  if [[ $v =~ ^[1-9][0-9]{4,5}$ ]]; then printf '%d.%d' $((v / 10000)) $(((v % 10000) / 100)); fi
}

detect_device() {
  local kb
  DEV_BRAND=$(prop ro.product.brand)
  DEV_MODEL=$(prop ro.product.model)
  DEV_NAME=$(prop ro.product.marketname)
  if [[ -z $DEV_NAME ]]; then DEV_NAME=$(prop ro.product.vendor.marketname); fi
  ANDROID_REL=$(prop ro.build.version.release)
  ANDROID_SDK=$(prop ro.build.version.sdk)
  ONEUI_VER=$(oneui_version "$(prop ro.build.version.oneui)")
  SOC_MODEL=$(prop ro.soc.model)
  SOC_MFR=$(prop ro.soc.manufacturer)
  ARCH=$(uname -m)
  KERNEL=$(uname -r)
  kb=$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null) || kb=0
  RAM_MB=$(($(num "$kb") / 1024))
  kb=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2{print $4}') || kb=0
  FREE_MB=$(($(num "$kb") / 1024))
  detect_gpu
}

detect_gpu() {
  GPU_EGL=$(prop ro.hardware.egl)
  GPU_VK=$(prop ro.hardware.vulkan)
  GPU_MODEL=""
  if [[ -r /sys/class/kgsl/kgsl-3d0/gpu_model ]]; then
    GPU_MODEL=$(tr -d '\0\n' </sys/class/kgsl/kgsl-3d0/gpu_model 2>/dev/null) || GPU_MODEL=""
  fi
  gpu_classify "$GPU_MODEL" "$GPU_EGL" "$GPU_VK" "${SOC_MODEL:-}" "${SOC_MFR:-}"
}

# gpu_classify modello egl vulkan soc produttore  (pura, testabile)
# Imposta: GPU_FAMILY (adreno|mali|powervr|xclipse|other), GPU_ADRENO (es. 840), GPU_REV, GPU_LABEL
# Solo Adreno ha un driver Mesa (Turnip/Freedreno); le altre passano dal driver del telefono (virgl).
# I nodi in /dev contano più delle proprietà: i MediaTek recenti dichiarano "meow" (uno strato sopra il
# driver vero) e certi telefoni non dichiarano niente. TOD_DEV_DIR: un /dev finto per i test.
gpu_classify() {
  local model=${1,,} egl=${2,,} vk=${3,,} soc=${4^^} mfr=${5,,} dev=${TOD_DEV_DIR:-/dev}
  GPU_FAMILY=other GPU_ADRENO="" GPU_REV=""
  if [[ $model == adreno* || $egl == adreno* || $vk == adreno* || -e $dev/kgsl-3d0 ]]; then
    GPU_FAMILY=adreno
  elif [[ $egl == mali* || $vk == mali* || -e $dev/mali0 ]]; then
    GPU_FAMILY=mali
  elif [[ $egl == powervr* || $vk == powervr* || -e $dev/pvr_sync ]]; then
    GPU_FAMILY=powervr
  elif [[ $egl == *xclipse* || $vk == *xclipse* ]]; then
    GPU_FAMILY=xclipse
  elif [[ $egl == meow* && ($mfr == mediatek* || $soc == MT*) ]]; then
    GPU_FAMILY=mali
  elif [[ $mfr == samsung* || $soc == S5E* ]]; then
    GPU_FAMILY=xclipse
  fi
  if [[ $GPU_FAMILY == adreno ]]; then
    if [[ $model =~ ([0-9]{3}) ]]; then
      GPU_ADRENO=${BASH_REMATCH[1]}
    else
      case $soc in
        SM8850*) GPU_ADRENO=840 ;; SM8750*) GPU_ADRENO=830 ;; SM8650*) GPU_ADRENO=750 ;;
        SM8550*) GPU_ADRENO=740 ;; SM8475* | SM8450*) GPU_ADRENO=730 ;;
        SM8350*) GPU_ADRENO=660 ;; SM8250*) GPU_ADRENO=650 ;;
      esac
    fi
    if [[ $model =~ v([0-9]+)$ ]]; then GPU_REV=${BASH_REMATCH[1]}; fi
    GPU_LABEL="Adreno ${GPU_ADRENO:-?}${GPU_REV:+ v$GPU_REV}"
  else
    case $GPU_FAMILY in
      powervr) GPU_LABEL="PowerVR" ;;
      other) GPU_LABEL="${2:-?}" ;;
      *) GPU_LABEL="${GPU_FAMILY^}" ;;
    esac
    if [[ $GPU_FAMILY != other && -n ${2:-} && ${2,,} != "$GPU_FAMILY" ]]; then GPU_LABEL+=" ($2)"; fi
  fi
}

detect_termux() {
  TERMUX_VER=${TERMUX_VERSION:-}
  TERMUX_SRC=${TERMUX_APP__APK_RELEASE:-${TERMUX_APK_RELEASE:-}}
  X11_APK=""
  if X11_APK=$("${TOD_PM:-/system/bin/pm}" path com.termux.x11 2>/dev/null); then
    X11_APK=$(sed -n '1s/^package://p' <<<"$X11_APK")
  else
    X11_APK=""
  fi
}

x11_app_installed() {
  local p=""
  p=$("${TOD_PM:-/system/bin/pm}" path com.termux.x11 2>/dev/null) || p=""
  [[ $p == package:* ]]
}

device_summary() {
  printf '  %-12s %s %s%s\n' "$(t lbl_device)" "${DEV_BRAND^}" "${DEV_NAME:-$DEV_MODEL}" "${DEV_NAME:+ ($DEV_MODEL)}"
  printf '  %-12s Android %s%s · kernel %s\n' "$(t lbl_system)" "$ANDROID_REL" "${ONEUI_VER:+ · One UI $ONEUI_VER}" "$KERNEL"
  printf '  %-12s %s · %s\n' "$(t lbl_soc)" "${SOC_MODEL:-?}" "$GPU_LABEL${GPU_MODEL:+ [$GPU_MODEL]}"
  printf '  %-12s %s MB RAM · %s MB %s\n' "$(t lbl_memory)" "$RAM_MB" "$FREE_MB" "$(t lbl_free)"
  printf '  %-12s %s%s\n' "Termux" "${TERMUX_VER:-?}" "${TERMUX_SRC:+ ($TERMUX_SRC)}"
}

# Quanto spazio serve ancora da qui in poi → "minimo consigliato" in MB. Un'installazione nuova con il
# container arriva a circa 8 GB; chi riprende a metà ha già scaricato quasi tutto e non va fermato.
preflight_spazio() {
  if step_done apps; then echo "500 1500"
  elif step_done cx || [[ $(conf_get CX_ENABLE yes) != yes ]]; then echo "2000 4000"
  else echo "4000 8000"; fi
}

preflight_32bit() { # Termux a 32 bit (anche su un telefono a 64): container, dischi e Termux:X11 nostri sono solo arm64
  local pa=${TOD_PKG_ARCH:-}
  if [[ -z $pa ]]; then pa=$(dpkg --print-architecture 2>/dev/null) || pa=""; fi
  [[ $pa == arm || $pa == i686 || $ARCH == armv7* || $ARCH == armv8l || $ARCH == i?86 ]]
}

preflight() { # 0 = ok, 1 = errore bloccante. Ogni riga dice anche cosa fare: chi si ferma qui non ha altro
  local fatal=0 serve consigliati
  if [[ $PREFIX != */com.termux/files/usr ]]; then err "$(t pf_not_termux)"; fatal=1; fi
  if [[ $(id -u) == 0 ]]; then err "$(t pf_root)"; fatal=1; fi
  if preflight_32bit; then err "$(t pf_arch32 "$ARCH")"; fatal=1
  elif [[ $ARCH != aarch64 ]]; then warn "$(t pf_arch "$ARCH")"; fi
  if [[ $ANDROID_SDK =~ ^[0-9]+$ ]] && (( ANDROID_SDK < 26 )); then err "$(t pf_android)"; fatal=1; fi
  case $TERMUX_SRC in
    GOOGLE_PLAY_STORE) warn "$(t pf_playstore)" ;;
    F_DROID) warn "$(t pf_fdroid)" ;;
  esac
  read -r serve consigliati <<<"$(preflight_spazio)"
  if (( FREE_MB < serve )); then err "$(t pf_space_ko "$FREE_MB" "$serve")"; fatal=1
  elif (( FREE_MB < consigliati )); then warn "$(t pf_space "$FREE_MB" "$consigliati")"; fi
  # sotto i 6 GB (dichiarati: il sistema ne vede un po' meno) Android chiude presto i processi in sottofondo
  if (( RAM_MB > 0 && RAM_MB < 5500 )); then warn "$(t pf_ram "$RAM_MB")"; fi
  return "$fatal"
}
