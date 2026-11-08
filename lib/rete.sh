# shellcheck shell=bash
# =============================================================================
#  La Rete in Esplora file (share/rete). I PC e i NAS di casa come cartelle in
#  /media/rete/<PC>/<cartella condivisa>, per tutte le app: tod-fs.so le
#  raggiunge come le partizioni dei dischi USB, ma il server di ogni PC è
#  share/rete/tod-smb (Samba, libsmbclient), compilato qui. Il servizio
#  share/rete/tod-rete.py trova i PC, tiene acceso un tod-smb per ciascuno e
#  chiede la password quando serve. RETE=0 la spegne.
# =============================================================================

# samba: libsmbclient (e le sue intestazioni, per compilare tod-smb)
RETE_PKGS="samba"

rete_on() { [[ $(conf_get RETE 1) == 1 ]]; }

rete_bin() { printf '%s/.local/share/tod-rete/tod-smb' "${TOD_TERMUX_HOME:-$HOME}"; }

rete_fs_ok() { # il tod-fs.so installato conosce /media/rete (pacchetto LKL dalla versione 3)
  grep -qa '/media/rete' "$(usb_data)/tod-fs.so" 2>/dev/null
}

rete_pronta() { # tod-smb compilato e tod-fs.so che lo raggiunge
  [[ -x $(rete_bin) ]] && rete_fs_ok
}

rete_compila() { # tod-smb: share/usb/lkl/sftp.c con Samba sotto (rifatto se il sorgente è più nuovo)
  local b s
  b=$(rete_bin)
  s="$TOD_ROOT/share/rete/tod-smb.c"
  if [[ -x $b && ! $s -nt $b && ! $TOD_ROOT/share/usb/lkl/sftp.c -nt $b ]]; then return 0; fi
  [[ -f $PREFIX/include/samba-4.0/libsmbclient.h ]] || return 1
  if ! have clang; then pkg_install clang || return 1; fi
  mkdir -p "${b%/*}"
  clang -O2 -Wall -I"$PREFIX/include/samba-4.0" -I"$TOD_ROOT/share/usb/lkl" -o "$b.nuovo" "$s" -lsmbclient \
    >>"$TOD_LOG" 2>&1 || return 1
  mv -f "$b.nuovo" "$b"
}

rete_samba_conf() { # libsmbclient legge la configurazione di Samba a ogni collegamento: senza, un avviso ogni volta
  local f="$PREFIX/etc/samba/smb.conf"
  [[ -e $f ]] && return 0
  mkdir -p "${f%/*}"
  printf '# Termux On DeX (lib/rete.sh): solo client, per le cartelle condivise in /media/rete\n[global]\n  client min protocol = SMB2\n' >"$f"
}

rete_install() { # tod rete prepara (e l'installazione): Samba, tod-smb
  rete_on || return 0
  # shellcheck disable=SC2086
  pkg_install $RETE_PKGS || return 1
  rete_samba_conf
  rete_compila
}

rete_mostra_esplora() { # "Esplora la rete" visibile: files_session la nasconde (senza la Rete è vuota), e l'8 novembre
  # l'avevo nascosta anch'io. Si toglie network:/// dalle voci nascoste di Thunar, senza toccare le altre
  have xfconf-query || return 0
  local v x
  local -a voci=()
  v=$(xfconf-query -c thunar -p /hidden-bookmarks 2>/dev/null | sed '1,/^$/d')
  if ! grep -qxF 'network:///' <<<"$v"; then return 0; fi
  while IFS= read -r x; do
    if [[ -n $x && $x != network:/// ]]; then voci+=(-t string -s "$x"); fi
  done <<<"$v"
  if ((${#voci[@]})); then
    xfconf-query -c thunar -p /hidden-bookmarks -n -a "${voci[@]}" 2>/dev/null || true
  else
    xfconf-query -c thunar -p /hidden-bookmarks -r 2>/dev/null || true
  fi
}

rete_start() { # dall'hook di sessione: il servizio della rete. In Esplora file la Rete è "Esplora la rete": la voce
  # di Thunar (network://) apre /media/rete grazie a tod-gettext.so (g_file_new_for_uri)
  rete_on || return 0
  rete_compila || true   # un tod-smb.c nuovo (aggiornamento): si ricompila
  rete_pronta || return 0
  rete_mostra_esplora
  (setsid python3 "$TOD_ROOT/share/rete/tod-rete.py" sorveglia </dev/null >>"$TOD_STATE_DIR/rete.log" 2>&1 &)
}

rete_stop() { # fine della sessione (mai nei test: agirebbe sui processi veri)
  [[ -z ${TOD_TEST_RUN:-} ]] || return 0
  [[ -d ${TMPDIR:-/tmp}/tod-rete ]] || return 0
  timeout 10 python3 "$TOD_ROOT/share/rete/tod-rete.py" ferma >>"$TOD_STATE_DIR/rete.log" 2>&1 || true
}

rete_cli() { # tod rete [stato|prepara|cerca|aggiungi INDIRIZZO [NOME]|togli NOME|dimentica NOME|riavvia|on|off]
  local sub=${1:-stato}
  if (($#)); then shift; fi
  case $sub in
    prepara | install) rete_install || die "$(t rete_no)"; ok "$(t rete_ok)" ;;
    on | si | sì) conf_set RETE 1; rete_start ;;
    off | no) conf_set RETE 0; rete_stop ;;
    riavvia | restart) rete_stop; sleep 1; rete_start ;;
    stato | status | cerca | elenco | aggiungi | togli | dimentica)
      LANGUAGE=$TOD_LANG python3 "$TOD_ROOT/share/rete/tod-rete.py" "$sub" "$@" ;;
    *) die "tod rete stato|prepara|cerca|aggiungi INDIRIZZO [NOME]|togli NOME|dimentica NOME|riavvia|on|off" ;;
  esac
}
