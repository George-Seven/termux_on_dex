#!/data/data/com.termux/files/usr/bin/bash
# Il server grafico con Venus (Vulkan del telefono per le app del container, telefoni senza Adreno).
#
# virglrenderer con gli stessi sorgenti e le stesse modifiche del pacchetto virglrenderer-android di Termux,
# ma con la parte per Android accesa (-D__TERMUX__): nel pacchetto è rimasta fuori dalla compilazione, e
# senza quella sul Mali il server si ferma alla prima immagine ("resource_create_blob failed").
# Accesa, la modifica 0011 non compila (manca una funzione): la aggiunge correggi-0011.py.
#
# Serve a chi prepara la release (pacchetto.sh), non agli utenti: loro scaricano i file già compilati.
# Pacchetti di Termux: clang, ninja, pkg-config, python (con meson e pyyaml), patch, curl.
# Tutto in ~/src/virgl-venus (TOD_VENUS_SRC per un'altra cartella), niente nel sistema.
set -euo pipefail
QUI=$(cd "$(dirname "$0")" && pwd)
B=${TOD_VENUS_SRC:-$HOME/src/virgl-venus}
I=$B/inst
V=1.3.0
E=1.5.10
VK=v1.4.334
RAW=https://raw.githubusercontent.com/termux/termux-packages/master/packages/virglrenderer-android
mkdir -p "$B/patch" "$B/inc/vulkan"
cd "$B"
if [[ ! -d src ]]; then
  curl -fsSL -o virgl.tar.gz "https://gitlab.freedesktop.org/virgl/virglrenderer/-/archive/virglrenderer-$V/virglrenderer-virglrenderer-$V.tar.gz"
  curl -fsSL -o epoxy.tar.gz "https://github.com/anholt/libepoxy/archive/refs/tags/$E.tar.gz"
  echo "56170f8caa1bb642a2624b649e3bcca095ec2834814e5c308efc8a85a709e4ce  virgl.tar.gz" | sha256sum -c -
  echo "a7ced37f4102b745ac86d6a70a9da399cc139ff168ba6b8002b4d8d43c900c15  epoxy.tar.gz" | sha256sum -c -
  tar xf virgl.tar.gz
  mv "virglrenderer-virglrenderer-$V" src
  tar xf epoxy.tar.gz
  mv "libepoxy-$E" src/libepoxy
  for p in 0001-do-not-use-gbm 0002-avoid-gnu-offsetof-extensions-error 0003-use-angle 0004-Fallback-to-ashmem \
    0005-Enable-debug-for-EGL 0006-Disable-feature-feat_dual_src_blend 0007-Ensure-EGL-GLES-ANGLE-help-and-socket-path \
    0008-Impl-DXTn-S3TC-texture-decompression-in-virglrenderer 0009-disable-termux-detection \
    0010-disable-memfd_create-timespec_get 0011-use-Android-Vulkan 0012-fix-build-on-arm; do
    curl -fsSL -o "patch/$p.patch" "$RAW/$p.patch.beforehostbuild"
    echo "== $p"
    sed "s|@TERMUX_PREFIX@|$PREFIX|g" "patch/$p.patch" | patch -d src -p1 --no-backup-if-mismatch
  done
fi
# l'intestazione di Vulkan per Android: l'NDK di Termux (ndk-sysroot) non la porta
if [[ ! -s inc/vulkan/vulkan_android.h ]]; then
  curl -fsSL -o inc/vulkan/vulkan_android.h \
    "https://raw.githubusercontent.com/KhronosGroup/Vulkan-Headers/$VK/include/vulkan/vulkan_android.h"
fi
python3 "$QUI/correggi-0011.py" "$B/src/src/venus/vkr_device_memory.c"
export PKG_CONFIG_LIBDIR=$I/lib/pkgconfig:$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig
if [[ ! -f $I/lib/libepoxy.so ]]; then
  meson setup "$B/epoxy-build" "$B/src/libepoxy" --prefix="$I" --libdir lib -Degl=yes -Dglx=no -Dx11=false -Dtests=false
  ninja -C "$B/epoxy-build" install -j4
fi
rm -rf "$B/build"
# i percorsi di $I restano scritti nei file, ma non servono: tod lancia il server con LD_LIBRARY_PATH e
# RENDER_SERVER_EXEC_PATH sulla cartella dove l'ha installato (virgl_comando in lib/gpu.sh)
meson setup "$B/build" "$B/src" --prefix="$I" --libdir lib -Dvenus=true -Dplatforms=egl \
  -Dc_args="-D__TERMUX__ -I$B/inc" -Dc_link_args="-Wl,-rpath=$I/lib"
ninja -C "$B/build" install -j4
ls -la "$I/bin" "$I/lib" "$I/libexec"
