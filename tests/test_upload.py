from ax_cli.commands.upload import _message_attachment_ref


def test_upload_message_attachment_ref_keeps_preview_pointers():
    assert _message_attachment_ref(
        attachment_id="att-1",
        content_type="image/png",
        filename="mockup.png",
        size_bytes=123,
        url="/api/v1/uploads/files/mockup.png",
        context_key="upload:123:mockup.png:att-1",
    ) == {
        "id": "att-1",
        "content_type": "image/png",
        "filename": "mockup.png",
        "size_bytes": 123,
        "url": "/api/v1/uploads/files/mockup.png",
        "context_key": "upload:123:mockup.png:att-1",
    }
