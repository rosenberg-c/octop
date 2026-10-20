# Mermaid diagram rendering lives in Makefile.mermaid when available.
-include Makefile.mermaid

.PHONY: test
test:
	python3 -m unittest discover -s tests -p 'test_octop.py'

.PHONY: install-octop
install-octop:
	mkdir -p "$(HOME)/.local/bin"
	ln -sfn "$(CURDIR)/bin/octop.py" "$(HOME)/.local/bin/octop"

.PHONY: uninstall-octop
uninstall-octop:
	rm -f "$(HOME)/.local/bin/octop"
