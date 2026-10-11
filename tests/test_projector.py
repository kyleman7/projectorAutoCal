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


class _ScriptedTransport:
    """Answers each sent command from a list of replies, in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def connect(self): pass
    def disconnect(self): pass

    def send(self, data):
        self.sent.append(data.decode())

    def recv_until_colon(self):
        return self.replies.pop(0)


def _client(replies, wait, monkeypatch):
    from projector_cal import projector as pj
    monkeypatch.setattr(pj, "_SIGNAL_POLL_S", 0)
    monkeypatch.setattr(pj, "_SIGNAL_SETTLE_S", 0)
    c = pj.ProjectorClient(_ScriptedTransport(replies), command_settle_ms=0)
    c.signal_wait_s = wait
    return c


def test_set_waits_for_lost_signal_and_retries(monkeypatch):
    c = _client(["ERR\r:", "SIGNAL=00:", "SIGNAL=00:", "SIGNAL=01:", ":"], wait=60, monkeypatch=monkeypatch)
    c.set_setting("GAMMALV", 86, param="00")
    assert c._transport.sent == ["GAMMALV 00 86\r", "SIGNAL?\r", "SIGNAL?\r", "SIGNAL?\r", "GAMMALV 00 86\r"]


def test_err_with_signal_present_is_not_retried(monkeypatch):
    import pytest
    from projector_cal.projector import ProjectorError
    c = _client(["ERR\r:", "SIGNAL=01:"], wait=60, monkeypatch=monkeypatch)
    with pytest.raises(ProjectorError):
        c.set_setting("MCFI", "00")
    assert c._transport.sent == ["MCFI 00\r", "SIGNAL?\r"]


def test_no_wait_by_default(monkeypatch):
    import pytest
    from projector_cal.projector import ProjectorError
    c = _client(["ERR\r:"], wait=0, monkeypatch=monkeypatch)
    with pytest.raises(ProjectorError):
        c.set_setting("GAMMALV", 86, param="00")
    assert c._transport.sent == ["GAMMALV 00 86\r"]
