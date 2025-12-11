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
#define CONFIG_ECOWATT_CLOUD_BASE_URL "http://192.168.8.195:5000"
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
#ifndef CONFIG_ECOWATT_PM_MIN_FREQ_MHZ
#define CONFIG_ECOWATT_PM_MIN_FREQ_MHZ 40
#endif
#ifndef CONFIG_ECOWATT_PM_MAX_FREQ_MHZ
#define CONFIG_ECOWATT_PM_MAX_FREQ_MHZ 160
#endif
#ifndef CONFIG_ECOWATT_WIFI_LISTEN_INTERVAL
#define CONFIG_ECOWATT_WIFI_LISTEN_INTERVAL 10
#endif
#ifndef CONFIG_ECOWATT_WIFI_PS_MODE
// 1 = MIN_MODEM (default, saves power), 0 = NONE (max throughput)
#define CONFIG_ECOWATT_WIFI_PS_MODE 1
#endif

#ifndef CONFIG_ECOWATT_WIFI_GATE_BETWEEN_UPLOADS
// 0 = keep Wi-Fi associated between uploads (default), 1 = stop/start
#define CONFIG_ECOWATT_WIFI_GATE_BETWEEN_UPLOADS 0
#endif

#ifndef CONFIG_ECOWATT_SLEEP_MARGIN_MS
// guard time before deadline so we don't oversleep
#define CONFIG_ECOWATT_SLEEP_MARGIN_MS 30
#endif

#ifndef CONFIG_ECOWATT_SAFE_MIN_SLEEP_MS
#define CONFIG_ECOWATT_SAFE_MIN_SLEEP_MS 50
#endif

#ifndef CONFIG_ECOWATT_WIFI_GATE_MIN_INTERVAL_SEC
// Min upload interval (seconds) required to safely gate Wi-Fi between uploads
#define CONFIG_ECOWATT_WIFI_GATE_MIN_INTERVAL_SEC 30
#endif

#ifndef CONFIG_ECOWATT_PS_BURST_TOGGLE
#define CONFIG_ECOWATT_PS_BURST_TOGGLE 0   // keep 0 to avoid AP hiccups
#endif
#ifndef CONFIG_ECOWATT_MANUAL_LIGHT_SLEEP
#define CONFIG_ECOWATT_MANUAL_LIGHT_SLEEP 0
#endif

#ifndef CONFIG_ECOWATT_ENABLE_AUTO_LIGHT_SLEEP
#define CONFIG_ECOWATT_ENABLE_AUTO_LIGHT_SLEEP 0
#endif

// =========================================================
#include <cstdio>   
#include <cstdlib> 
#include <cstring>
#include <string>
#include <sys/time.h>
#include <cinttypes>
#include <inttypes.h>
#include <algorithm>
#include <vector>
#include <inttypes.h>

#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_sntp.h"
#include "esp_pm.h"
#include "esp_sleep.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"

#include "acquisition.hpp"
#include "buffer.hpp"
#include "packetizer.hpp"
#include "codec.hpp"
#include "security.hpp"
#include "transport.hpp"
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

// Helper to check if sleep features should be enabled
static inline bool sleep_features_enabled() {
  return (g_cfg_cur.sampling_interval_ms >= CONFIG_ECOWATT_SLEEP_FEATURE_THRESHOLD_MS);
}

// NEW (M4): one-shot config_ack to merge into next payload
static std::string g_cfg_ack_json;
static bool g_cfg_ack_ready = false;

static control::PendingCommand g_cmd{};
static control::CommandResult  g_cmd_res{};

static uint64_t g_device_nonce = 0;
static uint64_t g_last_cloud_nonce = 0;

static esp_pm_lock_handle_t s_pm_lock = nullptr;

static uint64_t g_idle_budget_ms = 0;

static acquisition::Acquisition* g_acq = nullptr;
static uint32_t g_dropped_samples = 0;
static uint32_t g_acq_failures = 0;
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

struct {
  bool has = false;
  std::string reason = "";  // "corruption" or "boot_failed"
  std::string version = "";
} g_fota_failure;

// --- Power instrumentation ---
struct power_stats_t {
  uint64_t t_sleep_ms  = 0;       // manual light-sleep time
  uint64_t t_auto_sleep_us = 0;  // auto light-sleep time (from PM)
  uint64_t t_uplink_ms = 0;
  uint32_t uplink_bytes = 0;
} g_pwr;


// ---- Fault/event log (cleared after each successful upload) ----
static std::vector<std::string> g_events;
static inline void log_event(const char* e){ g_events.emplace_back(e); }
static inline void log_eventf(const char* tag, int v){
  char b[64]; snprintf(b,sizeof(b),"%s:%d",tag,v); g_events.emplace_back(b);
}

// ---- SIM Fault tracking ----
struct {
  bool has_fault = false;
  std::string fault_type = "";     // "exception", "crc_error", "corrupt", "packet_drop", "timeout"
  uint8_t exception_code = 0;      // Modbus exception code (01-0B)
  std::string last_error = "";     // Description of last fault
} g_sim_fault;


static inline uint64_t now_ms() { return (uint64_t)esp_timer_get_time()/1000ULL; }

static void eco_light_sleep_until(uint64_t wake_at_ms) {
  uint64_t now = now_ms();
  if (wake_at_ms <= now + CONFIG_ECOWATT_SLEEP_MARGIN_MS) return;
  uint64_t delta_ms = wake_at_ms - now - CONFIG_ECOWATT_SLEEP_MARGIN_MS;
  esp_sleep_enable_timer_wakeup(delta_ms * 1000ULL);
  uint64_t t0 = now_ms();
  esp_light_sleep_start();
  g_pwr.t_sleep_ms += (now_ms() - t0);
}

// Called by fota.cpp after each accepted chunk
extern "C" void fota_progress_notify(uint32_t written, uint32_t total){
  g_fota_progress.has   = true;
  g_fota_progress.written = written;
  g_fota_progress.total   = total;
}

// Helper: pull last FOTA error string from fota::status_json() for retry hint
// Called by acquisition when SIM faults occur
extern "C" void sim_fault_notify(const char* fault_type, uint8_t exception_code, const char* description){
  g_sim_fault.has_fault = true;
  g_sim_fault.fault_type = fault_type ? fault_type : "unknown";
  g_sim_fault.exception_code = exception_code;
  g_sim_fault.last_error = description ? description : "";
  log_event(("sim_fault:" + g_sim_fault.fault_type).c_str());
  ESP_LOGE(TAG, "SIM FAULT: type=%s exc=0x%02x desc=%s", 
           g_sim_fault.fault_type.c_str(), exception_code, description ? description : "");
}

static std::string fota_pull_error_string(){
  std::string s = fota::status_json();
  auto k = s.find("\"error\":\"");
  if (k == std::string::npos) return {};
  k += 9; auto e = s.find('"', k);
  if (e == std::string::npos || e <= k) return {};
  return s.substr(k, e-k);
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
  
  sta.sta.listen_interval = CONFIG_ECOWATT_WIFI_LISTEN_INTERVAL; // helps AP buffer beacons
  ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta));
  #if CONFIG_ECOWATT_WIFI_PS_MODE == 1
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_MIN_MODEM));  // sip power when idle
  #else
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));       // max throughput baseline
  #endif
  ESP_ERROR_CHECK(esp_wifi_start());

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
  if (bits & BIT_NTP_OK) {
    esp_sntp_stop();   // gate SNTP after first successful sync
  } else {
    ESP_LOGW(TAG, "NTP sync timed out; acquisition continues without epoch offset");
  }
  // if ((bits & BIT_NTP_OK) == 0) ESP_LOGW(TAG, "NTP sync timed out; acquisition continues without epoch offset");
}

// ------------------ Tasks ------------------
static void task_acq(void*){
  if ((xEventGroupGetBits(s_evt) & BIT_NTP_OK) == 0) {
    xEventGroupWaitBits(s_evt, BIT_NTP_OK, pdFALSE, pdFALSE, pdMS_TO_TICKS(60000));
  }
  TickType_t last = xTaskGetTickCount();
  uint32_t period_ms = g_cfg_cur.sampling_interval_ms;
  TickType_t period_ticks = pdMS_TO_TICKS(period_ms);

  while (true) {
    uint64_t loop_start_ms = now_ms();

    acquisition::Sample s{};
    std::vector<int> fids; for (auto f: g_cfg_cur.fields) fids.push_back((int)f);
    bool ok = false;
    if (!fids.empty()) ok = g_acq->read_selected(fids, s); else ok = g_acq->read_all(s);

    if (!ok) {
      ++g_acq_failures;
      // log event only on repeated failures to avoid log spam
      if ((g_acq_failures % 3) == 0) log_event("acq_read_fail");
    }

    buffer::Record rec{ now_ms_epoch(), s };
    xSemaphoreTake(g_ring_mtx, portMAX_DELAY);
    bool overflow = g_ring->push(rec);
    xSemaphoreGive(g_ring_mtx);
    if (overflow) { ++g_dropped_samples; log_event("buffer_overflow"); }

    ESP_LOGI(TAG, "ACQ tick @ %" PRIu64 " ms (epoch)", rec.epoch_ms);

  //   // (optional) manual sleep is OFF for now, so this block is skipped
  // #if CONFIG_ECOWATT_MANUAL_LIGHT_SLEEP
  //   uint64_t next_tick_ms = loop_start_ms + (uint64_t)period_ms;
  //   eco_light_sleep_until(next_tick_ms);
  // #endif

  // Idle budget = how much of this period we didn't use
  uint64_t work_ms = now_ms() - loop_start_ms;
  if (work_ms < period_ms) g_idle_budget_ms += (period_ms - work_ms);

  // inside task_acq loop, after work_ms computed
  #if CONFIG_ECOWATT_MANUAL_LIGHT_SLEEP
    if (sleep_features_enabled()) {
      const uint32_t safe_min_sleep_ms = 50; // minimal useful sleep (tune with measurements)
      uint64_t next_tick_ms = loop_start_ms + (uint64_t)period_ms;
      uint64_t now = now_ms();
      if (next_tick_ms > now + CONFIG_ECOWATT_SLEEP_MARGIN_MS + safe_min_sleep_ms) {
        eco_light_sleep_until(next_tick_ms);
      } // else skip sleeping because window too small
    }
  #endif
    vTaskDelayUntil(&last, period_ticks);

    if (period_ms != g_cfg_cur.sampling_interval_ms) {
      period_ms    = g_cfg_cur.sampling_interval_ms;
      period_ticks = pdMS_TO_TICKS(period_ms);
    }
  }

}

static void task_uplink(void*){
  TickType_t last = xTaskGetTickCount();
  const TickType_t period = pdMS_TO_TICKS(CONFIG_ECOWATT_UPLOAD_INTERVAL_SEC * 1000);

  while(true){
    // Apply pending runtime config (takes effect "after next upload")
    if(g_has_pending_cfg){
      g_cfg_cur = g_cfg_next;
      g_has_pending_cfg = false;
      std::string cfg_json = "{\"sampling_interval\":" + std::to_string(g_cfg_cur.sampling_interval_ms/1000) + "}";
      nvstore::set_str("cfg","runtime", cfg_json);
    }

    // Snapshot buffer
    std::vector<buffer::Record> batch;
    xSemaphoreTake(g_ring_mtx, portMAX_DELAY);
    batch = g_ring->snapshot_and_clear();
    xSemaphoreGive(g_ring_mtx);

    // Build base payload
    std::string body_json;
    if(!batch.empty()){
      codec::BenchResult br = codec::run_benchmark_delta_rle_v1(batch);
      double ratio = (br.orig_bytes>0)? double(br.comp_bytes)/double(br.orig_bytes) : 0.0;
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
    // FOTA failure report (corruption or boot failure)
    if (g_fota_failure.has) {
      char buf[160];
      snprintf(buf, sizeof(buf), ",\"fota\":{\"failure\":{\"reason\":\"%s\",\"version\":\"%s\"}}", 
               g_fota_failure.reason.c_str(), g_fota_failure.version.c_str());
      if (body_json.back()=='}'){ body_json.pop_back(); body_json += buf; body_json += "}"; }
      g_fota_failure.has = false;
    }
    // One-shot boot confirmation (set in app_main after cancel_rollback)
    if (g_fota_bootack.has){
      char buf[64];
      snprintf(buf,sizeof(buf),",\"fota\":{\"boot_ok\":true}");
      if (body_json.back()=='}'){ body_json.pop_back(); body_json += buf; body_json += "}"; }
      g_fota_bootack.has = false;
    }

    // NEW (M4): append one-shot config_ack if staged
    if (g_cfg_ack_ready && !g_cfg_ack_json.empty()) {
      if (body_json.back()=='}') {
        body_json.pop_back();
        // Strip braces from g_cfg_ack_json and merge
        std::string ack = g_cfg_ack_json;
        if (!ack.empty() && ack.front()=='{') ack.erase(0,1);
        if (!ack.empty() && ack.back()=='}')  ack.pop_back();
        body_json += "," + ack + "}";
      }
      g_cfg_ack_ready = false;
      g_cfg_ack_json.clear();
    }

    // SIM Fault reporting
    if (g_sim_fault.has_fault) {
      char buf[256];
      snprintf(buf, sizeof(buf), ",\"sim_fault\":{\"type\":\"%s\",\"exception_code\":%u,\"description\":\"%s\"}",
               g_sim_fault.fault_type.c_str(), (unsigned)g_sim_fault.exception_code, g_sim_fault.last_error.c_str());
      if (body_json.back()=='}'){ body_json.pop_back(); body_json += buf; body_json += "}"; }
      g_sim_fault.has_fault = false;
    }

    // expose compact FOTA error + next_chunk for retry-friendly server behavior
    {
      std::string fe = fota_pull_error_string();
      if (!fe.empty()) {
        log_event(("fota_err:" + fe).c_str());
        if (body_json.back()=='}') {
          char buf[160];
          snprintf(buf, sizeof(buf), ",\"fota\":{\"error\":\"%s\",\"next_chunk\":%lu}",
                   fe.c_str(), (unsigned long)fota::get_next_chunk_for_cloud());
          body_json.pop_back(); body_json += buf; body_json += "}";
        }
      }
    }
    // Estimate auto-sleep time: when auto light-sleep is enabled, the PM automatically
    // sleeps during idle periods. We can estimate this as (idle_budget - manual_sleep - uplink).
    // This is an approximation since some idle time may be spent in frequency scaling rather than sleep.
    #if CONFIG_ECOWATT_ENABLE_AUTO_LIGHT_SLEEP
    {
      // Auto-sleep happens during idle periods when not manually sleeping
      // A reasonable estimate: idle_budget represents available sleep window,
      // manual_sleep is what we explicitly slept, the rest is handled by PM auto-sleep
      // Note: This is a heuristic; actual auto-sleep may be less due to interrupts
      if (g_idle_budget_ms > g_pwr.t_sleep_ms) {
        // Estimate ~70% of remaining idle time was spent in auto light-sleep
        // (the rest is task switching, frequency scaling, interrupt handling)
        uint64_t remaining_idle_ms = g_idle_budget_ms - g_pwr.t_sleep_ms;
        g_pwr.t_auto_sleep_us = remaining_idle_ms * 700ULL;  // 70% in microseconds
      }
    }
    #endif

    // Append power stats (rolling) into the payload root
    {
      if (body_json.back()=='}') {
        // Combine manual + auto sleep time into total sleep
        uint64_t total_sleep_ms = g_pwr.t_sleep_ms + (g_pwr.t_auto_sleep_us / 1000ULL);
        char buf[256];
        snprintf(buf, sizeof(buf),
          ",\"power_stats\":{"
            "\"idle_budget_ms\":%" PRIu64 ","
            "\"t_sleep_ms\":%" PRIu64 ","
            "\"t_manual_sleep_ms\":%" PRIu64 ","
            "\"t_auto_sleep_ms\":%" PRIu64 ","
            "\"t_uplink_ms\":%" PRIu64 ","
            "\"uplink_bytes\":%u"
          "}",
          (unsigned long long)g_idle_budget_ms,
          (unsigned long long)total_sleep_ms,
          (unsigned long long)g_pwr.t_sleep_ms,
          (unsigned long long)(g_pwr.t_auto_sleep_us / 1000ULL),
          (unsigned long long)g_pwr.t_uplink_ms,
          (unsigned)g_pwr.uplink_bytes);
        body_json.pop_back(); body_json += buf; body_json += "}";
        g_pwr = power_stats_t{};          // reset rolling counters
        g_idle_budget_ms = 0;             // reset idle budget
      }
    }
    // Append diagnostic counters (dropped samples, acquisition failures, transport failures)
    if (body_json.back()=='}') {
      char dbuf[128];
      snprintf(dbuf, sizeof(dbuf), ",\"diag\":{\"dropped_samples\":%u,\"acq_failures\":%u,\"transport_failures\":%u}",
               (unsigned)g_dropped_samples, (unsigned)g_acq_failures, (unsigned)transport::get_conn_failures());
      body_json.pop_back(); body_json += dbuf; body_json += "}";
      // reset dropped counter after reporting
      g_dropped_samples = 0;
    }
      // Append events[] (then clear)
      if (!g_events.empty() && body_json.back()=='}') {
        body_json.pop_back();
        body_json += ",\"events\":[";
        for (size_t i=0;i<g_events.size();++i){
          body_json += "\"";
          // naive JSON escape for quotes/backslashes:
          for (char c: g_events[i]) { if (c=='\\' || c=='"') body_json += '\\'; body_json += c; }
          body_json += "\"";
          if (i+1<g_events.size()) body_json += ",";
        }
        body_json += "]}";
        g_events.clear();
    }

    // Envelope
    std::string psk = CONFIG_ECOWATT_PSK;
    std::string to_send = body_json;
    if (CONFIG_ECOWATT_USE_ENVELOPE) {
      ++g_device_nonce;
      nvstore::set_u64("sec","nonce_device", g_device_nonce);
      to_send = security::wrap_json_with_hmac(body_json, psk, g_device_nonce);
    }

    // POST (keep CPU at max during network burst)
    if (s_pm_lock) esp_pm_lock_acquire(s_pm_lock);   // <-- acquire AFTER to_send is ready

    // Optionally gate Wi-Fi between uploads if configured and interval is large enough
    bool can_gate_wifi = (CONFIG_ECOWATT_WIFI_GATE_BETWEEN_UPLOADS == 1) &&
                         (CONFIG_ECOWATT_UPLOAD_INTERVAL_SEC >= CONFIG_ECOWATT_WIFI_GATE_MIN_INTERVAL_SEC);

    if (can_gate_wifi) {
      // ensure Wi-Fi is started and connected before uploading
      if ((xEventGroupGetBits(s_evt) & BIT_GOT_IP) == 0) {
        esp_wifi_start();
        esp_wifi_connect();
        // wait up to 20s for IP
        (void)xEventGroupWaitBits(s_evt, BIT_GOT_IP, pdFALSE, pdFALSE, pdMS_TO_TICKS(20000));
      }
    }

    // Temporarily disable Wi-Fi PS for throughput during upload if burst toggle enabled
    #if CONFIG_ECOWATT_PS_BURST_TOGGLE
      esp_wifi_set_ps(WIFI_PS_NONE);   // BEFORE POST
    #endif
    uint64_t t0 = now_ms();
    std::string reply;
    bool ok = uplink::post_payload_and_get_reply(
              CONFIG_ECOWATT_CLOUD_BASE_URL,
              CONFIG_ECOWATT_CLOUD_KEY_B64,
              to_send,
              reply);
    g_pwr.t_uplink_ms += (now_ms() - t0);
    g_pwr.uplink_bytes += (uint32_t)to_send.size();


    // ESP_LOGI(TAG, "[PWR-DBG] idle=%" PRIu64 " sleep=%" PRIu64 " uplink=%" PRIu64 " bytes=%u",
    //         (unsigned long long)g_idle_budget_ms,
    //         (unsigned long long)g_pwr.t_sleep_ms,
    //         (unsigned long long)g_pwr.t_uplink_ms,
    //         (unsigned)g_pwr.uplink_bytes);

    ESP_LOGI(TAG, "[PWR-DBG]   uplink=%" PRIu64 " bytes=%u",
            (unsigned long long)g_pwr.t_uplink_ms,
            (unsigned)g_pwr.uplink_bytes);

    // // Restore idle PS mode right after the burst
    // #if CONFIG_ECOWATT_WIFI_PS_MODE == 1
    //   esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
    // #endif
    #if CONFIG_ECOWATT_PS_BURST_TOGGLE
      esp_wifi_set_ps(WIFI_PS_MIN_MODEM); // AFTER POST
    #endif

    if (can_gate_wifi) {
      // stop Wi-Fi until next upload to save power
      esp_wifi_stop();
      xEventGroupClearBits(s_evt, BIT_GOT_IP | BIT_CONNECTED);
    }

    if (s_pm_lock) esp_pm_lock_release(s_pm_lock);   // release immediately after POST

    ESP_LOGI(TAG, "upload POST ok=%d, reply bytes=%u", ok?1:0, (unsigned)reply.size());

    std::string inner = reply;
    if (ok && CONFIG_ECOWATT_USE_ENVELOPE && !reply.empty()) {
      auto unwrap = security::unwrap_and_verify_envelope(reply, psk, g_last_cloud_nonce, /*server uses b64*/ true);
      if(unwrap){ inner = *unwrap; nvstore::set_u64("sec","nonce_cloud", g_last_cloud_nonce); }
      else { inner.clear(); ESP_LOGW(TAG, "bad HMAC or replay in cloud reply — ignored"); }
    }

    if(!inner.empty()){
      // --- CONFIG UPDATE with ACK build ---
      if(inner.find("\"config_update\"") != std::string::npos){
        uint32_t si_sec = 0; 
        auto p = inner.find("\"sampling_interval\"");
        if(p!=std::string::npos){ p = inner.find(':', p); if(p!=std::string::npos) si_sec = std::strtoul(inner.c_str()+p+1,nullptr,10); }

        std::vector<std::string> regs_in;
        auto rpos = inner.find("\"registers\"");
        if(rpos!=std::string::npos){
          rpos = inner.find('[', rpos); auto r2 = inner.find(']', rpos);
          if(rpos!=std::string::npos && r2!=std::string::npos && r2>rpos){
            std::string arr = inner.substr(rpos+1, r2-rpos-1);
            size_t i=0; while(true){ auto q1=arr.find('"',i); if(q1==std::string::npos) break;
              auto q2=arr.find('"',q1+1); if(q2==std::string::npos) break; regs_in.push_back(arr.substr(q1+1,q2-q1-1)); i=q2+1; }
          }
        }

        control::RuntimeConfig next = g_cfg_cur;
        std::vector<control::FieldId> fids_new;
        bool regs_valid = regs_in.empty() ? true : control::map_field_names(regs_in, fids_new);

        std::vector<std::string> accepted, rejected, unchanged;

        // sampling_interval decision
        if (si_sec == 0) {
          unchanged.push_back("sampling_interval");
        } else {
          uint32_t want_ms = si_sec * 1000U;
          if (want_ms == g_cfg_cur.sampling_interval_ms) unchanged.push_back("sampling_interval");
          else { next.sampling_interval_ms = want_ms; accepted.push_back("sampling_interval"); }
        }

        // registers decision
        if (regs_in.empty()) {
          unchanged.push_back("registers");
        } else if (!regs_valid) {
          rejected.push_back("registers");
        } else {
          std::vector<int> cur_ids; for (auto f : g_cfg_cur.fields) cur_ids.push_back((int)f);
          std::vector<int> new_ids; for (auto f : fids_new)        new_ids.push_back((int)f);
          std::sort(cur_ids.begin(),cur_ids.end());
          std::sort(new_ids.begin(),new_ids.end());
          if (cur_ids == new_ids) unchanged.push_back("registers");
          else { next.fields = fids_new; accepted.push_back("registers"); }
        }
        for (auto& k : accepted)  log_event(("cfg_ok:"  + k).c_str());
        for (auto& k : rejected)  log_event(("cfg_bad:" + k).c_str());
        // apply-at-next-slot
        g_cfg_next = next; 
        g_has_pending_cfg = true;

        // stage config_ack for next payload
        auto join = [](const std::vector<std::string>& v) {
          std::string s="["; for (size_t i=0;i<v.size();++i){ s+="\""+v[i]+"\""; if(i+1<v.size()) s+=","; } s+="]"; return s;
        };
        g_cfg_ack_json = std::string("{\"config_ack\":{\"accepted\":") + join(accepted)
                       + ",\"rejected\":"  + join(rejected)
                       + ",\"unchanged\":" + join(unchanged) + "}}";
        g_cfg_ack_ready = true;

        ESP_LOGI(TAG, "queued config: sampling=%" PRIu32 "ms fields=%u (ack prepared)",
                 g_cfg_next.sampling_interval_ms, (unsigned)g_cfg_next.fields.size());
      }

      // command
      if(inner.find("\"command\"") != std::string::npos){
        auto vpos = inner.find("\"value\""); int val=-1;
        if(vpos!=std::string::npos){ vpos = inner.find(':', vpos); if(vpos!=std::string::npos) val = std::strtol(inner.c_str()+vpos+1, nullptr, 10); }
        if(val>=0){ g_cmd.has_cmd = true; g_cmd.export_pct = val; g_cmd.received_at_ms = now_ms_epoch();log_eventf("cmd_export_pct", val); }
      }

      // FOTA (manifest + chunk)
      {
        auto fpos = inner.find("\"fota\"");
        if (fpos != std::string::npos) {
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

          auto cpos = inner.find("\"chunk_number\"", fpos);
          if (cpos != std::string::npos) {
            auto colon = inner.find(':', cpos);
            uint32_t num = std::strtoul(inner.c_str()+colon+1, nullptr, 10);

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

    // FOTA finalize (verify + apply)
    bool ok_verify=false, ok_apply=false;
    if (fota::finalize_and_apply(ok_verify, ok_apply)) {
      // Defer reporting to the next payload
      g_fota_report.has = true;
      g_fota_report.verify_ok = ok_verify;
      g_fota_report.apply_ok  = ok_apply;
      ESP_LOGI(TAG, "FOTA finalize: verify=%d apply(reboot)=%d", ok_verify?1:0, ok_apply?1:0);
    } else {
      // Check if this was a verification failure (corruption)
      fota::FotaStatus status = fota::get_current_status();
      if (status == fota::FotaStatus::VERIFY_FAILED) {
        std::string failed_ver = fota::get_failed_version();
        if (!failed_ver.empty()) {
          g_fota_failure.has = true;
          g_fota_failure.reason = "corruption_detected";
          g_fota_failure.version = failed_ver;
          log_event(("fota_corruption:" + failed_ver).c_str());
          ESP_LOGE(TAG, "FOTA FAILURE: Image corruption detected for version %s. Rollback to previous version.",
                   failed_ver.c_str());
        }
      }
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
  ESP_ERROR_CHECK(esp_pm_lock_create(ESP_PM_CPU_FREQ_MAX, 0, "uplink", &s_pm_lock));

  esp_pm_config_t pmcfg = {
    .max_freq_mhz = CONFIG_ECOWATT_PM_MAX_FREQ_MHZ,
    .min_freq_mhz = CONFIG_ECOWATT_PM_MIN_FREQ_MHZ,
    .light_sleep_enable = (bool)(sleep_features_enabled() && CONFIG_ECOWATT_ENABLE_AUTO_LIGHT_SLEEP)
  };
  ESP_ERROR_CHECK(esp_pm_configure(&pmcfg));
  ESP_LOGI(TAG, "PM configured: max=%uMHz min=%uMHz auto_sleep=%s (threshold=%u, actual=%u)",
           (unsigned)CONFIG_ECOWATT_PM_MAX_FREQ_MHZ, (unsigned)CONFIG_ECOWATT_PM_MIN_FREQ_MHZ,
           (sleep_features_enabled() && CONFIG_ECOWATT_ENABLE_AUTO_LIGHT_SLEEP) ? "ON" : "OFF",
           (unsigned)CONFIG_ECOWATT_SLEEP_FEATURE_THRESHOLD_MS, (unsigned)g_cfg_cur.sampling_interval_ms);


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

  // Tune retry/backoff policies for transport and cloud uplink
  transport::set_retry_policy(3, 200 /*ms base*/, 2000 /*ms max*/);
  uplink::set_retry_policy(3, 1000 /*ms base*/, 4000 /*ms max*/);

  xTaskCreatePinnedToCore(task_acq,   "acq",   4096, nullptr, 5, nullptr, 0);
  xTaskCreatePinnedToCore(task_uplink,"uplink",8192, nullptr, 5, nullptr, 0);
}
