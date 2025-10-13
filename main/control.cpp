#include "control.hpp"
#include <algorithm>
#include <cctype>

namespace control {

static std::string lower(std::string s){ for(char& c: s) c=std::tolower((unsigned char)c); return s; }

bool map_field_names(const std::vector<std::string>& names, std::vector<FieldId>& out){
  out.clear();
  for(auto n: names){
    n = lower(n);
    if(n=="voltage" || n=="vac1") out.push_back(VAC1);
    else if(n=="current" || n=="iac1") out.push_back(IAC1);
    else if(n=="frequency" || n=="fac1") out.push_back(FAC1);
    else if(n=="vpv1") out.push_back(VPV1);
    else if(n=="vpv2") out.push_back(VPV2);
    else if(n=="ipv1") out.push_back(IPV1);
    else if(n=="ipv2") out.push_back(IPV2);
    else if(n=="temperature" || n=="temp") out.push_back(TEMP);
    else if(n=="export_percent" || n=="export") out.push_back(EXPORT_PERCENT);
    else if(n=="pac" || n=="power") out.push_back(PAC);
    else return false;
  }
  if(out.empty()) return false;
  std::sort(out.begin(), out.end());
  out.erase(std::unique(out.begin(), out.end()), out.end());
  return true;
}

std::string to_json_status(const CommandResult& r){
  if(!r.has_result) return "{}";
  char buf[160];
  std::snprintf(buf, sizeof(buf),
    "{\"command_result\":{\"status\":\"%s\",\"executed_at\":%llu,\"value\":%d}}",
    r.success ? "success":"failure",
    (unsigned long long)r.executed_at_ms, r.value);
  return std::string(buf);
}

} // namespace control
