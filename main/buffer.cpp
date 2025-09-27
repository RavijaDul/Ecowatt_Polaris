#include "buffer.hpp"                       // L1: Pull in the Ring/Record declarations for implementation.

namespace buffer {                          // L3: Start the buffer namespace to scope Ring/Record.

Ring::Ring(size_t capacity) : cap_(capacity), recs_(capacity) {} // L5: Constructor initializes capacity and pre-sizes storage.

void Ring::push(const Record& r) {          // L7: Push one sample into the ring buffer (overwrites oldest when full).
    std::lock_guard<std::mutex> lk(mu_);    // L8: Lock for thread safety (protect indices and storage).
    recs_[w_] = r;                           // L9: Write the new record at the current write index.
    w_ = (w_ + 1) % cap_;                    // L10: Advance write index with wrap-around.
    if (count_ < cap_) ++count_;             // L11: If not full, grow the element count.
    else r_ = (r_ + 1) % cap_; // overwrite oldest // L12: If full, advance read index to drop the oldest element.
}

std::vector<Record> Ring::snapshot_and_clear() { // L15: Take a time-ordered snapshot of all items, then empty the ring.
    std::lock_guard<std::mutex> lk(mu_);     // L16: Lock to create a consistent snapshot.
    std::vector<Record> out;                 // L17: Output vector for the snapshot.
    out.reserve(count_);                     // L18: Reserve exactly the number of items for efficiency.
    for (size_t i = 0; i < count_; ++i) out.push_back(recs_[(r_ + i) % cap_]); // L19: Copy from read index in order.
    r_ = w_ = count_ = 0;                    // L20: Clear the ring by resetting read, write, and count.
    return out;                              // L21: Return the snapshot.
}

size_t Ring::size() const { std::lock_guard<std::mutex> lk(mu_); return count_; } // L24: Thread-safe size query.
size_t Ring::capacity() const { return cap_; }                                    // L25: Return maximum capacity.

} // namespace buffer                        // L27: End of buffer namespace.
