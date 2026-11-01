.PHONY: test lint demo

test:
	python3 -m unittest discover -s tests

lint:
	claude plugin validate ./plugin --strict
	claude plugin validate . --strict

# Reads made-up data through the stand-in tools in tests/fakes.
demo:
	PATH="$$PWD/tests/fakes:$$PATH" python3 plugin/scripts/beforeitbroke.py --at 2026-11-01T14:32:00Z --github demo/shop --kube
