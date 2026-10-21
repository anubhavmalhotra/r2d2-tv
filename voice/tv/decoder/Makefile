CC ?= cc
PKG_CONFIG ?= pkg-config
CFLAGS ?= -O2
CFLAGS += -std=c11 -Wall -Wextra -Werror
SBC_CFLAGS = $(shell $(PKG_CONFIG) --cflags sbc)
SBC_LIBS = $(shell $(PKG_CONFIG) --libs sbc)

.PHONY: all check-sbc clean
all: decode-reports

check-sbc:
	@command -v $(PKG_CONFIG) >/dev/null || { echo 'pkg-config is required' >&2; exit 1; }
	@$(PKG_CONFIG) --exists sbc || { echo 'System libsbc development files are required' >&2; exit 1; }

decode-reports: decode_reports.c capture_core.c capture_core.h | check-sbc
	$(CC) $(CPPFLAGS) $(CFLAGS) $(SBC_CFLAGS) decode_reports.c capture_core.c $(LDFLAGS) $(SBC_LIBS) -o $@

clean:
	rm -f decode-reports
