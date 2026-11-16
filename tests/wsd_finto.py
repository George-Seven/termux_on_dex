#!/usr/bin/env python3
"""Uno scanner WSD finto per le prove (tests/run.sh, test_scanner_wsd), fatto come la Ricoh MP C2011 di Pietro.

  wsd_finto.py [--pagine N] [--occupato K] [--registro FILE] [--per SECONDI]

Su 127.0.0.1: una porta UDP che risponde alla sonda WS-Discovery (ProbeMatch con l'indirizzo del dispositivo) e una
porta HTTP con il dispositivo (WS-Transfer Get → servizio ospitato /scan) e il servizio di scansione
(GetScannerElements, CreateScanJob, RetrieveImage in MTOM, CancelJob). Dal vetro una pagina, dall'alimentatore N, poi
ClientErrorNoImagesAvailable; le prime K CreateScanJob rispondono ServerErrorNotAcceptingJobs. Stampa
"<porta http> <porta udp>"; nel registro una riga per richiesta (azione) e il corpo di CreateScanJob.
"""
import argparse
import os
import re
import socket
import struct
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IDENT = "urn:uuid:fd9b6954-1dd1-11b2-a165-c82258373a8c"
ENV = ('<?xml version="1.0" encoding="UTF-8"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" '
       'xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
       'xmlns:wsdp="http://schemas.xmlsoap.org/ws/2006/02/devprof" xmlns:mex="http://schemas.xmlsoap.org/ws/2004/09/mex" '
       'xmlns:wscn="http://schemas.microsoft.com/windows/2006/08/wdp/scan"><SOAP-ENV:Header>'
       '<wsa:Action>{azione}</wsa:Action><wsa:MessageID>urn:uuid:{mid}</wsa:MessageID></SOAP-ENV:Header>'
       '<SOAP-ENV:Body>{corpo}</SOAP-ENV:Body></SOAP-ENV:Envelope>')
GUASTO = ('<SOAP-ENV:Fault><SOAP-ENV:Code><SOAP-ENV:Value>SOAP-ENV:Receiver</SOAP-ENV:Value><SOAP-ENV:Subcode>'
          '<SOAP-ENV:Value>wscn:{codice}</SOAP-ENV:Value></SOAP-ENV:Subcode></SOAP-ENV:Code><SOAP-ENV:Reason>'
          '<SOAP-ENV:Text xml:lang="en">{codice}</SOAP-ENV:Text></SOAP-ENV:Reason></SOAP-ENV:Fault>')
CONFIG = """<wscn:GetScannerElementsResponse><wscn:ScannerElements>
<wscn:ElementData Name="wscn:ScannerDescription" Valid="true"><wscn:ScannerDescription>
<wscn:ScannerName>RICOH MP C2011</wscn:ScannerName></wscn:ScannerDescription></wscn:ElementData>
<wscn:ElementData Name="wscn:ScannerConfiguration" Valid="true"><wscn:ScannerConfiguration>
<wscn:DeviceSettings><wscn:FormatsSupported><wscn:FormatValue>jfif</wscn:FormatValue><wscn:FormatValue>tiff-single-uncompressed</wscn:FormatValue></wscn:FormatsSupported></wscn:DeviceSettings>
<wscn:Platen><wscn:PlatenColor><wscn:ColorEntry>BlackAndWhite1</wscn:ColorEntry><wscn:ColorEntry>Grayscale8</wscn:ColorEntry><wscn:ColorEntry>RGB24</wscn:ColorEntry></wscn:PlatenColor>
<wscn:PlatenMinimumSize><wscn:Width>1000</wscn:Width><wscn:Height>1000</wscn:Height></wscn:PlatenMinimumSize>
<wscn:PlatenMaximumSize><wscn:Width>11693</wscn:Width><wscn:Height>16535</wscn:Height></wscn:PlatenMaximumSize>
<wscn:PlatenResolutions><wscn:Widths><wscn:Width>100</wscn:Width><wscn:Width>200</wscn:Width><wscn:Width>300</wscn:Width><wscn:Width>600</wscn:Width></wscn:Widths>
<wscn:Heights><wscn:Height>100</wscn:Height><wscn:Height>200</wscn:Height><wscn:Height>300</wscn:Height><wscn:Height>600</wscn:Height></wscn:Heights></wscn:PlatenResolutions></wscn:Platen>
<wscn:ADF><wscn:ADFSupportsDuplex>true</wscn:ADFSupportsDuplex><wscn:ADFFront>
<wscn:ADFColor><wscn:ColorEntry>Grayscale8</wscn:ColorEntry><wscn:ColorEntry>RGB24</wscn:ColorEntry></wscn:ADFColor>
<wscn:ADFMaximumSize><wscn:Width>11693</wscn:Width><wscn:Height>16535</wscn:Height></wscn:ADFMaximumSize>
<wscn:ADFResolutions><wscn:Widths><wscn:Width>200</wscn:Width><wscn:Width>300</wscn:Width></wscn:Widths>
<wscn:Heights><wscn:Height>200</wscn:Height><wscn:Height>300</wscn:Height></wscn:Heights></wscn:ADFResolutions></wscn:ADFFront></wscn:ADF>
</wscn:ScannerConfiguration></wscn:ElementData></wscn:ScannerElements></wscn:GetScannerElementsResponse>"""


def jpeg_finto(larghezza=1240, altezza=1754, dpi=150):
    app0 = b"JFIF\0\x01\x01\x01" + struct.pack(">HH", dpi, dpi) + b"\0\0"
    sof = b"\x08" + struct.pack(">HH", altezza, larghezza) + b"\x03" + b"\x01\x11\x00" * 3
    return (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", len(app0) + 2) + app0
            + b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof + b"\xff\xd9")


class Stato:
    def __init__(self, pagine, occupato, registro):
        self.pagine, self.occupato, self.registro = pagine, occupato, registro
        self.lavori, self.m = {}, threading.Lock()

    def scrivi(self, riga):
        if self.registro:
            with self.m, open(self.registro, "a") as f:
                f.write(riga + "\n")


def gestore(st, porta_di):
    class G(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def manda(self, corpo, azione="", tipo="application/soap+xml", codice=200):
            if isinstance(corpo, str):
                corpo = ENV.format(azione=azione, mid=uuid.uuid4(), corpo=corpo).encode()
            self.send_response(codice)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def guasto(self, codice):
            self.manda(GUASTO.format(codice=codice), "http://schemas.xmlsoap.org/ws/2004/08/addressing/fault", codice=400)

        def do_GET(self):
            self.manda(GUASTO.format(codice="NonSoap"), codice=500)

        def do_POST(self):
            d = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            azione = (re.search(r"Action>([^<]*)<", d) or [None, ""])[1]
            to = (re.search(r"To>([^<]*)<", d) or [None, ""])[1]
            breve = azione.rsplit("/", 1)[-1]
            st.scrivi(f"{self.path} {breve} {to}")
            if breve == "Get" and self.path == "/":
                if to != IDENT:
                    return self.guasto("DestinationUnreachable")
                h = f"http://127.0.0.1:{porta_di()}"
                return self.manda(
                    "<mex:Metadata><mex:MetadataSection Dialect=\"http://schemas.xmlsoap.org/ws/2006/02/devprof/ThisModel\">"
                    "<wsdp:ThisModel><wsdp:Manufacturer>RICOH</wsdp:Manufacturer><wsdp:ModelName>MP C2011</wsdp:ModelName>"
                    "</wsdp:ThisModel></mex:MetadataSection><mex:MetadataSection Dialect=\"http://schemas.xmlsoap.org/ws/2006/02/devprof/ThisDevice\">"
                    "<wsdp:ThisDevice><wsdp:FriendlyName>RICOH MP C2011</wsdp:FriendlyName></wsdp:ThisDevice></mex:MetadataSection>"
                    "<mex:MetadataSection Dialect=\"http://schemas.xmlsoap.org/ws/2006/02/devprof/Relationship\"><wsdp:Relationship>"
                    f"<wsdp:Hosted><wsa:EndpointReference><wsa:Address>{h}/print</wsa:Address></wsa:EndpointReference>"
                    "<wsdp:Types>wprt:PrinterServiceType</wsdp:Types></wsdp:Hosted>"
                    f"<wsdp:Hosted><wsa:EndpointReference><wsa:Address>http://ricoh-finta.local:{porta_di()}/scan</wsa:Address>"
                    "</wsa:EndpointReference><wsdp:Types>wscn:ScannerServiceType</wsdp:Types></wsdp:Hosted>"
                    "</wsdp:Relationship></mex:MetadataSection></mex:Metadata>",
                    "http://schemas.xmlsoap.org/ws/2004/09/transfer/GetResponse")
            if self.path != "/scan":
                return self.guasto("EndpointUnavailable")
            if breve == "GetScannerElements":
                return self.manda(CONFIG, azione + "Response")
            if breve == "CreateScanJob":
                st.scrivi(re.search(r"<soap:Body>(.*)</soap:Body>", d, re.S).group(1))
                if st.occupato > 0:
                    st.occupato -= 1
                    return self.guasto("ServerErrorNotAcceptingJobs")
                fonte = re.search(r"InputSource>([^<]*)<", d).group(1)
                lid = str(len(st.lavori) + 1)
                st.lavori[lid] = 1 if fonte == "Platen" else st.pagine * (2 if fonte == "ADFDuplex" else 1)
                if not st.lavori[lid]:
                    return self.guasto("ClientErrorNoImagesAvailable")
                return self.manda(f"<wscn:CreateScanJobResponse><wscn:JobId>{lid}</wscn:JobId>"
                                  f"<wscn:JobToken>gettone{lid}</wscn:JobToken></wscn:CreateScanJobResponse>", azione + "Response")
            if breve == "RetrieveImage":
                lid = re.search(r"JobId>([^<]*)<", d).group(1)
                if re.search(r"JobToken>([^<]*)<", d).group(1) != f"gettone{lid}":
                    return self.guasto("ClientErrorJobTokenNotValid")
                if st.lavori.get(lid, 0) <= 0:
                    return self.guasto("ClientErrorNoImagesAvailable")
                st.lavori[lid] -= 1
                sep = "MIMEBoundary" + uuid.uuid4().hex
                xml = ENV.format(azione=azione + "Response", mid=uuid.uuid4(),
                                 corpo="<wscn:RetrieveImageResponse><wscn:ScanData><xop:Include xmlns:xop="
                                       "\"http://www.w3.org/2004/08/xop/include\" href=\"cid:immagine\"/></wscn:ScanData>"
                                       "</wscn:RetrieveImageResponse>").encode()
                corpo = (f"--{sep}\r\nContent-Type: application/xop+xml; charset=UTF-8; type=\"application/soap+xml\"\r\n"
                         f"Content-ID: <radice>\r\n\r\n").encode() + xml + \
                        (f"\r\n--{sep}\r\nContent-Type: application/binary\r\nContent-ID: <immagine>\r\n\r\n").encode() + \
                        jpeg_finto() + f"\r\n--{sep}--\r\n".encode()
                return self.manda(corpo, tipo=f'multipart/related; type="application/xop+xml"; boundary="{sep}"; '
                                              'start="<radice>"; start-info="application/soap+xml"')
            if breve == "CancelJob":
                return self.manda("<wscn:CancelJobResponse/>", azione + "Response")
            return self.guasto("ActionNotSupported")
    return G


def risponditore(porta_http):
    """La porta UDP della ricerca: a ogni Probe con ScanDeviceType (o senza tipi) una ProbeMatch."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))

    def giro():
        while True:
            d, da = s.recvfrom(65535)
            t = d.decode(errors="replace")
            if "Probe" not in t or ("Types>" in t and "ScanDeviceType" not in t):
                continue
            s.sendto(ENV.format(azione="http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches", mid=uuid.uuid4(),
                                corpo=f"<wsd:ProbeMatches><wsd:ProbeMatch><wsa:EndpointReference><wsa:Address>{IDENT}"
                                      "</wsa:Address></wsa:EndpointReference><wsd:Types>wsdp:Device wprt:PrintDeviceType "
                                      f"wscn:ScanDeviceType</wsd:Types><wsd:XAddrs>http://127.0.0.1:{porta_http}</wsd:XAddrs>"
                                      "</wsd:ProbeMatch></wsd:ProbeMatches>").encode(), da)
    threading.Thread(target=giro, daemon=True).start()
    return s.getsockname()[1]


def principale():
    a = argparse.ArgumentParser()
    a.add_argument("--pagine", type=int, default=2)
    a.add_argument("--occupato", type=int, default=0)
    a.add_argument("--registro")
    a.add_argument("--per", type=float, default=60, help="secondi, poi si spegne da solo (mai uno scanner finto orfano)")
    o = a.parse_args()
    porta = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gestore(Stato(o.pagine, o.occupato, o.registro), lambda: porta[0]))
    porta.append(srv.server_address[1])
    print(porta[0], risponditore(porta[0]), flush=True)
    if o.per > 0:
        threading.Timer(o.per, lambda: os._exit(0)).start()
    srv.serve_forever()


if __name__ == "__main__":
    principale()
