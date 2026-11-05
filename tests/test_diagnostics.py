import logging

from copper_pilot_cli.diagnostics import RedactingFormatter


def _format(msg, args):
    record = logging.LogRecord("copper_pilot_cli.t", logging.INFO, __file__, 1, msg, args, None)
    return RedactingFormatter("%(message)s").format(record)


def test_numeric_format_args_survive_redaction() -> None:
    line = _format("Chat turn finished (wall=%.1fs, attempts=%d, ok=%s)", (12.34, 3, True))
    assert line == "Chat turn finished (wall=12.3s, attempts=3, ok=True)"


def test_text_args_are_still_redacted() -> None:
    line = _format("key %s / body %s", ("cf_live_abc123", "line one\nline two\nline three"))
    assert "cf_live_abc123" not in line
    assert "[REDACTED]" in line
    assert "line one … [2 lines omitted]" in line


def test_mapping_args_redact_values_and_keep_numbers() -> None:
    line = _format("token %(token)s took %(wall).1fs", {"token": "cf_live_x", "wall": 2.06})
    assert line == "token [REDACTED] took 2.1s"


def test_handler_writes_numeric_lines(tmp_path) -> None:
    handler = logging.FileHandler(tmp_path / "d.log", encoding="utf-8")
    handler.setFormatter(RedactingFormatter("%(message)s"))
    logger = logging.getLogger("copper_pilot_cli.test_diagnostics")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("wall=%.1fs", 1.25)
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert (tmp_path / "d.log").read_text().strip() == "wall=1.2s"
