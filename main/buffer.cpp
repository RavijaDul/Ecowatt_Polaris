#include "buffer.hpp"

namespace buffer {

Ring::Ring(size_t capacity) : cap_(capacity), recs_(capacity) {}

void Ring::push(const Record& r) {
  std::lock_guard<std::mutex> lk(mu_);
  recs_[w_] = r;
  w_ = (w_ + 1) % cap_;
  if (count_ < cap_) ++count_;
  else r_ = (r_ + 1) % cap_;
}

std::vector<Record> Ring::snapshot_and_clear() {
  std::lock_guard<std::mutex> lk(mu_);
  std::vector<Record> out; out.reserve(count_);
  for (size_t i=0;i<count_;++i) out.push_back(recs_[(r_ + i) % cap_]);
  r_=w_=count_=0;
  return out;
}

size_t Ring::size() const { std::lock_guard<std::mutex> lk(mu_); return count_; }
size_t Ring::capacity() const { return cap_; }

} // namespace buffer
