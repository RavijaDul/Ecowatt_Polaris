#include "transport.hpp"
#include <string>
#include "esp_http_client.h"
#include "esp_log.h"
#if __has_include("esp_crt_bundle.h")
#include "esp_crt_bundle.h"
#define USE_CRT_BUNDLE 1
#else
#define USE_CRT_BUNDLE 0
#endif

namespace transport {
static const char* TAG = "transport";

static inline std::string endpoint_for(const std::string& kind) {
  return (kind == "read") ? "/api/inverter/read" : "/api/inverter/write";
}
static inline bool is_https(const std::string& url) {
  return url.rfind("https://", 0) == 0;
}

struct RespBuf { std::string data; };
static esp_err_t http_evt(esp_http_client_event_t* e) {
  if (!e || !e->user_data) return ESP_OK;
  auto* buf = static_cast<RespBuf*>(e->user_data);
  if (e->event_id == HTTP_EVENT_ON_DATA && e->data && e->data_len > 0) {
    buf->data.append(static_cast<const char*>(e->data), e->data_len);
  }
  return ESP_OK;
}

static std::string extract_frame_field(const std::string& json) {
  auto pos = json.find("\"frame\"");
  if (pos == std::string::npos) return {};
  pos = json.find(':', pos); if (pos == std::string::npos) return {};
  pos = json.find('"', pos); if (pos == std::string::npos) return {};
  size_t start = pos + 1;
  size_t end = json.find('"', start);
  if (end == std::string::npos) return {};
  return json.substr(start, end - start);
}

std::string post_frame(const std::string& kind,
                       const std::string& base_url,
                       const std::string& api_key_b64,
                       const std::string& frame_hex)
{
  std::string url = base_url + endpoint_for(kind);
  std::string body = std::string("{\"frame\":\"") + frame_hex + "\"}";
  RespBuf rb;

  esp_http_client_config_t cfg{}; cfg.url = url.c_str(); cfg.method = HTTP_METHOD_POST;
  cfg.event_handler = http_evt; cfg.user_data = &rb; cfg.timeout_ms = 5000;
  cfg.transport_type = is_https(url) ? HTTP_TRANSPORT_OVER_SSL : HTTP_TRANSPORT_OVER_TCP;
#if USE_CRT_BUNDLE
  if (is_https(url)) cfg.crt_bundle_attach = esp_crt_bundle_attach;
#endif
  auto cli = esp_http_client_init(&cfg);
  if (!cli) { ESP_LOGE(TAG, "http init failed"); return {}; }
  esp_http_client_set_header(cli, "Content-Type", "application/json");
  if (!api_key_b64.empty()) esp_http_client_set_header(cli, "Authorization", api_key_b64.c_str());
  esp_http_client_set_post_field(cli, body.c_str(), body.size());

  esp_err_t err = esp_http_client_perform(cli);
  int code = (err==ESP_OK) ? esp_http_client_get_status_code(cli) : -1;
  if (err != ESP_OK || code != 200 || rb.data.empty()) {
    ESP_LOGW(TAG, "%s HTTP err=%s code=%d body_len=%d", kind.c_str(), esp_err_to_name(err), code, (int)rb.data.size());
    esp_http_client_cleanup(cli);
    return {};
  }
  esp_http_client_cleanup(cli);
  std::string frame = extract_frame_field(rb.data);
  if (frame.empty()) {
    ESP_LOGW(TAG, "No 'frame' in JSON reply");
    return {};
  }
  return frame;
}

} // namespace transport
