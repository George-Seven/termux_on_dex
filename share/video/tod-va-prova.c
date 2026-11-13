// tod-va-prova — la prova reale prima di accendere il video col chip nelle app Chromium/Electron (13 novembre 2026,
// lib/videochip.sh). Pietro: "deve funzionare anche per gli smartphone che non hanno Adreno ma hanno Mali".
//
// Il ponte mostra i fotogrammi con ANGLE su Vulkan, importando la memoria dma-buf delle superfici: serve, nel container e
// con lo stesso profilo grafico delle app (si lancia con tod cx run), un Vulkan HARDWARE con l'importazione dma-buf. Su
// Adreno c'è (turnip); su Mali di solito il container ha solo un Vulkan software o nessuno: lì il ponte resta spento e le
// app partono come sempre. Controlla anche /dev/dma_heap/system (la memoria delle superfici).
// Stampa righe leggibili e per ultima "ESITO ok" o "ESITO no: motivo"; esce 0 solo con ok.
// Compilato come il driver: clang di Termux per il glibc del container, senza intestazioni.
#include <stddef.h>
#include <stdint.h>
int printf(const char *, ...);
int open(const char *, int, ...);
int close(int);
int ioctl(int, unsigned long, ...);
void *dlopen(const char *, int);
void *dlsym(void *, const char *);
int strcmp(const char *, const char *);
void *calloc(unsigned long, unsigned long);

static int no(const char *motivo) {
    printf("ESITO no: %s\n", motivo);
    return 1;
}

struct heap { uint64_t len; uint32_t fd, fd_flags; uint64_t heap_flags; };

// --- Vulkan senza intestazioni: solo le strutture che servono ------------------------------------------------------
typedef struct { uint32_t sType; const void *pNext; uint32_t flags; const void *pApplicationInfo; uint32_t enabledLayerCount;
                 const char *const *ppEnabledLayerNames; uint32_t enabledExtensionCount;
                 const char *const *ppEnabledExtensionNames; } VkInstanceCreateInfo;
typedef struct { char extensionName[256]; uint32_t specVersion; } VkExtensionProperties;
typedef int (*PFN_create)(const VkInstanceCreateInfo *, const void *, void **);
typedef int (*PFN_enum_dev)(void *, uint32_t *, void **);
typedef void (*PFN_props)(void *, void *);
typedef int (*PFN_enum_ext)(void *, const char *, uint32_t *, VkExtensionProperties *);
typedef void *(*PFN_gipa)(void *, const char *);

static const char *TIPI[] = {"altro", "integrata", "dedicata", "virtuale", "software (processore)"};
// quello che ANGLE usa per mostrare un fotogramma NV12 che sta in un dma-buf
static const char *SERVONO[] = {"VK_KHR_external_memory_fd", "VK_EXT_external_memory_dma_buf",
                                "VK_EXT_image_drm_format_modifier", "VK_KHR_sampler_ycbcr_conversion"};

int main(void) {
    // 1) la memoria delle superfici
    int h = open("/dev/dma_heap/system", 02000000);
    if (h < 0) return no("/dev/dma_heap/system non si apre");
    struct heap d = {1 << 16, 0, 02 | 02000000, 0};
    if (ioctl(h, 0xc0184800ul, &d) < 0) return no("dma_heap non dà memoria");
    close((int)d.fd);
    close(h);
    printf("dma_heap: ok\n");

    // 2) un Vulkan hardware con l'importazione dma-buf
    void *vk = dlopen("libvulkan.so.1", 2);
    if (!vk) return no("niente Vulkan nel container (libvulkan.so.1)");
    PFN_gipa gipa = (PFN_gipa)dlsym(vk, "vkGetInstanceProcAddr");
    if (!gipa) return no("libvulkan senza vkGetInstanceProcAddr");
    PFN_create crea = (PFN_create)gipa(0, "vkCreateInstance");
    VkInstanceCreateInfo ci = {1 /* VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO */};
    void *ist = 0;
    if (!crea || crea(&ci, 0, &ist) != 0 || !ist) return no("Vulkan non si avvia (nessun driver?)");
    PFN_enum_dev elenca = (PFN_enum_dev)gipa(ist, "vkEnumeratePhysicalDevices");
    PFN_props proprieta = (PFN_props)gipa(ist, "vkGetPhysicalDeviceProperties");
    PFN_enum_ext estensioni = (PFN_enum_ext)gipa(ist, "vkEnumerateDeviceExtensionProperties");
    if (!elenca || !proprieta || !estensioni) return no("Vulkan incompleto");
    uint32_t n = 0;
    void *dev[8];
    if (elenca(ist, &n, 0) != 0 || n == 0) return no("Vulkan senza dispositivi");
    if (n > 8) n = 8;
    elenca(ist, &n, dev);
    for (uint32_t i = 0; i < n; i++) {
        uint8_t p[1024] = {0};   // VkPhysicalDeviceProperties: deviceType a 16, deviceName a 20
        proprieta(dev[i], p);
        uint32_t tipo = *(uint32_t *)(p + 16);
        printf("vulkan: %s (%s)\n", (const char *)(p + 20), tipo < 5 ? TIPI[tipo] : "?");
        if (tipo == 4) continue;   // lavapipe & co.: niente da guadagnare, e ANGLE su Vulkan software è lento
        uint32_t ne = 0;
        estensioni(dev[i], 0, &ne, 0);
        VkExtensionProperties *e = calloc(ne ? ne : 1, sizeof *e);
        if (!e) return no("memoria");
        estensioni(dev[i], 0, &ne, e);
        int mancano = 0;
        for (unsigned s = 0; s < sizeof SERVONO / sizeof *SERVONO; s++) {
            int c = 0;
            for (uint32_t k = 0; k < ne && !c; k++) c = !strcmp(e[k].extensionName, SERVONO[s]);
            if (!c) printf("  manca %s\n", SERVONO[s]), mancano++;
        }
        if (!mancano) {
            printf("ESITO ok\n");
            return 0;
        }
    }
    return no("nessun Vulkan hardware con l'importazione dma-buf (su Mali il container non ha il driver Vulkan del telefono)");
}

int __libc_start_main(int (*)(int, char **, char **), int, char **, void (*)(void), void (*)(void), void (*)(void), void *);
__attribute__((used)) static void parti(long *sp) {
    __libc_start_main((int (*)(int, char **, char **))main, (int)sp[0], (char **)(sp + 1), 0, 0, 0, sp);
}
__asm__(".globl _start\n_start:\n mov x29, #0\n mov x30, #0\n mov x0, sp\n and sp, x0, #-16\n b parti\n");
