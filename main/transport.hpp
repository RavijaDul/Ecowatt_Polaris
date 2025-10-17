#pragma once
#include <string>

namespace transport {

// kind = "read" or "write"
std::string post_frame(const std::string& kind,
                       const std::string& base_url,
                       const std::string& api_key_b64,
                       const std::string& frame_hex);

} // namespace transport
