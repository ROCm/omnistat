// ---------------------------------------------------------------------------
// MIT License
//
// Copyright (c) 2026 Advanced Micro Devices, Inc. All Rights Reserved.
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
// FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
// IN THE SOFTWARE.
// ---------------------------------------------------------------------------

// Simple HIP application that launches a configurable number of GPU kernels.
// Intended for testing kernel tracing with omnistat.
//
// Usage: launch_kernels <count> [delay_us]
//   count    - number of kernel launches
//   delay_us - microseconds to sleep between launches (default: 0)

#include <cstdio>
#include <cstdlib>
#include <hip/hip_runtime.h>
#include <unistd.h>

#define HIP_CHECK(call)                                                        \
  do {                                                                         \
    hipError_t err = (call);                                                   \
    if (err != hipSuccess) {                                                   \
      fprintf(stderr, "HIP error %d: %s at %s:%d\n", err,                      \
              hipGetErrorString(err), __FILE__, __LINE__);                     \
      exit(1);                                                                 \
    }                                                                          \
  } while (0)

__global__ void empty_kernel() {}

int main(int argc, char *argv[]) {
  if (argc < 2) {
    fprintf(stderr, "Usage: %s <count> [delay_us]\n", argv[0]);
    return 1;
  }

  int count = atoi(argv[1]);
  int delay_us = argc > 2 ? atoi(argv[2]) : 0;

  if (count <= 0) {
    fprintf(stderr, "count must be > 0\n");
    return 1;
  }

  for (int i = 0; i < count; i++) {
    empty_kernel<<<1, 1>>>();
    if (delay_us > 0)
      usleep(delay_us);
  }

  HIP_CHECK(hipDeviceSynchronize());
  return 0;
}
