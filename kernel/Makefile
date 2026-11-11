KDIR ?= /lib/modules/$(shell uname -r)/build
.PHONY: all clean

# Kbuild needs a path without spaces. Each invocation gets its own directory.
all:
	@set -eu; \
	build_dir=$$(mktemp -d /tmp/maya44e-build.XXXXXXXXXX); \
	artifact=; \
	trap 'rm -rf -- "$$build_dir"; [ -z "$$artifact" ] || rm -f -- "$$artifact"' EXIT; \
	trap 'exit 1' HUP INT TERM; \
	cp -- Kbuild maya44e.c "$$build_dir/"; \
	$(MAKE) -C "$(KDIR)" M="$$build_dir" modules; \
	artifact=$$(mktemp ./.snd-maya44e.ko.XXXXXXXXXX); \
	cp --preserve=mode -- "$$build_dir/snd-maya44e.ko" "$$artifact"; \
	mv -fT -- "$$artifact" snd-maya44e.ko

clean:
	$(RM) snd-maya44e.ko
