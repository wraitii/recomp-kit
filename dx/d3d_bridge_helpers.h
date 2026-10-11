#pragma once

#include "d3d_bridge.h"

#include <cstdint>
#include <memory>

struct D3dHostDevice {
    rust::Box<recomp::d3d::Device> value;

    D3dHostDevice() : value(recomp::d3d::new_device()) {}
};

struct D3dHostStorage {
    rust::Box<recomp::d3d::Storage> value;

    D3dHostStorage() : value(recomp::d3d::new_storage()) {}

    rust::Slice<const uint8_t> bytes() const {
        return recomp::d3d::storage_bytes(*value);
    }

    rust::Slice<uint8_t> bytes_mut() {
        return recomp::d3d::storage_bytes_mut(*value);
    }
};

struct D3dHostResource {
    rust::Box<recomp::d3d::Resource> value;

    D3dHostResource() : value(recomp::d3d::new_resource()) {}

    void set_flags(uint32_t usage, uint32_t pool, bool depth, bool gpu_target, uint32_t buffer_fvf,
                   uint32_t index_format, uint32_t buffer_usage, uint32_t buffer_pool) {
        recomp::d3d::set_resource_flags(*value, usage, pool, depth, gpu_target, buffer_fvf,
                                        index_format, buffer_usage, buffer_pool);
    }

    recomp::d3d::ResourceInfo info() const {
        return recomp::d3d::resource_info(*value);
    }

    recomp::d3d::ValueResult set_lod(uint32_t lod) {
        return recomp::d3d::set_resource_lod(*value, lod);
    }
    recomp::d3d::ValueResult set_priority(uint32_t priority) {
        return recomp::d3d::set_resource_priority(*value, priority);
    }
    void set_generation(uint64_t generation) {
        recomp::d3d::set_resource_generation(*value, generation);
    }
    uint64_t generation() const {
        return recomp::d3d::resource_generation(*value);
    }

    recomp::d3d::Status set_level(uint32_t level, uint32_t width, uint32_t height, uint32_t pitch,
                                  uint32_t byte_len, uint64_t generation) {
        return recomp::d3d::set_resource_level(*value, level, width, height, pitch, byte_len,
                                               generation);
    }

    recomp::d3d::ResourceLevelResult level(uint32_t index) const {
        return recomp::d3d::resource_level(*value, index);
    }

    recomp::d3d::ResourceConfigurationResult configure_texture(uint32_t width, uint32_t height,
                                                               uint32_t levels, uint32_t format) {
        return recomp::d3d::configure_texture_resource(*value, width, height, levels, format);
    }

    uint64_t bump_generation() {
        return recomp::d3d::bump_resource_generation(*value);
    }
    recomp::d3d::ResourceGenerationResult bump_level_generation(uint32_t level) {
        return recomp::d3d::bump_resource_level_generation(*value, level);
    }
};

struct D3dHostDevice7State {
    rust::Box<recomp::d3d::Device7State> value;
    uint32_t d3d_obj = 0;
    uint8_t device_guid[16] = {};
    bool tnl = false;

    D3dHostDevice7State() : value(recomp::d3d::new_device7_state()) {}
};

inline bool d3d_bridge_ok(const recomp::d3d::Status &status) {
    return status.code == recomp::d3d::StatusCode::Ok;
}
