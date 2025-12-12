#include "transport.hpp"
#include <string>
#include "esp_http_client.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
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
static uint32_t conn_failures = 0;
static uint8_t retry_count = 3;
static uint32_t backoff_base_ms = 200;
static uint32_t backoff_max_ms = 2000;
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

  std::string last_body;
  int last_code = -1;
  esp_err_t last_err = ESP_FAIL;
  for (uint8_t attempt = 0; attempt < retry_count; ++attempt) {
    rb.data.clear();
    last_err = esp_http_client_perform(cli);
    last_code = (last_err==ESP_OK) ? esp_http_client_get_status_code(cli) : -1;
    last_body = rb.data;
    if (last_err == ESP_OK && last_code == 200 && !rb.data.empty()) break;
    ++conn_failures;
    ESP_LOGW(TAG, "%s HTTP attempt=%u err=%s code=%d body_len=%d", kind.c_str(), (unsigned)attempt+1, esp_err_to_name(last_err), last_code, (int)rb.data.size());
    // backoff before next try
    uint32_t delay_ms = backoff_base_ms << attempt;
    if (delay_ms > backoff_max_ms) delay_ms = backoff_max_ms;
    vTaskDelay(pdMS_TO_TICKS(delay_ms));
  }
  if (last_err != ESP_OK || last_code != 200 || last_body.empty()) {
    esp_http_client_cleanup(cli);
    return {};
  }
  esp_http_client_cleanup(cli);
  std::string frame = extract_frame_field(last_body);
  if (frame.empty()) {
    ESP_LOGW(TAG, "No 'frame' in JSON reply");
    return {};
  }
  return frame;
}

std::string get_fota_chunk(const std::string& base_url,
                           const std::string& device_id,
                           uint32_t chunk_number)
{
  // Build URL: GET /api/fota/chunk?device=<id>&chunk=<number>
  char url_buf[256];
  snprintf(url_buf, sizeof(url_buf), "%s/api/fota/chunk?device=%s&chunk=%u",
           base_url.c_str(), device_id.c_str(), (unsigned)chunk_number);
  
  RespBuf rb;
  esp_http_client_config_t cfg{};
  cfg.url = url_buf;
  cfg.method = HTTP_METHOD_GET;
  cfg.event_handler = http_evt;
  cfg.user_data = &rb;
  cfg.timeout_ms = 10000;  // longer timeout for chunk download
  cfg.transport_type = is_https(base_url) ? HTTP_TRANSPORT_OVER_SSL : HTTP_TRANSPORT_OVER_TCP;
#if USE_CRT_BUNDLE
  if (is_https(base_url)) cfg.crt_bundle_attach = esp_crt_bundle_attach;
#endif
  
  auto cli = esp_http_client_init(&cfg);
  if (!cli) {
    ESP_LOGE(TAG, "fota_chunk: http init failed");
    return {};
  }
  
  std::string last_body;
  int last_code = -1;
  esp_err_t last_err = ESP_FAIL;
  for (uint8_t attempt = 0; attempt < retry_count; ++attempt) {
    rb.data.clear();
    last_err = esp_http_client_perform(cli);
    last_code = (last_err == ESP_OK) ? esp_http_client_get_status_code(cli) : -1;
    last_body = rb.data;
    if (last_err == ESP_OK && last_code == 200 && !rb.data.empty()) {
      ESP_LOGI(TAG, "FOTA chunk %u fetched (%d bytes)", (unsigned)chunk_number, (int)rb.data.size());
      esp_http_client_cleanup(cli);
      return rb.data;
    }
    ++conn_failures;
    ESP_LOGW(TAG, "FOTA chunk %u attempt=%u err=%s code=%d", 
             (unsigned)chunk_number, (unsigned)attempt+1, esp_err_to_name(last_err), last_code);
    if (attempt < retry_count - 1) {
      vTaskDelay(pdMS_TO_TICKS(backoff_base_ms));
    }
  }
  
  ESP_LOGE(TAG, "FOTA chunk %u failed after %u attempts", (unsigned)chunk_number, (unsigned)retry_count);
  esp_http_client_cleanup(cli);
  return {};
}

uint32_t get_conn_failures(){ return conn_failures; }

void set_retry_policy(uint8_t retries, uint32_t base_backoff_ms, uint32_t max_backoff_ms){
  retry_count = retries > 0 ? retries : 1;
  backoff_base_ms = base_backoff_ms > 0 ? base_backoff_ms : 200;
  backoff_max_ms = max_backoff_ms > 0 ? max_backoff_ms : 2000;
}
} // namespace transport
