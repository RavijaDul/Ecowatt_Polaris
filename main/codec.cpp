#include "codec.hpp"                      // L1: include corresponding header with declarations.
#include <cstring>                        // L2: standard header for memcpy etc. (not heavily used here).
#include <vector>                         // L3: std::vector container.

namespace codec {                         // L5: begin codec namespace (encoders/decoders live here).

// ---------- helper functions for serialization ----------
static inline void put_u16(std::string& b, uint16_t v){ // L8: append a 16-bit little-endian integer to string.
    b.push_back(uint8_t(v & 0xFF));                     // L9: low byte.
    b.push_back(uint8_t(v>>8));                         // L10: high byte.
}
static inline void put_u32(std::string& b, uint32_t v){ // L12: append a 32-bit little-endian integer to string.
    for(int i=0;i<4;++i) b.push_back(uint8_t((v>>(8*i))&0xFF)); // L13: four bytes in order.
}
static inline uint16_t get_u16(const uint8_t* p){       // L15: decode 16-bit little-endian from byte array.
    return uint16_t(p[0]) | (uint16_t(p[1])<<8);        // L16
}
static inline uint32_t get_u32(const uint8_t* p){       // L17: decode 32-bit little-endian from byte array.
    return uint32_t(p[0]) | (uint32_t(p[1])<<8) | (uint32_t(p[2])<<16) | (uint32_t(p[3])<<24); // L18
}

// ---------- CRC-32 (IEEE 802.3 polynomial) ----------
uint32_t crc32_ieee(const void* data, size_t len){       // L21: calculate CRC32 over data buffer.
    static uint32_t table[256]; static bool init=false;  // L22: static table built on first call.
    if(!init){                                           // L23: build table once.
        for(uint32_t i=0;i<256;++i){                     // L24: loop all byte values.
            uint32_t c=i;                                // L25
            for(int j=0;j<8;++j)                         // L26: bit loop.
                c=(c&1)?(0xEDB88320^(c>>1)):(c>>1);      // L27: reflected CRC32 update.
            table[i]=c;                                  // L28
        }
        init=true;                                       // L29
    }
    uint32_t crc=0xFFFFFFFF; const uint8_t* p = static_cast<const uint8_t*>(data); // L30: init state, pointer.
    for(size_t i=0;i<len;++i) crc = table[(crc^p[i])&0xFF] ^ (crc>>8);            // L31: process each byte.
    return crc ^ 0xFFFFFFFF;                              // L32: final XOR.
}

// ---------- convert Sample → field array ----------
static std::vector<uint16_t> field_view(const acquisition::Sample& s){ // L35
    return { s.vac1, s.iac1, s.fac1, s.vpv1, s.vpv2, s.ipv1, s.ipv2, s.temp, s.export_percent, s.pac }; // L36
}

// ---------- encoder (delta+RLE v1) ----------
std::string encode_delta_rle_v1(const std::vector<buffer::Record>& recs,
                                std::vector<std::string>& order){   // L40: compress a batch into binary blob.
    order = {"vac1","iac1","fac1","vpv1","vpv2","ipv1","ipv2","temp","export_percent","pac"}; // L42: field order.
    const uint8_t version = 1, n_fields = (uint8_t)order.size();   // L43: format version and field count.
    const uint16_t n = (uint16_t)recs.size();                      // L44: sample count.

    std::string out; out.reserve(16 + n*order.size());             // L46: pre-allocate rough space.

    out.push_back(version); out.push_back(n_fields);               // L48: version + n_fields.
    out.push_back(uint8_t(n & 0xFF)); out.push_back(uint8_t(n>>8));// L49: sample count (little-endian).
    out.append(4, '\0');                                           // L50: reserved 4 bytes.

    if(n==0){                                                      // L52: empty batch shortcut.
        put_u32(out, crc32_ieee(out.data(), out.size()));          // L53: CRC over header only.
        return out;                                                // L54
    }

    auto init_vals = field_view(recs[0].s);                        // L56: absolute initial values.
    for(uint16_t v: init_vals) put_u16(out, v);                    // L57: write them.

    for(size_t f=0; f<order.size(); ++f){                          // L59: for each field independently.
        uint16_t prev = init_vals[f];                              // L60: last value.
        uint8_t zero_run = 0;                                      // L61: run length counter.
        for(size_t i=1;i<n;++i){                                   // L62: loop over subsequent samples.
            uint16_t cur = field_view(recs[i].s)[f];               // L63: current value.
            int16_t d = int16_t(int32_t(cur) - int32_t(prev));     // L64: signed delta.
            if(d==0){                                              // L65: repeat case.
                if(zero_run==255){                                 // L66: flush when run maxed.
                    out.push_back(0x00); out.push_back(zero_run);  // L67
                    zero_run=0;                                    // L68
                }
                ++zero_run;                                        // L69
            } else {                                               // L70: delta case.
                if(zero_run){ out.push_back(0x00); out.push_back(zero_run); zero_run=0; } // L71: flush pending repeats.
                out.push_back(0x01);                               // L72: delta opcode.
                out.push_back(uint8_t(d & 0xFF));                  // L73: low byte.
                out.push_back(uint8_t((d>>8)&0xFF));               // L74: high byte.
                prev = cur;                                        // L75: update last value.
            }
        }
        if(zero_run){ out.push_back(0x00); out.push_back(zero_run); } // L77: flush final run.
    }

    put_u32(out, crc32_ieee(out.data(), out.size()));              // L80: append CRC32 at end.
    return out;                                                    // L81
}

// ---------- decoder (optional) ----------
bool decode_delta_rle_v1(const std::string& blob,
                         std::vector<acquisition::Sample>& out_samples,
                         std::vector<std::string>* out_order){    // L85
    if(blob.size() < 1+1+2+4+4) return false;                      // L86: check min length.

    const uint8_t* p = (const uint8_t*)blob.data(); size_t off=0;  // L88: pointer and offset.
    if(p[off++]!=1) return false;                                  // L89: version must be 1.
    uint8_t nf = p[off++];                                         // L90: number of fields.
    uint16_t n = uint16_t(p[off]) | (uint16_t(p[off+1])<<8);       // L91: number of samples.
    off += 2;                                                      // L92
    off += 4;                                                      // L93: reserved skip.

    if(n==0){                                                      // L95: handle empty.
        uint32_t crc = get_u32(p+off);                             // L96: CRC at tail.
        return crc == crc32_ieee(blob.data(), off);                // L97
    }
    if(blob.size() < off + nf*2 + 4) return false;                 // L98: ensure enough space.

    std::vector<uint16_t> last(nf);                                // L100: initial values vector.
    for(int f=0; f<nf; ++f){ last[f]=get_u16(p+off); off+=2; }     // L101: read absolute first values.

    std::vector<std::vector<uint16_t>> fields(nf, std::vector<uint16_t>(n)); // L103: output matrix.
    for(int f=0; f<nf; ++f){                                       // L104
        fields[f][0]=last[f];                                      // L105: set first sample.
        size_t produced=0;                                         // L106: count produced after first.
        while(produced < n-1){                                     // L107: decode until all samples.
            if(off >= blob.size()-4) return false;                 // L108: must leave room for CRC.
            uint8_t op = p[off++];                                 // L109: read opcode.
            if(op==0x00){                                          // L110: repeat run.
                if(off>=blob.size()-4) return false;               // L111: check len.
                uint8_t len = p[off++];                            // L112
                for(uint8_t k=0;k<len;++k){                        // L113
                    fields[f][1 + produced++] = last[f];           // L114
                }
            } else if(op==0x01){                                   // L116: delta.
                if(off+2>blob.size()-4) return false;              // L117: check bytes.
                int16_t d = int16_t(p[off] | (p[off+1]<<8));       // L118
                off+=2;                                            // L119
                size_t idx = 1 + produced++;                       // L120
                uint16_t cur = uint16_t(int32_t(last[f]) + int32_t(d)); // L121
                fields[f][idx]=cur;                                // L122
                last[f]=cur;                                       // L123
            } else {                                               // L124
                return false;                                      // L125: unknown opcode.
            }
        }
    }

    uint32_t crc = get_u32(p + (blob.size()-4));                   // L128: read CRC at tail.
    if(crc != crc32_ieee(blob.data(), blob.size()-4)) return false;// L129: verify CRC.

    out_samples.resize(n);                                         // L131: resize output.
    for(size_t i=0;i<n;++i){                                       // L132
        acquisition::Sample s{};                                   // L133
        s.vac1=fields[0][i]; s.iac1=fields[1][i]; s.fac1=fields[2][i]; // L134
        s.vpv1=fields[3][i]; s.vpv2=fields[4][i];                   // L135
        s.ipv1=fields[5][i]; s.ipv2=fields[6][i];                   // L136
        s.temp=fields[7][i]; s.export_percent=fields[8][i]; s.pac=fields[9][i]; // L137
        out_samples[i]=s;                                           // L138
    }
    if(out_order){ *out_order={"vac1","iac1","fac1","vpv1","vpv2","ipv1","ipv2","temp","export_percent","pac"}; } // L140
    return true;                                                   // L141
}

} // namespace codec                                               // L143
