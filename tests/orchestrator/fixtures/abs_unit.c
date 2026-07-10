/* The my_abs verification unit as a *kernel slice*: the function only, with no
 * main and no assert. The property under check is supplied by the synthesized
 * harness (#64), not by this file — so a run over it proves the loop consumes a
 * store-sourced property with no hand-written property in the path (#66). The
 * int64_t spelling resolves via the harness's own <stdint.h> include. */
int64_t my_abs(int64_t x) {
    return (x < 0) ? -x : x;
}
