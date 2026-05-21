# @jaringan-dagang/channel-adapter

Pure-interface contract for chatbot channel sidecars. Defines:

- `IncomingMessage` — normalized payload that channel webhooks emit
  (web, WA, FB Messenger, Telegram, IG DM) and feed to the bot bridge.
- `OutgoingMessage` — what the bot writes to the CRM DB. The channel
  adapter polls for these and renders per-platform on delivery.
- `Renderer<C>` — the per-channel transform function: markdown blocks
  → channel-flavored text.

No business logic. No I/O. Just types + the canonical markdown grammar
the bot uses. Channel apps depend on this; they don't depend on each
other.

## Renderer guarantees per channel

| Channel | Bold | Italic | Links | Inline images | Code |
|---|---|---|---|---|---|
| `web` | pass-through markdown | pass-through | pass-through | inline `<img>` | fenced |
| `whatsapp` | `*bold*` | `_italic_` | `label: url\n` | URL on its own line | ` ``` ` |
| `messenger` | strip emphasis | strip | Generic Template card | image attachment | strip |
| `telegram` | MarkdownV2 escapes | MarkdownV2 | inline `[label](url)` | `sendPhoto` | MarkdownV2 |
| `ig-dm` | strip | strip | `label: url\n` | image attachment | strip |

The `web` renderer is the identity function (already markdown). Other
renderers are pure transforms — no network calls.

## Reference impl

See `apps/channels/web` and `apps/channels/whatsapp` in
[MetatechID/jaringan-dagang](https://github.com/MetatechID/jaringan-dagang)
(private) for concrete delivery + webhook integrations.
