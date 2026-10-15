#pragma once

#include <cstdint>
#include <iterator>

#define RECOMP_ADAPTER_PROFILE_CHOICES                                                             \
    "Actual|Radeon 9800 Pro|GeForce 6800 GT|Intel GMA 950|Radeon X1900 XT|GeForce 8800 "           \
    "GTX|GeForce GTX 480"

struct AdapterProfile {
    const char *driver;
    const char *description;
    uint32_t vendor_id;
    uint32_t device_id;
    uint32_t driver_version_high;
    uint32_t driver_version_low;
};

inline constexpr AdapterProfile kAdapterProfiles[] = {
    {"ati2dvag.dll", "RADEON 9800 PRO", 0x1002, 0x4e48, 0x0006000e, 0x000a1a3b},
    {"nv4_disp.dll", "NVIDIA GeForce 6800 GT", 0x10de, 0x0045, 0x0006000e, 0x000a1bc1},
    {"ialmrnt5.dll", "Mobile Intel(R) 945 Express Chipset Family", 0x8086, 0x27a2, 0x0006000e,
     0x000a133e},
    {"ati2dvag.dll", "Radeon X1900 Series", 0x1002, 0x7249, 0x0006000e, 0x000a1a6c},
    {"nv4_disp.dll", "NVIDIA GeForce 8800 GTX", 0x10de, 0x0191, 0x0006000e, 0x000b1b09},
    {"nv4_disp.dll", "NVIDIA GeForce GTX 480", 0x10de, 0x06c0, 0x0006000e, 0x000c1708},
};

inline int g_adapter_profile = 0;

inline void adapter_profile_select(int choice) {
    g_adapter_profile = choice > 0 && choice <= int(std::size(kAdapterProfiles)) ? choice : 0;
}

inline const AdapterProfile *adapter_profile() {
    return g_adapter_profile ? &kAdapterProfiles[g_adapter_profile - 1] : nullptr;
}
