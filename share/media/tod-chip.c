/* tod-chip.c — decodifica video con il chip del telefono per GStreamer (12 novembre 2026).
 *
 * Il telefono ha un decodificatore video hardware (Qualcomm: c2.qti.avc.decoder…) che Android offre come MediaCodec.
 * L'ffmpeg di Termux lo usa già senza root (h264_mediacodec, hevc_mediacodec, vp9_mediacodec, av1_mediacodec, via NDK),
 * ma gst-libav scarta apposta i decodificatori video hardware. Questo elemento, "todchipdec", li porta in GStreamer:
 * il Lettore (share/media/tod-lettore.py) lo mette davanti a avdec_* alzandone il rango. Misure (S26, 12 novembre): 4K
 * HEVC 206% → 17% di un core, 1080p 15% → 5%.
 *
 * I fotogrammi tornano in memoria (NV12, I420 o P010) e si copiano nel buffer di GStreamer. Si compila in Termux con
 * clang e pkg-config (lib/desktop.sh, media_chip_compila): gstreamer-video-1.0, libavcodec, libavutil.
 */
#include <gst/gst.h>
#include <gst/video/video.h>
#include <libavcodec/avcodec.h>
#include <libavutil/imgutils.h>
#include <libavutil/log.h>
#include <string.h>
#include <dlfcn.h>

#define GST_TYPE_TOD_CHIP (gst_tod_chip_get_type())
G_DECLARE_FINAL_TYPE(GstTodChip, gst_tod_chip, GST, TOD_CHIP, GstVideoDecoder)

struct _GstTodChip {
    GstVideoDecoder parent;
    AVCodecContext *ctx;
    AVPacket *pkt;
    AVFrame *frame;
    GstVideoCodecState *input;
    int out_w, out_h, out_fmt;   /* ultimo formato annunciato a valle */
};

G_DEFINE_TYPE(GstTodChip, gst_tod_chip, GST_TYPE_VIDEO_DECODER)

GST_DEBUG_CATEGORY_STATIC(tod_chip_debug);
#define GST_CAT_DEFAULT tod_chip_debug

static GstStaticPadTemplate sink_t = GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS(
    "video/x-h264, stream-format = (string) { avc, byte-stream }, alignment = (string) au; "
    "video/x-h265, stream-format = (string) { hvc1, hev1, byte-stream }, alignment = (string) au; "
    "video/x-vp9; "
    "video/x-av1, stream-format = (string) obu-stream, alignment = (string) tu"));
static GstStaticPadTemplate src_t = GST_STATIC_PAD_TEMPLATE("src", GST_PAD_SRC, GST_PAD_ALWAYS,
    GST_STATIC_CAPS("video/x-raw, format = (string) { NV12, I420, P010_10LE }"));

static void chiudi(GstTodChip *s) {
    if (s->ctx) avcodec_free_context(&s->ctx);
    s->out_w = s->out_h = 0;
    s->out_fmt = -1;
}

static gboolean start(GstVideoDecoder *d) {
    GstTodChip *s = GST_TOD_CHIP(d);
    s->pkt = av_packet_alloc();
    s->frame = av_frame_alloc();
    s->out_fmt = -1;
    return s->pkt && s->frame;
}

static gboolean stop(GstVideoDecoder *d) {
    GstTodChip *s = GST_TOD_CHIP(d);
    chiudi(s);
    av_packet_free(&s->pkt);
    av_frame_free(&s->frame);
    g_clear_pointer(&s->input, gst_video_codec_state_unref);
    return TRUE;
}

static const char *nome_codec(const gchar *media) {
    if (!strcmp(media, "video/x-h264")) return "h264_mediacodec";
    if (!strcmp(media, "video/x-h265")) return "hevc_mediacodec";
    if (!strcmp(media, "video/x-vp9")) return "vp9_mediacodec";
    if (!strcmp(media, "video/x-av1")) return "av1_mediacodec";
    return NULL;
}

/* Il chip risponde con messaggi binder (quale pacchetto ha consumato, quale fotogramma è pronto): servono fili che li
 * ricevano nel nostro processo. ffmpeg da solo li ha; dentro GStreamer no, e il decodificatore Qualcomm prendeva 8
 * pacchetti e poi si fermava (12 novembre). ABinderProcess_startThreadPool è pubblica nel NDK (libbinder_ndk). */
static void fili_binder(void) {
    static gsize fatto = 0;
    if (!g_once_init_enter(&fatto)) return;
    void *b = dlopen("libbinder_ndk.so", RTLD_NOW | RTLD_GLOBAL);
    if (b) {
        void (*massimo)(uint32_t) = (void (*)(uint32_t))dlsym(b, "ABinderProcess_setThreadPoolMaxThreadCount");
        void (*avvia)(void) = (void (*)(void))dlsym(b, "ABinderProcess_startThreadPool");
        if (massimo) massimo(4);
        if (avvia) avvia();
        GST_INFO("fili binder %s", avvia ? "avviati" : "assenti");
    }
    g_once_init_leave(&fatto, 1);
}

static gboolean set_format(GstVideoDecoder *d, GstVideoCodecState *state) {
    GstTodChip *s = GST_TOD_CHIP(d);
    GstStructure *st = gst_caps_get_structure(state->caps, 0);
    const char *nome = nome_codec(gst_structure_get_name(st));
    const AVCodec *c = nome ? avcodec_find_decoder_by_name(nome) : NULL;
    if (!c) {
        GST_WARNING_OBJECT(s, "nessun decodificatore del chip per %" GST_PTR_FORMAT, state->caps);
        return FALSE;
    }
    chiudi(s);
    fili_binder();
    s->ctx = avcodec_alloc_context3(c);
    if (!s->ctx) return FALSE;
    s->ctx->width = GST_VIDEO_INFO_WIDTH(&state->info);
    s->ctx->height = GST_VIDEO_INFO_HEIGHT(&state->info);
    if (state->codec_data) {   /* avcC / hvcC: SPS e PPS per il chip */
        GstMapInfo m;
        if (gst_buffer_map(state->codec_data, &m, GST_MAP_READ)) {
            s->ctx->extradata = av_mallocz(m.size + AV_INPUT_BUFFER_PADDING_SIZE);
            memcpy(s->ctx->extradata, m.data, m.size);
            s->ctx->extradata_size = (int)m.size;
            gst_buffer_unmap(state->codec_data, &m);
        }
    }
    s->ctx->pkt_timebase = (AVRational){1, 1000000};   /* µs: il chip vuole i tempi di presentazione veri */
    int r = avcodec_open2(s->ctx, c, NULL);
    if (r < 0) {
        char e[128];
        av_strerror(r, e, sizeof e);
        GST_WARNING_OBJECT(s, "%s non si apre: %s", nome, e);
        chiudi(s);
        return FALSE;
    }
    GST_INFO_OBJECT(s, "decodifica col chip: %s %dx%d", nome, s->ctx->width, s->ctx->height);
    g_clear_pointer(&s->input, gst_video_codec_state_unref);
    s->input = gst_video_codec_state_ref(state);
    return TRUE;
}

static GstVideoFormat formato(int f) {
    switch (f) {
    case AV_PIX_FMT_NV12: return GST_VIDEO_FORMAT_NV12;
    case AV_PIX_FMT_YUV420P: return GST_VIDEO_FORMAT_I420;
    case AV_PIX_FMT_P010LE: return GST_VIDEO_FORMAT_P010_10LE;
    default: return GST_VIDEO_FORMAT_UNKNOWN;
    }
}

/* Il chip vuole il tempo di presentazione vero di ogni pacchetto (con il numero d'ordine al posto del tempo il
 * decodificatore Qualcomm accetta 8 pacchetti e poi si ferma senza restituire nulla, 12 novembre). Il fotogramma
 * decodificato torna con lo stesso tempo: si ritrova il GstVideoCodecFrame dal tempo. Senza tempo: il numero. */
static gint64 tempo_di(GstVideoCodecFrame *f) {
    return GST_CLOCK_TIME_IS_VALID(f->pts) ? (gint64)(f->pts / 1000) : -1 - (gint64)f->system_frame_number;
}

static GstVideoCodecFrame *per_tempo(GstVideoDecoder *d, gint64 t) {
    GList *l = gst_video_decoder_get_frames(d), *i;
    GstVideoCodecFrame *trovato = NULL, *primo = NULL;
    for (i = l; i; i = i->next) {
        GstVideoCodecFrame *f = i->data;
        if (!trovato && tempo_di(f) == t) trovato = gst_video_codec_frame_ref(f);
        if (!primo) primo = f;
    }
    if (!trovato && primo) trovato = gst_video_codec_frame_ref(primo);   /* tempo perso dal chip: il più vecchio */
    g_list_free_full(l, (GDestroyNotify)gst_video_codec_frame_unref);
    return trovato;
}

/* un fotogramma decodificato → il GstVideoCodecFrame con lo stesso tempo */
static GstFlowReturn consegna(GstTodChip *s) {
    GstVideoDecoder *d = GST_VIDEO_DECODER(s);
    for (;;) {
        int r = avcodec_receive_frame(s->ctx, s->frame);
        GST_LOG_OBJECT(s, "receive_frame: %d", r);
        if (r == AVERROR(EAGAIN) || r == AVERROR_EOF) return GST_FLOW_OK;
        if (r < 0) {
            GST_VIDEO_DECODER_ERROR(d, 1, STREAM, DECODE, ("decodifica col chip non riuscita"), (NULL), r);
            return GST_FLOW_OK;
        }
        GstVideoFormat vf = formato(s->frame->format);
        GstVideoCodecFrame *f = per_tempo(d, s->frame->pts);
        if (vf == GST_VIDEO_FORMAT_UNKNOWN) {
            GST_ELEMENT_ERROR(s, STREAM, DECODE, ("formato dei fotogrammi del chip sconosciuto (%d)", s->frame->format), (NULL));
            av_frame_unref(s->frame);
            if (f) gst_video_decoder_drop_frame(d, f);
            return GST_FLOW_ERROR;
        }
        if (!f) {   /* il fotogramma non c'è più (salto): si scarta */
            av_frame_unref(s->frame);
            continue;
        }
        if (vf != (GstVideoFormat)s->out_fmt || s->frame->width != s->out_w || s->frame->height != s->out_h) {
            GstVideoCodecState *o = gst_video_decoder_set_output_state(d, vf, s->frame->width, s->frame->height, s->input);
            gst_video_codec_state_unref(o);
            if (!gst_video_decoder_negotiate(d)) {
                av_frame_unref(s->frame);
                gst_video_decoder_drop_frame(d, f);
                return GST_FLOW_NOT_NEGOTIATED;
            }
            s->out_fmt = vf, s->out_w = s->frame->width, s->out_h = s->frame->height;
        }
        GstFlowReturn fr = gst_video_decoder_allocate_output_frame(d, f);
        if (fr != GST_FLOW_OK) {
            av_frame_unref(s->frame);
            gst_video_decoder_drop_frame(d, f);
            return fr;
        }
        GstVideoInfo info;
        gst_video_info_set_format(&info, vf, s->frame->width, s->frame->height);
        GstVideoFrame vfr;
        if (gst_video_frame_map(&vfr, &info, f->output_buffer, GST_MAP_WRITE)) {
            for (guint p = 0; p < GST_VIDEO_FRAME_N_PLANES(&vfr); p++) {
                guint8 *dst = GST_VIDEO_FRAME_PLANE_DATA(&vfr, p);
                gint ds = GST_VIDEO_FRAME_PLANE_STRIDE(&vfr, p);
                gint righe = GST_VIDEO_FRAME_COMP_HEIGHT(&vfr, p == 0 ? 0 : (vf == GST_VIDEO_FORMAT_I420 ? (gint)p : 1));
                gint larga = GST_VIDEO_FRAME_COMP_WIDTH(&vfr, p == 0 ? 0 : (vf == GST_VIDEO_FORMAT_I420 ? (gint)p : 1)) *
                             GST_VIDEO_FRAME_COMP_PSTRIDE(&vfr, p == 0 ? 0 : (vf == GST_VIDEO_FORMAT_I420 ? (gint)p : 1));
                const uint8_t *src = s->frame->data[p];
                int ss = s->frame->linesize[p];
                for (gint y = 0; y < righe; y++) memcpy(dst + (gsize)y * ds, src + (gsize)y * ss, (gsize)MIN(larga, ss));
            }
            gst_video_frame_unmap(&vfr);
        }
        av_frame_unref(s->frame);
        fr = gst_video_decoder_finish_frame(d, f);
        if (fr != GST_FLOW_OK) return fr;
    }
}

static GstFlowReturn handle_frame(GstVideoDecoder *d, GstVideoCodecFrame *f) {
    GstTodChip *s = GST_TOD_CHIP(d);
    if (!s->ctx) {
        gst_video_decoder_drop_frame(d, f);
        return GST_FLOW_NOT_NEGOTIATED;
    }
    GstMapInfo m;
    if (!gst_buffer_map(f->input_buffer, &m, GST_MAP_READ)) {
        gst_video_decoder_drop_frame(d, f);
        return GST_FLOW_ERROR;
    }
    av_packet_unref(s->pkt);
    if (av_new_packet(s->pkt, (int)m.size) < 0) {
        gst_buffer_unmap(f->input_buffer, &m);
        gst_video_decoder_drop_frame(d, f);
        return GST_FLOW_ERROR;
    }
    memcpy(s->pkt->data, m.data, m.size);
    gst_buffer_unmap(f->input_buffer, &m);
    s->pkt->pts = tempo_di(f);
    s->pkt->dts = GST_CLOCK_TIME_IS_VALID(f->dts) ? (gint64)(f->dts / 1000) : AV_NOPTS_VALUE;
    if (GST_VIDEO_CODEC_FRAME_IS_SYNC_POINT(f)) s->pkt->flags |= AV_PKT_FLAG_KEY;
    guint32 numero = f->system_frame_number;
    gst_video_codec_frame_unref(f);   /* lo riprende consegna() quando il chip lo restituisce */
    GstFlowReturn fr = GST_FLOW_OK;
    for (;;) {
        int r = avcodec_send_packet(s->ctx, s->pkt);
        GST_LOG_OBJECT(s, "send_packet %u (%d byte): %d", numero, s->pkt->size, r);
        if (r == AVERROR(EAGAIN)) {   /* il chip è pieno: prima si ritirano i fotogrammi pronti */
            fr = consegna(s);
            if (fr != GST_FLOW_OK) return fr;
            g_usleep(1000);
            continue;
        }
        if (r < 0) {
            GST_WARNING_OBJECT(s, "pacchetto %u rifiutato dal chip (%d)", numero, r);
            GstVideoCodecFrame *x = gst_video_decoder_get_frame(d, (int)numero);
            if (x) gst_video_decoder_drop_frame(d, x);   /* (qui il numero vale: il pacchetto non è entrato nel chip) */
            return GST_FLOW_OK;
        }
        break;
    }
    return consegna(s);
}

static void svuota(GstTodChip *s) {
    if (!s->ctx) return;
    av_packet_unref(s->pkt);
    if (avcodec_send_packet(s->ctx, NULL) == 0) {
        for (int i = 0; i < 400; i++) {   /* il chip restituisce gli ultimi fotogrammi un po' alla volta */
            int r = avcodec_receive_frame(s->ctx, s->frame);
            if (r == AVERROR_EOF) break;
            if (r == AVERROR(EAGAIN)) { g_usleep(2000); continue; }
            if (r < 0) break;
            /* un fotogramma: si rimette in coda e lo consegna la funzione di sempre */
            GstVideoCodecFrame *f = gst_video_decoder_get_frame(GST_VIDEO_DECODER(s), (int)s->frame->pts);
            if (f) {
                gst_video_codec_frame_unref(f);
                /* consegna() rilegge dal decodificatore: qui basta usare il fotogramma già preso */
            }
            av_frame_unref(s->frame);
        }
    }
    avcodec_flush_buffers(s->ctx);
}

static GstFlowReturn finish(GstVideoDecoder *d) {
    GstTodChip *s = GST_TOD_CHIP(d);
    if (s->ctx && avcodec_send_packet(s->ctx, NULL) == 0) {
        for (int i = 0; i < 400; i++) {
            GstFlowReturn fr = consegna(s);
            if (fr != GST_FLOW_OK) return fr;
            if (!gst_video_decoder_get_frames(d)) break;
            g_usleep(2000);
        }
        avcodec_flush_buffers(s->ctx);
    }
    return GST_FLOW_OK;
}

static gboolean flush(GstVideoDecoder *d) {
    GstTodChip *s = GST_TOD_CHIP(d);
    if (s->ctx) avcodec_flush_buffers(s->ctx);
    return TRUE;
}

static void gst_tod_chip_class_init(GstTodChipClass *k) {
    GstElementClass *e = GST_ELEMENT_CLASS(k);
    GstVideoDecoderClass *v = GST_VIDEO_DECODER_CLASS(k);
    gst_element_class_add_static_pad_template(e, &sink_t);
    gst_element_class_add_static_pad_template(e, &src_t);
    gst_element_class_set_static_metadata(e, "Decodifica col chip del telefono", "Codec/Decoder/Video/Hardware",
                                          "H.264, HEVC, VP9 e AV1 con MediaCodec di Android (via ffmpeg)",
                                          "Termux On DeX");
    v->start = start;
    v->stop = stop;
    v->set_format = set_format;
    v->handle_frame = handle_frame;
    v->finish = finish;
    v->flush = flush;
    (void)svuota;
}

static void gst_tod_chip_init(GstTodChip *s) {
    gst_video_decoder_set_packetized(GST_VIDEO_DECODER(s), TRUE);
    gst_video_decoder_set_needs_format(GST_VIDEO_DECODER(s), TRUE);
}

static gboolean plugin_init(GstPlugin *p) {
    GST_DEBUG_CATEGORY_INIT(tod_chip_debug, "todchipdec", 0, "decodifica col chip");
    if (g_getenv("TOD_CHIP_DEBUG")) av_log_set_level(AV_LOG_TRACE);   /* il registro di ffmpeg, per la diagnosi */
    /* rango basso: lo alza il Lettore (GST_PLUGIN_FEATURE_RANK), così le altre app non lo prendono senza volerlo */
    return gst_element_register(p, "todchipdec", GST_RANK_NONE, GST_TYPE_TOD_CHIP);
}

#define PACKAGE "tod-chip"
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, todchip, "Video col chip del telefono", plugin_init, "1.0", "LGPL",
                  PACKAGE, "https://github.com/PietroCav501/termux_on_dex")
