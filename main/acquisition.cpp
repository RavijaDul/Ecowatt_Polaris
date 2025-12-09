#include "acquisition.hpp"
#include "transport.hpp"
#include "modbus.hpp"
#include "esp_log.h"
#include <algorithm>
#include <vector>
#include <string>

using transport::post_frame;

// External callback for SIM fault reporting
extern "C" void sim_fault_notify(const char* fault_type, uint8_t exception_code, const char* description);

namespace acquisition {

static const char* TAG = "acq";
static constexpr uint8_t SLAVE = 0x11;

Acquisition::Acquisition(const std::string& base_url, const std::string& api_key_b64)
: base_url_(base_url), api_key_(api_key_b64) {}

bool Acquisition::read_group(uint16_t addr, uint16_t count, std::vector<uint16_t>& out_regs) {
  out_regs.clear();
  std::string req  = modbus::make_read_holding(SLAVE, addr, count);
  std::string resp = post_frame("read", base_url_, api_key_, req);
  if (resp.empty()) { 
    ESP_LOGW(TAG, "Blank read response [addr=%u cnt=%u]", (unsigned)addr, (unsigned)count);
    sim_fault_notify("timeout", 0, "No response from SIM");
    return false; 
  }
  uint8_t slave = 0, func = 0;
  if (!modbus::parse_read_response(resp, slave, func, out_regs)) {
    uint8_t exc = 0, s = 0, f = 0;
    if (modbus::parse_exception_response(resp, s, f, exc)) {
      ESP_LOGW(TAG, "Modbus exception 0x%02X (%s) [addr=%u cnt=%u]", exc, modbus::exception_name(exc), (unsigned)addr, (unsigned)count);
      sim_fault_notify("exception", exc, modbus::exception_name(exc));
    } else {
      ESP_LOGW(TAG, "Malformed/CRC error [addr=%u cnt=%u] payload=%s", (unsigned)addr, (unsigned)count, resp.c_str());
      sim_fault_notify("malformed_response", 0, "CRC or parse error");
    }
    return false;
  }
  if (slave != SLAVE || func != 0x03) { 
    ESP_LOGW(TAG, "Unexpected header slave=0x%02X func=0x%02X", slave, func); 
    sim_fault_notify("malformed_response", 0, "Unexpected header");
    return false; 
  }
  return true;
}

bool Acquisition::set_export_power(int percent, const std::string& reason_tag) {
  int pct = std::max(0, std::min(100, percent));
  if (pct != percent) ESP_LOGW(TAG, "Export power clamped to %d from %d", pct, percent);
  std::string req  = modbus::make_write_single(SLAVE, 8, (uint16_t)pct);
  std::string resp = post_frame("write", base_url_, api_key_, req);
  if (resp.empty()) { 
    ESP_LOGW(TAG, "Write blank response (reason=%s)", reason_tag.c_str());
    sim_fault_notify("timeout", 0, "No write response");
    return false; 
  }
  if (resp != req) {
    uint8_t exc = 0, s = 0, f = 0;
    if (modbus::parse_exception_response(resp, s, f, exc)) {
      ESP_LOGW(TAG, "Write exception 0x%02X (%s)", exc, modbus::exception_name(exc));
      sim_fault_notify("exception", exc, modbus::exception_name(exc));
    } else {
      ESP_LOGW(TAG, "Write echo mismatch: %s", resp.c_str());
      sim_fault_notify("malformed_response", 0, "Echo mismatch");
    }
    return false;
  }
  ESP_LOGI(TAG, "Set export power to %d%% (%s)", pct, reason_tag.c_str());
  return true;
}

bool Acquisition::read_all(Sample& out) {
  bool ok_any = false;
  std::vector<uint16_t> regs;
  if (read_group(0, 10, regs) && regs.size()==10) {
    out.vac1=regs[0]; out.iac1=regs[1]; out.fac1=regs[2]; out.vpv1=regs[3]; out.vpv2=regs[4];
    out.ipv1=regs[5]; out.ipv2=regs[6]; out.temp=regs[7]; out.export_percent=regs[8]; out.pac=regs[9];
    ok_any=true;
  } else {
    if (read_group(0,2,regs)) { out.vac1=regs[0]; out.iac1=regs[1]; ok_any=true; }
    if (read_group(2,1,regs)) { out.fac1=regs[0]; ok_any=true; }
    if (read_group(3,2,regs)) { out.vpv1=regs[0]; out.vpv2=regs[1]; ok_any=true; }
    if (read_group(5,3,regs)) { out.ipv1=regs[0]; out.ipv2=regs[1]; out.temp=regs[2]; ok_any=true; }
    if (read_group(8,1,regs)) { out.export_percent=regs[0]; ok_any=true; }
    if (read_group(9,1,regs)) { out.pac=regs[0]; ok_any=true; }
  }
  return ok_any;
}

bool Acquisition::read_selected(const std::vector<int>& field_ids, Sample& out) {
  if (field_ids.empty()) return false;
  auto addr_of = [](int fid)->uint16_t { return (uint16_t)fid; }; // enum maps directly to reg addr
  bool ok_any=false;
  size_t i=0;
  while(i<field_ids.size()){
    uint16_t a0 = addr_of(field_ids[i]);
    uint16_t cnt=1;
    size_t j=i+1;
    while(j<field_ids.size()){
      uint16_t aj = addr_of(field_ids[j]);
      if(aj == a0 + cnt){ ++cnt; ++j; } else break;
    }
    std::vector<uint16_t> regs;
    if(read_group(a0, cnt, regs) && regs.size()==cnt){
      ok_any=true;
      for(uint16_t k=0;k<cnt;++k){
        uint16_t v = regs[k];
        switch(a0+k){
          case 0: out.vac1=v; break; case 1: out.iac1=v; break; case 2: out.fac1=v; break;
          case 3: out.vpv1=v; break; case 4: out.vpv2=v; break; case 5: out.ipv1=v; break;
          case 6: out.ipv2=v; break; case 7: out.temp=v; break; case 8: out.export_percent=v; break;
          case 9: out.pac=v; break;
        }
      }
    }
    i=j;
  }
  return ok_any;
}

} // namespace acquisition
