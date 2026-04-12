SHELL := /bin/bash

.PHONY: tree
tree:
	find . -maxdepth 3 | sort

.PHONY: verify-docs
verify-docs:
	test -f README.md
	test -f docs/product-proposal.md
	test -f docs/governance.md

