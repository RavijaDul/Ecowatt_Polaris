// ===== must be first so Kconfig macros are visible =====
#include "sdkconfig.h"
#include <esp_ota_ops.h>

#ifndef CONFIG_ECOWATT_WIFI_SSID
#define CONFIG_ECOWATT_WIFI_SSID "YOUR_WIFI_SSID"
#endif
#ifndef CONFIG_ECOWATT_WIFI_PASS
#define CONFIG_ECOWATT_WIFI_PASS "YOUR_WIFI_PASSWORD"
#endif
#ifndef CONFIG_ECOWATT_API_BASE_URL
#define CONFIG_ECOWATT_API_BASE_URL "http://20.15.114.131:8080"
#endif
#ifndef CONFIG_ECOWATT_API_KEY_B64
#define CONFIG_ECOWATT_API_KEY_B64 ""
#endif
#ifndef CONFIG_ECOWATT_CLOUD_BASE_URL
#define CONFIG_ECOWATT_CLOUD_BASE_URL "http://192.168.246.159:5000"
#endif
#ifndef CONFIG_ECOWATT_CLOUD_KEY_B64
#define CONFIG_ECOWATT_CLOUD_KEY_B64 ""
#endif
#ifndef CONFIG_ECOWATT_UPLOAD_INTERVAL_SEC
#define CONFIG_ECOWATT_UPLOAD_INTERVAL_SEC 15
#endif
#ifndef CONFIG_ECOWATT_SAMPLE_PERIOD_MS
#define CONFIG_ECOWATT_SAMPLE_PERIOD_MS 5000
#endif
#ifndef CONFIG_ECOWATT_DEVICE_ID
#define CONFIG_ECOWATT_DEVICE_ID "EcoWatt-Dev-01"
#endif
#ifndef CONFIG_ECOWATT_PSK
#define CONFIG_ECOWATT_PSK "ecowatt-demo-psk"
#endif
#ifndef CONFIG_ECOWATT_USE_ENVELOPE
#define CONFIG_ECOWATT_USE_ENVELOPE 1
#endif

#include <cstring>
#include <string>
#include <sys/time.h>
#include <cinttypes>
#include <inttypes.h>

#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_sntp.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"

#include "acquisition.hpp"
#include "buffer.hpp"
#include "packetizer.hpp"
#include "codec.hpp"
#include "security.hpp"
#include "nvstore.hpp"
#include "control.hpp"
#include "fota.hpp"

static const char* TAG = "main";

static EventGroupHandle_t s_evt;
static constexpr int BIT_CONNECTED = BIT0;
static constexpr int BIT_GOT_IP    = BIT1;
static constexpr int BIT_NTP_OK    = BIT2;

static buffer::Ring* g_ring = nullptr;
static SemaphoreHandle_t g_ring_mtx = nullptr;

static int64_t s_epoch_offset_ms = 0;
static inline uint64_t monotonic_ms(){ return (uint64_t)esp_timer_get_time() / 1000ULL; }
static inline uint64_t now_ms_epoch(){ return (uint64_t)((int64_t)monotonic_ms() + s_epoch_offset_ms); }

// Runtime config/command/FOTA
static control::RuntimeConfig g_cfg_cur{}; 
static control::RuntimeConfig g_cfg_next{};
static bool g_has_pending_cfg = false;

static control::PendingCommand g_cmd{};
static control::CommandResult  g_cmd_res{};

static uint64_t g_device_nonce = 0;
static uint64_t g_last_cloud_nonce = 0;

static acquisition::Acquisition* g_acq = nullptr;
struct {
  bool has = false;
  uint32_t written = 0;
  uint32_t total   = 0;
} g_fota_progress;

struct {
  bool has = false;
  bool verify_ok = false;
  bool apply_ok  = false;
} g_fota_report;

struct {
  bool has = false;   // set once on first successful boot after OTA
} g_fota_bootack;

// Called by fota.cpp after each accepted chunk
extern "C" void fota_progress_notify(uint32_t written, uint32_t total){
  g_fota_progress.has   = true;
  g_fota_progress.written = written;
  g_fota_progress.total   = total;
}

static void on_wifi(void*, esp_event_base_t base, int32_t id, void*) {
  if (base != WIFI_EVENT) return;
  switch (id) {
    case WIFI_EVENT_STA_START:     esp_wifi_connect(); break;
    case WIFI_EVENT_STA_CONNECTED: xEventGroupSetBits(s_evt, BIT_CONNECTED); ESP_LOGI(TAG, "Wi-Fi associated"); break;
    case WIFI_EVENT_STA_DISCONNECTED:
      xEventGroupClearBits(s_evt, BIT_CONNECTED | BIT_GOT_IP);
      ESP_LOGW(TAG, "Wi-Fi disconnected — reconnecting…"); esp_wifi_connect(); break;
    default: break;
  }
}
static void on_ip(void*, esp_event_base_t base, int32_t id, void* data) {
  if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    auto* e = static_cast<ip_event_got_ip_t*>(data);
    ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&e->ip_info.ip));
    xEventGroupSetBits(s_evt, BIT_GOT_IP);
  }
}
static void wifi_start_and_wait_ip() {
  ESP_ERROR_CHECK(esp_netif_init());
  ESP_ERROR_CHECK(esp_event_loop_create_default());
  esp_netif_create_default_wifi_sta();
  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  ESP_ERROR_CHECK(esp_wifi_init(&cfg));
  s_evt = xEventGroupCreate();
  ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &on_wifi, nullptr, nullptr));
  ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &on_ip, nullptr, nullptr));
  wifi_config_t sta{}; std::strncpy((char*)sta.sta.ssid, CONFIG_ECOWATT_WIFI_SSID, sizeof(sta.sta.ssid)-1);
  std::strncpy((char*)sta.sta.password, CONFIG_ECOWATT_WIFI_PASS, sizeof(sta.sta.password)-1);
  sta.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
  ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
  ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta));
  ESP_ERROR_CHECK(esp_wifi_start());
  // s_evt = xEventGroupCreate();
  (void)xEventGroupWaitBits(s_evt, BIT_GOT_IP, pdFALSE, pdFALSE, pdMS_TO_TICKS(20000));
}

static void sntp_sync_cb(struct timeval *tv) {
  (void)tv;
  struct timeval now{}; gettimeofday(&now, nullptr);
  int64_t epoch_ms = (int64_t)now.tv_sec * 1000LL + (now.tv_usec / 1000);
  int64_t mono_ms  = (int64_t)monotonic_ms();
  s_epoch_offset_ms = epoch_ms - mono_ms;
  ESP_LOGI(TAG, "NTP sync: epoch_ms=%lld mono_ms=%lld offset_ms=%lld",
           (long long)epoch_ms, (long long)mono_ms, (long long)s_epoch_offset_ms);
  xEventGroupSetBits(s_evt, BIT_NTP_OK);
}
static void ntp_start_and_wait_blocking(uint32_t max_wait_ms) {
  esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
  esp_sntp_setservername(0, "pool.ntp.org");
  esp_sntp_set_time_sync_notification_cb(sntp_sync_cb);
  esp_sntp_init();
  EventBits_t bits = xEventGroupWaitBits(s_evt, BIT_NTP_OK, pdFALSE, pdFALSE, pdMS_TO_TICKS(max_wait_ms));
  if ((bits & BIT_NTP_OK) == 0) ESP_LOGW(TAG, "NTP sync timed out; acquisition continues without epoch offset");
}


// ------------------ Tasks ------------------
static void task_acq(void*){
  if ((xEventGroupGetBits(s_evt) & BIT_NTP_OK) == 0) {
    xEventGroupWaitBits(s_evt, BIT_NTP_OK, pdFALSE, pdFALSE, pdMS_TO_TICKS(60000));
  }
  TickType_t last = xTaskGetTickCount();
  uint32_t period_ms = g_cfg_cur.sampling_interval_ms;
  TickType_t period_ticks = pdMS_TO_TICKS(period_ms);

  while(true){
    acquisition::Sample s{};
    std::vector<int> fids; for(auto f: g_cfg_cur.fields) fids.push_back((int)f);
    if(!fids.empty()) g_acq->read_selected(fids, s); else g_acq->read_all(s);

    buffer::Record rec{ now_ms_epoch(), s };
    xSemaphoreTake(g_ring_mtx, portMAX_DELAY); g_ring->push(rec); xSemaphoreGive(g_ring_mtx);

    ESP_LOGI(TAG, "ACQ tick @ %" PRIu64 " ms (epoch)", rec.epoch_ms);
    vTaskDelayUntil(&last, period_ticks);
    if(period_ms != g_cfg_cur.sampling_interval_ms){
      period_ms   = g_cfg_cur.sampling_interval_ms;
      period_ticks= pdMS_TO_TICKS(period_ms);
    }
  }
}

static void task_uplink(void*){
  TickType_t last = xTaskGetTickCount();
  const TickType_t period = pdMS_TO_TICKS(CONFIG_ECOWATT_UPLOAD_INTERVAL_SEC * 1000);

  while(true){
    if(g_has_pending_cfg){
      g_cfg_cur = g_cfg_next;
      g_has_pending_cfg = false;
      std::string cfg_json = "{\"sampling_interval\":" + std::to_string(g_cfg_cur.sampling_interval_ms/1000) + "}";
      nvstore::set_str("cfg","runtime", cfg_json);
    }

    std::vector<buffer::Record> batch;
    xSemaphoreTake(g_ring_mtx, portMAX_DELAY);
    batch = g_ring->snapshot_and_clear();
    xSemaphoreGive(g_ring_mtx);

    std::string body_json;
    if(!batch.empty()){
      codec::BenchResult br = codec::run_benchmark_delta_rle_v1(batch);
      double ratio = (br.comp_bytes>0)? double(br.orig_bytes)/double(br.comp_bytes) : 0.0;
      ESP_LOGI(TAG,"[BENCH] n=%u orig=%uB comp=%uB ratio=%.2fx encode=%.3fms lossless=%s",
               (unsigned)br.n_samples,(unsigned)br.orig_bytes,(unsigned)br.comp_bytes,ratio,br.encode_ms,br.lossless_ok?"yes":"NO");
      auto payload = uplink::build_payload(batch, CONFIG_ECOWATT_DEVICE_ID);

      std::string extra = control::to_json_status(g_cmd_res);
      if(!extra.empty() && extra!="{}" && payload.json.back()=='}'){
        std::string j = payload.json; j.pop_back();
        if(extra.front()=='{') extra.erase(0,1);
        body_json = j + "," + extra;  // merged into root
      }
      if(body_json.empty()) body_json = payload.json;
    } else {
      body_json = std::string("{\"device_id\":\"") + CONFIG_ECOWATT_DEVICE_ID + "\",\"ts_start\":0,\"ts_end\":0,"
                  "\"seq\":0,\"codec\":\"none\",\"order\":[],\"block_b64\":\"\"}";
      ESP_LOGI(TAG, "upload: no samples");
    }
    // Add FOTA status if present
    if (g_fota_progress.has) {
      uint32_t pct = (g_fota_progress.total>0)
                    ? (uint32_t)((100ULL*g_fota_progress.written)/g_fota_progress.total)
                    : 0;
      // append: "fota":{"progress":pct,"next_chunk": S.next_chunk }
      char buf[96];
      snprintf(buf, sizeof(buf),
        ",\"fota\":{\"progress\":%lu,\"next_chunk\":%lu}",
        (unsigned long)pct, (unsigned long)fota::get_next_chunk_for_cloud());

      if (body_json.back()=='}'){ body_json.pop_back(); body_json += buf; body_json += "}"; }
      g_fota_progress.has = false;
    }
    // One-shot verify/apply report (after finalize)
    if (g_fota_report.has){
      const char* v = g_fota_report.verify_ok ? "ok" : "fail";
      const char* a = g_fota_report.apply_ok  ? "ok" : "fail";
      char buf[96];
      snprintf(buf,sizeof(buf),",\"fota\":{\"verify\":\"%s\",\"apply\":\"%s\"}", v, a);
      if (body_json.back()=='}'){ body_json.pop_back(); body_json += buf; body_json += "}"; }
      g_fota_report.has = false;
    }

    // One-shot boot confirmation (set in app_main after cancel_rollback)
    if (g_fota_bootack.has){
      char buf[64];
      snprintf(buf,sizeof(buf),",\"fota\":{\"boot_ok\":true}");
      if (body_json.back()=='}'){ body_json.pop_back(); body_json += buf; body_json += "}"; }
      g_fota_bootack.has = false;
    }

    
    // Envelope
    std::string psk = CONFIG_ECOWATT_PSK;
    std::string to_send = body_json;
    if (CONFIG_ECOWATT_USE_ENVELOPE) {
      ++g_device_nonce; nvstore::set_u64("sec","nonce_device", g_device_nonce);
      to_send = security::wrap_json_with_hmac(body_json, psk, g_device_nonce);
    }

    std::string reply;
    bool ok = uplink::post_payload_and_get_reply(CONFIG_ECOWATT_CLOUD_BASE_URL, CONFIG_ECOWATT_CLOUD_KEY_B64, to_send, reply);
    ESP_LOGI(TAG, "upload POST ok=%d, reply bytes=%u", ok?1:0, (unsigned)reply.size());

    std::string inner = reply;
    if (ok && CONFIG_ECOWATT_USE_ENVELOPE && !reply.empty()) {
      auto unwrap = security::unwrap_and_verify_envelope(reply, psk, g_last_cloud_nonce, /*server uses b64*/ true);
      if(unwrap){ inner = *unwrap; nvstore::set_u64("sec","nonce_cloud", g_last_cloud_nonce); }
      else { inner.clear(); ESP_LOGW(TAG, "bad HMAC or replay in cloud reply — ignored"); }
    }

    if(!inner.empty()){
      // config_update
      if(inner.find("\"config_update\"") != std::string::npos){
        uint32_t si_sec = 0; auto p = inner.find("\"sampling_interval\"");
        if(p!=std::string::npos){ p = inner.find(':', p); if(p!=std::string::npos) si_sec = std::strtoul(inner.c_str()+p+1,nullptr,10); }
        std::vector<std::string> regs;
        auto rpos = inner.find("\"registers\"");
        if(rpos!=std::string::npos){
          rpos = inner.find('[', rpos); auto r2 = inner.find(']', rpos);
          if(rpos!=std::string::npos && r2!=std::string::npos && r2>rpos){
            std::string arr = inner.substr(rpos+1, r2-rpos-1);
            size_t i=0; while(true){ auto q1=arr.find('"',i); if(q1==std::string::npos) break;
              auto q2=arr.find('"',q1+1); if(q2==std::string::npos) break; regs.push_back(arr.substr(q1+1,q2-q1-1)); i=q2+1; }
          }
        }
        control::RuntimeConfig next = g_cfg_cur;
        if(si_sec>0) next.sampling_interval_ms = si_sec*1000U;
        if(!regs.empty()){ std::vector<control::FieldId> f; if(control::map_field_names(regs, f)) next.fields = f; }
        g_cfg_next = next; g_has_pending_cfg = true;
        ESP_LOGI(TAG, "queued config: sampling=%" PRIu32 "ms fields=%u", g_cfg_next.sampling_interval_ms, (unsigned)g_cfg_next.fields.size());
      }
      // command
      if(inner.find("\"command\"") != std::string::npos){
        auto vpos = inner.find("\"value\""); int val=-1;
        if(vpos!=std::string::npos){ vpos = inner.find(':', vpos); if(vpos!=std::string::npos) val = std::strtol(inner.c_str()+vpos+1, nullptr, 10); }
        if(val>=0){ g_cmd.has_cmd = true; g_cmd.export_pct = val; g_cmd.received_at_ms = now_ms_epoch(); }
      }
      // // FOTA
      // if(inner.find("\"fota\"") != std::string::npos){
      //   auto mpos = inner.find("\"manifest\"");
      //   if(mpos!=std::string::npos){
      //     fota::Manifest mf{};
      //     auto v = inner.find("\"version\"", mpos); if(v!=std::string::npos){ v = inner.find('"', v+9); auto e=inner.find('"', v+1); mf.version = inner.substr(v+1, e-v-1); }
      //     auto s = inner.find("\"size\"", mpos);    if(s!=std::string::npos){ s = inner.find(':', s); mf.size = std::strtoul(inner.c_str()+s+1,nullptr,10); }
      //     auto h = inner.find("\"hash\"", mpos);    if(h!=std::string::npos){ h = inner.find('"', h+6); auto e=inner.find('"', h+1); mf.hash_hex = inner.substr(h+1, e-h-1); }
      //     auto cs= inner.find("\"chunk_size\"", mpos); if(cs!=std::string::npos){ cs = inner.find(':', cs); mf.chunk_size = std::strtoul(inner.c_str()+cs+1,nullptr,10); }
      //     fota::start(mf);
      //   }
      //   // auto cpos = inner.find("\"chunk_number\"");
      //   // if(cpos!=std::string::npos){
      //   //   cpos = inner.find(':', cpos); uint32_t num = std::strtoul(inner.c_str()+cpos+1,nullptr,10);
      //   //   auto d = inner.find("\"data\""); std::string data;
      //   //   if(d!=std::string::npos){ d = inner.find('"', d+6); auto e=inner.find('"', d+1); data = inner.substr(d+1, e-d-1); }
      //   //   if(!data.empty()) fota::ingest_chunk(num, data);
      //   // }
      //   // NEW (anchor the search inside the fota object and after cpos)
      //   auto fpos = inner.find("\"fota\"");
      //   if (fpos != std::string::npos) {
      //     auto cpos = inner.find("\"chunk_number\"", fpos);
      //     if (cpos != std::string::npos) {
      //       auto colon = inner.find(':', cpos);
      //       uint32_t num = std::strtoul(inner.c_str()+colon+1, nullptr, 10);

      //       auto d = inner.find("\"data\"", cpos);   // <-- key line: search AFTER chunk_number
      //       if (d != std::string::npos) {
      //         d = inner.find('"', d + 6);
      //         auto e = inner.find('"', d + 1);
      //         std::string data = inner.substr(d + 1, e - d - 1);
      //         if(!data.empty()) fota::ingest_chunk(num, data);
      //       }
      //     }
      //   }
      // }

    // FOTA
    {
      auto fpos = inner.find("\"fota\"");
      if (fpos != std::string::npos) {

        // Parse manifest if present
        auto mpos = inner.find("\"manifest\"", fpos);
        if (mpos != std::string::npos) {
          fota::Manifest mf{};
          auto v  = inner.find("\"version\"",    mpos);
          auto sz = inner.find("\"size\"",       mpos);
          auto hh = inner.find("\"hash\"",       mpos);
          auto cs = inner.find("\"chunk_size\"", mpos);

          if (v  != std::string::npos) { auto q1 = inner.find('"', v+9);  auto q2 = inner.find('"', q1+1);  mf.version   = inner.substr(q1+1, q2-q1-1); }
          if (sz != std::string::npos) { auto c  = inner.find(':', sz);   mf.size       = std::strtoul(inner.c_str()+c+1, nullptr, 10); }
          if (hh != std::string::npos) { auto q1 = inner.find('"', hh+6); auto q2 = inner.find('"', q1+1);  mf.hash_hex   = inner.substr(q1+1, q2-q1-1); }
          if (cs != std::string::npos) { auto c  = inner.find(':', cs);   mf.chunk_size = std::strtoul(inner.c_str()+c+1, nullptr, 10); }

          fota::start(mf);
        }

        // Parse and ingest chunk if present
        auto cpos = inner.find("\"chunk_number\"", fpos);
        if (cpos != std::string::npos) {
          auto colon = inner.find(':', cpos);
          uint32_t num = std::strtoul(inner.c_str()+colon+1, nullptr, 10);

          // IMPORTANT: search for "data" AFTER the chunk_number occurrence
          auto d = inner.find("\"data\"", cpos);
          if (d != std::string::npos) {
            d = inner.find('"', d + 6);
            auto e = inner.find('"', d + 1);
            if (d != std::string::npos && e != std::string::npos && e > d) {
              std::string data = inner.substr(d + 1, e - d - 1);
              if (!data.empty()) {
                fota::ingest_chunk(num, data);
              }
            }
          }
        }
      }
    }

    }
    
    // // After FOTA manifest/chunk handling in task_uplink()
    //   bool ok_verify=false, ok_apply=false;
    //   if (fota::finalize_and_apply(ok_verify, ok_apply)) {
    //     ESP_LOGI(TAG, "FOTA finalize: verify=%d apply(reboot)=%d", ok_verify?1:0, ok_apply?1:0);
    //   }
    // After FOTA manifest/chunk handling in task_uplink()
    bool ok_verify=false, ok_apply=false;
    if (fota::finalize_and_apply(ok_verify, ok_apply)) {
      // Defer reporting to the next payload
      g_fota_report.has = true;
      g_fota_report.verify_ok = ok_verify;
      g_fota_report.apply_ok  = ok_apply;
      ESP_LOGI(TAG, "FOTA finalize: verify=%d apply(reboot)=%d", ok_verify?1:0, ok_apply?1:0);
    }

    // Execute staged command now; report in next slot
    if(g_cmd.has_cmd){
      bool okw = g_acq->set_export_power(g_cmd.export_pct, "cloud_cmd");
      g_cmd_res.has_result = true; g_cmd_res.success = okw;
      g_cmd_res.executed_at_ms = now_ms_epoch(); g_cmd_res.value = g_cmd.export_pct;
      g_cmd.has_cmd = false;
    } else {
      if(g_cmd_res.has_result){ g_cmd_res.has_result=false; }
    }

    vTaskDelayUntil(&last, period);
  }
}

// ------------------ app_main ------------------
extern "C" void app_main(void) {
  nvstore::init();
  uint64_t tmp=0;
  if(nvstore::get_u64("sec","nonce_device", tmp)) g_device_nonce = tmp;
  if(nvstore::get_u64("sec","nonce_cloud", tmp))  g_last_cloud_nonce = tmp;
  const esp_partition_t* running = esp_ota_get_running_partition();
  esp_ota_img_states_t ota_state;
  if (esp_ota_get_state_partition(running, &ota_state) == ESP_OK &&
      ota_state == ESP_OTA_IMG_PENDING_VERIFY) {
    // Our app has started successfully: mark it valid and cancel rollback
    esp_ota_mark_app_valid_cancel_rollback();
    ESP_LOGI(TAG, "OTA image marked valid (cancel rollback).");
    g_fota_bootack.has = true;
  }
  ESP_ERROR_CHECK(nvs_flash_init());
  wifi_start_and_wait_ip();
  ntp_start_and_wait_blocking(60000);

  static acquisition::Acquisition acq(CONFIG_ECOWATT_API_BASE_URL, CONFIG_ECOWATT_API_KEY_B64);
  g_acq = &acq;
  acq.set_export_power(10, "boot");

  g_cfg_cur.sampling_interval_ms = CONFIG_ECOWATT_SAMPLE_PERIOD_MS;
  g_cfg_next = g_cfg_cur;

  const size_t cap = (CONFIG_ECOWATT_UPLOAD_INTERVAL_SEC * 1000 / CONFIG_ECOWATT_SAMPLE_PERIOD_MS) + 16;
  static buffer::Ring ring(cap);
  g_ring = &ring; g_ring_mtx = xSemaphoreCreateMutex();

  fota::init();

  xTaskCreatePinnedToCore(task_acq,   "acq",   4096, nullptr, 5, nullptr, 0);
  xTaskCreatePinnedToCore(task_uplink,"uplink",8192, nullptr, 5, nullptr, 0);
}
