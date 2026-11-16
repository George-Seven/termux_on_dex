# shellcheck shell=bash
# =============================================================================
#  Priorità e memoria del desktop (15 novembre 2026; Pietro: "una gestione della memoria tale da mantenere
#  sempre tutto fluido anche quando sotto sforzo"). share/tod-priorita.py tiene in alto lo schermo e l'app
#  attiva, porta le altre app a nice 0 e i lavori pesanti a nice 10, e quando la memoria sta per finire chiede
#  ad Android (con ADB) di chiudere le app del telefono ferme in cache. PRIORITA=0 lo spegne.
#  share/prova_fluidita.py misura quanto risponde lo schermo sotto carico.
# =============================================================================

priorita_voluta() { [[ $(conf_get PRIORITA 1) == 1 ]]; }

priorita_start() { # dall'hook di sessione; uno solo per sessione (lo garantisce il servizio con un lucchetto)
  if [[ $(conf_get PRIORITA 1) != 1 ]] || ! have python3; then return 0; fi
  (setsid python3 "$TOD_ROOT/share/tod-priorita.py" </dev/null >>"$TOD_STATE_DIR/priorita.log" 2>&1 &)
}

priorita_cli() { # tod priorita [stato|misura|on|off]
  case ${1:-stato} in
    stato | status) python3 "$TOD_ROOT/share/tod-priorita.py" stato ;;
    misura | measure) python3 "$TOD_ROOT/share/prova_fluidita.py" ;;
    on)
      conf_set PRIORITA 1
      priorita_start
      python3 "$TOD_ROOT/share/tod-priorita.py" stato
      ;;
    off)
      conf_set PRIORITA 0
      python3 "$TOD_ROOT/share/tod-priorita.py" ferma   # rimette le priorità di partenza
      ;;
    *) die "tod priorita [stato|misura|on|off]" ;;
  esac
}
