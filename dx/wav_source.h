#pragma once
#include <stdint.h>
#include <stddef.h>
#include <memory>
#include <vector>

struct AdpcmState;

struct WavSource {
    WavSource();
    ~WavSource();
    WavSource(WavSource &&) noexcept;
    WavSource &operator=(WavSource &&) noexcept;
    WavSource(const WavSource &) = delete;
    WavSource &operator=(const WavSource &) = delete;

    bool open(const std::vector<uint8_t> &bytes, uint32_t max_pcm_bytes = 0);
    bool decode_next(std::vector<int16_t> &out);
    uint32_t rate() const {
        return rate_;
    }
    uint32_t channels() const {
        return channels_;
    }
    void seek_frames(uint64_t target);
    bool drained() const {
        return drained_;
    }
    bool failed() const {
        return failed_;
    }
    uint64_t position_frames() const {
        return position_;
    }

  private:
    bool decode_pcm(std::vector<int16_t> &out);
    bool decode_adpcm(std::vector<int16_t> &out);
    bool open_adpcm();

    std::vector<uint8_t> bytes_;
    std::unique_ptr<AdpcmState> adpcm_;
    size_t data_off_ = 0;
    uint32_t data_bytes_ = 0;
    uint32_t rate_ = 0, channels_ = 0;
    uint32_t block_ = 0, fact_frames_ = 0, max_pcm_bytes_ = 0;
    uint64_t decoded_samples_ = 0, position_ = 0;
    uint16_t format_ = 0, bits_ = 0;
    bool drained_ = true, failed_ = false;
};
