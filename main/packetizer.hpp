#pragma once                                      // L1: Include guard.

#include <cstddef>                                // L3: size_t for byte counts.
#include <string>                                 // L4: std::string for JSON and IDs.
#include <vector>                                 // L5: std::vector for batches.
#include "buffer.hpp"                             // L6: buffer::Record used as input to the packetizer.

namespace uplink {                                // L8: Packetization + cloud upload helpers live here.

// Result of building an upload payload ready for POST.
struct Payload {                                  // L11: Small struct returned to caller.
    std::string json;                             // L12: Serialized JSON body to send to the cloud.
    std::size_t raw_bytes;                        // L13: Size of compressed block (or original) for logging.
};

// Build a JSON payload from a batch of records.
// Expected to include: device_id, ts_start/ts_end, codec, order, block_b64, etc.
Payload build_payload(const std::vector<buffer::Record>& batch,  // L18
                      const std::string& device_id);              // L19

// POST the payload JSON to the cloud base URL (e.g., "http://host:5000").
// Returns true on HTTP 200 OK, false otherwise.
bool post_payload(const std::string& cloud_base_url,             // L23
                  const std::string& api_key_b64,                // L24
                  const std::string& json_body);                 // L25

} // namespace uplink                               // L27
