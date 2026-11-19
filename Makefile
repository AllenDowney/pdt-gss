PYTHON ?= python
# A PublicDataToolkit checkout. Or `pip install -e $(PDT)` and run `pdt check`.
PDT ?= ../PublicDataToolkit
# An Rscript with jsonlite, e.g. from the pdt-xlang conda env.
RSCRIPT ?= Rscript

# gssrdoc's documentation table, pinned in sources.yaml (id: gssrdoc).
GSSRDOC_COMMIT = 112286deecd7e11b605afda8734ac1c08304726c
GSSRDOC_SHA256 = 2e55bf5640ca8f291332c4d6a0485f4cecdbe9d64164baa6e2da7e87bb4661fe
GSSRDOC_RDA = data/raw/gssrdoc-0.9/gss_doc.rda
GSSRDOC_JSON = data/interim/gssrdoc-0.9.json

.PHONY: gssrdoc build views check

## Fetch gssrdoc's gss_doc.rda at the pinned commit, check it, and convert it to JSON
gssrdoc: $(GSSRDOC_JSON)

$(GSSRDOC_RDA):
	mkdir -p $(dir $@)
	curl -sSfL -o $@ https://raw.githubusercontent.com/kjhealy/gssrdoc/$(GSSRDOC_COMMIT)/data/gss_doc.rda
	echo "$(GSSRDOC_SHA256)  $@" | sha256sum -c -

$(GSSRDOC_JSON): $(GSSRDOC_RDA) build/convert_gssrdoc.R
	mkdir -p $(dir $@)
	$(RSCRIPT) build/convert_gssrdoc.R $< $@

## Build metadata records and links from data/raw (build/build_slice.py, build/build_links.py)
build: $(GSSRDOC_JSON)
	$(PYTHON) build/build_slice.py
	$(PYTHON) build/build_links.py

## Generate agent/, docs/ and export/ from the records (pdt views)
views:
	PYTHONPATH=$(PDT)/lib $(PYTHON) -m pdt views .

## Check the repo against the PublicDataToolkit standard (pdt check)
check:
	PYTHONPATH=$(PDT)/lib $(PYTHON) -m pdt check .
