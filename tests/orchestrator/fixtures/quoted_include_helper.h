#ifndef QUOTED_INCLUDE_HELPER_H
#define QUOTED_INCLUDE_HELPER_H
#include <stdint.h>

static inline int64_t saturate_abs(int64_t x) {
    if (x == INT64_MIN) return INT64_MAX;
    return (x < 0) ? -x : x;
}
#endif
