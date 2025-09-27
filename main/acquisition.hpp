#pragma once                                      // L1: Ensure this header is included only once per TU.

#include <cstdint>                                // L3: Fixed-width integer types (uint16_t, etc.).
#include <string>                                 // L4: std::string for URLs and keys.
#include <vector>                                 // L5: std::vector for register lists.

namespace acquisition {                           // L7: All acquisition types live under this namespace.

// ---- Basic sample captured from inverter registers (10 fields total) ----
struct Sample {                                   // L10: POD struct mirroring the register map.
    uint16_t vac1;                                // L11: AC voltage (x0.1 V).
    uint16_t iac1;                                // L12: AC current (x0.1 A).
    uint16_t fac1;                                // L13: AC frequency (x0.01 Hz).
    uint16_t vpv1;                                // L14: PV1 voltage (x0.1 V).
    uint16_t vpv2;                                // L15: PV2 voltage (x0.1 V).
    uint16_t ipv1;                                // L16: PV1 current (x0.1 A).
    uint16_t ipv2;                                // L17: PV2 current (x0.1 A).
    uint16_t temp;                                // L18: Inverter temperature (x0.1 °C).
    uint16_t export_percent;                      // L19: Export power limit (%).
    uint16_t pac;                                 // L20: Active power (W).
};

// ---- Driver that talks to the Inverter SIM via HTTP (Modbus frames in JSON) ----
class Acquisition {                               // L24: High-level façade used by main.cpp.
public:
    // Construct with base server URL and Authorization header token (Base64 if Basic).
    Acquisition(const std::string& base_url,      // L27
                const std::string& api_key_b64);  // L28

    // Read a contiguous group of holding registers (function 0x03).
    // Returns true on success and fills 'out_regs' with 'count' words.
    bool read_group(uint16_t addr,                // L32
                    uint16_t count,               // L33
                    std::vector<uint16_t>& out_regs); // L34

    // Write export power percentage (0..100) into register 8 (function 0x06).
    // Returns true when the simulator echoes the exact write frame.
    bool set_export_power(int percent,            // L38
                          const std::string& reason_tag); // L39

    // Convenience: read all 10 registers into a Sample.
    // Tries one big read; falls back to smaller groups if needed.
    bool read_all(Sample& out);                   // L43

private:
    std::string base_url_;                        // L46: e.g., "http://20.15.114.131:8080".
    std::string api_key_;                         // L47: Authorization header value (may be empty).
};

} // namespace acquisition                         // L50: Close namespace.
