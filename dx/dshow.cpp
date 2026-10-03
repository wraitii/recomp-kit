// dshow.cpp - DirectShow multimedia streaming, the reading side.
//
// A game that streams its music decodes it through the DirectX Media
// multimedia streaming API rather than a codec of its own. It makes an
// AMMultiMediaStream and opens the music file on it; from there it plays the
// stream one of two ways, and this module serves both:
//
//  - Pulling samples. It asks the primary audio stream for its PCM format,
//    wraps a buffer of its own in an AMAudioData object, creates a sample over
//    that buffer and calls IAudioStreamSample::Update whenever it wants the
//    next stretch of PCM, which it mixes into its own DirectSound buffer.
//    Update fills the guest's buffer synchronously and signals the event it
//    was given; MS_S_ENDOFSTREAM marks the end and IMultiMediaStream::Seek
//    rewinds. Nothing here plays sound in this mode.
//
//  - Driving the graph. It asks the stream for its filter graph and runs that
//    through IMediaControl, sets the level through IBasicAudio, seeks through
//    IMediaSeeking or IMediaPosition and watches IMediaEventEx for
//    EC_COMPLETE. There is no graph of filters here: the file is decoded
//    with minimp3 and streamed to a host audio channel, refilled from the
//    frame pump, and EC_COMPLETE is posted when the last of it has played.
//
// Object model. One host object per COM object, as everywhere in dx/:
//   K_MMSTREAM      IAMMultiMediaStream (IMultiMediaStream is its prefix);
//                   owns the decoder and playback state, one audio media
//                   stream and, once asked for, one graph.
//   K_MEDIASTREAM   IMediaStream and IAudioMediaStream, two views of the
//                   stream the multimedia stream carries.
//   K_AUDIODATA     IAudioData (IMemoryData is its prefix): the guest's
//                   buffer, its length, the bytes valid in it and a format.
//   K_STREAMSAMPLE  IAudioStreamSample (IStreamSample is its prefix): joins
//                   one media stream to one audio data object.
//   K_GRAPH         IGraphBuilder, IMediaControl, IMediaEventEx,
//                   IMediaSeeking, IBasicAudio and IMediaPosition, six views
//                   of the stream's playback.
// The vtable slot orders follow the DirectX Media SDK's mmstream.h,
// amstream.h, austream.h, strmif.h and control.h.
#include "com.h"
#include "dx.h"
#include "host_api.h"
#include "mp3_source.h"
#include "../runtime/memory.h"
#include "../runtime/win32.h"

#include <stdio.h>
#include <string.h>
#include <deque>
#include <iterator>
#include <map>
#include <string>
#include <vector>

#define IID_BYTES(a, b, c, d0, d1, d2, d3, d4, d5, d6, d7)                                         \
    {(uint8_t)((a) & 0xff),                                                                        \
     (uint8_t)(((a) >> 8) & 0xff),                                                                 \
     (uint8_t)(((a) >> 16) & 0xff),                                                                \
     (uint8_t)(((a) >> 24) & 0xff),                                                                \
     (uint8_t)((b) & 0xff),                                                                        \
     (uint8_t)(((b) >> 8) & 0xff),                                                                 \
     (uint8_t)((c) & 0xff),                                                                        \
     (uint8_t)(((c) >> 8) & 0xff),                                                                 \
     d0,                                                                                           \
     d1,                                                                                           \
     d2,                                                                                           \
     d3,                                                                                           \
     d4,                                                                                           \
     d5,                                                                                           \
     d6,                                                                                           \
     d7}

namespace {

// mmstream.h
const uint32_t MS_S_ENDOFSTREAM = 0x00040003u;
const uint32_t MS_E_PURPOSEID = 0x80040402u;
const uint32_t MS_E_NOSTREAM = 0x80040403u;
const uint32_t MS_E_INCOMPATIBLE = 0x80040405u;
const uint32_t MS_E_NOTINIT = 0x80040407u;
const uint32_t MS_E_INVALIDSTREAMTYPE = 0x80040409u;
const uint32_t HRESULT_FILE_NOT_FOUND = 0x80070002u;
const uint32_t E_ABORT = 0x80004004u;
const uint32_t S_FALSE = 1u;
const uint32_t STREAMTYPE_READ = 0;
const uint32_t STREAMSTATE_RUN = 1;
// strmif.h / control.h / evcode.h
const uint32_t State_Stopped = 0, State_Paused = 1, State_Running = 2;
const uint32_t EC_COMPLETE = 0x01;
const uint32_t AM_SEEKING_PositioningBitsMask = 0x3;
const uint32_t AM_SEEKING_AbsolutePositioning = 0x1;
const uint32_t AM_SEEKING_RelativePositioning = 0x2;
// CanSeekAbsolute | CanSeekForwards | CanSeekBackwards | CanGetCurrentPos |
// CanGetStopPos | CanGetDuration
const uint32_t kSeekingCaps = 0x3f;
const int32_t OATRUE = -1;
const uint32_t kVolumeMin = (uint32_t)-10000;
// vfw.h error codes the graph reports when a connection cannot be made.
const uint32_t VFW_E_NOT_CONNECTED = 0x80040209u;
const uint32_t VFW_E_NOT_FOUND = 0x80040216u;
const uint32_t VFW_E_CANNOT_CONNECT = 0x80040217u;

const uint8_t CLSID_AMMultiMediaStream_[16] =
    IID_BYTES(0x49c47ce5, 0x9ba4, 0x11d0, 0x82, 0x12, 0x00, 0xc0, 0x4f, 0xc3, 0x2c, 0x45);
const uint8_t CLSID_AMAudioData_[16] =
    IID_BYTES(0xf2468580, 0xaf8a, 0x11d0, 0x82, 0x12, 0x00, 0xc0, 0x4f, 0xc3, 0x2c, 0x45);
const uint8_t IID_IMultiMediaStream_[16] =
    IID_BYTES(0xb502d1bc, 0x9a57, 0x11d0, 0x8f, 0xde, 0x00, 0xc0, 0x4f, 0xd9, 0x18, 0x9d);
const uint8_t IID_IAMMultiMediaStream_[16] =
    IID_BYTES(0xbebe595c, 0x9a6f, 0x11d0, 0x8f, 0xde, 0x00, 0xc0, 0x4f, 0xd9, 0x18, 0x9d);
const uint8_t IID_IMediaStream_[16] =
    IID_BYTES(0xb502d1bd, 0x9a57, 0x11d0, 0x8f, 0xde, 0x00, 0xc0, 0x4f, 0xd9, 0x18, 0x9d);
const uint8_t IID_IAudioMediaStream_[16] =
    IID_BYTES(0xf7537560, 0xa3be, 0x11d0, 0x82, 0x12, 0x00, 0xc0, 0x4f, 0xc3, 0x2c, 0x45);
const uint8_t IID_IMemoryData_[16] =
    IID_BYTES(0x327fc560, 0xaf60, 0x11d0, 0x82, 0x12, 0x00, 0xc0, 0x4f, 0xc3, 0x2c, 0x45);
const uint8_t IID_IAudioData_[16] =
    IID_BYTES(0x54c719c0, 0xaf60, 0x11d0, 0x82, 0x12, 0x00, 0xc0, 0x4f, 0xc3, 0x2c, 0x45);
const uint8_t IID_IStreamSample_[16] =
    IID_BYTES(0xb502d1be, 0x9a57, 0x11d0, 0x8f, 0xde, 0x00, 0xc0, 0x4f, 0xd9, 0x18, 0x9d);
const uint8_t IID_IAudioStreamSample_[16] =
    IID_BYTES(0x345fee00, 0xaba5, 0x11d0, 0x82, 0x12, 0x00, 0xc0, 0x4f, 0xc3, 0x2c, 0x45);
const uint8_t MSPID_PrimaryAudio_[16] =
    IID_BYTES(0xa35ff56b, 0x9fda, 0x11d0, 0x8f, 0xdf, 0x00, 0xc0, 0x4f, 0xd9, 0x18, 0x9d);
// The quartz interfaces share {xxxxxxxx-0ad4-11ce-b03a-0020af0ba770}.
const uint8_t IID_IFilterGraph_[16] =
    IID_BYTES(0x56a8687f, 0x0ad4, 0x11ce, 0xb0, 0x3a, 0x00, 0x20, 0xaf, 0x0b, 0xa7, 0x70);
const uint8_t IID_IGraphBuilder_[16] =
    IID_BYTES(0x56a868a9, 0x0ad4, 0x11ce, 0xb0, 0x3a, 0x00, 0x20, 0xaf, 0x0b, 0xa7, 0x70);
const uint8_t IID_IMediaControl_[16] =
    IID_BYTES(0x56a868b1, 0x0ad4, 0x11ce, 0xb0, 0x3a, 0x00, 0x20, 0xaf, 0x0b, 0xa7, 0x70);
const uint8_t IID_IMediaPosition_[16] =
    IID_BYTES(0x56a868b2, 0x0ad4, 0x11ce, 0xb0, 0x3a, 0x00, 0x20, 0xaf, 0x0b, 0xa7, 0x70);
const uint8_t IID_IBasicAudio_[16] =
    IID_BYTES(0x56a868b3, 0x0ad4, 0x11ce, 0xb0, 0x3a, 0x00, 0x20, 0xaf, 0x0b, 0xa7, 0x70);
const uint8_t IID_IMediaEvent_[16] =
    IID_BYTES(0x56a868b6, 0x0ad4, 0x11ce, 0xb0, 0x3a, 0x00, 0x20, 0xaf, 0x0b, 0xa7, 0x70);
const uint8_t IID_IMediaEventEx_[16] =
    IID_BYTES(0x56a868c0, 0x0ad4, 0x11ce, 0xb0, 0x3a, 0x00, 0x20, 0xaf, 0x0b, 0xa7, 0x70);
const uint8_t IID_IMediaSeeking_[16] =
    IID_BYTES(0x36b73880, 0xc2c8, 0x11cf, 0x8b, 0x46, 0x00, 0x80, 0x5f, 0x6c, 0xef, 0x60);
const uint8_t IID_IEnumFilters_[16] =
    IID_BYTES(0x56a86893, 0x0ad4, 0x11ce, 0xb0, 0x3a, 0x00, 0x20, 0xaf, 0x0b, 0xa7, 0x70);
const uint8_t TIME_FORMAT_MEDIA_TIME_[16] =
    IID_BYTES(0x7b785574, 0x8c82, 0x11cf, 0xbc, 0x0c, 0x00, 0xaa, 0x00, 0xac, 0x74, 0xf6);

// The movie player's own graph. The guest builds it with CoCreateInstance
// rather than opening an AMMultiMediaStream, so its filters and pins are a
// separate object model from the K_GRAPH above. The four CLSIDs are the ones
// FUN_0049db30 asks for; the interfaces are the ones FUN_0049d990 and
// FUN_0049da20 walk.
const uint8_t CLSID_FilterGraph_[16] =
    IID_BYTES(0xe436ebb3, 0x524f, 0x11ce, 0x9f, 0x53, 0x00, 0x20, 0xaf, 0x0b, 0xa7, 0x70);
const uint8_t CLSID_AviSplitter_[16] =
    IID_BYTES(0x1b544c20, 0xfd0b, 0x11ce, 0x8c, 0x63, 0x00, 0xaa, 0x00, 0x44, 0xb5, 0x1e);
const uint8_t CLSID_MPEG1Splitter_[16] =
    IID_BYTES(0x336475d0, 0x942a, 0x11ce, 0xa8, 0x70, 0x00, 0xaa, 0x00, 0x2f, 0xea, 0xb5);
const uint8_t CLSID_DSoundRender_[16] =
    IID_BYTES(0x79376820, 0x07d0, 0x11cf, 0xa2, 0x4d, 0x00, 0x20, 0xaf, 0xd7, 0x97, 0x67);
// {56a868xx-0ad4-11ce-b03a-0020af0ba770}
const uint8_t IID_IBaseFilter_[16] =
    IID_BYTES(0x56a86895, 0x0ad4, 0x11ce, 0xb0, 0x3a, 0x00, 0x20, 0xaf, 0x0b, 0xa7, 0x70);
const uint8_t IID_IPin_[16] =
    IID_BYTES(0x56a86891, 0x0ad4, 0x11ce, 0xb0, 0x3a, 0x00, 0x20, 0xaf, 0x0b, 0xa7, 0x70);
const uint8_t IID_IEnumPins_[16] =
    IID_BYTES(0x56a86892, 0x0ad4, 0x11ce, 0xb0, 0x3a, 0x00, 0x20, 0xaf, 0x0b, 0xa7, 0x70);
const uint8_t IID_IEnumMediaTypes_[16] =
    IID_BYTES(0x89c31040, 0x846b, 0x11ce, 0x97, 0xd3, 0x00, 0xaa, 0x00, 0x55, 0x59, 0x5a);
// The media types FUN_0049da20 selects pins by, and the format blocks a
// VIDEOINFOHEADER carries.
const uint8_t MEDIATYPE_Video_[16] =
    IID_BYTES(0x73646976, 0x0000, 0x0010, 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71);
const uint8_t MEDIATYPE_Audio_[16] =
    IID_BYTES(0x73647561, 0x0000, 0x0010, 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71);
const uint8_t MEDIASUBTYPE_RGB24_[16] =
    IID_BYTES(0xe436eb7d, 0x524f, 0x11ce, 0x9f, 0x53, 0x00, 0x20, 0xaf, 0x0b, 0xa7, 0x70);
const uint8_t MEDIASUBTYPE_PCM_[16] =
    IID_BYTES(0x00000001, 0x0000, 0x0010, 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71);
const uint8_t FORMAT_VideoInfo_[16] =
    IID_BYTES(0x05589f80, 0xc356, 0x11ce, 0xbf, 0x01, 0x00, 0xaa, 0x00, 0x55, 0x59, 0x5a);
const uint8_t FORMAT_WaveFormatEx_[16] =
    IID_BYTES(0x05589f81, 0xc356, 0x11ce, 0xbf, 0x01, 0x00, 0xaa, 0x00, 0x55, 0x59, 0x5a);
// AM_MEDIA_TYPE + the largest format block (a VIDEOINFOHEADER).
const uint32_t kAMMediaTypeSize = 0x48;
const uint32_t kVideoInfoHeaderSize = 0x58;

// Playback through the host: chunks of decoded PCM the host reads from guest
// heap memory, a ring of them so a chunk is never rewritten before it has
// played, and how far ahead of the play cursor the pump keeps the channel.
const uint32_t kChunkBytes = 16384; // 93 ms of 44.1 kHz stereo
const uint32_t kRingSlots = 16;
const uint32_t kAheadBytes = 8 * kChunkBytes;
// Ticks a refused chunk is offered again before the channel is restarted at it.
const uint32_t kMaxRefusals = 120;

// ---------------------------------------------------------------------------
// The state behind a multimedia stream, keyed by the K_MMSTREAM object id:
// the file and its decoder, and the playback its graph may be driving.
// ---------------------------------------------------------------------------
struct Source {
    // The file. The whole of it is read once and decoded a frame at a time.
    bool loaded = false;
    std::string name; // the guest path, for the log
    Mp3Source decoder;
    size_t file_bytes = 0;
    int hz = 0, channels = 0;
    std::vector<int16_t> carry; // decoded, not yet delivered
    size_t carry_pos = 0;
    uint64_t delivered = 0; // PCM frames taken from the decoder since the last seek

    // Playback through the graph.
    int32_t channel = -1;
    bool running = false, paused = false;
    uint64_t pos = 0;       // PCM frames: where playback stands while not running
    uint32_t ring = 0;      // guest heap: kRingSlots chunks the host plays from
    uint32_t slot = 0;      // next ring slot to fill
    uint64_t submitted = 0; // bytes handed to the host since the channel became a stream
    uint32_t pending_at = 0, pending_n = 0; // a decoded chunk the host has not taken yet
    uint32_t refusals = 0;                  // ticks the pending chunk has been refused
    int32_t volume = 0;                     // IBasicAudio units: hundredths of a dB, -10000..0
    int32_t balance = 0;
    uint32_t event = 0; // the completion event, once asked for
    std::deque<uint32_t> events;
};

std::map<uint32_t, Source> &sources() {
    static auto *m = new std::map<uint32_t, Source>();
    return *m;
}

// The stream's state, whether or not a file is open yet.
Source &source_for(const ComObj *mm) {
    return sources()[mm->id];
}

// The stream's state when a file is open, else null.
Source *source_of(const ComObj *mm) {
    if (!mm)
        return nullptr;
    auto it = sources().find(mm->id);
    return it != sources().end() && it->second.loaded ? &it->second : nullptr;
}

// The decoder owns the encoded bytes; DirectShow retains a partially read
// PCM frame when the guest's buffer is smaller than the decoder's output.
bool decode_next(Source &s) {
    s.carry_pos = 0;
    return s.decoder.decode_next(s.carry);
}

void seek_frames(Source &s, uint64_t target) {
    s.decoder.seek_frames(target);
    s.carry.clear();
    s.carry_pos = 0;
    s.delivered = s.decoder.position_frames();
}

// Copies up to `want` bytes of the next PCM into `dst`, whole frames only.
// Returns the bytes written; 0 once the file is spent.
size_t fill(Source &s, uint8_t *dst, size_t want) {
    const size_t bpf = (size_t)s.channels * 2;
    want -= want % bpf;
    size_t got = 0;
    while (got < want) {
        if (s.carry_pos >= s.carry.size() && !decode_next(s))
            break;
        size_t avail = (s.carry.size() - s.carry_pos) * 2;
        size_t n = avail < want - got ? avail : want - got;
        memcpy(dst + got, s.carry.data() + s.carry_pos, n);
        s.carry_pos += n / 2;
        got += n;
    }
    s.delivered += got / bpf;
    return got;
}

// Reads the whole file and its format. False when it cannot be a stream.
bool load_source(Source &s, const std::string &host_path) {
    FILE *f = fopen(host_path.c_str(), "rb");
    if (!f)
        return false;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (n <= 0) {
        fclose(f);
        return false;
    }
    std::vector<uint8_t> bytes((size_t)n);
    size_t got = fread(bytes.data(), 1, (size_t)n, f);
    fclose(f);
    bytes.resize(got);
    s.file_bytes = got;
    s.pos = s.delivered = 0;
    s.carry.clear();
    s.carry_pos = 0;
    // The format comes from the first audible frame, retained for the first read.
    if (!s.decoder.open(bytes))
        return false;
    s.hz = (int)s.decoder.rate();
    s.channels = (int)s.decoder.channels();
    decode_next(s);
    s.loaded = true;
    return true;
}

int64_t duration_frames(Source &s) {
    return s.decoder.duration_frames();
}

// STREAM_TIME and REFERENCE_TIME are 100 ns units.
uint64_t frames_to_time(uint64_t frames, int hz) {
    return hz > 0 ? frames * 10000000ull / (uint64_t)hz : 0;
}
uint64_t time_to_frames(uint64_t t, int hz) {
    return hz > 0 ? t * (uint64_t)hz / 10000000ull : 0;
}

void write_wfx(uint32_t wfx, int hz, int channels) {
    uint16_t align = (uint16_t)(channels * 2);
    wr16(wfx + WFX_OFF_wFormatTag, WAVE_FORMAT_PCM);
    wr16(wfx + WFX_OFF_nChannels, (uint16_t)channels);
    wr32(wfx + WFX_OFF_nSamplesPerSec, (uint32_t)hz);
    wr32(wfx + WFX_OFF_nAvgBytesPerSec, (uint32_t)hz * align);
    wr16(wfx + WFX_OFF_nBlockAlign, align);
    wr16(wfx + WFX_OFF_wBitsPerSample, 16);
    wr16(wfx + WFX_OFF_cbSize, 0);
}

std::string read_wide(uint32_t a) {
    std::string s;
    for (uint32_t i = 0; i < 1024; ++i) {
        if (!gm_valid(a + 2 * i, 2))
            break;
        uint16_t w = rd16(a + 2 * i);
        if (!w)
            break;
        s.push_back(w < 256 ? (char)w : '?');
    }
    return s;
}

bool is_primary_audio(uint32_t mspid) {
    return mspid && gm_valid(mspid, 16) && memcmp(gm_ptr(mspid), MSPID_PrimaryAudio_, 16) == 0;
}

void write_u64(uint32_t at, uint64_t v) {
    if (at && gm_valid(at, 8)) {
        wr32(at, (uint32_t)v);
        wr32(at + 4, (uint32_t)(v >> 32));
    }
}
uint64_t read_u64(uint32_t at) {
    return at && gm_valid(at, 8) ? (uint64_t)rd32(at) | ((uint64_t)rd32(at + 4) << 32) : 0;
}
void write_double(uint32_t at, double d) {
    if (at && gm_valid(at, 8))
        memcpy(gm_ptr(at), &d, 8);
}
// A double passed by value occupies two stack dwords.
double arg_double(X86 *c, int first) {
    uint64_t bits = (uint64_t)arg(c, first) | ((uint64_t)arg(c, first + 1) << 32);
    double d;
    memcpy(&d, &bits, 8);
    return d;
}

// The audio media stream a multimedia stream carries, made on first need.
ComObj *media_stream_of(ComObj *mm) {
    if (mm->dsh_stream) {
        ComObj *ms = com_get(mm->dsh_stream);
        if (ms && ms->alive)
            return ms;
    }
    ComObj *ms = com_new(K_MEDIASTREAM);
    if (!ms)
        return nullptr;
    ms->dsh_owner = mm->id;
    mm->dsh_stream = ms->id;
    return ms;
}

// The graph a multimedia stream plays through, made on first need.
ComObj *graph_of(ComObj *mm) {
    if (mm->dsh_graph) {
        ComObj *g = com_get(mm->dsh_graph);
        if (g && g->alive)
            return g;
    }
    ComObj *g = com_new(K_GRAPH);
    if (!g)
        return nullptr;
    g->dsh_owner = mm->id;
    mm->dsh_graph = g->id;
    return g;
}

ComObj *owner_of(const ComObj *o) {
    ComObj *mm = o ? com_get(o->dsh_owner) : nullptr;
    return mm && mm->alive ? mm : nullptr;
}

// Hands `o` out through `iface` with the AddRef COM requires of an out-param.
bool out_view(X86 *c, uint32_t out, ComObj *o, ComIface iface) {
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, E_POINTER);
        return false;
    }
    uint32_t v = com_view(o, iface);
    if (!v) {
        wr32(out, 0);
        com_ret(c, E_OUTOFMEMORY);
        return false;
    }
    com_addref(o);
    wr32(out, v);
    com_ret(c, S_OK);
    return true;
}

// ---------------------------------------------------------------------------
// Playback through the host.
// ---------------------------------------------------------------------------
size_t frame_bytes(const Source &s) {
    return (size_t)s.channels * 2;
}

// Bytes handed to the host that it has not played yet. The channel is a
// stream (host_audio_stream), so host_audio_played_bytes counts what has
// sounded since the conversion and never goes backwards; the difference is
// what the pump paces against. The voice's own remaining count is not used:
// the host reports it as zero whenever its playing flag is down, which a
// stream between chunks can be.
uint64_t outstanding_bytes(const Source &s) {
    if (s.channel < 0)
        return 0;
    uint64_t played = host_audio_played_bytes(s.channel);
    return s.submitted > played ? s.submitted - played : 0;
}

// Where playback stands, in PCM frames: what the decoder has handed the host
// less what the host still has to play.
uint64_t current_frames(const Source &s) {
    if (!s.running || s.channel < 0)
        return s.pos;
    uint64_t remaining = outstanding_bytes(s) / frame_bytes(s);
    return s.delivered > remaining ? s.delivered - remaining : 0;
}

// Decodes the next chunk into the ring. Returns its guest address in `addr`
// and its length, 0 once the file is spent or the ring cannot be had.
uint32_t decode_chunk(Source &s, uint32_t *addr) {
    if (!s.ring) {
        s.ring = heap_alloc(kChunkBytes * kRingSlots, false, 16);
        if (!s.ring) {
            LOGW("dshow: cannot allocate the playback ring");
            return 0;
        }
    }
    uint32_t at = s.ring + s.slot * kChunkBytes;
    s.slot = (s.slot + 1) % kRingSlots;
    size_t n = fill(s, gm_ptr(at), kChunkBytes);
    *addr = at;
    return (uint32_t)n;
}

void post_event(Source &s, uint32_t code) {
    s.events.push_back(code);
    if (s.event)
        win32_signal_event(s.event, false);
}

void stop_playback(Source &s) {
    s.pending_n = 0;
    s.refusals = 0;
    if (s.running && s.channel >= 0) {
        s.pos = current_frames(s);
        host_audio_stop(s.channel);
    }
    s.running = false;
}

// Starts the host playing from `s.pos`. False when there is no channel.
bool start_playback(Source &s) {
    stop_playback(s);
    s.paused = false;
    seek_frames(s, s.pos);
    if (s.channel < 0) {
        s.channel = dx_alloc_audio_channel();
        if (s.channel < 0) {
            LOGW("dshow: no free audio channel for %s", s.name.c_str());
            return false;
        }
    }
    uint32_t at = 0;
    uint32_t n = decode_chunk(s, &at);
    if (!n) {
        // Nothing left to play from here: the run completes at once.
        s.pos = s.delivered;
        post_event(s, EC_COMPLETE);
        return true;
    }
    HostAudioPlay p{};
    p.channel = s.channel;
    p.pcm = gm_ptr(at);
    p.bytes = n;
    p.sample_rate = s.hz;
    p.channels = s.channels;
    p.bits = 16;
    p.loop = 0;
    p.volume = s.volume;
    p.pan = s.balance;
    p.start_offset = 0;
    host_audio_play(&p);
    // A stream from the start: appends continue the sound, and the played
    // count runs from the byte the conversion found the cursor at.
    int32_t from = host_audio_stream(s.channel);
    s.submitted = (from >= 0 && (uint32_t)from <= n) ? n - (uint32_t)from : n;
    s.running = true;
    LOGV("dshow: %s runs from frame %llu on channel %d", s.name.c_str(), (unsigned long long)s.pos,
         s.channel);
    return true;
}

// Starts the channel afresh at a chunk: the fallback for a host that will not
// continue the sound. The join is audible, and logged once.
void restart_at(Source &s, uint32_t at, uint32_t n) {
    log_once("dshow.requeue",
             "dshow: the host kept refusing a queued chunk; restarting the channel at it");
    HostAudioPlay p{};
    p.channel = s.channel;
    p.pcm = gm_ptr(at);
    p.bytes = n;
    p.sample_rate = s.hz;
    p.channels = s.channels;
    p.bits = 16;
    p.volume = s.volume;
    p.pan = s.balance;
    host_audio_play(&p);
    int32_t from = host_audio_stream(s.channel);
    s.submitted = (from >= 0 && (uint32_t)from <= n) ? n - (uint32_t)from : n;
}

// Offers a chunk to the channel. False when the host would not take it, in
// which case the chunk is held for the next tick: the host builds the channel
// on its own thread and may not be ready for an append in the tick the
// sound started.
bool offer(Source &s, uint32_t at, uint32_t n) {
    int32_t taken = host_audio_queue(s.channel, gm_ptr(at), n);
    if (taken <= 0) {
        s.pending_at = at;
        s.pending_n = n;
        if (++s.refusals >= kMaxRefusals) {
            restart_at(s, at, n);
            s.pending_n = 0;
            s.refusals = 0;
            return true;
        }
        return false;
    }
    s.submitted += (uint64_t)taken;
    s.pending_n = 0;
    s.refusals = 0;
    return true;
}

// One tick: keep the channel ahead of its cursor, and notice the end.
void pump(Source &s) {
    if (!s.running || s.channel < 0)
        return;
    if (s.pending_n && !offer(s, s.pending_at, s.pending_n))
        return;
    uint64_t remaining = outstanding_bytes(s);
    if (!s.decoder.drained() || s.carry_pos < s.carry.size() || s.pending_n) {
        while (remaining < kAheadBytes) {
            uint32_t at = 0;
            uint32_t n = decode_chunk(s, &at);
            if (!n)
                break;
            if (!offer(s, at, n))
                return;
            remaining += n;
        }
        return;
    }
    if (remaining == 0) {
        s.running = false;
        s.pos = s.delivered;
        LOGV("dshow: %s completed", s.name.c_str());
        post_event(s, EC_COMPLETE);
    }
}

// Moves playback to `target` frames, keeping it running if it was.
void seek_playback(Source &s, uint64_t target) {
    bool was_running = s.running;
    stop_playback(s);
    s.pos = target;
    seek_frames(s, target);
    if (was_running)
        start_playback(s);
}

void release_playback(Source &s) {
    stop_playback(s);
    if (s.channel >= 0) {
        dx_free_audio_channel(s.channel);
        s.channel = -1;
    }
    if (s.ring) {
        heap_free(s.ring);
        s.ring = 0;
    }
}

// ===========================================================================
// The movie player's own filter graph. FUN_0049db30 asks ole32 for
// CLSID_FilterGraph and drives IGraphBuilder directly, so these filters are a
// different object model from the K_GRAPH above. Each filter, pin and
// enumerator is a ComObj whose data lives in the maps below, keyed by id, the
// way Source is.
// ===========================================================================
enum MovieRole {
    MR_SOURCE = 1,
    MR_SPLITTER,
    MR_DSOUND,
};

// One media type a pin offers. The format block is built on demand; only the
// major type is needed to select a pin, but a video type carries its
// VIDEOINFOHEADER so the renderer can be negotiated against it.
struct MovieType {
    uint8_t major[16] = {0};
    uint8_t subtype[16] = {0};
    bool has_major = false;
    bool is_video = false;
    bool is_audio = false;
    uint32_t width = 0, height = 0;
    uint32_t rate = 0;
    uint16_t channels = 0, bits = 16;
};

struct MoviePin {
    uint32_t filter = 0;
    uint32_t dir = 0; // PIN_INPUT 0, PIN_OUTPUT 1
    uint32_t connected = 0;
    std::vector<MovieType> types;
};

struct MovieFilter {
    uint32_t graph = 0;
    MovieRole role = MR_SOURCE;
    std::string path;
    std::vector<uint32_t> pins;
};

struct MovieGraph {
    std::vector<uint32_t> filters;       // our filter ids this graph owns
    std::vector<uint32_t> guest_filters; // guest IBaseFilter pointers (the renderer)
    std::string path;
};

std::map<uint32_t, MoviePin> &movie_pins() {
    static auto *m = new std::map<uint32_t, MoviePin>();
    return *m;
}
std::map<uint32_t, MovieFilter> &movie_filters() {
    static auto *m = new std::map<uint32_t, MovieFilter>();
    return *m;
}
std::map<uint32_t, MovieGraph> &movie_graphs() {
    static auto *m = new std::map<uint32_t, MovieGraph>();
    return *m;
}

ComObj *movie_graph_this(X86 *c) {
    ComObj *g = com_this_arg(c, IF_GRAPH);
    return g && g->kind == K_FILTERGRAPH ? g : nullptr;
}

ComObj *movie_new_filter(MovieRole role) {
    ComObj *f = com_new(K_BASEFILTER);
    if (!f)
        return nullptr;
    MovieFilter info;
    info.role = role;
    movie_filters()[f->id] = info;
    return f;
}

uint32_t movie_new_pin(ComObj *filter, uint32_t dir, std::vector<MovieType> types) {
    ComObj *p = com_new(K_PIN);
    if (!p)
        return 0;
    MoviePin pin;
    pin.filter = filter->id;
    pin.dir = dir;
    pin.types = std::move(types);
    movie_pins()[p->id] = pin;
    movie_filters()[filter->id].pins.push_back(p->id);
    return p->id;
}

void movie_graph_attach(ComObj *graph, ComObj *filter) {
    movie_filters()[filter->id].graph = graph->id;
    movie_graphs()[graph->id].filters.push_back(filter->id);
}

// Writes an AM_MEDIA_TYPE (and, for video/audio, its format block) starting
// at `mt`, which must hold kAMMediaTypeSize + kVideoInfoHeaderSize bytes.
void write_media_type(uint32_t mt, const MovieType &t) {
    gm_zero(mt, kAMMediaTypeSize + kVideoInfoHeaderSize);
    memcpy(gm_ptr(mt), t.major, 16);
    memcpy(gm_ptr(mt + 0x10), t.subtype, 16);
    wr32(mt + 0x20, 1); // bFixedSizeSamples
    wr32(mt + 0x24, 0); // bTemporalCompression
    if (t.is_video) {
        memcpy(gm_ptr(mt + 0x2c), FORMAT_VideoInfo_, 16);
        wr32(mt + 0x28, t.width * t.height * 3);
        wr32(mt + 0x40, kVideoInfoHeaderSize);
        wr32(mt + 0x44, mt + kAMMediaTypeSize);
        uint32_t vi = mt + kAMMediaTypeSize;
        wr32(vi + 0x00, t.width);  // rcSource.right
        wr32(vi + 0x04, t.height); // rcSource.bottom
        wr32(vi + 0x10, t.width);  // rcTarget.right
        wr32(vi + 0x14, t.height); // rcTarget.bottom
        wr32(vi + 0x28, 333333);   // AvgTimePerFrame: 30 fps at 100 ns
        wr32(vi + 0x30, 40);       // bmiHeader.biSize
        wr32(vi + 0x34, t.width);
        wr32(vi + 0x38, t.height);
        wr16(vi + 0x3c, 1);  // biPlanes
        wr16(vi + 0x3e, 24); // biBitCount
        wr32(vi + 0x40, 0);  // BI_RGB
        wr32(vi + 0x44, t.width * t.height * 3);
    } else if (t.is_audio) {
        memcpy(gm_ptr(mt + 0x2c), FORMAT_WaveFormatEx_, 16);
        wr32(mt + 0x40, 18); // sizeof(WAVEFORMATEX)
        wr32(mt + 0x44, mt + kAMMediaTypeSize);
        uint32_t wf = mt + kAMMediaTypeSize;
        uint16_t align = (uint16_t)(t.channels * (t.bits / 8));
        wr16(wf + WFX_OFF_wFormatTag, WAVE_FORMAT_PCM);
        wr16(wf + WFX_OFF_nChannels, t.channels);
        wr32(wf + WFX_OFF_nSamplesPerSec, t.rate);
        wr16(wf + WFX_OFF_nBlockAlign, align);
        wr32(wf + WFX_OFF_nAvgBytesPerSec, t.rate * align);
        wr16(wf + WFX_OFF_wBitsPerSample, t.bits);
    }
}

// Hands a freshly-created object's one reference to the caller through an
// out-param. Unlike out_view this does not AddRef: the object was made for
// this answer, so the guest's Release is the one that destroys it.
void give_view(X86 *c, uint32_t out, ComObj *o, ComIface iface) {
    if (!out || !gm_valid(out, 4)) {
        com_release(o);
        com_ret(c, E_POINTER);
        return;
    }
    uint32_t v = com_view(o, iface);
    if (!v) {
        com_release(o);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    wr32(out, v);
    com_ret(c, S_OK);
}

// --- IBaseFilter ---
void MF_GetClassID(X86 *c) {
    com_ret(c, E_NOTIMPL);
}
DX_STUB(MF_Stop, S_OK)
DX_STUB(MF_Pause, S_OK)
DX_STUB(MF_Run, S_OK)
DX_STUB(MF_GetState, S_OK)
DX_STUB(MF_SetSyncSource, S_OK)
DX_STUB(MF_GetSyncSource, S_OK)

void MF_EnumPins(X86 *c) {
    ComObj *f = com_this_arg(c, IF_BASEFILTER);
    uint32_t out = arg(c, 1);
    if (!f) {
        com_ret(c, E_FAIL);
        return;
    }
    ComObj *e = com_new(K_ENUMPINS);
    if (!e) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    e->dsh_filter = f->id;
    give_view(c, out, e, IF_ENUMPINS);
}

void MF_FindPin(X86 *c) {
    // FindPin(Id, ppPin): the graph looks pins up by enumeration, so this is
    // not reached. Refuse loudly rather than hand back nothing.
    uint32_t out = arg(c, 2);
    if (out && gm_valid(out, 4))
        wr32(out, 0);
    log_once("dshow.mf_findpin", "dshow: IBaseFilter::FindPin by name is not implemented");
    com_ret(c, E_NOTIMPL);
}

void MF_QueryFilterInfo(X86 *c) {
    uint32_t out = arg(c, 1);
    if (out && gm_valid(out, 260))
        gm_zero(out, 260);
    com_ret(c, S_OK);
}

void MF_JoinFilterGraph(X86 *c) {
    com_ret(c, S_OK);
}
DX_STUB(MF_QueryVendorInfo, E_NOTIMPL)

const ComMethod g_basefilter[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"GetClassID", 2, MF_GetClassID},
    {"Stop", 1, MF_Stop},
    {"Pause", 1, MF_Pause},
    {"Run", 2, MF_Run},
    {"GetState", 3, MF_GetState},
    {"SetSyncSource", 2, MF_SetSyncSource},
    {"GetSyncSource", 2, MF_GetSyncSource},
    {"EnumPins", 2, MF_EnumPins},
    {"FindPin", 3, MF_FindPin},
    {"QueryFilterInfo", 2, MF_QueryFilterInfo},
    {"JoinFilterGraph", 3, MF_JoinFilterGraph},
    {"QueryVendorInfo", 2, MF_QueryVendorInfo},
};

// --- IPin ---
void MP_Connect(X86 *c) {
    log_once("dshow.mp_connect", "dshow: IPin::Connect is not implemented");
    com_ret(c, E_NOTIMPL);
}
void MP_ReceiveConnection(X86 *c) {
    log_once("dshow.mp_receiveconnection", "dshow: IPin::ReceiveConnection is not implemented");
    com_ret(c, E_NOTIMPL);
}

void MP_Disconnect(X86 *c) {
    ComObj *p = com_this_arg(c, IF_PIN);
    if (!p) {
        com_ret(c, E_FAIL);
        return;
    }
    movie_pins()[p->id].connected = 0;
    com_ret(c, S_OK);
}

void MP_ConnectedTo(X86 *c) {
    ComObj *p = com_this_arg(c, IF_PIN);
    uint32_t out = arg(c, 1);
    if (!p || !out || !gm_valid(out, 4)) {
        com_ret(c, p ? E_POINTER : E_FAIL);
        return;
    }
    uint32_t other = movie_pins()[p->id].connected;
    ComObj *o = other ? com_get(other) : nullptr;
    if (!o) {
        wr32(out, 0);
        com_ret(c, VFW_E_NOT_CONNECTED);
        return;
    }
    wr32(out, com_view(o, IF_PIN));
    com_addref(o);
    com_ret(c, S_OK);
}

void MP_ConnectionMediaType(X86 *c) {
    com_ret(c, VFW_E_NOT_CONNECTED);
}

void MP_QueryPinInfo(X86 *c) {
    ComObj *p = com_this_arg(c, IF_PIN);
    uint32_t out = arg(c, 1);
    if (!p || !out || !gm_valid(out, 260)) {
        com_ret(c, p ? E_POINTER : E_FAIL);
        return;
    }
    gm_zero(out, 260);
    auto it = movie_pins().find(p->id);
    if (it != movie_pins().end()) {
        ComObj *f = com_get(it->second.filter);
        wr32(out, f ? com_view(f, IF_BASEFILTER) : 0);
        wr32(out + 4, it->second.dir);
    }
    com_ret(c, S_OK);
}

void MP_QueryDirection(X86 *c) {
    ComObj *p = com_this_arg(c, IF_PIN);
    uint32_t out = arg(c, 1);
    if (!p || !out || !gm_valid(out, 4)) {
        com_ret(c, p ? E_POINTER : E_FAIL);
        return;
    }
    auto it = movie_pins().find(p->id);
    wr32(out, it == movie_pins().end() ? 0 : it->second.dir);
    com_ret(c, S_OK);
}

void MP_QueryId(X86 *c) {
    com_ret(c, E_NOTIMPL);
}
void MP_QueryAccept(X86 *c) {
    // Our own pins accept any type; the renderer's QueryAccept is the one
    // Connect probes, and that pin is the guest's, reached through guest_call.
    com_ret(c, S_OK);
}

void MP_EnumMediaTypes(X86 *c) {
    ComObj *p = com_this_arg(c, IF_PIN);
    uint32_t out = arg(c, 1);
    if (!p) {
        com_ret(c, E_FAIL);
        return;
    }
    ComObj *e = com_new(K_ENUMMEDIATETYPES);
    if (!e) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    e->dsh_filter = p->id; // the owning pin
    give_view(c, out, e, IF_ENUMMEDIATETYPES);
}

DX_STUB(MP_QueryInternalConnections, E_NOTIMPL)
DX_STUB(MP_EndOfStream, S_OK)
DX_STUB(MP_BeginFlush, S_OK)
DX_STUB(MP_EndFlush, S_OK)
DX_STUB(MP_NewSegment, S_OK)

const ComMethod g_pin[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"Connect", 3, MP_Connect},
    {"ReceiveConnection", 3, MP_ReceiveConnection},
    {"Disconnect", 1, MP_Disconnect},
    {"ConnectedTo", 2, MP_ConnectedTo},
    {"ConnectionMediaType", 2, MP_ConnectionMediaType},
    {"QueryPinInfo", 2, MP_QueryPinInfo},
    {"QueryDirection", 2, MP_QueryDirection},
    {"QueryId", 2, MP_QueryId},
    {"QueryAccept", 2, MP_QueryAccept},
    {"EnumMediaTypes", 2, MP_EnumMediaTypes},
    {"QueryInternalConnections", 3, MP_QueryInternalConnections},
    {"EndOfStream", 1, MP_EndOfStream},
    {"BeginFlush", 1, MP_BeginFlush},
    {"EndFlush", 1, MP_EndFlush},
    {"NewSegment", 4, MP_NewSegment},
};

// --- IEnumPins ---
void EP_Next(X86 *c) {
    ComObj *e = com_this_arg(c, IF_ENUMPINS);
    uint32_t count = arg(c, 1), out = arg(c, 2), fetched = arg(c, 3);
    if (!e) {
        com_ret(c, E_FAIL);
        return;
    }
    if (out && gm_valid(out, 4))
        for (uint32_t i = 0; i < count; ++i)
            wr32(out + i * 4, 0);
    uint32_t n = 0;
    auto it = movie_filters().find(e->dsh_filter);
    if (it != movie_filters().end() && out && gm_valid(out, count * 4)) {
        for (uint32_t i = 0; i < count; ++i) {
            uint32_t idx = e->dsh_pos + i;
            if (idx >= it->second.pins.size())
                break;
            ComObj *p = com_get(it->second.pins[idx]);
            if (!p)
                continue;
            wr32(out + i * 4, com_view(p, IF_PIN));
            com_addref(p);
            ++n;
        }
        e->dsh_pos += n;
    }
    if (fetched && gm_valid(fetched, 4))
        wr32(fetched, n);
    com_ret(c, n == count ? S_OK : S_FALSE);
}
void EP_Skip(X86 *c) {
    ComObj *e = com_this_arg(c, IF_ENUMPINS);
    if (e)
        e->dsh_pos += arg(c, 1);
    com_ret(c, S_OK);
}
void EP_Reset(X86 *c) {
    ComObj *e = com_this_arg(c, IF_ENUMPINS);
    if (e)
        e->dsh_pos = 0;
    com_ret(c, S_OK);
}
void EP_Clone(X86 *c) {
    ComObj *e = com_this_arg(c, IF_ENUMPINS);
    uint32_t out = arg(c, 1);
    if (!e) {
        com_ret(c, E_FAIL);
        return;
    }
    ComObj *t = com_new(K_ENUMPINS);
    if (!t) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    t->dsh_filter = e->dsh_filter;
    t->dsh_pos = e->dsh_pos;
    give_view(c, out, t, IF_ENUMPINS);
}
const ComMethod g_enumpins[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"Next", 4, EP_Next},
    {"Skip", 2, EP_Skip},
    {"Reset", 1, EP_Reset},
    {"Clone", 2, EP_Clone},
};

// --- IEnumMediaTypes ---
void EM_Next(X86 *c) {
    ComObj *e = com_this_arg(c, IF_ENUMMEDIATETYPES);
    uint32_t count = arg(c, 1), out = arg(c, 2), fetched = arg(c, 3);
    if (!e) {
        com_ret(c, E_FAIL);
        return;
    }
    if (out && gm_valid(out, 4))
        for (uint32_t i = 0; i < count; ++i)
            wr32(out + i * 4, 0);
    uint32_t n = 0;
    auto it = movie_pins().find(e->dsh_filter);
    if (it != movie_pins().end() && out && gm_valid(out, count * 4)) {
        for (uint32_t i = 0; i < count; ++i) {
            uint32_t idx = e->dsh_pos + i;
            if (idx >= it->second.types.size())
                break;
            uint32_t mt = heap_alloc(kAMMediaTypeSize + kVideoInfoHeaderSize, true, 8);
            if (!mt)
                break;
            write_media_type(mt, it->second.types[idx]);
            wr32(out + i * 4, mt);
            ++n;
        }
        e->dsh_pos += n;
    }
    if (fetched && gm_valid(fetched, 4))
        wr32(fetched, n);
    com_ret(c, n == count ? S_OK : S_FALSE);
}
void EM_Skip(X86 *c) {
    ComObj *e = com_this_arg(c, IF_ENUMMEDIATETYPES);
    if (e)
        e->dsh_pos += arg(c, 1);
    com_ret(c, S_OK);
}
void EM_Reset(X86 *c) {
    ComObj *e = com_this_arg(c, IF_ENUMMEDIATETYPES);
    if (e)
        e->dsh_pos = 0;
    com_ret(c, S_OK);
}
void EM_Clone(X86 *c) {
    ComObj *e = com_this_arg(c, IF_ENUMMEDIATETYPES);
    uint32_t out = arg(c, 1);
    if (!e) {
        com_ret(c, E_FAIL);
        return;
    }
    ComObj *t = com_new(K_ENUMMEDIATETYPES);
    if (!t) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    t->dsh_filter = e->dsh_filter;
    t->dsh_pos = e->dsh_pos;
    give_view(c, out, t, IF_ENUMMEDIATETYPES);
}
const ComMethod g_enummediatypes[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"Next", 4, EM_Next},
    {"Skip", 2, EM_Skip},
    {"Reset", 1, EM_Reset},
    {"Clone", 2, EM_Clone},
};

// --- The splitter and the two renderers ---
ComObj *movie_splitter_create() {
    ComObj *f = movie_new_filter(MR_SPLITTER);
    if (!f)
        return nullptr;
    movie_new_pin(f, 0, {}); // input
    MovieType vids;
    memcpy(vids.major, MEDIATYPE_Video_, 16);
    memcpy(vids.subtype, MEDIASUBTYPE_RGB24_, 16);
    vids.has_major = vids.is_video = true;
    movie_new_pin(f, 1, {vids});
    MovieType auds;
    memcpy(auds.major, MEDIATYPE_Audio_, 16);
    memcpy(auds.subtype, MEDIASUBTYPE_PCM_, 16);
    auds.has_major = auds.is_audio = true;
    movie_new_pin(f, 1, {auds});
    return f;
}

ComObj *movie_dsound_create() {
    ComObj *f = movie_new_filter(MR_DSOUND);
    if (!f)
        return nullptr;
    MovieType auds;
    memcpy(auds.major, MEDIATYPE_Audio_, 16);
    memcpy(auds.subtype, MEDIASUBTYPE_PCM_, 16);
    auds.has_major = auds.is_audio = true;
    movie_new_pin(f, 0, {auds});
    return f;
}

ComObj *movie_graph_create() {
    ComObj *g = com_new(K_FILTERGRAPH);
    if (!g)
        return nullptr;
    movie_graphs()[g->id] = MovieGraph{};
    return g;
}

// ===========================================================================
// IGraphBuilder over the movie graph.
// ===========================================================================
void MG_AddFilter(X86 *c) {
    ComObj *g = movie_graph_this(c);
    uint32_t pf = arg(c, 1);
    if (!g) {
        com_ret(c, E_FAIL);
        return;
    }
    if (!pf || !gm_valid(pf, 4)) {
        com_ret(c, E_POINTER);
        return;
    }
    ComObj *f = com_iface_of(pf) != IF_NONE ? com_this(pf, IF_BASEFILTER) : nullptr;
    if (f) {
        if (movie_filters()[f->id].graph != g->id) {
            com_addref(f);
            movie_graph_attach(g, f);
        }
        com_ret(c, S_OK);
        return;
    }
    // The guest's texture renderer is a real filter we do not own; keep its
    // interface pointer so Connect can reach its pins through guest_call.
    movie_graphs()[g->id].guest_filters.push_back(pf);
    LOGV("dshow: graph adds guest filter %08x", pf);
    com_ret(c, S_OK);
}

void MG_RemoveFilter(X86 *c) {
    ComObj *g = movie_graph_this(c);
    uint32_t pf = arg(c, 1);
    if (!g) {
        com_ret(c, E_FAIL);
        return;
    }
    MovieGraph &info = movie_graphs()[g->id];
    for (size_t i = 0; i < info.filters.size(); ++i) {
        ComObj *f = com_get(info.filters[i]);
        if (f && com_view(f, IF_BASEFILTER) == pf) {
            info.filters.erase(info.filters.begin() + i);
            movie_filters()[f->id].graph = 0;
            com_release(f);
            com_ret(c, S_OK);
            return;
        }
    }
    for (size_t i = 0; i < info.guest_filters.size(); ++i) {
        if (info.guest_filters[i] == pf) {
            info.guest_filters.erase(info.guest_filters.begin() + i);
            com_ret(c, S_OK);
            return;
        }
    }
    com_ret(c, VFW_E_NOT_FOUND);
}

void MG_EnumFilters(X86 *c) {
    ComObj *g = movie_graph_this(c);
    uint32_t out = arg(c, 1);
    if (!g) {
        com_ret(c, E_FAIL);
        return;
    }
    ComObj *e = com_new(K_ENUMFILTERS);
    if (!e) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    e->dsh_owner = g->id;
    give_view(c, out, e, IF_ENUMFILTERS);
}

void MG_Connect(X86 *c) {
    ComObj *g = movie_graph_this(c);
    uint32_t a = arg(c, 1), b = arg(c, 2);
    if (!g) {
        com_ret(c, E_FAIL);
        return;
    }
    ComObj *pa = com_iface_of(a) != IF_NONE ? com_this(a, IF_PIN) : nullptr;
    ComObj *pb = com_iface_of(b) != IF_NONE ? com_this(b, IF_PIN) : nullptr;
    if (pa && pb) {
        MoviePin &x = movie_pins()[pa->id];
        MoviePin &y = movie_pins()[pb->id];
        if (x.dir != 1 || y.dir != 0) {
            LOGW("dshow: Connect between two of our pins has the wrong directions");
            com_ret(c, VFW_E_CANNOT_CONNECT);
            return;
        }
        x.connected = pb->id;
        y.connected = pa->id;
        com_ret(c, S_OK);
        return;
    }
    // One side is the guest renderer's pin. Negotiating it and pushing frames
    // is the next step; failing here is honest, not a half-connected graph.
    // Evidence for that step: the renderer's IMemInputPin is at
    // renderer+0x98, its vtable is 0x0086b458, and IMemInputPin::Receive is
    // slot 6 (+0x18), not slot 3. QueryAccept is IPin slot 11 (+0x2c) and
    // ReceiveConnection is IPin slot 4 (+0x10).
    log_once("dshow.movie.guestpin", "dshow: Connect to a guest pin is not implemented yet");
    com_ret(c, VFW_E_CANNOT_CONNECT);
}

void MG_AddSourceFilter(X86 *c) {
    ComObj *g = movie_graph_this(c);
    uint32_t path = arg(c, 1), out = arg(c, 3);
    if (!g) {
        com_ret(c, E_FAIL);
        return;
    }
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, E_POINTER);
        return;
    }
    wr32(out, 0);
    std::string guest = read_wide(path);
    std::string host = win32_host_path(guest, false);
    if (host.empty()) {
        LOGW("dshow: AddSourceFilter(%s): no host path", guest.c_str());
        com_ret(c, HRESULT_FILE_NOT_FOUND);
        return;
    }
    ComObj *f = movie_new_filter(MR_SOURCE);
    if (!f) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    movie_filters()[f->id].path = guest;
    movie_new_pin(f, 1, {}); // one output pin; the splitter consumes it
    movie_graph_attach(g, f);
    LOGV("dshow: AddSourceFilter(%s) -> filter %u", guest.c_str(), f->id);
    give_view(c, out, f, IF_BASEFILTER);
}

// ===========================================================================
// IAMMultiMediaStream
// ===========================================================================
void MM_GetInformation(X86 *c) {
    ComObj *mm = com_this_arg(c, IF_MMSTREAM);
    uint32_t flags = arg(c, 1), type = arg(c, 2);
    if (!mm) {
        com_ret(c, E_FAIL);
        return;
    }
    if (flags && gm_valid(flags, 4))
        wr32(flags, 0);
    if (type && gm_valid(type, 4))
        wr32(type, STREAMTYPE_READ);
    com_ret(c, S_OK);
}

void MM_GetMediaStream(X86 *c) {
    ComObj *mm = com_this_arg(c, IF_MMSTREAM);
    uint32_t mspid = arg(c, 1), out = arg(c, 2);
    if (!mm) {
        com_ret(c, E_FAIL);
        return;
    }
    if (out && gm_valid(out, 4))
        wr32(out, 0);
    if (!is_primary_audio(mspid) || !mm->dsh_stream) {
        com_ret(c, MS_E_NOSTREAM);
        return;
    }
    out_view(c, out, media_stream_of(mm), IF_MEDIASTREAM);
}

void MM_EnumMediaStreams(X86 *c) {
    ComObj *mm = com_this_arg(c, IF_MMSTREAM);
    uint32_t index = arg(c, 1), out = arg(c, 2);
    if (!mm) {
        com_ret(c, E_FAIL);
        return;
    }
    if (out && gm_valid(out, 4))
        wr32(out, 0);
    if (index != 0 || !mm->dsh_stream) {
        com_ret(c, S_FALSE);
        return;
    }
    out_view(c, out, media_stream_of(mm), IF_MEDIASTREAM);
}

void MM_GetState(X86 *c) {
    ComObj *mm = com_this_arg(c, IF_MMSTREAM);
    uint32_t out = arg(c, 1);
    if (!mm || !out || !gm_valid(out, 4)) {
        com_ret(c, mm ? E_POINTER : E_FAIL);
        return;
    }
    wr32(out, mm->dsh_state);
    com_ret(c, S_OK);
}

void MM_SetState(X86 *c) {
    ComObj *mm = com_this_arg(c, IF_MMSTREAM);
    if (!mm) {
        com_ret(c, E_FAIL);
        return;
    }
    mm->dsh_state = arg(c, 1) ? STREAMSTATE_RUN : 0;
    com_ret(c, S_OK);
}

void MM_GetTime(X86 *c) {
    ComObj *mm = com_this_arg(c, IF_MMSTREAM);
    uint32_t out = arg(c, 1);
    if (!mm || !out || !gm_valid(out, 8)) {
        com_ret(c, mm ? E_POINTER : E_FAIL);
        return;
    }
    Source *s = source_of(mm);
    write_u64(out, s ? frames_to_time(current_frames(*s), s->hz) : 0);
    com_ret(c, S_OK);
}

void MM_GetDuration(X86 *c) {
    ComObj *mm = com_this_arg(c, IF_MMSTREAM);
    uint32_t out = arg(c, 1);
    if (!mm || !out || !gm_valid(out, 8)) {
        com_ret(c, mm ? E_POINTER : E_FAIL);
        return;
    }
    Source *s = source_of(mm);
    if (!s) {
        com_ret(c, MS_E_NOSTREAM);
        return;
    }
    write_u64(out, frames_to_time((uint64_t)duration_frames(*s), s->hz));
    com_ret(c, S_OK);
}

// Seek(STREAM_TIME): the decoder moves; a graph that is running moves with it.
void MM_Seek(X86 *c) {
    ComObj *mm = com_this_arg(c, IF_MMSTREAM);
    uint64_t t = (uint64_t)arg(c, 1) | ((uint64_t)arg(c, 2) << 32);
    if (!mm) {
        com_ret(c, E_FAIL);
        return;
    }
    Source *s = source_of(mm);
    if (!s) {
        com_ret(c, MS_E_NOSTREAM);
        return;
    }
    seek_playback(*s, time_to_frames(t, s->hz));
    LOGV("dshow: %s seeks to frame %llu", s->name.c_str(), (unsigned long long)s->delivered);
    com_ret(c, S_OK);
}

DX_STUB(MM_GetEndOfStreamEventHandle, E_NOTIMPL)

void MM_Initialize(X86 *c) {
    ComObj *mm = com_this_arg(c, IF_MMSTREAM);
    uint32_t type = arg(c, 1);
    if (!mm) {
        com_ret(c, E_FAIL);
        return;
    }
    if (type != STREAMTYPE_READ) {
        log_once("dshow.write", "dshow: only STREAMTYPE_READ streams are served here");
        com_ret(c, MS_E_INVALIDSTREAMTYPE);
        return;
    }
    mm->dsh_initialised = true;
    com_ret(c, S_OK);
}

void MM_GetFilterGraph(X86 *c) {
    ComObj *mm = com_this_arg(c, IF_MMSTREAM);
    uint32_t out = arg(c, 1);
    if (!mm) {
        com_ret(c, E_FAIL);
        return;
    }
    if (out && gm_valid(out, 4))
        wr32(out, 0);
    ComObj *g = graph_of(mm);
    if (!g) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    out_view(c, out, g, IF_GRAPH);
}

DX_STUB(MM_GetFilter, E_NOTIMPL)

// AddMediaStream(pStreamObject, pPurposeId, dwFlags, ppNewStream): the primary
// audio stream is the one this module carries. A renderer object or the
// default renderer flag changes nothing: the graph plays through the host
// either way.
void MM_AddMediaStream(X86 *c) {
    ComObj *mm = com_this_arg(c, IF_MMSTREAM);
    uint32_t mspid = arg(c, 2), out = arg(c, 4);
    if (!mm) {
        com_ret(c, E_FAIL);
        return;
    }
    if (out && gm_valid(out, 4))
        wr32(out, 0);
    if (!mm->dsh_initialised) {
        com_ret(c, MS_E_NOTINIT);
        return;
    }
    if (!is_primary_audio(mspid)) {
        log_once("dshow.purpose", "dshow: only the primary audio stream is served here");
        com_ret(c, MS_E_PURPOSEID);
        return;
    }
    ComObj *ms = media_stream_of(mm);
    if (!ms) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    if (out)
        out_view(c, out, ms, IF_MEDIASTREAM);
    else
        com_ret(c, S_OK);
}

// OpenFile(LPCWSTR, dwFlags): the file becomes the stream's source. A path
// resolves the way CreateFileA's would.
void MM_OpenFile(X86 *c) {
    ComObj *mm = com_this_arg(c, IF_MMSTREAM);
    uint32_t path = arg(c, 1);
    if (!mm) {
        com_ret(c, E_FAIL);
        return;
    }
    if (!path || !gm_valid(path, 2)) {
        com_ret(c, E_POINTER);
        return;
    }
    if (!mm->dsh_initialised) {
        com_ret(c, MS_E_NOTINIT);
        return;
    }
    std::string guest = read_wide(path);
    std::string host = win32_host_path(guest, false);
    Source &s = source_for(mm);
    stop_playback(s);
    s.loaded = false;
    s.name = guest;
    if (host.empty() || !load_source(s, host)) {
        bool exists = false;
        if (!host.empty()) {
            if (FILE *f = fopen(host.c_str(), "rb")) {
                exists = true;
                fclose(f);
            }
        }
        LOGW("dshow: OpenFile(%s): %s", guest.c_str(),
             exists ? "not an MPEG audio file" : "no such file");
        com_ret(c, exists ? MS_E_INCOMPATIBLE : HRESULT_FILE_NOT_FOUND);
        return;
    }
    if (!mm->dsh_stream)
        media_stream_of(mm);
    LOGV("dshow: OpenFile(%s): %d Hz, %d channel(s), %zu bytes", guest.c_str(), s.hz, s.channels,
         s.file_bytes);
    com_ret(c, S_OK);
}

DX_STUB(MM_OpenMoniker, E_NOTIMPL)
DX_STUB(MM_Render, S_OK)

const ComMethod g_mmstream[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"GetInformation", 3, MM_GetInformation},
    {"GetMediaStream", 3, MM_GetMediaStream},
    {"EnumMediaStreams", 3, MM_EnumMediaStreams},
    {"GetState", 2, MM_GetState},
    {"SetState", 2, MM_SetState},
    {"GetTime", 2, MM_GetTime},
    {"GetDuration", 2, MM_GetDuration},
    {"Seek", 3, MM_Seek},
    {"GetEndOfStreamEventHandle", 2, MM_GetEndOfStreamEventHandle},
    {"Initialize", 4, MM_Initialize},
    {"GetFilterGraph", 2, MM_GetFilterGraph},
    {"GetFilter", 2, MM_GetFilter},
    {"AddMediaStream", 5, MM_AddMediaStream},
    {"OpenFile", 3, MM_OpenFile},
    {"OpenMoniker", 4, MM_OpenMoniker},
    {"Render", 2, MM_Render},
};

// ===========================================================================
// IMediaStream / IAudioMediaStream
// ===========================================================================
void MS_GetMultiMediaStream(X86 *c) {
    ComObj *ms = com_this_arg(c);
    uint32_t out = arg(c, 1);
    ComObj *mm = owner_of(ms);
    if (!mm) {
        com_ret(c, E_FAIL);
        return;
    }
    out_view(c, out, mm, IF_MMSTREAM);
}

void MS_GetInformation(X86 *c) {
    ComObj *ms = com_this_arg(c);
    uint32_t purpose = arg(c, 1), type = arg(c, 2);
    if (!ms) {
        com_ret(c, E_FAIL);
        return;
    }
    if (purpose && gm_valid(purpose, 16))
        memcpy(gm_ptr(purpose), MSPID_PrimaryAudio_, 16);
    if (type && gm_valid(type, 4))
        wr32(type, STREAMTYPE_READ);
    com_ret(c, S_OK);
}

DX_STUB(MS_SetSameFormat, S_OK)
DX_STUB(MS_AllocateSample, E_NOTIMPL)
DX_STUB(MS_CreateSharedSample, E_NOTIMPL)
DX_STUB(MS_SendEndOfStream, S_OK)

void AMS_GetFormat(X86 *c) {
    ComObj *ms = com_this_arg(c, IF_AUDIOMEDIASTREAM);
    uint32_t wfx = arg(c, 1);
    Source *s = source_of(owner_of(ms));
    if (!ms) {
        com_ret(c, E_FAIL);
        return;
    }
    if (!wfx || !gm_valid(wfx, WFX_SIZE)) {
        com_ret(c, E_POINTER);
        return;
    }
    if (!s) {
        com_ret(c, MS_E_NOSTREAM);
        return;
    }
    write_wfx(wfx, s->hz, s->channels);
    com_ret(c, S_OK);
}

// SetFormat: the stream's PCM is what the file holds; a request for exactly
// that is accepted and anything else is incompatible, since no conversion
// happens here.
void AMS_SetFormat(X86 *c) {
    ComObj *ms = com_this_arg(c, IF_AUDIOMEDIASTREAM);
    uint32_t wfx = arg(c, 1);
    Source *s = source_of(owner_of(ms));
    if (!ms) {
        com_ret(c, E_FAIL);
        return;
    }
    if (!wfx || !gm_valid(wfx, 16)) {
        com_ret(c, E_POINTER);
        return;
    }
    if (!s) {
        com_ret(c, MS_E_NOSTREAM);
        return;
    }
    bool same = rd16(wfx + WFX_OFF_wFormatTag) == WAVE_FORMAT_PCM &&
                rd16(wfx + WFX_OFF_nChannels) == (uint16_t)s->channels &&
                rd32(wfx + WFX_OFF_nSamplesPerSec) == (uint32_t)s->hz &&
                rd16(wfx + WFX_OFF_wBitsPerSample) == 16;
    if (!same)
        log_once("dshow.setformat",
                 "dshow: SetFormat asked for %u Hz x%u %u-bit; the stream is "
                 "%d Hz x%d 16-bit and is not converted",
                 rd32(wfx + WFX_OFF_nSamplesPerSec), rd16(wfx + WFX_OFF_nChannels),
                 rd16(wfx + WFX_OFF_wBitsPerSample), s->hz, s->channels);
    com_ret(c, same ? S_OK : MS_E_INCOMPATIBLE);
}

// CreateSample(pAudioData, dwFlags, ppSample)
void AMS_CreateSample(X86 *c) {
    ComObj *ms = com_this_arg(c, IF_AUDIOMEDIASTREAM);
    ComObj *ad = com_this(arg(c, 1), IF_AUDIODATA);
    uint32_t out = arg(c, 3);
    if (!ms) {
        com_ret(c, E_FAIL);
        return;
    }
    if (out && gm_valid(out, 4))
        wr32(out, 0);
    if (!ad) {
        com_ret(c, E_INVALIDARG);
        return;
    }
    ComObj *sp = com_new(K_STREAMSAMPLE);
    if (!sp) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    sp->dsh_owner = ms->id;
    sp->dsh_data = ad->id;
    com_addref(ms);
    com_addref(ad);
    uint32_t v = com_view(sp, IF_STREAMSAMPLE);
    if (!v || !out || !gm_valid(out, 4)) {
        com_release(sp);
        com_ret(c, v ? E_POINTER : E_OUTOFMEMORY);
        return;
    }
    wr32(out, v);
    com_ret(c, S_OK);
}

const ComMethod g_mediastream[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"GetMultiMediaStream", 2, MS_GetMultiMediaStream},
    {"GetInformation", 3, MS_GetInformation},
    {"SetSameFormat", 3, MS_SetSameFormat},
    {"AllocateSample", 3, MS_AllocateSample},
    {"CreateSharedSample", 4, MS_CreateSharedSample},
    {"SendEndOfStream", 2, MS_SendEndOfStream},
};

const ComMethod g_audiomediastream[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"GetMultiMediaStream", 2, MS_GetMultiMediaStream},
    {"GetInformation", 3, MS_GetInformation},
    {"SetSameFormat", 3, MS_SetSameFormat},
    {"AllocateSample", 3, MS_AllocateSample},
    {"CreateSharedSample", 4, MS_CreateSharedSample},
    {"SendEndOfStream", 2, MS_SendEndOfStream},
    {"GetFormat", 2, AMS_GetFormat},
    {"SetFormat", 2, AMS_SetFormat},
    {"CreateSample", 4, AMS_CreateSample},
};

// ===========================================================================
// IAudioData (IMemoryData)
// ===========================================================================
// SetBuffer(cbSize, pbData, dwFlags): the guest's buffer, checked once here
// and again at every fill.
void AD_SetBuffer(X86 *c) {
    ComObj *ad = com_this_arg(c, IF_AUDIODATA);
    uint32_t size = arg(c, 1), data = arg(c, 2);
    if (!ad) {
        com_ret(c, E_FAIL);
        return;
    }
    if (!size || !data || !gm_valid(data, size)) {
        com_ret(c, E_INVALIDARG);
        return;
    }
    ad->md_buffer = data;
    ad->md_length = size;
    ad->md_actual = 0;
    com_ret(c, S_OK);
}

// GetInfo(pdwLength, ppbData, pcbActualData): each may be null.
void AD_GetInfo(X86 *c) {
    ComObj *ad = com_this_arg(c, IF_AUDIODATA);
    uint32_t len = arg(c, 1), data = arg(c, 2), actual = arg(c, 3);
    if (!ad) {
        com_ret(c, E_FAIL);
        return;
    }
    if (len && gm_valid(len, 4))
        wr32(len, ad->md_length);
    if (data && gm_valid(data, 4))
        wr32(data, ad->md_buffer);
    if (actual && gm_valid(actual, 4))
        wr32(actual, ad->md_actual);
    com_ret(c, S_OK);
}

void AD_SetActual(X86 *c) {
    ComObj *ad = com_this_arg(c, IF_AUDIODATA);
    uint32_t n = arg(c, 1);
    if (!ad) {
        com_ret(c, E_FAIL);
        return;
    }
    if (n > ad->md_length) {
        com_ret(c, E_INVALIDARG);
        return;
    }
    ad->md_actual = n;
    com_ret(c, S_OK);
}

void AD_GetFormat(X86 *c) {
    ComObj *ad = com_this_arg(c, IF_AUDIODATA);
    uint32_t wfx = arg(c, 1);
    if (!ad) {
        com_ret(c, E_FAIL);
        return;
    }
    if (!wfx || !gm_valid(wfx, WFX_SIZE)) {
        com_ret(c, E_POINTER);
        return;
    }
    if (!ad->md_has_format) {
        com_ret(c, MS_E_NOSTREAM);
        return;
    }
    write_wfx(wfx, (int)ad->md_rate, ad->md_channels);
    uint16_t align = (uint16_t)(ad->md_channels * ad->md_bits / 8);
    wr16(wfx + WFX_OFF_wBitsPerSample, ad->md_bits);
    wr16(wfx + WFX_OFF_nBlockAlign, align);
    wr32(wfx + WFX_OFF_nAvgBytesPerSec, ad->md_rate * align);
    com_ret(c, S_OK);
}

void AD_SetFormat(X86 *c) {
    ComObj *ad = com_this_arg(c, IF_AUDIODATA);
    uint32_t wfx = arg(c, 1);
    if (!ad) {
        com_ret(c, E_FAIL);
        return;
    }
    if (!wfx || !gm_valid(wfx, 16)) {
        com_ret(c, E_POINTER);
        return;
    }
    if (rd16(wfx + WFX_OFF_wFormatTag) != WAVE_FORMAT_PCM) {
        com_ret(c, MS_E_INCOMPATIBLE);
        return;
    }
    ad->md_has_format = true;
    ad->md_channels = rd16(wfx + WFX_OFF_nChannels);
    ad->md_rate = rd32(wfx + WFX_OFF_nSamplesPerSec);
    ad->md_bits = rd16(wfx + WFX_OFF_wBitsPerSample);
    com_ret(c, S_OK);
}

const ComMethod g_audiodata[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"SetBuffer", 4, AD_SetBuffer},
    {"GetInfo", 4, AD_GetInfo},
    {"SetActual", 2, AD_SetActual},
    {"GetFormat", 2, AD_GetFormat},
    {"SetFormat", 2, AD_SetFormat},
};

// ===========================================================================
// IAudioStreamSample (IStreamSample)
// ===========================================================================
void SP_GetMediaStream(X86 *c) {
    ComObj *sp = com_this_arg(c, IF_STREAMSAMPLE);
    ComObj *ms = owner_of(sp);
    if (!ms) {
        com_ret(c, E_FAIL);
        return;
    }
    out_view(c, arg(c, 1), ms, IF_MEDIASTREAM);
}

void SP_GetSampleTimes(X86 *c) {
    ComObj *sp = com_this_arg(c, IF_STREAMSAMPLE);
    if (!sp) {
        com_ret(c, E_FAIL);
        return;
    }
    Source *s = source_of(owner_of(owner_of(sp)));
    int hz = s ? s->hz : 0;
    write_u64(arg(c, 1), frames_to_time(sp->smp_start, hz));
    write_u64(arg(c, 2), frames_to_time(sp->smp_end, hz));
    write_u64(arg(c, 3), s ? frames_to_time(s->delivered, hz) : 0);
    com_ret(c, S_OK);
}

DX_STUB(SP_SetSampleTimes, S_OK)

// Update(dwFlags, hEvent, pfnAPC, dwAPCData): fills the audio data object's
// buffer with the next PCM from the file, synchronously. The event, when one
// is given, is signalled so a caller that waits on it continues at once. At
// the end of the file nothing is written and MS_S_ENDOFSTREAM says so; the
// caller seeks to loop.
void SP_Update(X86 *c) {
    ComObj *sp = com_this_arg(c, IF_STREAMSAMPLE);
    uint32_t event = arg(c, 2), apc = arg(c, 3);
    if (!sp) {
        com_ret(c, E_FAIL);
        return;
    }
    ComObj *ms = owner_of(sp);
    ComObj *ad = com_get(sp->dsh_data);
    Source *s = source_of(owner_of(ms));
    if (!ms || !ad || !ad->alive) {
        com_ret(c, E_FAIL);
        return;
    }
    if (!s) {
        com_ret(c, MS_E_NOSTREAM);
        return;
    }
    if (apc)
        log_once("dshow.apc",
                 "dshow: Update's APC callback is not called; the fill is synchronous");
    if (!ad->md_buffer || !ad->md_length || !gm_valid(ad->md_buffer, ad->md_length)) {
        com_ret(c, E_INVALIDARG);
        return;
    }
    sp->smp_start = s->delivered;
    size_t got = fill(*s, gm_ptr(ad->md_buffer), ad->md_length);
    sp->smp_end = s->delivered;
    ad->md_actual = (uint32_t)got;
    if (got == 0) {
        com_ret(c, MS_S_ENDOFSTREAM);
        return;
    }
    if (event && !win32_signal_event(event, false))
        log_once("dshow.event", "dshow: Update's hEvent %08x is not an event", event);
    com_ret(c, S_OK);
}

// CompletionStatus(dwFlags, dwMilliseconds): every Update completed before it
// returned, so there is never anything pending.
void SP_CompletionStatus(X86 *c) {
    ComObj *sp = com_this_arg(c, IF_STREAMSAMPLE);
    com_ret(c, sp ? S_OK : E_FAIL);
}

void SP_GetAudioData(X86 *c) {
    ComObj *sp = com_this_arg(c, IF_STREAMSAMPLE);
    ComObj *ad = sp ? com_get(sp->dsh_data) : nullptr;
    if (!ad || !ad->alive) {
        com_ret(c, E_FAIL);
        return;
    }
    out_view(c, arg(c, 1), ad, IF_AUDIODATA);
}

const ComMethod g_streamsample[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"GetMediaStream", 2, SP_GetMediaStream},
    {"GetSampleTimes", 4, SP_GetSampleTimes},
    {"SetSampleTimes", 3, SP_SetSampleTimes},
    {"Update", 5, SP_Update},
    {"CompletionStatus", 3, SP_CompletionStatus},
    {"GetAudioData", 2, SP_GetAudioData},
};

// ===========================================================================
// The graph: IGraphBuilder, IMediaControl, IMediaEventEx, IMediaSeeking,
// IBasicAudio, IMediaPosition. Every view resolves to the stream's Source.
// ===========================================================================
// `this` of any graph view, with its stream and state. `s` is null when the
// graph's stream has no file open yet.
struct GraphThis {
    ComObj *g = nullptr;
    ComObj *mm = nullptr;
    Source *s = nullptr;
};
GraphThis graph_this(X86 *c) {
    GraphThis t;
    t.g = com_this_arg(c);
    if (t.g && t.g->kind != K_GRAPH)
        t.g = nullptr;
    t.mm = owner_of(t.g);
    t.s = t.mm ? &source_for(t.mm) : nullptr;
    return t;
}

// The IDispatch slots the automation interfaces carry; no scripting host
// calls them.
DX_STUB(DISP_GetTypeInfoCount, E_NOTIMPL)
DX_STUB(DISP_GetTypeInfo, E_NOTIMPL)
DX_STUB(DISP_GetIDsOfNames, E_NOTIMPL)
DX_STUB(DISP_Invoke, E_NOTIMPL)

// --- IEnumFilters: over a K_GRAPH it is empty, and an enumerator that
// fetches nothing is how that graph says so. A movie graph's filters are
// real, so Next walks them.
void EF_Next(X86 *c) {
    ComObj *e = com_this_arg(c, IF_ENUMFILTERS);
    uint32_t count = arg(c, 1), out = arg(c, 2), fetched = arg(c, 3);
    if (!e) {
        com_ret(c, E_FAIL);
        return;
    }
    if (out && gm_valid(out, 4))
        for (uint32_t i = 0; i < count; ++i)
            wr32(out + i * 4, 0);
    uint32_t n = 0;
    ComObj *owner = com_get(e->dsh_owner);
    if (owner && owner->kind == K_FILTERGRAPH && out && gm_valid(out, count * 4)) {
        MovieGraph &g = movie_graphs()[owner->id];
        for (uint32_t i = 0; i < count; ++i) {
            uint32_t idx = e->dsh_pos + i;
            if (idx >= g.filters.size())
                break;
            ComObj *f = com_get(g.filters[idx]);
            if (!f)
                continue;
            wr32(out + i * 4, com_view(f, IF_BASEFILTER));
            com_addref(f);
            ++n;
        }
        e->dsh_pos += n;
    }
    if (fetched && gm_valid(fetched, 4))
        wr32(fetched, n);
    com_ret(c, n == count ? S_OK : S_FALSE);
}
void EF_Skip(X86 *c) {
    ComObj *e = com_this_arg(c, IF_ENUMFILTERS);
    if (e)
        e->dsh_pos += arg(c, 1);
    com_ret(c, S_FALSE);
}
void EF_Reset(X86 *c) {
    ComObj *e = com_this_arg(c, IF_ENUMFILTERS);
    if (e)
        e->dsh_pos = 0;
    com_ret(c, S_OK);
}
void EF_Clone(X86 *c) {
    ComObj *e = com_this_arg(c, IF_ENUMFILTERS);
    uint32_t out = arg(c, 1);
    if (!e) {
        com_ret(c, E_FAIL);
        return;
    }
    ComObj *twin = com_new(K_ENUMFILTERS);
    if (!twin) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    twin->dsh_owner = e->dsh_owner;
    twin->dsh_pos = e->dsh_pos;
    uint32_t v = com_view(twin, IF_ENUMFILTERS);
    if (!v || !out || !gm_valid(out, 4)) {
        com_release(twin);
        com_ret(c, v ? E_POINTER : E_OUTOFMEMORY);
        return;
    }
    wr32(out, v);
    com_ret(c, S_OK);
}

const ComMethod g_enumfilters[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"Next", 4, EF_Next},
    {"Skip", 2, EF_Skip},
    {"Reset", 1, EF_Reset},
    {"Clone", 2, EF_Clone},
};

// --- IGraphBuilder: the K_GRAPH has no filters to build with, so its
// mutators are inert. The movie graph's are real and dispatch to MG_*.
void GB_AddFilter(X86 *c) {
    if (movie_graph_this(c)) {
        MG_AddFilter(c);
        return;
    }
    log_once("dx.GB_AddFilter", "dx: AddFilter on a multimedia-stream graph does nothing");
    com_ret(c, E_NOTIMPL);
}
void GB_RemoveFilter(X86 *c) {
    if (movie_graph_this(c)) {
        MG_RemoveFilter(c);
        return;
    }
    log_once("dx.GB_RemoveFilter", "dx: RemoveFilter on a multimedia-stream graph does nothing");
    com_ret(c, E_NOTIMPL);
}

void GB_EnumFilters(X86 *c) {
    if (movie_graph_this(c)) {
        MG_EnumFilters(c);
        return;
    }
    GraphThis t = graph_this(c);
    uint32_t out = arg(c, 1);
    if (!t.g) {
        com_ret(c, E_FAIL);
        return;
    }
    if (out && gm_valid(out, 4))
        wr32(out, 0);
    ComObj *e = com_new(K_ENUMFILTERS);
    if (!e) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    e->dsh_owner = t.g->id;
    uint32_t v = com_view(e, IF_ENUMFILTERS);
    if (!v || !out || !gm_valid(out, 4)) {
        com_release(e);
        com_ret(c, v ? E_POINTER : E_OUTOFMEMORY);
        return;
    }
    wr32(out, v);
    com_ret(c, S_OK);
}
DX_STUB(GB_FindFilterByName, E_NOTIMPL)
DX_STUB(GB_ConnectDirect, E_NOTIMPL)
DX_STUB(GB_Reconnect, E_NOTIMPL)
DX_STUB(GB_Disconnect, E_NOTIMPL)
DX_STUB(GB_SetDefaultSyncSource, S_OK)
void GB_Connect(X86 *c) {
    if (movie_graph_this(c)) {
        MG_Connect(c);
        return;
    }
    log_once("dx.GB_Connect", "dx: Connect on a multimedia-stream graph does nothing");
    com_ret(c, E_NOTIMPL);
}
DX_STUB(GB_Render, E_NOTIMPL)
DX_STUB(GB_RenderFile, E_NOTIMPL)
void GB_AddSourceFilter(X86 *c) {
    if (movie_graph_this(c)) {
        MG_AddSourceFilter(c);
        return;
    }
    log_once("dx.GB_AddSourceFilter",
             "dx: AddSourceFilter on a multimedia-stream graph does nothing");
    com_ret(c, E_NOTIMPL);
}
DX_STUB(GB_SetLogFile, S_OK)
DX_STUB(GB_Abort, S_OK)
DX_STUB(GB_ShouldOperationContinue, S_OK)

const ComMethod g_graph[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"AddFilter", 3, GB_AddFilter},
    {"RemoveFilter", 2, GB_RemoveFilter},
    {"EnumFilters", 2, GB_EnumFilters},
    {"FindFilterByName", 3, GB_FindFilterByName},
    {"ConnectDirect", 4, GB_ConnectDirect},
    {"Reconnect", 2, GB_Reconnect},
    {"Disconnect", 2, GB_Disconnect},
    {"SetDefaultSyncSource", 1, GB_SetDefaultSyncSource},
    {"Connect", 3, GB_Connect},
    {"Render", 2, GB_Render},
    {"RenderFile", 3, GB_RenderFile},
    {"AddSourceFilter", 4, GB_AddSourceFilter},
    {"SetLogFile", 2, GB_SetLogFile},
    {"Abort", 1, GB_Abort},
    {"ShouldOperationContinue", 1, GB_ShouldOperationContinue},
};

// --- IMediaControl
void MC_Run(X86 *c) {
    GraphThis t = graph_this(c);
    if (!t.s) {
        com_ret(c, E_FAIL);
        return;
    }
    if (!t.s->loaded) {
        com_ret(c, MS_E_NOSTREAM);
        return;
    }
    if (t.s->running) {
        com_ret(c, S_OK);
        return;
    }
    com_ret(c, start_playback(*t.s) ? S_OK : E_FAIL);
}

// Pause and Stop both silence the channel and keep the position, which is
// what a later Run continues from; DirectShow's Stop keeps it too.
void MC_Pause(X86 *c) {
    GraphThis t = graph_this(c);
    if (!t.s) {
        com_ret(c, E_FAIL);
        return;
    }
    stop_playback(*t.s);
    t.s->paused = true;
    com_ret(c, S_OK);
}

void MC_Stop(X86 *c) {
    GraphThis t = graph_this(c);
    if (!t.s) {
        com_ret(c, E_FAIL);
        return;
    }
    stop_playback(*t.s);
    t.s->paused = false;
    com_ret(c, S_OK);
}

// GetState(msTimeout, pfs)
void MC_GetState(X86 *c) {
    GraphThis t = graph_this(c);
    uint32_t out = arg(c, 2);
    if (!t.s) {
        com_ret(c, E_FAIL);
        return;
    }
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, E_POINTER);
        return;
    }
    wr32(out, t.s->running ? State_Running : t.s->paused ? State_Paused : State_Stopped);
    com_ret(c, S_OK);
}

DX_STUB(MC_RenderFile, E_NOTIMPL)
DX_STUB(MC_AddSourceFilter, E_NOTIMPL)
DX_STUB(MC_get_FilterCollection, E_NOTIMPL)
DX_STUB(MC_get_RegFilterCollection, E_NOTIMPL)

const ComMethod g_mediacontrol[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"GetTypeInfoCount", 2, DISP_GetTypeInfoCount},
    {"GetTypeInfo", 4, DISP_GetTypeInfo},
    {"GetIDsOfNames", 6, DISP_GetIDsOfNames},
    {"Invoke", 9, DISP_Invoke},
    {"Run", 1, MC_Run},
    {"Pause", 1, MC_Pause},
    {"Stop", 1, MC_Stop},
    {"GetState", 3, MC_GetState},
    {"RenderFile", 2, MC_RenderFile},
    {"AddSourceFilter", 3, MC_AddSourceFilter},
    {"get_FilterCollection", 2, MC_get_FilterCollection},
    {"get_RegFilterCollection", 2, MC_get_RegFilterCollection},
    {"StopWhenReady", 1, MC_Stop},
};

// --- IMediaEventEx. The completion event is a manual-reset event that stays
// set while the queue holds anything, as the real graph's does.
void ME_GetEventHandle(X86 *c) {
    GraphThis t = graph_this(c);
    uint32_t out = arg(c, 1);
    if (!t.s) {
        com_ret(c, E_FAIL);
        return;
    }
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, E_POINTER);
        return;
    }
    if (!t.s->event)
        t.s->event = win32_create_event(true, !t.s->events.empty());
    wr32(out, t.s->event);
    com_ret(c, S_OK);
}

// GetEvent(plEventCode, plParam1, plParam2, msTimeout): the next event, or
// E_ABORT when there is none. The timeout is not waited: the pump that would
// produce an event runs on the frame, not here.
void ME_GetEvent(X86 *c) {
    GraphThis t = graph_this(c);
    uint32_t code = arg(c, 1), p1 = arg(c, 2), p2 = arg(c, 3);
    if (!t.s) {
        com_ret(c, E_FAIL);
        return;
    }
    if (t.s->events.empty()) {
        com_ret(c, E_ABORT);
        return;
    }
    uint32_t ev = t.s->events.front();
    t.s->events.pop_front();
    if (code && gm_valid(code, 4))
        wr32(code, ev);
    if (p1 && gm_valid(p1, 4))
        wr32(p1, 0);
    if (p2 && gm_valid(p2, 4))
        wr32(p2, 0);
    if (t.s->events.empty() && t.s->event)
        win32_reset_event(t.s->event);
    com_ret(c, S_OK);
}

// WaitForCompletion(msTimeout, pEvCode): answers from what has happened;
// nothing blocks here.
void ME_WaitForCompletion(X86 *c) {
    GraphThis t = graph_this(c);
    uint32_t out = arg(c, 2);
    if (!t.s) {
        com_ret(c, E_FAIL);
        return;
    }
    bool done = !t.s->running && t.s->loaded && t.s->decoder.drained();
    if (out && gm_valid(out, 4))
        wr32(out, done ? EC_COMPLETE : 0);
    com_ret(c, done ? S_OK : E_ABORT);
}

DX_STUB(ME_CancelDefaultHandling, S_OK)
DX_STUB(ME_RestoreDefaultHandling, S_OK)
DX_STUB(ME_FreeEventParams, S_OK)
DX_STUB(ME_SetNotifyWindow, S_OK)

void ME_SetNotifyFlags(X86 *c) {
    GraphThis t = graph_this(c);
    if (!t.g) {
        com_ret(c, E_FAIL);
        return;
    }
    t.g->dsh_notify_flags = arg(c, 1);
    com_ret(c, S_OK);
}

void ME_GetNotifyFlags(X86 *c) {
    GraphThis t = graph_this(c);
    uint32_t out = arg(c, 1);
    if (!t.g) {
        com_ret(c, E_FAIL);
        return;
    }
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, E_POINTER);
        return;
    }
    wr32(out, t.g->dsh_notify_flags);
    com_ret(c, S_OK);
}

const ComMethod g_mediaevent[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"GetTypeInfoCount", 2, DISP_GetTypeInfoCount},
    {"GetTypeInfo", 4, DISP_GetTypeInfo},
    {"GetIDsOfNames", 6, DISP_GetIDsOfNames},
    {"Invoke", 9, DISP_Invoke},
    {"GetEventHandle", 2, ME_GetEventHandle},
    {"GetEvent", 5, ME_GetEvent},
    {"WaitForCompletion", 3, ME_WaitForCompletion},
    {"CancelDefaultHandling", 2, ME_CancelDefaultHandling},
    {"RestoreDefaultHandling", 2, ME_RestoreDefaultHandling},
    {"FreeEventParams", 4, ME_FreeEventParams},
    {"SetNotifyWindow", 4, ME_SetNotifyWindow},
    {"SetNotifyFlags", 2, ME_SetNotifyFlags},
    {"GetNotifyFlags", 2, ME_GetNotifyFlags},
};

// --- IMediaSeeking, in media time (100 ns units), the only format here.
bool is_media_time(uint32_t guid) {
    return guid && gm_valid(guid, 16) && memcmp(gm_ptr(guid), TIME_FORMAT_MEDIA_TIME_, 16) == 0;
}

void SK_GetCapabilities(X86 *c) {
    GraphThis t = graph_this(c);
    uint32_t out = arg(c, 1);
    if (!t.s || !out || !gm_valid(out, 4)) {
        com_ret(c, t.s ? E_POINTER : E_FAIL);
        return;
    }
    wr32(out, kSeekingCaps);
    com_ret(c, S_OK);
}

void SK_CheckCapabilities(X86 *c) {
    GraphThis t = graph_this(c);
    uint32_t inout = arg(c, 1);
    if (!t.s || !inout || !gm_valid(inout, 4)) {
        com_ret(c, t.s ? E_POINTER : E_FAIL);
        return;
    }
    uint32_t asked = rd32(inout);
    wr32(inout, asked & kSeekingCaps);
    com_ret(c, (asked & kSeekingCaps) == asked ? S_OK : S_FALSE);
}

void SK_IsFormatSupported(X86 *c) {
    com_ret(c, is_media_time(arg(c, 1)) ? S_OK : S_FALSE);
}

void SK_QueryPreferredFormat(X86 *c) {
    uint32_t out = arg(c, 1);
    if (!out || !gm_valid(out, 16)) {
        com_ret(c, E_POINTER);
        return;
    }
    memcpy(gm_ptr(out), TIME_FORMAT_MEDIA_TIME_, 16);
    com_ret(c, S_OK);
}

void SK_IsUsingTimeFormat(X86 *c) {
    com_ret(c, is_media_time(arg(c, 1)) ? S_OK : S_FALSE);
}

void SK_SetTimeFormat(X86 *c) {
    com_ret(c, is_media_time(arg(c, 1)) ? S_OK : E_INVALIDARG);
}

void SK_GetDuration(X86 *c) {
    GraphThis t = graph_this(c);
    uint32_t out = arg(c, 1);
    if (!t.s || !out || !gm_valid(out, 8)) {
        com_ret(c, t.s ? E_POINTER : E_FAIL);
        return;
    }
    if (!t.s->loaded) {
        com_ret(c, MS_E_NOSTREAM);
        return;
    }
    write_u64(out, frames_to_time((uint64_t)duration_frames(*t.s), t.s->hz));
    com_ret(c, S_OK);
}

void SK_GetCurrentPosition(X86 *c) {
    GraphThis t = graph_this(c);
    uint32_t out = arg(c, 1);
    if (!t.s || !out || !gm_valid(out, 8)) {
        com_ret(c, t.s ? E_POINTER : E_FAIL);
        return;
    }
    write_u64(out, t.s->loaded ? frames_to_time(current_frames(*t.s), t.s->hz) : 0);
    com_ret(c, S_OK);
}

// ConvertTimeFormat(pTarget, pTargetFormat, Source, pSourceFormat): one
// format, so the value passes through.
void SK_ConvertTimeFormat(X86 *c) {
    uint32_t out = arg(c, 1);
    uint64_t src = (uint64_t)arg(c, 3) | ((uint64_t)arg(c, 4) << 32);
    if (!out || !gm_valid(out, 8)) {
        com_ret(c, E_POINTER);
        return;
    }
    write_u64(out, src);
    com_ret(c, S_OK);
}

// SetPositions(pCurrent, dwCurrentFlags, pStop, dwStopFlags): the current
// position moves the decoder and a running channel with it; the stop
// position is always the end of the file.
void SK_SetPositions(X86 *c) {
    GraphThis t = graph_this(c);
    uint32_t cur = arg(c, 1), cur_flags = arg(c, 2);
    if (!t.s) {
        com_ret(c, E_FAIL);
        return;
    }
    if (!t.s->loaded) {
        com_ret(c, MS_E_NOSTREAM);
        return;
    }
    uint32_t how = cur_flags & AM_SEEKING_PositioningBitsMask;
    if (how == AM_SEEKING_AbsolutePositioning || how == AM_SEEKING_RelativePositioning) {
        if (!cur || !gm_valid(cur, 8)) {
            com_ret(c, E_POINTER);
            return;
        }
        uint64_t target = time_to_frames(read_u64(cur), t.s->hz);
        if (how == AM_SEEKING_RelativePositioning)
            target += current_frames(*t.s);
        uint64_t end = (uint64_t)duration_frames(*t.s);
        if (target > end)
            target = end;
        seek_playback(*t.s, target);
        // The position as set, for a caller that asked to see it.
        write_u64(cur, frames_to_time(target, t.s->hz));
    }
    com_ret(c, S_OK);
}

void SK_GetPositions(X86 *c) {
    GraphThis t = graph_this(c);
    if (!t.s) {
        com_ret(c, E_FAIL);
        return;
    }
    uint64_t dur = t.s->loaded ? frames_to_time((uint64_t)duration_frames(*t.s), t.s->hz) : 0;
    write_u64(arg(c, 1), t.s->loaded ? frames_to_time(current_frames(*t.s), t.s->hz) : 0);
    write_u64(arg(c, 2), dur);
    com_ret(c, S_OK);
}

void SK_GetAvailable(X86 *c) {
    GraphThis t = graph_this(c);
    if (!t.s) {
        com_ret(c, E_FAIL);
        return;
    }
    write_u64(arg(c, 1), 0);
    write_u64(arg(c, 2),
              t.s->loaded ? frames_to_time((uint64_t)duration_frames(*t.s), t.s->hz) : 0);
    com_ret(c, S_OK);
}

void SK_SetRate(X86 *c) {
    double rate = arg_double(c, 1);
    if (rate != 1.0)
        log_once("dshow.rate", "dshow: a playback rate of %g is not served; the stream plays at 1",
                 rate);
    com_ret(c, rate == 1.0 ? S_OK : E_INVALIDARG);
}

void SK_GetRate(X86 *c) {
    uint32_t out = arg(c, 1);
    if (!out || !gm_valid(out, 8)) {
        com_ret(c, E_POINTER);
        return;
    }
    write_double(out, 1.0);
    com_ret(c, S_OK);
}

void SK_GetPreroll(X86 *c) {
    uint32_t out = arg(c, 1);
    if (!out || !gm_valid(out, 8)) {
        com_ret(c, E_POINTER);
        return;
    }
    write_u64(out, 0);
    com_ret(c, S_OK);
}

const ComMethod g_mediaseeking[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"GetCapabilities", 2, SK_GetCapabilities},
    {"CheckCapabilities", 2, SK_CheckCapabilities},
    {"IsFormatSupported", 2, SK_IsFormatSupported},
    {"QueryPreferredFormat", 2, SK_QueryPreferredFormat},
    {"GetTimeFormat", 2, SK_QueryPreferredFormat},
    {"IsUsingTimeFormat", 2, SK_IsUsingTimeFormat},
    {"SetTimeFormat", 2, SK_SetTimeFormat},
    {"GetDuration", 2, SK_GetDuration},
    {"GetStopPosition", 2, SK_GetDuration},
    {"GetCurrentPosition", 2, SK_GetCurrentPosition},
    {"ConvertTimeFormat", 6, SK_ConvertTimeFormat},
    {"SetPositions", 5, SK_SetPositions},
    {"GetPositions", 3, SK_GetPositions},
    {"GetAvailable", 3, SK_GetAvailable},
    {"SetRate", 3, SK_SetRate},
    {"GetRate", 2, SK_GetRate},
    {"GetPreroll", 2, SK_GetPreroll},
};

// --- IBasicAudio: the level and balance, in hundredths of a decibel, the
// same units the host channel takes.
void BA_put_Volume(X86 *c) {
    GraphThis t = graph_this(c);
    int32_t v = (int32_t)arg(c, 1);
    if (!t.s) {
        com_ret(c, E_FAIL);
        return;
    }
    if (v > 0 || v < (int32_t)kVolumeMin) {
        com_ret(c, E_INVALIDARG);
        return;
    }
    t.s->volume = v;
    if (t.s->running && t.s->channel >= 0)
        host_audio_set_volume(t.s->channel, v);
    com_ret(c, S_OK);
}

void BA_get_Volume(X86 *c) {
    GraphThis t = graph_this(c);
    uint32_t out = arg(c, 1);
    if (!t.s || !out || !gm_valid(out, 4)) {
        com_ret(c, t.s ? E_POINTER : E_FAIL);
        return;
    }
    wr32(out, (uint32_t)t.s->volume);
    com_ret(c, S_OK);
}

void BA_put_Balance(X86 *c) {
    GraphThis t = graph_this(c);
    int32_t v = (int32_t)arg(c, 1);
    if (!t.s) {
        com_ret(c, E_FAIL);
        return;
    }
    if (v < -10000 || v > 10000) {
        com_ret(c, E_INVALIDARG);
        return;
    }
    t.s->balance = v;
    if (t.s->running && t.s->channel >= 0)
        host_audio_set_pan(t.s->channel, v);
    com_ret(c, S_OK);
}

void BA_get_Balance(X86 *c) {
    GraphThis t = graph_this(c);
    uint32_t out = arg(c, 1);
    if (!t.s || !out || !gm_valid(out, 4)) {
        com_ret(c, t.s ? E_POINTER : E_FAIL);
        return;
    }
    wr32(out, (uint32_t)t.s->balance);
    com_ret(c, S_OK);
}

const ComMethod g_basicaudio[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"GetTypeInfoCount", 2, DISP_GetTypeInfoCount},
    {"GetTypeInfo", 4, DISP_GetTypeInfo},
    {"GetIDsOfNames", 6, DISP_GetIDsOfNames},
    {"Invoke", 9, DISP_Invoke},
    {"put_Volume", 2, BA_put_Volume},
    {"get_Volume", 2, BA_get_Volume},
    {"put_Balance", 2, BA_put_Balance},
    {"get_Balance", 2, BA_get_Balance},
};

// --- IMediaPosition: the same positions in seconds, as doubles.
double seconds_of(Source &s, uint64_t frames) {
    return s.hz > 0 ? (double)frames / (double)s.hz : 0.0;
}

void MP_get_Duration(X86 *c) {
    GraphThis t = graph_this(c);
    uint32_t out = arg(c, 1);
    if (!t.s || !out || !gm_valid(out, 8)) {
        com_ret(c, t.s ? E_POINTER : E_FAIL);
        return;
    }
    if (!t.s->loaded) {
        com_ret(c, MS_E_NOSTREAM);
        return;
    }
    write_double(out, seconds_of(*t.s, (uint64_t)duration_frames(*t.s)));
    com_ret(c, S_OK);
}

void MP_put_CurrentPosition(X86 *c) {
    GraphThis t = graph_this(c);
    double secs = arg_double(c, 1);
    if (!t.s) {
        com_ret(c, E_FAIL);
        return;
    }
    if (!t.s->loaded) {
        com_ret(c, MS_E_NOSTREAM);
        return;
    }
    if (secs < 0)
        secs = 0;
    uint64_t target = (uint64_t)(secs * (double)t.s->hz + 0.5);
    uint64_t end = (uint64_t)duration_frames(*t.s);
    seek_playback(*t.s, target > end ? end : target);
    com_ret(c, S_OK);
}

void MP_get_CurrentPosition(X86 *c) {
    GraphThis t = graph_this(c);
    uint32_t out = arg(c, 1);
    if (!t.s || !out || !gm_valid(out, 8)) {
        com_ret(c, t.s ? E_POINTER : E_FAIL);
        return;
    }
    write_double(out, t.s->loaded ? seconds_of(*t.s, current_frames(*t.s)) : 0.0);
    com_ret(c, S_OK);
}

DX_STUB(MP_put_StopTime, S_OK)
DX_STUB(MP_put_PrefetchTime, S_OK)

void MP_get_PrefetchTime(X86 *c) {
    uint32_t out = arg(c, 1);
    if (!out || !gm_valid(out, 8)) {
        com_ret(c, E_POINTER);
        return;
    }
    write_double(out, 0.0);
    com_ret(c, S_OK);
}

void MP_put_Rate(X86 *c) {
    com_ret(c, arg_double(c, 1) == 1.0 ? S_OK : E_INVALIDARG);
}

void MP_get_Rate(X86 *c) {
    uint32_t out = arg(c, 1);
    if (!out || !gm_valid(out, 8)) {
        com_ret(c, E_POINTER);
        return;
    }
    write_double(out, 1.0);
    com_ret(c, S_OK);
}

void MP_CanSeek(X86 *c) {
    uint32_t out = arg(c, 1);
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, E_POINTER);
        return;
    }
    wr32(out, (uint32_t)OATRUE);
    com_ret(c, S_OK);
}

const ComMethod g_mediaposition[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"GetTypeInfoCount", 2, DISP_GetTypeInfoCount},
    {"GetTypeInfo", 4, DISP_GetTypeInfo},
    {"GetIDsOfNames", 6, DISP_GetIDsOfNames},
    {"Invoke", 9, DISP_Invoke},
    {"get_Duration", 2, MP_get_Duration},
    {"put_CurrentPosition", 3, MP_put_CurrentPosition},
    {"get_CurrentPosition", 2, MP_get_CurrentPosition},
    {"get_StopTime", 2, MP_get_Duration},
    {"put_StopTime", 3, MP_put_StopTime},
    {"get_PrefetchTime", 2, MP_get_PrefetchTime},
    {"put_PrefetchTime", 3, MP_put_PrefetchTime},
    {"put_Rate", 3, MP_put_Rate},
    {"get_Rate", 2, MP_get_Rate},
    {"CanSeekForward", 2, MP_CanSeek},
    {"CanSeekBackward", 2, MP_CanSeek},
};

// ===========================================================================
// Lifetime
// ===========================================================================
void mmstream_destroy(ComObj *mm) {
    auto it = sources().find(mm->id);
    if (it != sources().end()) {
        release_playback(it->second);
        sources().erase(it);
    }
    uint32_t children[2] = {mm->dsh_stream, mm->dsh_graph};
    mm->dsh_stream = mm->dsh_graph = 0;
    for (uint32_t id : children) {
        ComObj *o = id ? com_get(id) : nullptr;
        if (o && o->alive) {
            o->dsh_owner = 0;
            com_release(o);
        }
    }
}

void sample_destroy(ComObj *sp) {
    ComObj *ms = com_get(sp->dsh_owner);
    ComObj *ad = com_get(sp->dsh_data);
    sp->dsh_owner = sp->dsh_data = 0;
    if (ms && ms->alive)
        com_release(ms);
    if (ad && ad->alive)
        com_release(ad);
}

ComObj *mmstream_create() {
    return com_new(K_MMSTREAM);
}
ComObj *audiodata_create() {
    return com_new(K_AUDIODATA);
}

void movie_pin_destroy(ComObj *p) {
    movie_pins().erase(p->id);
}

void movie_filter_destroy(ComObj *f) {
    auto it = movie_filters().find(f->id);
    if (it != movie_filters().end()) {
        std::vector<uint32_t> pins = it->second.pins;
        it->second.pins.clear();
        movie_filters().erase(it);
        for (uint32_t id : pins) {
            ComObj *p = com_get(id);
            if (p && p->alive)
                com_release(p);
        }
    }
}

void movie_graph_destroy(ComObj *g) {
    auto it = movie_graphs().find(g->id);
    if (it == movie_graphs().end())
        return;
    std::vector<uint32_t> ours = it->second.filters;
    it->second.filters.clear();
    movie_graphs().erase(it);
    for (uint32_t id : ours) {
        ComObj *f = com_get(id);
        if (!f || !f->alive)
            continue;
        movie_filters()[id].graph = 0;
        com_release(f);
    }
}

} // namespace

void dshow_frame_pump(X86 *) {
    for (auto &entry : sources())
        pump(entry.second);
}

void dshow_register() {
    static bool done = false;
    if (done)
        return;
    done = true;

    com_define(IF_MMSTREAM, "AMSTREAM.dll", "IAMMultiMediaStream", g_mmstream,
               std::size(g_mmstream));
    com_define(IF_MEDIASTREAM, "AMSTREAM.dll", "IMediaStream", g_mediastream,
               std::size(g_mediastream));
    com_define(IF_AUDIOMEDIASTREAM, "AMSTREAM.dll", "IAudioMediaStream", g_audiomediastream,
               std::size(g_audiomediastream));
    com_define(IF_AUDIODATA, "AMSTREAM.dll", "IAudioData", g_audiodata, std::size(g_audiodata));
    com_define(IF_STREAMSAMPLE, "AMSTREAM.dll", "IAudioStreamSample", g_streamsample,
               std::size(g_streamsample));
    com_define(IF_GRAPH, "QUARTZ.dll", "IGraphBuilder", g_graph, std::size(g_graph));
    com_define(IF_MEDIACONTROL, "QUARTZ.dll", "IMediaControl", g_mediacontrol,
               std::size(g_mediacontrol));
    com_define(IF_MEDIAEVENT, "QUARTZ.dll", "IMediaEventEx", g_mediaevent, std::size(g_mediaevent));
    com_define(IF_MEDIASEEKING, "QUARTZ.dll", "IMediaSeeking", g_mediaseeking,
               std::size(g_mediaseeking));
    com_define(IF_BASICAUDIO, "QUARTZ.dll", "IBasicAudio", g_basicaudio, std::size(g_basicaudio));
    com_define(IF_MEDIAPOSITION, "QUARTZ.dll", "IMediaPosition", g_mediaposition,
               std::size(g_mediaposition));
    com_define(IF_ENUMFILTERS, "QUARTZ.dll", "IEnumFilters", g_enumfilters,
               std::size(g_enumfilters));
    com_define(IF_BASEFILTER, "QUARTZ.dll", "IBaseFilter", g_basefilter, std::size(g_basefilter));
    com_define(IF_PIN, "QUARTZ.dll", "IPin", g_pin, std::size(g_pin));
    com_define(IF_ENUMPINS, "QUARTZ.dll", "IEnumPins", g_enumpins, std::size(g_enumpins));
    com_define(IF_ENUMMEDIATETYPES, "QUARTZ.dll", "IEnumMediaTypes", g_enummediatypes,
               std::size(g_enummediatypes));

    com_bind(IF_MMSTREAM, K_MMSTREAM);
    com_bind(IF_MEDIASTREAM, K_MEDIASTREAM);
    com_bind(IF_AUDIOMEDIASTREAM, K_MEDIASTREAM);
    com_bind(IF_AUDIODATA, K_AUDIODATA);
    com_bind(IF_STREAMSAMPLE, K_STREAMSAMPLE);
    com_bind(IF_GRAPH, K_GRAPH);
    com_bind(IF_MEDIACONTROL, K_GRAPH);
    com_bind(IF_MEDIAEVENT, K_GRAPH);
    com_bind(IF_MEDIASEEKING, K_GRAPH);
    com_bind(IF_BASICAUDIO, K_GRAPH);
    com_bind(IF_MEDIAPOSITION, K_GRAPH);
    com_bind(IF_ENUMFILTERS, K_ENUMFILTERS);
    // The movie graph shares IGraphBuilder with the multimedia stream, and
    // adds the filter graph's own interfaces.
    com_bind(IF_GRAPH, K_FILTERGRAPH);
    com_bind(IF_BASEFILTER, K_BASEFILTER);
    com_bind(IF_PIN, K_PIN);
    com_bind(IF_ENUMPINS, K_ENUMPINS);
    com_bind(IF_ENUMMEDIATETYPES, K_ENUMMEDIATETYPES);

    com_register_iid(IF_MMSTREAM, IID_IAMMultiMediaStream_);
    com_register_iid(IF_MMSTREAM, IID_IMultiMediaStream_);
    com_register_iid(IF_MEDIASTREAM, IID_IMediaStream_);
    com_register_iid(IF_AUDIOMEDIASTREAM, IID_IAudioMediaStream_);
    com_register_iid(IF_AUDIODATA, IID_IAudioData_);
    com_register_iid(IF_AUDIODATA, IID_IMemoryData_);
    com_register_iid(IF_STREAMSAMPLE, IID_IAudioStreamSample_);
    com_register_iid(IF_STREAMSAMPLE, IID_IStreamSample_);
    com_register_iid(IF_GRAPH, IID_IGraphBuilder_);
    com_register_iid(IF_GRAPH, IID_IFilterGraph_);
    com_register_iid(IF_MEDIACONTROL, IID_IMediaControl_);
    com_register_iid(IF_MEDIAEVENT, IID_IMediaEventEx_);
    com_register_iid(IF_MEDIAEVENT, IID_IMediaEvent_);
    com_register_iid(IF_MEDIASEEKING, IID_IMediaSeeking_);
    com_register_iid(IF_BASICAUDIO, IID_IBasicAudio_);
    com_register_iid(IF_MEDIAPOSITION, IID_IMediaPosition_);
    com_register_iid(IF_ENUMFILTERS, IID_IEnumFilters_);
    com_register_iid(IF_BASEFILTER, IID_IBaseFilter_);
    com_register_iid(IF_PIN, IID_IPin_);
    com_register_iid(IF_ENUMPINS, IID_IEnumPins_);
    com_register_iid(IF_ENUMMEDIATETYPES, IID_IEnumMediaTypes_);

    com_set_destructor(K_MMSTREAM, mmstream_destroy);
    com_set_destructor(K_STREAMSAMPLE, sample_destroy);
    com_set_destructor(K_FILTERGRAPH, movie_graph_destroy);
    com_set_destructor(K_BASEFILTER, movie_filter_destroy);
    com_set_destructor(K_PIN, movie_pin_destroy);

    com_register_class(CLSID_AMMultiMediaStream_, "AMMultiMediaStream", IF_MMSTREAM,
                       mmstream_create);
    com_register_class(CLSID_AMAudioData_, "AMAudioData", IF_AUDIODATA, audiodata_create);
    com_register_class(CLSID_FilterGraph_, "FilterGraph", IF_GRAPH, movie_graph_create);
    com_register_class(CLSID_AviSplitter_, "AVI Splitter", IF_BASEFILTER, movie_splitter_create);
    com_register_class(CLSID_MPEG1Splitter_, "MPEG-1 Splitter", IF_BASEFILTER,
                       movie_splitter_create);
    com_register_class(CLSID_DSoundRender_, "DSound Renderer", IF_BASEFILTER, movie_dsound_create);
}

void dshow_reset() {
    // The guest heap the rings and events lived in is gone with mem_init;
    // the host channels are released by the audio reset.
    sources().clear();
    movie_pins().clear();
    movie_filters().clear();
    movie_graphs().clear();
}
