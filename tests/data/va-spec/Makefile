.SECONDARY:
.DELETE_ON_ERROR:

BUILD_DIR := build
DEF_DIR := def
JSON_DIR := json
IMPORT_DIR := import

vpath %tag $(BUILD_DIR)
vpath %.classes $(BUILD_DIR)

# va-spec is flat: every source lives here. The MSP tools place each class's
# json/def in the right dir on their own — base sources (va-core,
# domain-entities) -> ./json + ./def; each XXX-profile-source.yaml -> XXX/json +
# XXX/def -- so this one Makefile drives all of it (no per-subdirectory Makefiles).
SOURCES := $(wildcard *-source.yaml)
TAGS := $(SOURCES:-source.yaml=.tag)
IMPORTS := $(wildcard $(IMPORT_DIR)/*-source.yaml)
# Sub-namespace output dirs, one per XXX-profile-source.yaml.
PROFILE_NS := $(patsubst %-profile-source.yaml,%,$(wildcard *-profile-source.yaml))

all: build-ordered ${TAGS} prune-tag

build-ordered: | $(BUILD_DIR)

$(BUILD_DIR):
	mkdir $(BUILD_DIR)

%.tag: %.classes %.json-tag %.defs-tag
	touch $(BUILD_DIR)/$@

%.classes: %-source.yaml
	source2classes $< >${BUILD_DIR}/$@

%.json-tag: %-source.yaml ${IMPORTS}
	source2splitjs $<
	touch $(BUILD_DIR)/$@

%.defs-tag: %-source.yaml ${IMPORTS}
	y2t $<
	touch $(BUILD_DIR)/$@

prune-tag: ${TAGS}
	$(MAKE) -f prune.mk
	touch $(BUILD_DIR)/$@

clean:
	rm -rf $(BUILD_DIR) $(DEF_DIR) $(JSON_DIR) $(PROFILE_NS)
