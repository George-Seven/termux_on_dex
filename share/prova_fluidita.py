#!/data/data/com.termux/files/usr/bin/python3
"""prova_fluidita.py — quanto risponde lo schermo quando il telefono è sotto sforzo (15 novembre 2026, tod priorita misura).

Un filo con la priorità dello schermo (-10, come il server grafico) disegna "fotogrammi": ogni 16,7 ms (60 al secondo)
fa un lavoro che a riposo dura circa 4 ms, e misura quanto ci mette davvero. Sotto carico, alla pari con gli altri,
deve dividere il core e il fotogramma si allunga: oltre 16,7 ms è un fotogramma perso, uno scatto. (Misurare solo il
risveglio dopo una pausa non basta: chi dorme passa comunque davanti, e col carico la CPU resta sveglia e risponde
persino prima.) Intanto tanti processi quanti i core girano a vuoto, come una compilazione:
  a riposo        niente carico
  come prima      carico alla stessa priorità dello schermo (fino al 15 novembre tutto ereditava -10)
  app             carico a nice 0 (le app e i servizi col servizio delle priorità acceso)
  lavori pesanti  carico a nice 10 (compilazioni, apt, prove)
Tempi del fotogramma in millisecondi: mediana, 95° percentile, massimo, e fotogrammi persi. Pochi secondi per prova:
il telefono si scalda poco.
"""
import os
import subprocess
import sys
import time

ITALIAN = os.environ.get("LANGUAGE", os.environ.get("LANG", "it")).startswith("it")
FOTOGRAMMA = 1000 / 60   # ms
LAVORO = 4.0             # ms di lavoro per fotogramma, a riposo


def T(it, en):
    return it if ITALIAN else en


def statistiche(valori):
    """(mediana, 95°, 99°, massimo) in ms"""
    if not valori:
        return 0.0, 0.0, 0.0, 0.0
    v = sorted(valori)

    def q(p):
        return v[min(len(v) - 1, int(p * len(v)))]
    return q(0.5), q(0.95), q(0.99), v[-1]


def carico(n, nice, secondi):
    """N processi che girano a vuoto per SECONDI a quella priorità; il nome "prova-carico" li fa ignorare al servizio
    delle priorità (share/priorita_dati.py), che altrimenti li sposterebbe"""
    codice = (f"import os, time\nos.setpriority(os.PRIO_PROCESS, 0, {nice})\nf = time.monotonic() + {secondi}\n"
              "while time.monotonic() < f:\n    pass\n")
    return [subprocess.Popen(["prova-carico", "-c", codice], executable=sys.executable,
                             stdin=subprocess.DEVNULL) for _ in range(n)]


def lavora(n):
    x = 0
    for i in range(n):
        x += i * i
    return x


def calibra():
    """quante iterazioni fanno LAVORO ms a riposo (mediana di 15 prove)"""
    n = 20000
    for _ in range(6):
        t = time.perf_counter()
        lavora(n)
        ms = (time.perf_counter() - t) * 1000
        n = max(1000, int(n * LAVORO / max(ms, 0.01)))
    tempi = []
    for _ in range(15):
        t = time.perf_counter()
        lavora(n)
        tempi.append((time.perf_counter() - t) * 1000)
    return int(n * LAVORO / statistiche(tempi)[0])


def misura(n, secondi):
    """i tempi dei fotogrammi (ms), uno ogni 16,7 ms"""
    tempi, fine = [], time.monotonic() + secondi
    prossimo = time.perf_counter()
    while time.monotonic() < fine:
        t = time.perf_counter()
        lavora(n)
        tempi.append((time.perf_counter() - t) * 1000)
        prossimo += FOTOGRAMMA / 1000
        attesa = prossimo - time.perf_counter()
        if attesa > 0:
            time.sleep(attesa)
        else:
            prossimo = time.perf_counter()
    return tempi


def main():
    secondi = float(os.environ.get("TOD_PROVA_SECONDI", "5"))
    core = os.cpu_count() or 1
    try:   # come il server grafico, chiunque la lanci (Android permette a Termux fino a -20)
        os.setpriority(os.PRIO_PROCESS, 0, -10)
    except OSError:
        pass
    mio = os.getpriority(os.PRIO_PROCESS, 0)
    n = calibra()
    print(T(f"Filo dello schermo a nice {mio}, {LAVORO:.0f} ms di lavoro per fotogramma a riposo; carico su {core} core, "
            f"{secondi:.0f} s per prova.", f"Screen thread at nice {mio}, {LAVORO:.0f} ms of work per frame when idle; "
            f"load on {core} cores, {secondi:.0f} s per test."))
    print(T("Tempo del fotogramma (ms)        mediana     95%   massimo   persi (oltre 16,7 ms)",
            "Frame time (ms)                   median     95%   maximum   dropped (over 16.7 ms)"))
    for nome, nice in ((T("a riposo", "idle"), None), (T("come prima (carico a ", "as before (load at ") + f"{mio})", mio),
                       (T("app (carico a 0)", "apps (load at 0)"), 0),
                       (T("lavori pesanti (carico a 10)", "heavy work (load at 10)"), 10)):
        figli = carico(core, nice, secondi + 0.5) if nice is not None else []
        time.sleep(0.3)
        try:
            tempi = misura(n, secondi)
        finally:
            for f in figli:
                f.kill()
                f.wait()
        m, p95, _p99, mx = statistiche(tempi)
        persi = sum(1 for t in tempi if t > FOTOGRAMMA) * 100 / max(1, len(tempi))
        print(f"  {nome:31} {m:6.1f} {p95:7.1f} {mx:9.1f}   {persi:5.1f}%")
        time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
