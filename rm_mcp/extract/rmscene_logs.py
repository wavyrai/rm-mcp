"""Keep rmscene's "newer format" notices out of the user's terminal.

Pages from current tablet firmware carry fields rmscene 0.8 does not parse.
The common one is SceneInfo (block type 13, 0x0D): newer firmware writes more
fields after ``paper_size``, rmscene reads the ones it knows and keeps the rest
as ``extra_data``. It then logs, for every page it reads:

- WARNING "Some data has not been read. The data may have been written using a
  newer format than this reader supports."
- INFO "In MainBlockInfo(... block_type=13 ...) only read N bytes"

and for a block type it does not know at all, WARNING "Unknown block type N.
Skipping M bytes." Strokes and text live in other blocks and read in full
(tests/test_fixtures.py checks this on ``future_format.rm``), so these are
debug detail. They are re-logged at DEBUG on ``rm_mcp.extract.rmscene``; every
other rmscene message passes through unchanged.
"""

import logging

logger = logging.getLogger("rm_mcp.extract.rmscene")

_NOTICES = (
    "Some data has not been read",
    "In %s only read %d bytes",
    "Unknown block type",
)
_SOURCES = ("rmscene.tagged_block_reader", "rmscene.scene_stream")


class _NewerFormatToDebug(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and record.msg.startswith(_NOTICES):
            logger.debug("rmscene: %s", record.getMessage())
            return False
        return True


_FILTER = _NewerFormatToDebug()


def quiet_rmscene() -> None:
    """Install the filter (idempotent)."""
    for name in _SOURCES:
        source = logging.getLogger(name)
        if _FILTER not in source.filters:
            source.addFilter(_FILTER)
