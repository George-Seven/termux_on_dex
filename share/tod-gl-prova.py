#!/usr/bin/env python3
"""tod-gl-prova.py — un profilo GPU disegna davvero? (16 novembre 2026)

`glxinfo` dice solo che il driver si apre. Provato il 16 novembre: virgl con ANGLE-GL supera glxinfo e poi ogni disegno
fallisce ("failed to dispatch DRAW_VBO"); zink sopra un Vulkan senza presentazione dà il nome giusto e poi "could not
create swapchain". Su un telefono mai provato (Mali, Xclipse, PowerVR) un profilo così vuol dire finestre vuote.
Qui si disegna un triangolo con uno shader e un buffer di vertici, prima su una texture e poi su una finestra vera
(fuori dallo schermo: non si vede), e si rileggono i punti.

Solo ctypes, libGL e libX11: gira col python3 di Termux e con quello di Debian nel container.
  RENDERER=<nome>      il driver che ha risposto
  TEXTURE=ok|ko        disegno su una texture (il motore del driver)
  FINESTRA=ok|ko       disegno sulla finestra e scambio dei fotogrammi (la strada verso lo schermo)
  ESITO=ok|ko|incerto
Esce con 0 = disegna, 1 = non disegna, 2 = non si è potuto provare (librerie, schermo, contesto): chi chiama decide.
"""
import ctypes
import ctypes.util
import os
import sys

LATO = 64
VERDE = (0, 255, 0)
VS = """#version 120
attribute vec2 p;
uniform mat4 m;
uniform vec3 luce;
varying vec3 n;
varying vec2 uv;
varying float forza;
void main() {
    vec4 q = m * vec4(p, 0.0, 1.0);
    n = normalize(mat3(m) * vec3(p, 1.0));
    uv = p * 0.5 + 0.5;
    forza = pow(max(dot(n, normalize(luce)), 0.0), 8.0);
    gl_Position = q;
}
"""
FS = """#version 120
uniform sampler2D t;
varying vec3 n;
varying vec2 uv;
varying float forza;
void main() {
    vec4 c = texture2D(t, uv);
    float poco = (forza + length(n) + c.r) * 0.0;
    gl_FragColor = vec4(poco, c.g, poco, 1.0);
}
"""


def colore_giusto(rgba, atteso=VERDE, scarto=24):
    """il punto riletto è del colore disegnato? (i driver arrotondano: un po' di scarto)"""
    return all(abs(int(a) - b) <= scarto for a, b in zip(rgba[:3], atteso))


def esito(texture, finestra):
    """dai due disegni (True, False o None = non provato) all'esito e al codice d'uscita"""
    if texture is False or finestra is False:
        return "ko", 1
    if texture is None and finestra is None:
        return "incerto", 2
    return "ok", 0


def _carica(nomi):
    for n in nomi:
        if not n:
            continue
        try:
            return ctypes.CDLL(n)
        except OSError:
            pass
    return None


class _Attributi(ctypes.Structure):   # XSetWindowAttributes
    _fields_ = [("background_pixmap", ctypes.c_ulong), ("background_pixel", ctypes.c_ulong),
                ("border_pixmap", ctypes.c_ulong), ("border_pixel", ctypes.c_ulong),
                ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int),
                ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong), ("save_under", ctypes.c_int),
                ("event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
                ("override_redirect", ctypes.c_int), ("colormap", ctypes.c_ulong), ("cursor", ctypes.c_ulong)]


class _Visuale(ctypes.Structure):     # XVisualInfo
    _fields_ = [("visual", ctypes.c_void_p), ("visualid", ctypes.c_ulong), ("screen", ctypes.c_int),
                ("depth", ctypes.c_int), ("klass", ctypes.c_int), ("red_mask", ctypes.c_ulong),
                ("green_mask", ctypes.c_ulong), ("blue_mask", ctypes.c_ulong), ("colormap_size", ctypes.c_int),
                ("bits_per_rgb", ctypes.c_int)]


def prova():
    P, V, I, U = ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_ulong
    x = _carica([ctypes.util.find_library("X11"), "libX11.so.6", "libX11.so"])
    gl = _carica([ctypes.util.find_library("GL"), "libGL.so.1", "libGL.so"])
    if x is None or gl is None:
        print("MOTIVO=libGL o libX11 mancanti")
        return None, None
    try:
        x.XOpenDisplay.restype, x.XOpenDisplay.argtypes = P, [ctypes.c_char_p]
        x.XDefaultScreen.argtypes = [P]
        x.XRootWindow.restype, x.XRootWindow.argtypes = U, [P, I]
        x.XCreateColormap.restype, x.XCreateColormap.argtypes = U, [P, U, V, I]
        x.XCreateWindow.restype = U
        x.XCreateWindow.argtypes = [P, U, I, I, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, I, ctypes.c_uint, V, U,
                                    ctypes.POINTER(_Attributi)]
        x.XMapWindow.argtypes = [P, U]
        x.XSync.argtypes = [P, I]
        x.XDestroyWindow.argtypes = [P, U]
        x.XCloseDisplay.argtypes = [P]
        gl.glXChooseFBConfig.restype = ctypes.POINTER(ctypes.c_void_p)
        gl.glXChooseFBConfig.argtypes = [P, I, ctypes.POINTER(I), ctypes.POINTER(I)]
        gl.glXGetVisualFromFBConfig.restype = ctypes.POINTER(_Visuale)
        gl.glXGetVisualFromFBConfig.argtypes = [P, V]
        gl.glXCreateNewContext.restype, gl.glXCreateNewContext.argtypes = P, [P, V, I, P, I]
        gl.glXMakeCurrent.argtypes = [P, U, P]
        gl.glXSwapBuffers.argtypes = [P, U]
        gl.glXDestroyContext.argtypes = [P, P]
        gl.glGetString.restype, gl.glGetString.argtypes = ctypes.c_char_p, [ctypes.c_uint]
        gl.glShaderSource.argtypes = [ctypes.c_uint, I, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(I)]
        gl.glGetShaderiv.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(I)]
        gl.glGetProgramiv.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(I)]
        gl.glBindAttribLocation.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_char_p]
        gl.glGenBuffers.argtypes = [I, ctypes.POINTER(ctypes.c_uint)]
        gl.glGenTextures.argtypes = [I, ctypes.POINTER(ctypes.c_uint)]
        gl.glGenFramebuffers.argtypes = [I, ctypes.POINTER(ctypes.c_uint)]
        gl.glBufferData.argtypes = [ctypes.c_uint, ctypes.c_ssize_t, V, ctypes.c_uint]
        gl.glVertexAttribPointer.argtypes = [ctypes.c_uint, I, ctypes.c_uint, ctypes.c_ubyte, I, V]
        gl.glTexImage2D.argtypes = [ctypes.c_uint, I, I, I, I, I, ctypes.c_uint, ctypes.c_uint, V]
        gl.glClearColor.argtypes = [ctypes.c_float] * 4
        gl.glGetUniformLocation.argtypes = [ctypes.c_uint, ctypes.c_char_p]
        gl.glUniformMatrix4fv.argtypes = [I, I, ctypes.c_ubyte, V]
        gl.glUniform3f.argtypes = [I, ctypes.c_float, ctypes.c_float, ctypes.c_float]
        gl.glReadPixels.argtypes = [I, I, I, I, ctypes.c_uint, ctypes.c_uint, V]
    except AttributeError as e:
        print(f"MOTIVO=funzione mancante ({e})")
        return None, None

    # un errore di X (BadMatch, BadWindow...) chiuderebbe il programma: lo si conta e si va avanti
    errori = []
    gestore = ctypes.CFUNCTYPE(I, P, P)(lambda d, e: errori.append(1) or 0)
    x.XSetErrorHandler(gestore)

    d = x.XOpenDisplay(None)
    if not d:
        print("MOTIVO=nessuno schermo (DISPLAY)")
        return None, None
    schermo = x.XDefaultScreen(d)
    # GLX_X_RENDERABLE, GLX_DRAWABLE_TYPE=finestra, GLX_RENDER_TYPE=RGBA, 8 bit per colore, doppio buffer
    voglio = (I * 15)(0x8012, 1, 0x8010, 1, 0x8011, 1, 8, 8, 9, 8, 10, 8, 5, 1, 0)
    quanti = I(0)
    conf = gl.glXChooseFBConfig(d, schermo, voglio, ctypes.byref(quanti))
    if not conf or quanti.value < 1:
        print("MOTIVO=nessuna configurazione GLX a colori con doppio buffer")
        return None, None
    vis = gl.glXGetVisualFromFBConfig(d, conf[0])
    if not vis:
        print("MOTIVO=nessuna visuale per la configurazione GLX")
        return None, None
    radice = x.XRootWindow(d, schermo)
    att = _Attributi()
    att.override_redirect = 1   # il gestore delle finestre non la tocca: niente cornice, niente barra
    att.colormap = x.XCreateColormap(d, radice, vis.contents.visual, 0)
    # fuori dallo schermo: esiste ed è mappata (serve per la presentazione), ma non si vede
    fin = x.XCreateWindow(d, radice, -LATO * 4, -LATO * 4, LATO, LATO, 0, vis.contents.depth, 1,
                          vis.contents.visual, (1 << 3) | (1 << 9) | (1 << 13), ctypes.byref(att))
    x.XMapWindow(d, fin)
    x.XSync(d, 0)
    ctx = gl.glXCreateNewContext(d, conf[0], 0x8014, None, 1)
    if not ctx or not gl.glXMakeCurrent(d, fin, ctx):
        print("MOTIVO=il contesto OpenGL non si crea")
        return None, None
    nome = gl.glGetString(0x1F01)
    print(f"RENDERER={(nome or b'').decode('utf-8', 'replace')}")

    def shader(tipo, testo):
        s = gl.glCreateShader(tipo)
        sorgente = ctypes.c_char_p(testo.encode())
        gl.glShaderSource(s, 1, ctypes.byref(sorgente), None)
        gl.glCompileShader(s)
        fatto = I(0)
        gl.glGetShaderiv(s, 0x8B81, ctypes.byref(fatto))
        return s if fatto.value else None

    # shader "da app vera" (matrice, luce, texture), non il minimo: è lì che un driver a metà si tradisce (il
    # traduttore di virgl verso un GLES senza gpu_shader5 fallisce su questi e non su un colore fisso)
    vs = shader(0x8B31, VS)
    fs = shader(0x8B30, FS)
    if vs is None or fs is None:
        print("MOTIVO=gli shader non si compilano")
        return False, None
    prog = gl.glCreateProgram()
    gl.glAttachShader(prog, vs)
    gl.glAttachShader(prog, fs)
    gl.glBindAttribLocation(prog, 0, b"p")
    gl.glLinkProgram(prog)
    fatto = I(0)
    gl.glGetProgramiv(prog, 0x8B82, ctypes.byref(fatto))
    if not fatto.value:
        print("MOTIVO=il programma degli shader non si collega")
        return False, None
    gl.glUseProgram(prog)
    identita = (ctypes.c_float * 16)(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
    gl.glUniformMatrix4fv(gl.glGetUniformLocation(prog, b"m"), 1, 0, ctypes.cast(identita, V))
    gl.glUniform3f(gl.glGetUniformLocation(prog, b"luce"), 0.3, 0.5, 1.0)
    gl.glUniform1i(gl.glGetUniformLocation(prog, b"t"), 0)
    # la texture da cui lo shader prende il verde: 2x2 punti verdi
    verde = (ctypes.c_ubyte * 16)(*([0, 255, 0, 255] * 4))
    sorgente_tex = ctypes.c_uint(0)
    gl.glGenTextures(1, ctypes.byref(sorgente_tex))
    gl.glBindTexture(0x0DE1, sorgente_tex)
    gl.glTexImage2D(0x0DE1, 0, 0x8058, 2, 2, 0, 0x1908, 0x1401, ctypes.cast(verde, V))
    gl.glTexParameteri(0x0DE1, 0x2801, 0x2600)
    gl.glTexParameteri(0x0DE1, 0x2800, 0x2600)
    # un triangolo più grande del riquadro: lo copre tutto
    vertici = (ctypes.c_float * 6)(-1.0, -1.0, 3.0, -1.0, -1.0, 3.0)
    buf = ctypes.c_uint(0)
    gl.glGenBuffers(1, ctypes.byref(buf))
    gl.glBindBuffer(0x8892, buf)
    gl.glBufferData(0x8892, ctypes.sizeof(vertici), ctypes.cast(vertici, V), 0x88E4)
    gl.glEnableVertexAttribArray(0)
    gl.glVertexAttribPointer(0, 2, 0x1406, 0, 0, None)
    gl.glPixelStorei(0x0D05, 1)

    def disegna_e_leggi():
        gl.glViewport(0, 0, LATO, LATO)
        gl.glClearColor(1.0, 0.0, 0.0, 1.0)   # rosso: se il triangolo non arriva, si legge questo
        gl.glClear(0x4000)
        gl.glDrawArrays(4, 0, 3)
        gl.glFinish()
        punto = (ctypes.c_ubyte * 4)()
        gl.glReadPixels(LATO // 2, LATO // 2, 1, 1, 0x1908, 0x1401, ctypes.cast(punto, V))
        return tuple(punto)

    while gl.glGetError():
        pass
    # 1) su una texture
    tex, fbo = ctypes.c_uint(0), ctypes.c_uint(0)
    gl.glGenTextures(1, ctypes.byref(tex))
    gl.glBindTexture(0x0DE1, tex)
    gl.glTexImage2D(0x0DE1, 0, 0x8058, LATO, LATO, 0, 0x1908, 0x1401, None)
    gl.glTexParameteri(0x0DE1, 0x2801, 0x2600)
    gl.glTexParameteri(0x0DE1, 0x2800, 0x2600)
    gl.glGenFramebuffers(1, ctypes.byref(fbo))
    gl.glBindFramebuffer(0x8D40, fbo)
    gl.glFramebufferTexture2D(0x8D40, 0x8CE0, 0x0DE1, tex, 0)
    gl.glBindTexture(0x0DE1, sorgente_tex)   # si legge dalla verde, non da quella su cui si disegna
    if gl.glCheckFramebufferStatus(0x8D40) != 0x8CD5:
        print("MOTIVO=la texture di destinazione non è accettata")
        texture = False
    else:
        p = disegna_e_leggi()
        texture = colore_giusto(p) and gl.glGetError() == 0
        print(f"TEXTURE={'ok' if texture else 'ko'} {p[0]},{p[1]},{p[2]}")
    # 2) sulla finestra, con lo scambio dei fotogrammi (due volte: il secondo fotogramma riusa i buffer)
    gl.glBindFramebuffer(0x8D40, 0)
    while gl.glGetError():
        pass
    del errori[:]
    finestra = True
    for _giro in (1, 2):
        p = disegna_e_leggi()
        if not colore_giusto(p) or gl.glGetError() != 0:
            finestra = False
        gl.glXSwapBuffers(d, fin)
        x.XSync(d, 0)
    # gli errori di X da soli non bocciano: fuori dallo schermo i driver che copiano col processore (virpipe di
    # Debian) ricevono BadMatch da GetImage anche quando disegnano bene. Conta il punto riletto.
    print(f"FINESTRA={'ok' if finestra else 'ko'} {p[0]},{p[1]},{p[2]}" + (f" errori X: {len(errori)}" if errori else ""))
    gl.glXMakeCurrent(d, 0, None)
    gl.glXDestroyContext(d, ctx)
    x.XDestroyWindow(d, fin)
    x.XCloseDisplay(d)
    return texture, finestra


def main():
    try:
        texture, finestra = prova()
    except Exception as e:   # una prova non deve mai far cadere chi la chiama
        print(f"MOTIVO={type(e).__name__}: {e}")
        texture = finestra = None
    parola, codice = esito(texture, finestra)
    print(f"ESITO={parola}")
    sys.stdout.flush()
    # niente pulizie di Python: alcuni driver cadono alla chiusura, e l'esito è già scritto
    os._exit(codice)


if __name__ == "__main__":
    main()
