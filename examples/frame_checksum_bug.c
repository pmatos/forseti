/* Staged-bug twin of `frame_checksum.c`: the short-frame guard is gone.
 *
 * `sum_bytes` is byte-for-byte the clean file's leaf and stays memory-safe under
 * its own precondition — S2 still reports it ASSUMED_VERIFIED. The defect is in
 * a *caller*: with the guard dropped, `len - HEADER_BYTES` underflows for a
 * frame shorter than the header and `payload_checksum` asks `sum_bytes` to read
 * a near-SIZE_MAX run of bytes from an interior pointer.
 *
 * That is precisely the class of bug an *assumed* precondition hides and a
 * *discharged* one catches: verifying the leaf in isolation says nothing about
 * it, while
 *
 *   `forseti discharge examples/frame_checksum_bug.c --function sum_bytes`
 *
 * reports VIOLATED at the call site, naming `payload_checksum`. `frame_checksum`
 * is unchanged and still discharges — the failure is attributed to one caller,
 * not to the leaf.
 */
#include <stddef.h>
#include <stdint.h>

#define HEADER_BYTES 2

/* The leaf: sums `len` bytes of `buf`. */
uint32_t sum_bytes(const uint8_t *buf, size_t len) {
    uint32_t acc = 0;
    for (size_t i = 0; i < len; i++) {
        acc += buf[i];
    }
    return acc;
}

/* Forwards the whole frame — owes `sum_bytes` exactly what it was given. */
uint32_t frame_checksum(const uint8_t *frame, size_t len) {
    return sum_bytes(frame, len);
}

/* The staged defect: no short-frame guard, so `len - HEADER_BYTES` underflows. */
uint32_t payload_checksum(const uint8_t *frame, size_t len) {
    return sum_bytes(frame + HEADER_BYTES, len - HEADER_BYTES);
}
