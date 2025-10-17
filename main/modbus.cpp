#include "modbus.hpp"
#include <cctype>

namespace modbus {

uint16_t crc16(const uint8_t* data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; ++i) {
    crc ^= static_cast<uint8_t>(data[i]);
    for (int j = 0; j < 8; ++j) {
      if (crc & 0x0001) { crc >>= 1; crc ^= 0xA001; }
      else crc >>= 1;
    }
  }
  return crc;
}

static inline int hexval(char c) {
  if (c >= '0' && c <= '9') return (c - '0');
  c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
  if (c >= 'A' && c <= 'F') return (10 + c - 'A');
  return -1;
}

std::vector<uint8_t> hex_to_bytes(const std::string& hex) {
  std::vector<uint8_t> out; out.reserve(hex.size()/2);
  int hi = -1;
  for (char c : hex) {
    if (std::isspace(static_cast<unsigned char>(c))) continue;
    int v = hexval(c);
    if (v < 0) continue;
    if (hi < 0) hi = v;
    else { out.push_back(static_cast<uint8_t>((hi<<4)|v)); hi = -1; }
  }
  return out;
}

std::string bytes_to_hex(const uint8_t* data, size_t len) {
  static const char* HEX = "0123456789ABCDEF";
  std::string s; s.resize(len*2);
  for (size_t i=0;i<len;++i){ s[2*i]=HEX[(data[i]>>4)&0xF]; s[2*i+1]=HEX[data[i]&0xF]; }
  return s;
}

std::string make_read_holding(uint8_t slave, uint16_t start_addr, uint16_t count) {
  std::vector<uint8_t> buf; buf.reserve(8);
  buf.push_back(slave); buf.push_back(0x03);
  buf.push_back(uint8_t((start_addr>>8)&0xFF)); buf.push_back(uint8_t(start_addr&0xFF));
  buf.push_back(uint8_t((count>>8)&0xFF));      buf.push_back(uint8_t(count&0xFF));
  uint16_t c = crc16(buf.data(), buf.size());
  buf.push_back(uint8_t(c & 0xFF)); buf.push_back(uint8_t((c>>8)&0xFF));
  return bytes_to_hex(buf.data(), buf.size());
}

std::string make_write_single(uint8_t slave, uint16_t reg_addr, uint16_t value) {
  std::vector<uint8_t> buf; buf.reserve(8);
  buf.push_back(slave); buf.push_back(0x06);
  buf.push_back(uint8_t((reg_addr>>8)&0xFF)); buf.push_back(uint8_t(reg_addr&0xFF));
  buf.push_back(uint8_t((value>>8)&0xFF));    buf.push_back(uint8_t(value&0xFF));
  uint16_t c = crc16(buf.data(), buf.size());
  buf.push_back(uint8_t(c & 0xFF)); buf.push_back(uint8_t((c>>8)&0xFF));
  return bytes_to_hex(buf.data(), buf.size());
}

bool parse_read_response(const std::string& resp_hex, uint8_t& out_slave, uint8_t& out_func, std::vector<uint16_t>& out_regs) {
  out_regs.clear();
  auto bytes = hex_to_bytes(resp_hex);
  if (bytes.size() < 5) return false;
  const size_t n = bytes.size();
  uint16_t given = uint16_t(bytes[n-2] | (uint16_t(bytes[n-1])<<8));
  uint16_t calc  = crc16(bytes.data(), n-2);
  if (given != calc) return false;
  out_slave = bytes[0];
  out_func  = bytes[1];
  if (out_func & 0x80) return false;
  if (out_func != 0x03) return false;
  uint8_t byte_count = bytes[2];
  if (3 + byte_count + 2 != bytes.size()) return false;
  if (byte_count % 2) return false;
  size_t nregs = byte_count/2;
  out_regs.reserve(nregs);
  for (size_t i=0;i<nregs;++i){
    uint16_t hi = bytes[3+2*i], lo = bytes[3+2*i+1];
    out_regs.push_back(uint16_t((hi<<8)|lo));
  }
  return true;
}

bool parse_exception_response(const std::string& resp_hex, uint8_t& out_slave, uint8_t& out_func, uint8_t& out_exc_code) {
  auto bytes = hex_to_bytes(resp_hex);
  if (bytes.size() < 5) return false;
  const size_t n = bytes.size();
  uint16_t given = uint16_t(bytes[n-2] | (uint16_t(bytes[n-1])<<8));
  uint16_t calc  = crc16(bytes.data(), n-2);
  if (given != calc) return false;
  out_slave = bytes[0];
  out_func  = bytes[1];
  if ((out_func & 0x80u) == 0) return false;
  out_exc_code = bytes[2];
  return true;
}

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
