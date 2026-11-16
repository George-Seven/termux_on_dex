# La modifica 0011 di Termux, accesa con -D__TERMUX__, non compila: la chiamata generica usa
# vkr_gbm_get_fd_info_from_allocation_info, che nel ramo per Android non è definita (lì la funzione si
# chiama vkr_get_fd_info_from_allocation_info). Si aggiunge quella mancante, che passa la mano.
import sys
f = sys.argv[1]
s = open(f).read()
segno = "/* tod: funzione mancante nel ramo per Android */"
if segno in s:
    sys.exit(0)
a = """#else
static inline VkResult
vkr_gbm_get_fd_info_from_allocation_info(UNUSED struct vkr_physical_device *physical_dev,
                                         UNUSED const VkMemoryAllocateInfo *alloc_info,"""
assert s.count(a) == 1
b = segno + """
static inline VkResult
vkr_gbm_get_fd_info_from_allocation_info(struct vkr_physical_device *physical_dev,
                                         const VkMemoryAllocateInfo *alloc_info,
                                         void **out_gbm_bo,
                                         VkImportMemoryFdInfoKHR *out_fd_info)
{
   return vkr_get_fd_info_from_allocation_info(physical_dev, alloc_info, out_gbm_bo,
                                               out_fd_info);
}
""" + a
open(f, "w").write(s.replace(a, b))
