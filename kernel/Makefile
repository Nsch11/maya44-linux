KDIR ?= /lib/modules/$(shell uname -r)/build
# Kbuild canonicalizes symlinks and cannot build under a path with spaces.
BUILD_DIR ?= /tmp/maya44e-build-$(shell id -u)
.PHONY: all clean
all:
	@mkdir -p -- "$(BUILD_DIR)"
	@cp -- Kbuild maya44e.c "$(BUILD_DIR)/"
	$(MAKE) -C "$(KDIR)" M="$(BUILD_DIR)" modules
	@cp -- "$(BUILD_DIR)/snd-maya44e.ko" .
clean:
	$(MAKE) -C "$(KDIR)" M="$(BUILD_DIR)" clean
	$(RM) snd-maya44e.ko
