from projector_cal.projector import _reply


def test_reply_waits_past_an_event_notice():
    assert _reply(b"IMEVENT=0001 03 00000002 00000000 T1 F1\r:") is None


def test_reply_strips_event_notice_before_the_answer():
    assert _reply(b"IMEVENT=0001 03 00000000 00000000 T1 F1\r:PWR=01\r:") == "PWR=01\r:"


def test_plain_replies_unchanged():
    assert _reply(b"SIGNAL=01\r:") == "SIGNAL=01\r:"
    assert _reply(b":") == ":"
    assert _reply(b"ERR\r:") == "ERR\r:"
    assert _reply(b"PWR=0") is None
