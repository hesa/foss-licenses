JSON_FILES = aliases.json compat_as.json

.PHONY: test

all: test

test:
	@$(foreach var,$(JSON_FILES), echo -n "Check $(var): " && jq . $(var) > /dev/null && echo "OK" ;)
