#!/usr/bin/env bash
set -euo pipefail
checker=$(cd "$(dirname "$0")/.." && pwd)/scripts/check-audio-safety
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/safe/runtime"
printf '%s\n' '#!/usr/bin/env bash' 'exec carla-single lv2 example' >"$tmp/safe/runtime/start"
"$checker" "$tmp/safe"
mkdir -p "$tmp/sfizz/runtime"
printf '%s\n' '#!/usr/bin/env bash' 'sfizz_jack instrument.sfz' >"$tmp/sfizz/runtime/start"
if "$checker" "$tmp/sfizz" >/dev/null 2>&1; then
  echo 'sfizz_jack negative control unexpectedly passed' >&2
  exit 1
fi
mkdir -p "$tmp/log/runtime"
printf '%s\n' '#!/usr/bin/env bash' 'player >>session.log 2>&1 &' >"$tmp/log/runtime/start"
if "$checker" "$tmp/log" >/dev/null 2>&1; then
  echo 'unbounded log negative control unexpectedly passed' >&2
  exit 1
fi
echo 'audio safety tests: PASS'
