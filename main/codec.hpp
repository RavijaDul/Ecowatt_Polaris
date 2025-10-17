#pragma once
#include <cstddef>
#include <string>
#include <vector>
#include "buffer.hpp"
#include "acquisition.hpp"

namespace codec {
uint32_t crc32_ieee(const void* data, size_t len);

std::string encode_delta_rle_v1(const std::vector<buffer::Record>& recs, std::vector<std::string>& order);
bool decode_delta_rle_v1(const std::string& blob, std::vector<acquisition::Sample>& out_samples, std::vector<std::string>* out_order = nullptr);

struct BenchResult {
  std::string method;
  size_t n_samples = 0;
  size_t orig_bytes = 0;
  size_t comp_bytes = 0;
  double encode_ms = 0.0;
  bool lossless_ok = false;
};

codec::BenchResult run_benchmark_delta_rle_v1(const std::vector<buffer::Record>& recs);

} // namespace codec
