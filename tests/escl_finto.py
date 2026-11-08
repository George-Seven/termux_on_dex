#!/usr/bin/env python3
"""Uno scanner eSCL finto per le prove (tests/run.sh, test_scanner_*): vetro e alimentatore con fronte-retro.

  escl_finto.py [--pagine N] [--occupato K] [--jpeg FILE] [--registro FILE] [--porta P]

Scrive la porta su stdout e serve /eSCL su 127.0.0.1. Dal vetro una pagina, dall'alimentatore N; le prime K
NextDocument rispondono 503 (si scalda); Location dice "localhost" come le multifunzione USB. Nel registro una riga
per richiesta (metodo, percorso, Host) e il corpo delle POST.
"""
import argparse
import os
import re
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAPACITA = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
    xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <pwg:MakeAndModel>Multifunzione finta MF-100</pwg:MakeAndModel>
  <pwg:SerialNumber>FINTA0001</pwg:SerialNumber>
  <scan:Platen>
    <scan:PlatenInputCaps>
      <scan:MinWidth>16</scan:MinWidth>
      <scan:MaxWidth>2550</scan:MaxWidth>
      <scan:MinHeight>16</scan:MinHeight>
      <scan:MaxHeight>3508</scan:MaxHeight>
      <scan:SettingProfiles>
        <scan:SettingProfile>
          <scan:ColorModes>
            <scan:ColorMode>BlackAndWhite1</scan:ColorMode>
            <scan:ColorMode>Grayscale8</scan:ColorMode>
            <scan:ColorMode>RGB24</scan:ColorMode>
          </scan:ColorModes>
          <scan:DocumentFormats>
            <pwg:DocumentFormat>application/pdf</pwg:DocumentFormat>
            <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
            <scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt>
          </scan:DocumentFormats>
          <scan:SupportedResolutions>
            <scan:DiscreteResolutions>
              <scan:DiscreteResolution><scan:XResolution>75</scan:XResolution><scan:YResolution>75</scan:YResolution></scan:DiscreteResolution>
              <scan:DiscreteResolution><scan:XResolution>150</scan:XResolution><scan:YResolution>150</scan:YResolution></scan:DiscreteResolution>
              <scan:DiscreteResolution><scan:XResolution>300</scan:XResolution><scan:YResolution>300</scan:YResolution></scan:DiscreteResolution>
              <scan:DiscreteResolution><scan:XResolution>600</scan:XResolution><scan:YResolution>600</scan:YResolution></scan:DiscreteResolution>
              <scan:DiscreteResolution><scan:XResolution>300</scan:XResolution><scan:YResolution>600</scan:YResolution></scan:DiscreteResolution>
            </scan:DiscreteResolutions>
          </scan:SupportedResolutions>
        </scan:SettingProfile>
      </scan:SettingProfiles>
    </scan:PlatenInputCaps>
  </scan:Platen>
  <scan:Adf>
    <scan:AdfSimplexInputCaps>
      <scan:MaxWidth>2550</scan:MaxWidth>
      <scan:MaxHeight>4200</scan:MaxHeight>
      <scan:SettingProfiles>
        <scan:SettingProfile>
          <scan:ColorModes>
            <scan:ColorMode>Grayscale8</scan:ColorMode>
            <scan:ColorMode>RGB24</scan:ColorMode>
          </scan:ColorModes>
          <scan:DocumentFormats><pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat></scan:DocumentFormats>
          <scan:SupportedResolutions>
            <scan:ResolutionRange>
              <scan:XResolutionRange><scan:Min>100</scan:Min><scan:Max>300</scan:Max><scan:Step>1</scan:Step></scan:XResolutionRange>
              <scan:YResolutionRange><scan:Min>100</scan:Min><scan:Max>300</scan:Max><scan:Step>1</scan:Step></scan:YResolutionRange>
            </scan:ResolutionRange>
          </scan:SupportedResolutions>
        </scan:SettingProfile>
      </scan:SettingProfiles>
    </scan:AdfSimplexInputCaps>
    <scan:AdfDuplexInputCaps><scan:MaxWidth>2550</scan:MaxWidth></scan:AdfDuplexInputCaps>
  </scan:Adf>
</scan:ScannerCapabilities>
"""

STATO = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
    xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"><pwg:Version>2.63</pwg:Version><pwg:State>Idle</pwg:State></scan:ScannerStatus>
"""


def jpeg_finto(larghezza, altezza, componenti=3, dpi=150):
    """Solo l'intestazione di un JPEG (JFIF con il dpi, SOF0): basta per il PDF delle prove."""
    app0 = b"JFIF\0\x01\x01\x01" + struct.pack(">HH", dpi, dpi) + b"\0\0"
    sof = b"\x08" + struct.pack(">HH", altezza, larghezza) + bytes([componenti]) + b"\x01\x11\x00" * componenti
    return (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", len(app0) + 2) + app0
            + b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof + b"\xff\xd9")


class Scanner:
    def __init__(self, pagine, occupato, jpeg, registro):
        self.pagine, self.occupato, self.jpeg, self.registro = pagine, occupato, jpeg, registro
        self.lavori, self.n, self.m = {}, 0, threading.Lock()

    def scrivi(self, riga):
        if self.registro:
            with self.m, open(self.registro, "a") as f:
                f.write(riga.rstrip("\n") + "\n")


def gestore(sc):
    class G(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def manda(self, cod, corpo=b"", tipo="text/xml", extra=None):
            self.send_response(cod)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(corpo)

        def do_GET(self):
            sc.scrivi(f"GET {self.path} Host={self.headers.get('Host')}")
            if self.path == "/eSCL/ScannerCapabilities":
                return self.manda(200, CAPACITA.encode())
            if self.path == "/eSCL/ScannerStatus":
                return self.manda(200, STATO.encode())
            m = re.match(r"^/eSCL/ScanJobs/(\d+)/NextDocument$", self.path)
            if not m or m.group(1) not in sc.lavori:
                return self.manda(404)
            with sc.m:
                if sc.occupato > 0:
                    sc.occupato -= 1
                    cod = 503
                elif sc.lavori[m.group(1)] > 0:
                    sc.lavori[m.group(1)] -= 1
                    cod = 200
                else:
                    cod = 404
            if cod == 200:
                return self.manda(200, sc.jpeg, "image/jpeg")
            return self.manda(cod)

        def do_POST(self):
            corpo = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
            sc.scrivi(f"POST {self.path} Host={self.headers.get('Host')}")
            sc.scrivi(corpo)
            if self.path != "/eSCL/ScanJobs":
                return self.manda(404)
            with sc.m:
                sc.n += 1
                ident = str(sc.n)
                sc.lavori[ident] = sc.pagine if "<pwg:InputSource>Feeder<" in corpo else 1
            self.manda(201, extra={"Location": f"http://localhost/eSCL/ScanJobs/{ident}"})

        def do_DELETE(self):
            sc.scrivi(f"DELETE {self.path} Host={self.headers.get('Host')}")
            ident = self.path.rsplit("/", 1)[-1]
            with sc.m:
                trovato = sc.lavori.pop(ident, None) is not None
            self.manda(200 if trovato else 404)
    return G


def principale():
    a = argparse.ArgumentParser()
    a.add_argument("--pagine", type=int, default=2)
    a.add_argument("--occupato", type=int, default=0)
    a.add_argument("--jpeg")
    a.add_argument("--registro")
    a.add_argument("--porta", type=int, default=0)
    a.add_argument("--per", type=float, default=120, help="secondi, poi si spegne da solo (mai uno scanner finto orfano)")
    o = a.parse_args()
    jpeg = open(o.jpeg, "rb").read() if o.jpeg else jpeg_finto(1240, 1754)
    srv = ThreadingHTTPServer(("127.0.0.1", o.porta), gestore(Scanner(o.pagine, o.occupato, jpeg, o.registro)))
    print(srv.server_address[1], flush=True)
    if o.per > 0:
        threading.Timer(o.per, lambda: os._exit(0)).start()
    srv.serve_forever()


if __name__ == "__main__":
    principale()
