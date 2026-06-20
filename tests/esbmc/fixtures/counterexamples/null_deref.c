#include <stdlib.h>

int main(void) {
  int *p = malloc(sizeof(int));
  *p = 5; /* malloc may return NULL: dereference failure */
  free(p);
  return 0;
}
