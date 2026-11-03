# slack_message

Send one Slack message as a bot: to a user (opening or reusing the DM) or to a channel, optionally
uploading one file with the message as its comment. Every Slack response is read back, so a refusal
fails the play with Slack's own error string instead of becoming a message nobody receives.

Inputs are in [`meta/argument_specs.py`](meta/argument_specs.py) (the `.yml` and
`defaults/main.yml` are generated from it). The recipient, the text, the file and the token's
1Password item are **required, with no default**: they are the caller's facts. The only defaults (`credential` field, the `https://slack.com/api` base URL, a 30 s share wait) are right
for any caller.

## Why `slack_*` and not `config_*` / `utils_*`

- not `config_*`: that prefix configures one cross-cutting concern ON hosts. This role configures
  nothing; it performs an action against an external API.
- not `utils_*`: those are pure helpers with no side effects. Posting a message is a side effect.
- not `service_*`: it deploys nothing.
- **`<vendor>_*`, "bound to one vendor's API"**, is exactly this: every call is a Slack Web API
  method. It is a mechanism used by many callers, and the content (who, what, which bot) belongs to
  them.

## What it does

All I/O tasks run on the controller (`delegate_to: localhost`), whatever the play's hosts are.

| step | Slack method | read-back |
|---|---|---|
| user target (`U…`/`W…`) | `conversations.open` | ok, channel id returned |
| text only | `chat.postMessage` | ok, response names the same channel and a `ts` |
| file: 1 | `files.getUploadURLExternal` (filename, length) | ok, `upload_url` + `file_id` |
| file: 2 | POST the raw bytes to `upload_url` (`uri` with `src`) | HTTP 200 |
| file: 3 | `files.completeUploadExternal` (files, channel_id, initial_comment) | ok, returns that `file_id` |
| file: 4 | `files.info`, polled up to `slack_message_share_wait_s` | the file's `shares` include the conversation |

Step 4 exists because Slack shares a completed upload **asynchronously**: an ok from step 3 does not
prove the recipient can see the file. Needs scopes `chat:write`, `im:write` (user targets),
`files:write`, `files:read`.

`uri` with `src` sends the file as the request body byte for byte. A `slurp` + `b64decode` round
trip goes through a Jinja string and would corrupt a binary file such as a signed `.shortcut`.

## Secrets

The token is resolved once with `richard_lynch.common.onepassword_cached` and every task that carries it is
`no_log`. Calls run with `failed_when: false` and a separate assert reports the result: with the
token in scope a failure would print only "censored", and the assert prints Slack's answer
(`{"ok":false,"error":"..."}`), which contains no secret. The parsed response is also `no_log`,
because `files.getUploadURLExternal` returns a pre-signed upload URL.

## Sending is an action

A second run sends a second message. `changed` is reported only by `chat.postMessage` and
`files.completeUploadExternal`.

## Usage

```yaml
- ansible.builtin.include_role:
    name: richard_lynch.services.slack_message
  vars:
    slack_message_target: C0123456789          # a channel id, or a user id for a DM
    slack_message_text: "Report attached."
    slack_message_file: /tmp/report.pdf
    slack_message_token_item: slack-bot-token  # the 1Password item holding the bot token
    slack_message_op_vault: Example-Vault
```

Pass every option in the include's `vars:`. A value set at play level is lost on the role's second
call in the same play: the generated resolver rebinds unsupplied options to their defaults, and
the default API URL is the real slack.com.

## Tests

`molecule test` runs the real role against `molecule/default/slack_stub.py`, a stdlib HTTP stand-in
for the five methods and the upload URL on an ephemeral port. A stub `richard_lynch.common.onepassword_cached`
returns a fake token, and the real collection is not on the path, so a test run cannot reach
1Password or Slack. `verify.yml` asserts on what reached the stub (channel, comment, title, and the
sha256 of the uploaded bytes against the fixture) rather than on what the role reported. Six cases
must fail with a reason: an unknown user (`user_not_found`), a file never shared, a wrong token, an
empty token, a missing file and a malformed target.

Two mutations are known to fail the scenario: posting `channel_id` as the raw user id (the role's
own share read-back fails in converge) and sending the title as the comment (`verify` fails).
