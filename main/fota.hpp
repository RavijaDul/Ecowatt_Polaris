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
uint32_t get_next_chunk_for_cloud();
void init();
bool start(const Manifest& m);
bool ingest_chunk(uint32_t number, const std::string& b64);
bool finalize_and_apply(bool& ok_verify, bool& ok_apply);
std::string status_json();

} // namespace fota
