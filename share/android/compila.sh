#!/data/data/com.termux/files/usr/bin/bash
# compila.sh — AppAndroid.java → tod-app-android.dex, l'aiutante della Gestione attività per le app del telefono.
# Solo per chi sviluppa: sul telefono degli utenti va il .dex già pronto (niente Java da 200 MB per tutti).
# Serve, in Termux: pkg install openjdk-21 d8
# Accanto al .dex resta l'impronta del sorgente (tod-app-android.sorgente): un test controlla che il .dex sia
# stato rifatto dopo ogni modifica al .java.
set -Eeuo pipefail
qui=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
for p in javac d8; do
  command -v "$p" >/dev/null || { echo "compila.sh: manca $p (pkg install openjdk-21 d8)" >&2; exit 1; }
done
unset CLASSPATH   # quello della sessione (Termux:X11) non c'entra
t=$(mktemp -d)
trap 'rm -rf "$t"' EXIT
javac --release 8 -Xlint:-options -d "$t/classi" "$qui/AppAndroid.java"
mapfile -t classi < <(find "$t/classi" -name '*.class')
d8 --release --min-api 26 --output "$t" "${classi[@]}"
mv "$t/classes.dex" "$qui/tod-app-android.dex"
sha256sum "$qui/AppAndroid.java" | cut -d' ' -f1 >"$qui/tod-app-android.sorgente"
echo "compila.sh: $qui/tod-app-android.dex ($(wc -c <"$qui/tod-app-android.dex") byte)"
