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

#include "log.hpp"

#include <cctype>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <syncstream>
#include <string>
#include <vector>
#include <unistd.h>

namespace omnistat::log {

namespace {

// All three are constinit, and that is load-bearing rather than decorative:
// constant initialisation means there is no dynamic init to order against other
// translation units, and nothing here has a destructor that could run before the
// exit summary, which is written during finalisation. The keyword makes the
// compiler enforce what would otherwise be a comment.
//
// init() overwrites the placeholder with "[host][pid][omnistat] ". The
// placeholder, and the two defaults below, keep a message emitted before init()
// -- or from a target that never calls it -- identifiable and somewhere useful.
// Sized from the format rather than guessed: the host, a pid_t rendered by %d
// (10 digits for a 32-bit int, plus a sign), and the fixed punctuation with its
// NUL. snprintf would truncate rather than overflow, but then the prefix would
// quietly go short.
constexpr std::size_t PREFIX_SIZE = HOST_NAME_MAX + 11 + sizeof("[][][omnistat] ");

constinit char g_prefix[PREFIX_SIZE] = "[omnistat] ";
constinit Level g_level = Level::Warning;
constinit std::ostream* g_destination = &std::cerr;

std::string hostname() {
    char host[HOST_NAME_MAX + 1] = {};
    if (gethostname(host, sizeof(host) - 1) != 0) {
        return "unknown";
    }
    return host;
}

Level resolve_level(std::vector<std::string>& deferred) {
    const char* value = std::getenv("OMNISTAT_TRACE_LOG_LEVEL");
    if (value == nullptr || *value == '\0') {
        return Level::Warning;
    }

    std::string setting{value};
    for (char& c : setting) {
        c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    }

    if (setting == "DEBUG") {
        return Level::Debug;
    }

    if (setting == "INFO") {
        return Level::Info;
    }

    if (setting != "WARNING") {
        deferred.emplace_back("invalid OMNISTAT_TRACE_LOG_LEVEL value (" + std::string(value) +
                              "); using warning");
    }

    return Level::Warning;
}

std::ostream* resolve_destination(std::vector<std::string>& deferred) {
    const char* value = std::getenv("OMNISTAT_TRACE_LOG_OUTPUT");
    const std::string setting{value == nullptr ? "" : value};

    if (setting.empty() || setting == "stderr") {
        return &std::cerr;
    }

    if (setting == "stdout") {
        return &std::cout;
    }

    // Anything else is a path prefix: ranks sharing a node would otherwise
    // overwrite one another, so append the host and pid. Never truncated.
    const std::string path = setting + "." + hostname() + "." + std::to_string(getpid());

    auto* file = new std::ofstream(path, std::ios::app);
    if (!file->is_open()) {
        deferred.emplace_back("cannot write to " + path + "; using stderr");
        return &std::cerr;
    }

    return file;
}

} // namespace

void init() {
    std::snprintf(g_prefix, sizeof(g_prefix), "[%s][%d][omnistat] ", hostname().c_str(), getpid());

    std::vector<std::string> deferred;
    g_level = resolve_level(deferred);
    g_destination = resolve_destination(deferred);

    for (const std::string& message : deferred) {
        emit(message);
    }
}

Level level() {
    return g_level;
}

void emit(std::string_view message) {
    std::osyncstream(*g_destination) << g_prefix << message << std::endl;
}

} // namespace omnistat::log
