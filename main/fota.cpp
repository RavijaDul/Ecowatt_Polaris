// fota.cpp — REAL OTA (ESP-IDF)

#include "fota.hpp"
#include "nvstore.hpp"
#include <array> 
#include <memory>
#include <cstring> 
#include <algorithm>
#include <esp_ota_ops.h>
#include <esp_system.h>
#include <esp_log.h>
#include <string>
#include <vector>
#include <algorithm>
#include <mbedtls/sha256.h>
#include <mbedtls/base64.h>

namespace {

// ---------- Persistent keys (NVS) ----------
static constexpr const char* NS   = "fota";
static constexpr const char* K_VER= "mf.ver";
static constexpr const char* K_SZ = "mf.size";
static constexpr const char* K_HASH="mf.hash";
static constexpr const char* K_WR = "bytes_written";
static constexpr const char* K_NEXT="next_chunk";
// Global C-linkage progress callback implemented in main.cpp
extern "C" void fota_progress_notify(uint32_t written, uint32_t total);

// ---------- State ----------
struct State {
  bool        session_active = false;
  fota::Manifest mf{};
  esp_ota_handle_t ota = 0;
  const esp_partition_t* part = nullptr;
  uint32_t   bytes_written = 0;
  uint32_t   next_chunk = 0;             // expected chunk_number (optional)
  bool       finalize_requested = false;  // true once size reached
  bool       finalized = false;

  // Hash (streaming)
  mbedtls_sha256_context sha;
  bool sha_init = false;

  // Progress strings
  std::string last_error;
} S;

static const char* TAG = "fota";

static std::string b64dec(const std::string& s){
  size_t out_len=0;
  mbedtls_base64_decode(nullptr,0,&out_len,(const unsigned char*)s.data(),s.size());
  std::string out; out.resize(out_len);
  if(mbedtls_base64_decode((unsigned char*)out.data(), out_len, &out_len,
                           (const unsigned char*)s.data(), s.size())==0){
    out.resize(out_len); return out;
  }
  return {};
}

static std::string sha256_hex_finish(){
  unsigned char h[32];
  mbedtls_sha256_finish(&S.sha, h);
  static const char* HEX="0123456789abcdef";
  std::string s; s.resize(64);
  for(int i=0;i<32;++i){ s[2*i]=HEX[(h[i]>>4)&0xF]; s[2*i+1]=HEX[h[i]&0xF]; }
  return s;
}

static inline std::string lower(std::string x){
  std::transform(x.begin(), x.end(), x.begin(), [](unsigned char c){ return (char)std::tolower(c);});
  return x;
}

static bool eq_hex_ci(const std::string& a, const std::string& b){
  return lower(a) == lower(b);
}

} // anon

uint32_t fota::get_next_chunk_for_cloud(){
  // No locking needed if only called from the same task; add a mutex if you later use multi-task access.
  return S.session_active ? S.next_chunk : 0u;
}

namespace fota {

void init(){
  nvstore::init(); // ensure NVS ready
}

// Begin a (possibly resumable) OTA session
bool start(const Manifest& m){
  // If we already have an identical manifest persisted and OTA not finalized, resume.
  std::string old_ver, old_hash;
  uint64_t sz=0, wr=0, next=0;
  nvstore::get_str(NS, K_VER, old_ver);
  nvstore::get_str(NS, K_HASH, old_hash);
  nvstore::get_u64(NS, K_SZ, sz);
  nvstore::get_u64(NS, K_WR, wr);
  nvstore::get_u64(NS, K_NEXT, next);

  bool can_resume = (old_ver == m.version) && (old_hash == m.hash_hex) && (sz == m.size) && (wr < sz);

  // If the same manifest arrives while a session is already active, ignore it.
  // This prevents resetting S.next_chunk to 0 when the server repeats the manifest.
  if (S.session_active) {
    if (S.mf.version  == m.version &&
        S.mf.hash_hex == m.hash_hex &&
        S.mf.size     == m.size) {
      ESP_LOGI(TAG, "FOTA start: duplicate manifest — ignoring (next_chunk=%u, written=%u)",
               (unsigned)S.next_chunk, (unsigned)S.bytes_written);
      return true;  // keep current session & progress
    }
  }

  // If a different manifest arrives while a session is active, cleanly abort previous context.
  if (S.session_active && !can_resume) {
    if (S.ota != 0) { esp_ota_end(S.ota); }
    S = State{}; // reset
  }


  // Initialize state
  S.session_active = true;
  S.mf = m;
  S.last_error.clear();
  S.finalize_requested = false;
  S.finalized = false;

  // Pick target partition
  S.part = esp_ota_get_next_update_partition(nullptr);
  if (!S.part) {
    S.last_error = "no-update-partition";
    ESP_LOGE(TAG, "No OTA update partition found");
    return false;
  }

  // Begin OTA (with resume support)
  esp_err_t err = ESP_OK;
  if (can_resume && wr > 0) {
    // Resume: we still must call esp_ota_begin; IDF allows continuing writes.
    err = esp_ota_begin(S.part, S.mf.size, &S.ota);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "esp_ota_begin(resume) failed: %s", esp_err_to_name(err));
      S.last_error = "ota-begin-resume";
      return false;
    }

    // ---- Sanity clamp resume values ----
    if (wr > m.size) wr = m.size;
    uint64_t chunks_total = (m.size + m.chunk_size - 1) / m.chunk_size;
    if (next > chunks_total) next = 0;  // force fresh if nonsense

    // ---- REBUILD rolling SHA over already-written bytes (use small heap buffer) ----
    mbedtls_sha256_init(&S.sha);
    mbedtls_sha256_starts(&S.sha, 0);
    S.sha_init = true;

    const size_t CHUNK = 1024; // keep small to reduce memory pressure
    std::unique_ptr<uint8_t[]> tmp(new uint8_t[CHUNK]);
    uint64_t off = 0;
    while (off < wr) {
      size_t to_read = (size_t)std::min<uint64_t>(CHUNK, wr - off);
      esp_err_t er = esp_partition_read(S.part, off, tmp.get(), to_read);
      if (er != ESP_OK) {
        ESP_LOGE(TAG, "resume: esp_partition_read failed @%" PRIu64 " (%s)", off, esp_err_to_name(er));
        S.last_error = "resume-read";
        return false;
      }
      mbedtls_sha256_update(&S.sha, tmp.get(), to_read);
      off += to_read;
    }

    S.bytes_written = (uint32_t)wr;
    S.next_chunk    = (uint32_t)next;

    ESP_LOGW(TAG, "FOTA resume: version=%s written=%u next_chunk=%u (SHA rebuilt)",
            m.version.c_str(), (unsigned)S.bytes_written, (unsigned)S.next_chunk);
  }
 else {
    // Fresh session
    err = esp_ota_begin(S.part, S.mf.size, &S.ota);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "esp_ota_begin failed: %s", esp_err_to_name(err));
      S.last_error = "ota-begin";
      return false;
    }
    S.bytes_written = 0;
    S.next_chunk    = 0;
    mbedtls_sha256_init(&S.sha); mbedtls_sha256_starts(&S.sha, 0); S.sha_init = true;

    // Persist manifest & progress for resume
    nvstore::set_str(NS, K_VER,  S.mf.version);
    nvstore::set_str(NS, K_HASH, S.mf.hash_hex);
    nvstore::set_u64(NS, K_SZ,   S.mf.size);
    nvstore::set_u64(NS, K_WR,   0);
    nvstore::set_u64(NS, K_NEXT, 0);
  }

  ESP_LOGI(TAG, "FOTA start: version=%s size=%u chunk=%u",
           m.version.c_str(), (unsigned)m.size, (unsigned)m.chunk_size);
  return true;
}

// Accept a chunk (any time during the session)
bool ingest_chunk(uint32_t number, const std::string& b64){
  if (!S.session_active || !S.sha_init || S.finalized) return false;


  // Enforce strict order
  if (number != S.next_chunk) {
    ESP_LOGW(TAG, "Reject chunk #%u (expecting #%u).",
            (unsigned)number, (unsigned)S.next_chunk);
    S.last_error = "out-of-order";
    return false;  // <- fail hard so server can resend the right one
  }
  auto bin = b64dec(b64);
  if (bin.empty()) {
    ESP_LOGE(TAG, "Base64 decode failed at chunk #%u", (unsigned)number);
    S.last_error = "bad-b64";
    return false;
  }

  // Bounds check
  if (S.bytes_written + bin.size() > S.mf.size) {
    ESP_LOGE(TAG, "Chunk overflow: written=%u + %u > total=%u",
             (unsigned)S.bytes_written, (unsigned)bin.size(), (unsigned)S.mf.size);
    S.last_error = "overflow";
    return false;
  }

  // Write to OTA partition
  esp_err_t err = esp_ota_write(S.ota, bin.data(), bin.size());
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "esp_ota_write failed: %s", esp_err_to_name(err));
    S.last_error = "ota-write";
    return false;
  }

  // Update rolling hash and counters
  mbedtls_sha256_update(&S.sha, (const unsigned char*)bin.data(), bin.size());
  S.bytes_written += (uint32_t)bin.size();
  S.next_chunk = number + 1;

  // Persist resume progress
  nvstore::set_u64(NS, K_WR,   S.bytes_written);
  nvstore::set_u64(NS, K_NEXT, S.next_chunk);

  ESP_LOGI(TAG, "FOTA chunk #%u accepted, total_written=%u/%u",
           (unsigned)number, (unsigned)S.bytes_written, (unsigned)S.mf.size);

  // Progress callback hook (device → main/uplink)
  // extern void fota_progress_notify(uint32_t written, uint32_t total);
  ::fota_progress_notify(S.bytes_written, S.mf.size);



  // If we exactly reached the target size, we’re ready to finalize in next step.
  if (S.bytes_written == S.mf.size) S.finalize_requested = true;
  return true;
}

// Can be called every loop; will perform finalization exactly once when ready.
// Returns true if it *completed an attempt* to finalize this cycle.
bool finalize_and_apply(bool& ok_verify, bool& ok_apply) {
  ok_verify = false; ok_apply = false;
  if (!S.session_active || S.finalized) return false;
  if (!S.sha_init) return false;
  if (S.bytes_written != S.mf.size) return false;

  // 1) Hash & verify
  unsigned char sha_out[32];
  mbedtls_sha256_finish(&S.sha, sha_out);
  mbedtls_sha256_free(&S.sha); S.sha_init = false;

  // hex->bin of mf.hash_hex (32 bytes)
  auto hex2bin = [](const std::string& h, unsigned char out[32])->bool{
    if (h.size()!=64) return false;
    for (int i=0;i<32;i++){
      char c1=h[2*i], c2=h[2*i+1];
      auto v = [](char c)->int{ if(c>='0'&&c<='9')return c-'0';
                                c|=0x20; if(c>='a'&&c<='f')return c-'a'+10; return -1; };
      int hi=v(c1), lo=v(c2); if(hi<0||lo<0) return false;
      out[i] = (uint8_t)((hi<<4)|lo);
    }
    return true;
  };

  unsigned char want[32];
  if (!hex2bin(S.mf.hash_hex, want)) {
    ESP_LOGE(TAG, "Bad manifest hash format");
    S.last_error="bad-hash-format";
    return false;
  }
  ok_verify = (memcmp(sha_out, want, 32)==0);

  // 2) Close OTA handle
  esp_err_t er = esp_ota_end(S.ota);
  if (er != ESP_OK) {
    ESP_LOGE(TAG, "esp_ota_end failed: %s", esp_err_to_name(er));
    S.last_error = "ota-end";
    return false;
  }

  if (!ok_verify) {
    ESP_LOGE(TAG, "SHA256 mismatch — keeping current app, not switching.");
    S.finalized = true;
    nvstore::set_u64(NS, K_WR,0); nvstore::set_u64(NS, K_NEXT,0);
    return false; // no reboot
  }

  // 3) Switch boot partition (apply)
  er = esp_ota_set_boot_partition(S.part);
  if (er == ESP_OK) {
    ok_apply = true;
    ESP_LOGI(TAG, "FOTA finalize success: verified & boot partition set. Rebooting...");
    S.finalized = true;
    nvstore::set_u64(NS, K_WR,0); nvstore::set_u64(NS, K_NEXT,0);
    esp_restart();
  } else {
    ESP_LOGE(TAG, "esp_ota_set_boot_partition failed: %s", esp_err_to_name(er));
    S.last_error = "set-boot";
  }
  return ok_verify && ok_apply;
}

std::string status_json(){
  // Progress & last_error are handy for cloud logs/UI
  char buf[192];
  snprintf(buf, sizeof(buf),
    "{\"fota_status\":{"
      "\"active\":%s,"
      "\"version\":\"%s\","
      "\"written\":%u,"
      "\"total\":%u,"
      "\"next_chunk\":%u,"
      "\"finalize_requested\":%s,"
      "\"finalized\":%s,"
      "\"error\":\"%s\""
    "}}",
    S.session_active ? "true":"false",
    S.mf.version.c_str(),
    (unsigned)S.bytes_written,
    (unsigned)S.mf.size,
    (unsigned)S.next_chunk,
    S.finalize_requested ? "true":"false",
    S.finalized ? "true":"false",
    S.last_error.c_str());
  return std::string(buf);
}

} // namespace fota

