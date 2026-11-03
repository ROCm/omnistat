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

// Simple HIP vector addition workload for hardware counter validation.
// Each element performs one VALU add instruction, ensuring measurable
// GPU activity for counters like GRBM_GUI_ACTIVE and SQ_INSTS_VALU.
//
// Usage: vector_add <num_elements>

#include "common.hpp"

__global__ void vector_add_kernel(const float *A, const float *B, float *C,
                                  int N) {
  int i = blockDim.x * blockIdx.x + threadIdx.x;
  if (i < N) {
    C[i] = A[i] + B[i];
  }
}

int main(int argc, char *argv[]) {
  if (argc < 2) {
    fprintf(stderr, "Usage: %s <num_elements>\n", argv[0]);
    return 1;
  }

  int N = atoi(argv[1]);
  if (N <= 0) {
    fprintf(stderr, "num_elements must be > 0\n");
    return 1;
  }

  size_t bytes = N * sizeof(float);

  // Allocate host memory
  float *h_A = (float *)malloc(bytes);
  float *h_B = (float *)malloc(bytes);
  float *h_C = (float *)malloc(bytes);

  if (!h_A || !h_B || !h_C) {
    fprintf(stderr, "Host allocation failed\n");
    return 1;
  }

  // Initialize host vectors
  for (int i = 0; i < N; i++) {
    h_A[i] = 1.0f;
    h_B[i] = 2.0f;
  }

  // Allocate device memory
  float *d_A, *d_B, *d_C;
  HIP_CHECK(hipMalloc(&d_A, bytes));
  HIP_CHECK(hipMalloc(&d_B, bytes));
  HIP_CHECK(hipMalloc(&d_C, bytes));

  // Copy to device
  HIP_CHECK(hipMemcpy(d_A, h_A, bytes, hipMemcpyHostToDevice));
  HIP_CHECK(hipMemcpy(d_B, h_B, bytes, hipMemcpyHostToDevice));

  // Launch kernel
  int blockSize = 256;
  int gridSize = (N + blockSize - 1) / blockSize;
  vector_add_kernel<<<gridSize, blockSize>>>(d_A, d_B, d_C, N);
  HIP_CHECK(hipDeviceSynchronize());

  // Copy result back
  HIP_CHECK(hipMemcpy(h_C, d_C, bytes, hipMemcpyDeviceToHost));

  // Verify results
  int errors = 0;
  for (int i = 0; i < N; i++) {
    if (h_C[i] != 3.0f) {
      errors++;
      if (errors <= 5) {
        fprintf(stderr, "Mismatch at %d: expected 3.0, got %f\n", i, h_C[i]);
      }
    }
  }

  // Cleanup
  HIP_CHECK(hipFree(d_A));
  HIP_CHECK(hipFree(d_B));
  HIP_CHECK(hipFree(d_C));
  free(h_A);
  free(h_B);
  free(h_C);

  if (errors > 0) {
    fprintf(stderr, "FAILED: %d errors\n", errors);
    return 1;
  }

  return 0;
}
