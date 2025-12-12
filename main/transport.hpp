#pragma once
#include <string>

namespace transport {

// kind = "read" or "write"
std::string post_frame(const std::string& kind,
                       const std::string& base_url,
                       const std::string& api_key_b64,
                       const std::string& frame_hex);

// FOTA: GET a chunk independently (returns JSON with "data" field or error)
std::string get_fota_chunk(const std::string& base_url,
                           const std::string& device_id,
                           uint32_t chunk_number);

// Get number of connection/perform failures observed since boot
uint32_t get_conn_failures();

// Configure retry policy used by post_frame: retries, base_backoff_ms, max_backoff_ms
void set_retry_policy(uint8_t retries, uint32_t base_backoff_ms, uint32_t max_backoff_ms);

} // namespace transport
