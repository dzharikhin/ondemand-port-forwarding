import dataclasses
import logging

from ondemandportforward.notifier import Notifier


async def test_no_smtp_logs_replies_instead_of_sending(cfg, caplog):
    cfg = dataclasses.replace(cfg, smtp_host="")
    notifier = Notifier(cfg)

    with caplog.at_level(logging.INFO, logger="ondemandportforward.notifier"):
        await notifier.send_failure("to@example.com", "boom")  # must not raise
        await notifier.send_stopped("to@example.com", ["a1b2c3d4"])

    assert caplog.text.count("smtp disabled") == 2
