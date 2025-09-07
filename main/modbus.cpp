// main/modbus.cpp
#include "modbus.hpp"                      // Declarations for CRC, hex helpers, frame builders/parsers
#include <cctype>                          // std::toupper, std::isspace for hex parsing
#include <stdexcept>                       // (not used here; kept for parity with header/other TU includes)

namespace modbus {

// ===== CRC-16 (Modbus RTU, poly 0xA001, init 0xFFFF) =====
// Computes the Modbus RTU CRC over 'len' bytes at 'data'.
// Algorithm: reflected CRC-16 with polynomial 0xA001, initial value 0xFFFF.
// Output: little-endian in frames (low byte first, then high byte).
uint16_t crc16(const uint8_t* data, size_t len) {
    uint16_t crc = 0xFFFF;                 // Modbus CRC initial value
    for (size_t i = 0; i < len; ++i) {     // Process each input byte
        crc ^= static_cast<uint8_t>(data[i]); // XOR byte into LSB of CRC
        for (int j = 0; j < 8; ++j) {      // For each bit in the byte
            if (crc & 0x0001) {            // If LSB is set, shift and apply polynomial
                crc >>= 1;
                crc ^= 0xA001;             // 0xA001 is the reversed 0x8005 polynomial
            } else {
                crc >>= 1;                 // If LSB not set, just shift
            }
        }
    }
    return crc;                            // Caller decides how to serialize (low, high)
}

// ===== Hex helpers =====

// Converts a single hex character to its integer value [0..15].
// Returns -1 if 'c' is not a hex digit.
static inline int hexval(char c) {
    if (c >= '0' && c <= '9') return (c - '0'); // Fast path for digits
    c = static_cast<char>(std::toupper(static_cast<unsigned char>(c))); // Normalize to uppercase
    if (c >= 'A' && c <= 'F') return (10 + c - 'A'); // Map A..F → 10..15
    return -1;                          // Not a hex character
}

// Converts an ASCII hex string (optionally with spaces/newlines) to a byte vector.
// Non-hex characters are ignored defensively; odd trailing nibble is dropped.
std::vector<uint8_t> hex_to_bytes(const std::string& hex) {
    std::vector<uint8_t> out;              // Output byte buffer
    out.reserve(hex.size() / 2);           // Reserve a reasonable upper bound

    int hi = -1;                            // Holds high nibble until a low nibble arrives
    for (char c : hex) {                    // Walk each character in the input
        if (std::isspace(static_cast<unsigned char>(c))) continue; // Skip whitespace
        int v = hexval(c);                  // Convert to nibble value or -1
        if (v < 0) continue;                // Ignore non-hex characters (defensive)
        if (hi < 0) {
            hi = v;                         // Store high nibble; wait for low nibble
        } else {
            out.push_back(static_cast<uint8_t>((hi << 4) | v)); // Combine nibbles into a byte
            hi = -1;                        // Reset for next byte
        }
    }
    return out;                             // If 'hi' is set here, last nibble was dangling; intentionally dropped
}

// Converts a raw byte buffer to an uppercase ASCII hex string without separators.
std::string bytes_to_hex(const uint8_t* data, size_t len) {
    static const char* HEX = "0123456789ABCDEF"; // Lookup table for hex digits
    std::string s;                         // Output string with exact size
    s.resize(len * 2);                     // Two hex chars per byte
    for (size_t i = 0; i < len; ++i) {     // Encode each byte as two chars
        s[2*i + 0] = HEX[(data[i] >> 4) & 0xF]; // High nibble
        s[2*i + 1] = HEX[data[i] & 0xF];        // Low nibble
    }
    return s;
}

// ===== Frame builders =====

// Builds a Modbus RTU "Read Holding Registers" (function 0x03) request.
// Layout: [slave][0x03][start_hi][start_lo][count_hi][count_lo][CRC_lo][CRC_hi] → hex.
std::string make_read_holding(uint8_t slave, uint16_t start_addr, uint16_t count) {
    std::vector<uint8_t> buf;              // Temporary byte buffer for the frame
    buf.reserve(8);                        // Exact request length without spaces
    buf.push_back(slave);                  // Slave address
    buf.push_back(0x03);                   // Function code: Read Holding Registers
    buf.push_back(uint8_t((start_addr >> 8) & 0xFF)); // Start address high byte
    buf.push_back(uint8_t(start_addr & 0xFF));        // Start address low byte
    buf.push_back(uint8_t((count >> 8) & 0xFF));      // Quantity of registers high byte
    buf.push_back(uint8_t(count & 0xFF));             // Quantity of registers low byte
    uint16_t c = crc16(buf.data(), buf.size());       // CRC over the 6-byte header
    buf.push_back(uint8_t(c & 0xFF));                 // CRC low byte (Modbus little-endian)
    buf.push_back(uint8_t((c >> 8) & 0xFF));          // CRC high byte
    return bytes_to_hex(buf.data(), buf.size());      // Return as uppercase hex string
}

// Builds a Modbus RTU "Write Single Register" (function 0x06) request.
// Layout: [slave][0x06][reg_hi][reg_lo][val_hi][val_lo][CRC_lo][CRC_hi] → hex.
std::string make_write_single(uint8_t slave, uint16_t reg_addr, uint16_t value) {
    std::vector<uint8_t> buf;              // Temporary byte buffer for the frame
    buf.reserve(8);                        // Fixed length for 0x06 request
    buf.push_back(slave);                  // Slave address
    buf.push_back(0x06);                   // Function code: Write Single Register
    buf.push_back(uint8_t((reg_addr >> 8) & 0xFF));   // Register address high byte
    buf.push_back(uint8_t(reg_addr & 0xFF));          // Register address low byte
    buf.push_back(uint8_t((value >> 8) & 0xFF));      // Register value high byte
    buf.push_back(uint8_t(value & 0xFF));             // Register value low byte
    uint16_t c = crc16(buf.data(), buf.size());       // CRC over the 6-byte header
    buf.push_back(uint8_t(c & 0xFF));                 // CRC low byte
    buf.push_back(uint8_t((c >> 8) & 0xFF));          // CRC high byte
    return bytes_to_hex(buf.data(), buf.size());      // Return as uppercase hex string
}

// ===== Parsers =====

// Parses a Modbus RTU "Read Holding Registers" normal response (function 0x03).
// Expected layout: [slave][0x03][byte_count][data...][CRC_lo][CRC_hi]
// - 'data' is big-endian register words: hi,lo per register
// Returns true on success, filling out_slave, out_func, and out_regs.
bool parse_read_response(const std::string& resp_hex,
                         uint8_t& out_slave,
                         uint8_t& out_func,
                         std::vector<uint16_t>& out_regs) {
    out_regs.clear();                       // Start with an empty output vector
    auto bytes = hex_to_bytes(resp_hex);    // Convert ASCII hex to raw bytes
    if (bytes.size() < 5) return false;     // Minimum length: addr, func, count, CRC_lo, CRC_hi

    // CRC check: verify that the last two bytes match the computed CRC
    const size_t n = bytes.size();
    uint16_t crc_given = uint16_t(bytes[n-2] | (uint16_t(bytes[n-1]) << 8)); // Little-endian CRC in frame
    uint16_t crc_calc  = crc16(bytes.data(), n - 2);   // Compute CRC over all but trailing CRC
    if (crc_given != crc_calc) return false;           // Reject on CRC mismatch

    out_slave = bytes[0];                  // Extract slave address from frame
    out_func  = bytes[1];                  // Extract function code

    // If MSB is set, this is an exception frame, not a normal 0x03 response
    if (out_func & 0x80) return false;

    // Accept only 0x03 responses here
    if (out_func != 0x03) return false;

    uint8_t byte_count = bytes[2];         // Number of data bytes following
    if (3 + byte_count + 2 != bytes.size()) return false; // Validate total length (hdr+data+CRC)
    if (byte_count % 2) return false;      // Must be an even number of data bytes

    const size_t nregs = byte_count / 2;   // Each register is 2 bytes
    out_regs.reserve(nregs);               // Reserve exact capacity
    for (size_t i = 0; i < nregs; ++i) {   // Parse big-endian register words
        uint16_t hi = bytes[3 + 2*i + 0];  // High byte of register i
        uint16_t lo = bytes[3 + 2*i + 1];  // Low  byte of register i
        out_regs.push_back(uint16_t((hi << 8) | lo)); // Combine into host-endian uint16_t
    }
    return true;                           // Parsed successfully
}

// Parses a Modbus RTU exception response.
// Layout: [slave][func|0x80][exception_code][CRC_lo][CRC_hi]
// Returns true and fills out_* on valid exception frame; false otherwise.
bool parse_exception_response(const std::string& resp_hex,
                              uint8_t& out_slave,
                              uint8_t& out_func,
                              uint8_t& out_exc_code) {
    auto bytes = hex_to_bytes(resp_hex);    // Convert ASCII hex to raw bytes
    if (bytes.size() < 5) return false;     // Minimum length for exception frame

    const size_t n = bytes.size();
    uint16_t crc_given = uint16_t(bytes[n-2] | (uint16_t(bytes[n-1]) << 8)); // CRC from frame
    uint16_t crc_calc  = crc16(bytes.data(), n - 2);   // CRC over payload
    if (crc_given != crc_calc) return false;           // CRC must match

    out_slave = bytes[0];                  // Slave address
    out_func  = bytes[1];                  // Function code with MSB set for exception
    if ((out_func & 0x80u) == 0) return false; // Not an exception if MSB not set
    out_exc_code = bytes[2];               // Exception code (e.g., 0x02 = Illegal Data Address)
    return true;                           // Valid exception frame
}

// Maps known Modbus exception codes to human-readable descriptions.
const char* exception_name(uint8_t c) {
    switch (c) {
        case 0x01: return "Illegal Function";
        case 0x02: return "Illegal Data Address";
        case 0x03: return "Illegal Data Value";
        case 0x04: return "Slave Device Failure";
        case 0x05: return "Acknowledge (processing delayed)";
        case 0x06: return "Slave Device Busy";
        case 0x08: return "Memory Parity Error";
        case 0x0A: return "Gateway Path Unavailable";
        case 0x0B: return "Gateway Target Failed to Respond";
        default:   return "Unknown Modbus exception";
    }
}

} // namespace modbus
