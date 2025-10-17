#pragma once
#include <string>
#include <optional>
#include <cstdint>

namespace security {
std::string wrap_json_with_hmac(const std::string& payload_json,
                                const std::string& psk,
                                uint64_t next_device_nonce);

std::optional<std::string> unwrap_and_verify_envelope(const std::string& env_json,
                                                      const std::string& psk,
                                                      uint64_t& last_seen_nonce_io,
                                                      bool treat_payload_as_base64);
} // namespace security
