// ---------------------------------------------------------------------------
// MIT License
//
// Copyright (c) 2025 - 2026 Advanced Micro Devices, Inc. All Rights Reserved.
//
// Permission is hereby granted, free of charge, to any person obtaining a
// copy of this software and associated documentation files (the "Software"),
// to deal in the Software without restriction, including without limitation
// the rights to use, copy, modify, merge, publish, distribute, sublicense,
// and/or sell copies of the Software, and to permit persons to whom the
// Software is furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
// FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
// DEALINGS IN THE SOFTWARE.
// ---------------------------------------------------------------------------

#pragma once

#include <string_view>
#include <utility>

#if defined(HAS_STD_FORMAT)
#include <format>
namespace fmt = std;
#else
#include <fmt/core.h>
#endif

namespace omnistat::log {

// How much the library reports. Each level adds to the one before it: warning
// reports failures once each, info adds the exit summary even when nothing
// failed, debug adds every repeat of a failure.
enum class Level { Warning = 0, Info, Debug };

// Reads the level and the destination from the environment, and builds the
// prefix that every message carries. Call once from the library entry point.
void init();

// Returns the configured verbosity level.
Level level();

// Stamps the prefix and writes the whole line in one step. Used by message();
// call that instead.
void emit(std::string_view message);

// Format and write one line. Call sites decide whether to report at all, by
// consulting level(); that keeps the condition visible where it applies, and
// means an argument is only built when it is going to be used.
template <typename... Args>
void message(fmt::format_string<Args...> format, Args&&... args) {
    emit(fmt::format(format, std::forward<Args>(args)...));
}

} // namespace omnistat::log
