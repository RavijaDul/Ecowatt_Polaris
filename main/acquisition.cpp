// main/acquisition.cpp
#include "acquisition.hpp"                 // Class/struct declarations for acquisition layer
#include "transport.hpp"                   // HTTP transport for posting Modbus frames
#include "modbus.hpp"                      // Helpers to build/parse Modbus RTU frames
#include "esp_log.h"                       // ESP-IDF logging macros
#include <algorithm>                       // std::min/std::max for clamping
#include <vector>                          // std::vector for register buffers
#include <string>                          // std::string for request/response data

using transport::post_frame;               // Pull post_frame into scope for brevity

namespace acquisition {                    // All acquisition symbols live in this namespace

static const char* TAG = "acq";            // Log tag used by ESP_LOGx macros
static constexpr uint8_t SLAVE = 0x11;     // Slave address expected by the simulator

// Constructor stores base URL and API key (already base64-encoded) for later HTTP calls
Acquisition::Acquisition(const std::string& base_url, const std::string& api_key_b64)
: base_url_(base_url), api_key_(api_key_b64) {}
// Writes the export power limit (%) to register 8 and logs the reason for auditing
bool Acquisition::set_export_power(int percent, const std::string& reason_tag) {
    // Clamp requested percentage into valid range [0, 100]
    int pct = std::max(0, std::min(100, percent));
    if (pct != percent) {                  // Emit a warning if clamping changed the input
        ESP_LOGW(TAG, "Export power percent clamped to %d from %d (reason=%s)",
                 pct, percent, reason_tag.c_str());
    }

    // Build Modbus 0x06 (Write Single Register) for address 8 with the clamped value
    std::string req  = modbus::make_write_single(SLAVE, /*addr=*/8, /*value=*/(uint16_t)pct);
    // Send the write frame to the /write endpoint
    std::string resp = post_frame("write", base_url_, api_key_, req);
    if (resp.empty()) {                    // Empty means no usable HTTP/JSON reply
        ESP_LOGW(TAG, "Write returned blank response (reason=%s)", reason_tag.c_str());
        return false;                      // Treat as write failure
    }

    // For a successful 0x06 write, the device echoes the request frame verbatim
    if (resp != req) {                     // Non-identical → either exception or malformed reply
        uint8_t exc = 0, s = 0, f = 0;     // Try to decode a Modbus exception frame
        if (modbus::parse_exception_response(resp, s, f, exc)) {
            ESP_LOGW(TAG, "Write failed: Modbus exception 0x%02X (%s) [reg=8 reason=%s]",
                     exc, modbus::exception_name(exc), reason_tag.c_str());
        } else {
            ESP_LOGW(TAG, "Write echo mismatch / malformed reply: %s (reason=%s)",
                     resp.c_str(), reason_tag.c_str());
        }
        return false;                      // Write not accepted
    }

    // Echo matched, so the write is confirmed
    ESP_LOGI(TAG, "Set export power to %d%% (reason=%s)", pct, reason_tag.c_str());
    return true;                           // Success
}


// Reads a contiguous group of holding registers starting at 'addr' for 'count' registers
// On success, fills 'out_regs' with big-endian 16-bit values and returns true
bool Acquisition::read_group(uint16_t addr, uint16_t count, std::vector<uint16_t>& out_regs) {
    out_regs.clear();                      // Ensure output buffer starts empty

    // Build a Modbus 0x03 (Read Holding Registers) RTU frame with CRC
    std::string req  = modbus::make_read_holding(SLAVE, addr, count);
    // Send the frame to the /read endpoint and capture the returned frame as hex
    std::string resp = post_frame("read", base_url_, api_key_, req);
    if (resp.empty()) {                    // Empty means HTTP/JSON transport failed or server returned no frame
        ESP_LOGW(TAG, "Blank HTTP/JSON response [addr=%u cnt=%u]", (unsigned)addr, (unsigned)count);
        return false;                      // Propagate failure to caller
    }

    uint8_t slave = 0, func = 0;           // Placeholders for parsed header fields
    // Parse a normal 0x03 response; on success, out_regs is filled from payload
    if (!modbus::parse_read_response(resp, slave, func, out_regs)) {
        // If normal parse failed, check whether this is a Modbus exception response
        uint8_t exc = 0, s = 0, f = 0;     // Holders for exception, slave and function
        if (modbus::parse_exception_response(resp, s, f, exc)) {
            // Log exception code and meaning (e.g., 0x02 = Illegal Data Address)
            ESP_LOGW(TAG, "Modbus exception 0x%02X (%s) [addr=%u cnt=%u]",
                     exc, modbus::exception_name(exc),
                     (unsigned)addr, (unsigned)count);
        } else {
            // If not an exception either, treat as malformed frame or CRC error
            ESP_LOGW(TAG, "Malformed/CRC error [addr=%u cnt=%u] payload=%s",
                     (unsigned)addr, (unsigned)count, resp.c_str());
        }
        return false;                      // Read failed
    }

    // Validate header fields: slave address and function code should match expectations
    if (slave != SLAVE || func != 0x03) {
        ESP_LOGW(TAG, "Unexpected header: slave=0x%02X func=0x%02X [addr=%u cnt=%u]",
                 slave, func, (unsigned)addr, (unsigned)count);
        return false;                      // Reject mismatched headers
    }
    // Validate payload size: the number of 16-bit words must match 'count'
    if (out_regs.size() != count) {
        ESP_LOGW(TAG, "ByteCount mismatch: expected %u regs, got %u [addr=%u]",
                 (unsigned)count, (unsigned)out_regs.size(), (unsigned)addr);
        return false;                      // Reject if server returned an unexpected size
    }
    return true;                           // All checks passed
}


// Reads the full telemetry set in a few grouped requests and prints a formatted summary
bool Acquisition::read_all(Sample& out) {
    bool ok_any = false;                   // Tracks if at least one group read succeeded

    std::vector<uint16_t> regs;            // Scratch buffer reused across group reads

    // Registers 0..1 : Vac1, Iac1 (two 16-bit words)
    if (read_group(0, 2, regs)) {
        out.vac1 = regs[0];                // Store AC voltage raw (scaled later)
        out.iac1 = regs[1];                // Store AC current raw (scaled later)
        ok_any = true;                     // Mark that at least one read worked
    }

    // Register 2 : Fac1 (line frequency)
    if (read_group(2, 1, regs)) {
        out.fac1 = regs[0];                // Store AC frequency raw
        ok_any = true;
    }

    // Registers 3..4 : Vpv1, Vpv2 (PV string voltages)
    if (read_group(3, 2, regs)) {
        out.vpv1 = regs[0];                // PV1 voltage raw
        out.vpv2 = regs[1];                // PV2 voltage raw
        ok_any = true;
    }

    // Registers 5..7 : Ipv1, Ipv2, Temp (PV currents and internal temperature)
    if (read_group(5, 3, regs)) {
        out.ipv1 = regs[0];                // PV1 current raw
        out.ipv2 = regs[1];                // PV2 current raw
        out.temp = regs[2];                // Temperature raw
        ok_any = true;
    }

    // Register 8 : Export power limit (%) — readable for telemetry as well
    if (read_group(8, 1, regs)) {
        out.export_percent = regs[0];      // Export limit raw (0..100)
        ok_any = true;
    }

    // Register 9 : Pac (active power, W)
    if (read_group(9, 1, regs)) {
        out.pac = regs[0];                 // Active power raw
        ok_any = true;
    }

    if (ok_any) {                          // Only print if at least one group succeeded
        // Apply documented scaling:
        // Vac/Iac/Vpv/Ipv/Temp in tenths; Fac in hundredths; Export% is unitless; Pac is in watts
        float Vac  = out.vac1 / 10.0f;     // e.g., 2301 → 230.1 V
        float Iac  = out.iac1 / 10.0f;     // e.g.,  152 → 15.2 A
        float Fac  = out.fac1 / 100.0f;    // e.g.,  5000 → 50.00 Hz
        float Vpv1 = out.vpv1 / 10.0f;     // PV1 voltage in volts
        float Vpv2 = out.vpv2 / 10.0f;     // PV2 voltage in volts
        float Ipv1 = out.ipv1 / 10.0f;     // PV1 current in amps
        float Ipv2 = out.ipv2 / 10.0f;     // PV2 current in amps
        float Temp = out.temp / 10.0f;     // Internal temperature in °C
        int   ExportPct = (int)out.export_percent; // Export limit percent, 0..100
        int   PacW = (int)out.pac;         // Active power in watts

        // Single-line summary for logs/demos
        ESP_LOGI(TAG,
                 "Vac=%.1fV Iac=%.1fA Fac=%.2fHz Vpv1=%.1fV Vpv2=%.1fV Ipv1=%.1fA Ipv2=%.1fA Temp=%.1fC Export%%=%d Pac=%dW",
                 Vac, Iac, Fac, Vpv1, Vpv2, Ipv1, Ipv2, Temp, ExportPct, PacW);
    }

    return ok_any;                         // True if at least one group was read successfully
}

} // namespace acquisition
