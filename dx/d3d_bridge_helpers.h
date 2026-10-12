#pragma once

#include "d3d_bridge.h"

#include <cstdint>
#include <map>
#include <memory>

struct D3dHostDevice {
    rust::Box<recomp::d3d::Device> value;

    D3dHostDevice() : value(recomp::d3d::new_device()) {}
};

struct D3d9HostResource;

struct D3d9HostDevice {
    rust::Box<recomp::d3d::D3d9Device> value;

    D3d9HostDevice() : value(recomp::d3d::new_d3d9_device()) {}

    recomp::d3d::Status register_resource(uint32_t id, const D3d9HostResource &resource);
    void unregister_resource(uint32_t id) {
        recomp::d3d::d3d9_unregister_resource(*value, id);
    }
    recomp::d3d::D3d9ResetResult reset(const recomp::d3d::D3d9ResetRequest &request) {
        return recomp::d3d::d3d9_reset(*value, request);
    }
    recomp::d3d::ValueResult test_cooperative_level(uint32_t thread_id) {
        return recomp::d3d::d3d9_test_cooperative_level(*value, thread_id);
    }
    recomp::d3d::ValuesResult registered_resource_ids() const {
        return recomp::d3d::d3d9_registered_resource_ids(*value);
    }
    recomp::d3d::Status prepare_resource_reset(uint32_t id, const D3d9HostResource &resource);
    recomp::d3d::Status reset_ready() {
        return recomp::d3d::d3d9_reset_ready(*value);
    }
    void commit_resource_reset(uint32_t id, D3d9HostResource &resource);
    recomp::d3d::D3d9ResetResult finish_reset() {
        return recomp::d3d::d3d9_finish_reset(*value);
    }
    recomp::d3d::Status abort_reset() {
        return recomp::d3d::d3d9_abort_reset(*value);
    }
    recomp::d3d::D3d9FrameResult present() {
        return recomp::d3d::d3d9_present(*value);
    }
    recomp::d3d::D3d9SurfaceDescriptorResult backbuffer_descriptor() const {
        return recomp::d3d::d3d9_backbuffer_descriptor(*value);
    }
    recomp::d3d::D3d9SurfaceDescriptorResult depthbuffer_descriptor() const {
        return recomp::d3d::d3d9_depthbuffer_descriptor(*value);
    }
    recomp::d3d::ValueResult number_of_swap_chains() const {
        return recomp::d3d::d3d9_number_of_swap_chains(*value);
    }
    recomp::d3d::D3d9DisplayModeResult
    display_mode(const recomp::d3d::D3d9DisplayModeRequest &request) const {
        return recomp::d3d::d3d9_display_mode(*value, request);
    }
    recomp::d3d::D3d9CapsResult caps() const {
        return recomp::d3d::d3d9_device_caps(*value);
    }
};

struct D3d9Staging {
    uint32_t addr = 0;
    uint32_t size = 0;
};

struct D3d9HostResource {
    rust::Box<recomp::d3d::D3d9Resource> value;
    std::map<uint64_t, D3d9Staging> staging;

    D3d9HostResource() : value(recomp::d3d::new_d3d9_resource()) {}

    recomp::d3d::Status create_buffer(const D3d9HostDevice &device,
                                      const recomp::d3d::D3d9BufferRequest &request) {
        return recomp::d3d::d3d9_create_buffer(*device.value, *value, request);
    }

    recomp::d3d::D3d9TextureCreateResult
    create_texture(const D3d9HostDevice &device, const recomp::d3d::D3d9TextureRequest &request) {
        return recomp::d3d::d3d9_create_texture(*device.value, *value, request);
    }

    recomp::d3d::Status create_surface(const D3d9HostDevice &device,
                                       const recomp::d3d::D3d9SurfaceRequest &request) {
        return recomp::d3d::d3d9_create_surface(*device.value, *value, request);
    }

    recomp::d3d::D3d9BufferDescriptorResult descriptor() const {
        return recomp::d3d::d3d9_buffer_descriptor(*value);
    }

    recomp::d3d::D3d9BufferLockResult
    lock_prepare(const recomp::d3d::D3d9BufferLockRequest &request) {
        return recomp::d3d::d3d9_buffer_lock_prepare(*value, request);
    }

    recomp::d3d::D3d9BufferLockResult
    lock_commit(const recomp::d3d::D3d9BufferLockRequest &request) {
        return recomp::d3d::d3d9_buffer_lock_commit(*value, request);
    }

    rust::Slice<const uint8_t> bytes() const {
        return recomp::d3d::d3d9_buffer_bytes(*value);
    }

    recomp::d3d::D3d9BufferUnlockResult unlock(rust::Slice<const uint8_t> bytes) {
        return recomp::d3d::d3d9_buffer_unlock(*value, bytes);
    }

    recomp::d3d::ValueResult resource_type() const {
        return recomp::d3d::d3d9_resource_type(*value);
    }

    recomp::d3d::D3d9TextureDescriptorResult texture_descriptor() const {
        return recomp::d3d::d3d9_texture_descriptor(*value);
    }

    recomp::d3d::D3d9SurfaceDescriptorResult surface_descriptor() const {
        return recomp::d3d::d3d9_surface_descriptor(*value);
    }

    recomp::d3d::D3d9LevelDescriptorResult texture_level_descriptor(uint32_t face,
                                                                    uint32_t level) const {
        return recomp::d3d::d3d9_texture_level_descriptor(*value, face, level);
    }

    recomp::d3d::D3d9LevelDescriptorResult texture_volume_descriptor(uint32_t level) const {
        return recomp::d3d::d3d9_texture_volume_descriptor(*value, level);
    }

    recomp::d3d::D3d9LevelLockResult
    level_lock_prepare(const recomp::d3d::D3d9LevelLockRequest &request) {
        return recomp::d3d::d3d9_level_lock_prepare(*value, request);
    }

    recomp::d3d::D3d9LevelLockResult
    level_lock_commit(const recomp::d3d::D3d9LevelLockRequest &request) {
        return recomp::d3d::d3d9_level_lock_commit(*value, request);
    }

    rust::Slice<const uint8_t> level_bytes(uint32_t face, uint32_t level) const {
        return recomp::d3d::d3d9_level_bytes(*value, face, level);
    }

    recomp::d3d::D3d9BufferUnlockResult level_unlock(uint32_t face, uint32_t level,
                                                     rust::Slice<const uint8_t> bytes) {
        return recomp::d3d::d3d9_level_unlock(*value, face, level, bytes);
    }

    recomp::d3d::ValueResult set_priority(uint32_t priority) {
        return recomp::d3d::d3d9_set_priority(*value, priority);
    }
    recomp::d3d::ValueResult priority() const {
        return recomp::d3d::d3d9_priority(*value);
    }
    recomp::d3d::ValueResult set_lod(uint32_t lod) {
        return recomp::d3d::d3d9_set_lod(*value, lod);
    }
    recomp::d3d::ValueResult lod() const {
        return recomp::d3d::d3d9_lod(*value);
    }
    recomp::d3d::Status set_private_data(uint32_t view, rust::Slice<const uint8_t> guid,
                                         rust::Slice<const uint8_t> data, uint32_t flags) {
        return recomp::d3d::d3d9_set_private_data(*value, view, guid, data, flags);
    }
    recomp::d3d::D3d9PrivateDataResult get_private_data(uint32_t view,
                                                        rust::Slice<const uint8_t> guid) const {
        return recomp::d3d::d3d9_get_private_data(*value, view, guid);
    }
    recomp::d3d::Status free_private_data(uint32_t view, rust::Slice<const uint8_t> guid) {
        return recomp::d3d::d3d9_free_private_data(*value, view, guid);
    }
    recomp::d3d::Status add_dirty_rect(uint32_t face, uint32_t level, bool full_region,
                                       const recomp::d3d::D3d9LockRect &rect) {
        return recomp::d3d::d3d9_add_dirty_rect(*value, face, level, full_region, rect);
    }
    recomp::d3d::Status add_dirty_box(uint32_t level, bool full_region,
                                      const recomp::d3d::D3d9LockBox &bounds) {
        return recomp::d3d::d3d9_add_dirty_box(*value, level, full_region, bounds);
    }
    recomp::d3d::Status preload() {
        return recomp::d3d::d3d9_preload(*value);
    }
    recomp::d3d::Status set_autogen_filter(uint32_t filter) {
        return recomp::d3d::d3d9_set_autogen_filter(*value, filter);
    }
    recomp::d3d::ValueResult autogen_filter() const {
        return recomp::d3d::d3d9_get_autogen_filter(*value);
    }
    recomp::d3d::Status generate_mips() {
        return recomp::d3d::d3d9_generate_mips(*value);
    }
    recomp::d3d::Status upload_buffer() {
        return recomp::d3d::d3d9_upload_buffer(*value);
    }
    recomp::d3d::Status readback_buffer() {
        return recomp::d3d::d3d9_readback_buffer(*value);
    }
    recomp::d3d::Status upload_level(uint32_t face, uint32_t level) {
        return recomp::d3d::d3d9_upload_level(*value, face, level);
    }
    recomp::d3d::Status readback_level(uint32_t face, uint32_t level) {
        return recomp::d3d::d3d9_readback_level(*value, face, level);
    }
};

inline recomp::d3d::Status D3d9HostDevice::register_resource(uint32_t id,
                                                             const D3d9HostResource &resource) {
    return recomp::d3d::d3d9_register_resource(*value, id, *resource.value);
}

inline recomp::d3d::Status
D3d9HostDevice::prepare_resource_reset(uint32_t id, const D3d9HostResource &resource) {
    return recomp::d3d::d3d9_prepare_resource_reset(*value, id, *resource.value);
}

inline void D3d9HostDevice::commit_resource_reset(uint32_t id, D3d9HostResource &resource) {
    recomp::d3d::d3d9_commit_resource_reset(*value, id, *resource.value);
}

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
