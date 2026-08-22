# QBZD QConnect recovery

`audio-qbzd-qconnect-recovery-v1.service` repairs one bounded failure mode of the
headless Qobuz reference path: QBZD remains alive and authenticated locally, but
QConnect stays in `retrying`/`reconnecting` and the Qobuz cloud repeatedly
rejects the session until the daemon is restarted.

The recovery is deliberately narrower than a generic watchdog. It never
restarts PipeWire, PipeWire-Pulse, WirePlumber, the host, or the Audiozentrale.
Its only deliberate mutation is `systemctl --user try-restart qbzd.service`.
`try-restart` is used so an intentionally inactive QBZD is not started by the
watchdog. The watcher has only an ordering dependency on `qbzd.service`; it does
not pull that service in.

A restart attempt is allowed only after all of these conditions remain true for
at least five minutes on the same boot and the same QBZD process identity, then
survive additional restart-edge observations:

- the fixed loopback status endpoint `127.0.0.1:8182/api/status` returns bounded,
  strict API-v1 JSON;
- Qobuz authentication is `logged_in` and the network is reported online;
- QConnect is `retrying` or `reconnecting` with no active session;
- QBZD is configured for ALSA and exactly `front:CARD=M2,DEV=0`;
- the configured MOTU device is present and QBZD reports its device closed;
- `qbzd.service` is active, has one positive `MainPID`, `/proc/<pid>/comm` is
  exactly `qbzd`, and the process start tick plus systemd cgroup remain stable;
- the five-minute timer is based on a monotonic clock and is bound to the boot
  ID, PID and process start tick. A QBZD restart or host reboot therefore starts
  a fresh observation window; wall-clock jumps cannot satisfy it;
- every `/proc/asound/card*/pcm*/sub*/status` entry is bounded and readable.
  For each open PCM, the kernel-reported owner task is resolved through
  `/proc/<tid>/status` to its TGID and through `/proc/<tid>/cgroup` to its
  service cgroup. An owner thread in the QBZD process, or any helper in the
  same `qbzd.service` cgroup, blocks recovery even if QBZD's own
  `device_open` field is stale;
- QConnect state, service identity, boot identity and the ALSA-owner gate are
  repeated at the restart edge immediately before the effect.

The restart attempt is durably armed **before** `try-restart`: the exact process
identity and a minimum 15-minute retry deadline are fsynced first. If the
`systemctl` response is lost, the service is killed after the effect, or a
post-effect state write fails, a new observer run cannot immediately repeat the
restart. A later healthy QConnect read turns that pending arm into a successful
recovery cooldown without another mutation.

After `try-restart` returns, bounded readback waits up to one minute for QConnect
to reach `connected` with an active session. The 15-minute success cooldown is
measured from the **confirmed healthy readback**, not from the beginning of the
attempt. Failed readback starts exponential backoff from its completion time,
from 15 minutes up to one hour. Control deadlines use the monotonic clock;
wall-clock time is retained only as diagnostic recovery time.

State JSON rejects non-finite numbers, negative control timestamps, incomplete
process bindings and stale state from another boot. Malformed state, unreadable
status, offline/logged-out state, active-device state, service-identity
ambiguity, unreadable ALSA ownership, or any changed observation fails closed.

There is no atomic exclusion primitive shared with arbitrary non-cooperating
ALSA/QBZD activity. Playback could theoretically begin in the very small
sub-call interval after the final status/PCM observation and before
`try-restart`. The repeated status, process, TGID/cgroup and PCM gates minimize
that interval but do not claim to eliminate it. `try-restart` additionally
prevents a service that becomes inactive in that interval from being resurrected.

The versioned state is stored at `${STATE_DIRECTORY}/state.json` with a
systemd-managed `StateDirectory` and mode `0700`; the file itself is written
atomically with mode `0600`. The contract intentionally uses systemd's exported
`STATE_DIRECTORY` instead of assuming a filesystem location. On the Heim-PC's
systemd 249 user manager this directory is below the user configuration root;
other systemd versions may resolve the managed state root differently without
changing the service contract.

The service is pulled in by `audio-control-ui-v1.service`, ordered after
`qbzd.service`, and `PartOf` the Audiozentrale lifecycle so revision-bound deploy
or rollback cannot leave candidate recovery code running across a release
pointer change. QBZD itself remains an independently enabled user service.
