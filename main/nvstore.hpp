#pragma once
#include <cstdint>
#include <string>

namespace nvstore {
void init();
bool get_u64(const char* ns, const char* key, uint64_t& out);
void set_u64(const char* ns, const char* key, uint64_t v);
bool get_str(const char* ns, const char* key, std::string& out);
void set_str(const char* ns, const char* key, const std::string& v);
}
