#!/data/data/com.termux/files/usr/bin/bash
# pacchetto.sh [VERSIONE]: l'archivio del server compilato da costruisci.sh, da caricare nella release
# venus-VERSIONE del progetto (tod-venus-aarch64.tar.xz), e lib/gpu.sh aggiornato con versione e impronta.
# Il caricamento lo fa una persona (gh release create ...): qui solo l'archivio.
set -euo pipefail
QUI=$(cd "$(dirname "$0")" && pwd)
RADICE=$(cd "$QUI/../.." && pwd)
I=${TOD_VENUS_SRC:-$HOME/src/virgl-venus}/inst
GPU="$RADICE/lib/gpu.sh"
V=${1:-$(sed -n 's/^VENUS_VERSIONE=//p' "$GPU")}
FILE=$(sed -n 's/^VENUS_FILE="\(.*\)"$/\1/p' "$GPU")
OUT="$RADICE/dist/tod-venus-aarch64.tar.xz"

for f in $FILE; do [[ -s $I/$f ]] || { echo "manca $I/$f: prima costruisci.sh" >&2; exit 1; }; done
t=$(mktemp -d)
trap 'rm -rf "$t"' EXIT
for f in $FILE; do mkdir -p "$t/${f%/*}"; cp "$I/$f" "$t/$f"; done
cat >"$t/SORGENTI.txt" <<FINE
Termux On DeX: server grafico con Venus (Vulkan del telefono per le app), versione $V.

virglrenderer $(sed -n 's/^V=//p' "$QUI/costruisci.sh") (licenza MIT) e libepoxy $(sed -n 's/^E=//p' "$QUI/costruisci.sh") (licenza MIT), con le modifiche
del pacchetto virglrenderer-android di Termux (https://github.com/termux/termux-packages) e la parte per
Android accesa. La ricetta esatta è share/venus/costruisci.sh del progetto:
https://github.com/PietroCav501/termux_on_dex
FINE
mkdir -p "${OUT%/*}"
tar -cJf "$OUT" -C "$t" $FILE SORGENTI.txt
sha=$(sha256sum "$OUT" | cut -d' ' -f1)
sed -i "s/^VENUS_VERSIONE=.*/VENUS_VERSIONE=$V/; s/^VENUS_SHA256=.*/VENUS_SHA256=$sha/" "$GPU"
echo "archivio: $OUT ($(du -h "$OUT" | cut -f1)), sha256 $sha"
echo "da caricare nella release venus-$V (asset tod-venus-aarch64.tar.xz); lib/gpu.sh aggiornato"
