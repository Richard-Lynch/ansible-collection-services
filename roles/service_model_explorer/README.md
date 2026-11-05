# service_model_explorer

Runs the **model discovery app**, a read-only UI listing every model a LiteLLM
gateway can reach, labelled by _how_ it is reachable. It is usually deployed on
the same host as the gateway. The app is its own project,
[Richard-Lynch/model-explorer](https://github.com/Richard-Lynch/model-explorer),
released as the container image `ghcr.io/richard-lynch/model-explorer`. This
role deploys that image with Docker Compose, **by digest**, and mounts the
caller's catalogue snapshot read-only. It builds nothing, and no node toolchain
is needed anywhere.

## The problem the app exists to solve

A LiteLLM gateway can **route** far more models than it can **enumerate**.
`openrouter/*` routes every OpenRouter model and enumerates none of them, so a
model can work perfectly and still be invisible to Open WebUI's picker. The app
shows both populations in one list and labels every row. Its README covers the
app itself: the snapshot format, staleness, failure states and the write path.

## Inputs

See [`meta/argument_specs.yml`](meta/argument_specs.yml). Two options are
required, and neither has a default:

| option                         | what                                                                                                                                                   |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `model_explorer_image`         | the image as `name@sha256:<64 hex>`. A **digest**, never a tag. Each model-explorer release records its digest in its notes and in `image-digest.txt`. |
| `model_explorer_catalogue_src` | path **on the controller** of the catalogue snapshot (`catalogue.json`)                                                                                |

Everything else has a generic default: container name, config directory, Docker
network, host port (mapped to the image's 8080) and timezone.

Which release a host runs is the caller's decision, so the digest belongs in
inventory and not in this role. The role refuses a tag, a bare name or a
malformed digest before it touches the host.

## Usage

```yaml
- name: Deploy the model discovery app
  hosts: gateway
  tasks:
    - name: Serve the model explorer
      ansible.builtin.include_role:
        name: richard_lynch.services.service_model_explorer
      vars:
        model_explorer_image: "ghcr.io/richard-lynch/model-explorer@sha256:<digest>"
        model_explorer_catalogue_src: "{{ inventory_dir }}/files/catalogue.json"
        model_explorer_network: gateway-net
```

## What it does

1. **Gates**: the image is a digest reference, and the snapshot exists and is
   non-empty.
2. **Renders** the compose project. The container gets a read-only root
   filesystem with a tmpfs `/tmp`, and the catalogue **directory** is mounted
   read-only at `/srv/catalogue`. A directory, not the file: a file-level bind
   mount pins the inode, and the snapshot is replaced by an atomic rename, so
   the container would keep serving the old one.
3. **Ships** the snapshot. A refreshed snapshot is served on the next request,
   with no restart and no new image.
4. **Converges** with `recreate: auto`, so a new digest recreates the container.
5. **Reads back**:
   - the container is running, its **own HEALTHCHECK** reports `healthy`, and it
     runs the pinned digest;
   - the served page is _the app_. A 200 is not enough, because nginx's welcome
     page is a 200 too;
   - the served catalogue parses and carries models.
6. **Cleans up, last**: only after every read-back has passed, it removes the
   `site/` and `nginx.conf` that versions before 2.0.0 copied in. Until then the
   previous container may still be serving from them.

## This service holds NO credential

- The role declares **no credential options at all**.
- Nothing in the compose project or the image carries a key, and molecule
  **asserts** that (`NO CREDENTIAL may reach the rendered compose project`).
- The only credential near this feature is the gateway master key. Whatever
  produces the snapshot uses it, and it is never written into the snapshot.

## Observability notes

- nginx logs every request to the container's stdout, so any container log
  shipper picks it up.
- **Do not scrape `/metrics`.** The SPA fallback serves `index.html` for every
  unknown path, so a scrape job would sit permanently `up` while scraping a web
  page.

## What this role does NOT do

- **It does not build the app.** The model-explorer repository's CI builds and
  releases the image.
- **It does not talk to the gateway** or produce the snapshot.
- **It does not prove a model works.** A catalogue entry is not a working route.

## Testing

```bash
cd roles/service_model_explorer && molecule test
```

There is no Docker in the scenario, so coverage is **partial**. It renders the
compose template for two different callers and asserts on the result:

- the digest reference;
- the read-only directory mount and root filesystem;
- the ports;
- no credential;
- no hardcoded value.

It also runs the role's `_compose` entrypoint for real, through
`richard_lynch.common.deployment_config`, against a scratch directory, and
requires the rendered `docker-compose.yml` to be there with the caller's digest.
That is the step that decides what compose runs; in 2.0.0 its source path
resolved inside the wrong role, rendered nothing, and left the old container in
place.

It then includes the **real role** twice and requires both calls to be refused
for the right reason: once with no image reference (argspec), and once with a
tag (the digest gate). The live behaviour is asserted by the role's own
read-backs: health, digest, the served page and the catalogue.
