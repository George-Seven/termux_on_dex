#!/usr/bin/env python3
"""Ponte USB per una stampante: Android la passa a Termux (termux-usb), qui la si offre al servizio di stampa
(share/stampa/tod-stampa.py) come una stampante di rete sul telefono stesso, su 127.0.0.1:<porta>.

- IPP via USB (interfacce 7/1/4: quasi tutte le stampanti recenti, quelle AirPrint e Mopria). Ogni
  collegamento TCP va su una delle sue interfacce IPP, byte per byte: è HTTP dentro USB, come fa ipp-usb.
  CUPS la usa come una stampante IPP Everywhere, senza driver.
- Le altre (7/1/2 e 7/1/1): un collegamento è un lavoro, che va dritto alla stampante, come la porta 9100
  delle stampanti di rete. Il driver lo sceglie il servizio dall'ID IEEE 1284 che si legge qui.

  termux-usb -r -e "python3 tod-stampante-usb.py ID" /dev/bus/usb/BBB/DDD   (termux-usb aggiunge il descrittore)

Lo lancia tod-usb, che tiene il pid in $TMPDIR/tod-usb/stampa-ID.pid. Scrive $TMPDIR/tod-stampa/usb/ID.json
(porta, tipo, ID 1284, nome, numero di serie) e lo toglie quando la stampante si stacca.
"""
import ctypes
import json
import os
import queue
import socket
import sys
import threading
import time

PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
TMP = os.environ.get("TMPDIR", os.path.join(PREFIX, "tmp"))
USB_RUN = os.path.join(TMP, "tod-usb")
STAMPA_USB = os.path.join(TMP, "tod-stampa", "usb")

ERR_IO, ERR_NO_DEVICE, ERR_TIMEOUT, ERR_PIPE = -1, -4, -7, -9


def log(msg):
    print(f"{time.strftime('%F %T')} stampante USB: {msg}", flush=True)


class Staccata(Exception):
    """La stampante non c'è più (scollegata o spenta)."""


def descrittori(raw):
    """Dai descrittori letti dal file usbfs: il dispositivo e le interfacce della prima configurazione.
    Interfaccia: (numero, alternativa, classe, sottoclasse, protocollo, endpoint OUT, endpoint IN)."""
    dev, intf, cur, i = {}, [], None, 0
    while i + 2 <= len(raw):
        ln, tipo = raw[i], raw[i + 1]
        if ln < 2:
            break
        d = raw[i:i + ln]
        if tipo == 1 and ln >= 18:
            dev = {"vendor": "%04x" % (d[8] | d[9] << 8), "product": "%04x" % (d[10] | d[11] << 8),
                   "iproduct": d[15], "iserial": d[16]}
        elif tipo == 2 and intf:
            break   # una seconda configurazione: basta la prima
        elif tipo == 4 and ln >= 9:
            cur = {"num": d[2], "alt": d[3], "classe": d[5], "sub": d[6], "proto": d[7], "out": None, "in": None}
            intf.append(cur)
        elif tipo == 5 and ln >= 7 and cur is not None and d[3] & 3 == 2:   # bulk
            cur["in" if d[2] & 0x80 else "out"] = d[2]
        i += ln
    return dev, intf


def scegli(intf):
    """("ipp", [una alternativa 7/1/4 per interfaccia]) o ("raw", [la migliore 7/1/2 o 7/1/1]) o (None, [])."""
    ipp, visti = [], set()
    for i in intf:
        if (i["classe"], i["sub"], i["proto"]) == (7, 1, 4) and i["out"] and i["in"] and i["num"] not in visti:
            ipp.append(i)
            visti.add(i["num"])
    if ipp:
        return "ipp", ipp
    for proto in (2, 1):
        for i in intf:
            if (i["classe"], i["sub"], i["proto"]) == (7, 1, proto) and i["out"]:
                return "raw", [i]
    return None, []


def campi_1284(device_id):
    out = {}
    for parte in device_id.split(";"):
        k, _, v = parte.partition(":")
        if k.strip():
            out[k.strip().upper()] = v.strip()
    return out


def nome_di(device_id, prodotto):
    c = campi_1284(device_id)
    mfg, mdl = c.get("MFG") or c.get("MANUFACTURER", ""), c.get("MDL") or c.get("MODEL", "")
    if mdl:
        return mdl if mfg and mdl.lower().startswith(mfg.lower()) or not mfg else f"{mfg} {mdl}"
    return prodotto or ""


def pompa(conn, usb, out_ep, in_ep, attesa=500):
    """Un collegamento TCP e un'interfaccia della stampante, nei due sensi, finché il client chiude. Poi si
    butta quello che la stampante manda ancora: una risposta a metà rovinerebbe il collegamento dopo."""
    fermo = threading.Event()
    errori = []

    def verso_usb():
        try:
            while not fermo.is_set():
                d = conn.recv(65536)
                if not d:
                    break
                usb.scrivi(out_ep, d)
        except Staccata as e:
            errori.append(e)
        except OSError:
            pass
        fermo.set()

    t = threading.Thread(target=verso_usb, daemon=True)
    t.start()
    try:
        while not fermo.is_set():
            if in_ep is None:   # stampante a una via: niente da leggere
                fermo.wait(0.5)
                continue
            d = usb.leggi(in_ep, 65536, attesa)
            if d:
                conn.sendall(d)
    except Staccata as e:
        errori.append(e)
    except OSError:
        pass
    fermo.set()
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    t.join(5)
    if in_ep is not None and not errori:
        fine = time.time() + 0.3
        while time.time() < fine:
            try:
                if not usb.leggi(in_ep, 65536, 100):
                    break
            except Staccata as e:
                errori.append(e)
                break
    conn.close()
    if errori:
        raise errori[0]


class Ponte:
    """Il servizio TCP su 127.0.0.1: ogni collegamento prende un'interfaccia libera (le altre aspettano)."""

    def __init__(self, usb, canali):
        self.usb = usb
        self.liberi = queue.Queue()
        for c in canali:
            self.liberi.put(c)
        self.staccata = threading.Event()
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.porta = self.srv.getsockname()[1]

    def lavora(self, conn):
        c = self.liberi.get()
        try:
            pompa(conn, self.usb, c["out"], c["in"])
        except Staccata:
            self.staccata.set()
        finally:
            self.liberi.put(c)

    def gira(self):
        self.srv.settimeout(1)
        while not self.staccata.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(None)
            threading.Thread(target=self.lavora, args=(conn,), daemon=True).start()
        self.srv.close()


class Usb:
    """libusb (di Termux) sul descrittore aperto da termux-usb."""

    def __init__(self, fd):
        lib = ctypes.CDLL("libusb-1.0.so")
        self.lib = lib
        vp = ctypes.c_void_p
        lib.libusb_init.argtypes = [ctypes.POINTER(vp)]
        lib.libusb_wrap_sys_device.argtypes = [vp, ctypes.c_ssize_t, ctypes.POINTER(vp)]
        lib.libusb_set_auto_detach_kernel_driver.argtypes = [vp, ctypes.c_int]
        lib.libusb_claim_interface.argtypes = [vp, ctypes.c_int]
        lib.libusb_release_interface.argtypes = [vp, ctypes.c_int]
        lib.libusb_set_interface_alt_setting.argtypes = [vp, ctypes.c_int, ctypes.c_int]
        lib.libusb_clear_halt.argtypes = [vp, ctypes.c_ubyte]
        lib.libusb_bulk_transfer.argtypes = [vp, ctypes.c_ubyte, ctypes.c_char_p, ctypes.c_int,
                                             ctypes.POINTER(ctypes.c_int), ctypes.c_uint]
        lib.libusb_control_transfer.argtypes = [vp, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ushort, ctypes.c_ushort,
                                                ctypes.c_char_p, ctypes.c_ushort, ctypes.c_uint]
        lib.libusb_get_string_descriptor_ascii.argtypes = [vp, ctypes.c_ubyte, ctypes.c_char_p, ctypes.c_int]
        lib.libusb_set_option(None, 2)   # LIBUSB_OPTION_NO_DEVICE_DISCOVERY: Android non lascia elencare /dev/bus/usb
        self.ctx, self.h = vp(), vp()
        if lib.libusb_init(ctypes.byref(self.ctx)):
            raise OSError("libusb_init")
        if lib.libusb_wrap_sys_device(self.ctx, fd, ctypes.byref(self.h)):
            raise OSError("libusb_wrap_sys_device")
        lib.libusb_set_auto_detach_kernel_driver(self.h, 1)
        self.prese = []

    def stringa(self, indice):
        if not indice:
            return ""
        b = ctypes.create_string_buffer(256)
        n = self.lib.libusb_get_string_descriptor_ascii(self.h, indice, b, 256)
        return b.raw[:n].decode("latin-1").strip() if n > 0 else ""

    def prendi(self, num, alt):
        if self.lib.libusb_claim_interface(self.h, num):
            raise OSError(f"interfaccia {num} occupata")
        self.prese.append(num)
        if alt:
            self.lib.libusb_set_interface_alt_setting(self.h, num, alt)

    def device_id(self, num, alt):
        """L'ID IEEE 1284 (GET_DEVICE_ID della classe stampante): "MFG:HP;MDL:DeskJet 2700;CMD:PCL3GUI,…"."""
        b = ctypes.create_string_buffer(1024)
        n = self.lib.libusb_control_transfer(self.h, 0xA1, 0, 0, (num << 8) | alt, b, 1024, 5000)
        if n < 2:
            return ""
        lung = min(n, (b.raw[0] << 8) | b.raw[1])
        return b.raw[2:lung].decode("latin-1", "replace").strip("\0 ")

    def scrivi(self, ep, dati):
        fatto = ctypes.c_int()
        while dati:
            pezzo = dati[:65536]
            r = self.lib.libusb_bulk_transfer(self.h, ep, pezzo, len(pezzo), ctypes.byref(fatto), 60000)
            if r == ERR_NO_DEVICE:
                raise Staccata()
            if r == ERR_PIPE:
                self.lib.libusb_clear_halt(self.h, ep)
                continue
            if r and r != ERR_TIMEOUT:
                raise Staccata(f"scrittura: {r}")
            dati = dati[fatto.value:]

    def leggi(self, ep, n, ms):
        b = ctypes.create_string_buffer(n)
        fatto = ctypes.c_int()
        r = self.lib.libusb_bulk_transfer(self.h, ep, b, n, ctypes.byref(fatto), ms)
        if r == ERR_NO_DEVICE:
            raise Staccata()
        if r == ERR_PIPE:
            self.lib.libusb_clear_halt(self.h, ep)
        elif r and r != ERR_TIMEOUT:
            raise Staccata(f"lettura: {r}")
        return b.raw[:fatto.value]

    def viva(self):
        b = ctypes.create_string_buffer(2)
        return self.lib.libusb_control_transfer(self.h, 0x80, 0, 0, 0, b, 2, 2000) != ERR_NO_DEVICE

    def chiudi(self):
        for num in self.prese:
            self.lib.libusb_release_interface(self.h, num)


def principale(argv):
    ident = argv[1] if len(argv) > 1 else "st"
    fd = int(os.environ.get("TERMUX_USB_FD") or (argv[-1] if argv[-1].isdigit() else -1))
    os.makedirs(STAMPA_USB, exist_ok=True)
    os.makedirs(USB_RUN, exist_ok=True)
    with open(os.path.join(USB_RUN, f"stampa-{ident}.pid"), "w") as f:
        f.write(str(os.getpid()))
    os.lseek(fd, 0, os.SEEK_SET)
    dev, intf = descrittori(os.read(fd, 65536))
    tipo, canali = scegli(intf)
    if not tipo:
        log(f"{ident}: nessuna interfaccia di stampa")
        return 1
    usb = Usb(fd)
    for c in canali:
        usb.prendi(c["num"], c["alt"])
    did = usb.device_id(canali[0]["num"], canali[0]["alt"])
    prodotto = usb.stringa(dev.get("iproduct", 0))
    seriale = usb.stringa(dev.get("iserial", 0)) or campi_1284(did).get("SN", "") or campi_1284(did).get("SERN", "")
    ponte = Ponte(usb, canali)
    info = {"id": ident, "porta": ponte.porta, "tipo": tipo, "device_id": did, "nome": nome_di(did, prodotto),
            "seriale": seriale, "vendor": dev.get("vendor"), "product": dev.get("product"), "interfacce": len(canali)}
    uscita = os.path.join(STAMPA_USB, f"{ident}.json")
    with open(uscita + ".nuovo", "w") as f:
        json.dump(info, f, ensure_ascii=False)
    os.replace(uscita + ".nuovo", uscita)
    log(f"{ident}: {info['nome'] or dev.get('vendor')} ({tipo}, {len(canali)} interfacce) su 127.0.0.1:{ponte.porta}")

    def sorveglia():   # da ferma nessun trasferimento lo direbbe: una richiesta di stato ogni 3 s
        while not ponte.staccata.is_set():
            time.sleep(3)
            if not usb.viva():
                ponte.staccata.set()

    threading.Thread(target=sorveglia, daemon=True).start()
    try:
        ponte.gira()
    finally:
        try:
            os.unlink(uscita)
        except OSError:
            pass
        usb.chiudi()
        log(f"{ident}: staccata")
    return 0


if __name__ == "__main__":
    sys.exit(principale(sys.argv))
