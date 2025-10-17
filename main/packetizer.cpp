#include "packetizer.hpp"
#include "codec.hpp"
#include <string>
#include <vector>
#include <cinttypes>
#include <esp_log.h>
#include <esp_http_client.h>
#if __has_include("esp_crt_bundle.h")
#include "esp_crt_bundle.h"
#define USE_CRT_BUNDLE 1
#else
#define USE_CRT_BUNDLE 0
#endif
#include <mbedtls/base64.h>

static inline bool is_https(const std::string& url) { return url.rfind("https://", 0) == 0; }

namespace uplink {
static const char* TAG="uplink";

static std::string b64(const std::string& bin){
  size_t out_len=0; mbedtls_base64_encode(nullptr,0,&out_len,(const unsigned char*)bin.data(),bin.size());
  std::string out; out.resize(out_len);
  if(mbedtls_base64_encode((unsigned char*)out.data(), out_len, &out_len,
                           (const unsigned char*)bin.data(), bin.size())==0){ out.resize(out_len); return out; }
  return {};
}

Payload build_payload(const std::vector<buffer::Record>& recs, const std::string& device_id){
  Payload p{}; if(recs.empty()) return p;
  std::vector<std::string> order;
  std::string blob = codec::encode_delta_rle_v1(recs, order);
  std::string blob_b64 = b64(blob);
  uint64_t t0 = recs.front().epoch_ms, t1 = recs.back().epoch_ms;

  std::string ts_json = "[";
  for(size_t i=0;i<recs.size();++i){
    char num[32]; snprintf(num, sizeof(num), "%llu",(unsigned long long)recs[i].epoch_ms);
    ts_json += num; if(i+1<recs.size()) ts_json += ",";
  }
  ts_json += "]";

  std::string json; json.reserve(blob_b64.size() + 256);
  json += "{\"device_id\":\""; json += device_id; json += "\",";
  char buf[128];
  snprintf(buf, sizeof(buf), "\"ts_start\":%llu,\"ts_end\":%llu,", (unsigned long long)t0, (unsigned long long)t1);
  json += buf;
  json += "\"seq\":0,\"codec\":\"delta_rle_v1\",\"order\":[";
  for(size_t i=0;i<order.size();++i){ json+='"'; json+=order[i]; json+='"'; if(i+1<order.size()) json+=','; }
  json += "],\"ts_list\":"; json += ts_json; json += ",";
  json += "\"block_b64\":\""; json += blob_b64; json += "\"}";

  // Add orig stats (server will store if present)
  json.pop_back();
  size_t orig_samples = recs.size();
  size_t orig_bytes = orig_samples * 28;
  char xbuf[96];
  snprintf(xbuf, sizeof(xbuf), ",\"orig_samples\":%u,\"orig_bytes\":%u}", (unsigned)orig_samples, (unsigned)orig_bytes);
  json += xbuf;

  p.json = std::move(json); p.raw_bytes = blob.size();
  return p;
}

bool post_payload(const std::string& base_url, const std::string& api_key_b64, const std::string& json_body){
  std::string url = base_url; if(!url.empty() && url.back()=='/') url.pop_back(); url += "/api/device/upload";
  esp_http_client_config_t cfg{}; cfg.url = url.c_str(); cfg.method = HTTP_METHOD_POST; cfg.timeout_ms = 8000;
  cfg.transport_type = is_https(url) ? HTTP_TRANSPORT_OVER_SSL : HTTP_TRANSPORT_OVER_TCP;
#if USE_CRT_BUNDLE
  if (is_https(url)) cfg.crt_bundle_attach = esp_crt_bundle_attach;
#endif
  auto cli = esp_http_client_init(&cfg); if(!cli){ ESP_LOGE(TAG, "http init failed"); return false; }
  if(!api_key_b64.empty()) esp_http_client_set_header(cli, "Authorization", api_key_b64.c_str());
  esp_http_client_set_header(cli, "Content-Type", "application/json");
  esp_http_client_set_post_field(cli, json_body.c_str(), json_body.size());
  esp_err_t e = esp_http_client_perform(cli);
  int code = (e==ESP_OK) ? esp_http_client_get_status_code(cli) : -1;
  ESP_LOGI(TAG, "POST %s -> %d (%u bytes)", url.c_str(), code, (unsigned)json_body.size());
  esp_http_client_cleanup(cli);
  return (e==ESP_OK && code>=200 && code<300);
}

struct RespBuf { std::string data; };
static esp_err_t http_evt(esp_http_client_event_t* e){
  if(!e || !e->user_data) return ESP_OK;
  if(e->event_id==HTTP_EVENT_ON_DATA && e->data && e->data_len>0){
    auto* rb = static_cast<RespBuf*>(e->user_data);
    rb->data.append((const char*)e->data, e->data_len);
  }
  return ESP_OK;
}

bool post_payload_and_get_reply(const std::string& base_url,
                                const std::string& api_key_b64,
                                const std::string& json_body,
                                std::string& out_reply_body)
{
  out_reply_body.clear();
  std::string url = base_url; if(!url.empty() && url.back()=='/') url.pop_back(); url += "/api/device/upload";
  esp_http_client_config_t cfg{}; cfg.url = url.c_str(); cfg.method = HTTP_METHOD_POST; cfg.timeout_ms = 8000;
  RespBuf rb; cfg.event_handler = http_evt; cfg.user_data = &rb;
  cfg.transport_type = is_https(url) ? HTTP_TRANSPORT_OVER_SSL : HTTP_TRANSPORT_OVER_TCP;
#if USE_CRT_BUNDLE
  if (is_https(url)) cfg.crt_bundle_attach = esp_crt_bundle_attach;
#endif
  auto cli = esp_http_client_init(&cfg); if(!cli) return false;
  if(!api_key_b64.empty()) esp_http_client_set_header(cli, "Authorization", api_key_b64.c_str());
  esp_http_client_set_header(cli, "Content-Type", "application/json");
  esp_http_client_set_post_field(cli, json_body.c_str(), json_body.size());
  esp_err_t e = esp_http_client_perform(cli);
  int code = (e==ESP_OK) ? esp_http_client_get_status_code(cli) : -1;
  bool ok = (e==ESP_OK && code>=200 && code<300);
  if(ok) out_reply_body.swap(rb.data);
  esp_http_client_cleanup(cli);
  return ok;
}

} // namespace uplink
