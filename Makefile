# ---- GUI (minios-image-builder) ----
GUI_LAUNCHER = bin/minios-image-builder
GUI_LIB = lib/*.py
GUI_DESKTOP = share/applications/minios-image-builder.desktop
GUI_STYLE = share/styles/style.css
GUI_CONFIG_READER = helper/minios-image-builder-read-live-config
GUI_CONFIG_POLICY = share/polkit-1/actions/org.minios.imagebuilder.read-live-config.policy
GUI_DOC = doc/minios-image-builder.md
GUI_MAN = manpages/en/minios-image-builder.1

# ---- CLI (minios-image-compose) ----
CLI_SCRIPT = cli/bin/minios-image-compose
CLI_ENGINE = cli/lib/minios_image_compose_engine.py
CLI_COMPLETION = cli/completion/minios-image-compose
CLI_DOC = cli/doc/minios-image-compose.md
CLI_MAN = manpages/en/minios-image-compose.1

BINDIR = usr/bin
LIBDIR = usr/lib/minios-image-builder
APPLICATIONSDIR = usr/share/applications
LOCALEDIR = usr/share/locale
SHAREDIR = usr/share/minios-image-builder
POLKITACTIONSDIR = usr/share/polkit-1/actions
COMPLETIONDIR = usr/share/bash-completion/completions
CLI_ENGINEDIR = usr/lib/minios-image-compose

PO_FILES = $(shell find po -maxdepth 1 -name "*.po")
MO_FILES = $(patsubst %.po,%.mo,$(PO_FILES))
CLI_PO_FILES = $(shell find cli/po -maxdepth 1 -name "*.po" 2>/dev/null)
CLI_MO_FILES = $(patsubst %.po,%.mo,$(CLI_PO_FILES))
MAN_PO_FILES = $(shell find manpages/po -name "*.po" 2>/dev/null)

build: mo cli-mo man

mo: $(MO_FILES)

cli-mo: $(CLI_MO_FILES)

man: $(GUI_MAN) $(CLI_MAN)

%.mo: %.po
	@echo "Generating mo file for $<"
	msgfmt -o $@ $<
	chmod 644 $@

$(GUI_MAN): $(GUI_DOC)
	@echo "Generating man page for $<"
	mkdir -p $(@D)
	pandoc -s -t man $< -o $@
	sed -i 's/^\.TP$$/.TP 4/' $@

$(CLI_MAN): $(CLI_DOC)
	@echo "Generating man page for $<"
	mkdir -p $(@D)
	pandoc -s -t man $< -o $@
	sed -i 's/^\.TP$$/.TP 4/' $@

clean:
	rm -f $(MO_FILES) $(CLI_MO_FILES)

install: build
	# --- GUI ---
	install -d $(DESTDIR)/$(BINDIR) \
				$(DESTDIR)/$(LIBDIR) \
				$(DESTDIR)/$(APPLICATIONSDIR) \
				$(DESTDIR)/$(SHAREDIR)
	cp $(GUI_LAUNCHER) $(DESTDIR)/$(BINDIR)/
	cp $(GUI_LIB) $(DESTDIR)/$(LIBDIR)/
	chmod +x $(DESTDIR)/$(LIBDIR)/main_image_builder.py
	cp $(GUI_DESKTOP) $(DESTDIR)/$(APPLICATIONSDIR)/
	cp $(GUI_STYLE) $(DESTDIR)/$(SHAREDIR)/
	install -Dm755 $(GUI_CONFIG_READER) $(DESTDIR)/$(LIBDIR)/minios-image-builder-read-live-config
	install -Dm644 $(GUI_CONFIG_POLICY) $(DESTDIR)/$(POLKITACTIONSDIR)/org.minios.imagebuilder.read-live-config.policy
	@for MO_FILE in $(MO_FILES); do \
		LOCALE=$$(basename $$MO_FILE .mo); \
		install -Dm644 "$$MO_FILE" "$(DESTDIR)/$(LOCALEDIR)/$$LOCALE/LC_MESSAGES/minios-image-builder.mo"; \
	done
	# --- CLI ---
	install -Dm755 $(CLI_SCRIPT) $(DESTDIR)/$(BINDIR)/minios-image-compose
	install -Dm644 $(CLI_ENGINE) $(DESTDIR)/$(CLI_ENGINEDIR)/minios_image_compose_engine.py
	install -Dm644 $(CLI_COMPLETION) $(DESTDIR)/$(COMPLETIONDIR)/minios-image-compose
	@for MO_FILE in $(CLI_MO_FILES); do \
		LOCALE=$$(basename $$MO_FILE .mo); \
		install -Dm644 "$$MO_FILE" "$(DESTDIR)/$(LOCALEDIR)/$$LOCALE/LC_MESSAGES/minios-image-compose.mo"; \
	done

test:
	python3 -m pytest -q
	bats cli/tests/minios-image-compose.bats

check:
	python3 -m py_compile lib/*.py
	python3 -m py_compile $(GUI_CONFIG_READER)
	bash -n cli/bin/minios-image-compose
	desktop-file-validate $(GUI_DESKTOP)
	@for PO_FILE in $(PO_FILES) $(CLI_PO_FILES) $(MAN_PO_FILES); do \
		msgfmt --check --output-file=/dev/null "$$PO_FILE" || exit 1; \
	done

.PHONY: build mo cli-mo man clean install test check
