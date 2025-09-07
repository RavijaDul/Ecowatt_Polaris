#pragma once
// Ensures this header is included only once during compilation.

#include <string>   // std::string for server URL and API key
#include <vector>   // std::vector for temporary register buffers
#include <cstdint>  // fixed-width integer types (e.g., uint16_t)

namespace acquisition {  // Public API for the acquisition layer

// Sample
// Holds the raw register values read from the inverter simulator.
// Notes:
//  - Addresses (addr) correspond to Modbus holding registers.
//  - "gain" indicates the scale factor to obtain engineering units
//    (e.g., value/gain for Vac1 → volts).
//  - All fields are raw 16-bit unsigned words.
struct Sample {
    uint16_t vac1;           // addr 0, gain 10   → AC voltage (Vac1): value/10 V
    uint16_t iac1;           // addr 1, gain 10   → AC current (Iac1): value/10 A
    uint16_t fac1;           // addr 2, gain 100  → AC frequency (Fac1): value/100 Hz
    uint16_t vpv1;           // addr 3, gain 10   → PV1 voltage (Vpv1): value/10 V
    uint16_t vpv2;           // addr 4, gain 10   → PV2 voltage (Vpv2): value/10 V
    uint16_t ipv1;           // addr 5, gain 10   → PV1 current (Ipv1): value/10 A
    uint16_t ipv2;           // addr 6, gain 10   → PV2 current (Ipv2): value/10 A
    uint16_t temp;           // addr 7, gain 10   → Internal temperature: value/10 °C
    uint16_t export_percent; // addr 8, gain 1    → Export power limit (%), read/write
    uint16_t pac;            // addr 9, gain 1    → Active power (Pac) in watts
};

// Acquisition
// High-level façade for reading/writing Modbus registers via the HTTP API.
// Responsibilities:
//  - Read grouped registers and populate a Sample.
//  - Write the export power limit (register 8) with bounds checking.
//  - Hide transport details (URL, auth) and Modbus frame handling.
class Acquisition {
public:
    // Constructor
    // Stores the base server URL (e.g., "http://<ip>:8080") and the Base64
    // authorization string ("user:pass" encoded) for subsequent requests.
    Acquisition(const std::string& base_url, const std::string& api_key_b64);

    // set_export_power
    // Writes the export power limit (0..100) to register 8 using a Modbus
    // Write Single Register (function 0x06). Values are clamped to [0,100].
    // Returns true on a successful echo reply; false on exception/malformed/transport error.
    bool set_export_power(int percent, const std::string& reason_tag);

    // read_all
    // Performs a small number of grouped Modbus reads to fill the provided Sample.
    // Returns true if at least one group read succeeded; false if all groups failed.
    // Scaling to engineering units is not applied here (raw values only).
    bool read_all(Sample& out);

private:
    // Base server URL (no trailing slash), e.g., "http://20.15.114.131:8080"
    std::string base_url_;

    // Authorization header value (already Base64-encoded "user:pass")
    std::string api_key_;

    // read_group
    // Helper to read 'count' contiguous holding registers starting at 'addr'.
    // On success, fills 'out_regs' with raw 16-bit words (host endian) and returns true.
    // Returns false on Modbus exception, CRC/malformed frame, or transport error.
    bool read_group(uint16_t addr, uint16_t count, std::vector<uint16_t>& out_regs);
};

} // namespace acquisition
