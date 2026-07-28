check:
    bash tests/test-audio-safety.sh
    bash scripts/check-audio-safety .
    python3 -m unittest discover -s tests -v
    python3 -m compileall -q scripts tests

doctor:
    ./scripts/audio-doctor --pretty

truth output="${XDG_STATE_HOME:-$HOME/.local/state}/audio/truth/latest.v1.json":
    ./scripts/audio-truth capture --output "{{output}}"

truth-verify report="${XDG_STATE_HOME:-$HOME/.local/state}/audio/truth/latest.v1.json":
    ./scripts/audio-truth verify "{{report}}"

truth-drift before after output="${XDG_STATE_HOME:-$HOME/.local/state}/audio/truth/drift.v1.json":
    ./scripts/audio-truth compare "{{before}}" "{{after}}" --output "{{output}}"

reference-tone output="/tmp/audio-reference-1khz-minus20dbfs.wav":
    ./scripts/generate-audio-reference "{{output}}" --kind tone --dbfs -20 --duration 5

physical-status:
    ./scripts/audio-physical status

plan profile="desktop-mixed":
    ./scripts/audio-plan "{{profile}}"

calibration-pack pack output:
    ./scripts/create-calibration-pack "{{pack}}" "{{output}}"

level wav:
    ./scripts/analyze-audio-level "{{wav}}"

whale-doctor:
    python3 scripts/whale_live.py doctor

whale-demo output="/tmp/buckelwal-live-voice-v1-demo.wav":
    python3 scripts/whale_live.py demo "{{output}}"

whale-start:
    python3 scripts/whale_live.py start

whale-status:
    python3 scripts/whale_live.py status

whale-stop:
    python3 scripts/whale_live.py stop
