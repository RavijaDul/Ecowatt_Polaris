// main/transport_idf.cpp — complete
#include "transport.hpp"                   // Public declaration for post_frame()
#include <string>                          // std::string for URL/body/JSON
#include <cstring>                         // std::strlen etc. (not strictly required here)
#include <cstdio>                          // std::snprintf (not used; common in logging patterns)
#include "esp_http_client.h"               // ESP-IDF HTTP client APIs
#include "esp_log.h"                       // ESP-IDF logging macros

namespace transport {

static const char* TAG = "transport";      // Log tag used with ESP_LOGx macros

// endpoint_for
// Maps operation kind → REST endpoint path.
// "read"  → /api/inverter/read
// others → /api/inverter/write
static inline std::string endpoint_for(const std::string& kind) {
    return (kind == "read") ? "/api/inverter/read" : "/api/inverter/write";
}

// RespBuf
// Small accumulator for HTTP response body bytes; filled from the event callback.
struct RespBuf { std::string data; };

// http_evt
// Event callback used by esp_http_client to deliver response chunks.
// Only DATA events are appended; other events are ignored safely.
static esp_err_t http_evt(esp_http_client_event_t* e) {
    if (!e || !e->user_data) return ESP_OK;                           // Ignore if callback data missing
    auto* buf = static_cast<RespBuf*>(e->user_data);                  // Recover accumulator
    if (e->event_id == HTTP_EVENT_ON_DATA && e->data && e->data_len > 0) {
        buf->data.append(static_cast<const char*>(e->data), e->data_len); // Append body slice
    }
    return ESP_OK;                                                     // Continue HTTP processing
}

// extract_frame_field
// Minimal JSON extraction for the exact server reply shape: {"frame":"...."}.
// Avoids pulling a full JSON library on embedded targets.
// Returns the string value of the "frame" field, or empty on any parsing failure.
static std::string extract_frame_field(const std::string& json) {
    auto pos = json.find("\"frame\"");      // Locate the "frame" key
    if (pos == std::string::npos) return {}; // Key not present → fail
    pos = json.find(':', pos);              // Find colon after the key
    if (pos == std::string::npos) return {}; // Malformed JSON → fail
    pos = json.find('"', pos);              // Opening quote of string value
    if (pos == std::string::npos) return {}; // No opening quote → fail
    size_t start = pos + 1;                 // Start of the value contents
    size_t end = json.find('"', start);     // Closing quote of the value
    if (end == std::string::npos) return {}; // Unterminated string → fail
    return json.substr(start, end - start); // Return the extracted substring
}

// post_frame
// Sends a Modbus RTU frame (hex string) to the configured REST endpoint and
// returns the "frame" field from the JSON response. Empty string indicates
// transport error, non-200 status, blank reply, or missing/invalid JSON.
//
// Parameters:
//   kind        : "read" or "write" → selects endpoint path
//   base_url    : e.g., "http://<ip>:8080" (no trailing slash required)
//   api_key_b64 : Authorization header value (Base64 pair as provided; no "Basic " prefix)
//   frame_hex   : Modbus RTU frame as uppercase hex (including CRC, low byte first)
std::string post_frame(const std::string& kind,
                       const std::string& base_url,
                       const std::string& api_key_b64,
                       const std::string& frame_hex) {
    // Compose full URL and small JSON body: {"frame":"<HEX>"}
    std::string url = base_url + endpoint_for(kind);      // Base + path
    std::string body;                                     // Pre-size to reduce reallocations
    body.reserve(20 + frame_hex.size());                  // Enough for {"frame":""} + payload
    body += "{\"frame\":\"";
    body += frame_hex;
    body += "\"}";

    RespBuf rb;                                           // Response buffer for callback to fill

    // Configure HTTP client
    esp_http_client_config_t cfg = {};                    // Zero-initialize config
    cfg.url = url.c_str();                                // Target URL (C-string)
    cfg.method = HTTP_METHOD_POST;                        // Always POST
    cfg.event_handler = http_evt;                         // Body collector
    cfg.user_data = &rb;                                  // Pass accumulator to callback
    cfg.timeout_ms = 5000;                                // 5 s timeout (plain HTTP)

    esp_http_client_handle_t cli = esp_http_client_init(&cfg); // Create client
    if (!cli) {                                           // Abort on init failure
        ESP_LOGE(TAG, "esp_http_client_init failed");
        return {};
    }

    // Set required headers
    // Note: Authorization expects the Base64 value directly; do not prepend "Basic ".
    esp_http_client_set_header(cli, "Content-Type", "application/json");
    esp_http_client_set_header(cli, "Authorization", api_key_b64.c_str());
    esp_http_client_set_post_field(cli, body.c_str(), body.size());   // Attach request body

    // Execute request
    esp_err_t err = esp_http_client_perform(cli);         // Blocking perform
    int status = -1;                                      // Placeholder for HTTP status
    if (err == ESP_OK) status = esp_http_client_get_status_code(cli); // Read status if OK

    if (err != ESP_OK) {                                  // Network/transport error
        ESP_LOGW(TAG, "%s HTTP perform error: %s", kind.c_str(), esp_err_to_name(err));
        esp_http_client_cleanup(cli);                     // Free client handle
        return {};                                        // Indicate failure
    }

    ESP_LOGI(TAG, "%s HTTP %d, %d bytes", kind.c_str(), status, (int)rb.data.size()); // Trace

    // Non-200 status → return empty and log response body for diagnostics
    if (status != 200) {
        ESP_LOGW(TAG, "%s non-200 status: %d. Body: '%.*s'",
                 kind.c_str(), status, (int)rb.data.size(), rb.data.c_str());
        esp_http_client_cleanup(cli);
        return {};
    }

    // An empty body means the server rejected the input or produced no frame
    if (rb.data.empty()) {
        // Per API: blank body indicates invalid/failed frame processing
        ESP_LOGW(TAG, "Blank JSON body");
        esp_http_client_cleanup(cli);
        return {};
    }

    // Extract the "frame" string from the tiny JSON reply
    std::string frame = extract_frame_field(rb.data);
    if (frame.empty()) {                                  // Missing/invalid JSON shape
        ESP_LOGW(TAG, "No 'frame' field in JSON: '%.*s'", (int)rb.data.size(), rb.data.c_str());
        esp_http_client_cleanup(cli);
        return {};
    }

    esp_http_client_cleanup(cli);                         // Always release the client handle
    return frame;                                         // Return parsed Modbus frame (hex)
}

} // namespace transport
