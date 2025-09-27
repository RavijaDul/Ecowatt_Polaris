// main/modbus.cpp
#include "modbus.hpp"                          // L1: Declarations for CRC, hex helpers, frame builders, and parsers.
#include <cctype>                              // L2: std::toupper / std::isspace for hex parsing.
#include <stdexcept>                           // L3: Not strictly used here, but harmless to include for parity.

namespace modbus {                             // L5: Start the modbus namespace to scope all helpers.

// ================= CRC-16 (Modbus RTU, poly 0xA001, init 0xFFFF) =================
uint16_t crc16(const uint8_t* data, size_t len) { // L8: Compute Modbus RTU CRC16 over a byte buffer.
    uint16_t crc = 0xFFFF;                       // L9: Modbus CRC initial value.
    for (size_t i = 0; i < len; ++i) {           // L10: Iterate each byte in the buffer.
        crc ^= static_cast<uint8_t>(data[i]);    // L11: XOR the low-order byte of CRC with data byte.
        for (int j = 0; j < 8; ++j) {            // L12: For each bit in the byte…
            if (crc & 0x0001) {                  // L13: If LSB is set…
                crc >>= 1;                       // L14: Shift right 1.
                crc ^= 0xA001;                   // L15: Then XOR with reflected polynomial 0xA001.
            } else {                             
                crc >>= 1;                       // L17: Else just shift right 1.
            }
        }
    }
    return crc;                                  // L21: Return final CRC (LSB/MSB order decided on serialization).
}

// ============================ ASCII-hex helpers ============================

// Convert a single hex char to its value [0..15], or -1 if not a hex digit.
static inline int hexval(char c) {               // L27
    if (c >= '0' && c <= '9') return (c - '0');  // L28: Decimal digits.
    c = static_cast<char>(std::toupper(static_cast<unsigned char>(c))); // L29: Uppercase for letter handling.
    if (c >= 'A' && c <= 'F') return (10 + c - 'A'); // L30: Hex A–F.
    return -1;                                   // L31: Not a hex digit.
}

// ASCII hex → bytes (ignores whitespace and any non-hex; odd trailing nibble is dropped).
std::vector<uint8_t> hex_to_bytes(const std::string& hex) { // L35
    std::vector<uint8_t> out;                    // L36: Output buffer.
    out.reserve(hex.size() / 2);                 // L37: Reserve approximate size.

    int hi = -1;                                 // L39: Store high nibble until we see the low nibble.
    for (char c : hex) {                         // L40: Scan each character.
        if (std::isspace(static_cast<unsigned char>(c))) continue; // L41: Skip whitespace.
        int v = hexval(c);                       // L42: Convert to nibble value.
        if (v < 0) continue;                     // L43: Ignore non-hex (tolerant parser).
        if (hi < 0) {                            // L44: If we don’t have a high nibble yet…
            hi = v;                              // L45: Store it.
        } else {                                 // L46: Else we have both nibbles; form a byte.
            out.push_back(static_cast<uint8_t>((hi << 4) | v)); // L47: Combine high and low nibble.
            hi = -1;                             // L48: Reset for next byte.
        }
    }
    return out;                                  // L51: Return parsed bytes (odd nibble is ignored).
}

// bytes → uppercase ASCII hex without separators (convenient for JSON/logs).
std::string bytes_to_hex(const uint8_t* data, size_t len) { // L55
    static const char* HEX = "0123456789ABCDEF"; // L56: Lookup table for hex digits.
    std::string s;                               
    s.resize(len * 2);                           // L58: Pre-size: 2 chars per byte.
    for (size_t i = 0; i < len; ++i) {           // L59: For each byte…
        s[2*i + 0] = HEX[(data[i] >> 4) & 0xF];  // L60: High nibble character.
        s[2*i + 1] = HEX[data[i] & 0xF];         // L61: Low nibble character.
    }
    return s;                                    // L63: Return the hex string.
}

// ============================ Frame builders ============================

// Build 0x03 (Read Holding Registers) request frame and return as ASCII hex.
std::string make_read_holding(uint8_t slave, uint16_t start_addr, uint16_t count) { // L69
    std::vector<uint8_t> buf;                   // L70: Build binary frame first.
    buf.reserve(8);                             // L71: Typical RTU request is 8 bytes.
    buf.push_back(slave);                       // L72: Slave address byte.
    buf.push_back(0x03);                        // L73: Function code = 0x03 (Read Holding Registers).
    buf.push_back(uint8_t((start_addr >> 8) & 0xFF)); // L74: Start address high byte.
    buf.push_back(uint8_t(start_addr & 0xFF));        // L75: Start address low byte.
    buf.push_back(uint8_t((count >> 8) & 0xFF));      // L76: Quantity high byte.
    buf.push_back(uint8_t(count & 0xFF));             // L77: Quantity low byte.
    uint16_t c = crc16(buf.data(), buf.size());       // L78: Compute CRC over header/function/data.
    buf.push_back(uint8_t(c & 0xFF));                 // L79: CRC LSB.
    buf.push_back(uint8_t((c >> 8) & 0xFF));          // L80: CRC MSB.
    return bytes_to_hex(buf.data(), buf.size());      // L81: Return ASCII hex version for transport.
}

// Build 0x06 (Write Single Register) request frame and return as ASCII hex.
std::string make_write_single(uint8_t slave, uint16_t reg_addr, uint16_t value) { // L85
    std::vector<uint8_t> buf;                   // L86: Buffer for binary frame.
    buf.reserve(8);                             // L87: 8 bytes typical.
    buf.push_back(slave);                       // L88: Slave address.
    buf.push_back(0x06);                        // L89: Function code = 0x06 (Write Single Register).
    buf.push_back(uint8_t((reg_addr >> 8) & 0xFF)); // L90: Register address high.
    buf.push_back(uint8_t(reg_addr & 0xFF));        // L91: Register address low.
    buf.push_back(uint8_t((value >> 8) & 0xFF));    // L92: Register value high.
    buf.push_back(uint8_t(value & 0xFF));           // L93: Register value low.
    uint16_t c = crc16(buf.data(), buf.size());     // L94: CRC over message.
    buf.push_back(uint8_t(c & 0xFF));               // L95: CRC LSB.
    buf.push_back(uint8_t((c >> 8) & 0xFF));        // L96: CRC MSB.
    return bytes_to_hex(buf.data(), buf.size());    // L97: Return ASCII hex for HTTP body.
}

// ============================== Parsers ==============================

// Parse normal 0x03 response: [slave][0x03][byte_count][data...][CRC_lo][CRC_hi].
bool parse_read_response(const std::string& resp_hex,   // L103
                         uint8_t& out_slave,            // L104: Output slave address.
                         uint8_t& out_func,             // L105: Output function code.
                         std::vector<uint16_t>& out_regs) { // L106: Output registers (big-endian words).
    out_regs.clear();                                   // L107: Start fresh.

    auto bytes = hex_to_bytes(resp_hex);                // L109: Convert ASCII hex → bytes.
    if (bytes.size() < 5) return false;                 // L110: Minimum size check.

    const size_t n = bytes.size();                      // L112: Total bytes.
    uint16_t crc_given = uint16_t(bytes[n-2] | (uint16_t(bytes[n-1]) << 8)); // L113: CRC from frame (LSB,MSB).
    uint16_t crc_calc  = crc16(bytes.data(), n - 2);    // L114: CRC over message excluding CRC field.
    if (crc_given != crc_calc) return false;            // L115: Reject if CRC mismatch.

    out_slave = bytes[0];                               // L117: Slave address.
    out_func  = bytes[1];                               // L118: Function code.

    if (out_func & 0x80) return false;                  // L120: If MSB set → exception frame (not normal 0x03).
    if (out_func != 0x03) return false;                 // L121: Only accept function 0x03 here.

    uint8_t byte_count = bytes[2];                      // L123: Byte count for data.
    if (3 + byte_count + 2 != bytes.size()) return false; // L124: Check frame size matches declared byte count.
    if (byte_count % 2) return false;                   // L125: Must be an even number of data bytes.

    const size_t nregs = byte_count / 2;                // L127: Number of registers in payload.
    out_regs.reserve(nregs);                            // L128: Reserve space.
    for (size_t i = 0; i < nregs; ++i) {                // L129: For each register…
        uint16_t hi = bytes[3 + 2*i + 0];               // L130: High byte.
        uint16_t lo = bytes[3 + 2*i + 1];               // L131: Low byte.
        out_regs.push_back(uint16_t((hi << 8) | lo));   // L132: Combine (big-endian).
    }
    return true;                                        // L134: Parsed successfully.
}

// Parse exception frame: [slave][func|0x80][exception][CRC_lo][CRC_hi].
bool parse_exception_response(const std::string& resp_hex, // L138
                              uint8_t& out_slave,           // L139
                              uint8_t& out_func,            // L140
                              uint8_t& out_exc_code) {      // L141
    auto bytes = hex_to_bytes(resp_hex);                    // L142: Convert ASCII hex → bytes.
    if (bytes.size() < 5) return false;                     // L143: Minimum frame length.

    const size_t n = bytes.size();                          // L145: Total bytes.
    uint16_t crc_given = uint16_t(bytes[n-2] | (uint16_t(bytes[n-1]) << 8)); // L146: Given CRC.
    uint16_t crc_calc  = crc16(bytes.data(), n - 2);        // L147: Computed CRC (exclude CRC field).
    if (crc_given != crc_calc) return false;                // L148: CRC mismatch → fail.

    out_slave = bytes[0];                                   // L150: Slave address.
    out_func  = bytes[1];                                   // L151: Function code with MSB set.
    if ((out_func & 0x80u) == 0) return false;              // L152: Must have MSB set to indicate exception.
    out_exc_code = bytes[2];                                // L153: Exception code.
    return true;                                            // L154: Parsed successfully.
}

// Human-readable label for common Modbus exception codes.
const char* exception_name(uint8_t c) {                     // L158
    switch (c) {                                            // L159
        case 0x01: return "Illegal Function";               // L160
        case 0x02: return "Illegal Data Address";           // L161
        case 0x03: return "Illegal Data Value";             // L162
        case 0x04: return "Slave Device Failure";           // L163
        case 0x05: return "Acknowledge (processing delayed)";// L164
        case 0x06: return "Slave Device Busy";              // L165
        case 0x08: return "Memory Parity Error";            // L166
        case 0x0A: return "Gateway Path Unavailable";       // L167
        case 0x0B: return "Gateway Target Failed to Respond"; // L168
        default:   return "Unknown Modbus exception";       // L169
    }
}

} // namespace modbus                                        // L172: End of modbus namespace.
