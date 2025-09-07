#include <cstring>                         // For std::strncpy
#include <string>                          // For std::string literals in logging
#include "esp_wifi.h"                      // ESP-IDF Wi-Fi Station APIs
#include "esp_event.h"                     // Event loop and handler registration
#include "esp_log.h"                       // Logging macros (ESP_LOGx)
#include "nvs_flash.h"                     // NVS init (required by Wi-Fi stack)
#include "esp_netif.h"                     // TCP/IP adapter (netif) initialization
#include "freertos/FreeRTOS.h"             // FreeRTOS base definitions
#include "freertos/task.h"                 // vTaskDelay, task API
#include "freertos/event_groups.h"         // Event groups for cross-task flags

#include "acquisition.hpp"                 // Acquisition facade (read/write Modbus via HTTP)

// ---- Kconfig defaults (can be overridden in menuconfig) ----
#ifndef CONFIG_ECOWATT_WIFI_SSID
#define CONFIG_ECOWATT_WIFI_SSID "YOUR_WIFI_SSID"       // Placeholder SSID if not set in Kconfig
#endif
#ifndef CONFIG_ECOWATT_WIFI_PASS
#define CONFIG_ECOWATT_WIFI_PASS "YOUR_WIFI_PASSWORD"   // Placeholder password if not set in Kconfig
#endif
#ifndef CONFIG_ECOWATT_API_BASE_URL
#define CONFIG_ECOWATT_API_BASE_URL "http://20.15.114.131:8080"  // Cloud Inverter SIM base URL
#endif
#ifndef CONFIG_ECOWATT_API_KEY_B64
#define CONFIG_ECOWATT_API_KEY_B64 "PUT_BASE64_KEY_HERE"         // "user:pass" in Base64 for Authorization
#endif

static const char* TAG = "EcoWatt";        // Log tag for this translation unit

// Event group and bit definitions used to track Wi-Fi/IP status
static EventGroupHandle_t s_evt;           // Created at startup; shared via static
static constexpr int BIT_CONNECTED = BIT0; // Set when Wi-Fi station is associated to AP
static constexpr int BIT_GOT_IP   = BIT1;  // Set when DHCP completes and IP is obtained

// on_wifi — handles Wi-Fi station lifecycle events
static void on_wifi(void*, esp_event_base_t base, int32_t id, void* data) {
    if (base == WIFI_EVENT) {              // Ensure this is a Wi-Fi event
        switch (id) {                      // Branch on the specific Wi-Fi event id
            case WIFI_EVENT_STA_START:     // Station interface started
                esp_wifi_connect();        // Begin association with configured AP
                break;
            case WIFI_EVENT_STA_CONNECTED: // Association to AP succeeded
                xEventGroupSetBits(s_evt, BIT_CONNECTED); // Mark “connected” state
                ESP_LOGI(TAG, "Wi-Fi associated");        // Informational log
                break;
            case WIFI_EVENT_STA_DISCONNECTED: // Link lost or auth failed
                xEventGroupClearBits(s_evt, BIT_CONNECTED | BIT_GOT_IP); // Clear both flags
                ESP_LOGW(TAG, "Wi-Fi disconnected — reconnecting…");     // Diagnostic log
                esp_wifi_connect();          // Immediate reconnect attempt (simple policy)
                break;
        }
    }
}

// on_ip — handles IP events (DHCP success)
static void on_ip(void*, esp_event_base_t base, int32_t id, void* data) {
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) { // Only care about GOT_IP
        auto* e = static_cast<ip_event_got_ip_t*>(data); // Cast to expected payload type
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&e->ip_info.ip)); // Print assigned IPv4
        xEventGroupSetBits(s_evt, BIT_GOT_IP);            // Signal “network ready”
    }
}

// wifi_start_and_wait_ip — sets up station mode and blocks until IP (or timeout)
static void wifi_start_and_wait_ip() {
    ESP_ERROR_CHECK(esp_netif_init());                    // Initialize TCP/IP stack
    ESP_ERROR_CHECK(esp_event_loop_create_default());     // Create default event loop
    esp_netif_create_default_wifi_sta();                  // Create default STA netif

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();  // Default Wi-Fi driver config
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));                 // Initialize Wi-Fi driver
    // Register event callbacks for Wi-Fi and IP state changes
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &on_wifi, nullptr));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &on_ip, nullptr));

    wifi_config_t sta = {};                               // Zero-initialize ensures null-termination
    // Copy SSID/PASS from Kconfig strings into the fixed-size C buffers
    std::strncpy((char*)sta.sta.ssid,     CONFIG_ECOWATT_WIFI_SSID, sizeof(sta.sta.ssid));
    std::strncpy((char*)sta.sta.password, CONFIG_ECOWATT_WIFI_PASS, sizeof(sta.sta.password));
    sta.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;      // WPA2 minimum; WPA3 negotiated if supported

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));    // Station mode
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta)); // Apply station credentials
    ESP_ERROR_CHECK(esp_wifi_start());                    // Start Wi-Fi driver

    ESP_LOGI(TAG, "Wi-Fi connecting to %s ...", CONFIG_ECOWATT_WIFI_SSID); // Progress log

    // Poll the event group for up to ~20 s until IP is acquired; proceed even if timed out
    for (int i = 0; i < 80; ++i) {                        // 80 * 250 ms = 20 s budget
        EventBits_t bits = xEventGroupGetBits(s_evt);     // Snapshot of current flags
        if ((bits & BIT_GOT_IP) == BIT_GOT_IP) {          // Check if IP flag is set
            ESP_LOGI(TAG, "Network ready");               // Confirm ready state
            return;                                       // Exit on success
        }
        vTaskDelay(pdMS_TO_TICKS(250));                   // Sleep 250 ms between checks
    }
    ESP_LOGW(TAG, "Timed out waiting for IP — continuing (HTTP will retry)."); // Non-fatal warning
}

// app_main — entry point for ESP-IDF applications (C linkage required)
extern "C" void app_main(void) {
    ESP_ERROR_CHECK(nvs_flash_init());                    // Initialize NVS (required by Wi-Fi)
    s_evt = xEventGroupCreate();                          // Create event group for Wi-Fi/IP flags

    wifi_start_and_wait_ip();                             // Bring network up (best-effort)

    // Create acquisition facade bound to API base URL and Authorization key
    acquisition::Acquisition acq(CONFIG_ECOWATT_API_BASE_URL, CONFIG_ECOWATT_API_KEY_B64);

    // One mandatory write to the SIM after network is up (Milestone-2 requirement)
    acq.set_export_power(10, "boot");                     // Writes reg 8 (export limit) to 10%

    acquisition::Sample s{};                              // Struct to hold raw register values
    while (true) {
        acq.read_all(s);                                  // Read grouped registers 0..9, log scaled view
        vTaskDelay(pdMS_TO_TICKS(5000));                  // Poll every 5 s (placeholder rate)
    }
}
