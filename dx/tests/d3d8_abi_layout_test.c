/* Compile the generated ABI as both C11 and C++ and verify guest plain data. */
#include "d3d8_abi.h"
#include <stddef.h>
#ifdef __cplusplus
#define ABI_ASSERT static_assert
#else
#define ABI_ASSERT _Static_assert
#endif
ABI_ASSERT(sizeof(D3d8Error) == 260, "diagnostic ABI");
ABI_ASSERT(sizeof(D3d8Status) == 4, "status ABI");
ABI_ASSERT(offsetof(D3d8Error, message) == 4, "diagnostic offset");
ABI_ASSERT(sizeof(D3d8AdapterInfo) == 152, "adapter ABI");
ABI_ASSERT(sizeof(D3d8Matrix) == 64, "matrix ABI");
ABI_ASSERT(sizeof(D3d8ColorValue) == 16, "color ABI");
ABI_ASSERT(sizeof(D3d8Material) == 68, "material ABI");
ABI_ASSERT(offsetof(D3d8Material, power) == 64, "material power offset");
ABI_ASSERT(sizeof(D3d8Light) == 104, "light ABI");
ABI_ASSERT(offsetof(D3d8Light, direction) == 64, "light direction offset");
ABI_ASSERT(offsetof(D3d8Light, phi) == 100, "light phi offset");
ABI_ASSERT(sizeof(D3d8LevelLayout) == 20, "mip layout ABI");
ABI_ASSERT(D3D8_STATUS_OUT_OF_MEMORY == 5, "status values");
