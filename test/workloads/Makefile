SOURCES := $(wildcard *.cpp)
TARGETS := $(SOURCES:.cpp=)

all: $(TARGETS)

%: %.cpp
	hipcc $< -o $@

clean:
	rm -f $(TARGETS)

.PHONY: all clean
