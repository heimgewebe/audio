check:
    bash tests/test-audio-safety.sh
    bash scripts/check-audio-safety .
    python3 -m unittest discover -s tests -v
    python3 -m compileall -q scripts tests
