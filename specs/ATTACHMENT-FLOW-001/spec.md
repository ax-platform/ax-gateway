# ATTACHMENT-FLOW-001: CLI Context Attachment Flow

**Status:** Draft  
**Owner:** @alex  
**Date:** 2026-04-12  
**Related:** ax-backend ATTACHMENTS-001, LISTENER-001

## Purpose

Define the CLI contract for sharing files through aX context and messages.
The CLI must not create split-brain attachment state where the uploaded bytes
land in one space, the context pointer lands in another, and the message lands
in a third. It must also make the product distinction clear: message
attachments are polished transcript previews, while context uploads are durable
app signals that open the Context experience.

## Flow

`axctl upload file` performs one logical collaboration operation:

1. Resolve the target `space_id`.
2. Upload bytes to `POST /api/v1/uploads/` with that `space_id`.
3. Store a context pointer under that same `space_id`.
4. Send a message in that same `space_id` with attachment metadata containing
   `id`, `filename`, `content_type`, `size_bytes`, `url`, and `context_key`.

The message is the visible signal. The context entry is the backing store.
Agents and humans should learn that a file exists from the message stream, then
load the artifact from context when they need the bytes or text content.

`axctl send --file` follows the same message-backed attachment model. Use it
when the main user intent is "send this message with this file attached." This
path should render as the best available inline attachment preview in the
transcript.

`axctl upload file` is for the "this artifact was added to context" event. It
should emit one compact context-upload signal card. Opening that signal should
launch the Context widget/app panel. The Context widget is the long-term
North Star and should converge toward the quality of the inline attachment
preview before the preview surface is simplified.

`axctl context upload-file` is a lower-level storage-only primitive. It should
be used for quiet backing-store writes, not as the default collaboration path.
If a human or agent should notice the upload, use `axctl upload file` or
`axctl send --file`.

`axctl upload file --no-message` and `--quiet` still store the context pointer,
but intentionally skip the chat signal.

`axctl context download <key>` performs the inverse:

1. Resolve the target `space_id`.
2. Read the context pointer from that `space_id`.
3. Follow the stored upload URL while passing the same `space_id`.

## File Type Policy

The backend owns the canonical allowlist. The CLI should set accurate MIME
types for common artifact files so the backend can make an explicit decision.

Code and active-document formats may be accepted for collaboration, but they
should not be treated as inline-safe previews unless the backend explicitly
marks them safe.

## Acceptance Criteria

- Upload API, context API, and message API receive the same resolved space id.
- Message metadata contains both `url` and `context_key`.
- `axctl upload file` sends a message by default.
- `axctl send --file` is documented as the message attachment preview path.
- `axctl upload file` is documented as the context-upload signal path.
- `axctl upload file --no-message` stores context without sending a message.
- `axctl context download <key>` can retrieve a file uploaded by an agent when
  the active profile has access to the target space.
- Unsupported file types fail with a clear backend error.
