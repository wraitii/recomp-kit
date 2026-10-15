#include "wav_source.h"
#include "riff.h"
#include "../runtime/imports.h"

#ifdef RECOMP_HAVE_FFMPEG
extern "C" {
#include <libavcodec/avcodec.h>
}
#endif

#include <algorithm>
#include <cstring>
#include <utility>

struct AdpcmState {
#ifdef RECOMP_HAVE_FFMPEG
    AVCodecContext *dec = nullptr;
    AVFrame *frame = nullptr;
    AVPacket *packet = nullptr;
    uint32_t offset = 0;
    bool flushed = false;

    ~AdpcmState() {
        av_frame_free(&frame);
        av_packet_free(&packet);
        avcodec_free_context(&dec);
    }
#endif
};

WavSource::WavSource() = default;
WavSource::~WavSource() = default;
WavSource::WavSource(WavSource &&) noexcept = default;
WavSource &WavSource::operator=(WavSource &&) noexcept = default;

bool WavSource::open_adpcm() {
#ifdef RECOMP_HAVE_FFMPEG
    const AVCodec *codec = avcodec_find_decoder(AV_CODEC_ID_ADPCM_IMA_WAV);
    if (!codec) {
        LOGW("wav_source: IMA ADPCM decoder is unavailable");
        return false;
    }
    auto st = std::make_unique<AdpcmState>();
    st->dec = avcodec_alloc_context3(codec);
    if (!st->dec) {
        LOGW("wav_source: IMA ADPCM context allocation failed");
        return false;
    }
    st->dec->sample_rate = (int)rate_;
    st->dec->block_align = (int)block_;
    st->dec->bits_per_coded_sample = 4;
    av_channel_layout_default(&st->dec->ch_layout, (int)channels_);
    if (avcodec_open2(st->dec, codec, nullptr) < 0) {
        LOGW("wav_source: IMA ADPCM decoder open failed");
        return false;
    }
    st->frame = av_frame_alloc();
    st->packet = av_packet_alloc();
    if (!st->frame || !st->packet) {
        LOGW("wav_source: IMA ADPCM frame allocation failed");
        return false;
    }
    adpcm_ = std::move(st);
    return true;
#else
    LOGW("wav_source: IMA ADPCM needs a build with FFmpeg");
    return false;
#endif
}

bool WavSource::open(const std::vector<uint8_t> &bytes, uint32_t max_pcm_bytes) {
    bytes_ = bytes;
    adpcm_.reset();
    data_off_ = 0;
    data_bytes_ = 0;
    rate_ = channels_ = 0;
    format_ = bits_ = 0;
    block_ = fact_frames_ = 0;
    max_pcm_bytes_ = max_pcm_bytes;
    decoded_samples_ = 0;
    position_ = 0;
    drained_ = true;
    failed_ = false;
    WavImage image{};
    if (!riff_parse_wave_host(bytes_.data(), bytes_.size(), &image))
        return false;
    data_off_ = (size_t)(image.data - bytes_.data());
    data_bytes_ = image.bytes;
    rate_ = image.rate;
    channels_ = image.channels;
    format_ = image.format;
    bits_ = image.bits;
    block_ = image.block;
    fact_frames_ = image.frames;
    if (format_ != 1) {
        if (format_ != 0x11 || block_ < 4 * channels_ || block_ % (4 * channels_) || bits_ != 4)
            return false;
        if (!open_adpcm())
            return false;
    }
    drained_ = false;
    return true;
}

bool WavSource::decode_pcm(std::vector<int16_t> &out) {
    uint32_t frame_bytes = (uint32_t)channels_ * (bits_ / 8u);
    if (!frame_bytes) {
        drained_ = true;
        return false;
    }
    uint64_t total = data_bytes_ / frame_bytes;
    if (position_ >= total) {
        drained_ = true;
        return false;
    }
    uint32_t frames = 4096;
    if ((uint64_t)frames > total - position_)
        frames = (uint32_t)(total - position_);
    size_t begin = (size_t)position_ * frame_bytes;
    out.resize((size_t)frames * channels_);
    const uint8_t *src = bytes_.data() + data_off_ + begin;
    if (bits_ == 8) {
        for (size_t i = 0; i < out.size(); ++i)
            out[i] = (int16_t)(((int32_t)src[i] - 128) * 256);
    } else {
        memcpy(out.data(), src, out.size() * 2);
    }
    position_ += frames;
    if (position_ >= total)
        drained_ = true;
    return true;
}

bool WavSource::decode_adpcm(std::vector<int16_t> &out) {
#ifdef RECOMP_HAVE_FFMPEG
    if (!adpcm_ || failed_) {
        drained_ = true;
        return false;
    }
    AdpcmState &st = *adpcm_;
    if (fact_frames_ && decoded_samples_ >= (uint64_t)fact_frames_ * channels_) {
        drained_ = true;
        return false;
    }
    for (;;) {
        if (st.offset < data_bytes_) {
            uint32_t bytes = std::min(data_bytes_ - st.offset, block_);
            if (bytes < 4 * channels_ || bytes % (4 * channels_)) {
                LOGW("wav_source: IMA ADPCM invalid block length %u for %u channels", bytes,
                     channels_);
                failed_ = true;
                drained_ = true;
                return false;
            }
            av_packet_unref(st.packet);
            if (av_new_packet(st.packet, (int)bytes) < 0) {
                LOGW("wav_source: IMA ADPCM packet allocation failed");
                failed_ = true;
                drained_ = true;
                return false;
            }
            memcpy(st.packet->data, bytes_.data() + data_off_ + st.offset, bytes);
            st.offset += bytes;
            if (avcodec_send_packet(st.dec, st.packet) < 0) {
                LOGW("wav_source: IMA ADPCM send failed at byte %u", st.offset);
                failed_ = true;
                drained_ = true;
                return false;
            }
        } else if (!st.flushed) {
            st.flushed = true;
            if (avcodec_send_packet(st.dec, nullptr) < 0) {
                LOGW("wav_source: IMA ADPCM flush failed");
                failed_ = true;
                drained_ = true;
                return false;
            }
        } else {
            drained_ = true;
            return false;
        }
        int rc = avcodec_receive_frame(st.dec, st.frame);
        if (rc == AVERROR(EAGAIN))
            continue;
        if (rc < 0) {
            if (rc != AVERROR_EOF) {
                LOGW("wav_source: IMA ADPCM decode error %d at byte %u", rc, st.offset);
                failed_ = true;
            }
            drained_ = true;
            return false;
        }
        if (st.frame->format != AV_SAMPLE_FMT_S16 && st.frame->format != AV_SAMPLE_FMT_S16P) {
            LOGW("wav_source: IMA ADPCM frame format %d is not 16-bit", st.frame->format);
            failed_ = true;
            av_frame_unref(st.frame);
            drained_ = true;
            return false;
        }
        int frame_samples = st.frame->nb_samples;
        size_t samples = (size_t)frame_samples * channels_;
        uint64_t limit = fact_frames_ ? (uint64_t)fact_frames_ * channels_ : 0;
        if (limit && decoded_samples_ + samples > limit)
            samples = decoded_samples_ < limit ? (size_t)(limit - decoded_samples_) : 0;
        if (max_pcm_bytes_ && (decoded_samples_ + samples) * 2 > max_pcm_bytes_) {
            LOGW("wav_source: IMA ADPCM decoded PCM exceeds %u bytes", max_pcm_bytes_);
            failed_ = true;
            av_frame_unref(st.frame);
            drained_ = true;
            return false;
        }
        out.resize(samples);
        if (samples && st.frame->format == AV_SAMPLE_FMT_S16) {
            memcpy(out.data(), st.frame->extended_data[0], samples * 2);
        } else if (samples) {
            for (int i = 0; i < frame_samples && (size_t)i * channels_ < samples; ++i)
                for (uint32_t ch = 0; ch < channels_ && (size_t)i * channels_ + ch < samples; ++ch)
                    memcpy(out.data() + ((size_t)i * channels_ + ch),
                           st.frame->extended_data[ch] + (size_t)i * 2, 2);
        }
        decoded_samples_ += samples;
        position_ += samples / channels_;
        av_frame_unref(st.frame);
        if (!samples) {
            drained_ = true;
            return false;
        }
        if (limit && decoded_samples_ >= limit)
            drained_ = true;
        return true;
    }
#else
    (void)out;
    drained_ = true;
    return false;
#endif
}

bool WavSource::decode_next(std::vector<int16_t> &out) {
    out.clear();
    if (drained_)
        return false;
    if (format_ == 1)
        return decode_pcm(out);
    return decode_adpcm(out);
}

void WavSource::seek_frames(uint64_t target) {
    if (format_ == 1) {
        uint32_t frame_bytes = (uint32_t)channels_ * (bits_ / 8u);
        uint64_t total = frame_bytes ? data_bytes_ / frame_bytes : 0;
        position_ = target > total ? total : target;
        drained_ = position_ >= total;
        return;
    }
    if (!adpcm_) {
        position_ = 0;
        drained_ = true;
        return;
    }
    adpcm_.reset();
    decoded_samples_ = 0;
    position_ = 0;
    failed_ = false;
    if (!open_adpcm()) {
        drained_ = true;
        return;
    }
    drained_ = false;
    while (position_ < target && !drained_) {
        std::vector<int16_t> discard;
        decode_next(discard);
    }
}
