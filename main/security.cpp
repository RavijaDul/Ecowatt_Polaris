#include "security.hpp"
#include <mbedtls/md.h>
#include <mbedtls/base64.h>
#include <cctype>
#include <string>

namespace {

static std::string json_get_str(const std::string& s, const char* key) {
  std::string k = std::string("\"") + key + "\"";
  auto p = s.find(k); if (p == std::string::npos) return {};
  p = s.find(':', p); if (p == std::string::npos) return {};
  p = s.find('"', p); if (p == std::string::npos) return {};
  size_t start = p + 1; size_t end = s.find('"', start);
  if (end == std::string::npos) return {};
  return s.substr(start, end - start);
}
static bool json_get_u64(const std::string& s, const char* key, uint64_t& out) {
  std::string k = std::string("\"") + key + "\"";
  auto p = s.find(k); if (p == std::string::npos) return false;
  p = s.find(':', p); if (p == std::string::npos) return false;
  size_t start = p + 1;
  while (start < s.size() && std::isspace((unsigned char)s[start])) ++start;
  size_t end = start;
  while (end < s.size() && std::isdigit((unsigned char)s[end])) ++end;
  if (end == start) return false;
  out = std::strtoull(s.substr(start, end-start).c_str(), nullptr, 10);
  return true;
}
static std::string to_hex(const unsigned char* buf, size_t n){
  static const char* H="0123456789abcdef";
  std::string s; s.resize(n*2);
  for(size_t i=0;i<n;++i){ s[2*i]=H[(buf[i]>>4)&0xF]; s[2*i+1]=H[buf[i]&0xF]; }
  return s;
}
static bool eq_ci_hex(const std::string& a, const std::string& b){
  if(a.size()!=b.size()) return false;
  for(size_t i=0;i<a.size();++i){
    char x=std::tolower((unsigned char)a[i]);
    char y=std::tolower((unsigned char)b[i]);
    if(x!=y) return false;
  }
  return true;
}
static bool hmac_sha256(const std::string& key, const std::string& msg, std::string& mac_hex) {
  unsigned char h[32];
  const mbedtls_md_info_t* info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (!info) return false;
  if (mbedtls_md_hmac(info, (const unsigned char*)key.data(), key.size(),
                      (const unsigned char*)msg.data(), msg.size(), h) != 0) return false;
  mac_hex = to_hex(h, sizeof(h));
  return true;
}
static std::string b64_encode(const std::string& bin){
  size_t out_len=0; mbedtls_base64_encode(nullptr,0,&out_len,(const unsigned char*)bin.data(),bin.size());
  std::string out; out.resize(out_len);
  if(mbedtls_base64_encode((unsigned char*)out.data(), out_len, &out_len,
                           (const unsigned char*)bin.data(), bin.size())==0){ out.resize(out_len); return out; }
  return {};
}
static std::string b64_decode(const std::string& s){
  size_t out_len=0; mbedtls_base64_decode(nullptr,0,&out_len,(const unsigned char*)s.data(),s.size());
  std::string out; out.resize(out_len);
  if(mbedtls_base64_decode((unsigned char*)out.data(), out_len, &out_len,
                           (const unsigned char*)s.data(), s.size())==0){ out.resize(out_len); return out; }
  return {};
}

} // anon

namespace security {

std::string wrap_json_with_hmac(const std::string& payload_json,
                                const std::string& psk,
                                uint64_t next_device_nonce)
{
  std::string p_b64 = b64_encode(payload_json);
  char buf[64]; std::snprintf(buf, sizeof(buf), "%llu", (unsigned long long)next_device_nonce);
  std::string msg = std::string(buf) + "." + p_b64;
  std::string mac; (void)hmac_sha256(psk, msg, mac);
  std::string out = "{\"nonce\":"; out += buf;
  out += ",\"payload\":\""; out += p_b64; out += "\",\"mac\":\""; out += mac; out += "\"}";
  return out;
}

std::optional<std::string> unwrap_and_verify_envelope(const std::string& env_json,
                                                      const std::string& psk,
                                                      uint64_t& last_seen_nonce_io,
                                                      bool treat_payload_as_base64)
{
  uint64_t nonce=0; if(!json_get_u64(env_json,"nonce",nonce)) return std::nullopt;
  std::string payload = json_get_str(env_json,"payload");
  std::string mac_hex = json_get_str(env_json,"mac");
  if(payload.empty() || mac_hex.empty()) return std::nullopt;

  char nbuf[64]; std::snprintf(nbuf,sizeof(nbuf), "%llu", (unsigned long long)nonce);
  std::string msg = std::string(nbuf) + "." + payload;

  std::string calc; if(!hmac_sha256(psk, msg, calc)) return std::nullopt;
  if(!eq_ci_hex(calc, mac_hex)) return std::nullopt;

  if (nonce <= last_seen_nonce_io) return std::nullopt;  // anti-replay
  last_seen_nonce_io = nonce;

  if (treat_payload_as_base64) {
    auto bin = b64_decode(payload);
    if (bin.empty()) return std::nullopt;
    return std::optional<std::string>(bin);
  }
  return std::optional<std::string>(payload);
}

} // namespace security
