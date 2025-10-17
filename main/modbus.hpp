#pragma once
#include <cstdint>
#include <string>
#include <vector>

namespace modbus {

// CRC-16 (Modbus RTU, poly 0xA001)
uint16_t crc16(const uint8_t* data, size_t len);

// ASCII-hex helpers
std::vector<uint8_t> hex_to_bytes(const std::string& hex);
std::string bytes_to_hex(const uint8_t* data, size_t len);

// Builders
std::string make_read_holding(uint8_t slave, uint16_t start_addr, uint16_t count);
std::string make_write_single(uint8_t slave, uint16_t reg_addr, uint16_t value);

// Parsers
bool parse_read_response(const std::string& resp_hex, uint8_t& out_slave, uint8_t& out_func, std::vector<uint16_t>& out_regs);
bool parse_exception_response(const std::string& resp_hex, uint8_t& out_slave, uint8_t& out_func, uint8_t& out_exc_code);
const char* exception_name(uint8_t c);

} // namespace modbus
