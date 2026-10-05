// mss32.cpp - Miles Sound System 6 (mss32.dll) as a game imports it:
// stdcall exports whose decorated names carry their arity (_AIL_name@bytes).
// Samples play PCM WAVE images and streams decode MP3 through shared host
// audio channels. The 3D provider API reports "no providers" so a game can
// use its plain 2D path. Every entry preserves the guest's stdcall stack.
#include "com.h"
#include "dx.h"
#include "audio3d.h"
#include "host_api.h"
#include "riff.h"
#include "mp3_source.h"
#include "../runtime/imports.h"
#include "../runtime/memory.h"
#include "../runtime/win32.h"
#include "../platform/os.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <vector>
#include <iterator>
#include <map>

namespace {

const uint32_t SMP_FREE = 1, SMP_DONE = 2, SMP_PLAYING = 4;

int32_t miles_volume_mb(int32_t v) {
    v = std::clamp(v, 0, 127);
    return v == 0 ? -10000
                  : std::clamp((int32_t)std::lround(2000.0 * std::log10(v / 127.0)), -10000, 0);
}

int32_t miles_pan_mb(int32_t p) {
    p = std::clamp(p, 0, 127);
    return std::clamp((p - 64) * 10000 / 63, -10000, 10000);
}

struct Driver {
    uint32_t rate = 22050, bits = 16, channels = 2;
    int master = 127;
};
std::map<uint32_t, Driver> g_drivers;
uint32_t g_next_driver = 0x10000;
int32_t g_preferences[64]{};

struct Sample {
    uint32_t handle = 0, driver = 0;
    std::vector<int16_t> decoded;
    bool alive = false;
    int32_t channel = -1;
    RiffWave wave{};
    int32_t volume = 127, pan = 64;
    uint32_t loops = 1, remaining = 0;
    bool playing = false;
    uint32_t eos_cb = 0; // AIL_register_EOS_callback for a 2D sample
    bool eos_fired = false;

    // A Miles 3D sample plays through the same host-channel machinery as a 2D
    // one. Where it is and how far away decide the pan and the distance gain;
    // vol01 is AIL_set_3D_sample_volume's 0..1 value and rate overrides the
    // file's own sample rate when non-zero.
    bool is_3d = false;
    float vol01 = 1.0f;
    float pos3d[3] = {0.0f, 0.0f, 0.0f};
    float min_d = 1.0f, max_d = 1.0e9f;
    uint32_t rate = 0;
};
std::vector<Sample> g_samples(64);

// The single digital driver every sample belongs to. A sample handle does not
// name a driver, so master volume comes from the most recently opened one.
uint32_t g_last_digital_driver = 0;

// Miles' 3D listener. RT3 sets an identity orientation and the origin (its
// sound code pre-transforms world positions into listener space), so these are
// the defaults a source is placed against until the title says otherwise.
float g_listener_pos3d[3] = {0.0f, 0.0f, 0.0f};
float g_listener_front3d[3] = {0.0f, 0.0f, 1.0f};
float g_listener_top3d[3] = {0.0f, 1.0f, 0.0f};
float g_rolloff3d = 1.0f;

// The listener handle the shim hands out; distinct from sample handles so
// AIL_set_3D_position/orientation can tell the two apart.
const uint32_t kListener3DHandle = 0x00030001u;

// Miles timers. RT3 uses one to fade the music stream: AIL_register_timer
// names a callback, then a frequency and user word are set and the timer is
// started. Miles fired these on its own thread; the shim runs them on the
// guest frame seam, which is the thread the callback already assumes because
// it touches the guest heap and the music stream.
struct AilTimer {
    bool alive = false;
    bool running = false;
    uint32_t handle = 0;
    uint32_t callback = 0;
    uint32_t user = 0;
    uint32_t frequency = 0;
    uint64_t next_ns = 0;
};
std::vector<AilTimer> g_ail_timers(16);

Sample *sample_for(uint32_t handle) {
    if (!handle || handle > g_samples.size())
        return nullptr;
    Sample &s = g_samples[handle - 1];
    return s.alive ? &s : nullptr;
}

// The sample borrows its WAVE image. The host copies PCM on submission;
// Miles keeps the shared channel reserved until init or handle release.
void sample_stop(Sample &s) {
    if (s.channel >= 0)
        host_audio_stop(s.channel);
    s.playing = false;
    s.remaining = 0;
}

void sample_reset(Sample &s) {
    sample_stop(s);
    dx_free_audio_channel(s.channel);
    uint32_t handle = s.handle, driver = s.driver;
    s = Sample{};
    s.handle = handle;
    s.driver = driver;
    s.alive = true;
}

// Inverse-distance attenuation against the listener, matching the
// DirectSound3D model in dsound.cpp's calc_3d: a source inside MinDistance is
// untouched, beyond it falls off and is clamped at MaxDistance. RT3 hands
// Miles listener-relative positions, so the listener is the origin.
float sample3d_distance_gain(const Sample &s) {
    audio3d::V3 dir = {s.pos3d[0], s.pos3d[1], s.pos3d[2]};
    float dist = audio3d::v3_len(dir);
    if (dist > s.max_d)
        dist = s.max_d;
    if (dist < s.min_d)
        dist = s.min_d;
    float adjusted = s.min_d + (dist - s.min_d) * g_rolloff3d;
    return (s.min_d > 0.0f && adjusted > 0.0f) ? s.min_d / adjusted : 1.0f;
}

// The hundredths-of-a-dB pan for a listener-relative source, using the same
// axis convention and speaker mix as DirectSound3D (front +z, top +y).
int32_t sample3d_pan_mb(const Sample &s) {
    audio3d::V3 dir = {s.pos3d[0], s.pos3d[1], s.pos3d[2]};
    float dist = audio3d::v3_len(dir);
    if (dist == 0.0f)
        return 0;
    audio3d::V3 front = {g_listener_front3d[0], g_listener_front3d[1], g_listener_front3d[2]};
    audio3d::V3 top = {g_listener_top3d[0], g_listener_top3d[1], g_listener_top3d[2]};
    audio3d::V3 left = audio3d::v3_cross(front, top);
    float angle = audio3d::v3_angle(left, dir);
    if (audio3d::v3_angle(front, dir) > audio3d::PI_F_ / 2.0f)
        angle = -angle;
    angle -= audio3d::PI_F_ / 2.0f;
    if (angle < -audio3d::PI_F_)
        angle += 2.0f * audio3d::PI_F_;
    float lg = 1.0f, rg = 1.0f;
    audio3d::stereo_gains(angle, &lg, &rg);
    int32_t pan;
    if (lg > 0.0f && rg > 0.0f)
        pan = (int32_t)std::lround(2000.0f * std::log10(rg / lg));
    else if (rg > lg)
        pan = 10000;
    else
        pan = -10000;
    return std::clamp(pan, -10000, 10000);
}

int sample_volume(const Sample &s) {
    auto it = g_drivers.find(s.driver);
    int master = miles_volume_mb(it == g_drivers.end() ? 127 : it->second.master);
    if (s.is_3d) {
        // The 3D volume is a 0..1 gain, unlike the 2D API's 0..127. Distance
        // attenuation is folded in here so one call site reaches every gain.
        float gain = s.vol01 * sample3d_distance_gain(s);
        if (gain <= 0.0f)
            return -10000;
        return std::clamp(master + (int)std::lround(2000.0 * std::log10(gain)), -10000, 0);
    }
    return miles_volume_mb(s.volume) + master;
}

// Push a sample's current volume and pan to its host channel. A playing 3D
// source moves, so this is called on every position change as well as start.
void sample_apply_mix(Sample &s) {
    if (s.channel < 0)
        return;
    host_audio_set_volume(s.channel, std::max(-10000, sample_volume(s)));
    host_audio_set_pan(s.channel, s.is_3d ? sample3d_pan_mb(s) : miles_pan_mb(s.pan));
}

void sample_play(Sample &s) {
    HostAudioPlay p{};
    p.channel = s.channel;
    p.pcm = s.decoded.empty() ? (const void *)(g_mem + s.wave.pcm) : s.decoded.data();
    p.bytes = s.wave.pcm_bytes;
    p.sample_rate = (int32_t)(s.rate ? s.rate : s.wave.rate);
    p.channels = s.wave.channels;
    p.bits = s.wave.bits;
    p.loop = s.loops == 0 ? 1 : 0;
    p.volume = std::max(-10000, sample_volume(s));
    p.pan = s.is_3d ? sample3d_pan_mb(s) : miles_pan_mb(s.pan);
    host_audio_play(&p);
    s.playing = true;
}

// The host loop flag is boolean. Finite repeats restart at completion on
// the runtime's frame seam (also observed by status), with n total plays.
// Returns true only when the sample naturally finished, which is when the
// registered EOS callback fires.
bool sample_update(Sample &s) {
    if (!s.playing || host_audio_is_playing(s.channel))
        return false;
    if (s.remaining > 1) {
        --s.remaining;
        sample_play(s);
        return false;
    }
    s.remaining = 0;
    s.playing = false;
    return true;
}

// Use CreateFileA's read resolver, including overlays, drive mapping and
// case folding. Both NULL (ordinary Miles allocation) and FILE_READ_WITH_SIZE
// request a guest allocation here; the returned image begins with file bytes.
void ail_file_read(X86 *c) {
    set_eax(c, 0);
    std::string path = win32_host_path_op(gm_str(arg(c, 0)), WIN32_FILE_READ);
    if (path.empty())
        return;
    int fd = os_fd_open(path.c_str(), OS_O_RDONLY);
    if (fd < 0)
        return;
    OsStat st{};
    if (os_fd_stat(fd, &st) != 0 || !st.is_regular || !st.size || st.size > UINT32_MAX) {
        os_fd_close(fd);
        return;
    }
    uint32_t bytes = (uint32_t)st.size, dest = arg(c, 1);
    bool allocate = dest == 0 || dest == 0xffffffffu;
    if (allocate)
        dest = heap_alloc(bytes);
    if (!dest || !gm_valid(dest, bytes)) {
        if (allocate && dest)
            heap_free(dest);
        os_fd_close(fd);
        return;
    }
    uint32_t got = 0;
    while (got < bytes) {
        int64_t n = os_fd_read(fd, g_mem + dest + got, bytes - got);
        if (n <= 0)
            break;
        got += (uint32_t)n;
    }
    os_fd_close(fd);
    if (got != bytes) {
        if (allocate)
            heap_free(dest);
        return;
    }
    set_eax(c, dest);
}

void ail_mem_free_lock(X86 *c) {
    heap_free(arg(c, 0));
    set_eax(c, 0);
}

void ail_allocate_sample_handle(X86 *c) {
    set_eax(c, 0);
    for (uint32_t i = 0; i < g_samples.size(); ++i) {
        Sample &s = g_samples[i];
        if (s.alive)
            continue;
        s = Sample{};
        s.handle = i + 1;
        s.driver = arg(c, 0);
        s.alive = true;
        set_eax(c, s.handle);
        return;
    }
}

void ail_release_sample_handle(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0))) {
        sample_reset(*s);
        s->alive = false;
    }
    set_eax(c, 0);
}

void ail_init_sample(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0)))
        sample_reset(*s);
    set_eax(c, 0);
}

// The byte length of an in-memory sound image. Miles entry points take a
// pointer, not a length, so the length comes from the guest heap when the
// image is a heap block (which is how the engine's loader allocates a whole
// file) and otherwise from the RIFF header, which is how an image that lives
// in a fixed buffer is measured.
uint32_t sample_image_bytes(uint32_t image, uint32_t explicit_bytes) {
    if (explicit_bytes)
        return explicit_bytes;
    uint32_t allocated = heap_size(image);
    if (allocated != UINT32_MAX)
        return allocated;
    if (image && gm_valid(image, 12)) {
        uint64_t bytes = (uint64_t)rd32(image + 4) + 8;
        if (bytes <= UINT32_MAX)
            return (uint32_t)bytes;
    }
    return 0;
}

// Load an in-memory sound image into a sample. RT3's effects and music arrive
// as either a RIFF WAVE or a bare MPEG audio file (Miles ships its MP3 codec
// in Data/Miles), so both are accepted: RIFF PCM stays in the guest image, and
// MPEG is decoded once into PCM the sample owns.
bool sample_load_image(Sample &s, uint32_t image, uint32_t bytes) {
    s.wave = RiffWave{};
    s.decoded.clear();
    if (!image || !bytes || !gm_valid(image, bytes))
        return false;
    if (riff_parse_wave(image, bytes, &s.wave))
        return s.wave.pcm_bytes != 0;
    Mp3Source source;
    if (!source.open(std::vector<uint8_t>(g_mem + image, g_mem + image + bytes)))
        return false;
    std::vector<int16_t> frame;
    while (source.decode_next(frame)) {
        if (s.decoded.size() + frame.size() > 64 * 1024 * 1024) {
            s.decoded.clear();
            return false;
        }
        s.decoded.insert(s.decoded.end(), frame.begin(), frame.end());
    }
    s.wave.pcm_bytes = uint32_t(s.decoded.size() * 2);
    s.wave.rate = source.rate();
    s.wave.channels = source.channels();
    s.wave.bits = 16;
    return s.wave.pcm_bytes != 0;
}

void ail_set_sample_file(X86 *c) {
    set_eax(c, 0);
    Sample *s = sample_for(arg(c, 0));
    if (!s)
        return;
    sample_stop(*s);
    uint32_t image = arg(c, 1), bytes = arg(c, 2);
    if (sample_load_image(*s, image, sample_image_bytes(image, bytes)))
        set_eax(c, 1);
}

// Named samples carry a byte length, allowing packed MP3 effects as well as
// RIFF. This is also the shared decoder behind the file-image entry points.
void ail_set_named_sample_file(X86 *c) {
    Sample *s = sample_for(arg(c, 0));
    set_eax(c, 0);
    if (!s)
        return;
    sample_stop(*s);
    if (sample_load_image(*s, arg(c, 2), arg(c, 3)))
        set_eax(c, 1);
}

// AIL_WAV_info(void *file_image, AILSOUNDINFO *info). The structure is nine
// dwords: format, data_ptr, data_len, rate, bits, channels, samples,
// block_size and initial_ptr (Miles mss.h). RT3's loader copies it wholesale
// into its sound object and only the raw image reaches the sample-file call,
// so a wrong field is at worst a wrong reported length, never a misdecode.
void ail_wav_info(X86 *c) {
    uint32_t image = arg(c, 0), out = arg(c, 1);
    set_eax(c, 0);
    if (!image || !out || !gm_valid(out, 36))
        return;
    uint32_t bytes = sample_image_bytes(image, 0);
    gm_zero(out, 36);
    RiffWave w{};
    if (bytes && riff_parse_wave(image, bytes, &w)) {
        uint32_t block = (uint32_t)w.channels * (w.bits / 8u);
        wr32(out + 0, 1); // WAVE_FORMAT_PCM
        wr32(out + 4, w.pcm);
        wr32(out + 8, w.pcm_bytes);
        wr32(out + 12, w.rate);
        wr32(out + 16, w.bits);
        wr32(out + 20, w.channels);
        wr32(out + 24, block ? w.pcm_bytes / block : 0);
        wr32(out + 28, block);
        wr32(out + 32, w.pcm);
        set_eax(c, 1);
        return;
    }
    if (bytes) {
        Mp3Source source;
        if (source.open(std::vector<uint8_t>(g_mem + image, g_mem + image + bytes))) {
            wr32(out + 0, 0x55); // WAVE_FORMAT_MPEGLAYER3
            wr32(out + 4, image);
            wr32(out + 8, bytes);
            wr32(out + 12, source.rate());
            wr32(out + 16, 16);
            wr32(out + 20, source.channels());
            set_eax(c, 1);
        }
    }
}

// A successful waveOutOpen must populate HDIGDRIVER, not just return zero.
void ail_wave_open(X86 *c) {
    uint32_t out = arg(c, 0), fmt = arg(c, 3);
    set_eax(c, 1);
    if (!out || !gm_valid(out, 4))
        return;
    wr32(out, 0);
    if (!fmt || !gm_valid(fmt, 16) || rd16(fmt) != 1 || !rd32(fmt + 4) ||
        (rd16(fmt + 2) != 1 && rd16(fmt + 2) != 2) || (rd16(fmt + 14) != 8 && rd16(fmt + 14) != 16))
        return;
    uint32_t id = g_next_driver++;
    g_drivers[id] = {rd32(fmt + 4), rd16(fmt + 14), rd16(fmt + 2), 127};
    wr32(out, id);
    if (arg(c, 1) && gm_valid(arg(c, 1), 4))
        wr32(arg(c, 1), id);
    LOGV("mss32: opened digital driver %08x (%u Hz)", id, rd32(fmt + 4));
    set_eax(c, 0);
}
// AIL_open_digital_driver(frequency, bits, channels, flags) returns the
// HDIGDRIVER directly rather than through an out parameter. RT3 opens the
// driver at the game's mixed rate (22050 Hz, 16-bit stereo). A zero rate
// selects Miles' default; the shim records the requested shape so
// digital_configuration and sample volume answer consistently.
void ail_open_digital_driver(X86 *c) {
    uint32_t rate = arg(c, 0), bits = arg(c, 1), channels = arg(c, 2);
    if (!rate)
        rate = 22050;
    if (bits != 8 && bits != 16)
        bits = 16;
    if (channels != 1 && channels != 2)
        channels = 2;
    uint32_t id = g_next_driver++;
    g_drivers[id] = {rate, bits, channels, 127};
    g_last_digital_driver = id;
    LOGV("mss32: opened digital driver %08x (%u Hz, %u-bit, %u ch)", id, rate, bits, channels);
    set_eax(c, id);
}

void ail_wave_close(X86 *c) {
    for (auto &s : g_samples)
        if (s.alive && s.driver == arg(c, 0)) {
            sample_reset(s);
            s.alive = false;
        }
    g_drivers.erase(arg(c, 0));
    set_eax(c, 0);
}
void ail_configuration(X86 *c) {
    auto it = g_drivers.find(arg(c, 0));
    if (it != g_drivers.end()) {
        if (arg(c, 1) && gm_valid(arg(c, 1), 4))
            wr32(arg(c, 1), it->second.rate);
        if (arg(c, 2) && gm_valid(arg(c, 2), 4))
            wr32(arg(c, 2), (it->second.channels == 2 ? 2 : 0) | (it->second.bits == 16 ? 1 : 0));
        if (arg(c, 3) && gm_valid(arg(c, 3), 128))
            gm_put_str(arg(c, 3), "Native digital audio", 128);
    }
    set_eax(c, 0);
}
void ail_master_volume(X86 *c) {
    auto it = g_drivers.find(arg(c, 0));
    if (it != g_drivers.end())
        it->second.master = std::clamp((int32_t)arg(c, 1), 0, 127);
    for (auto &s : g_samples)
        if (s.alive && s.channel >= 0 && s.driver == arg(c, 0))
            host_audio_set_volume(s.channel, std::max(-10000, sample_volume(s)));
    set_eax(c, 0);
}
void ail_preference(X86 *c) {
    uint32_t index = arg(c, 0);
    set_eax(c, index < 64 ? g_preferences[index] : 0);
    if (index < 64)
        g_preferences[index] = (int32_t)arg(c, 1);
}
void ail_get_preference(X86 *c) {
    set_eax(c, arg(c, 0) < 64 ? g_preferences[arg(c, 0)] : 0);
}

void ail_start_sample(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0)); s && s->wave.pcm_bytes) {
        if (s->channel < 0)
            s->channel = dx_alloc_audio_channel();
        if (s->channel >= 0) {
            s->remaining = s->loops;
            s->eos_fired = false;
            sample_play(*s);
        }
    }
    set_eax(c, 0);
}

void ail_end_sample(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0)))
        sample_stop(*s);
    set_eax(c, 0);
}

// AIL_lock/AIL_unlock serialize access to Miles' global state. The shim's
// state is touched only from the guest's own thread seam, so there is nothing
// to guard; returning zero matches the API's void-like result.
void ail_lock(X86 *c) {
    set_eax(c, 0);
}
void ail_unlock(X86 *c) {
    set_eax(c, 0);
}

void ail_stop_sample(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0)))
        sample_stop(*s);
    set_eax(c, 0);
}

// AIL_resume_sample continues a sample the engine stopped. The shim has no
// separate paused state, so a stopped sample with PCM restarts from its first
// channel and re-submits the image.
void ail_resume_sample(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0)); s && !s->playing && s->wave.pcm_bytes) {
        if (s->channel < 0)
            s->channel = dx_alloc_audio_channel();
        if (s->channel >= 0) {
            s->remaining = s->loops;
            s->eos_fired = false;
            sample_play(*s);
        }
    }
    set_eax(c, 0);
}

void ail_sample_status(X86 *c) {
    Sample *s = sample_for(arg(c, 0));
    if (s)
        sample_update(*s);
    set_eax(c, !s ? SMP_FREE : s->playing ? SMP_PLAYING : SMP_DONE);
}

void ail_set_sample_volume(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0))) {
        s->volume = std::clamp((int32_t)arg(c, 1), 0, 127);
        if (s->channel >= 0)
            host_audio_set_volume(s->channel, std::max(-10000, sample_volume(*s)));
    }
    set_eax(c, 0);
}

void ail_set_sample_pan(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0))) {
        s->pan = std::clamp((int32_t)arg(c, 1), 0, 127);
        if (s->channel >= 0)
            host_audio_set_pan(s->channel, miles_pan_mb(s->pan));
    }
    set_eax(c, 0);
}

void ail_set_sample_loop_count(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0)))
        s->loops = arg(c, 1);
    set_eax(c, 0);
}

void ail_sample_loop_count(X86 *c) {
    Sample *s = sample_for(arg(c, 0));
    set_eax(c, s ? s->loops : 0);
}

float bits_to_float(uint32_t bits) {
    float v;
    memcpy(&v, &bits, 4);
    return v;
}

// AIL_set_sample_volume_levels(HSAMPLE, F32 left, F32 right) and
// AIL_set_sample_volume_pan(HSAMPLE, F32 volume, F32 pan) are the two forms
// the engine's 2D path uses. The shim's channel has one volume and one pan,
// so the two levels collapse to their loudest and their balance, and the
// combined form maps its 0..1 pan onto the 0..127 range SetPan uses.
void ail_set_sample_volume_levels(X86 *c) {
    Sample *s = sample_for(arg(c, 0));
    set_eax(c, 0);
    if (!s)
        return;
    float l = std::clamp(bits_to_float(arg(c, 1)), 0.0f, 1.0f);
    float r = std::clamp(bits_to_float(arg(c, 2)), 0.0f, 1.0f);
    float loud = l > r ? l : r;
    float sum = l + r;
    s->volume = std::clamp((int)std::lround(loud * 127.0f), 0, 127);
    s->pan = sum > 0.0f ? std::clamp((int)std::lround((r / sum) * 127.0f), 0, 127) : 64;
    sample_apply_mix(*s);
}

void ail_set_sample_volume_pan(X86 *c) {
    Sample *s = sample_for(arg(c, 0));
    set_eax(c, 0);
    if (!s)
        return;
    float v = std::clamp(bits_to_float(arg(c, 1)), 0.0f, 1.0f);
    float p = std::clamp(bits_to_float(arg(c, 2)), 0.0f, 1.0f);
    s->volume = std::clamp((int)std::lround(v * 127.0f), 0, 127);
    s->pan = std::clamp((int)std::lround(p * 127.0f), 0, 127);
    sample_apply_mix(*s);
}

void ail_set_sample_playback_rate(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0))) {
        s->rate = arg(c, 1);
        if (s->channel >= 0 && s->rate)
            host_audio_set_frequency(s->channel, s->rate);
    }
    set_eax(c, 0);
}

void ail_register_eos_callback(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0))) {
        s->eos_cb = arg(c, 1);
        s->eos_fired = false;
    }
    set_eax(c, 0);
}

// Stream handles own the encoded image, decoder and shared host channel.
// All state is serviced on the guest frame seam; the host copies each PCM
// submission. A refused chunk remains pending until a later frame accepts it.
struct Stream {
    bool alive = false, playing = false;
    // A paused stream is one the host voice has been stopped for. The host has
    // no pause of its own, so resuming restarts the stream; a title that pauses
    // to close does not care, and one that pauses for focus will hear the track
    // restart.
    bool paused = false;
    int32_t channel = -1, volume = 127;
    uint32_t loops = 1, remaining = 1;
    uint64_t submitted = 0;
    Mp3Source source;
    std::vector<int16_t> pending;
    size_t pending_pos = 0;
};
std::vector<Stream> g_streams(64);

Stream *stream_for(uint32_t handle) {
    if (!handle || handle > g_streams.size())
        return nullptr;
    Stream &s = g_streams[handle - 1];
    return s.alive ? &s : nullptr;
}

// Read through the runtime resolver, sharing CreateFileA's overlays, drive
// mapping and case folding. Open does not play until AIL_start_stream.
void ail_open_stream(X86 *c) {
    set_eax(c, 0);
    uint32_t name = arg(c, 1);
    if (!name || !gm_valid(name, 1))
        return;
    std::string guest = gm_str(name);
    std::string path = win32_host_path_op(guest, WIN32_FILE_READ);
    int fd = path.empty() ? -1 : os_fd_open(path.c_str(), OS_O_RDONLY);
    if (fd < 0) {
        LOGW("mss32: open_stream(%s): cannot open %s", guest.c_str(), path.c_str());
        return;
    }
    OsStat st{};
    if (os_fd_stat(fd, &st) != 0 || !st.is_regular || !st.size || st.size > UINT32_MAX) {
        os_fd_close(fd);
        return;
    }
    std::vector<uint8_t> bytes((size_t)st.size);
    size_t got = 0;
    while (got < bytes.size()) {
        int64_t n = os_fd_read(fd, bytes.data() + got, bytes.size() - got);
        if (n <= 0)
            break;
        got += (size_t)n;
    }
    os_fd_close(fd);
    if (got != bytes.size()) {
        LOGW("mss32: open_stream(%s): short read of %s", guest.c_str(), path.c_str());
        return;
    }
    for (uint32_t i = 0; i < g_streams.size(); ++i) {
        Stream &s = g_streams[i];
        if (s.alive)
            continue;
        s = Stream{};
        if (!s.source.open(bytes)) {
            LOGW("mss32: open_stream(%s): not an MPEG audio file: %s", guest.c_str(), path.c_str());
            return;
        }
        s.channel = dx_alloc_audio_channel();
        if (s.channel < 0) {
            s = Stream{};
            return;
        }
        s.alive = true;
        LOGV("mss32: open_stream(%s): %s, %u Hz, %u channel(s)", guest.c_str(), path.c_str(),
             s.source.rate(), s.source.channels());
        set_eax(c, i + 1);
        return;
    }
}

// Keep one second of appended PCM ahead. Decoder EOF is not playback EOF:
// status stays playing until the host consumes the first play and all queues.
void stream_update(Stream &s) {
    if (!s.playing)
        return;
    uint32_t ahead = s.source.rate() * s.source.channels() * 2;
    while (host_audio_queued_bytes(s.channel) < ahead) {
        if (s.pending.empty()) {
            if (!s.source.decode_next(s.pending)) {
                if (s.loops != 0 && s.remaining <= 1)
                    break;
                if (s.remaining > 1)
                    --s.remaining;
                s.source.seek_frames(0);
                if (!s.source.decode_next(s.pending))
                    break;
            }
            s.pending_pos = 0;
        }
        uint32_t bytes = (uint32_t)(s.pending.size() - s.pending_pos) * 2;
        int32_t taken = host_audio_queue(s.channel, s.pending.data() + s.pending_pos, bytes);
        if (taken <= 0)
            return;
        s.submitted += (uint32_t)taken;
        s.pending_pos += (uint32_t)taken / 2;
        if (s.pending_pos >= s.pending.size())
            s.pending.clear();
    }
    if (s.source.drained() && s.pending.empty() &&
        host_audio_played_bytes(s.channel) >= s.submitted)
        s.playing = false;
}

// Start with one decoded frame, then convert the channel so subsequent
// frames append without restarting playback. Starting again rewinds.
void stream_start(Stream &s) {
    host_audio_stop(s.channel);
    s.playing = false;
    s.paused = false;
    s.pending.clear();
    s.source.seek_frames(0);
    s.remaining = s.loops;
    if (s.source.decode_next(s.pending)) {
        HostAudioPlay p{};
        p.channel = s.channel;
        p.pcm = s.pending.data();
        p.bytes = (uint32_t)s.pending.size() * 2;
        p.sample_rate = (int32_t)s.source.rate();
        p.channels = (int32_t)s.source.channels();
        p.bits = 16;
        p.volume = miles_volume_mb(s.volume);
        host_audio_play(&p);
        s.submitted = p.bytes;
        s.pending.clear();
        s.playing = host_audio_stream(s.channel) >= 0;
        if (!s.playing)
            host_audio_stop(s.channel);
    }
}

void ail_start_stream(X86 *c) {
    if (Stream *s = stream_for(arg(c, 0)))
        stream_start(*s);
    set_eax(c, 0);
}

void ail_close_stream(X86 *c) {
    if (Stream *s = stream_for(arg(c, 0))) {
        host_audio_stop(s->channel);
        dx_free_audio_channel(s->channel);
        *s = Stream{};
    }
    set_eax(c, 0);
}

void ail_stream_status(X86 *c) {
    Stream *s = stream_for(arg(c, 0));
    if (s) {
        if (s->paused) {
            set_eax(c, 0x8u); // SMP_STOPPED
            return;
        }
        stream_update(*s);
    }
    set_eax(c, s && s->playing ? SMP_PLAYING : SMP_DONE);
}

void ail_set_stream_volume(X86 *c) {
    if (Stream *s = stream_for(arg(c, 0))) {
        s->volume = std::clamp((int32_t)arg(c, 1), 0, 127);
        host_audio_set_volume(s->channel, miles_volume_mb(s->volume));
    }
    set_eax(c, 0);
}

void ail_stream_volume(X86 *c) {
    Stream *s = stream_for(arg(c, 0));
    set_eax(c, s ? (uint32_t)s->volume : 0);
}

void ail_set_stream_loop_count(X86 *c) {
    if (Stream *s = stream_for(arg(c, 0)))
        s->remaining = s->loops = arg(c, 1);
    set_eax(c, 0);
}

// AIL_pause_stream(HSTREAM, S32 pause). The host voice is stopped on pause and
// the stream restarted on resume; SMP_STOPPED from the status call is what
// tells the engine it may resume.
void ail_pause_stream(X86 *c) {
    Stream *s = stream_for(arg(c, 0));
    if (s && arg(c, 1)) {
        if (s->playing || s->channel >= 0)
            host_audio_stop(s->channel);
        s->playing = false;
        s->paused = true;
    } else if (s) {
        // Resume by restarting: the host has no pause to undo.
        s->paused = false;
        if (!s->playing)
            stream_start(*s);
    }
    set_eax(c, 0);
}

// AIL_set_stream_volume_levels(HSTREAM, F32 left, F32 right): the two levels
// collapse to the host channel's single volume, as the sample form does.
void ail_set_stream_volume_levels(X86 *c) {
    if (Stream *s = stream_for(arg(c, 0))) {
        float l = std::clamp(bits_to_float(arg(c, 1)), 0.0f, 1.0f);
        float r = std::clamp(bits_to_float(arg(c, 2)), 0.0f, 1.0f);
        s->volume = std::clamp((int)std::lround(std::max(l, r) * 127.0f), 0, 127);
        host_audio_set_volume(s->channel, miles_volume_mb(s->volume));
    }
    set_eax(c, 0);
}

// ---------------------------------------------------------------------------
// Miles 3D providers, listener and positional samples.
//
// RT3 will not initialise audio at all without a 3D provider: its
// MusicSystem::initializeMilesAudio opens one by name and allocates 32 3D
// sample handles, and a zero provider count makes that path return failure and
// the settings UI report "This computer has no sound capabilities." (language
// string 3130). Miles always enumerates its software positional providers on a
// machine without 3D hardware, so the shim does too. The host mixer is 2D; a
// 3D sample's listener-relative position drives pan and distance gain through
// the model shared with DirectSound3D in audio3d.h.
// ---------------------------------------------------------------------------
struct Provider3D {
    const char *name;
    uint32_t handle;
};
const Provider3D g_providers3d[] = {
    {"DirectSound3D 7+ Software - Pan and Volume", 0x00020001u},
};
uint32_t g_provider_names3d = 0;

void ail_enumerate_3D_providers(X86 *c) {
    uint32_t cursor = arg(c, 0), next = arg(c, 1), name = arg(c, 2);
    set_eax(c, 0);
    if (!cursor || !next || !name || !gm_valid(cursor, 4))
        return;
    // The title keeps the char* Miles hands it and compares it against later
    // enumerations, so the names live in guest memory, not host string
    // literals, and stay put for the run.
    if (!g_provider_names3d) {
        uint32_t block = heap_alloc(128, false, 16);
        if (!block)
            return;
        g_provider_names3d = block;
        for (size_t i = 0; i < std::size(g_providers3d); ++i)
            gm_put_str(block + (uint32_t)i * 64, g_providers3d[i].name, 64);
    }
    uint32_t idx = rd32(cursor);
    if (idx >= std::size(g_providers3d))
        return;
    wr32(next, g_providers3d[idx].handle);
    wr32(name, g_provider_names3d + idx * 64);
    wr32(cursor, idx + 1);
    set_eax(c, 1);
}

// AIL_open_3D_provider returns zero on success; anything else is a Miles error
// code the engine reads as "provider unavailable". The one software provider
// always opens.
void ail_open_3D_provider(X86 *c) {
    set_eax(c, 0);
}

void ail_3D_speaker_type(X86 *c) {
    set_eax(c, 0); // AIL_3D_HEADPHONE; the host mix is stereo
}
void ail_set_3D_speaker_type(X86 *c) {
    set_eax(c, 0);
}
// A room type of -1 means "no environmental reverb", which is what a host
// mixer with no effect chain honestly reports.
void ail_3D_room_type(X86 *c) {
    set_eax(c, 0xffffffffu);
}
void ail_set_3D_distance_factor(X86 *c) {
    g_rolloff3d = bits_to_float(arg(c, 1));
    set_eax(c, 0);
}

void ail_open_3D_listener(X86 *c) {
    set_eax(c, kListener3DHandle);
}
void ail_close_3D_listener(X86 *c) {
    set_eax(c, 0);
}

void ail_allocate_3D_sample_handle(X86 *c) {
    set_eax(c, 0);
    for (uint32_t i = 0; i < g_samples.size(); ++i) {
        Sample &s = g_samples[i];
        if (s.alive)
            continue;
        s = Sample{};
        s.handle = i + 1;
        s.driver = g_last_digital_driver;
        s.alive = true;
        s.is_3d = true;
        set_eax(c, s.handle);
        return;
    }
}

void ail_release_3D_sample_handle(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0))) {
        sample_reset(*s);
        s->alive = false;
    }
    set_eax(c, 0);
}

// AIL_set_3D_sample_file(H3DSAMPLE, void *file_image). The engine hands it the
// same in-memory image its loader already read, so the shared decoder is used.
void ail_set_3D_sample_file(X86 *c) {
    set_eax(c, 0);
    Sample *s = sample_for(arg(c, 0));
    if (!s)
        return;
    sample_stop(*s);
    uint32_t bytes = sample_image_bytes(arg(c, 1), 0);
    if (sample_load_image(*s, arg(c, 1), bytes))
        set_eax(c, 1);
}

void ail_set_3D_sample_loop_count(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0)))
        s->loops = arg(c, 1);
    set_eax(c, 0);
}

void ail_set_3D_sample_volume(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0))) {
        s->vol01 = std::clamp(bits_to_float(arg(c, 1)), 0.0f, 1.0f);
        sample_apply_mix(*s);
    }
    set_eax(c, 0);
}

// AIL_set_3D_sample_distances(H3DSAMPLE, F32 max, F32 min).
void ail_set_3D_sample_distances(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0))) {
        s->max_d = bits_to_float(arg(c, 1));
        s->min_d = bits_to_float(arg(c, 2));
        sample_apply_mix(*s);
    }
    set_eax(c, 0);
}

void ail_set_3D_sample_playback_rate(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0))) {
        s->rate = arg(c, 1);
        if (s->channel >= 0 && s->rate)
            host_audio_set_frequency(s->channel, s->rate);
    }
    set_eax(c, 0);
}

// Effects level and occlusion are accepted and not applied: the host mix has
// no effect chain, and RT3 does not drive a source orientation that would
// change the pan. Each is reported once so a future title that depends on one
// is not silent about it.
void ail_set_3D_sample_effects_level(X86 *c) {
    log_once("mss32.3dfx",
             "mss32: a 3D sample effects level is set but the host mix applies no effects");
    set_eax(c, 0);
}
void ail_set_3D_sample_occlusion(X86 *c) {
    log_once("mss32.3doccl", "mss32: 3D sample occlusion is set but is not applied to the mix");
    set_eax(c, 0);
}

void ail_set_3D_position(X86 *c) {
    uint32_t handle = arg(c, 0);
    float x = bits_to_float(arg(c, 1)), y = bits_to_float(arg(c, 2)), z = bits_to_float(arg(c, 3));
    if (handle == kListener3DHandle) {
        g_listener_pos3d[0] = x;
        g_listener_pos3d[1] = y;
        g_listener_pos3d[2] = z;
    } else if (Sample *s = sample_for(handle)) {
        s->pos3d[0] = x;
        s->pos3d[1] = y;
        s->pos3d[2] = z;
        sample_apply_mix(*s);
    }
    set_eax(c, 0);
}

void ail_set_3D_orientation(X86 *c) {
    uint32_t handle = arg(c, 0);
    if (handle == kListener3DHandle) {
        for (int i = 0; i < 3; ++i) {
            g_listener_front3d[i] = bits_to_float(arg(c, 1 + i));
            g_listener_top3d[i] = bits_to_float(arg(c, 4 + i));
        }
        // Every source's pan is measured against the listener, so a listener
        // move re-places anything already playing.
        for (auto &s : g_samples)
            if (s.alive && s.is_3d)
                sample_apply_mix(s);
    }
    set_eax(c, 0);
}

void ail_start_3D_sample(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0)); s && s->wave.pcm_bytes) {
        if (s->channel < 0)
            s->channel = dx_alloc_audio_channel();
        if (s->channel >= 0) {
            s->remaining = s->loops;
            s->eos_fired = false;
            sample_play(*s);
        }
    }
    set_eax(c, 0);
}
void ail_stop_3D_sample(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0)))
        sample_stop(*s);
    set_eax(c, 0);
}
void ail_resume_3D_sample(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0)); s && !s->playing && s->wave.pcm_bytes) {
        if (s->channel < 0)
            s->channel = dx_alloc_audio_channel();
        if (s->channel >= 0) {
            s->remaining = s->loops;
            s->eos_fired = false;
            sample_play(*s);
        }
    }
    set_eax(c, 0);
}
void ail_end_3D_sample(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0)))
        sample_stop(*s);
    set_eax(c, 0);
}
void ail_3D_sample_status(X86 *c) {
    Sample *s = sample_for(arg(c, 0));
    if (s)
        sample_update(*s);
    set_eax(c, !s ? SMP_FREE : s->playing ? SMP_PLAYING : SMP_DONE);
}
void ail_register_3D_eos_callback(X86 *c) {
    if (Sample *s = sample_for(arg(c, 0))) {
        s->eos_cb = arg(c, 1);
        s->eos_fired = false;
    }
    set_eax(c, 0);
}

// ---------------------------------------------------------------------------
// Miles timers.
// ---------------------------------------------------------------------------
AilTimer *timer_for(uint32_t handle) {
    for (auto &t : g_ail_timers)
        if (t.alive && t.handle == handle)
            return &t;
    return nullptr;
}

// AIL_register_timer(AILTIMERCB callback) returns a handle, or -1 when no
// timer slot is free. The callback is a guest address and is invoked with the
// user word.
void ail_register_timer(X86 *c) {
    set_eax(c, 0xffffffffu);
    if (!arg(c, 0))
        return;
    for (uint32_t i = 0; i < g_ail_timers.size(); ++i) {
        if (g_ail_timers[i].alive)
            continue;
        AilTimer &t = g_ail_timers[i];
        t = AilTimer{};
        t.alive = true;
        t.handle = i + 1;
        t.callback = arg(c, 0);
        set_eax(c, t.handle);
        return;
    }
}
void ail_set_timer_user(X86 *c) {
    if (AilTimer *t = timer_for(arg(c, 0)))
        t->user = arg(c, 1);
    set_eax(c, 0);
}
void ail_set_timer_frequency(X86 *c) {
    if (AilTimer *t = timer_for(arg(c, 0))) {
        t->frequency = std::clamp(arg(c, 1), 1u, 1000u);
        if (t->running)
            t->next_ns = os_monotonic_ns() + 1000000000ull / t->frequency;
    }
    set_eax(c, 0);
}
void ail_start_timer(X86 *c) {
    if (AilTimer *t = timer_for(arg(c, 0))) {
        t->running = true;
        t->next_ns = os_monotonic_ns() + 1000000000ull / std::max(1u, t->frequency);
    }
    set_eax(c, 0);
}
void ail_stop_timer(X86 *c) {
    if (AilTimer *t = timer_for(arg(c, 0)))
        t->running = false;
    set_eax(c, 0);
}
void ail_release_timer_handle(X86 *c) {
    if (AilTimer *t = timer_for(arg(c, 0)))
        *t = AilTimer{};
    set_eax(c, 0);
}

void ret0(X86 *c) {
    set_eax(c, 0);
}
void ret1(X86 *c) {
    set_eax(c, 1);
}
void ret_done(X86 *c) {
    set_eax(c, SMP_DONE);
}

#define AIL(name, bytes, fn) {"mss32.dll", "_AIL_" #name "@" #bytes, (bytes) / 4, fn}

const ImportShim g_mss32_shims[] = {
    AIL(startup, 0, ret1),
    AIL(shutdown, 0, ret0),
    // AIL_set_redist_directory tells Miles where to find its own runtime
    // modules. The shim is the runtime, so the path is irrelevant; the engine
    // ignores the result and calls AIL_startup next.
    AIL(set_redist_directory, 4, ret1),
    AIL(lock, 0, ail_lock),
    AIL(unlock, 0, ail_unlock),
    AIL(stop_sample, 4, ail_stop_sample),
    AIL(resume_sample, 4, ail_resume_sample),
    AIL(set_preference, 8, ail_preference),
    AIL(get_preference, 4, ail_get_preference),
    AIL(waveOutOpen, 16, ail_wave_open),
    AIL(waveOutClose, 4, ail_wave_close),
    AIL(open_digital_driver, 16, ail_open_digital_driver),
    AIL(digital_configuration, 16, ail_configuration),
    AIL(set_digital_master_volume, 8, ail_master_volume),
    AIL(set_named_sample_file, 20, ail_set_named_sample_file),
    AIL(mem_free_lock, 4, ail_mem_free_lock),
    AIL(file_read, 8, ail_file_read),
    AIL(WAV_info, 8, ail_wav_info),
    AIL(allocate_sample_handle, 4, ail_allocate_sample_handle),
    AIL(release_sample_handle, 4, ail_release_sample_handle),
    AIL(init_sample, 4, ail_init_sample),
    AIL(set_sample_file, 12, ail_set_sample_file),
    AIL(start_sample, 4, ail_start_sample),
    AIL(end_sample, 4, ail_end_sample),
    AIL(sample_status, 4, ail_sample_status),
    AIL(set_sample_volume, 8, ail_set_sample_volume),
    AIL(set_sample_pan, 8, ail_set_sample_pan),
    AIL(set_sample_loop_count, 8, ail_set_sample_loop_count),
    AIL(sample_loop_count, 4, ail_sample_loop_count),
    AIL(set_sample_playback_rate, 8, ail_set_sample_playback_rate),
    AIL(set_sample_volume_levels, 12, ail_set_sample_volume_levels),
    AIL(set_sample_volume_pan, 12, ail_set_sample_volume_pan),
    AIL(set_sample_reverb, 16, ret0),
    AIL(register_EOS_callback, 8, ail_register_eos_callback),
    AIL(register_stream_callback, 8, ret0),
    AIL(register_timer, 4, ail_register_timer),
    AIL(release_timer_handle, 4, ail_release_timer_handle),
    AIL(set_timer_user, 8, ail_set_timer_user),
    AIL(set_timer_frequency, 8, ail_set_timer_frequency),
    AIL(start_timer, 4, ail_start_timer),
    AIL(stop_timer, 4, ail_stop_timer),
    AIL(open_stream, 12, ail_open_stream),
    AIL(start_stream, 4, ail_start_stream),
    AIL(close_stream, 4, ail_close_stream),
    AIL(stream_status, 4, ail_stream_status),
    AIL(set_stream_volume, 8, ail_set_stream_volume),
    AIL(stream_volume, 4, ail_stream_volume),
    AIL(set_stream_loop_count, 8, ail_set_stream_loop_count),
    AIL(pause_stream, 8, ail_pause_stream),
    AIL(set_stream_volume_levels, 12, ail_set_stream_volume_levels),
    AIL(enumerate_3D_providers, 12, ail_enumerate_3D_providers),
    AIL(open_3D_provider, 4, ail_open_3D_provider),
    AIL(close_3D_provider, 4, ret0),
    AIL(open_3D_listener, 4, ail_open_3D_listener),
    AIL(close_3D_listener, 4, ret0),
    AIL(3D_speaker_type, 4, ail_3D_speaker_type),
    AIL(set_3D_speaker_type, 8, ail_set_3D_speaker_type),
    AIL(3D_room_type, 4, ail_3D_room_type),
    AIL(set_3D_distance_factor, 8, ail_set_3D_distance_factor),
    AIL(set_3D_provider_preference, 12, ret0),
    AIL(3D_provider_attribute, 12, ret0),
    AIL(allocate_3D_sample_handle, 4, ail_allocate_3D_sample_handle),
    AIL(release_3D_sample_handle, 4, ail_release_3D_sample_handle),
    AIL(set_3D_sample_file, 8, ail_set_3D_sample_file),
    AIL(start_3D_sample, 4, ail_start_3D_sample),
    AIL(stop_3D_sample, 4, ail_stop_3D_sample),
    AIL(resume_3D_sample, 4, ail_resume_3D_sample),
    AIL(end_3D_sample, 4, ail_end_3D_sample),
    AIL(3D_sample_status, 4, ail_3D_sample_status),
    AIL(set_3D_sample_volume, 8, ail_set_3D_sample_volume),
    AIL(set_3D_sample_loop_count, 8, ail_set_3D_sample_loop_count),
    AIL(set_3D_sample_distances, 12, ail_set_3D_sample_distances),
    AIL(set_3D_sample_playback_rate, 8, ail_set_3D_sample_playback_rate),
    AIL(set_3D_sample_effects_level, 8, ail_set_3D_sample_effects_level),
    AIL(set_3D_sample_occlusion, 8, ail_set_3D_sample_occlusion),
    AIL(set_3D_position, 16, ail_set_3D_position),
    AIL(set_3D_orientation, 28, ail_set_3D_orientation),
    AIL(register_3D_EOS_callback, 8, ail_register_3D_eos_callback),
    AIL(3D_update_position, 8, ret0),
};

} // namespace

void mss32_frame_pump(X86 *c) {
    if (!c)
        return;
    for (auto &s : g_samples) {
        if (!s.alive)
            continue;
        if (sample_update(s) && s.eos_cb && !s.eos_fired) {
            // Mark before the call: the callback may stop or release the
            // sample, and re-entering the shim must not fire it twice.
            s.eos_fired = true;
            uint32_t cb = s.eos_cb, handle = s.handle;
            guest_call(c, cb, handle);
        }
    }
    for (auto &s : g_streams)
        if (s.alive)
            stream_update(s);

    // Miles timers, on the same seam. Each pass is bounded so a callback that
    // has slipped behind cannot run away inside one frame; the bound is per
    // timer, and a released timer is re-read rather than held as a reference.
    uint64_t now = os_monotonic_ns();
    for (size_t i = 0; i < g_ail_timers.size(); ++i) {
        for (int guard = 0; guard < 8; ++guard) {
            AilTimer &t = g_ail_timers[i];
            if (!t.alive || !t.running || now < t.next_ns)
                break;
            uint64_t period = 1000000000ull / std::max(1u, t.frequency);
            t.next_ns += period;
            uint32_t cb = t.callback, user = t.user;
            guest_call(c, cb, user);
            now = os_monotonic_ns();
        }
    }
}

// Drop everything that names the guest arena and the host channels before
// dx_reset discards them. The provider name block and the sample WAVE images
// are guest heap allocations; the digital drivers and channels are host state.
void mss32_reset() {
    for (auto &s : g_samples)
        if (s.alive && s.channel >= 0)
            host_audio_stop(s.channel);
    for (auto &s : g_streams)
        if (s.alive && s.channel >= 0)
            host_audio_stop(s.channel);
    g_samples.assign(g_samples.size(), Sample{});
    g_streams.assign(g_streams.size(), Stream{});
    g_ail_timers.assign(g_ail_timers.size(), AilTimer{});
    g_drivers.clear();
    g_last_digital_driver = 0;
    g_provider_names3d = 0;
    g_listener_pos3d[0] = g_listener_pos3d[1] = g_listener_pos3d[2] = 0.0f;
    g_listener_front3d[0] = 0.0f;
    g_listener_front3d[1] = 0.0f;
    g_listener_front3d[2] = 1.0f;
    g_listener_top3d[0] = 0.0f;
    g_listener_top3d[1] = 1.0f;
    g_listener_top3d[2] = 0.0f;
    g_rolloff3d = 1.0f;
}

void mss32_register() {
    static bool done = false;
    if (done)
        return;
    done = true;
    imports_register(g_mss32_shims, std::size(g_mss32_shims));
}
