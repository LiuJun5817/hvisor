DTS_FILES := $(wildcard *.dts)
DTB_FILES := $(DTS_FILES:.dts=.dtb)

.PHONY: all clean

all: $(DTB_FILES)

%.dtb: %.dts
	dtc -I dts -O dtb $< -o $@

clean:
	rm -f $(DTB_FILES)
