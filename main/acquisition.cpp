// main/acquisition.cpp
#include "acquisition.hpp"   // L1: header for Acquisition class, Sample struct, etc.
#include "transport.hpp"     // L2: for post_frame() function (HTTP transport)
#include "modbus.hpp"        // L3: for Modbus frame builders/parsers
#include "esp_log.h"         // L4: for ESP_LOG macros
#include <algorithm>         // L5: for std::max / std::min
#include <vector>            // L6: for std::vector
#include <string>            // L7: for std::string

using transport::post_frame; // L9: import post_frame into this namespace

namespace acquisition { // L11: start of acquisition namespace

static const char* TAG = "acq";            // L13: log tag
static constexpr uint8_t SLAVE = 0x11;     // L14: Modbus slave address of the inverter SIM

// -----------------------------------------------------------------------------
// Acquisition constructor
// -----------------------------------------------------------------------------
Acquisition::Acquisition(const std::string& base_url, const std::string& api_key_b64)
: base_url_(base_url), api_key_(api_key_b64) {} // L19–L20: store base URL + API key

// -----------------------------------------------------------------------------
// read_group()
// Reads a contiguous block of registers from Inverter SIM using Modbus function 0x03
// -----------------------------------------------------------------------------
bool Acquisition::read_group(uint16_t addr, uint16_t count, std::vector<uint16_t>& out_regs) {
    out_regs.clear(); // L26: clear the output vector

    // Build Modbus frame for "Read Holding Registers"
    std::string req  = modbus::make_read_holding(SLAVE, addr, count); // L29: request frame
    std::string resp = post_frame("read", base_url_, api_key_, req);   // L30: send via HTTP POST

    // If no response received, log warning and fail
    if (resp.empty()) { // L32
        ESP_LOGW(TAG, "Blank HTTP/JSON response [addr=%u cnt=%u]", (unsigned)addr, (unsigned)count); // L33
        return false; // L34
    }

    uint8_t slave = 0, func = 0; // L36: prepare for response header
    if (!modbus::parse_read_response(resp, slave, func, out_regs)) { // L37: try to parse as normal response
        // If parsing failed, try to parse as exception frame
        uint8_t exc = 0, s = 0, f = 0; // L39
        if (modbus::parse_exception_response(resp, s, f, exc)) { // L40
            ESP_LOGW(TAG, "Modbus exception 0x%02X (%s) [addr=%u cnt=%u]",
                     exc, modbus::exception_name(exc),
                     (unsigned)addr, (unsigned)count); // L41–L43
        } else {
            ESP_LOGW(TAG, "Malformed/CRC error [addr=%u cnt=%u] payload=%s",
                     (unsigned)addr, (unsigned)count, resp.c_str()); // L45–L46
        }
        return false; // L47
    }

    // Validate header fields
    if (slave != SLAVE || func != 0x03) { // L50: wrong slave or function
        ESP_LOGW(TAG, "Unexpected header: slave=0x%02X func=0x%02X [addr=%u cnt=%u]",
                 slave, func, (unsigned)addr, (unsigned)count); // L51–L52
        return false; // L53
    }
    if (out_regs.size() != count) { // L54: wrong number of registers returned
        ESP_LOGW(TAG, "ByteCount mismatch: expected %u regs, got %u [addr=%u]",
                 (unsigned)count, (unsigned)out_regs.size(), (unsigned)addr); // L55–L56
        return false; // L57
    }
    return true; // L58: success
}

// -----------------------------------------------------------------------------
// set_export_power()
// Write a value (0–100%) to register 8 (export power percentage).
// Uses Modbus function 0x06.
// -----------------------------------------------------------------------------
bool Acquisition::set_export_power(int percent, const std::string& reason_tag) {
    int pct = std::max(0, std::min(100, percent)); // L64: clamp to [0,100]
    if (pct != percent) { // L65: if clamped
        ESP_LOGW(TAG, "Export power percent clamped to %d from %d (reason=%s)",
                 pct, percent, reason_tag.c_str()); // L66–L67
    }

    std::string req  = modbus::make_write_single(SLAVE, /*addr=*/8, (uint16_t)pct); // L69: build write frame
    std::string resp = post_frame("write", base_url_, api_key_, req); // L70: send via HTTP

    if (resp.empty()) { // L72: no reply
        ESP_LOGW(TAG, "Write returned blank response (reason=%s)", reason_tag.c_str()); // L73
        return false; // L74
    }

    // Simulator should echo the same frame on success
    if (resp != req) { // L77: mismatch
        uint8_t exc = 0, s = 0, f = 0; // L78
        if (modbus::parse_exception_response(resp, s, f, exc)) { // L79
            ESP_LOGW(TAG, "Write failed: Modbus exception 0x%02X (%s) [reg=8 reason=%s]",
                     exc, modbus::exception_name(exc), reason_tag.c_str()); // L80–L81
        } else {
            ESP_LOGW(TAG, "Write echo mismatch / malformed reply: %s (reason=%s)",
                     resp.c_str(), reason_tag.c_str()); // L83–L84
        }
        return false; // L85
    }

    ESP_LOGI(TAG, "Set export power to %d%% (reason=%s)", pct, reason_tag.c_str()); // L87: log success
    return true; // L88
}

// -----------------------------------------------------------------------------
// read_all()
// Reads all 10 inverter registers into a Sample struct.
// First tries one big block (addr 0..9). If fails, falls back to smaller groups.
// -----------------------------------------------------------------------------
bool Acquisition::read_all(Sample& out) {
    bool ok_any = false; // L94: track if any success
    std::vector<uint16_t> regs; // L95: temp vector

    // Preferred path: one big read (0–9)
    if (read_group(/*addr=*/0, /*count=*/10, regs)) { // L98
        out.vac1           = regs[0]; // L99
        out.iac1           = regs[1]; // L100
        out.fac1           = regs[2]; // L101
        out.vpv1           = regs[3]; // L102
        out.vpv2           = regs[4]; // L103
        out.ipv1           = regs[5]; // L104
        out.ipv2           = regs[6]; // L105
        out.temp           = regs[7]; // L106
        out.export_percent = regs[8]; // L107
        out.pac            = regs[9]; // L108
        ok_any = true; // L109
    } else {
        // Fallback: multiple smaller groups
        if (read_group(0, 2, regs)) { // L112
            out.vac1 = regs[0]; // L113
            out.iac1 = regs[1]; // L114
            ok_any = true; // L115
        }
        if (read_group(2, 1, regs)) { // L116
            out.fac1 = regs[0]; // L117
            ok_any = true; // L118
        }
        if (read_group(3, 2, regs)) { // L119
            out.vpv1 = regs[0]; // L120
            out.vpv2 = regs[1]; // L121
            ok_any = true; // L122
        }
        if (read_group(5, 3, regs)) { // L123
            out.ipv1 = regs[0]; // L124
            out.ipv2 = regs[1]; // L125
            out.temp = regs[2]; // L126
            ok_any = true; // L127
        }
        if (read_group(8, 1, regs)) { // L128
            out.export_percent = regs[0]; // L129
            ok_any = true; // L130
        }
        if (read_group(9, 1, regs)) { // L131
            out.pac = regs[0]; // L132
            ok_any = true; // L133
        }
    }

    // If any values were read, log them in scaled form
    if (ok_any) { // L136
        float Vac  = out.vac1 / 10.0f; // L137
        float Iac  = out.iac1 / 10.0f; // L138
        float Fac  = out.fac1 / 100.0f; // L139
        float Vpv1 = out.vpv1 / 10.0f; // L140
        float Vpv2 = out.vpv2 / 10.0f; // L141
        float Ipv1 = out.ipv1 / 10.0f; // L142
        float Ipv2 = out.ipv2 / 10.0f; // L143
        float Temp = out.temp / 10.0f; // L144
        int   ExportPct = (int)out.export_percent; // L145
        int   PacW = (int)out.pac; // L146

        ESP_LOGI(TAG,
                 "Vac=%.1fV Iac=%.1fA Fac=%.2fHz Vpv1=%.1fV Vpv2=%.1fV Ipv1=%.1fA Ipv2=%.1fA Temp=%.1fC Export%%=%d Pac=%dW",
                 Vac, Iac, Fac, Vpv1, Vpv2, Ipv1, Ipv2, Temp, ExportPct, PacW); // L148–L151
    }

    return ok_any; // L153
}

} // namespace acquisition // L155
