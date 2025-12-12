#pragma once
#include <string>
#include <cstdint>

namespace fota {

struct Manifest {
  std::string version;
  uint32_t size = 0;
  std::string hash_hex;
  uint32_t chunk_size = 1024;
};

// FOTA status codes for device → cloud reporting
enum class FotaStatus : uint8_t {
  IDLE = 0,           // no active session
  DOWNLOADING = 1,    // chunks being received
  VERIFY_OK = 2,      // hash verified successfully
  VERIFY_FAILED = 3,  // hash mismatch (corruption)
  BOOT_OK = 4,        // image booted and marked valid
  BOOT_ROLLBACK = 5   // image failed to boot, rolled back
};

uint32_t get_next_chunk_for_cloud();
uint32_t get_last_acked_chunk();
bool is_session_active();
Manifest get_current_manifest();
void init();
bool start(const Manifest& m);
bool ingest_chunk(uint32_t number, const std::string& b64);
bool finalize_and_apply(bool& ok_verify, bool& ok_apply);
std::string status_json();
FotaStatus get_current_status();
std::string get_failed_version();

} // namespace fota
