from voqelis.audio import is_audio_document


def test_audio_mime_is_accepted():
    assert is_audio_document(mime_type="audio/mpeg", file_name="unknown.bin")


def test_known_audio_extension_is_accepted():
    assert is_audio_document(mime_type=None, file_name="recording.m4a")


def test_non_audio_document_is_rejected():
    assert not is_audio_document(mime_type="application/pdf", file_name="file.pdf")
