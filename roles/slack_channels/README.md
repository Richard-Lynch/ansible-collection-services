# slack_channels

Reconciles a declared list of Slack **public** channels against a workspace, acting as a bot:
create a channel, or rename it from its current name; join the bot; invite the declared members;
set purpose and topic. Report-only unless `slack_channels_apply: true`.

**Why `slack_*` and not `config_*`.** The role is bound to one vendor's API (`<vendor>_*`). It
configures Slack, not a cross-cutting concern on hosts, and its only caller kind is "a play that
owns a Slack workspace". Sending
messages and uploads is a separate role; this one never posts.

## What it does, in order

One call of the collection's `slack_channels` module per run:

1. `auth.test`: the bot's user id, and its **granted** scopes from the `x-oauth-scopes` response header.
2. `conversations.list` (paged, archived channels included) and `conversations.members` for every
   declared channel and every `rename_from`.
3. Plan, per declared channel: `create`, or `join` the old channel (if needed) then `rename`; `join`;
   `invite` the missing members (never the bot itself); `purpose`; `topic`.
4. Report: the scopes, the scopes the plan needs and which are missing, each channel's live state
   (id, membership, purpose, topic, members nobody declared), and the plan.
5. Applied only: the plan is executed in that order. A failed step skips the rest for the same
   channel. Then the workspace is re-read from scratch (`conversations.list`, `.members`, and
   `conversations.info` per channel) and re-planned. The role fails unless every step succeeded
   **and** the re-plan is empty, so a call that answered `ok` and changed nothing still fails.

Slack answers HTTP 200 with `{"ok": false, "error": ...}` for nearly every failure. Every call is
judged on `ok`, and the exact `error` (plus `needed`/`provided` on `missing_scope`) is reported.

## Guides, topics and bookmarks

- `topic`: one line, set with `conversations.setTopic`; compared after Slack's `&`/`<`/`>`
  escaping, so a topic containing `<repo>` is not re-set on every run.
- `guide`: Slack mrkdwn, posted ONCE by the bot as ":pushpin: *How to use #name*" plus a trailing
  context element `<slack_channels_guide_marker_prefix><name>` (default `guide:<name>`), and
  pinned. Every run finds the bot's message whose text ENDS with that marker (so `guide:ops` never
  matches `ops-config`'s), compares its text,
  and on drift edits it in place with `chat.update`: never a repost. An unpinned guide is re-pinned;
  a duplicate is deleted (newest kept). Dropping `guide:` from the declaration unpins and deletes it.
- `bookmarks`: `[{title, link}]`, matched by title: added, or re-pointed with `bookmarks.edit`. A
  bookmark nobody declared is reported (`undeclared_bookmarks`), never removed. They need
  `bookmarks:read`/`bookmarks:write`: the owner's user token is used for them when it holds both
  and the bot does not (`bookmarks_token` in the report); with neither, the apply is refused.
- `prompt`: not Slack's, and ignored by the module. It sits beside the guide so a caller can hand
  it to whatever else reads the declaration (an agent's per-channel instruction, for example).

Applied, a create or a join is followed by a second round in the same run, so a channel's guide
and bookmarks land on the first apply rather than the next one.

## What it refuses, and what it never does

- Refuses (and in apply mode, acts on **nothing**): a declared `id` the name no longer resolves
  to (or that would have to be created); a target that is archived; a target and its
  `rename_from` that both exist (two channels, not a rename); a name written with `#`; an unknown
  key; a member that is not a user id; a name used twice.
- Never archives, unarchives, removes a member, or touches an undeclared channel. Members nobody
  declared are listed in `undeclared_members`, not removed.
- An omitted `purpose` or `topic` is left as the owner wrote it.

## Options

| option | default | meaning |
|---|---|---|
| `slack_channels_token` | required, `no_log` | bot token (xoxb-); resolve it from a secret store in the play |
| `slack_channels` | required | the declared channels (below) |
| `slack_channels_apply` | `false` | act on the plan |
| `slack_channels_admin_token` | `""`, `no_log` | the workspace owner's USER token (xoxp-), used ONLY for renames |
| `slack_channels_api_url` | `https://slack.com/api` | only a test changes it |

```yaml
slack_channels:
  - name: ops                 # no '#'
    rename_from: operations   # optional: the current name; a rename keeps the id and history
    purpose: "Main conversation"   # optional; omitted = left alone
    topic: "..."              # optional; omitted = left alone
    members: [U0123456789]    # user ids to invite; the bot always joins
    id: C0123456789           # optional: once known. Refused unless the name resolves to it, and
                              # never created again, so config written against the id stays true
```

Declare `id` once a channel exists and derive every other reference from the declaration (for
example a name-to-id map built with `items2dict` over it), so an id is written in exactly one
place.

## Scopes (bot token)

| step | Slack method | scope |
|---|---|---|
| read | `conversations.list`, `.members`, `.info` | `channels:read` |
| guide read | `pins.list`, `conversations.history` | `pins:read`, `channels:history` |
| guide post, update, delete; pin | `chat.postMessage`, `chat.update`, `chat.delete`; `pins.add`, `pins.remove` | `chat:write`; `pins:write` |
| bookmarks (owner token if the bot lacks them) | `bookmarks.list`, `.add`, `.edit` | `bookmarks:read`, `bookmarks:write` |
| create, rename, invite, purpose, topic | `conversations.create`, `.rename`, `.invite`, `.setPurpose`, `.setTopic` | `channels:manage` |
| join | `conversations.join` | `channels:join` |

Only the scopes the current plan needs are checked, so a converged workspace needs only
`channels:read`.

**Renaming is restricted beyond the scope.** Slack allows a rename only by the channel's creator,
a Workspace Admin, or a Channel Manager. A bot can rename a channel it created; a channel a human
made answers `not_authorized` to a bot token. The role reports that error exactly and fails.
Renaming a channel a human created answers **`not_authorized`** to a bot token even when the
scope check passes, because the refusal is the authorization rule, not a missing scope.

**So renames run as the workspace owner.** With `slack_channels_admin_token` set, the rename
call alone uses that user token (user scope `channels:write`); every other step stays the bot's.
Without it, a planned rename is reported as `rename_needs_admin_token: true`. The report also
names whose token it is (`admin_user_id`) and its scopes. `admin.*` methods would need Enterprise
Grid, and the report says whether the workspace is on it (`enterprise_grid`). The owner token
can do anything the owner can, so treat it as the workspace's most powerful credential.

## Calling it

```yaml
- hosts: localhost
  gather_facts: false
  roles:
    - role: richard_lynch.services.slack_channels
      slack_channels_token: "{{ slack_bot_token }}"   # resolved from a secret store
      slack_channels: "{{ slack_channels_declared }}"  # declared in inventory
      slack_channels_apply: false                      # report first; true to act
```

## Tests

- `tests/test_slack_channels.py` (stdlib): validation, the planner, and full runs against
  `tests/stub_slack.py`, a stub workspace that enforces Slack's scope, membership and rename rules and
  pages `conversations.list` two at a time.
- `molecule/default`: the same stub, served for the whole sequence. Converge applies a rename, a
  create with invite, purpose and topic, a join, and a channel with an undeclared member;
  idempotence is a real second reconcile; verify reads the stub's state, then proves report-only
  acts on nothing and that a bot rename of a human's channel (`not_authorized`), an archived
  target and a `#name` all fail without changing the workspace.
