"""Server-side structured parsing over tfsm_fire (your 'output selects template').

Kept out of the connection adapter on purpose: parsing is engine-agnostic
policy. use_textfsm=True routes raw output here; the show-only core works
fully without tfsm_fire or a template DB present — if the parser can't be
constructed, the raw text is returned alongside the error so nothing is lost.

The filter string is intentionally broad — just the vendor family — so
tfsm_fire scores output across all that family's templates and lets the
output pick its own template. Narrow it with command tokens if you want.
"""

import logging
from functools import lru_cache
from typing import Any

from uf.mcpssh.config import settings

logger = logging.getLogger(__name__)

_FAMILY = {"cisco": "cisco_ios", "arista": "arista_eos", "juniper": "juniper_junos"}


@lru_cache(maxsize=1)
def _get_parser():
    # Lazy + cached: importing/constructing only happens on first textfsm request.
    from uf.mcpssh.parsers import TextFSMParser

    return TextFSMParser(db_path=settings.tfsm_db_path)


def parse_with_tfsm(raw: str, vendor: str, command: str) -> dict[str, Any]:
    try:
        parser = _get_parser()
    except Exception as e:  # missing engine, missing DB, etc.
        logger.debug("TextFSM unavailable: %s", e)
        return {"parsed": False, "error": f"TextFSM unavailable: {e}", "raw": raw}

    family = _FAMILY.get(vendor, vendor)
    result = parser.parse(raw, family)
    if result.success:
        return {
            "parsed": True,
            "template": result.template_name,
            "score": result.score,
            "record_count": result.record_count,
            "records": result.records,
        }
    return {"parsed": False, "error": result.error, "raw": raw}
