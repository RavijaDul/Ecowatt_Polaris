#pragma once                                        // L1: Prevent multiple inclusion of this header.

#include <cstdint>                                  // L3: Fixed-width integer types.
#include <string>                                   // L4: std::string for hex frames.
#include <vector>                                   // L5: std::vector for register lists.

namespace modbus {                                  // L7: Begin modbus namespace.

// ------------------------ CRC ------------------------

// Compute Modbus RTU CRC16 (poly 0xA001, init 0xFFFF) over a byte buffer.
uint16_t crc16(const uint8_t* data, size_t len);    // L12

// -------------------- Hex helpers --------------------

// Convert ASCII hex (whitespace tolerated) into bytes; ignores non-hex characters.
std::vector<uint8_t> hex_to_bytes(const std::string& hex); // L16

// Convert bytes → uppercase ASCII hex without separators.
std::string bytes_to_hex(const uint8_t* data, size_t len); // L18

// -------------------- Builders -----------------------

// Build a Modbus 0x03 (Read Holding Registers) request frame and return as ASCII hex.
std::string make_read_holding(uint8_t slave, uint16_t start_addr, uint16_t count); // L22

// Build a Modbus 0x06 (Write Single Register) request frame and return as ASCII hex.
std::string make_write_single(uint8_t slave, uint16_t reg_addr, uint16_t value);   // L24

// --------------------- Parsers -----------------------

// Parse a normal 0x03 response from ASCII hex.
// On success, fills out_slave, out_func (=0x03), and out_regs (big-endian words).
bool parse_read_response(const std::string& resp_hex,
                         uint8_t& out_slave,
                         uint8_t& out_func,
                         std::vector<uint16_t>& out_regs);                           // L31–L34

// Parse an exception frame from ASCII hex.
// On success, fills out_slave, out_func (=original|0x80), and out_exc_code.
bool parse_exception_response(const std::string& resp_hex,
                              uint8_t& out_slave,
                              uint8_t& out_func,
                              uint8_t& out_exc_code);                                // L39–L42

// Return a human-readable name for common Modbus exception codes.
const char* exception_name(uint8_t c);                                               // L44

} // namespace modbus                            // L46
