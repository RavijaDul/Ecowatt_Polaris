#include "packetizer.hpp"                          // L1: Header for Payload struct + function declarations.
#include "codec.hpp"                               // L2: For encode_delta_rle_v1 (compression).
#include <string>                                  // L3: std::string support.
#include <vector>                                  // L4: std::vector for records and order list.
#include <cinttypes>                               // L5: For portable PRIu64 format macros.
#include <esp_log.h>                               // L6: ESP logging macros.
#include <esp_http_client.h>                       // L7: ESP-IDF HTTP client API.

#if __has_include("esp_crt_bundle.h")              // L9: Check if certificate bundle header is available.
  #include "esp_crt_bundle.h"                      // L10: Include it if present (provides CA roots).
  #define USE_CRT_BUNDLE 1                         // L11: Flag → we can attach cert bundle.
#else
  #define USE_CRT_BUNDLE 0                         // L13: Fallback → no cert bundle available.
#endif

// Utility: check scheme
static inline bool is_https(const std::string& url) { // L16
    return url.rfind("https://", 0) == 0;             // L17: True if string starts with "https://".
}

namespace uplink {                                    // L19: All packetizer functions in this namespace.
static const char* TAG="uplink";                     // L20: Logging tag.

// ---------------- Base64 Encoding (mbedTLS) ----------------
#include <mbedtls/base64.h>                          // L23: Use mbedTLS base64 implementation.
static std::string b64(const std::string& bin){      // L24: Encode binary string → Base64.
    size_t out_len=0; 
    mbedtls_base64_encode(nullptr,0,&out_len,        // L26: First call → compute required length.
        (const unsigned char*)bin.data(),bin.size());
    std::string out; out.resize(out_len);            // L28: Allocate output buffer.
    if(mbedtls_base64_encode((unsigned char*)out.data(), out_len, &out_len,
                             (const unsigned char*)bin.data(), bin.size())==0){ // L30: Encode actual data.
        out.resize(out_len); return out;             // L31: Shrink to real length and return.
    }
    return {};                                       // L33: On error → return empty string.
}

// ---------------- Build JSON payload ----------------
Payload build_payload(const std::vector<buffer::Record>& recs,
                      const std::string& device_id){ // L37
    Payload p{}; if(recs.empty()) return p;          // L38: If no records, return empty payload.

    std::vector<std::string> order;                  // L40: To hold field order (names).
    std::string blob = codec::encode_delta_rle_v1(recs, order); // L41: Compress records → binary blob.
    std::string blob_b64 = b64(blob);                // L42: Base64 encode compressed blob.

    uint64_t t0 = recs.front().epoch_ms, t1 = recs.back().epoch_ms; // L44: Start/end timestamps.

    // ---- Build ts_list as JSON array ----
    std::string ts_json = "[";                       // L47
    for(size_t i=0;i<recs.size();++i){               // L48
        char num[32];                                // L49: Temp buffer for number printing.
        snprintf(num, sizeof(num), "%llu",(unsigned long long)recs[i].epoch_ms); // L50: Print epoch-ms.
        ts_json += num;                              // L51: Append number.
        if(i+1<recs.size()) ts_json += ",";          // L52: Add comma if not last.
    }
    ts_json += "]";                                  // L54: Close array.

    // ---- Build final JSON ----
    std::string json;
    json.reserve(blob_b64.size() + 256);             // L58: Reserve space to minimize reallocations.
    json += "{\"device_id\":\""; json += device_id; json += "\","; // L59: Device ID.
    char buf[128];
    snprintf(buf, sizeof(buf), "\"ts_start\":%llu,\"ts_end\":%llu,",
             (unsigned long long)t0, (unsigned long long)t1); // L62: Start/end timestamps.
    json += buf;
    json += "\"seq\":0,\"codec\":\"delta_rle_v1\",\"order\":["; // L64: Hardcoded seq=0, codec name.
    for(size_t i=0;i<order.size();++i){             // L65: Append field order array.
        json+='"'; json+=order[i]; json+='"';
        if(i+1<order.size()) json+=',';             // L67
    }
    json += "],\"ts_list\":"; json += ts_json; json += ","; // L69: Insert ts_list.
    json += "\"block_b64\":\""; json += blob_b64; json += "\"}"; // L70: Insert compressed block.

    p.json = std::move(json);                        // L72: Save JSON into payload struct.
    p.raw_bytes = blob.size();                       // L73: Record compressed size.
    return p;                                        // L74
}

// ---------------- Post JSON payload ----------------
bool post_payload(const std::string& base_url,
                  const std::string& api_key_b64,
                  const std::string& json_body){     // L78
    std::string url = base_url;                      // L79: Copy base URL.
    if (!url.empty() && url.back() == '/') url.pop_back(); // L80: Strip trailing slash if present.
    url += "/api/device/upload";                     // L81: Append upload endpoint.

    esp_http_client_config_t cfg{};                  // L83: Init config struct.
    cfg.url = url.c_str();                           // L84: Target URL.
    cfg.method = HTTP_METHOD_POST;                   // L85: POST request.
    cfg.timeout_ms = 8000;                           // L86: 8s timeout.
    cfg.transport_type = is_https(url) ? HTTP_TRANSPORT_OVER_SSL
                                       : HTTP_TRANSPORT_OVER_TCP; // L87: Choose TLS/plain.
#if USE_CRT_BUNDLE
    if (is_https(url)) cfg.crt_bundle_attach = esp_crt_bundle_attach; // L90: Attach CA certs if available.
#endif

    esp_http_client_handle_t cli = esp_http_client_init(&cfg); // L93: Create client handle.
    if(!cli){ ESP_LOGE(TAG, "http init failed"); return false; } // L94: Bail if failed.

    if(!api_key_b64.empty()){                        // L96: Add Authorization header if key provided.
        esp_http_client_set_header(cli, "Authorization", api_key_b64.c_str());
    }
    esp_http_client_set_header(cli, "Content-Type", "application/json"); // L99
    esp_http_client_set_post_field(cli, json_body.c_str(), json_body.size()); // L100: Set body.

    esp_err_t e = esp_http_client_perform(cli);      // L102: Execute POST.
    int code = (e==ESP_OK) ? esp_http_client_get_status_code(cli) : -1; // L103: HTTP status.

    if(e != ESP_OK){                                 // L105: Transport failure.
        ESP_LOGW(TAG, "POST %s failed: %s",
                 is_https(url) ? "https" : "http", esp_err_to_name(e));
        esp_http_client_cleanup(cli);
        return false;
    }

    ESP_LOGI(TAG, "POST %s %s -> %d (%u bytes)",    // L111: Log success.
             is_https(url) ? "https" : "http", url.c_str(), code,
             (unsigned)json_body.size());

    esp_http_client_cleanup(cli);                    // L114: Free resources.
    if(code < 200 || code >= 300){                   // L115: Reject non-2xx codes.
        ESP_LOGW(TAG, "upload failed http=%d", code);
        return false;
    }
    return true;                                     // L118: Upload successful.
}

} // namespace uplink                                // L120
