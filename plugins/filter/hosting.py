# SPDX-License-Identifier: GPL-3.0-or-later
"""Filters for service_hosting: the compose gate and the compose builder.

Why this is Python and not a Jinja expression: the gate is the security boundary of the DEV tier
(definitions deploy there without review), so it must be unit-testable with fixtures that MUST be
refused and near-misses that MUST pass. A Jinja chain over a nested compose dict cannot be tested
that way, and a gate nobody has seen refuse anything may not refuse anything.

The main filters:

  hosting_violations(app, tier_name, tiers, image_allowlist) -> list[str]
      Every reason this app definition must be refused. EMPTY means acceptable. All checks run
      (no early return) so one refusal lists everything wrong with the definition.

  hosting_resolved_violations(resolved, app_root, limits) -> list[str]
      The same judgement applied to what `docker compose config --format json` RESOLVED the written
      file to, because compose interpolates ${VAR:-default} before it mounts anything.

  hosting_secret_files(app, tier_name, tiers) -> list[dict]
      The platform secrets a PROD app declares (`secrets:` beside `compose:`, never inside it),
      each as {key, item, field, service, uid, path}. The deploy resolves `item`/`field` from the
      secret store on the controller and writes the value to `path` (0400, owned by `uid`); the
      compose gets a top-level `secrets:` entry naming exactly that file. The app cannot name a
      host path: `path` is derived from the tier root, the app name and the key.

  hosting_compose(app, tier_name, tiers, defaults) -> dict
      The compose project that is actually written: the app's own services with Traefik labels,
      the tier network, resource limits and no-new-privileges injected by THIS code, never taken
      from the app. Raises ValueError if the app is not acceptable, so building a compose from a
      refused definition is not possible even if a caller forgets to check.
"""

from __future__ import annotations

import ipaddress
import posixpath
import re

try:  # pragma: no cover - exercised under ansible, not under bare python
    from ansible.errors import AnsibleFilterError
except Exception:  # pragma: no cover

    class AnsibleFilterError(Exception):  # type: ignore[no-redef]
        pass


NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,28}[a-z0-9]$")
# Names that would shadow the platform's own containers or read as the platform itself.
RESERVED_NAMES = {"traefik", "socket-proxy", "docker", "hosting", "dev", "apps", "www", "mail"}
MAX_TTL_HOURS = 720  # 30 days: a DEV app is a demo, not a deployment.
FLOATING_TAGS = {"latest", "stable", "main", "master", "edge", "canary", "nightly", "rolling"}

# THE GATE IS AN ALLOWLIST. A service key that is not in ALLOWED_SERVICE_KEYS is refused. An
# allowlist rejects every compose feature nobody thought of, which is exactly the property a
# security boundary needs: Compose 2.36 added `use_api_socket` (hands the container the Docker
# socket, i.e. root on the host), and a denylist written before it lets it straight through. A new
# compose feature is refused until someone reads it and adds it here. Keys starting `x-` are compose extension fields (inert).
ALLOWED_SERVICE_KEYS = frozenset(
    {
        "image",
        "command",
        "entrypoint",
        "environment",
        "labels",
        "volumes",
        "networks",
        "expose",
        "healthcheck",
        "depends_on",
        "restart",
        "user",
        "working_dir",
        "tmpfs",
        "cap_drop",
        "read_only",
        "mem_limit",
        "cpus",
        "deploy",
        "stop_signal",
        "stop_grace_period",
        "init",
        "hostname",
        "domainname",
        "logging",
        "shm_size",
        "ulimits",
        "stdin_open",
        "tty",
        "platform",
    }
)
# What `docker compose config` adds to the RESOLVED model on top of the above: the platform's own
# injected security_opt (checked for its exact value below) and normalisation keys.
RESOLVED_EXTRA_KEYS = frozenset({"security_opt", "secrets"})

# The reasons for the keys most worth naming. Every one is ALSO outside the allowlist, so this
# dict only improves the message; removing an entry here does not let its key through.
FORBIDDEN_SERVICE_KEYS = {
    "use_api_socket": "mounts the Docker API socket into the container (root on the guest)",
    "provider": "delegates the service to an external provider plugin",
    "label_file": "reads labels from the host",
    "privileged": "runs with full host privileges",
    "network_mode": "bypasses the tier network (host/container networking)",
    "pid": "shares a PID namespace",
    "ipc": "shares an IPC namespace",
    "uts": "shares the UTS namespace",
    "userns_mode": "changes the user namespace mapping",
    "cgroup_parent": "escapes the guest's cgroup accounting",
    "cgroup": "changes the cgroup namespace",
    "cap_add": "adds Linux capabilities",
    "devices": "maps host devices into the container",
    "device_cgroup_rules": "grants device access",
    "security_opt": "overrides the security profile (injected by the platform)",
    "sysctls": "changes kernel parameters",
    "runtime": "selects a different container runtime",
    "ports": "publishes a host port (Traefik is the only way in)",
    "build": "builds from a local context (images must be pre-built and allowlisted)",
    "env_file": "reads a file from the host",
    "container_name": "fixes a container name (the platform names containers)",
    "extends": "pulls in another compose file",
    "external_links": "reaches containers outside the app",
    "group_add": "adds supplementary groups",
    "pull_policy": "overrides image pull behaviour",
    "volumes_from": "mounts another container's volumes",
    "labels_file": "reads labels from the host",
    "oom_kill_disable": "lets one app exhaust the guest's memory",
    "oom_score_adj": "changes the app's standing with the OOM killer",
    "gpus": "maps a GPU into the container",
    "memswap_limit": "overrides the platform's memory/swap accounting",
    "mem_reservation": "overrides the platform's memory accounting",
}
FORBIDDEN_TOP_KEYS = {
    "include": "pulls in other compose files",
    "configs": "configs can read host files",
    "secrets": "secrets can read host files",
    "name": "the project name is set by the platform",
    "models": "not supported",
}
RESERVED_LABEL_PREFIXES = ("traefik.", "hosting.", "com.docker.compose.")

# PLATFORM SECRETS. An app on a tier declared `secrets: true` (normally PROD only) may name
# secret-store items the platform delivers as files. The app's compose still cannot
# carry a `secrets:` key of its own (FORBIDDEN_TOP_KEYS): the platform writes that block, pointing
# only at files it wrote. Refused on any other tier: a DEV definition deploys without review, so
# letting it name a vault item would let its author read any credential.
SECRET_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
SECRET_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
SECRET_ITEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,127}$")
SECRET_KEYS = {"item", "field", "service", "uid"}


_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def _bytes(value):
    """Parse a docker memory value ('512m', '1g', 536870912) to bytes; None if unreadable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([bkmgt]?)b?\s*$", str(value).lower())
    if not m:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2)])


def _limit_problems(sname, svc, limits):
    """A cap the platform will not let an app raise. Without it the 'injected' limit is only a
    default: mem_limit: 64g passes and one app starves a small host."""
    out = []
    max_mem = _bytes((limits or {}).get("max_mem", "1g"))
    max_cpus = float((limits or {}).get("max_cpus", 2.0))
    res = (svc.get("deploy") or {}).get("resources") or {}
    res_limits = res.get("limits") or {}
    if res.get("reservations"):
        out.append(
            "service %r: deploy.resources.reservations is refused (devices/gpus/reserved memory)"
            % sname
        )
    for label, raw in (
        ("mem_limit", svc.get("mem_limit")),
        ("deploy.resources.limits.memory", res_limits.get("memory")),
    ):
        if raw is None:
            continue
        got = _bytes(raw)
        if got is None or got <= 0 or (max_mem is not None and got > max_mem):
            out.append(
                "service %r: %s %r is unreadable or above the platform maximum of %s"
                % (sname, label, raw, (limits or {}).get("max_mem", "1g"))
            )
    for label, raw in (
        ("cpus", svc.get("cpus")),
        ("deploy.resources.limits.cpus", res_limits.get("cpus")),
    ):
        if raw is None:
            continue
        try:
            got = float(raw)
        except (TypeError, ValueError):
            got = -1.0
        if got <= 0 or got > max_cpus:
            out.append(
                "service %r: %s %r is unreadable or above the platform maximum of %s"
                % (sname, label, raw, max_cpus)
            )
    return out


def _key_problems(sname, svc, allowed, verb):
    """Every service key outside `allowed` (x- extension fields excepted), with a reason if known."""
    out = []
    for key in svc:
        k = str(key)
        if k in allowed or k.startswith("x-"):
            continue
        why = FORBIDDEN_SERVICE_KEYS.get(k, "is not on the platform's allowlist of service keys")
        out.append("service %r: %r %s: %s" % (sname, k, verb, why))
    return out


def _is_app(app):
    return isinstance(app, dict)


def _labels_as_dict(labels):
    if isinstance(labels, dict):
        return {str(k): v for k, v in labels.items()}
    out = {}
    for item in labels or []:
        s = str(item)
        k, dummy, v = s.partition("=")
        out[k] = v
    return out


def _volume_problem(vol, app_root):
    """Return a string describing why a service volume entry is refused, or None."""
    if isinstance(vol, dict):
        vtype = vol.get("type", "volume")
        source = str(vol.get("source", ""))
        target = str(vol.get("target", ""))
        if "$" in source or "$" in target:
            return "contains '$': compose interpolates it before mounting"
        if vtype == "tmpfs":
            return None
        if vtype == "bind":
            return _bind_problem(source, target, app_root)
        if vtype == "volume":
            if "docker.sock" in source or "docker.sock" in target:
                return "mounts the Docker socket"
            return _named_volume_problem(source)
        return "volume type %r is not allowed" % vtype
    text = str(vol)
    if "$" in text:
        return "contains '$': compose interpolates it before mounting, so the path is not what it reads as"
    parts = text.split(":")
    source = parts[0]
    target = parts[1] if len(parts) > 1 else ""
    if "docker.sock" in text:
        return "mounts the Docker socket"
    if source == "":
        return None  # anonymous volume
    if source.startswith(("/", ".", "~", "$")):
        return _bind_problem(source, target, app_root)
    return _named_volume_problem(source)


def _named_volume_problem(source):
    if "docker.sock" in source:
        return "mounts the Docker socket"
    return None


def _bind_problem(source, target, app_root):
    """Every host bind is refused. A bind under data/ looks safe, but data/ is
    writable by the app: it can write a symlink there (data/x -> /) and a LATER definition, or the
    container's own restart, mounts the symlink's target. State belongs in a named volume or tmpfs,
    which Docker places under its own root, so no host path is ever an app's to name."""
    if "docker.sock" in source or "docker.sock" in target:
        return "mounts the Docker socket"
    return (
        "is a host bind mount (%r): host paths are refused, use a named volume or tmpfs "
        "(the app's own %s/data is writable by the app and could hold a symlink)"
        % (source, posixpath.join(app_root, "data"))
    )


def _allowed(image, entry):
    """An entry ending in ':' names a REPOSITORY (any tag or digest of it); anything else is a plain
    prefix. So 'nginx:' allows nginx:1.27 and nginx@sha256:... but not nginxevil/x:1."""
    if entry.endswith(":"):
        repo = entry[:-1]
        return image.startswith((repo + ":", repo + "@"))
    return image.startswith(entry)


def _image_problem(image, allowlist):
    if not isinstance(image, str) or not image:
        return "has no image"
    if "$" in image:
        return "image %r contains '$' (compose would interpolate it)" % image
    if not any(_allowed(image, p) for p in allowlist):
        return "image %r is not on the allowlist" % image
    if "@sha256:" in image:
        return None
    last = image.rsplit("/", 1)[-1]
    if ":" not in last:
        return "image %r has no pinned tag (an untagged image is :latest)" % image
    tag = last.split(":", 1)[1]
    if tag.lower() in FLOATING_TAGS:
        return "image %r uses the floating tag %r" % (image, tag)
    return None


def _secret_problems(app, tier_name, tier):
    secrets = app.get("secrets")
    if secrets is None:
        return []
    if not tier.get("secrets"):
        return [
            "secrets are refused on tier %r: only a tier declared `secrets: true` (PROD, where the "
            "definition is reviewed and merged) may name a secret-store item" % tier_name
        ]
    if not isinstance(secrets, dict) or not secrets:
        return ["secrets must be a non-empty mapping of key -> {item, field, service}"]
    out = []
    services = (app.get("compose") or {}).get("services") or {}
    for key, spec in secrets.items():
        if not isinstance(key, str) or not SECRET_KEY_RE.match(key):
            out.append("secret %r: the key must be 2-40 chars of a-z, 0-9 and '_'" % (key,))
            continue
        if not isinstance(spec, dict):
            out.append("secret %r is not a mapping" % key)
            continue
        extra = sorted(set(map(str, spec)) - SECRET_KEYS)
        if extra:
            out.append("secret %r: unknown keys %s" % (key, extra))
        item, field = spec.get("item"), spec.get("field", "password")
        if not isinstance(item, str) or not SECRET_ITEM_RE.match(item):
            out.append("secret %r: item %r is not a secret-store item name or id" % (key, item))
        if not isinstance(field, str) or not SECRET_FIELD_RE.match(field):
            out.append("secret %r: field %r is not a field name" % (key, field))
        if spec.get("service") not in services:
            out.append(
                "secret %r: service %r is not in compose.services %s"
                % (key, spec.get("service"), sorted(services))
            )
        uid = spec.get("uid", 0)
        if not isinstance(uid, int) or isinstance(uid, bool) or not 0 <= uid <= 65535:
            out.append("secret %r: uid %r must be an integer 0-65535" % (key, uid))
    return out


def hosting_secret_files(app, tier_name, tiers):
    """The platform-delivered secret files for an ACCEPTED app (empty list when it declares none)."""
    tier = (tiers or {})[tier_name]
    root = posixpath.join(tier.get("root", "/srv/apps/%s" % tier_name), app["name"], "secrets")
    return [
        {
            "key": key,
            "item": spec["item"],
            "field": spec.get("field", "password"),
            "service": spec["service"],
            "uid": spec.get("uid", 0),
            "path": posixpath.join(root, key),
        }
        for key, spec in sorted((app.get("secrets") or {}).items())
    ]


def hosting_violations(app, tier_name, tiers, image_allowlist, limits=None):
    """Every reason `app` must be refused on tier `tier_name`; empty list means acceptable."""
    v = []
    if not _is_app(app):
        return ["the app definition is not a mapping"]
    tiers = tiers or {}
    if tier_name not in tiers:
        return ["tier %r is not declared (known: %s)" % (tier_name, sorted(tiers))]
    tier = tiers[tier_name]
    name = app.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        v.append(
            "name %r must be 3-30 chars of a-z, 0-9 and '-', start with a letter and not end with '-'"
            % (name,)
        )
        name = name if isinstance(name, str) else ""
    elif name in RESERVED_NAMES:
        v.append("name %r is reserved by the platform" % name)
    if app.get("tier") != tier_name:
        v.append(
            "the definition says tier %r but is being deployed as %r" % (app.get("tier"), tier_name)
        )

    port = app.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        v.append(
            "port %r must be an integer 1-65535 (the container port Traefik targets)" % (port,)
        )

    # TTL (the reaper). Only a tier declared `reap: true` removes apps by TTL, so a
    # TTL on any other tier would read as a promise nothing keeps: refused rather than ignored.
    ttl = app.get("ttl_hours", 0)
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not 0 <= ttl <= MAX_TTL_HOURS:
        v.append("ttl_hours %r must be an integer 0-%d (0 = never expires)" % (ttl, MAX_TTL_HOURS))
    elif ttl > 0 and not tier.get("reap"):
        v.append(
            "ttl_hours %d on tier %r, which is not reaped: apps there never expire, so a TTL would"
            " be a promise nothing keeps" % (ttl, tier_name)
        )

    v.extend(_secret_problems(app, tier_name, tier))

    compose = app.get("compose")
    if not isinstance(compose, dict):
        v.append("compose must be a mapping with a services: key")
        return v
    for key, why in FORBIDDEN_TOP_KEYS.items():
        if key in compose:
            v.append("top-level %r is refused: %s" % (key, why))
    services = compose.get("services")
    if not isinstance(services, dict) or not services:
        v.append("compose.services must be a non-empty mapping")
        return v
    routed = app.get("service")
    if routed not in services:
        v.append("the routed service %r is not in compose.services %s" % (routed, sorted(services)))

    app_root = posixpath.join(tier.get("root", "/srv/apps/%s" % tier_name), name)
    for sname, svc in services.items():
        if not isinstance(svc, dict):
            v.append("service %r is not a mapping" % sname)
            continue
        v.extend(_key_problems(sname, svc, ALLOWED_SERVICE_KEYS, "is refused"))
        problem = _image_problem(svc.get("image"), image_allowlist or [])
        if problem:
            v.append("service %r: %s" % (sname, problem))
        v.extend(_limit_problems(sname, svc, limits))
        for label in _labels_as_dict(svc.get("labels")):
            if label.startswith(RESERVED_LABEL_PREFIXES):
                v.append(
                    "service %r: label %r is refused: routing labels are written by the platform "
                    "from name/port, so an app cannot claim another hostname or entrypoint"
                    % (sname, label)
                )
        for vol in svc.get("volumes", []) or []:
            problem = _volume_problem(vol, app_root)
            if problem:
                v.append("service %r: volume %r %s" % (sname, vol, problem))
        nets = svc.get("networks")
        if isinstance(nets, dict):
            nets = list(nets)
        for n in nets or []:
            if n in (tier.get("network"),) or str(n).startswith("hosting_"):
                v.append("service %r: network %r is a platform network" % (sname, n))

    for vname, vol in (compose.get("volumes") or {}).items():
        if isinstance(vol, dict):
            for key in ("driver_opts", "external", "name", "driver"):
                if key in vol:
                    v.append(
                        "volume %r: %r is refused (a local-driver bind or an external volume "
                        "reaches the host)" % (vname, key)
                    )
    for nname, net in (compose.get("networks") or {}).items():
        if isinstance(net, dict):
            for key in ("external", "name", "driver_opts"):
                if key in net:
                    v.append("network %r: %r is refused" % (nname, key))
            if net.get("driver", "bridge") != "bridge":
                v.append("network %r: driver %r is refused" % (nname, net.get("driver")))
        if str(nname).startswith("hosting_"):
            v.append("network %r collides with a platform network" % nname)
    return v


def _escape_dollars(node):
    if isinstance(node, dict):
        return {_escape_dollars(k): _escape_dollars(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_escape_dollars(v) for v in node]
    if isinstance(node, str):
        return node.replace("$", "$$")
    return node


def hosting_resolved_violations(resolved, app_root, limits=None, secret_files=None):
    """Judge what `docker compose config --format json` says Docker WILL run.

    The first gate reads the YAML as written; compose then interpolates variables, so the written
    text and the resolved model can differ. This applies the host-reaching rules to the resolved
    model, which is the one thing that decides what is mounted. EMPTY means acceptable.
    """
    v = []
    if not isinstance(resolved, dict) or not isinstance(resolved.get("services"), dict):
        return ["the resolved compose model has no services (could not be read)"]
    # Platform secrets: the resolved model may carry ONLY the files the platform wrote, each given
    # to the service that declared it (a resolved secret pointing anywhere else reaches the host).
    declared = {f["key"]: f for f in (secret_files or [])}
    top = resolved.get("secrets") or {}
    for key, spec in top.items():
        want = declared.get(key)
        got = spec.get("file") if isinstance(spec, dict) else None
        if want is None or got != want["path"]:
            v.append("resolved secret %r reads %r, which the platform did not write" % (key, got))
    for sname, svc in resolved["services"].items():
        if not isinstance(svc, dict):
            v.append("service %r: resolved service is not a mapping" % sname)
            continue
        for ref in svc.get("secrets") or []:
            src = ref.get("source") if isinstance(ref, dict) else ref
            if src not in declared or declared[src]["service"] != sname:
                v.append("service %r: resolved secret %r was not declared for it" % (sname, src))
        if svc.get("security_opt") not in (None, ["no-new-privileges:true"]):
            v.append("service %r: resolved security_opt is %r" % (sname, svc.get("security_opt")))
        # compose config writes some keys with an empty value; only a SET key widens anything.
        set_keys = {k: val for k, val in svc.items() if val not in (None, [], {}, False, "")}
        v.extend(
            _key_problems(
                sname,
                set_keys,
                ALLOWED_SERVICE_KEYS | RESOLVED_EXTRA_KEYS,
                "is set in the resolved model",
            )
        )
        for vol in svc.get("volumes") or []:
            if not isinstance(vol, dict):
                v.append("service %r: resolved volume %r is not in long form" % (sname, vol))
                continue
            vtype = vol.get("type")
            source = str(vol.get("source", ""))
            if "docker.sock" in source or "docker.sock" in str(vol.get("target", "")):
                v.append("service %r: resolved volume mounts the Docker socket" % sname)
            if vtype == "bind":
                v.append(
                    "service %r: resolved volume is a host bind of %r (host binds are refused)"
                    % (sname, source)
                )
            elif vtype not in ("volume", "tmpfs"):
                v.append("service %r: resolved volume type %r" % (sname, vtype))
        v.extend(_limit_problems(sname, _resolved_limits(svc), limits))
    for vname, vol in (resolved.get("volumes") or {}).items():
        if isinstance(vol, dict) and (vol.get("driver_opts") or vol.get("external")):
            v.append("resolved volume %r has driver_opts or is external (reaches the host)" % vname)
    return v


def _resolved_limits(svc):
    """compose config reports mem_limit in bytes and cpus as a number; reuse the one checker."""
    out = {}
    if svc.get("mem_limit") is not None:
        out["mem_limit"] = svc["mem_limit"]
    if svc.get("cpus") is not None:
        out["cpus"] = svc["cpus"]
    return out


BRIDGE_MAX = 15  # IFNAMSIZ - 1: the kernel refuses a longer interface name


def hosting_app_bridge(app_name):
    """The stable linux bridge of an app that holds an egress grant: its own, and only its own."""
    return "hst-a-%s" % app_name


def hosting_egress_rules(grants, tiers):
    """The firewall's egress exceptions, each pinned to ONE container's address.

    A grant is {app, service, address, subnet, cidr, ports, tier (default prod), proto (default
    tcp)}. deploy_app puts the app's default network on a platform-named bridge (hst-a-<app>) with
    that subnet and pins `service` to `address` on it; the firewall allows the grant only from that
    bridge AND that source address. So the grant means that one container: not another app (other
    bridge), not the app's other services (other address). A rule that matched every bridge
    (docker0, br-+, hst-+) would give every container on the host the grant. Anything less than a
    full grant is refused rather than widened back to the host.
    """
    out, problems, seen = [], [], set()
    for i, g in enumerate(grants or []):
        where = "hosting_egress_allow[%d]" % i
        if not isinstance(g, dict):
            problems.append("%s is not a mapping" % where)
            continue
        missing = [
            k for k in ("app", "service", "address", "subnet", "cidr", "ports") if not g.get(k)
        ]
        if missing:
            problems.append(
                "%s lacks %s: an egress grant is for ONE pinned container; a wider allowance is "
                "refused" % (where, ", ".join(missing))
            )
            continue
        tier = g.get("tier", "prod")
        if tier not in (tiers or {}):
            problems.append("%s: tier %r is not a hosting tier" % (where, tier))
            continue
        bridge = hosting_app_bridge(g["app"])
        if len(bridge) > BRIDGE_MAX:
            problems.append(
                "%s: app %r makes bridge %r, longer than %d characters"
                % (where, g["app"], bridge, BRIDGE_MAX)
            )
            continue
        try:
            net = ipaddress.ip_network(str(g["subnet"]), strict=True)
            addr = ipaddress.ip_address(str(g["address"]))
        except ValueError as exc:
            problems.append("%s: %s" % (where, exc))
            continue
        # Docker takes the first host address as the bridge's gateway, and hands the app's OTHER
        # services addresses from ip_range; the pinned address sits in the lower half of the
        # subnet and ip_range is the upper half, so a later dynamic address can never take it.
        if net.prefixlen > 29:
            problems.append("%s: subnet %s is smaller than a /29" % (where, net))
            continue
        lower, upper = list(net.subnets(prefixlen_diff=1))
        if addr not in lower or addr in (net.network_address, net.network_address + 1):
            problems.append(
                "%s: address %s must be in the lower half of %s (%s), past the gateway"
                % (where, addr, net, lower)
            )
            continue
        if (g["app"], tier) in seen:
            problems.append("%s: a second grant for app %r; one grant per app" % (where, g["app"]))
            continue
        seen.add((g["app"], tier))
        ports = g["ports"] if isinstance(g["ports"], list) else [g["ports"]]
        for port in ports:
            out.append(
                {
                    "app": g["app"],
                    "service": g["service"],
                    "tier": tier,
                    "bridge": bridge,
                    "subnet": str(net),
                    "ip_range": str(upper),
                    "source": str(addr),
                    "cidr": g["cidr"],
                    "port": port,
                    "proto": g.get("proto", "tcp"),
                }
            )
    if problems:
        raise ValueError("; ".join(problems))
    return out


def hosting_compose(app, tier_name, tiers, defaults):
    """Build the compose project that is written to disk. Raises ValueError on a refused app."""
    allowlist = (defaults or {}).get("image_allowlist", [])
    limits = {
        "max_mem": (defaults or {}).get("max_mem", "1g"),
        "max_cpus": (defaults or {}).get("max_cpus", 2.0),
    }
    problems = hosting_violations(app, tier_name, tiers, allowlist, limits)
    if problems:
        raise ValueError("refusing to build compose: " + "; ".join(problems))
    tier = tiers[tier_name]
    name = app["name"]
    rid = "%s-%s" % (tier_name, name)
    out = {"name": "hosting-%s" % rid, "services": {}}
    for sname, svc in app["compose"]["services"].items():
        # Escape EVERY '$' in the app's own values: compose interpolates ${VAR:-default} before it
        # mounts or runs anything, so an unescaped '$' lets a value mean something the gate never
        # read. '$$' is compose's literal dollar.
        s = _escape_dollars(dict(svc))
        labels = _labels_as_dict(s.get("labels"))
        labels.update(
            {
                "hosting.managed": "true",
                "hosting.tier": tier_name,
                "hosting.app": name,
            }
        )
        s.setdefault("restart", "unless-stopped")
        s["security_opt"] = ["no-new-privileges:true"]
        if "mem_limit" not in s and "memory" not in (
            ((s.get("deploy") or {}).get("resources") or {}).get("limits") or {}
        ):
            s["mem_limit"] = (defaults or {}).get("mem_limit", "512m")
        if "cpus" not in s:
            s["cpus"] = (defaults or {}).get("cpus", 1.0)
        if sname == app["service"]:
            labels.update(
                {
                    "traefik.enable": "true",
                    "traefik.docker.network": tier["network"],
                    "traefik.http.routers.%s.rule" % rid: "Host(`%s.%s`)" % (name, tier["suffix"]),
                    "traefik.http.routers.%s.entrypoints" % rid: tier_name,
                    "traefik.http.routers.%s.service" % rid: rid,
                    "traefik.http.services.%s.loadbalancer.server.port" % rid: str(app["port"]),
                }
            )
            nets = s.get("networks")
            if isinstance(nets, dict):
                nets = dict(nets)
                nets.setdefault("default", None)
                nets[tier["network"]] = None
            else:
                nets = list(nets or ["default"])
                if "default" not in nets:
                    nets.append("default")
                nets.append(tier["network"])
            s["networks"] = nets
        s["labels"] = labels
        out["services"][sname] = s
    for key in ("volumes", "networks"):
        if app["compose"].get(key):
            out[key] = _escape_dollars(dict(app["compose"][key]))
    files = hosting_secret_files(app, tier_name, tiers)
    if files:
        out["secrets"] = {f["key"]: {"file": f["path"]} for f in files}
        for f in files:
            svc = out["services"][f["service"]]
            svc["secrets"] = sorted(set(svc.get("secrets") or []) | {f["key"]})
    out.setdefault("networks", {})
    out["networks"][tier["network"]] = {"external": True, "name": tier["network"]}
    # An app holding an egress grant gets its default network on a STABLE, app-named bridge with
    # the grant's subnet, and the granted service a PINNED address on it; the firewall's grant
    # matches that bridge and that source address. Named anew (<project>_egress) so compose creates it with the
    # bridge name rather than meeting an existing default network of a different shape. The app
    # definition cannot set name or driver_opts itself (the gate refuses both), so only the
    # platform can put a container on that bridge.
    granted = [
        r
        for r in hosting_egress_rules((defaults or {}).get("egress_grants"), tiers)
        if r["app"] == name and r["tier"] == tier_name
    ]
    if granted:
        g = granted[0]
        if g["service"] not in out["services"]:
            raise ValueError(
                "egress grant for %r names service %r, which the app does not have"
                % (name, g["service"])
            )
        default_net = dict(out["networks"].get("default") or {})
        default_net["name"] = "%s_egress" % out["name"]
        default_net["driver_opts"] = {"com.docker.network.bridge.name": g["bridge"]}
        default_net["ipam"] = {"config": [{"subnet": g["subnet"], "ip_range": g["ip_range"]}]}
        out["networks"]["default"] = default_net
        svc = out["services"][g["service"]]
        nets = svc.get("networks")
        if isinstance(nets, dict):
            nets = dict(nets)
        else:
            nets = {n: None for n in (nets or ["default"])}
        nets["default"] = {"ipv4_address": g["source"]}
        svc["networks"] = nets
    return out


def hosting_target_refusals(guest, inventory_hosts, limit="", target=""):
    """Every reason a deploy is aimed somewhere other than `guest`, or [] when it is not.

    A host limit is refused in ANY form, because every shape of it defeats a different gate and
    none of them fails on its own:
      * --limit <guest>     skips the controller-side play, i.e. the PROD merge gate, and deploys;
      * --limit localhost   runs the gate, skips the deploy, and exits green having done nothing;
      * --limit <other>     matches no play at all and exits green.
    A `target` naming another host is refused rather than ignored: ignoring it would deploy to the
    guest while the caller believes it deployed elsewhere.
    """
    out = []
    hosts = [str(h) for h in (inventory_hosts or [])]
    guest = str(guest or "")
    if not guest:
        out.append("no hosting guest was named; the deploy has no target")
    elif guest not in hosts:
        out.append(
            "%s is not in the inventory, so the deploy play's only host would match nothing and "
            "exit green having deployed nothing" % guest
        )
    lim = str(limit or "").strip()
    if lim:
        out.append(
            "a host limit (%r) was given; this play takes none. It deploys to %s only, and a limit "
            "can only skip one of its gates (to %s: the PROD merge gate; to localhost: the deploy "
            "itself; to any other host: every play) while the run still exits green"
            % (lim, guest, guest)
        )
    tgt = str(target or "").strip()
    if tgt and tgt != guest:
        out.append(
            "target=%r names a different host; this play deploys to %s and nowhere else, and does "
            "not honour target= (an app belongs on the hosting guest or not at all)" % (tgt, guest)
        )
    return out


REGISTRY_KEYS = {"host", "username", "item", "field", "vault", "probe_image"}
_REGISTRY_HOST = re.compile(r"\A[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?\Z")


def hosting_registry_problems(registries):
    """Every reason the `hosting_registries` list cannot be logged in to and proven, or [].

    The argspec checks the keys exist; this checks what it cannot: a host docker would read as
    something else (a scheme, a path), a duplicate host (the second login silently replaces the
    first), and a probe image that is not ON that registry or not pinned. The probe is the proof
    the login works, so a probe elsewhere (or a floating tag that may vanish) proves nothing.
    """
    out = []
    if not isinstance(registries, list):
        return ["hosting_registries must be a list, got %s" % type(registries).__name__]
    seen = set()
    for i, reg in enumerate(registries):
        if not isinstance(reg, dict):
            out.append("registry #%d is not a mapping" % i)
            continue
        host = str(reg.get("host") or "")
        where = "registry #%d (%s)" % (i, host or "no host")
        unknown = sorted(set(reg) - REGISTRY_KEYS)
        if unknown:
            out.append("%s has unknown key(s) %s" % (where, ", ".join(unknown)))
        for key in ("host", "username", "item", "vault", "probe_image"):
            if not isinstance(reg.get(key), str) or not reg.get(key).strip():
                out.append("%s is missing %s" % (where, key))
        if host and not _REGISTRY_HOST.match(host):
            out.append(
                "%s: host %r must be a bare lowercase registry host (no scheme, no path)"
                % (where, host)
            )
        if host and host in seen:
            out.append("%s: host %r is listed twice; one login per registry" % (where, host))
        seen.add(host)
        probe = reg.get("probe_image")
        if host and isinstance(probe, str) and probe:
            if not probe.startswith(host + "/"):
                out.append(
                    "%s: probe_image %r is not on %s, so reading it proves nothing about the login"
                    % (where, probe, host)
                )
            else:
                problem = _image_problem(probe, [host + "/"])
                if problem:
                    out.append("%s: probe_%s" % (where, problem))
    return out


class FilterModule(object):
    def filters(self):
        return {
            "hosting_violations": hosting_violations,
            "hosting_resolved_violations": hosting_resolved_violations,
            "hosting_compose": _ansible_compose,
            "hosting_secret_files": hosting_secret_files,
            "hosting_target_refusals": hosting_target_refusals,
            "hosting_registry_problems": hosting_registry_problems,
            "hosting_egress_rules": _ansible_egress_rules,
        }


def _ansible_compose(app, tier_name, tiers, defaults):
    try:
        return hosting_compose(app, tier_name, tiers, defaults)
    except ValueError as exc:
        raise AnsibleFilterError(str(exc))


def _ansible_egress_rules(grants, tiers):
    try:
        return hosting_egress_rules(grants, tiers)
    except ValueError as exc:
        raise AnsibleFilterError(str(exc))
