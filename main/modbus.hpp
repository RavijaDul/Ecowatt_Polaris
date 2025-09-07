// main/modbus.hpp
#pragma once
// Ensures this header is included only once during translation.

#include <cstdint>  // Fixed-width integer types (e.g., uint8_t, uint16_t)
#include <string>   // std::string for hex strings
#include <vector>   // std::vector for dynamic byte/register buffers

namespace modbus {

// ============================== Utilities ===============================

// crc16
// Computes Modbus RTU CRC-16 over a byte buffer.
// Algorithm: reflected CRC-16 with polynomial 0xA001, initial value 0xFFFF.
// Return value is the raw CRC; when serialized into frames, CRC is little-endian
// (low byte first, then high byte).
uint16_t crc16(const uint8_t* data, size_t len);

// hex_to_bytes
// Converts an ASCII hex string into raw bytes.
// Accepts uppercase/lowercase hex; whitespace is ignored.
// Non-hex characters are ignored defensively; an odd trailing nibble is dropped.
std::vector<uint8_t> hex_to_bytes(const std::string& hex);

// bytes_to_hex
// Converts a raw byte buffer to an uppercase ASCII hex string with no separators.
std::string bytes_to_hex(const uint8_t* data, size_t len);

// ============================ Frame builders ============================

// make_read_holding
// Builds a Modbus RTU "Read Holding Registers" (function 0x03) request.
// Layout (before hex encoding):
//   [slave][0x03][start_hi][start_lo][count_hi][count_lo][CRC_lo][CRC_hi]
std::string make_read_holding(uint8_t slave, uint16_t start_addr, uint16_t count);

// make_write_single
// Builds a Modbus RTU "Write Single Register" (function 0x06) request.
// Layout (before hex encoding):
//   [slave][0x06][reg_hi][reg_lo][val_hi][val_lo][CRC_lo][CRC_hi]
std::string make_write_single(uint8_t slave, uint16_t reg_addr, uint16_t value);

// ============================== Parsers =================================

// parse_read_response
// Parses a normal Modbus RTU response to function 0x03.
// Expected layout (after hex→bytes):
//   [slave][0x03][byte_count][data...][CRC_lo][CRC_hi]
// - 'data' is big-endian 16-bit registers (hi,lo per register).
// On success: returns true, sets out_slave, out_func, and fills out_regs.
// On failure: returns false (CRC mismatch, function not 0x03, malformed size, or exception frame).
bool parse_read_response(const std::string& resp_hex,
                         uint8_t& out_slave,
                         uint8_t& out_func,
                         std::vector<uint16_t>& out_regs);

// parse_exception_response
// Parses a Modbus RTU exception response.
// Expected layout:
//   [slave][(func|0x80)][exception_code][CRC_lo][CRC_hi]
// On success: returns true and sets out_slave, out_func (with MSB set), and out_exc_code.
// On failure: returns false (CRC mismatch, not an exception frame, or malformed).
bool parse_exception_response(const std::string& resp_hex,
                              uint8_t& out_slave,
                              uint8_t& out_func,
                              uint8_t& out_exc_code);

// exception_name
// Maps a Modbus exception code (e.g., 0x02) to a short human-readable description.
// Returns "Unknown Modbus exception" for unmapped codes.
const char* exception_name(uint8_t code);

} // namespace modbus
