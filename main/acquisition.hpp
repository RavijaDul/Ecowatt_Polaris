#pragma once
#include <cstdint>
#include <string>
#include <vector>

namespace acquisition {

struct Sample {
  uint16_t vac1;
  uint16_t iac1;
  uint16_t fac1;
  uint16_t vpv1;
  uint16_t vpv2;
  uint16_t ipv1;
  uint16_t ipv2;
  uint16_t temp;
  uint16_t export_percent;
  uint16_t pac;
};

class Acquisition {
public:
  Acquisition(const std::string& base_url, const std::string& api_key_b64);
  bool read_group(uint16_t addr, uint16_t count, std::vector<uint16_t>& out_regs);
  bool set_export_power(int percent, const std::string& reason_tag);
  bool read_all(Sample& out);
  // NEW: read only selected fields (enum values 0..9 map to register addresses 0..9)
  bool read_selected(const std::vector<int>& field_ids, Sample& out);
private:
  std::string base_url_;
  std::string api_key_;
};

} // namespace acquisition
