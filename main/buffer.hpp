#pragma once
#include <cstdint>
#include <vector>
#include <mutex>
#include "acquisition.hpp"

namespace buffer {

struct Record {
  uint64_t epoch_ms;
  acquisition::Sample s;
};

class Ring {
public:
  explicit Ring(size_t capacity);
  void push(const Record& r);
  std::vector<Record> snapshot_and_clear();
  size_t size() const;
  size_t capacity() const;
private:
  size_t cap_;
  mutable std::mutex mu_;
  std::vector<Record> recs_;
  size_t r_=0, w_=0, count_=0;
};

} // namespace buffer
