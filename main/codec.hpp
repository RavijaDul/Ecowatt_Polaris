#pragma once                                  // L1: single-inclusion guard.

#include <cstdint>                            // L3: integer types.
#include <string>                             // L4: std::string for blobs.
#include <vector>                             // L5: std::vector for batches.
#include "buffer.hpp"                         // L6: for buffer::Record.
#include "acquisition.hpp"                    // L7: for acquisition::Sample.

namespace codec {                             // L9: begin codec namespace.

// CRC32 helper
uint32_t crc32_ieee(const void* data, size_t len); // L12: calculate CRC-32 of buffer.

// Encode a batch of records into delta+RLE compressed binary blob.
// Returns binary string and fills 'order' with field names used.
std::string encode_delta_rle_v1(const std::vector<buffer::Record>& recs,
                                std::vector<std::string>& order); // L16–L17

// Decode delta+RLE blob back into samples.
// Returns true on success, false on CRC or format error.
bool decode_delta_rle_v1(const std::string& blob,
                         std::vector<acquisition::Sample>& out_samples,
                         std::vector<std::string>* out_order=nullptr); // L21–L23

} // namespace codec                           // L25
