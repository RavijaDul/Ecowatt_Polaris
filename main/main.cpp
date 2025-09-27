// ===== must be first so Kconfig macros are visible =====
#include "sdkconfig.h" // L1: include the auto-generated configuration header from ESP-IDF build

// Back-compat guards (silence IntelliSense/old IDF diffs)
#ifndef CONFIG_LOG_MAXIMUM_LEVEL
#define CONFIG_LOG_MAXIMUM_LEVEL CONFIG_LOG_DEFAULT_LEVEL // L6–L7: ensure logging macros exist
#endif
#ifndef CONFIG_FREERTOS_HZ
#define CONFIG_FREERTOS_HZ configTICK_RATE_HZ // L10–L11: fallback for tick frequency
#endif
#ifndef CONFIG_ECOWATT_NTP_SERVER
#define CONFIG_ECOWATT_NTP_SERVER "pool.ntp.org" // L14–L15: default NTP server
#endif
// =======================================================

#include <cstring>    // L19: for strncpy
#include <string>     // L20: C++ std::string
#include <sys/time.h> // L21: for gettimeofday

#include "esp_wifi.h"   // L23: Wi-Fi functions
#include "esp_event.h"  // L24: event loop
#include "esp_log.h"    // L25: logging macros
#include "nvs_flash.h"  // L26: nonvolatile storage
#include "esp_netif.h"  // L27: network interface
#include "esp_timer.h"  // L28: monotonic timer
#include "esp_sntp.h"   // L29: NTP client

#include "freertos/FreeRTOS.h"  // L31: FreeRTOS core
#include "freertos/task.h"      // L32: FreeRTOS tasks
#include "freertos/event_groups.h" // L33: FreeRTOS event groups
#include "freertos/semphr.h"       // L34: FreeRTOS semaphores

#include "acquisition.hpp" // L36: acquisition class
#include "buffer.hpp"      // L37: ring buffer
#include "packetizer.hpp"  // L38: payload builder for uplink

// ---- Kconfig fallbacks ----
#ifndef CONFIG_ECOWATT_WIFI_SSID
#define CONFIG_ECOWATT_WIFI_SSID "YOUR_WIFI_SSID" // L42–L43: default Wi-Fi SSID
#endif
#ifndef CONFIG_ECOWATT_WIFI_PASS
#define CONFIG_ECOWATT_WIFI_PASS "YOUR_WIFI_PASSWORD" // L45–L46: default Wi-Fi password
#endif
#ifndef CONFIG_ECOWATT_API_BASE_URL
#define CONFIG_ECOWATT_API_BASE_URL "http://20.15.114.131:8080" // L48–L49: SIM base URL
#endif
#ifndef CONFIG_ECOWATT_API_KEY_B64
#define CONFIG_ECOWATT_API_KEY_B64 "" // L51–L52: API key for SIM
#endif
#ifndef CONFIG_ECOWATT_CLOUD_BASE_URL
#define CONFIG_ECOWATT_CLOUD_BASE_URL "http://192.168.1.100:5000" // L54–L55: Cloud server URL
#endif
#ifndef CONFIG_ECOWATT_CLOUD_KEY_B64
#define CONFIG_ECOWATT_CLOUD_KEY_B64 "" // L57–L58: Cloud API key
#endif
#ifndef CONFIG_ECOWATT_UPLOAD_INTERVAL_SEC
#define CONFIG_ECOWATT_UPLOAD_INTERVAL_SEC 15 // L60–L61: default upload window
#endif
#ifndef CONFIG_ECOWATT_SAMPLE_PERIOD_MS
#define CONFIG_ECOWATT_SAMPLE_PERIOD_MS 5000 // L63–L64: sample every 5 seconds
#endif
#ifndef CONFIG_ECOWATT_DEVICE_ID
#define CONFIG_ECOWATT_DEVICE_ID "EcoWatt-Dev-01" // L66–L67: device identifier
#endif
// -----------------------------------------------------

static const char* TAG = "main"; // L70: log tag
static EventGroupHandle_t s_evt; // L71: event group handle
static constexpr int BIT_CONNECTED = BIT0; // L72: bitmask for Wi-Fi connected
static constexpr int BIT_GOT_IP   = BIT1; // L73: bitmask for IP received
static constexpr int BIT_NTP_OK   = BIT2; // L74: bitmask for NTP sync done

// Shared ring + mutex
static buffer::Ring* g_ring = nullptr; // L77: pointer to shared ring buffer
static SemaphoreHandle_t g_ring_mtx = nullptr; // L78: mutex to guard ring

// epoch offset (epoch_ms = monotonic_ms + offset_ms)
static int64_t s_epoch_offset_ms = 0; // L81: offset between monotonic and epoch
static inline uint64_t monotonic_ms(){ return (uint64_t)esp_timer_get_time() / 1000ULL; } // L82–L83: read monotonic ms
static inline uint64_t now_ms_epoch(){ return (uint64_t)((int64_t)monotonic_ms() + s_epoch_offset_ms); } // L84–L85: convert to epoch ms

// Wi-Fi events
static void on_wifi(void*, esp_event_base_t base, int32_t id, void*) { // L88–L89: Wi-Fi event handler
    if (base != WIFI_EVENT) return; // L90: ignore non-WiFi events
    switch (id) { // L91: handle Wi-Fi event ID
        case WIFI_EVENT_STA_START:      esp_wifi_connect(); break; // L92: when started, connect
        case WIFI_EVENT_STA_CONNECTED:  xEventGroupSetBits(s_evt, BIT_CONNECTED); ESP_LOGI(TAG, "Wi-Fi associated"); break; // L93: log association
        case WIFI_EVENT_STA_DISCONNECTED:
            xEventGroupClearBits(s_evt, BIT_CONNECTED | BIT_GOT_IP); // L95: clear status bits
            ESP_LOGW(TAG, "Wi-Fi disconnected — reconnecting…"); // L96: warn
            esp_wifi_connect(); // L97: reconnect
            break;
        default: break; // L98: ignore others
    }
}
static void on_ip(void*, esp_event_base_t base, int32_t id, void* data) { // L101: IP event handler
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) { // L102: check IP event
        auto* e = static_cast<ip_event_got_ip_t*>(data); // L103: cast event
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&e->ip_info.ip)); // L104: log IP
        xEventGroupSetBits(s_evt, BIT_GOT_IP); // L105: set IP bit
    }
}
static void wifi_start_and_wait_ip() { // L107: Wi-Fi init + block until IP
    ESP_ERROR_CHECK(esp_netif_init()); // L108: initialize netif
    ESP_ERROR_CHECK(esp_event_loop_create_default()); // L109: create event loop
    esp_netif_create_default_wifi_sta(); // L110: create default station

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT(); // L112: default Wi-Fi config
    ESP_ERROR_CHECK(esp_wifi_init(&cfg)); // L113: init Wi-Fi

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &on_wifi, nullptr, nullptr)); // L115: register Wi-Fi handler
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &on_ip, nullptr, nullptr)); // L116: register IP handler

    wifi_config_t sta{}; // L118: zero-init Wi-Fi STA config
    std::strncpy(reinterpret_cast<char*>(sta.sta.ssid), CONFIG_ECOWATT_WIFI_SSID, sizeof(sta.sta.ssid)-1); // L119–L120: set SSID
    std::strncpy(reinterpret_cast<char*>(sta.sta.password), CONFIG_ECOWATT_WIFI_PASS, sizeof(sta.sta.password)-1); // L121–L122: set password
    sta.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK; // L123: force WPA2

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA)); // L125: set mode STA
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta)); // L126: apply config
    ESP_ERROR_CHECK(esp_wifi_start()); // L127: start Wi-Fi

    s_evt = xEventGroupCreate(); // L129: create event group
    (void)xEventGroupWaitBits(s_evt, BIT_GOT_IP, pdFALSE, pdFALSE, pdMS_TO_TICKS(20000)); // L130: wait up to 20s
}

// --------- NTP sync ---------
static void sntp_sync_cb(struct timeval *tv) { // L134: callback when NTP sync
    (void)tv; // L135: unused
    struct timeval now{}; // L136: current time
    gettimeofday(&now, nullptr); // L137: fill struct
    int64_t epoch_ms = (int64_t)now.tv_sec * 1000LL + (now.tv_usec / 1000); // L138–L139: epoch ms
    int64_t mono_ms  = (int64_t)monotonic_ms(); // L140: monotonic ms
    s_epoch_offset_ms = epoch_ms - mono_ms; // L141: compute offset
    ESP_LOGI(TAG, "NTP sync: epoch_ms=%lld mono_ms=%lld offset_ms=%lld",
             (long long)epoch_ms, (long long)mono_ms, (long long)s_epoch_offset_ms); // L142–L144: log values
    xEventGroupSetBits(s_evt, BIT_NTP_OK); // L145: mark NTP OK
}
static void ntp_start_and_wait_blocking(uint32_t max_wait_ms) { // L147: start SNTP and wait
    esp_sntp_setoperatingmode(SNTP_OPMODE_POLL); // L148: set poll mode
    esp_sntp_setservername(0, CONFIG_ECOWATT_NTP_SERVER); // L149: set server
    esp_sntp_set_time_sync_notification_cb(sntp_sync_cb); // L150: callback
    esp_sntp_init(); // L151: start SNTP

    EventBits_t bits = xEventGroupWaitBits(
        s_evt, BIT_NTP_OK, pdFALSE, pdFALSE, pdMS_TO_TICKS(max_wait_ms)
    ); // L153–L155: wait for NTP OK
    if ((bits & BIT_NTP_OK) == 0) { // L156: timed out
        ESP_LOGW(TAG, "NTP sync timed out; delaying acquisition until next sync"); // L157
    }
}

// ------------------ Tasks ------------------
static acquisition::Acquisition* g_acq = nullptr; // L161: global pointer to acquisition driver

static void task_acq(void*){ // L163: acquisition task
    if ((xEventGroupGetBits(s_evt) & BIT_NTP_OK) == 0) { // L164: check NTP ready
        xEventGroupWaitBits(s_evt, BIT_NTP_OK, pdFALSE, pdFALSE, pdMS_TO_TICKS(60000)); // L165: wait 60s
    }

    TickType_t last = xTaskGetTickCount(); // L167: last tick count
    uint32_t period_ticks = pdMS_TO_TICKS(CONFIG_ECOWATT_SAMPLE_PERIOD_MS); // L168: convert period

    while(true){ // L170: forever
        acquisition::Sample s{}; // L171: empty sample
        g_acq->read_all(s); // L172: fill sample

        uint64_t t_epoch_ms = now_ms_epoch(); // L174: get epoch ms
        buffer::Record rec{ t_epoch_ms, s }; // L175: make record

        xSemaphoreTake(g_ring_mtx, portMAX_DELAY); // L177: lock ring
        g_ring->push(rec); // L178: push into ring
        xSemaphoreGive(g_ring_mtx); // L179: release

        ESP_LOGI(TAG, "ACQ tick @ %" PRIu64 " ms (epoch)", t_epoch_ms); // L181: log tick
        vTaskDelayUntil(&last, period_ticks); // L182: periodic delay
    }
}

static void task_uplink(void*){ // L185: uplink task
    TickType_t last = xTaskGetTickCount(); // L186: last tick
    const TickType_t period = pdMS_TO_TICKS(CONFIG_ECOWATT_UPLOAD_INTERVAL_SEC * 1000); // L187: convert upload interval

    while(true){ // L189: forever
        std::vector<buffer::Record> batch; // L190: buffer

        xSemaphoreTake(g_ring_mtx, portMAX_DELAY); // L192: lock
        batch = g_ring->snapshot_and_clear(); // L193: take snapshot
        xSemaphoreGive(g_ring_mtx); // L194: release

        if(!batch.empty()){ // L196: if data
            auto payload = uplink::build_payload(batch, CONFIG_ECOWATT_DEVICE_ID); // L197: build JSON
            bool ok = uplink::post_payload(CONFIG_ECOWATT_CLOUD_BASE_URL,
                                           CONFIG_ECOWATT_CLOUD_KEY_B64,
                                           payload.json); // L198–L200: send to cloud
            ESP_LOGI(TAG, "upload: samples=%u, compressed=%zu bytes, ok=%d",
                     (unsigned)batch.size(), payload.raw_bytes, ok); // L201–L202
        }else{
            ESP_LOGI(TAG, "upload: no samples in window"); // L204: nothing
        }
        vTaskDelayUntil(&last, period); // L205: wait interval
    }
}

extern "C" void app_main(void) { // L208: entry point
    ESP_ERROR_CHECK(nvs_flash_init()); // L209: init NVS
    wifi_start_and_wait_ip(); // L210: Wi-Fi
    ntp_start_and_wait_blocking(/*max_wait_ms=*/60000);  // L211: wait for time

    static acquisition::Acquisition acq(CONFIG_ECOWATT_API_BASE_URL, CONFIG_ECOWATT_API_KEY_B64); // L213–L214: acquisition driver
    g_acq = &acq; // L215: store pointer
    acq.set_export_power(10, "boot"); // L216: set initial export power

    const size_t cap = (CONFIG_ECOWATT_UPLOAD_INTERVAL_SEC * 1000 / CONFIG_ECOWATT_SAMPLE_PERIOD_MS) + 16; // L218: ring capacity
    static buffer::Ring ring(cap); // L219: create ring
    g_ring = &ring; // L220: store pointer

    g_ring_mtx = xSemaphoreCreateMutex(); // L222: create mutex

    xTaskCreatePinnedToCore(task_acq,    "acq",    4096, nullptr, 5, nullptr, 0); // L224: create acq task
    xTaskCreatePinnedToCore(task_uplink, "uplink", 4096, nullptr, 5, nullptr, 0); // L225: create uplink task
}


