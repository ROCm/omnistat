SOURCES := $(wildcard *.cpp)
TARGETS := $(SOURCES:.cpp=)

all: $(TARGETS)

OFFLOAD_ARCH ?=

HIPCC_FLAGS := $(foreach arch,$(OFFLOAD_ARCH),--offload-arch=$(arch))

%: %.cpp
	hipcc $(HIPCC_FLAGS) $< -o $@

clean:
	rm -f $(TARGETS)

.PHONY: all clean
