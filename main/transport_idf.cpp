// main/transport_idf.cpp — HTTP client glue for ESP-IDF
#include "transport.hpp"                           // L1: Declarations for post_frame(...) used by acquisition.cpp.
#include <string>                                  // L2: std::string for URLs and JSON bodies.
#include <cstring>                                 // L3: C string helpers if needed by client.
#include <cstdio>                                  // L4: printf-style formatting (not strictly needed).
#include "esp_http_client.h"                       // L5: ESP-IDF HTTP client API.
#include "esp_log.h"                               // L6: Logging macros.

#if __has_include("esp_crt_bundle.h")              // L8: Check if system CA bundle header exists.
  #include "esp_crt_bundle.h"                      // L9: Pull in CA bundle attach function (TLS verification).
  #define USE_CRT_BUNDLE 1                         // L10: Enable bundle usage flag.
#else
  #define USE_CRT_BUNDLE 0                         // L12: Fallback if the bundle isn’t available.
#endif

namespace transport {                              // L14: Begin transport namespace for isolation.

static const char* TAG = "transport";              // L16: Log tag for this module.

// Map the logical kind ("read"/"write") to the SIM’s REST path.
static inline std::string endpoint_for(const std::string& kind) {  // L19
    return (kind == "read") ? "/api/inverter/read" : "/api/inverter/write"; // L20: Two known endpoints.
}

// Detect scheme to select TLS or plain TCP.
static inline bool is_https(const std::string& url) {              // L23
    return url.rfind("https://", 0) == 0;                          // L24: True if it starts with "https://".
}

// Small accumulator used by esp_http_client event callback.
struct RespBuf { std::string data; };                              // L27: Holds response body bytes as they arrive.

// HTTP event handler: collect body chunks into RespBuf::data.
static esp_err_t http_evt(esp_http_client_event_t* e) {            // L30
    if (!e || !e->user_data) return ESP_OK;                        // L31: Nothing to do if null.
    auto* buf = static_cast<RespBuf*>(e->user_data);               // L32: Cast back to our buffer.
    if (e->event_id == HTTP_EVENT_ON_DATA && e->data && e->data_len > 0) { // L33: New data arrived?
        buf->data.append(static_cast<const char*>(e->data), e->data_len);  // L34: Append bytes to string.
    }
    return ESP_OK;                                                 // L36: Continue normal processing.
}

// Extract the JSON field "frame":"...." out of a tiny JSON object.
// NOTE: very naive parser; assumes no escaping and a flat object.
static std::string extract_frame_field(const std::string& json) {  // L40
    auto pos = json.find("\"frame\"");                             // L41: Look for the key literal.
    if (pos == std::string::npos) return {};                       // L42: Not found → empty.
    pos = json.find(':', pos);              if (pos == std::string::npos) return {}; // L43–L44: Find colon.
    pos = json.find('"', pos);              if (pos == std::string::npos) return {}; // L45–L46: Opening quote.
    size_t start = pos + 1;                                                     // L47: Start of value.
    size_t end = json.find('"', start);     if (end == std::string::npos) return {}; // L48–L49: Closing quote.
    return json.substr(start, end - start);                                      // L50: Return the raw hex string.
}

// POST {"frame":"<hex>"} to /read or /write; return the "frame" from JSON reply.
std::string post_frame(const std::string& kind,                      // L54: "read" or "write".
                       const std::string& base_url,                  // L55: Base URL of SIM, e.g., http://20.15.114.131:8080
                       const std::string& api_key_b64,               // L56: Authorization header contents (may be empty).
                       const std::string& frame_hex) {               // L57: Hex string payload to send.
    std::string url = base_url + endpoint_for(kind);                 // L58: Compose full endpoint URL.

    std::string body;                                                // L60: Build a tiny JSON body.
    body.reserve(20 + frame_hex.size());                             // L61: Reserve to avoid reallocs.
    body += "{\"frame\":\""; body += frame_hex; body += "\"}";       // L62: {"frame":"HEX"} (no escaping).

    RespBuf rb;                                                      // L64: Response accumulator.

    esp_http_client_config_t cfg = {};                               // L66: Zero-init client configuration.
    cfg.url = url.c_str();                                           // L67: Set request URL.
    cfg.method = HTTP_METHOD_POST;                                   // L68: Use POST.
    cfg.event_handler = http_evt;                                    // L69: Hook event handler for body collection.
    cfg.user_data = &rb;                                             // L70: Pass our accumulator through the client.
    cfg.timeout_ms = 5000;                                           // L71: 5s timeout (tunable).

    cfg.transport_type = is_https(url) ? HTTP_TRANSPORT_OVER_SSL     // L73: Choose TLS for https:// ...
                                       : HTTP_TRANSPORT_OVER_TCP;    // L74: ... else plain TCP.
#if USE_CRT_BUNDLE
    if (is_https(url)) cfg.crt_bundle_attach = esp_crt_bundle_attach; // L76: Attach CA bundle for TLS verify.
#endif

    esp_http_client_handle_t cli = esp_http_client_init(&cfg);       // L79: Create client handle.
    if (!cli) { ESP_LOGE(TAG, "esp_http_client_init failed"); return {}; } // L80: Bail out if init fails.

    esp_http_client_set_header(cli, "Content-Type", "application/json");      // L82: JSON content type.
    if (!api_key_b64.empty())                                                // L83: If caller configured auth…
        esp_http_client_set_header(cli, "Authorization", api_key_b64.c_str()); // L84: … set Authorization header.
    esp_http_client_set_post_field(cli, body.c_str(), body.size());          // L85: Provide POST body.

    esp_err_t err = esp_http_client_perform(cli);                             // L87: Execute request synchronously.
    int status = (err == ESP_OK) ? esp_http_client_get_status_code(cli) : -1; // L88: Grab HTTP status if OK.

    if (err != ESP_OK) {                                                      // L90: Network/transport error?
        ESP_LOGW(TAG, "%s HTTP perform error: %s", kind.c_str(), esp_err_to_name(err)); // L91: Warn with reason.
        esp_http_client_cleanup(cli);                                         // L92: Free client handle.
        return {};                                                            // L93: Signal failure with empty string.
    }

    ESP_LOGI(TAG, "%s HTTP %d, %d bytes", kind.c_str(), status, (int)rb.data.size()); // L96: Log status and body size.

    if (status != 200 || rb.data.empty()) {                                   // L98: Expect 200 OK + nonempty body.
        ESP_LOGW(TAG, "%s non-200/empty: %d, body='%.*s'",                    // L99: Log and bail on unexpected status.
                 kind.c_str(), status, (int)rb.data.size(), rb.data.c_str());
        esp_http_client_cleanup(cli);                                         // L101
        return {};                                                            // L102
    }

    std::string frame = extract_frame_field(rb.data);                          // L104: Pull "frame" value out of JSON.
    if (frame.empty()) {                                                       // L105: Missing key or parse failure?
        ESP_LOGW(TAG, "No 'frame' field in JSON: '%.*s'", (int)rb.data.size(), rb.data.c_str()); // L106: Warn.
        esp_http_client_cleanup(cli);                                          // L107: Free handle.
        return {};                                                             // L108: Signal failure.
    }

    esp_http_client_cleanup(cli);                                              // L110: Clean up client on success.
    return frame;                                                              // L111: Return raw hex frame string.
}

} // namespace transport                                                         // L114: End namespace.
