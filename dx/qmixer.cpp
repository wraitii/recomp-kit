// qmixer.cpp - QMixer (QMixer.dll), the positional wave mixer the game uses
// for sound effects. 28 QSWaveMix* exports.
//
// Return conventions, read off the game's own call sites in the Ghidra
// decompilation rather than guessed:
//   QSWaveMixInitEx     returns a session handle, 0 on failure
//                       (0056f440: `if (iVar4 == 0) return 400;`)
//   QSWaveMixOpenWaveEx returns a wave handle, 0 on failure
//                       (0056f720: `if (iVar2 == 0) return 0xffffffff;`)
//   everything else     returns 0 on success, non-zero on failure
//                       (0056f440: `if (QSWaveMixActivate(...) != 0) return -1;`)
//
// QSWAVEMIXINITDATA is confirmed from the same function: dwSize at 0 (the
// game passes 0x40), dwFlags at 4, dwSamplingRate at 8 (the game passes
// 0x5622 = 22050).
//
// The export arities are confirmed against the real QMixer.dll (the trace
// bundle carries it): every one of the 28 registered here matches its `ret N`.
// Argument *order* is a different matter and is settled per call from what the
// game passes and from the library's own prologues where they are identical -
// see QSWaveMixOpenChannel and QSWaveMixEnableChannel, both of which were
// guessed wrong and both of which dropped sounds.
//
// QSWAVEMIXOPENWAVEDATA is now recovered rather than guessed: see the field
// map in dxtypes.h, taken from the disassembly of both call sites and of the
// routine that fills its format field. The waves are raw PCM plus an explicit
// WAVEFORMATEX, never RIFF files. Anything that does not match that contract
// is refused, so a sound that cannot be decoded is reported instead of being
// handed back as a silent handle.
#include "com.h"
#include "dx.h"
#include "ddraw.h"
#include "host_api.h"
#include "../runtime/memory.h"
#include "../runtime/win32.h"
#include "../platform/os.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <deque>
#include <unordered_map>
#include <vector>
#include <iterator>

namespace {

// The last error QMixer reported, process-wide: QSWaveMixGetLastError takes no
// handle (see below), so it cannot be answered per session.
uint32_t g_qs_last_error = 0;
const uint32_t QS_OK = 0;
const uint32_t QS_ERROR = 1;

// QMixer error values. The real library's numeric codes are NOT in the
// evidence: only the text path is (LHaudiodllR 0x10202f50/0x10202fc0 call
// GetLastError then GetErrorText(code, buf, 0x3f6)). These values are invented
// so the two calls can be implemented without guessing a real numbering; they
// are distinct and nonzero, and the 'QM' high word keeps them out of the
// Windows error space. SHIM(temporary), see docs/shims.md.
const uint32_t QSERR_NO_SESSION = 0x514d0001u;
const uint32_t QSERR_BAD_WAVE_RECORD = 0x514d0002u;
const uint32_t QSERR_BAD_FORMAT = 0x514d0003u;
const uint32_t QSERR_BAD_DATA = 0x514d0004u;
const uint32_t QSERR_NO_CHANNEL = 0x514d0005u;
const uint32_t QSERR_PLAY_FAILED = 0x514d0006u;

// A session. The game creates exactly one, but the handle space is real.
struct Session {
    uint32_t handle = 0;
    bool active = false;
    uint32_t sampling_rate = 22050;
    uint32_t flags = 0;
    uint32_t options = 0;
    bool alive = false;
    // The last failure this session reported. GetLastError reads it and
    // GetErrorText turns it into a string. Success does not clear it, matching
    // the usual GetLastError contract.
    uint32_t last_error = QS_OK;
};

// A wave: PCM located in guest memory, plus its format.
//
// A streamed wave has no samples of its own. It has a guest callback that
// fills a buffer on demand, and the buffer this shim allocates for it. The
// samples arrive a chunk at a time, pulled on the guest's own thread from
// QSWaveMixPump, which is what the game calls every frame for exactly this
// purpose.
struct Wave {
    uint32_t handle = 0;
    uint32_t pcm = 0; // guest address of the sample data
    uint32_t bytes = 0;
    uint32_t rate = 22050;
    uint32_t channels = 1;
    uint32_t bits = 8;
    bool alive = false;

    bool streamed = false;
    uint32_t callback = 0;    // guest LPFNQSWAVEMIXCALLBACK
    uint32_t context = 0;     // its pvContext, opaque to this shim
    uint32_t chunk_bytes = 0; // what one callback call is asked to fill
    uint32_t buffer = 0;      // guest heap, QSTREAM_CHUNKS * chunk_bytes
    uint32_t buffer_bytes = 0;
};

// How many callback-sized chunks are pulled and submitted as one host buffer.
// The host copies the samples when playback is submitted, so refilling guest
// memory in place is not heard; a longer submission is the only thing that
// makes the refill boundary rarer. Four keeps a boundary about every 1.4 s at
// the size the game asks for.
const uint32_t QSTREAM_CHUNKS = 4;

// A channel. QMixer channels are small integers the guest chooses, so the
// table is keyed by the guest's own index.
struct Channel {
    // The format of the sound the host voice was last started with, so a queued
    // wave can be appended only when it matches.
    uint32_t voice_rate = 0, voice_channels = 0, voice_bits = 0;
    // Waves submitted to the host voice, in play order, each with the completion
    // callback the guest named in its play parameters. `voice_total` is every
    // byte given to the voice since it was last started, so a wave is finished
    // when the voice has played past its `end`.
    struct Pending {
        uint32_t wave, end, callback, context;
    };
    std::deque<Pending> pending;
    uint64_t voice_total = 0;
    bool open = false;
    bool enabled = true;
    bool paused = false;
    bool playing = false;
    uint32_t wave = 0;          // handle of the wave last played
    int32_t audio_channel = -1; // the host mixer channel
    // Full scale, because that is what a channel nobody has set a volume on
    // has to be. Zero is silence on this scale, not unity, so the default
    // cannot be zero the way it can be for a value in decibels.
    int32_t volume = 32767, pan = 0;
    uint32_t frequency = 0;
    uint32_t config = 0;
    uint32_t enable_request = 1; // what EnableChannel last asked for
    // Set while a streamed wave is feeding this channel. The pump refills it;
    // nothing else has to know that the samples are arriving late.
    uint32_t stream_wave = 0;
    uint32_t stream_submitted = 0; // bytes handed to the host last time
    bool stream_ended = false;     // the callback said it had no more
    bool stream_loop = false;
    // How the host takes a refill. Not known until the first one is offered:
    // host_audio_queue reports 0 when the host cannot continue a sound, and
    // that answer decides the shape of every refill afterwards.
    enum StreamFeed : uint8_t { FEED_UNKNOWN, FEED_QUEUE, FEED_RESUBMIT };
    StreamFeed stream_feed = FEED_UNKNOWN;
    float position[3] = {0, 0, 0};
    float velocity[3] = {0, 0, 0};
};

// ---------------------------------------------------------------------------
// Requested against delivered.
//
// "Some sounds are missing" is not something a log of successes can answer.
// Every call the game makes is counted, and every play that does not reach the
// host is counted under the reason it did not, so a run says how many sounds
// were asked for, how many were heard, and what happened to the difference.
// The first few of each reason are printed with the detail that identifies
// them; after that only the totals move.
// ---------------------------------------------------------------------------
// RECOMP_AUDIO_TRACE=N also covers this module: the first N calls print their
// raw arguments. The QMixer SDK's argument order is not recoverable from the
// game's binary alone - the decompilation does not reach these call sites -
// so what the game actually passes is the only evidence there is.
int qm_trace_budget() {
    static int budget = -1;
    if (budget < 0) {
        const char *v = recomp_env("AUDIO_TRACE");
        budget = v ? (int)strtol(v, nullptr, 0) : 0;
    }
    return budget;
}
bool qm_trace_take() {
    static int left = -1;
    if (left < 0)
        left = qm_trace_budget();
    if (left <= 0)
        return false;
    --left;
    return true;
}
// Milliseconds since the first traced event. The comparison against a Wine
// snoop log of the original needs a clock on both sides - Wine supplies one
// with +timestamp - because "a play on a channel whose last sound had not
// finished" is a question about time and cannot be answered from order alone.
uint32_t qm_trace_ms() {
    uint64_t now = os_monotonic_ns() / 1000000ull;
    static uint64_t base = 0;
    if (!base)
        base = now;
    return (uint32_t)(now - base);
}

#define QTRACE(fmt, ...)                                                                           \
    do {                                                                                           \
        if (qm_trace_budget() && qm_trace_take())                                                  \
            LOGW("[%u ms] " fmt, qm_trace_ms(), __VA_ARGS__);                                      \
    } while (0)

struct Counters {
    uint32_t open_wave_static = 0, open_wave_streamed = 0;
    uint32_t open_wave_refused_rec = 0, open_wave_refused_fmt = 0;
    uint32_t open_wave_refused_data = 0, open_wave_refused_stream = 0;
    uint32_t open_wave_no_session = 0;
    uint32_t play_calls = 0, play_delivered = 0, play_queued_static = 0, completions = 0;
    uint32_t drop_no_session = 0, drop_no_wave = 0, drop_no_channel = 0;
    uint32_t drop_disabled = 0, drop_paused = 0, drop_inactive = 0;
    uint32_t drop_no_voice = 0, drop_empty_wave = 0, drop_stream_dry = 0;
    uint32_t open_channel = 0, enable_on = 0, enable_off = 0, configure = 0;
    uint32_t set_volume = 0, set_frequency = 0, set_position = 0;
    uint32_t set_distance = 0, set_cone = 0;
    uint32_t host_plays = 0, host_queues_ok = 0, host_queues_refused = 0;
    uint32_t stop_calls = 0, pause_calls = 0, free_wave = 0;
    uint32_t pump_calls = 0, pump_refills = 0, pump_skipped = 0, frame_pumps = 0;
    int32_t loudest_volume = -1, quietest_volume = 0x7fffffff;
    // A range alone invited the wrong inference: a minimum of 0 reads as "the
    // game is silencing sounds" when it was one sound out of 779. The counts
    // are what say whether a quiet extreme is the rule or the exception.
    uint32_t volume_silent = 0, volume_quiet = 0;
};

Counters &counters() {
    static Counters c;
    return c;
}

// The first few reasons are worth a line each; the rest are worth a number.
void drop_reason(const char *why, uint32_t idx, uint32_t detail) {
    static uint32_t said = 0;
    if (said >= 20)
        return;
    ++said;
    LOGW("qmixer: play on channel %u did not reach the host: %s (%u)", idx, why, detail);
}

std::vector<Session> &sessions() {
    static auto *v = new std::vector<Session>();
    return *v;
}
// Wave handle (a guest-heap address) -> index in waves().
std::unordered_map<uint32_t, size_t> &wave_index() {
    static auto *m = new std::unordered_map<uint32_t, size_t>();
    return *m;
}
std::vector<Wave> &waves() {
    static auto *v = new std::vector<Wave>();
    return *v;
}
std::vector<Channel> &channels() {
    static auto *v = new std::vector<Channel>();
    return *v;
}

// QMixer's volume is a linear amplitude on 0 to 32767, and this is not a
// guess: QMixer.dll's parameter handler for the volume tag is
//
//     18006d0b  fild  dword ptr [edi + 4]        ; the value the caller passed
//     18006d0e  fmul  dword ptr [0x180244d4]     ; 3.051851e-05, which is 1/32767
//     ...       fstp  dword ptr [esi + 0x50]     ; the channel's gain
//
// so gain is value/32767 and full scale is 32767. The game passes 14190, which
// is a gain of 0.433, or -7.3 dB - not the full volume it was being played at
// while this was read as hundredths of a decibel and every positive number
// clamped to unity.
//
// The host takes hundredths of a decibel, so the conversion is the logarithm.
// Zero is silence rather than -0 dB, which the linear form says and the
// logarithmic one cannot.
int32_t qmix_volume_to_millibels(int32_t v) {
    if (v <= 0)
        return -10000;
    if (v >= 32767)
        return 0;
    double mb = 2000.0 * log10((double)v / 32767.0);
    if (mb < -10000.0)
        mb = -10000.0;
    if (mb > 0.0)
        mb = 0.0;
    return (int32_t)(mb < 0 ? mb - 0.5 : mb + 0.5);
}

float g_listener_pos[3] = {0, 0, 0};
float g_listener_front[3] = {0, 0, 1};
float g_listener_top[3] = {0, 1, 0};
float g_listener_vel[3] = {0, 0, 0};
float g_speed_of_sound = 331.5f;
uint32_t g_speaker_placement = 0;

const uint32_t SESSION_HANDLE_BASE = 0x00510000u;

Session *session_for(uint32_t h) {
    if (h < SESSION_HANDLE_BASE)
        return nullptr;
    uint32_t i = h - SESSION_HANDLE_BASE;
    if (i >= sessions().size())
        return nullptr;
    return sessions()[i].alive ? &sessions()[i] : nullptr;
}

// A wave handle is the address of a small guest-heap record, because the guest
// dereferences it: LHaudiodllR 0x10211f2d does `MOV EDX,[EAX+4]` on the value
// OpenWaveEx returned and keeps that as the wave's sample rate, from which it
// derives every SetFrequency it issues. Handles that were integers in the
// executable's own address range made that read return code bytes, so every
// sound was played at a nonsense rate (high-pitched static).
Wave *wave_for(uint32_t h) {
    auto it = wave_index().find(h);
    if (it == wave_index().end() || it->second >= waves().size())
        return nullptr;
    return waves()[it->second].alive ? &waves()[it->second] : nullptr;
}

// Channels are indexed by the guest's own number, capped so a wild index
// cannot grow the table without bound.
const uint32_t MAX_CHANNELS = 64;
Channel *channel_for(uint32_t i, bool create = false) {
    if (i >= MAX_CHANNELS)
        return nullptr;
    if (channels().size() <= i) {
        if (!create)
            return nullptr;
        channels().resize(i + 1);
    }
    Channel &ch = channels()[i];
    if (!ch.open && !create)
        return nullptr;
    return &ch;
}

// Reads three consecutive floats from a guest vector pointer.
void read_vec3(uint32_t a, float out[3]) {
    if (!a || !gm_valid(a, 12))
        return;
    for (int i = 0; i < 3; ++i)
        out[i] = rdf32(a + 4u * (uint32_t)i);
}

// ---------------------------------------------------------------------------
// Opening a wave
// ---------------------------------------------------------------------------
// QSWAVEMIXOPENWAVEDATA is the five-dword record documented in dxtypes.h,
// recovered from the disassembly of both call sites. The game supplies raw PCM
// plus an explicit WAVEFORMATEX; there is no RIFF container anywhere on this
// path, so nothing here looks for one.
//
// Anything that does not match the recovered contract fails closed: the wave
// handle is refused rather than handed back silent, because a handle that
// plays nothing is indistinguishable from working audio to everything
// upstream. The game already handles the failure, returning -1 from its own
// loader at 0x56f720.
// The memory-file form (flag 0x4): the record's first dword points at an
// MMIOINFO with fccIOProc 'MEM ', cchBuffer at +0x14 and pchBuffer at +0x18,
// and QMixer parses the RIFF/WAVE image itself. Evidence: LHaudiodllR
// 0x10211dad..0x10211ef7 (and the same shape at 0x10211d66) clear an 18-dword
// MMIOINFO, store 'MEM ' at +4 and the size and data pointer at +0x14/+0x18,
// then call OpenWaveEx with flags 4 and a record whose field 0 points at it;
// RECOMP_TRACE_IMPORTS showed the call arriving with exactly that.
// Only PCM is played. A compressed tag (ADPCM 2, MPEG 0x55, ...) is refused by
// name: decoding it is a separate piece of work, not something to guess at.
// Diagnostic, bounded: the raw format bytes and the first sample bytes of the
// first waves opened, so a wrong rate or format can be read off the log against
// the bytes the guest actually supplied (RECOMP_TRACE_IMPORTS shows only the
// pointers).
void dump_wave_bytes(const char *form, uint32_t fmt, uint32_t data, uint32_t bytes) {
    static int said = 0;
    if (said >= 24)
        return;
    ++said;
    char f[3 * 20 + 1] = {}, d[3 * 16 + 1] = {};
    if (gm_valid(fmt, 20))
        for (int i = 0; i < 20; ++i)
            snprintf(f + 3 * i, 4, "%02x ", rd8(fmt + (uint32_t)i));
    if (data && bytes >= 16 && gm_valid(data, 16))
        for (int i = 0; i < 16; ++i)
            snprintf(d + 3 * i, 4, "%02x ", rd8(data + (uint32_t)i));
    LOGW("qmixer: wave open (%s) fmt@%08x: %s| data@%08x (%u bytes): %s", form, fmt, f, data, bytes,
         d);
}

bool read_riff_wave(uint32_t info, Wave *w, uint32_t *error) {
    const auto fail = [&](uint32_t code) {
        if (error)
            *error = code;
        return false;
    };
    if (!gm_valid(info, 0x24) || rd32(info + 4) != 0x204d454du /* 'MEM ' */) {
        log_once("qmixer.mmio",
                 "qmixer: OpenWaveEx flag 4 record does not point at a 'MEM ' "
                 "MMIOINFO (%08x); refusing the wave",
                 info);
        ++counters().open_wave_refused_rec;
        return fail(QSERR_BAD_WAVE_RECORD);
    }
    const uint32_t size = rd32(info + 0x14), base = rd32(info + 0x18);
    if (size < 12 || !gm_fits(base, size) || rd32(base) != 0x46464952u /* RIFF */ ||
        rd32(base + 8) != 0x45564157u /* WAVE */) {
        log_once("qmixer.riff",
                 "qmixer: OpenWaveEx memory image at %08x (%u bytes) is not a "
                 "RIFF/WAVE; refusing the wave",
                 base, size);
        ++counters().open_wave_refused_data;
        return fail(QSERR_BAD_DATA);
    }
    bool have_fmt = false;
    uint32_t fmt_at = 0;
    uint32_t data = 0, bytes = 0;
    uint32_t pos = 12;
    while (pos + 8 <= size) {
        const uint32_t id = rd32(base + pos), len = rd32(base + pos + 4), body = pos + 8;
        if (body > size)
            break;
        const uint32_t avail = size - body < len ? size - body : len; // a truncated last chunk
        if (id == 0x20746d66u /* 'fmt ' */ && avail >= 16 && !have_fmt) {
            const uint16_t tag = rd16(base + body + WFX_OFF_wFormatTag);
            if (tag != WAVE_FORMAT_PCM) {
                log_once("qmixer.riff.tag",
                         "qmixer: OpenWaveEx RIFF wave has format tag 0x%x (not PCM); compressed "
                         "waves are not decoded here; refusing the wave",
                         tag);
                ++counters().open_wave_refused_fmt;
                return fail(QSERR_BAD_FORMAT);
            }
            w->channels = rd16(base + body + WFX_OFF_nChannels);
            w->rate = rd32(base + body + WFX_OFF_nSamplesPerSec);
            w->bits = rd16(base + body + WFX_OFF_wBitsPerSample);
            fmt_at = base + body;
            have_fmt = true;
        } else if (id == 0x61746164u /* 'data' */ && !data) {
            data = base + body;
            bytes = avail;
        }
        pos = body + len + (len & 1u); // chunks are word aligned
    }
    if (!have_fmt || !data || !bytes) {
        log_once("qmixer.riff.chunks",
                 "qmixer: OpenWaveEx RIFF wave at %08x has no usable "
                 "'fmt '/'data' chunks; refusing the wave",
                 base);
        ++counters().open_wave_refused_data;
        return fail(QSERR_BAD_DATA);
    }
    if (!w->channels || w->channels > 2 || !w->rate || (w->bits != 8 && w->bits != 16)) {
        log_once("qmixer.riff.vals",
                 "qmixer: OpenWaveEx RIFF format is %u Hz, %u channels, %u bits, which is not "
                 "something this mixer can play",
                 w->rate, w->channels, w->bits);
        ++counters().open_wave_refused_fmt;
        return fail(QSERR_BAD_FORMAT);
    }
    dump_wave_bytes("riff", fmt_at, data, bytes);
    w->pcm = data;
    w->bytes = bytes;
    return true;
}

bool read_wave_record(uint32_t rec, uint32_t flags, Wave *w, uint32_t *error) {
    const auto fail = [&](uint32_t code) {
        if (error)
            *error = code;
        return false;
    };
    if (!rec || !gm_valid(rec, QSWAVEMIXOPENWAVEDATA_SIZE)) {
        log_once("qmixer.rec", "qmixer: OpenWaveEx record at %08x is not 20 readable bytes", rec);
        ++counters().open_wave_refused_rec;
        return fail(QSERR_BAD_WAVE_RECORD);
    }

    // Field 0 is the format, written at every call site - except the memory
    // file form, where it is the MMIOINFO of a RIFF image.
    if (flags & 0x4u)
        return read_riff_wave(rd32(rec + QSOWD_OFF_lpFormat), w, error);
    uint32_t fmt = rd32(rec + QSOWD_OFF_lpFormat);
    if (!fmt || !gm_valid(fmt, 16)) {
        log_once("qmixer.fmt", "qmixer: OpenWaveEx format pointer %08x is not readable", fmt);
        ++counters().open_wave_refused_fmt;
        return fail(QSERR_BAD_FORMAT);
    }
    uint16_t tag = rd16(fmt + WFX_OFF_wFormatTag);
    if (tag != WAVE_FORMAT_PCM) {
        log_once("qmixer.tag",
                 "qmixer: OpenWaveEx format tag %u is not PCM; refusing the wave "
                 "rather than playing the bytes as if it were",
                 tag);
        ++counters().open_wave_refused_fmt;
        return fail(QSERR_BAD_FORMAT);
    }
    w->channels = rd16(fmt + WFX_OFF_nChannels);
    w->rate = rd32(fmt + WFX_OFF_nSamplesPerSec);
    w->bits = rd16(fmt + WFX_OFF_wBitsPerSample);
    if (!w->channels || w->channels > 2 || !w->rate || (w->bits != 8 && w->bits != 16)) {
        log_once("qmixer.fmtvals",
                 "qmixer: OpenWaveEx format is %u Hz, %u channels, %u bits, which "
                 "is not something this mixer can play",
                 w->rate, w->channels, w->bits);
        ++counters().open_wave_refused_fmt;
        return fail(QSERR_BAD_FORMAT);
    }

    // The streaming form carries a callback instead of the samples: field 1 is
    // the size the callback is asked to fill, field 2 the callback and field 3
    // its context. Recovered from the game's own call site at 0056f8da, which
    // zeroes the twenty-byte record and then writes exactly those four fields,
    // with a length of 0x7800 or 0x3c00 depending on the format.
    //
    // The callback is stdcall with three arguments, (lpBuffer, dwBytes,
    // pvContext) in that order, and 0x576660 ends in `RET 0xc`. Both the
    // argument order and the meaning of the result are read off the game's own
    // reuse of the same callback in its DirectSound streamer at 0056f090,
    // which is the only place either is stated:
    //
    //   0056f10a  PUSH EAX ; PUSH 0x3c00 ; LEA EAX,[ESI+0x34] ; PUSH EAX
    //             CALL 0x00576660                 -> (buffer, bytes, context)
    //   0056f121  TEST EAX,EAX ; JNZ ... ; OR [record],1
    //
    // so a *zero* result is what marks the stream finished, not what marks it
    // healthy: non-zero means more will follow. And the chunk is used either
    // way - the copy into the sound buffer at 0056f12c runs whatever the
    // callback returned - so the last chunk is played, not discarded.
    if (flags & QSWAVEMIX_STREAMED) {
        uint32_t chunk = rd32(rec + QSOWD_OFF_lpData);
        uint32_t cb = rd32(rec + QSOWD_OFF_pfnCallback);
        uint32_t ctx = rd32(rec + QSOWD_OFF_pvContext);
        if (!cb) {
            log_once("qmixer.streamcb", "qmixer: OpenWaveEx asked for a streamed wave with no "
                                        "callback; there is no way to obtain its samples");
            ++counters().open_wave_refused_stream;
            return fail(QSERR_BAD_WAVE_RECORD);
        }
        if (!chunk || chunk > (1u << 22)) {
            log_once("qmixer.streamlen",
                     "qmixer: OpenWaveEx streamed wave asks for %u-byte chunks, "
                     "which is not a size this mixer will allocate",
                     chunk);
            ++counters().open_wave_refused_stream;
            return fail(QSERR_BAD_FORMAT);
        }
        // Whole frames only: half a sample frame in a chunk would put every
        // later chunk out of phase with the channel interleave.
        uint32_t align = w->channels * (w->bits / 8);
        if (align)
            chunk -= chunk % align;
        if (!chunk)
            return fail(QSERR_BAD_FORMAT);
        w->streamed = true;
        w->callback = cb;
        w->context = ctx;
        w->chunk_bytes = chunk;
        w->buffer_bytes = chunk * QSTREAM_CHUNKS;
        w->buffer = heap_alloc(w->buffer_bytes, true, 16);
        if (!w->buffer) {
            log_once("qmixer.streambuf", "qmixer: no guest memory for a %u-byte streaming buffer",
                     w->buffer_bytes);
            ++counters().open_wave_refused_stream;
            return fail(QSERR_BAD_DATA);
        }
        w->pcm = w->buffer;
        w->bytes = 0; // nothing pulled yet
        return true;
    }

    uint32_t data = rd32(rec + QSOWD_OFF_lpData);
    uint32_t bytes = rd32(rec + QSOWD_OFF_dwDataSize);
    if (!data || !bytes || !gm_fits(data, (uint64_t)bytes)) {
        log_once("qmixer.data",
                 "qmixer: OpenWaveEx sample data at %08x for %u bytes is not "
                 "inside guest memory",
                 data, bytes);
        ++counters().open_wave_refused_data;
        return fail(QSERR_BAD_DATA);
    }
    dump_wave_bytes("record", fmt, data, bytes);
    w->pcm = data;
    w->bytes = bytes;
    return true;
}

void complete_wave(X86 *c, uint32_t channel, const Channel::Pending &p, const char *why);

// Delivers the completion callbacks detached by a stop.
//
// The real QMixer completes a channel's queued waves when it flushes, stops or
// replaces it, and LHaudiodllR depends on that: its sample callback 0x102108c0
// is the only thing that clears a sample's playing flag at +0x8c, so a wave
// whose callback is dropped leaves SaySoundEffectPlaying true forever (the
// Land1 FollowUs wait). Evidence in QMixer.dll: FlushChannel (0x18001810) and
// StopChannel (0x18004990) reach fcn.1800a380, which calls fcn.1800c7d0; that
// routine invokes the play-parameters callback stored on the voice node at
// +0x5c with its context at +0x60. The flush/stop wrappers at 0x18007580 /
// 0x18007520 walk the channel's pending wave list and call it for every node.
// This runs on the guest thread that called the shim, the same context the
// frame pump uses.
void deliver_pending(X86 *c, uint32_t idx, std::deque<Channel::Pending> &finished,
                     const char *why = "stopped") {
    if (!c)
        return;
    for (const Channel::Pending &p : finished) {
        ++counters().completions;
        complete_wave(c, idx, p, why);
    }
}

void stop_channel(X86 *c, Channel *ch, uint32_t idx) {
    // Detach the callbacks before the reset so a callback that starts a new
    // play on this channel is not wiped out by the reset below.
    std::deque<Channel::Pending> finished;
    finished.swap(ch->pending);
    if (ch->playing && ch->audio_channel >= 0)
        host_audio_stop(ch->audio_channel);
    ch->playing = false;
    ch->voice_total = 0;
    ch->stream_wave = 0;
    ch->stream_submitted = 0;
    ch->stream_ended = false;
    ch->stream_feed = Channel::FEED_UNKNOWN;
    deliver_pending(c, idx, finished);
}

// ---------------------------------------------------------------------------
// Streaming
//
// The callback runs on the guest's own thread, inside whichever shim called
// this, so it is an ordinary guest call: nothing here may be reached from a
// host audio thread, because the callback re-enters translated code and only
// the thread holding the scheduler baton may do that. QSWaveMixPump is the
// game's per-frame service call and is where the refill happens.
// ---------------------------------------------------------------------------

// Fills the wave's buffer by calling the guest back up to QSTREAM_CHUNKS
// times. Returns how many bytes were obtained; sets `ended` when the callback
// reported it had no more to give.
// How many of the game's stream buffers to pull in one go.
//
// This was QSTREAM_CHUNKS, all four, on every pull. That is not what the
// library does and the game can tell. The size the game asks OpenWaveEx for
// is the size of one buffer - 0x3c00 at 0056f90a, or 0x7800 when the flag at
// [edi+0x6a] is set - and QMixer fills that one buffer and calls back when it
// needs the next. Pulling four at once runs the game's own stream reader
// about 1.4 seconds ahead of anything audible, and the reader is the game's
// object, not ours: the callback at 00576660 forwards to a virtual method on
// it and returns end-of-stream when that method returns 1, so draining the
// source is a state change in the game's emitter, not a private buffering
// decision of the mixer's.
//
// One per pull, not two, because the play path already primes two: it pulls a
// buffer to submit and then pulls the next straight into the host's queue, so
// a sound starts with one playing and one waiting, which is what a double
// buffer is. Asking for two per pull made that four again.
// RECOMP_QMIX_CHUNKS overrides the per-pull count so its effect stays
// measurable. It overrides that count and nothing else: the pump's top-up
// threshold below is one buffer either way, so setting it to four does not
// restore the whole of what this replaced.
uint32_t stream_chunks() {
    static uint32_t n = 0;
    if (!n) {
        n = 1;
        if (const char *e = recomp_env("QMIX_CHUNKS")) {
            uint32_t v = (uint32_t)strtoul(e, nullptr, 10);
            if (v >= 1 && v <= QSTREAM_CHUNKS)
                n = v;
        }
    }
    return n;
}

// RECOMP_QMIX_GATE=queue puts the refill gate back on host_audio_queued_bytes.
//
// Here for the same reason RECOMP_QMIX_CHUNKS is: the switch to the voice query
// is justified by the contract - queued_bytes counts only what THIS caller
// appended and a play resets it, while QMixer plays on a named channel and
// refills behind it - but a contract argument is not a measurement, and I
// attached a play count to this change that turned out to sit inside the
// harness's own run-to-run spread. A knob makes the difference measurable over
// as many runs as it takes, on the numbers the gate actually moves: refills
// per play, and dropouts.
int &gate_cache() {
    static int v = -1;
    return v;
}

bool gate_reads_queue() {
    int &v = gate_cache();
    if (v < 0) {
        const char *e = recomp_env("QMIX_GATE");
        v = (e && strcmp(e, "queue") == 0) ? 1 : 0;
    }
    return v != 0;
}

uint32_t pull_stream(X86 *c, Wave *w, bool *ended) {
    *ended = false;
    if (!w->streamed || !w->buffer || !w->callback)
        return 0;
    uint32_t got = 0;
    const uint32_t want = stream_chunks();
    for (uint32_t i = 0; i < want; ++i) {
        uint32_t dst = w->buffer + got;
        // The callback is not obliged to touch every byte it is given, so the
        // chunk is cleared first: a short fill is then silence rather than the
        // previous chunk played a second time.
        gm_zero(dst, w->chunk_bytes);
        uint32_t r = guest_call(c, w->callback, dst, w->chunk_bytes, w->context);
        // The chunk counts whatever the result was. Zero says this was the
        // last one, not that it was bad; throwing it away would clip the end
        // off every stream and, since the game returns non-zero for the whole
        // healthy body of a track, throwing away the first chunk of every one.
        got += w->chunk_bytes;
        if (r == 0) {
            *ended = true;
            break;
        }
    }
    return got;
}

// The loudest sample in a block of guest PCM, as a fraction of full scale.
// Measured on this side of host_audio_play so that a silent run says which
// side of the boundary lost the sound: the host prints the same number for
// what it decoded, and the two together name the broken link.
float pcm_peak(uint32_t addr, uint32_t bytes, uint32_t bits) {
    if (!addr || !bytes || !gm_fits(addr, (uint64_t)bytes))
        return 0.0f;
    const uint8_t *p = (const uint8_t *)gm_ptr(addr);
    float peak = 0.0f;
    if (bits == 8) {
        for (uint32_t i = 0; i < bytes; ++i) {
            float v = ((float)p[i] - 128.0f) / 128.0f;
            if (v < 0)
                v = -v;
            if (v > peak)
                peak = v;
        }
    } else {
        for (uint32_t i = 0; i + 1 < bytes; i += 2) {
            int16_t sv = (int16_t)(uint16_t)(p[i] | (p[i + 1] << 8));
            float v = (float)sv / 32768.0f;
            if (v < 0)
                v = -v;
            if (v > peak)
                peak = v;
        }
    }
    return peak;
}

// One line per wave, the first time it is heard, so a live run explains
// itself without a per-refill flood.
std::vector<uint32_t> &announced() {
    static auto *v = new std::vector<uint32_t>();
    return *v;
}

void announce_once(const Channel *ch, const Wave *w, uint32_t addr, uint32_t bytes) {
    for (uint32_t h : announced())
        if (h == w->handle)
            return;
    announced().push_back(w->handle);
    // The guest's channel index as well as the host voice. They are not the
    // same question: the guest's says whether it meant these sounds to share a
    // voice and cut each other off, and the host's only says where they went.
    uint32_t idx = 0;
    for (uint32_t i = 0; i < channels().size(); ++i)
        if (&channels()[i] == ch) {
            idx = i;
            break;
        }
    // The peak of the whole wave and the peak of its first twentieth. A play
    // arrives on the busy channels every 75 ms onto waves averaging 1544 ms,
    // so about a twentieth of each is what actually gets heard - and if that
    // opening twentieth is the quiet attack of a speech sample, the effects
    // are continuously present and barely measurable, which is what "muffled"
    // sounds like and what a level meter over a whole run reports as nothing.
    // Two numbers rather than one, because the whole-wave peak alone says the
    // sound is loud when the part being played is not.
    uint32_t head = bytes / 20;
    uint32_t align = w->channels * (w->bits / 8);
    if (align)
        head -= head % align;
    if (!head)
        head = bytes;
    LOGW("qmixer: [%u ms] wave %08x first play on QMixer channel %u, host channel %d: "
         "%u Hz, %u ch, %u bit, %u bytes, %s, volume %d of 32767 (%d mB), "
         "peak %.3f, first 5%% peak %.3f",
         qm_trace_ms(), w->handle, idx, ch->audio_channel, ch->frequency ? ch->frequency : w->rate,
         w->channels, w->bits, bytes, w->streamed ? "streamed" : "static", ch->volume,
         qmix_volume_to_millibels(ch->volume), (double)pcm_peak(addr, bytes, w->bits),
         (double)pcm_peak(addr, head, w->bits));
}

// Hands the wave's current buffer contents to the host on the channel's voice.
void submit_stream(Channel *ch, const Wave *w, uint32_t bytes) {
    HostAudioPlay p;
    memset(&p, 0, sizeof p);
    p.channel = ch->audio_channel;
    p.pcm = gm_ptr(w->buffer);
    p.bytes = bytes;
    p.sample_rate = (int32_t)(ch->frequency ? ch->frequency : w->rate);
    p.channels = (int32_t)w->channels;
    p.bits = (int32_t)w->bits;
    p.loop = 0; // a stream is refilled, never looped by the host
    p.volume = qmix_volume_to_millibels(ch->volume);
    p.pan = ch->pan;
    announce_once(ch, w, w->buffer, bytes);
    host_audio_play(&p);
    ++counters().host_plays;
    ch->stream_submitted = bytes;
}

// ---------------------------------------------------------------------------
// Session and channel management
// ---------------------------------------------------------------------------
void QSWaveMixInitEx(X86 *c) {
    QTRACE("qmixer: QSWaveMixInitEx(%08x, %08x, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    uint32_t init = arg(c, 0);
    Session s;
    s.alive = true;
    if (init && gm_valid(init, 12)) {
        uint32_t size = rd32(init + 0);
        s.flags = rd32(init + 4);
        uint32_t rate = rd32(init + 8);
        if (rate >= 4000 && rate <= 192000)
            s.sampling_rate = rate;
        LOGV("qmixer: InitEx size=%u flags=%08x rate=%u", size, s.flags, s.sampling_rate);
    }
    sessions().push_back(s);
    uint32_t h = SESSION_HANDLE_BASE + (uint32_t)sessions().size() - 1;
    sessions().back().handle = h;
    set_eax(c, h);
}

void QSWaveMixActivate(X86 *c) {
    QTRACE("qmixer: QSWaveMixActivate(%08x, %08x, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    if (!s) {
        set_eax(c, QS_ERROR);
        return;
    }
    s->active = arg(c, 1) != 0;
    if (!s->active)
        for (size_t i = 0; i < channels().size(); ++i)
            stop_channel(c, &channels()[i], (uint32_t)i);
    set_eax(c, QS_OK);
}

// Allocate the requested mixer channels and initialize their guest-visible state.
// Reject unsupported channel ranges before publishing any handles.
void QSWaveMixOpenChannel(X86 *c) {
    QTRACE("qmixer: QSWaveMixOpenChannel(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    ++counters().open_channel;
    Session *s = session_for(arg(c, 0));
    uint32_t idx = arg(c, 1);
    uint32_t flags = arg(c, 2);
    if (!s) {
        set_eax(c, QS_ERROR);
        return;
    }
    Channel *ch = channel_for(idx, true);
    if (!ch) {
        LOGW("qmixer: OpenChannel for channel %u is outside the %u the shim keeps", idx,
             MAX_CHANNELS);
        set_eax(c, QS_ERROR);
        return;
    }
    // The third argument selects what `idx` means, and QMixer.dll settles the
    // range: QSWaveMixOpenChannel switches on it with `cmp eax,3 / ja` and a
    // four-entry jump table, so it is 0 to 3. The game passes 2 with idx 15,
    // and then plays on channels 0 upwards - so 2 is a count, not an index,
    // and reading it as an index leaves every channel the game actually uses
    // unopened. That does not silence anything on its own, because a play
    // opens its channel on demand, but it means the shim's idea of the
    // session bears no relation to the guest's.
    uint32_t first = idx, count = 1;
    switch (flags) {
    case 1:
        first = 0;
        count = MAX_CHANNELS;
        break; // all of them
    case 2:
        first = 0;
        count = idx;
        break; // this many, from 0
    case 0:
        break; // just this one
    default:
        log_once("qmixer.openflags",
                 "qmixer: OpenChannel flags %08x is outside the four the "
                 "library takes; treating %u as a single channel",
                 flags, idx);
        break;
    }
    if (count > MAX_CHANNELS)
        count = MAX_CHANNELS;
    uint32_t opened = 0;
    for (uint32_t i = 0; i < count; ++i) {
        Channel *one = channel_for(first + i, true);
        if (!one)
            break;
        one->open = true;
        one->enabled = true;
        one->config = flags;
        ++opened;
    }
    if (!opened) {
        set_eax(c, QS_ERROR);
        return;
    }
    LOGV("qmixer: opened %u channel%s from %u (flags %08x)", opened, opened == 1 ? "" : "s", first,
         flags);
    set_eax(c, QS_OK);
}

void QSWaveMixCloseSession(X86 *c) {
    QTRACE("qmixer: QSWaveMixCloseSession(%08x, %08x, %08x, %08x, %08x, %08x)", arg(c, 0),
           arg(c, 1), arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    if (!s) {
        set_eax(c, QS_ERROR);
        return;
    }
    for (size_t i = 0; i < channels().size(); ++i) {
        Channel &ch = channels()[i];
        stop_channel(c, &ch, (uint32_t)i);
        if (ch.audio_channel >= 0) {
            dx_free_audio_channel(ch.audio_channel);
            ch.audio_channel = -1;
        }
        ch.open = false;
    }
    for (Wave &w : waves()) {
        if (w.buffer) {
            heap_free(w.buffer);
            w.buffer = 0;
            w.buffer_bytes = 0;
        }
        w.pcm = 0;
        w.bytes = 0;
        if (w.alive && w.handle) { // a wave the game never freed still owns its handle record
            wave_index().erase(w.handle);
            heap_free(w.handle);
        }
        w.alive = false;
    }
    s->alive = false;
    set_eax(c, QS_OK);
}

void QSWaveMixSetOptions(X86 *c) {
    QTRACE("qmixer: QSWaveMixSetOptions(%08x, %08x, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    if (!s) {
        set_eax(c, QS_ERROR);
        return;
    }
    // (hMix, dwMask, dwOptions): only the masked bits change.
    uint32_t mask = arg(c, 1), options = arg(c, 2);
    s->options = (s->options & ~mask) | (options & mask);
    set_eax(c, QS_OK);
}

// Services every streamed wave: refills the ones that have run low by calling
// the guest back for more, and hands what it gets to the host.
//
// This is called from the frame pump rather than only from QSWaveMixPump,
// because the game never calls QSWaveMixPump. Real QMixer runs its own mixing
// thread; the Pump export is for an application that wants to drive the mixer
// by hand, and this one does not - zero calls in a whole run. Everything here
// re-enters translated code through the guest callback, so it may only run on
// a thread holding the scheduler baton, which is what the frame pump is.
void pump_streams(X86 *c) {
    for (Channel &ch : channels()) {
        if (!ch.playing || !ch.stream_wave || ch.audio_channel < 0)
            continue;
        Wave *w = wave_for(ch.stream_wave);
        if (!w || !w->streamed) {
            ch.stream_wave = 0;
            continue;
        }

        // When the host can continue a sound, the only question is how much is
        // still waiting to be heard: hand over the next buffer well before
        // that reaches zero, because a queue that drains is a seam.
        //
        // When it cannot, the samples were copied at submit time and the
        // buffer cannot be refilled until the voice has finished reading it,
        // which puts the refill boundary at a buffer edge and is audible.
        //
        // A channel knows which host it has from the moment it starts -
        // QSWaveMixPlayEx converts it and records the answer - so the second
        // test is for a host with no streaming and nothing else. An unknown
        // channel is treated as a queue rather than left to the position test,
        // because that test only fires once the voice has run out, which is
        // the one moment a refill must not be waiting for.
        uint32_t align = w->channels * (w->bits / 8);
        if (!align)
            align = 1;
        if (ch.stream_feed != Channel::FEED_RESUBMIT) {
            // One buffer of slack, which is the buffer the game asked for.
            // Refilling only when nothing is left is a seam waiting to happen,
            // so the top-up starts as soon as the voice has less than a chunk
            // in front of it, leaving one playing and one waiting.
            //
            // The quantity is host_audio_voice_remaining_bytes and not
            // host_audio_queued_bytes, which is what this used to read. The
            // two answer different questions on purpose. queued_bytes is the
            // DirectSound streaming contract - how much of what THIS CALLER
            // appended is still to play - and a Play resets it, because a Play
            // starts a new sound rather than continuing one. QMixer does not
            // append into a ring; it plays a sound on a named channel and
            // refills behind it, so a Play is exactly the moment it needs the
            // answer to be large. Reading queued_bytes told it nothing was
            // outstanding thirty milliseconds into a second-and-a-half sound,
            // and it refilled 635 times in run 15 (impl-t7's measurement).
            //
            // Trying to make one query serve both is what impl-t7 measured and
            // rejected: folding the voice's own sound into queued_bytes took
            // the headless intro from no dropouts to thirty-one silent gaps,
            // because a ring writer then waits out a whole re-issued lap
            // before appending. Two queries is the shape that keeps both
            // callers right, not the untidy one.
            //
            // Separately, this count was two chunks and the pull was four:
            // the queue held up to five buffers and the game's stream reader
            // ran nearly two seconds ahead of anything audible, which the game
            // could see, because that reader is its own emitter.
            uint32_t ahead = gate_reads_queue()
                                 ? host_audio_queued_bytes(ch.audio_channel)
                                 : host_audio_voice_remaining_bytes(ch.audio_channel);
            if (ahead >= w->chunk_bytes) {
                ++counters().pump_skipped;
                continue;
            }
        } else {
            uint32_t pos = host_audio_position(ch.audio_channel);
            if (ch.stream_submitted > align && pos + align < ch.stream_submitted) {
                ++counters().pump_skipped;
                continue;
            }
        }
        ++counters().pump_refills;
        QTRACE("qmixer: refill channel %d, feed %d, queued %u of %u chunk, "
               "submitted %u",
               ch.audio_channel, (int)ch.stream_feed, host_audio_queued_bytes(ch.audio_channel),
               w->chunk_bytes, ch.stream_submitted);

        if (ch.stream_ended) {
            // The source had no more to give and the last buffer has now been
            // heard. Looping restarts the stream; otherwise the channel stops.
            if (!ch.stream_loop) {
                ch.playing = false;
                ch.stream_wave = 0;
                ch.stream_submitted = 0;
                continue;
            }
            ch.stream_ended = false;
        }
        bool ended = false;
        uint32_t got = pull_stream(c, w, &ended);
        ch.stream_ended = ended;
        if (!got) {
            ch.playing = false;
            ch.stream_wave = 0;
            ch.stream_submitted = 0;
            continue;
        }
        // Continue the sound if the host can; start it again if it cannot.
        // A queue that is accepted joins sample-accurately, which is the
        // difference between a stream and four seams a second.
        int32_t taken = 0;
        if (ch.stream_feed != Channel::FEED_RESUBMIT) {
            taken = host_audio_queue(ch.audio_channel, gm_ptr(w->buffer), got);
            // Only count an answer that was actually asked for. Counting the
            // re-submitting path as a refusal reads as "the host said no" when
            // it means "we never asked", and a counter that says the wrong
            // thing is worse than one that says nothing.
            if (taken > 0)
                ++counters().host_queues_ok;
            else
                ++counters().host_queues_refused;
        }
        if (taken > 0) {
            ch.stream_feed = Channel::FEED_QUEUE;
            ch.stream_submitted = (uint32_t)taken;
            announce_once(&ch, w, w->buffer, got);
        } else {
            if (ch.stream_feed == Channel::FEED_UNKNOWN) {
                ch.stream_feed = Channel::FEED_RESUBMIT;
                log_once("qmixer.noqueue", "qmixer: this host cannot continue a sound, so a "
                                           "streamed wave is re-submitted at every refill; the "
                                           "join is audible");
            }
            submit_stream(&ch, w, got);
        }
    }
}

void pump_completions(X86 *c);

void QSWaveMixPump(X86 *c) {
    QTRACE("qmixer: QSWaveMixPump(%08x, %08x, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    ++counters().pump_calls;
    // The game does not call this. It is implemented because the export
    // exists and an application may drive the mixer by hand, and because a
    // call that did nothing would be a lie about a mixer that needs pumping.
    dsound_pump();
    pump_streams(c);
    pump_completions(c);
    set_eax(c, QS_OK);
}

void QSWaveMixGetDirectSound(X86 *c) {
    QTRACE("qmixer: QSWaveMixGetDirectSound(%08x, %08x, %08x, %08x, %08x, %08x)", arg(c, 0),
           arg(c, 1), arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    uint32_t out = arg(c, 1);
    if (out && gm_valid(out, 4))
        wr32(out, 0);
    // This session is not built on DirectSound, so there is no object to hand
    // back. QMixer reports failure in exactly this situation.
    log_once("qmixer.getds", "qmixer: GetDirectSound has no DirectSound object to return; "
                             "this session mixes through the host directly");
    set_eax(c, QS_ERROR);
}

// ---------------------------------------------------------------------------
// Waves
// ---------------------------------------------------------------------------
void QSWaveMixOpenWaveEx(X86 *c) {
    QTRACE("qmixer: QSWaveMixOpenWaveEx(%08x, %08x, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    uint32_t data = arg(c, 1);
    uint32_t flags = arg(c, 2);
    if (!s) {
        ++counters().open_wave_no_session;
        set_eax(c, 0);
        return;
    }
    // The error value is read back through the session handle, so a refusal
    // below sets it even though the guest handles the zero return.

    Wave w;
    w.alive = true;
    uint32_t error = QS_OK;
    if (!read_wave_record(data, flags, &w, &error)) {
        s->last_error = error, g_qs_last_error = error;
        set_eax(c, 0); // the documented failure; the game handles it
        return;
    }
    if (w.streamed)
        ++counters().open_wave_streamed;
    else
        ++counters().open_wave_static;
    // The handle record. Only +4 (nSamplesPerSec) is evidenced as read by the
    // guest; the rest of the QMixer wave header is unknown and left zero.
    // SHIM(temporary): partial QMIXWAVE layout, see docs/shims.md.
    uint32_t h = heap_alloc(16, true, 16);
    if (!h) {
        if (w.buffer) {
            heap_free(w.buffer);
            w.buffer = 0;
        }
        s->last_error = QSERR_BAD_DATA, g_qs_last_error = QSERR_BAD_DATA;
        set_eax(c, 0);
        return;
    }
    // The handle is the wave's leading WAVEFORMATEX (tag, channels, rate, avg
    // bytes/s, block align, bits). The engine reads it directly: HelpDude's
    // PlaySample (runblack 0x5bb530) divides a clip's byte count by
    // 2 * [handle + 2] (nChannels) to get its length, and a zero there is a
    // divide error that left every spoken sentence with the wrong duration.
    // SHIM(temporary): the fields past +14 of the real header are unknown.
    wr16(h + 0, 1); // WAVE_FORMAT_PCM: compressed waves are refused before here
    wr16(h + 2, (uint16_t)w.channels);
    wr32(h + 4, w.rate);
    const uint32_t block_align = w.channels * (w.bits / 8 ? w.bits / 8 : 1);
    wr32(h + 8, w.rate * block_align);
    wr16(h + 12, (uint16_t)block_align);
    wr16(h + 14, (uint16_t)w.bits);
    waves().push_back(w);
    waves().back().handle = h;
    wave_index()[h] = waves().size() - 1;
    LOGV("qmixer: opened wave %08x: %u bytes, %u Hz, %u ch, %u bit", h, w.bytes, w.rate, w.channels,
         w.bits);
    set_eax(c, h);
}

void QSWaveMixFreeWave(X86 *c) {
    QTRACE("qmixer: QSWaveMixFreeWave(%08x, %08x, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    ++counters().free_wave;
    Session *s = session_for(arg(c, 0));
    Wave *w = wave_for(arg(c, 1));
    if (!s || !w) {
        set_eax(c, QS_ERROR);
        return;
    }
    // Invalidate the wave before any completion callback runs. The music-chunk
    // callback 0x1020dc80 calls QSWaveMixFreeWave on its own wave; the real
    // mixer tolerates that because it unlinks the node from the channel before
    // the callback runs. Here the erase makes a recursive free a clean failure.
    const uint32_t handle = w->handle;
    w->alive = false;
    wave_index().erase(handle);
    // A channel still playing this wave has to stop: the sample memory is the
    // guest's and it may reuse it the moment this returns.
    for (size_t i = 0; i < channels().size(); ++i) {
        Channel &ch = channels()[i];
        if (ch.playing && ch.wave == handle)
            stop_channel(c, &ch, (uint32_t)i);
        // And any channel still naming it as its stream, playing or not: the
        // buffer is about to go and the pump would read it next frame.
        if (ch.stream_wave == handle) {
            ch.stream_wave = 0;
            ch.stream_submitted = 0;
        }
    }
    // A streamed wave owns a buffer this shim allocated for it, and nothing
    // else will ever free it. The game opens a wave per sound and frees the
    // previous one before each new play - 388 of them in one scripted level -
    // so leaking a buffer here leaks the guest's heap at the rate the game
    // makes sounds, and a long session runs it out. What that looks like is
    // not a crash: read_wave_record fails to allocate, the wave is refused,
    // and the sounds stop.
    if (w->buffer) {
        heap_free(w->buffer);
        w->buffer = 0;
        w->buffer_bytes = 0;
        w->pcm = 0;
        w->bytes = 0;
    }
    heap_free(handle);
    set_eax(c, QS_OK);
}

// ---------------------------------------------------------------------------
// Playback
// ---------------------------------------------------------------------------
// The play-parameters block (PlayEx's last argument). Evidence: LHaudiodllR
// 0x1020f314..0x1020f31b builds one with 0x28 at +0, the completion callback at
// +0xc and a context pointer at +0x10; the callback 0x1020dc80 is stdcall
// (channel, wave handle, context), frees the wave with FreeWave and marks the
// channel's state, and the game's audio thread refills from there. Without a
// completion callback the game queues its four pre-roll chunks and then never
// feeds the channel again (the music stopped after about 4 seconds).
bool read_play_callback(uint32_t params, uint32_t *cb, uint32_t *ctx) {
    *cb = *ctx = 0;
    if (!params || !gm_valid(params, 0x14))
        return false;
    if (rd32(params) < 0x14)
        return false;
    *cb = rd32(params + 0xc);
    *ctx = rd32(params + 0x10);
    return *cb != 0;
}

// Calls the guest's completion callback for one finished wave.
void complete_wave(X86 *c, uint32_t channel, const Channel::Pending &p, const char *why) {
    if (p.callback)
        guest_call(c, p.callback, channel, p.wave, p.context);
}

// Fires the callbacks of every wave the host voice has finished. Called from the
// frame pump, which is guest-callable context.
void pump_completions(X86 *c) {
    for (size_t i = 0; i < channels().size(); ++i) {
        // The callback is guest code and may start, stop or free anything, so the
        // channel is re-read each time round rather than held.
        for (;;) {
            Channel &ch = channels()[i];
            if (ch.pending.empty() || ch.audio_channel < 0)
                break;
            const uint64_t remaining = host_audio_voice_remaining_bytes(ch.audio_channel);
            const uint64_t played = ch.voice_total > remaining ? ch.voice_total - remaining : 0;
            const Channel::Pending front = ch.pending.front();
            if (remaining != 0 && front.end > played)
                break;
            ch.pending.pop_front();
            ++counters().completions;
            char why[96];
            snprintf(why, sizeof why, "played out: remaining %llu of %llu, end %u, played %llu",
                     (unsigned long long)remaining, (unsigned long long)ch.voice_total, front.end,
                     (unsigned long long)played);
            complete_wave(c, (uint32_t)i, front, why);
        }
    }
}

void QSWaveMixPlayEx(X86 *c) {
    QTRACE("qmixer: QSWaveMixPlayEx(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    // (hMix, iChannel, dwFlags, hWave, iLoops, lpPlayParams)
    Session *s = session_for(arg(c, 0));
    uint32_t idx = arg(c, 1);
    uint32_t flags = arg(c, 2);
    Wave *w = wave_for(arg(c, 3));
    uint32_t loops = arg(c, 4);
    ++counters().play_calls;
    if (!s) {
        ++counters().drop_no_session;
        drop_reason("no session", idx, arg(c, 0));
        set_eax(c, QS_ERROR);
        return;
    }
    if (!w) {
        ++counters().drop_no_wave;
        drop_reason("no such wave", idx, arg(c, 3));
        s->last_error = QSERR_PLAY_FAILED, g_qs_last_error = QSERR_PLAY_FAILED;
        set_eax(c, QS_ERROR);
        return;
    }
    Channel *ch = channel_for(idx, true);
    if (!ch) {
        ++counters().drop_no_channel;
        drop_reason("channel out of range", idx, MAX_CHANNELS);
        s->last_error = QSERR_NO_CHANNEL, g_qs_last_error = QSERR_NO_CHANNEL;
        set_eax(c, QS_ERROR);
        return;
    }
    ch->open = true;
    ch->wave = w->handle;
    // A new play un-pauses the channel: the game pauses a channel and later
    // reuses it with SetVolume, ConfigureChannel, EnableChannel and PlayEx,
    // never RestartChannel, and expects the sound. Pause only holds the sound
    // that was playing when it was called.
    ch->paused = false;
    if (!ch->enabled || !s->active) {
        // The call still succeeds; the sound is simply not audible, which is
        // what a disabled channel means.
        if (!ch->enabled) {
            ++counters().drop_disabled;
            drop_reason("channel disabled", idx, 0);
        } else {
            ++counters().drop_inactive;
            drop_reason("session not active", idx, 0);
        }
        set_eax(c, QS_OK);
        return;
    }
    if (ch->audio_channel < 0)
        ch->audio_channel = dx_alloc_audio_channel();
    if (ch->audio_channel < 0) {
        ++counters().drop_no_voice;
        drop_reason("no host voice left", idx, 0);
        set_eax(c, QS_ERROR);
        return;
    }

    if (w->streamed) {
        // Pull the first buffer now so the sound starts on this call, as it
        // would with a wave whose samples were already in memory.
        bool ended = false;
        uint32_t got = pull_stream(c, w, &ended);
        ch->stream_wave = w->handle;
        ch->stream_ended = ended;
        ch->stream_loop = loops != 0;
        // A new sound gets a fresh answer about how the host takes refills;
        // the last one's may have been decided against a host that has since
        // gained the ability to continue a sound.
        ch->stream_feed = Channel::FEED_UNKNOWN;
        if (!got) {
            ++counters().drop_stream_dry;
            drop_reason("streamed wave gave nothing on the first pull", idx, w->handle);
            ch->playing = false;
            ch->stream_wave = 0;
            set_eax(c, QS_OK);
            return;
        }
        submit_stream(ch, w, got);
        ++counters().play_delivered;
        ch->playing = true;
        // Say at once that this channel is a stream. Until it is one the host
        // counts the buffer that started it as the whole sound and retires the
        // voice when it runs out, and then every append is refused; and the
        // conversion is what makes host_audio_queue accept at all.
        ch->stream_feed = host_audio_stream(ch->audio_channel) >= 0 ? Channel::FEED_QUEUE
                                                                    : Channel::FEED_RESUBMIT;
        // And fill the queue behind it now rather than at the next frame.
        // Nothing has been appended yet, so the sound is riding entirely on
        // the lap the conversion re-issued; waiting for a pump to notice
        // leaves the first hand-over as the only one with no slack in front
        // of it, which is the one place a stream can be late by construction.
        if (ch->stream_feed == Channel::FEED_QUEUE && !ch->stream_ended) {
            bool more = false;
            uint32_t next = pull_stream(c, w, &more);
            if (next) {
                ch->stream_ended = more;
                if (host_audio_queue(ch->audio_channel, gm_ptr(w->buffer), next) > 0)
                    ++counters().host_queues_ok;
                else
                    ++counters().host_queues_refused;
            }
        }
        LOGV("qmixer: streaming wave %08x on channel %u: %u bytes, %u Hz, "
             "%u ch, %u bit",
             w->handle, idx, got, w->rate, w->channels, w->bits);
        set_eax(c, QS_OK);
        return;
    }

    if (!w->bytes || !w->pcm) {
        ++counters().drop_empty_wave;
        drop_reason("wave has no samples", idx, w->handle);
        set_eax(c, QS_OK);
        return;
    }

    uint32_t cb = 0, ctx = 0;
    const bool cb_ok = read_play_callback(arg(c, 5), &cb, &ctx);
    // Queueing rule. QMixer.dll alone does not settle it: the exported PlayEx
    // stores dwFlags verbatim into the channel at +0x3c (0x1800bff0, from
    // PlayEx arg2), and the insert routine then tests bit 0x8 of that word
    // (18006027: mov edx,[ebx+0x3c] / shr edx,3 / testb $0x1,dl). But no code
    // anywhere tests the 0x20 or 0x400 bits, and bit 0x8 is never passed by
    // this game. The two call sites pin the intended behavior instead:
    //
    //   ordinary samples: 0x421 (LHaudiodllR 0x1021293b, 0x1020527b)
    //   music chunks:     0x400 (LHaudiodllR 0x1020f4ca)
    //
    // Speech/SFX must replace the voice. Appending a speech wave computes its
    // completion end against the earlier sound's total, so its callback runs
    // when that sound ends, clears the sample's playing flag at +0x8c and lets
    // the subtitle advance. Music chunks must append back-to-back or only the
    // last one is heard. The difference is bit 0x1 (set in 0x421, clear in
    // 0x400), so the fit is:
    //
    //   queue = (flags & 0x400) && !(flags & 0x1)
    //
    // DIVERGENCE(original): this rule is a fit of the two call sites, not the
    // library's own bit 0x8 test; documented pending a cleaner read of QMixer.
    // Only same-format waves are appended; a different format replaces, loudly.
    if ((flags & 0x400u) && !(flags & 0x1u) && ch->playing &&
        host_audio_voice_remaining_bytes(ch->audio_channel)) {
        const uint32_t rate = ch->frequency ? ch->frequency : w->rate;
        if (rate == ch->voice_rate && w->channels == ch->voice_channels &&
            w->bits == ch->voice_bits &&
            host_audio_queue(ch->audio_channel, gm_ptr(w->pcm), w->bytes) > 0) {
            ch->wave = w->handle;
            ch->voice_total += w->bytes;
            if (cb_ok)
                ch->pending.push_back({w->handle, (uint32_t)ch->voice_total, cb, ctx});
            ++counters().play_delivered;
            ++counters().play_queued_static;
            log_once("qmixer.queue",
                     "qmixer: PlayEx flags 0x400 without 0x1 are treated as QUEUEWAVE: the "
                     "wave is appended to the channel's playing sound (fit of the two "
                     "call sites, see docs/shims.md)");
            set_eax(c, QS_OK);
            return;
        }
        log_once("qmixer.queue.fmt",
                 "qmixer: SHIM(temporary): a queued wave (%u Hz %u ch %u bit) does not match the "
                 "playing voice (%u Hz %u ch %u bit) and replaces it; queuing across formats "
                 "needs resampling",
                 rate, w->channels, w->bits, ch->voice_rate, ch->voice_channels, ch->voice_bits);
    }

    HostAudioPlay p;
    memset(&p, 0, sizeof p);
    p.channel = ch->audio_channel;
    p.pcm = gm_ptr(w->pcm);
    p.bytes = w->bytes;
    p.sample_rate = (int32_t)(ch->frequency ? ch->frequency : w->rate);
    p.channels = (int32_t)w->channels;
    p.bits = (int32_t)w->bits;
    p.loop = loops ? 1 : 0;
    p.volume = qmix_volume_to_millibels(ch->volume);
    p.pan = ch->pan;
    announce_once(ch, w, w->pcm, w->bytes);
    host_audio_play(&p);
    ch->voice_rate = (uint32_t)p.sample_rate;
    ch->voice_channels = w->channels;
    ch->voice_bits = w->bits;
    // A new voice replaces what was playing. The real mixer finishes the
    // replaced wave (its pending node is unlinked and its callback invoked;
    // QMixer 0x180060f0 -> fcn.18007580), so the replaced callbacks are run
    // after the new play is in place rather than dropped.
    std::deque<Channel::Pending> replaced;
    replaced.swap(ch->pending);
    ch->voice_total = w->bytes;
    if (cb_ok)
        ch->pending.push_back({w->handle, (uint32_t)w->bytes, cb, ctx});
    ++counters().host_plays;
    ++counters().play_delivered;
    ch->playing = true;
    deliver_pending(c, idx, replaced, "replaced");
    set_eax(c, QS_OK);
}

void QSWaveMixStopChannel(X86 *c) {
    QTRACE("qmixer: QSWaveMixStopChannel(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    ++counters().stop_calls;
    Session *s = session_for(arg(c, 0));
    Channel *ch = channel_for(arg(c, 1));
    if (!s || !ch) {
        set_eax(c, QS_ERROR);
        return;
    }
    stop_channel(c, ch, arg(c, 1));
    set_eax(c, QS_OK);
}

void QSWaveMixPauseChannel(X86 *c) {
    QTRACE("qmixer: QSWaveMixPauseChannel(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    ++counters().pause_calls;
    Session *s = session_for(arg(c, 0));
    Channel *ch = channel_for(arg(c, 1));
    if (!s || !ch) {
        set_eax(c, QS_ERROR);
        return;
    }
    ch->paused = true;
    // There is no host pause, so a paused channel stops. Restart resumes from
    // the beginning, which is audible but not silent.
    if (ch->playing) {
        host_audio_stop(ch->audio_channel);
        log_once("qmixer.pause", "qmixer: PauseChannel stops the sound rather than pausing it; "
                                 "RestartChannel replays it from the start");
    }
    set_eax(c, QS_OK);
}

void QSWaveMixRestartChannel(X86 *c) {
    QTRACE("qmixer: QSWaveMixRestartChannel(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0),
           arg(c, 1), arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    uint32_t idx = arg(c, 1);
    Channel *ch = channel_for(idx);
    if (!s || !ch) {
        set_eax(c, QS_ERROR);
        return;
    }
    ch->paused = false;
    Wave *w = ch->wave ? wave_for(ch->wave) : nullptr;
    if (w && w->pcm && w->bytes && ch->playing) {
        if (ch->audio_channel < 0)
            ch->audio_channel = dx_alloc_audio_channel();
        if (ch->audio_channel >= 0) {
            HostAudioPlay p;
            memset(&p, 0, sizeof p);
            p.channel = ch->audio_channel;
            p.pcm = gm_ptr(w->pcm);
            p.bytes = w->bytes;
            p.sample_rate = (int32_t)(ch->frequency ? ch->frequency : w->rate);
            p.channels = (int32_t)w->channels;
            p.bits = (int32_t)w->bits;
            p.volume = qmix_volume_to_millibels(ch->volume);
            p.pan = ch->pan;
            host_audio_play(&p);
        }
    }
    set_eax(c, QS_OK);
}

void QSWaveMixEnableChannel(X86 *c) {
    QTRACE("qmixer: QSWaveMixEnableChannel(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    uint32_t idx = arg(c, 1);
    Channel *ch = channel_for(idx, true);
    if (!s || !ch) {
        set_eax(c, QS_ERROR);
        return;
    }
    // (hMix, iChannel, dwFlags, fEnable)
    // (hMix, iChannel, dwFlags, x). The shape is confirmed - QMixer.dll's
    // QSWaveMixEnableChannel and QSWaveMixSetVolume are byte for byte the same
    // prologue, both wrapping the fourth argument in a local and handing
    // (hMix, arg2, arg3, &that) to a shared routine - but what the fourth
    // argument means is not. The game calls this with 0 and then plays on the
    // same channel and expects to hear it, which is the only direct evidence
    // there is, and it says the call cannot mean "silence this channel".
    //
    // So the request is recorded and playback is not gated on it. Refusing to
    // guess costs a mute the game may never ask for; guessing wrong cost every
    // sound on the channel, which is what it did.
    uint32_t want = arg(c, 3);
    if (want)
        ++counters().enable_on;
    else
        ++counters().enable_off;
    ch->enable_request = want;
    if (!want) {
        log_once("qmixer.enable",
                 "qmixer: EnableChannel(channel %u, flags %08x, %08x) - the "
                 "game plays on this channel immediately afterwards, so this "
                 "is not read as a mute and the channel stays audible",
                 idx, arg(c, 2), want);
    }
    set_eax(c, QS_OK);
}

// QSWaveMixGetChannelParams(hMixer, channel, params*). Evidence is the guest's
// own use (LHaudiodllR 0x10215820, 0x1020df60, 0x1020dd50, 0x10211420): it sets
// params[0] = 0xdc (a size, 55 dwords) before the call and then reads only the
// dword at +4, testing the same 0x20000/0x10000/0x1000000/0x2000000 bits that it
// passes to QSWaveMixConfigureChannel. So +4 returns the flags last configured on
// the channel. The rest of the 0xdc-byte layout is NOT established: it is zeroed,
// not guessed, and nothing in the guest reads it.
// SHIM(temporary): only the flags at +4 are real; the remaining 0xd8 bytes of
// the documented 0xdc-byte structure are zero, not a reconstructed channel
// description. A real implementation needs the QMixer channel-parameter layout.
void QSWaveMixGetChannelParams(X86 *c) {
    QTRACE("qmixer: QSWaveMixGetChannelParams(%08x, %u, %08x)", arg(c, 0), arg(c, 1), arg(c, 2));
    Session *s = session_for(arg(c, 0));
    Channel *ch = channel_for(arg(c, 1), false);
    uint32_t out = arg(c, 2);
    if (!s || !out) {
        set_eax(c, QS_ERROR);
        return;
    }
    uint32_t size = rd32(out);
    if (size < 8 || size > 0xdc)
        size = 0xdc; // the guest always passes 0xdc; do not write past it
    for (uint32_t off = 4; off + 4 <= size; off += 4)
        wr32(out + off, 0);
    wr32(out + 4, ch ? ch->config : 0);
    log_once("qmixer.getparams",
             "SHIM(temporary): GetChannelParams reports only the configured flags at +4; the "
             "rest of the 0xdc-byte structure is zero (layout unverified)");
    set_eax(c, QS_OK);
}

void QSWaveMixConfigureChannel(X86 *c) {
    QTRACE("qmixer: QSWaveMixConfigureChannel(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0),
           arg(c, 1), arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    ++counters().configure;
    Session *s = session_for(arg(c, 0));
    uint32_t idx = arg(c, 1);
    Channel *ch = channel_for(idx, true);
    if (!s || !ch) {
        set_eax(c, QS_ERROR);
        return;
    }
    ch->open = true;
    ch->config = arg(c, 2);
    set_eax(c, QS_OK);
}

// ---------------------------------------------------------------------------
// Per-channel parameters
// ---------------------------------------------------------------------------
void QSWaveMixSetVolume(X86 *c) {
    QTRACE("qmixer: QSWaveMixSetVolume(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    Channel *ch = channel_for(arg(c, 1), true);
    if (!s || !ch) {
        set_eax(c, QS_ERROR);
        return;
    }
    // (hMix, iChannel, dwFlags, lVolume) in hundredths of a dB.
    ch->volume = (int32_t)arg(c, 3);
    ++counters().set_volume;
    if (ch->volume > counters().loudest_volume)
        counters().loudest_volume = ch->volume;
    if (ch->volume < counters().quietest_volume)
        counters().quietest_volume = ch->volume;
    if (ch->volume <= 0)
        ++counters().volume_silent;
    else if (ch->volume < 3277)
        ++counters().volume_quiet; // under a tenth
    if (ch->audio_channel >= 0)
        host_audio_set_volume(ch->audio_channel, qmix_volume_to_millibels(ch->volume));
    set_eax(c, QS_OK);
}

void QSWaveMixSetFrequency(X86 *c) {
    QTRACE("qmixer: QSWaveMixSetFrequency(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    ++counters().set_frequency;
    Session *s = session_for(arg(c, 0));
    Channel *ch = channel_for(arg(c, 1), true);
    if (!s || !ch) {
        set_eax(c, QS_ERROR);
        return;
    }
    ch->frequency = arg(c, 3);
    if (ch->audio_channel >= 0 && ch->frequency)
        host_audio_set_frequency(ch->audio_channel, ch->frequency);
    set_eax(c, QS_OK);
}

void QSWaveMixSetPosition(X86 *c) {
    QTRACE("qmixer: QSWaveMixSetPosition(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    ++counters().set_position;
    Session *s = session_for(arg(c, 0));
    Channel *ch = channel_for(arg(c, 1), true);
    if (!s || !ch) {
        set_eax(c, QS_ERROR);
        return;
    }
    // (hMix, iChannel, dwFlags, lpPosition): the game passes a vector pointer.
    read_vec3(arg(c, 3), ch->position);
    set_eax(c, QS_OK);
}

// QSWaveMixSetPolarPosition(hMix, iChannel, dwFlags, lpPolar). Guest call site
// LHaudiodllR 0x1020f3f0: pushes (hMix, iChannel, 0x20, &zeroedVec). The
// channel loop treats it as the polar-coordinate counterpart of SetPosition,
// so the vector is stored on the channel the same way. SHIM(temporary): the
// direction is stored, not spatialized; that matches SetPosition and is not a
// claim about the 0x20 flag word.
void QSWaveMixSetPolarPosition(X86 *c) {
    QTRACE("qmixer: QSWaveMixSetPolarPosition(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0),
           arg(c, 1), arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    ++counters().set_position;
    Session *s = session_for(arg(c, 0));
    Channel *ch = channel_for(arg(c, 1), true);
    if (!s || !ch) {
        set_eax(c, QS_ERROR);
        return;
    }
    read_vec3(arg(c, 3), ch->position);
    log_once("qmixer.polar",
             "SHIM(temporary): QSWaveMixSetPolarPosition stores the channel vector; it is not "
             "spatialized and the 0x20 flag word is not interpreted");
    set_eax(c, QS_OK);
}

// QSWaveMixIsChannelDone(hMix, iChannel). Guest call sites LHaudiodllR
// 0x1020ec5a and 0x1020f6c5 push (hMix, iChannel) and branch on the answer to
// decide whether to flush. Nonzero means there is nothing left to play.
void QSWaveMixIsChannelDone(X86 *c) {
    QTRACE("qmixer: QSWaveMixIsChannelDone(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    Channel *ch = channel_for(arg(c, 1), false);
    // Done when nothing was ever played, or a static sound has drained from the
    // host voice and every wave's completion callback has been delivered. A
    // streamed channel is done when it is not playing.
    bool done = !s || !ch || !ch->playing;
    if (!done && !ch->stream_wave && ch->audio_channel >= 0 && ch->pending.empty() &&
        host_audio_voice_remaining_bytes(ch->audio_channel) == 0)
        done = true;
    log_once("qmixer.isdone",
             "SHIM(temporary): QSWaveMixIsChannelDone is true when the static voice has "
             "drained and its completion callbacks have run; streamed channels answer from "
             "the front-end 'playing' flag");
    set_eax(c, done ? 1u : 0u);
}

// QSWaveMixFlushChannel(hMix, iChannel, dwFlags). Guest call sites
// LHaudiodllR 0x1020ec77 and 0x1020f6e2 push (hMix, iChannel, 0), after
// IsChannelDone, to release a finished sound's channel. The real mixer resets
// the channel; this stops the host voice and clears the stream state, which is
// the same reset stop_channel performs.
void QSWaveMixFlushChannel(X86 *c) {
    QTRACE("qmixer: QSWaveMixFlushChannel(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    Channel *ch = channel_for(arg(c, 1), true);
    if (!s || !ch) {
        set_eax(c, QS_ERROR);
        return;
    }
    stop_channel(c, ch, arg(c, 1));
    log_once("qmixer.flush",
             "SHIM(temporary): QSWaveMixFlushChannel is modelled as stop_channel; whether the "
             "real mixer also frees the channel's wave association is unverified");
    set_eax(c, QS_OK);
}

// QSWaveMixGetPlayPosition(hMix, iChannel, lpPlayPos, lpWritePos, dwFlags).
//
// Guest call sites LHaudiodllR 0x1020f385 and 0x10214c5a push five words
// (hMix, iChannel, two guest output pointers, a unit selector). QMixer.dll
// 0x18002ca0 -> 0x18008970 -> 0x1800c590 settles the outputs:
//
//   *lpPlayPos = obj[+0x40]                                (sample frames)
//   *lpWritePos = flags & 3 == 0 ? frames * nBlockAlign    (bytes)
//                 flags & 3 == 1 ? frames                  (sample frames)
//                 flags & 3 == 2 ? frames*1000/nSamplesPerSec, rounded (ms)
//                 flags & 3 == 3 ? 0
//
// The ms form is the signed 64-bit helper at 0x1802cb20, called from
// 0x18024450 as (frames, 1000, nSamplesPerSec): (frames*1000 + rate/2) / rate.
// When the channel has no voice object (obj[+0xe4] == 0, i.e. after a
// Stop/Flush) 0x1800c590 still writes the retained frame cursor to *lpPlayPos
// but zeroes *lpWritePos regardless of the selector.
//
// This is the fix for the advisor speech being cut off about half a second
// after it starts. HelpDude::ApplyLipSync (runblack 0x5bcd00) calls
// LHSampleGetPlayPosition (LHaudiodllR 0x10214c00) and, when that returns
// negative, calls StopSentence once the sentence has been playing for 0.5 s.
// That wrapper calls this function with flags 2 and then tests the QMixer
// *lpPlayPos dword (0x10214c56..0x10214c70) and returns -1 when it is zero;
// only a nonzero play position returns the millisecond value. The old shim
// zeroed both outputs, so every query read as "not started yet" and the
// sentence was abandoned half a second in. Reporting the real host cursor
// makes the frame position nonzero and lets the lip-sync run.
void QSWaveMixGetPlayPosition(X86 *c) {
    QTRACE("qmixer: QSWaveMixGetPlayPosition(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0),
           arg(c, 1), arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    Channel *ch = channel_for(arg(c, 1), true);
    if (!s || !ch) {
        set_eax(c, QS_ERROR);
        return;
    }
    // host_audio_position is the voice cursor in bytes of the voice format. It
    // is cumulative across a queued chunk, retained across a stop (which is
    // what a paused or flushed channel reads) and saturates at the sound's
    // length when a one-shot finishes.
    uint32_t pos_bytes = 0;
    if (ch->audio_channel >= 0)
        pos_bytes = host_audio_position(ch->audio_channel);
    uint32_t frame_bytes = ch->voice_channels * (ch->voice_bits / 8);
    if (!frame_bytes)
        frame_bytes = 1;
    const uint32_t rate = ch->voice_rate ? ch->voice_rate : 1;
    const uint32_t frames = pos_bytes / frame_bytes;
    // Only a live voice has a write cursor; a stopped or flushed one reports 0
    // exactly as the real mixer does once the voice object is gone.
    uint32_t write = 0;
    if (ch->playing) {
        switch (arg(c, 4) & 3u) {
        case 0:
            write = frames * frame_bytes;
            break;
        case 1:
            write = frames;
            break;
        case 2:
            write = (uint32_t)(((uint64_t)frames * 1000u + rate / 2u) / rate);
            break;
        case 3:
        default:
            write = 0;
            break;
        }
    }
    const uint32_t play_addr = arg(c, 2), write_addr = arg(c, 3);
    if (play_addr && gm_valid(play_addr, 4))
        wr32(play_addr, frames);
    if (write_addr && gm_valid(write_addr, 4))
        wr32(write_addr, write);
    set_eax(c, QS_OK);
}

void QSWaveMixSetSourcePosition(X86 *c) {
    QTRACE("qmixer: QSWaveMixSetSourcePosition(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0),
           arg(c, 1), arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    Channel *ch = channel_for(arg(c, 1), true);
    if (!s || !ch) {
        set_eax(c, QS_ERROR);
        return;
    }
    read_vec3(arg(c, 3), ch->position);
    set_eax(c, QS_OK);
}

void QSWaveMixSetSourceVelocity(X86 *c) {
    QTRACE("qmixer: QSWaveMixSetSourceVelocity(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0),
           arg(c, 1), arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    Channel *ch = channel_for(arg(c, 1), true);
    if (!s || !ch) {
        set_eax(c, QS_ERROR);
        return;
    }
    read_vec3(arg(c, 3), ch->velocity);
    set_eax(c, QS_OK);
}

// Stored, not applied: this task records positional parameters and Task 7
// decides whether to use them.
void QSWaveMixSetSourceCone(X86 *c) {
    QTRACE("qmixer: QSWaveMixSetSourceCone(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    ++counters().set_cone;
    Session *s = session_for(arg(c, 0));
    if (!s) {
        set_eax(c, QS_ERROR);
        return;
    }
    set_eax(c, QS_OK);
}

void QSWaveMixSetDistanceMapping(X86 *c) {
    QTRACE("qmixer: QSWaveMixSetDistanceMapping(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0),
           arg(c, 1), arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    ++counters().set_distance;
    Session *s = session_for(arg(c, 0));
    if (!s) {
        set_eax(c, QS_ERROR);
        return;
    }
    set_eax(c, QS_OK);
}

void QSWaveMixSetPanRate(X86 *c) {
    QTRACE("qmixer: QSWaveMixSetPanRate(%08x, %u, %08x, %08x, %08x, %08x)", arg(c, 0), arg(c, 1),
           arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    if (!s) {
        set_eax(c, QS_ERROR);
        return;
    }
    set_eax(c, QS_OK);
}

// ---------------------------------------------------------------------------
// Listener
// ---------------------------------------------------------------------------
void QSWaveMixSetListenerPosition(X86 *c) {
    QTRACE("qmixer: QSWaveMixSetListenerPosition(%08x, %08x, %08x, %08x, %08x, %08x)", arg(c, 0),
           arg(c, 1), arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    if (!s) {
        set_eax(c, QS_ERROR);
        return;
    }
    read_vec3(arg(c, 1), g_listener_pos);
    set_eax(c, QS_OK);
}

void QSWaveMixSetListenerOrientation(X86 *c) {
    QTRACE("qmixer: QSWaveMixSetListenerOrientation(%08x, %08x, %08x, %08x, %08x, %08x)", arg(c, 0),
           arg(c, 1), arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    if (!s) {
        set_eax(c, QS_ERROR);
        return;
    }
    read_vec3(arg(c, 1), g_listener_front);
    read_vec3(arg(c, 2), g_listener_top);
    set_eax(c, QS_OK);
}

void QSWaveMixSetListenerVelocity(X86 *c) {
    QTRACE("qmixer: QSWaveMixSetListenerVelocity(%08x, %08x, %08x, %08x, %08x, %08x)", arg(c, 0),
           arg(c, 1), arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    if (!s) {
        set_eax(c, QS_ERROR);
        return;
    }
    read_vec3(arg(c, 1), g_listener_vel);
    set_eax(c, QS_OK);
}

void QSWaveMixSetSpeedOfSound(X86 *c) {
    QTRACE("qmixer: QSWaveMixSetSpeedOfSound(%08x, %08x, %08x, %08x, %08x, %08x)", arg(c, 0),
           arg(c, 1), arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    if (!s) {
        set_eax(c, QS_ERROR);
        return;
    }
    uint32_t v = arg(c, 1);
    memcpy(&g_speed_of_sound, &v, 4);
    set_eax(c, QS_OK);
}

void QSWaveMixSetSpeakerPlacement(X86 *c) {
    QTRACE("qmixer: QSWaveMixSetSpeakerPlacement(%08x, %08x, %08x, %08x, %08x, %08x)", arg(c, 0),
           arg(c, 1), arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    Session *s = session_for(arg(c, 0));
    if (!s) {
        set_eax(c, QS_ERROR);
        return;
    }
    g_speaker_placement = arg(c, 1);
    set_eax(c, QS_OK);
}

// QSWaveMixGetLastError(): takes NO arguments. Evidence: LHaudiodllR 0x10202f50
// does `PUSH ESI; CALL [GetLastError]; MOV ESI,EAX; ... POP ESI`. That push is
// the function saving ESI, not an argument; registering one argument made the
// shim pop the saved ESI and the whole frame returned one word off (found with
// RECOMP_WATCH_FRAME: ESI came back as the return address).
// SHIM(temporary): the code values are invented (see QSWaveMixGetErrorText).
void QSWaveMixGetLastError(X86 *c) {
    QTRACE("qmixer: QSWaveMixGetLastError(%s)", "");
    set_eax(c, g_qs_last_error);
}

// QSWaveMixGetErrorText(code, lpBuffer, cchBuffer). Evidence: LHaudiodllR
// 0x10202f83 (`PUSH 0x3f6; PUSH buf(0x10242b78); PUSH code; CALL`) and
// 0x10202fef (same shape, arg0 moved from the stack), so three args with the
// code first and the size last. Writes a NUL-terminated message into the guest
// buffer and returns the characters written. The code values are the invented
// QSERR_* ones; the text is ours, not the real library's wording.
void QSWaveMixGetErrorText(X86 *c) {
    QTRACE("qmixer: QSWaveMixGetErrorText(%08x, %08x, %08x, %08x, %08x, %08x)", arg(c, 0),
           arg(c, 1), arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5));
    uint32_t code = arg(c, 0), buf = arg(c, 1), size = arg(c, 2);
    const char *text;
    switch (code) {
    case QS_OK:
        text = "No error";
        break;
    case QSERR_NO_SESSION:
        text = "No mixer session";
        break;
    case QSERR_BAD_WAVE_RECORD:
        text = "Invalid wave record";
        break;
    case QSERR_BAD_FORMAT:
        text = "Unsupported wave format";
        break;
    case QSERR_BAD_DATA:
        text = "Wave data is not readable";
        break;
    case QSERR_NO_CHANNEL:
        text = "No such channel";
        break;
    case QSERR_PLAY_FAILED:
        text = "The wave could not be played";
        break;
    default:
        text = "Unknown QMixer error";
        break;
    }
    if (!buf || !size || !gm_valid(buf, size)) {
        set_eax(c, 0);
        return;
    }
    uint32_t n = 0;
    while (n + 1 < size && text[n]) {
        wr8(buf + n, (uint8_t)text[n]);
        ++n;
    }
    wr8(buf + n, 0);
    log_once("qmixer.errortext",
             "SHIM(temporary): QMixer error codes/text are invented (QM prefix); only the "
             "call shape is evidenced");
    set_eax(c, n);
}

const ImportShim g_qmixer_shims[] = {
    {"QMIXER.dll", "QSWaveMixGetLastError", 0, QSWaveMixGetLastError},
    {"QMIXER.dll", "QSWaveMixGetErrorText", 3, QSWaveMixGetErrorText},
    {"QMIXER.dll", "QSWaveMixSetSpeakerPlacement", 2, QSWaveMixSetSpeakerPlacement},
    {"QMIXER.dll", "QSWaveMixSetSpeedOfSound", 3, QSWaveMixSetSpeedOfSound},
    {"QMIXER.dll", "QSWaveMixSetPanRate", 4, QSWaveMixSetPanRate},
    {"QMIXER.dll", "QSWaveMixSetListenerOrientation", 4, QSWaveMixSetListenerOrientation},
    {"QMIXER.dll", "QSWaveMixSetListenerPosition", 3, QSWaveMixSetListenerPosition},
    {"QMIXER.dll", "QSWaveMixSetVolume", 4, QSWaveMixSetVolume},
    {"QMIXER.dll", "QSWaveMixSetDistanceMapping", 4, QSWaveMixSetDistanceMapping},
    {"QMIXER.dll", "QSWaveMixSetSourceCone", 6, QSWaveMixSetSourceCone},
    {"QMIXER.dll", "QSWaveMixSetFrequency", 4, QSWaveMixSetFrequency},
    {"QMIXER.dll", "QSWaveMixSetListenerVelocity", 3, QSWaveMixSetListenerVelocity},
    {"QMIXER.dll", "QSWaveMixSetSourceVelocity", 4, QSWaveMixSetSourceVelocity},
    {"QMIXER.dll", "QSWaveMixSetSourcePosition", 4, QSWaveMixSetSourcePosition},
    {"QMIXER.dll", "QSWaveMixSetPosition", 4, QSWaveMixSetPosition},
    {"QMIXER.dll", "QSWaveMixRestartChannel", 3, QSWaveMixRestartChannel},
    {"QMIXER.dll", "QSWaveMixPauseChannel", 3, QSWaveMixPauseChannel},
    {"QMIXER.dll", "QSWaveMixStopChannel", 3, QSWaveMixStopChannel},
    {"QMIXER.dll", "QSWaveMixConfigureChannel", 5, QSWaveMixConfigureChannel},
    {"QMIXER.dll", "QSWaveMixGetChannelParams", 3, QSWaveMixGetChannelParams},
    {"QMIXER.dll", "QSWaveMixSetPolarPosition", 4, QSWaveMixSetPolarPosition},
    {"QMIXER.dll", "QSWaveMixIsChannelDone", 2, QSWaveMixIsChannelDone},
    {"QMIXER.dll", "QSWaveMixFlushChannel", 3, QSWaveMixFlushChannel},
    {"QMIXER.dll", "QSWaveMixGetPlayPosition", 5, QSWaveMixGetPlayPosition},
    {"QMIXER.dll", "QSWaveMixEnableChannel", 4, QSWaveMixEnableChannel},
    {"QMIXER.dll", "QSWaveMixOpenWaveEx", 3, QSWaveMixOpenWaveEx},
    {"QMIXER.dll", "QSWaveMixFreeWave", 2, QSWaveMixFreeWave},
    {"QMIXER.dll", "QSWaveMixPlayEx", 6, QSWaveMixPlayEx},
    {"QMIXER.dll", "QSWaveMixCloseSession", 1, QSWaveMixCloseSession},
    {"QMIXER.dll", "QSWaveMixGetDirectSound", 2, QSWaveMixGetDirectSound},
    {"QMIXER.dll", "QSWaveMixInitEx", 1, QSWaveMixInitEx},
    {"QMIXER.dll", "QSWaveMixActivate", 2, QSWaveMixActivate},
    {"QMIXER.dll", "QSWaveMixOpenChannel", 3, QSWaveMixOpenChannel},
    {"QMIXER.dll", "QSWaveMixSetOptions", 3, QSWaveMixSetOptions},
    {"QMIXER.dll", "QSWaveMixPump", 0, QSWaveMixPump},
};

} // namespace

// Only for the test that asserts what the old gate did. The reading is cached
// because getenv on every look of the pump is not free, and the pump looks
// tens of thousands of times a run.
void qmixer_gate_reset_for_test() {
    gate_cache() = -1;
}

// The frame pump. Registered by dx_register_shims and run from the guest's own
// message loop, on the main guest thread, between frames.
void qmixer_frame_pump(X86 *c) {
    // The guest callback this ends in is guest code, and guest code may reach
    // the message loop, which is where this is called from. Once round is what
    // is wanted; a second round inside the first would refill against a queue
    // depth the host has not been told about yet.
    static bool inside = false;
    if (inside)
        return;
    inside = true;
    ++counters().frame_pumps;
    dsound_pump();
    pump_streams(c);
    pump_completions(c);
    inside = false;
}

void qmixer_register() {
    static bool done = false;
    if (done)
        return;
    done = true;
    imports_register(g_qmixer_shims, std::size(g_qmixer_shims));
}

// Print mixer activity and per-channel diagnostics for the current run.
// These counters describe calls and channel state, not proof that audio reached the speaker.
void qmixer_dump(FILE *out) {
    const Counters &n = counters();
    uint32_t refused = n.open_wave_refused_rec + n.open_wave_refused_fmt +
                       n.open_wave_refused_data + n.open_wave_refused_stream +
                       n.open_wave_no_session;
    uint32_t dropped = n.drop_no_session + n.drop_no_wave + n.drop_no_channel + n.drop_disabled +
                       n.drop_paused + n.drop_inactive + n.drop_no_voice + n.drop_empty_wave +
                       n.drop_stream_dry;
    fprintf(out,
            "qmixer: waves opened %u static, %u streamed, %u refused"
            " (record %u, format %u, data %u, streaming %u, no session %u)\n",
            n.open_wave_static, n.open_wave_streamed, refused, n.open_wave_refused_rec,
            n.open_wave_refused_fmt, n.open_wave_refused_data, n.open_wave_refused_stream,
            n.open_wave_no_session);
    fprintf(out,
            "qmixer: plays asked %u, delivered %u, dropped %u"
            " (no session %u, no wave %u, no channel %u, disabled %u,"
            " paused %u, session inactive %u, no host voice %u,"
            " wave empty %u, stream dry %u)\n",
            n.play_calls, n.play_delivered, dropped, n.drop_no_session, n.drop_no_wave,
            n.drop_no_channel, n.drop_disabled, n.drop_paused, n.drop_inactive, n.drop_no_voice,
            n.drop_empty_wave, n.drop_stream_dry);
    fprintf(out,
            "qmixer: host plays %u, queues %u accepted %u refused"
            " (a refusal is the host saying no, not a host that was never asked)\n",
            n.host_plays, n.host_queues_ok, n.host_queues_refused);
    fprintf(out,
            "qmixer: Pump called %u times, frame pump %u times, %u refills, "
            "%u looks that found nothing to do\n",
            n.pump_calls, n.frame_pumps, n.pump_refills, n.pump_skipped);
    fprintf(out,
            "qmixer: OpenChannel calls %u, EnableChannel %u on / %u off,"
            " ConfigureChannel %u; stops %u, pauses %u, waves freed %u\n",
            n.open_channel, n.enable_on, n.enable_off, n.configure, n.stop_calls, n.pause_calls,
            n.free_wave);
    if (n.set_volume) {
        fprintf(out,
                "qmixer: volume set %u times, from %d (%d mB) to %d (%d mB)"
                " on QMixer's 0..32767 scale; frequency %u, position %u,"
                " distance mapping %u, cone %u\n",
                n.set_volume, n.quietest_volume, qmix_volume_to_millibels(n.quietest_volume),
                n.loudest_volume, qmix_volume_to_millibels(n.loudest_volume), n.set_frequency,
                n.set_position, n.set_distance, n.set_cone);
        fprintf(out,
                "qmixer: of those, %u asked for silence and %u for under a"
                " tenth of full scale\n",
                n.volume_silent, n.volume_quiet);
    } else {
        fprintf(out,
                "qmixer: volume never set; frequency %u, position %u,"
                " distance mapping %u, cone %u\n",
                n.set_frequency, n.set_position, n.set_distance, n.set_cone);
    }
}

void qmixer_reset() {
    for (Channel &ch : channels()) {
        if (ch.playing && ch.audio_channel >= 0)
            host_audio_stop(ch.audio_channel);
    }
    sessions().clear();
    waves().clear();
    wave_index().clear();
    channels().clear();
    announced().clear();
}
