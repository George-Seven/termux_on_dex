# shellcheck shell=bash
# =============================================================================
#  Stampanti (share/stampa). Il servizio di stampa è il CUPS di Debian, nel
#  container, senza root e senza proot: share/stampa/tod-stampa.py lo accende
#  con la sessione, aggiunge da solo le stampanti Wi-Fi (Bonjour) e USB (i
#  ponti di share/stampa/tod-stampante-usb.py, lanciati da tod-usb) e risponde
#  alle Impostazioni. client.conf di Termux e del container puntano al suo
#  socket: ogni app stampa lì. STAMPA=0 lo spegne.
# =============================================================================

# i pacchetti del container: arrivano con l'installazione e con gli aggiornamenti (CX_AIUTI in lib/container.sh).
# cups-core-drivers: pstops, per le app che stampano in PostScript
STAMPA_CX_PKGS="cups-daemon cups-filters cups-core-drivers"

stampa_on() { [[ $(conf_get STAMPA 1) == 1 ]]; }

stampa_socket() { printf '%s/tod-stampa/cups.sock' "${TMPDIR:-/tmp}"; }

stampa_pronta() { # cupsd e i filtri ci sono nel container
  local root
  root=$(cx_rootfs) || return 1
  [[ -x $root/usr/sbin/cupsd && -x $root/usr/lib/cups/filter/pdftopdf ]]
}

stampa_client_conf() { # client.conf di Termux e del container: le app trovano il nostro CUPS da sole
  local riga f
  riga="ServerName $(stampa_socket)"
  for f in "$PREFIX/etc/cups/client.conf" "$(cx_rootfs 2>/dev/null || echo /nessuno)/etc/cups/client.conf"; do
    [[ $f == /nessuno/* ]] && continue
    [[ -d ${f%/*} ]] || mkdir -p "${f%/*}" 2>/dev/null || continue
    if [[ $(cat "$f" 2>/dev/null) != *"$riga"* ]]; then
      printf '# Termux On DeX (lib/stampa.sh): il servizio di stampa del desktop\n%s\n' "$riga" >"$f"
    fi
  done
  return 0
}

stampa_filtri() { # i nostri filtri di CUPS nel container: share/stampa/tod-pdftourf (stampanti solo AirPrint)
  local d
  d="$(cx_rootfs 2>/dev/null || echo /nessuno)/usr/lib/cups/filter"
  [[ -d $d ]] || return 0
  if ! cmp -s "$TOD_ROOT/share/stampa/tod-pdftourf" "$d/tod-pdftourf"; then
    cp -f "$TOD_ROOT/share/stampa/tod-pdftourf" "$d/tod-pdftourf.$$" && chmod 755 "$d/tod-pdftourf.$$" &&
      mv -f "$d/tod-pdftourf.$$" "$d/tod-pdftourf"
  fi
  return 0
}

stampa_start() { # dall'hook di sessione: il servizio di stampa, se il container ha CUPS
  stampa_on || return 0
  cx_installed || return 0
  stampa_pronta || return 0
  stampa_client_conf
  stampa_filtri
  (setsid python3 "$TOD_ROOT/share/stampa/tod-stampa.py" sorveglia </dev/null >>"$TOD_STATE_DIR/stampa.log" 2>&1 &)
}

stampa_stop() { # fine della sessione: servizio e CUPS chiusi (mai nei test: agirebbe sui processi veri)
  [[ -z ${TOD_TEST_RUN:-} ]] || return 0
  [[ -d ${TMPDIR:-/tmp}/tod-stampa ]] || return 0
  timeout 20 python3 "$TOD_ROOT/share/stampa/tod-stampa.py" ferma >>"$TOD_STATE_DIR/stampa.log" 2>&1 || true
}

stampa_prepara() { # tod stampa prepara: CUPS e i filtri nel container, poi il servizio
  cx_installed || die "$(t stampa_no_cx)"
  if ! stampa_pronta; then
    # shellcheck disable=SC2086
    cx_pkg_install $STAMPA_CX_PKGS || die "$(t stampa_no_pkgs)"
  fi
  stampa_client_conf
  stampa_filtri
  ok "$(t stampa_ok)"
}

stampa_cli() { # tod stampa [stato|prepara|cerca|aggiungi|togli|predefinita|prova|riavvia|on|off]
  local sub=${1:-stato}
  if (($#)); then shift; fi
  case $sub in
    prepara | install) stampa_prepara ;;
    on | si | sì) conf_set STAMPA 1; desktop_scanner_entry; stampa_start ;;
    off | no) conf_set STAMPA 0; desktop_scanner_entry; stampa_stop ;;
    riavvia | restart) stampa_stop; stampa_start ;;
    stato | status | cerca | elenco | aggiungi | togli | predefinita | prova | annulla)
      LANGUAGE=$TOD_LANG python3 "$TOD_ROOT/share/stampa/tod-stampa.py" "$sub" "$@" ;;
    *) die "tod stampa stato|prepara|cerca|aggiungi INDIRIZZO|togli NOME|predefinita NOME|prova NOME|riavvia|on|off" ;;
  esac
}

scanner_cli() { # tod scanner [elenco|aggiungi INDIRIZZO|togli CHIAVE|scansiona FILE …]: senza niente la finestra
  # (share/stampa/tod-scanner.py: eSCL, Wi-Fi e multifunzione USB; niente servizio, niente container)
  LANGUAGE=$TOD_LANG python3 "$TOD_ROOT/share/stampa/tod-scanner.py" "$@"
}
