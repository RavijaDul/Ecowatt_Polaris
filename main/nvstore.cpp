#include "nvstore.hpp"
#include "nvs_flash.h"
#include "nvs.h"
#include <vector>

namespace nvstore {

void init(){
  static bool inited=false; if(inited) return;
  esp_err_t r = nvs_flash_init();
  if (r == ESP_ERR_NVS_NO_FREE_PAGES || r == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    nvs_flash_erase(); nvs_flash_init();
  }
  inited = true;
}

bool get_u64(const char* ns, const char* key, uint64_t& out){
  nvs_handle_t h; if(nvs_open(ns, NVS_READONLY, &h)!=ESP_OK) return false;
  esp_err_t e = nvs_get_u64(h, key, &out); nvs_close(h);
  return e==ESP_OK;
}
void set_u64(const char* ns, const char* key, uint64_t v){
  nvs_handle_t h; if(nvs_open(ns, NVS_READWRITE, &h)!=ESP_OK) return;
  nvs_set_u64(h, key, v); nvs_commit(h); nvs_close(h);
}

bool get_str(const char* ns, const char* key, std::string& out){
  nvs_handle_t h; if(nvs_open(ns, NVS_READONLY, &h)!=ESP_OK) return false;
  size_t len=0; if(nvs_get_str(h, key, nullptr, &len)!=ESP_OK){ nvs_close(h); return false; }
  std::vector<char> buf(len+1);
  if(nvs_get_str(h, key, buf.data(), &len)!=ESP_OK){ nvs_close(h); return false; }
  nvs_close(h); out.assign(buf.data(), len); return true;
}
void set_str(const char* ns, const char* key, const std::string& v){
  nvs_handle_t h; if(nvs_open(ns, NVS_READWRITE, &h)!=ESP_OK) return;
  nvs_set_str(h, key, v.c_str()); nvs_commit(h); nvs_close(h);
}

} // namespace nvstore
