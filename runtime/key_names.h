#pragma once
#include <stdint.h>
#include <string>

using GuestKeyNameProvider = std::string (*)(uint32_t dik);

void guest_key_names_set_provider(GuestKeyNameProvider provider);
void guest_key_names_use_system(bool enabled);
bool guest_key_names_system();
std::string guest_key_name(uint32_t dik);
