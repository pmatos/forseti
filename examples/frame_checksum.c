/* Compositional discharge target — a leaf unit and the callers that owe it a
 * precondition (RFC-0003 S3).
 *
 * `sum_bytes` is the leaf: memory-safe exactly under the L0 precondition "`buf`
 * is a valid object of `len` bytes". S2 verifies it against a sidecar that
 * *assumes* that (`malloc(len)`), which is honest but nobody's obligation. S3
 * injects the same predicate into a generated copy of this file as a **checked**
 * obligation at `sum_bytes`'s entry, then verifies each caller's own sidecar
 * against that copy — so the assumption is discharged by proof at every call
 * site in the translation unit:
 *
 *   `forseti discharge examples/frame_checksum.c --function sum_bytes`
 *
 * The two callers are the interesting pair. `frame_checksum` forwards the whole
 * buffer unchanged; `payload_checksum` hands on an **interior pointer** with an
 * adjusted length, which is valid only because of its own short-frame guard. The
 * `_bug` twin drops that guard: `len - HEADER_BYTES` then underflows to a huge
 * size_t and the caller asks `sum_bytes` to read far past the frame — a real
 * caller-side bug that the leaf unit, verified in isolation, cannot see.
 *
 * No `main`: the generated sidecars #include this file (or the injected copy).
 */
#include <stddef.h>
#include <stdint.h>

#define HEADER_BYTES 2

/* The leaf: sums `len` bytes of `buf`. `static`, so this translation unit is
 * its whole world — the two callers below are *every* caller, which is what
 * lets the discharge close here instead of exporting the obligation. */
static uint32_t sum_bytes(const uint8_t *buf, size_t len) {
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

/* Checksums the payload only. The guard is load-bearing: without it the
 * subtraction underflows and the obligation below is unsatisfiable. */
uint32_t payload_checksum(const uint8_t *frame, size_t len) {
    if (len < HEADER_BYTES) {
        return 0;
    }
    return sum_bytes(frame + HEADER_BYTES, len - HEADER_BYTES);
}
