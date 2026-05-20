SOURCES := $(wildcard *.cpp)
HEADERS := $(wildcard *.hpp)
TARGETS := $(SOURCES:.cpp=)

all: $(TARGETS)

OFFLOAD_ARCH ?=

HIPCC_FLAGS := $(foreach arch,$(OFFLOAD_ARCH),--offload-arch=$(arch))

%: %.cpp $(HEADERS)
	hipcc $(HIPCC_FLAGS) $< -o $@

clean:
	rm -f $(TARGETS)

.PHONY: all clean
