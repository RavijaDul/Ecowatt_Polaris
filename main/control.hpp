#pragma once
#include <vector>
#include <string>
#include <cstdint>

namespace control {

enum FieldId {
  VAC1=0, IAC1=1, FAC1=2, VPV1=3, VPV2=4, IPV1=5, IPV2=6, TEMP=7, EXPORT_PERCENT=8, PAC=9
};

struct RuntimeConfig {
  uint32_t sampling_interval_ms = 5000;
  std::vector<FieldId> fields = { VAC1,IAC1,FAC1,VPV1,VPV2,IPV1,IPV2,TEMP,EXPORT_PERCENT,PAC };
};

struct PendingCommand {
  bool has_cmd = false;
  int export_pct = -1;
  uint64_t received_at_ms = 0;
};
struct CommandResult {
  bool has_result = false;
  bool success = false;
  uint64_t executed_at_ms = 0;
  int value = -1;
};

bool map_field_names(const std::vector<std::string>& names, std::vector<FieldId>& out);
std::string to_json_status(const CommandResult& r);

} // namespace control
