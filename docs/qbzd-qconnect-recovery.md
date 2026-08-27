# QBZD QConnect recovery

`audio-qbzd-qconnect-recovery-v1.service` repairs one bounded failure mode of the
headless Qobuz reference path: QBZD remains alive and authenticated locally, but
QConnect stays in `retrying`/`reconnecting`, has no active session and the Qobuz
cloud repeatedly rejects or fails to establish the control session. This is the
backend state observed alongside stale or contradictory remote-control behavior.

The recovery is deliberately narrower than a generic watchdog. It never
restarts PipeWire, PipeWire-Pulse, WirePlumber, the host, or the Audiozentrale.
A continuously bound `retrying`/`reconnecting` candidate waits 90 seconds before
it may first cycle only QConnect with the verified live QBZD image: `qbzd qconnect
disable` followed by `qbzd qconnect enable`. A terminal `exhausted` candidate
must still survive a prior bound observation, but does not repeat QBZD's already
consumed reconnect window. A broader
`systemctl --user try-restart qbzd.service` remains the fallback only after the
original five-minute stuck window. `try-restart` is used so an intentionally
inactive QBZD is not started by the watchdog. The watcher has only an ordering
dependency on `qbzd.service`; it does not pull that service in.

## Adaptive observation without healthy-state ALSA probe noise

QBZD 2.0.2 performs an ALSA device enumeration while serving `/api/status`. On
the Heim-PC this is functionally harmless but produces repeated `pcm_oss` and
`pcm_dmix` warnings. Calling that endpoint every 30 seconds while QConnect is
healthy therefore creates avoidable device probes and journal noise.

The watchdog now separates **wake-up evidence** from **restart authority**. On
startup it first captures the current `qbzd.service` journal cursor and then
performs one authoritative status reconciliation. While QConnect is healthy it
checks only bounded journal deltas every 30 seconds. Real QBZD 2.0.2 failure
logs observed for the target fault include `Lifecycle -> Reconnecting`,
`Cloud rejected session`, reconnect scheduling/exhaustion and the maximum-retry
message; any of these wakes the full status reconciliation within the same
30-second cadence as the previous implementation. Journal text never authorizes
a restart by itself. A narrow exception only attests network reachability when
a timestamped bounded journal sequence contains `WebSocket connected`,
`Authenticated with JWT` and `Cloud rejected session` in that order within
30 seconds. Freshness comes from journald's `__REALTIME_TIMESTAMP`, not from
when the watchdog happens to read the delta. The rejection event must be no more
than 90 seconds old (with only a five-second future-clock tolerance). The exact
journal event timestamp is carried into reconciliation and its freshness is
revalidated against the current wall clock at every status gate and immediately
before either QConnect cycling or daemon restart. That attestation may substitute
for QBZD's contradictory `network.online=false`; all other recovery gates remain
authoritative. Missing, malformed, expired or future-skewed event time fails
closed.

Once the status path arms a recovery candidate, the watchdog returns to the
full 30-second reconciliation cadence. The candidate remains bound to the same
boot, PID and process start tick. `retrying`/`reconnecting` cannot cause an
effect before 90 seconds; a previously armed terminal `exhausted` state may use
the narrow QConnect-only path earlier. Every QConnect effect rechecks status,
process identity and the appropriate ALSA ownership gate at the effect edge:
PCM-idle for the original closed-device path, or exact target-MOTU ownership plus
kernel-reported ALSA `PAUSED` for the paused-open path. QBZD's own playback
`state=paused` remains a diagnostic consistency check, not effect authority: it
has been observed stale while the same PCM was already kernel-visible as
`RUNNING`. QConnect recovery has its own durable pre-effect arm, bounded readback
and exponential backoff. The broader daemon restart still requires the original
five-minute stuck window and always retains the stricter PCM-idle and repeated
restart-edge gates, durable arm and post-effect cooldown.

Two fail-safe fallbacks prevent the journal optimization from becoming a new
single point of failure:

- if the journal cursor is missing, malformed, rotated, unreadable, too large or
  otherwise unavailable, the watcher first attempts a replacement journal baseline
  and still performs the old `/api/status` reconciliation in that cycle. This order
  keeps a reconnect racing with cursor recovery visible either to the status read or
  to the following journal delta. If the replacement also fails, the old 30-second
  status cadence remains active until journald is observable again;
- even with a quiet healthy journal, one authoritative status reconciliation is
  forced every five minutes. This bounds detection if a future QBZD version
  silently changes its logging contract.

The local `/api/events` and `/api/sse` surfaces are deliberately not part of this
contract because they have not been validated as a stable QConnect lifecycle
API. For the actually observed QBZD 2.0.2 reconnect/auth failure, the journal
transition is proven and keeps the previous worst-case wake-up latency while
reducing healthy watchdog status calls from 120 to at most 12 per hour, apart
from service startup and explicit diagnostics.

A QConnect-only repair is allowed only after the candidate is bound to the same
boot and QBZD process identity and survives additional effect-edge observations.
`retrying`/`reconnecting` retain the 90-second stuck window; terminal `exhausted`
still requires a prior bound observation but does not repeat that already-consumed
reconnect window. A daemon restart requires five minutes and then survives the
original stricter restart-edge observations:

- the fixed loopback status endpoint `127.0.0.1:8182/api/status` returns bounded,
  strict API-v1 JSON;
- Qobuz authentication is `logged_in` and either the network is reported online,
  or a timestamped QConnect journal sequence proves that the WebSocket connected,
  JWT authentication succeeded, and the cloud then rejected the session within
  the last 90 seconds; the age is derived from the journal event time, never the
  watchdog read time;
- QConnect is `retrying`, `reconnecting`, or terminal `exhausted` with no active
  session;
- QBZD is configured for ALSA and exactly `front:CARD=M2,DEV=0`;
- the configured MOTU device is present. A closed device follows the original
  PCM-idle path. An open device is eligible **only for the QConnect-only cycle**
  when QBZD's snapshot is strictly `paused`, track ID and position are valid, a
  second status read after the stabilization delay reports the identical track
  and non-progressing position, the final effect-edge read still matches, **and**
  the independent kernel PCM state is `PAUSED`. Any kernel `RUNNING`, missing or
  ambiguous state blocks the effect even if QBZD still reports `paused`. These
  playback fields are required only for this paused-open exception: if a QBZD
  status omits them, the exception fails closed while the original
  closed-device/PCM-idle recovery path remains available;
- `qbzd.service` is active, has one positive `MainPID`, `/proc/<pid>/comm` is
  exactly `qbzd`, and the process start tick plus systemd cgroup remain stable;
- both the 90-second QConnect threshold and the five-minute daemon threshold
  derive from the same monotonic candidate age bound to boot ID, PID and process
  start tick. A QBZD restart or host reboot therefore starts a fresh observation
  window; wall-clock jumps cannot satisfy either threshold;
- every `/proc/asound/card*/pcm*/sub*/status` entry is bounded and readable.
  For the daemon restart, an owner thread in the QBZD process or any helper in
  the same `qbzd.service` cgroup still blocks the effect exactly as before. For
  the paused-open **QConnect-only** cycle, the inverse proof is required instead:
  an open PCM must resolve to the exact QBZD TGID and exact bound service cgroup,
  every open target playback substream must report `state: PAUSED`, and the
  service PID/start tick is revalidated before and after the kernel-state scan.
  Unknown ownership, a same-cgroup helper without the exact QBZD TGID, `RUNNING`
  or another non-`PAUSED` state, or identity drift blocks the cycle;
- QConnect state, playback fingerprint, service identity, boot identity and the
  appropriate ALSA gate are repeated immediately before the effect. The
  QConnect-only path additionally opens the previously observed absolute `qbzd`
  image first, verifies that pinned file descriptor against the current
  `/proc/<pid>/exe`, PID start tick and service cgroup, and executes through
  `/proc/self/fd/<fd>`. PID reuse therefore cannot redirect the action to a
  different process image. The complete service identity is rechecked again
  between `disable` and `enable`. For the production paused-open path the kernel
  `PAUSED`/owner gate is repeated once more after the final QBZD status and
  process read, immediately before the durable effect arm.

The QConnect-only effect is durably armed **before** `qconnect disable`: exact
PID, process start tick and executable identity plus a minimum retry deadline are
fsynced first. That arm also persists a separate **re-enable obligation** before
disable. A pre-existing obligation is never cleared merely because the lifecycle
still says `retrying`, `reconnecting`, or `exhausted`; the watchdog first issues
an idempotent `qconnect enable` again. QBZD 2.0.2's explicit `qconnect.enabled`
field is preferred as the post-command control readback whenever present. If an
older or synthetic status omits that field, lifecycle fallback is accepted only
after the enable command itself returned success; it can never clear an
outcome-unknown obligation on its own. If disable or enable has an error, the
obligation survives a QBZD or host restart and blocks the broader daemon restart
until the control plane has been positively restored. This prevents a partial
disable/enable cycle from silently leaving QConnect off. A successful cycle is
accepted only when bounded readback reaches `connected` with an active session
on the same service identity. Failure receives QConnect-specific exponential
backoff from 120 seconds up to 15 minutes, so a partial or ambiguous
control-session cycle is not hammered.

The broader restart attempt remains durably armed **before** `try-restart`: the
exact process identity and a minimum 15-minute retry deadline are fsynced first.
If the `systemctl` response is lost, the service is killed after the effect, or a
post-effect state write fails, a new observer run cannot immediately repeat the
restart. A later healthy QConnect read turns that pending arm into a successful
recovery cooldown without another mutation.

After `try-restart` returns, bounded readback waits up to one minute for QConnect
to reach `connected` with an active session. The 15-minute success cooldown is
measured from the **confirmed healthy readback**, not from the beginning of the
attempt. Failed daemon-restart readback starts exponential backoff from its
completion time, from 15 minutes up to one hour. Control deadlines use the
monotonic clock; wall-clock time is retained only as diagnostic recovery time.

This watchdog can repair the proven backend failure class, but it does not treat
client UI metadata as authoritative. If QBZD already reports `connected` with an
active session while a Qobuz client independently displays the wrong track, this
watchdog deliberately does nothing: that would require a separate, evidence-bound
client/playback-identity observer rather than guessing from UI state.

State JSON rejects non-finite numbers, negative control timestamps, incomplete
process bindings and stale state from another boot. Malformed state, unreadable
status, logged-out state, offline state without the short-lived QConnect network
attestation above, active/buffering/unknown playback, a changed track or advancing
position during a paused-open proof, kernel `RUNNING` or another non-`PAUSED`
open target PCM state, service-identity ambiguity, unreadable ALSA ownership, or
any changed observation fails closed.

There is no atomic exclusion primitive shared with arbitrary non-cooperating
ALSA/QBZD activity. Playback could theoretically begin in the very small
sub-call interval after the final status/kernel-PCM observation and before either
the QConnect-only control call or `try-restart`. Repeated status/playback,
process, TGID/cgroup and kernel PCM gates minimize that interval but do not claim
to eliminate it. The paused-open exception therefore never authorizes a daemon
restart, and `try-restart` additionally prevents a service that becomes inactive
in that interval from being resurrected.

The versioned state is stored at `${STATE_DIRECTORY}/state.json` with a
systemd-managed `StateDirectory` and mode `0700`; the file itself is written
atomically with mode `0600`. The contract intentionally uses systemd's exported
`STATE_DIRECTORY` instead of assuming a filesystem location. On the Heim-PC's
systemd 249 user manager this directory is below the user configuration root;
other systemd versions may resolve the managed state root differently without
changing the service contract.

Schema v3 adds QConnect-specific durable effect state that the previous schema-v2
watchdog cannot parse. A deployment rollback therefore stops the candidate
watchdog first and runs that candidate release's `prepare-rollback` helper in a
short-lived systemd user service with the same `StateDirectory=` contract. The
helper resolves any pending QConnect re-enable obligation before atomically
projecting the validated state onto the exact schema-v2 field set. If re-enable
or its readback cannot be proven, or the state is invalid, rollback fails closed
before the previous watchdog is restarted. No filesystem root is guessed by the
deployer; systemd exports the effective `STATE_DIRECTORY` to the helper.

The service is pulled in by `audio-control-ui-v1.service`, ordered after
`qbzd.service`, and `PartOf` the Audiozentrale lifecycle so revision-bound deploy
or rollback cannot leave candidate recovery code running across a release
pointer change. QBZD itself remains an independently enabled user service.

## Audiozentrale-Readback

Die Audiozentrale liest den Zustand dieses Recovery-Wächters zusammen mit dem
zweiten Qobuz-Recovery-Dienst in einer einzigen begrenzten, read-only
`systemctl --user show`-Abfrage zurück. Ein verwaltetes Deployment wird als
`attention` markiert, wenn einer der Wächter inaktiv oder nicht eindeutig
lesbar ist. Ein aktiver Wächter belegt ausschließlich seine eigene
Prozessbereitschaft; daraus folgt weder erfolgreiche Qobuz-Wiedergabe noch
track-native/bitperfekte Ausgabe.
