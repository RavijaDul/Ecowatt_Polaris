#include "codec.hpp"
#include <cstring>
#include <vector>
#include <esp_timer.h>

namespace codec {

static inline void put_u16(std::string& b, uint16_t v) { b.push_back(uint8_t(v & 0xFF)); b.push_back(uint8_t(v >> 8)); }
static inline void put_u32(std::string& b, uint32_t v) { for(int i=0;i<4;++i) b.push_back(uint8_t((v>>(8*i))&0xFF)); }
static inline uint16_t get_u16(const uint8_t* p){ return uint16_t(p[0]) | (uint16_t(p[1])<<8); }
static inline uint32_t get_u32(const uint8_t* p){ return uint32_t(p[0]) | (uint32_t(p[1])<<8) | (uint32_t(p[2])<<16) | (uint32_t(p[3])<<24); }

uint32_t crc32_ieee(const void* data, size_t len) {
  static uint32_t table[256]; static bool init=false;
  if(!init){ for(uint32_t i=0;i<256;++i){ uint32_t c=i; for(int j=0;j<8;++j) c=(c&1)?(0xEDB88320U^(c>>1)):(c>>1); table[i]=c; } init=true; }
  uint32_t crc = 0xFFFFFFFFU;
  auto* p = static_cast<const uint8_t*>(data);
  for(size_t i=0;i<len;++i) crc = table[(crc ^ p[i]) & 0xFF] ^ (crc >> 8);
  return crc ^ 0xFFFFFFFFU;
}

static std::vector<uint16_t> field_view(const acquisition::Sample& s) {
  return { s.vac1, s.iac1, s.fac1, s.vpv1, s.vpv2, s.ipv1, s.ipv2, s.temp, s.export_percent, s.pac };
}

std::string encode_delta_rle_v1(const std::vector<buffer::Record>& recs, std::vector<std::string>& order) {
  order = {"vac1","iac1","fac1","vpv1","vpv2","ipv1","ipv2","temp","export_percent","pac"};
  const uint8_t version = 1, n_fields = (uint8_t)order.size();
  const uint16_t n = (uint16_t)recs.size();
  std::string out; out.reserve(16 + n * order.size());
  out.push_back(version); out.push_back(n_fields);
  out.push_back(uint8_t(n & 0xFF)); out.push_back(uint8_t(n >> 8));
  out.append(4, '\0'); // reserved
  if (n == 0) { put_u32(out, crc32_ieee(out.data(), out.size())); return out; }

  auto init_vals = field_view(recs[0].s);
  for (uint16_t v : init_vals) put_u16(out, v);

  for (size_t f = 0; f < order.size(); ++f) {
    uint16_t prev = init_vals[f]; uint8_t zero_run = 0;
    for (size_t i = 1; i < n; ++i) {
      uint16_t cur = field_view(recs[i].s)[f];
      int16_t d = int16_t(int32_t(cur) - int32_t(prev));
      if (d == 0) {
        if (zero_run == 255) { out.push_back(0x00); out.push_back(zero_run); zero_run = 0; }
        ++zero_run;
      } else {
        if (zero_run) { out.push_back(0x00); out.push_back(zero_run); zero_run = 0; }
        out.push_back(0x01); out.push_back(uint8_t(d & 0xFF)); out.push_back(uint8_t((d >> 8) & 0xFF));
        prev = cur;
      }
    }
    if (zero_run) { out.push_back(0x00); out.push_back(zero_run); }
  }
  put_u32(out, crc32_ieee(out.data(), out.size()));
  return out;
}

bool decode_delta_rle_v1(const std::string& blob, std::vector<acquisition::Sample>& out_samples, std::vector<std::string>* out_order) {
  if (blob.size() < 12) return false;
  const uint8_t* p = (const uint8_t*)blob.data();
  size_t off = 0;
  if (p[off++] != 1) return false;
  uint8_t nf = p[off++]; uint16_t n = uint16_t(p[off]) | (uint16_t(p[off+1]) << 8); off += 2;
  off += 4; // reserved
  if (blob.size() < off + nf*2 + 4) return false;

  std::vector<uint16_t> last(nf);
  for (int f=0; f<nf; ++f) { last[f] = get_u16(p+off); off+=2; }
  std::vector<std::vector<uint16_t>> fields(nf, std::vector<uint16_t>(n));
  for (int f=0; f<nf; ++f) {
    fields[f][0]=last[f]; size_t produced=0;
    while(produced < n-1){
      if(off >= blob.size()-4) return false;
      uint8_t op = p[off++];
      if(op==0x00){
        if(off >= blob.size()-4) return false;
        uint8_t len = p[off++];
        for(uint8_t k=0;k<len;++k) fields[f][1+produced++] = last[f];
      } else if(op==0x01){
        if(off+2 > blob.size()-4) return false;
        int16_t d = int16_t(p[off] | (p[off+1]<<8)); off+=2;
        size_t idx = 1 + produced++; uint16_t cur = uint16_t(int32_t(last[f]) + int32_t(d));
        fields[f][idx] = cur; last[f]=cur;
      } else return false;
    }
  }
  uint32_t crc = get_u32(p + (blob.size()-4));
  if(crc != crc32_ieee(blob.data(), blob.size()-4)) return false;

  out_samples.resize(n);
  for (size_t i=0;i<n;++i){
    acquisition::Sample s{};
    s.vac1=fields[0][i]; s.iac1=fields[1][i]; s.fac1=fields[2][i];
    s.vpv1=fields[3][i]; s.vpv2=fields[4][i]; s.ipv1=fields[5][i];
    s.ipv2=fields[6][i]; s.temp=fields[7][i]; s.export_percent=fields[8][i]; s.pac=fields[9][i];
    out_samples[i]=s;
  }
  if(out_order) *out_order={"vac1","iac1","fac1","vpv1","vpv2","ipv1","ipv2","temp","export_percent","pac"};
  return true;
}

static inline size_t uncompressed_bytes_per_sample() { return 28; } // 10*2 + 8 (ts)

BenchResult run_benchmark_delta_rle_v1(const std::vector<buffer::Record>& recs) {
  BenchResult r; r.method="delta_rle_v1"; r.n_samples=recs.size();
  r.orig_bytes = recs.size()*uncompressed_bytes_per_sample();
  if(recs.empty()){ r.lossless_ok=true; return r; }
  std::vector<std::string> order; int64_t t0=esp_timer_get_time();
  std::string blob = encode_delta_rle_v1(recs, order);
  int64_t t1=esp_timer_get_time(); r.encode_ms = double(t1-t0)/1000.0; r.comp_bytes = blob.size();
  std::vector<acquisition::Sample> decoded; std::vector<std::string> order_out;
  bool ok = decode_delta_rle_v1(blob, decoded, &order_out);
  if(!ok || decoded.size()!=recs.size()){ r.lossless_ok=false; return r; }
  r.lossless_ok=true;
  for(size_t i=0;i<recs.size();++i){
    const auto& a=recs[i].s; const auto& b=decoded[i];
    if(a.vac1!=b.vac1 || a.iac1!=b.iac1 || a.fac1!=b.fac1 || a.vpv1!=b.vpv1 || a.vpv2!=b.vpv2 ||
       a.ipv1!=b.ipv1 || a.ipv2!=b.ipv2 || a.temp!=b.temp || a.export_percent!=b.export_percent || a.pac!=b.pac){
      r.lossless_ok=false; break;
    }
  }
  return r;
}

} // namespace codec
