#!/command/with-contenv sh
# shellcheck shell=sh
# Hermes policy preflight entry. Delivered by service_hermes.
#
# The container's ENTRYPOINT is ["/init", "<this file>"]: /init stays PID 1 (s6 supervision, the
# cont-init hooks, the uid remap), and this script is s6's "main program" in place of
# /opt/hermes/docker/main-wrapper.sh. It runs the preflight and only then execs main-wrapper.sh
# with the original CMD, so the gateway NEVER starts on an unverified managed scope. A refusal
# exits non-zero, /init takes the container down, and the textfile metric says why:
#
#   hermes_preflight_ok 0|1                     a preflight alert reads this
#   hermes_preflight_last_run_timestamp_seconds  so a stale 1 is distinguishable from a fresh one
#
# The check itself runs as the unprivileged `hermes` user, because that is the reader that matters:
# a root-only 0600 managed file passes for root and is unreadable to the gateway. The status write
# happens as root (the textfile directory is root-owned on the guest).
set -u

POLICY=/opt/hermes-policy
MANAGED="${HERMES_MANAGED_DIR:-/etc/hermes-managed}"
TEXTFILE=/run/hermes-textfile

# THE GRAMMAR IS THE CONSUMER'S. The image has no PyYAML (a PyYAML import refuses at `parses` with
# ModuleNotFoundError); Hermes reads YAML through its own
# hermes_yaml module in $ROOT and .env files with python-dotenv, so --consumer-root makes the
# preflight import exactly those, and FAIL `parser` if it cannot, rather than fall back to another.
# -I (isolated): no PYTHON* variables and no user site. The hermes user's site directory lives under
# /opt/data, which the agent can write, so without -I a planted `dotenv` there could answer for
# the real one. -B: /opt/hermes is root-owned, so do not try to write bytecode into it. Neither
# flag stops the venv's own pyvenv.cfg and .pth files from running, so the preflight's `venv` check
# refuses unless those (and the interpreter and site-packages) are root-owned, not agent-writable.
ROOT=/opt/hermes
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || echo "[preflight] FAIL interpreter: $PY is missing or not executable"

out="$(s6-setuidgid hermes "$PY" -I -B "$POLICY/policy_preflight.py" \
    --consumer-root "$ROOT" \
    --managed-dir "$MANAGED" \
    --expect-dir "${HERMES_PREFLIGHT_PINNED_DIR:-/etc/hermes-managed}" \
    --sha-file "$POLICY/managed.sha256" \
    --owned "$POLICY/owned-keys.json" \
    --expect-uid 0 \
    --dotenv "${HERMES_HOME:-/opt/data}/.env" \
    --dotenv "${HERMES_HOME:-/opt/data}/profiles/*/.env" 2>&1)"
rc=$?
printf '%s\n' "$out" | sed 's/^/[preflight] /'

ok=0
[ "$rc" -eq 0 ] && ok=1
if [ -d "$TEXTFILE" ]; then
    tmp="$TEXTFILE/.hermes_preflight.prom.$$"
    {
        echo "# HELP hermes_preflight_ok 1 when the managed-scope preflight passed at the last container start."
        echo "# TYPE hermes_preflight_ok gauge"
        echo "hermes_preflight_ok $ok"
        echo "# HELP hermes_preflight_last_run_timestamp_seconds When the preflight last ran."
        echo "# TYPE hermes_preflight_last_run_timestamp_seconds gauge"
        echo "hermes_preflight_last_run_timestamp_seconds $(date +%s)"
    } > "$tmp" && mv -f "$tmp" "$TEXTFILE/hermes_preflight.prom"
else
    echo "[preflight] WARNING: $TEXTFILE is not mounted; the result is not exported"
fi

if [ "$rc" -ne 0 ]; then
    echo "[preflight] REFUSED: the gateway will not start (see the FAIL lines above)" >&2
    exit 1
fi
exec /opt/hermes/docker/main-wrapper.sh "$@"
