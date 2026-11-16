package com.termuxondex;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

/**
 * AppAndroid — le app del telefono aperte, per la Gestione attività (15 novembre 2026; Pietro: "mi serve che gestione
 * attività mi permetta di gestire le app android aperte").
 *
 * Android non mostra a Termux i processi delle altre app (/proc, dumpsys e "am stack" sono per ADB). Con il permesso
 * "Accesso ai dati di utilizzo" dato a Termux (che lo dichiara nel suo manifest) però dice:
 *   - gli eventi delle attività dall'accensione (UsageStatsManager.queryEvents): quali app sono state aperte, quando,
 *     e quali finestre sono visibili adesso;
 *   - lo stato del processo di ogni app (ActivityManager.getUidProcessState): in primo piano, in background, sospesa
 *     (in cache), chiusa.
 * Gira con app_process come utente di Termux, come "am" di Termux (niente ADB, niente root). Usa solo la riflessione,
 * così si compila senza android.jar: share/android/compila.sh (javac + d8) → tod-app-android.dex.
 *
 * Protocollo: una riga su stdin ("stato") → una riga JSON su stdout; finisce quando stdin si chiude.
 *   {"ok":true,"app":[["pacchetto",uid,stato del processo,ultimo evento in ms,visibile 0/1],...]}
 *   {"ok":false,"errore":"permesso"}   (manca l'Accesso ai dati di utilizzo)
 * "stato" come argomento: una risposta sola e fine (per le prove a mano).
 */
public final class AppAndroid {
    static final String NOI = "com.termux";
    // UsageEvents.Event: attività in primo piano, in pausa (visibile), fermata (in background), chiusa (nascosto),
    // accensione del telefono
    static final int RIPRESA = 1, PAUSA = 2, FERMATA = 23, CHIUSA = 24, ACCENSIONE = 27;
    static final int NESSUN_PROCESSO = 20;   // ActivityManager.PROCESS_STATE_NONEXISTENT

    static Object usm, am, pm;
    static Method mEventi, mStato, mUid;
    static int mioUid;
    static Class<?> cEvento;
    static Method eHa, eProssimo, ePacchetto, eClasse, eTempo, eTipo, eIstanza;
    static long dal;   // da qui si chiedono gli eventi (con un po' di sovrapposizione: rileggerli non cambia niente)
    static final Map<String, Long> ultimo = new HashMap<String, Long>();             // pacchetto → ultimo evento
    static final Map<String, Set<String>> visibili = new HashMap<String, Set<String>>();   // pacchetto → attività
    static final Map<String, Integer> uid = new HashMap<String, Integer>();

    public static void main(String[] a) throws Exception {
        if (a.length > 0) {
            System.out.println(rispondi(a[0]));
            System.out.flush();
            return;
        }
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
        String r;
        while ((r = in.readLine()) != null) {
            System.out.println(rispondi(r.trim()));
            System.out.flush();
        }
    }

    static String rispondi(String richiesta) {
        try {
            if (!richiesta.equals("stato")) {
                return "{\"ok\":false,\"errore\":\"richiesta sconosciuta\"}";
            }
            prepara();
            // il permesso si prova con lo stato del nostro processo: senza l'Accesso ai dati di utilizzo Android lancia
            // SecurityException. queryEvents invece dà null anche quando non ci sono eventi nuovi (15 novembre: preso
            // per "permesso", le app del telefono sparivano e tornavano a ogni giro)
            statoDi(mioUid);
            leggiEventi();
            StringBuilder s = new StringBuilder("{\"ok\":true,\"app\":[");
            boolean primo = true;
            for (Map.Entry<String, Long> e : ultimo.entrySet()) {
                String p = e.getKey();
                int u = uidDi(p);
                int st = u < 0 ? NESSUN_PROCESSO : statoDi(u);
                Set<String> v = visibili.get(p);
                if (!primo) {
                    s.append(',');
                }
                primo = false;
                s.append("[\"").append(json(p)).append("\",").append(u).append(',').append(st).append(',')
                        .append(e.getValue()).append(',').append(v != null && !v.isEmpty() ? 1 : 0).append(']');
            }
            return s.append("]}").toString();
        } catch (Throwable e) {
            Throwable c = e;
            while (c instanceof InvocationTargetException && c.getCause() != null) {
                c = c.getCause();
            }
            if (c instanceof SecurityException) {
                return "{\"ok\":false,\"errore\":\"permesso\"}";
            }
            return "{\"ok\":false,\"errore\":\"" + json(c.toString()) + "\"}";
        }
    }

    /** i servizi di Android, una volta sola (le interfacce nascoste: app_process non ha i limiti delle app) */
    static void prepara() throws Exception {
        if (usm != null) {
            return;
        }
        Class<?> binder = Class.forName("android.os.IBinder");
        Object b = Class.forName("android.os.ServiceManager").getMethod("getService", String.class)
                .invoke(null, "usagestats");
        if (b == null) {
            throw new IllegalStateException("servizio usagestats assente");
        }
        Object u = Class.forName("android.app.usage.IUsageStatsManager$Stub").getMethod("asInterface", binder)
                .invoke(null, b);
        mEventi = Class.forName("android.app.usage.IUsageStatsManager")
                .getMethod("queryEvents", long.class, long.class, String.class);
        am = Class.forName("android.app.ActivityManager").getMethod("getService").invoke(null);
        mStato = Class.forName("android.app.IActivityManager").getMethod("getUidProcessState", int.class, String.class);
        pm = Class.forName("android.app.ActivityThread").getMethod("getPackageManager").invoke(null);
        Class<?> ipm = Class.forName("android.content.pm.IPackageManager");
        try {   // Android 13 e dopo: flag long
            mUid = ipm.getMethod("getPackageUid", String.class, long.class, int.class);
        } catch (NoSuchMethodException x) {
            mUid = ipm.getMethod("getPackageUid", String.class, int.class, int.class);
        }
        cEvento = Class.forName("android.app.usage.UsageEvents$Event");
        Class<?> cEventi = Class.forName("android.app.usage.UsageEvents");
        eHa = cEventi.getMethod("hasNextEvent");
        eProssimo = cEventi.getMethod("getNextEvent", cEvento);
        ePacchetto = cEvento.getMethod("getPackageName");
        eClasse = cEvento.getMethod("getClassName");
        eTempo = cEvento.getMethod("getTimeStamp");
        eTipo = cEvento.getMethod("getEventType");
        try {   // Android 10 e dopo: un numero per ogni finestra dell'attività (la stessa attività può essere aperta due volte)
            eIstanza = cEvento.getMethod("getInstanceId");
        } catch (NoSuchMethodException x) {
            eIstanza = null;
        }
        mioUid = (Integer) Class.forName("android.os.Process").getMethod("myUid").invoke(null);
        long acceso = (Long) Class.forName("android.os.SystemClock").getMethod("elapsedRealtime").invoke(null);
        dal = System.currentTimeMillis() - acceso;   // dall'accensione del telefono
        usm = u;
    }

    /** gli eventi nuovi (null da Android = nessuno in questo intervallo) */
    static void leggiEventi() throws Exception {
        long ora = System.currentTimeMillis();
        Object eventi = mEventi.invoke(usm, dal, ora, NOI);
        Object e = cEvento.getConstructor().newInstance();
        while (eventi != null && (Boolean) eHa.invoke(eventi)) {
            eProssimo.invoke(eventi, e);
            int tipo = (Integer) eTipo.invoke(e);
            if (tipo == ACCENSIONE) {
                ultimo.clear();
                visibili.clear();
                continue;
            }
            if (tipo != RIPRESA && tipo != PAUSA && tipo != FERMATA && tipo != CHIUSA) {
                continue;
            }
            String p = (String) ePacchetto.invoke(e);
            if (p == null) {
                continue;
            }
            String chi = eIstanza != null ? String.valueOf(eIstanza.invoke(e)) : String.valueOf(eClasse.invoke(e));
            Set<String> v = visibili.get(p);
            if (v == null) {
                v = new HashSet<String>();
                visibili.put(p, v);
            }
            if (tipo == RIPRESA || tipo == PAUSA) {
                v.add(chi);
            } else {
                v.remove(chi);
            }
            long t = (Long) eTempo.invoke(e);
            Long prima = ultimo.get(p);
            if (prima == null || t > prima) {
                ultimo.put(p, t);
            }
        }
        dal = Math.max(dal, ora - 5000);   // un evento scritto in ritardo non si perde
    }

    static int uidDi(String p) throws Exception {
        Integer u = uid.get(p);
        if (u == null) {
            Object flag = mUid.getParameterTypes()[1] == long.class ? (Object) 0L : (Object) 0;
            u = (Integer) mUid.invoke(pm, p, flag, 0);
            if (u >= 0) {   // un'app non installata (-1) si richiede: può arrivare dopo
                uid.put(p, u);
            }
        }
        return u;
    }

    static int statoDi(int u) throws Exception {
        return (Integer) mStato.invoke(am, u, NOI);
    }

    static String json(String s) {
        StringBuilder b = new StringBuilder();
        for (char c : s.toCharArray()) {
            if (c == '"' || c == '\\') {
                b.append('\\').append(c);
            } else if (c < 0x20) {
                b.append(String.format("\\u%04x", (int) c));
            } else {
                b.append(c);
            }
        }
        return b.toString();
    }
}
