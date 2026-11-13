# shellcheck shell=bash
# =============================================================================
#  Video col chip nelle app Chromium/Electron del container (12-13 novembre;
#  Pietro: "deve essere generale per tutti: installo una nuova app e ha già
#  questa miglioria"). VS Code, il Chromium di Debian e ogni app che
#  verrà installata decodificano H.264 e VP9 con il chip del telefono
#  (MediaCodec) invece che col processore. HEVC no: VA non passa al driver gli
#  insiemi di riferimento della SPS, che il chip vorrebbe. Prima di accenderlo,
#  una prova reale (tod-va-prova): su un telefono Mali senza Vulkan hardware nel
#  container resta spento e le app partono come sempre. Pezzi in share/video:
#    tod_drv_video.c  driver VA-API "tod" (glibc, nel processo GPU di Chromium)
#    tod-va-shim.c    nodo DRM finto, /sys finto, risposta a DRI3 Open
#    tod-gbm.c        libgbm con memoria /dev/dma_heap/system
#    tod-va-chip.c    l'aiutante in Termux (ffmpeg *_mediacodec), socket @tod-va-chip
#  Compilati qui, sul telefono, quando il sorgente è più nuovo. Le app
#  Chromium/Electron le riconosce diretto_run (e tod-run in proot) dalla loro
#  cartella; l'ambiente e i flag stanno in /etc/tod/video.env del container,
#  che c'è solo col ponte pronto e acceso. VIDEO_CHIP=0 lo spegne.
# =============================================================================

# Termux: le intestazioni di libva per compilare il driver (l'ffmpeg col chip c'è già: MEDIA_PKGS)
VIDEOCHIP_PKGS="libva"
# i flag di Chromium: GPU con ANGLE su Vulkan (l'unica che mostra i fotogrammi dma-buf senza copie: ANGLE su GLX non li
# importa, su EGL non ha i visual di X, l'EGL nativo in Chromium 156 non è più permesso), il decodificatore VA-API anche
# fuori da ChromeOS, i fotogrammi NV12 mostrati così come sono, il nostro nodo al posto della scheda che non c'è
VIDEOCHIP_FLAGS="--enable-features=Vulkan,DefaultANGLEVulkan,VulkanFromANGLE,AcceleratedVideoDecodeLinuxZeroCopyGL,VaapiVideoDecoder,VaapiVideoDecodeLinuxGL,VaapiIgnoreDriverChecks,AcceleratedVideoDecodeLinuxGL --use-angle=vulkan --ignore-gpu-blocklist --render-node-override=/dev/dri/renderD128"

videochip_on() { [[ $(conf_get VIDEO_CHIP 1) == 1 ]]; }

videochip_dir() { printf '%s/.local/share/tod-va' "${TOD_TERMUX_HOME:-$HOME}"; }

videochip_pronto() { # tutti i pezzi compilati e la scheda finta
  local d
  d=$(videochip_dir)
  [[ -f $d/tod_drv_video.so && -f $d/tod-va-shim.so && -f $d/tod-gbm.so && -x $d/tod-va-chip && -x $d/tod-va-prova &&
    -f $d/finto/sys/dev/char/226:128/device/uevent ]]
}

# --- la prova reale (share/video/tod-va-prova.c): Pietro, 13 novembre: "deve funzionare anche per gli smartphone che non
# hanno Adreno ma hanno Mali". La metà "decodifica" (MediaCodec) c'è su tutti gli Android; la metà "grafica" (ANGLE su
# Vulkan che importa i dma-buf) solo se nel container c'è un Vulkan hardware: su Adreno turnip, su Mali di solito no.
# Senza, il ponte resta spento e le app partono come sempre. Si rifà quando cambia il profilo grafico del container.
videochip_firma() { # profilo grafico del container + la prova stessa
  local root
  root=$(cx_rootfs) || return 0
  cat "$root/etc/tod/gpu.env" "$(videochip_dir)/tod-va-prova" 2>/dev/null | cksum | cut -d' ' -f1
}

videochip_prova() { # → 0 se il telefono regge il ponte; il risultato (con la firma) in tod-va/prova.txt
  local f firma
  f="$(videochip_dir)/prova.txt"
  firma=$(videochip_firma)
  if [[ -f $f && $(sed -n 1p "$f") == "$firma" ]]; then
    grep -qx 'ESITO ok' "$f"
    return
  fi
  { echo "$firma"; videochip_esegui_prova 2>&1 | grep -E '^(dma_heap|vulkan|  manca|ESITO)' || true; } >"$f.nuovo"
  mv -f "$f.nuovo" "$f"
  log "video col chip, prova reale: $(grep -E '^(vulkan|ESITO)' "$f" | tr '\n' ' ')"
  grep -qx 'ESITO ok' "$f"
}

videochip_esegui_prova() { # nel container, con lo stesso profilo grafico delle app (al più un minuto)
  timeout 60 bash "$TOD_ROOT/tod" cx run -- "$(videochip_dir)/tod-va-prova"
}

videochip_motivo() { # perché il ponte non c'è (dall'ultima prova)
  sed -n 's/^ESITO no: //p' "$(videochip_dir)/prova.txt" 2>/dev/null | sed -n 1p
}

videochip_adatto() { # ultima prova riuscita (senza rifarla)
  grep -qx 'ESITO ok' "$(videochip_dir)/prova.txt" 2>/dev/null
}

videochip_compila() { # i quattro pezzi, rifatti solo se il sorgente è più nuovo
  local d src root libc f o flags
  d=$(videochip_dir)
  src="$TOD_ROOT/share/video"
  root=$(cx_rootfs) || return 1
  libc="$root/lib/aarch64-linux-gnu/libc.so.6"
  [[ -f $libc && -f $PREFIX/include/va/va_backend.h ]] || return 1
  have clang || return 1
  mkdir -p "$d/include" "$TOD_STATE_DIR"
  # solo le intestazioni di libva: quelle della libc di Termux (Bionic) non vanno bene per il glibc del container
  ln -sfn "$PREFIX/include/va" "$d/include/va"
  for f in tod_drv_video tod-va-shim tod-gbm; do
    o="$d/$f.so"
    if [[ -f $o && ! $src/$f.c -nt $o ]]; then continue; fi
    if clang --target=aarch64-linux-gnu -O2 -fPIC -shared -nostdlib -ffreestanding -fno-builtin -fuse-ld=lld \
      -I"$d/include" -o "$o.nuovo" "$src/$f.c" "$libc" >>"$TOD_STATE_DIR/tod.log" 2>&1; then
      mv -f "$o.nuovo" "$o"
      log "video col chip: $f compilato"
    else
      rm -f "$o.nuovo"
      log "video col chip: $f non compilato (resta la decodifica col processore)"
      return 1
    fi
  done
  # la prova reale (un eseguibile del container: caricatore di glibc)
  o="$d/tod-va-prova"
  if [[ ! -x $o || $src/tod-va-prova.c -nt $o ]]; then
    if clang --target=aarch64-linux-gnu -O1 -nostdlib -ffreestanding -fno-builtin -fuse-ld=lld \
      -Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1 -o "$o.nuovo" "$src/tod-va-prova.c" "$libc" >>"$TOD_STATE_DIR/tod.log" 2>&1; then
      mv -f "$o.nuovo" "$o"
    else
      rm -f "$o.nuovo"
      log "video col chip: prova non compilata"
      return 1
    fi
  fi
  o="$d/tod-va-chip"
  if [[ -x $o && ! $src/tod-va-chip.c -nt $o ]]; then return 0; fi
  flags=$(pkg-config --cflags --libs libavcodec libavutil 2>/dev/null) || return 1
  # shellcheck disable=SC2086
  if clang -O2 -o "$o.nuovo" "$src/tod-va-chip.c" $flags >>"$TOD_STATE_DIR/tod.log" 2>&1; then
    mv -f "$o.nuovo" "$o"
    log "video col chip: aiutante compilato"
  else
    rm -f "$o.nuovo"
    log "video col chip: aiutante non compilato"
    return 1
  fi
}

videochip_finto() { # la scheda grafica finta che lo shim mostra al posto di /sys (Chromium vuole un dispositivo PCI)
  local f d
  f="$(videochip_dir)/finto"
  d="$f/sys/dev/char/226:128/device"
  if [[ -f $d/uevent ]]; then return 0; fi
  mkdir -p "$f/dev/dri" "$d/drm/renderD128" "$f/sys/bus/pci"
  : >"$f/dev/dri/renderD128"
  ln -sfn ../../../../bus/pci "$d/subsystem"
  printf '0x5143\n' >"$d/vendor"
  printf '0x0840\n' >"$d/device"
  printf '0x5143\n' >"$d/subsystem_vendor"
  printf '0x0001\n' >"$d/subsystem_device"
  printf '0x00\n' >"$d/revision"
  printf '0x030000\n' >"$d/class"
  printf '1\n' >"$d/boot_vga"
  printf 'MAJOR=226\nMINOR=128\nDEVNAME=dri/renderD128\nDEVTYPE=drm_minor\n' >"$f/sys/dev/char/226:128/uevent"
  printf 'DRIVER=tod\nPCI_CLASS=30000\nPCI_ID=5143:0840\nPCI_SUBSYS_ID=5143:0001\nPCI_SLOT_NAME=0000:01:00.0\n' >"$d/uevent"
}

videochip_env() { # /etc/tod/video.env del container: c'è solo col ponte pronto e acceso (lo leggono diretto_run e tod-run)
  local root f d
  root=$(cx_rootfs) || return 0
  f="$root/etc/tod/video.env"
  d=$(videochip_dir)
  if videochip_on && videochip_pronto && videochip_adatto; then
    mkdir -p "${f%/*}"
    {
      printf '# video.env - generato da Termux On DeX (lib/videochip.sh): video col chip nelle app Chromium/Electron\n'
      printf 'VA_PRELOAD="%s %s"\n' "$d/tod-gbm.so" "$d/tod-va-shim.so"
      printf 'VA_FLAGS="%s"\n' "$VIDEOCHIP_FLAGS"
      printf 'LIBVA_DRIVER_NAME="tod"\nLIBVA_DRIVERS_PATH="%s"\nTOD_VA_FINTO="%s"\n' "$d" "$d/finto"
    } >"$f.nuovo"
    mv -f "$f.nuovo" "$f"
  else
    rm -f "$f"
  fi
}

videochip_aiutante_vivo() { # l'aiutante di questa sessione è acceso (pid nel file, ed è proprio lui)
  local p
  p=$(cat "$TMPDIR/tod-va-chip.pid" 2>/dev/null || true)
  [[ -n $p && -r /proc/$p/cmdline ]] && tr '\0' ' ' <"/proc/$p/cmdline" | grep -q 'tod-va-chip'
}

videochip_start() { # dall'hook di sessione: compila se serve, scrive video.env, accende l'aiutante (uno per sessione)
  if ! videochip_on; then
    videochip_env
    return 0
  fi
  videochip_compila || true
  videochip_finto || true
  if videochip_pronto; then videochip_prova || true; fi
  videochip_env
  videochip_pronto || return 0
  videochip_adatto || return 0   # telefono senza la grafica che serve (Mali): niente aiutante, le app come sempre
  if videochip_aiutante_vivo; then return 0; fi
  # un registro per sessione: l'aiutante scrive poche righe per video (mai un registro che cresce senza fine)
  (
    setsid "$(videochip_dir)/tod-va-chip" </dev/null >"$TOD_STATE_DIR/video-chip.log" 2>&1 &
    echo $! >"$TMPDIR/tod-va-chip.pid"
  )
}

videochip_stop() { # spento dall'utente: niente più ambiente per le app nuove, e l'aiutante si chiude
  [[ -z ${TOD_TEST_RUN:-} ]] || return 0
  videochip_env
  if videochip_aiutante_vivo; then kill "$(cat "$TMPDIR/tod-va-chip.pid")" 2>/dev/null || true; fi
  rm -f "$TMPDIR/tod-va-chip.pid"
}

videochip_install() { # installazione e aggiornamenti: libva in Termux, i pezzi compilati, la scheda finta
  videochip_on || return 0
  # shellcheck disable=SC2086
  pkg_install $VIDEOCHIP_PKGS || return 1
  videochip_compila || return 1
  videochip_finto
  videochip_prova || true   # un telefono che non la passa non è un errore: il ponte resta spento
  videochip_env
}

videochip_cli() { # tod videochip [stato|prepara|on|off]
  local sub=${1:-stato}
  case $sub in
    prepara | install)
      videochip_install || die "$(t vc_no)"
      if videochip_adatto; then ok "$(t vc_ok)"; else warn "$(t vc_non_adatto "$(videochip_motivo)")"; fi
      ;;
    on | si | sì) conf_set VIDEO_CHIP 1; videochip_start; ok "$(t vc_acceso)" ;;
    off | no) conf_set VIDEO_CHIP 0; videochip_stop; ok "$(t vc_spento)" ;;
    stato | status)
      if ! videochip_on; then
        echo "$(t vc_spento)"
      elif videochip_pronto && videochip_aiutante_vivo; then
        echo "$(t vc_attivo)"
      elif [[ -n $(videochip_motivo) ]]; then
        echo "$(t vc_non_adatto "$(videochip_motivo)")"
      else
        echo "$(t vc_non_pronto)"
      fi
      ;;
    *) die "tod videochip stato|prepara|on|off" ;;
  esac
}
