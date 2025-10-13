#pragma once
#include <string>
#include <vector>
#include "buffer.hpp"

namespace uplink {

struct Payload {
  std::string json;
  std::size_t raw_bytes = 0;
};

Payload build_payload(const std::vector<buffer::Record>& batch, const std::string& device_id);

// existing
bool post_payload(const std::string& cloud_base_url, const std::string& api_key_b64, const std::string& json_body);

// NEW: like above, but returns cloud reply body
bool post_payload_and_get_reply(const std::string& cloud_base_url,
                                const std::string& api_key_b64,
                                const std::string& json_body,
                                std::string& out_reply_body);

} // namespace uplink
