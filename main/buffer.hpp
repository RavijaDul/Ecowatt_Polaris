#pragma once                                  // L1: Single-inclusion guard for this header.

#include <cstdint>                            // L3: Fixed-width integer types (uint64_t).
#include <vector>                             // L4: std::vector for storage and snapshots.
#include <mutex>                              // L5: std::mutex for thread-safe access.
#include "acquisition.hpp"                    // L6: For acquisition::Sample inside Record.

namespace buffer {                            // L8: Begin buffer namespace.

struct Record {                               // L10: One buffered item: timestamp + sample payload.
    uint64_t epoch_ms;                        // L11: Wall-clock timestamp in milliseconds since epoch.
    acquisition::Sample s;                    // L12: Raw inverter sample (10 registers).
};

class Ring {                                  // L15: Fixed-capacity ring buffer with overwrite-oldest policy.
public:
    explicit Ring(size_t capacity);           // L17: Construct with a maximum number of records.
    void push(const Record& r);               // L18: Append one record (drops oldest if full).
    std::vector<Record> snapshot_and_clear(); // L19: Atomically read all items (time order) and empty the buffer.
    size_t size() const;                      // L20: Current number of stored records.
    size_t capacity() const;                  // L21: Maximum number of storable records.

private:
    size_t cap_;                              // L24: Fixed capacity set at construction.
    mutable std::mutex mu_;                   // L25: Mutex protecting indices and storage (mutable for const size()).
    std::vector<Record> recs_;                // L26: Underlying storage pre-sized to capacity.
    size_t r_ = 0, w_ = 0, count_ = 0;        // L27: Read index, write index, and current count.
};

} // namespace buffer                          // L30: End buffer namespace.
