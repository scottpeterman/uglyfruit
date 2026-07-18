"""
SCNG TextFSM Parser - Template-based CLI output parsing.

Path: scng/discovery/ssh/parsers.py

Uses tfsm_fire.TextFSMAutoEngine from scng.utils for template matching.
"""

import re
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


@dataclass
class ParseResult:
    """Result of parsing CLI output."""
    success: bool
    template_name: Optional[str] = None
    records: Optional[List[Dict[str, Any]]] = None
    score: float = 0.0
    error: Optional[str] = None

    @property
    def record_count(self) -> int:
        return len(self.records) if self.records else 0


class OutputCleaner:
    """Clean raw CLI output for TextFSM parsing."""

    # Patterns to skip at start of output
    PREAMBLE_PATTERNS = [
        r'^terminal\s+(length|width)',
        r'^pagination\s+disabled',
        r'^screen-length\s+disable',
        r'^\s*$',
    ]

    # Command echo pattern
    COMMAND_ECHO_PATTERN = r'^[\w\-\.]+[\#\>\$\)].*?(show|display|get)\s+'

    # Trailing prompt pattern
    TRAILING_PROMPT_PATTERN = r'^[\w\-\.]+[\#\>\$\)]\s*$'

    @classmethod
    def clean(cls, raw_output: str) -> str:
        """
        Clean raw CLI output for TextFSM parsing.

        Removes:
        - Preamble lines (terminal length, pagination messages)
        - Command echo (hostname#show command)
        - Trailing prompts
        """
        lines = raw_output.split('\n')
        cleaned_lines = []
        found_output_start = False

        for line in lines:
            line_stripped = line.strip()

            # Skip empty lines at start
            if not found_output_start and not line_stripped:
                continue

            # Skip preamble
            if not found_output_start:
                is_preamble = any(
                    re.match(p, line_stripped, re.IGNORECASE)
                    for p in cls.PREAMBLE_PATTERNS
                )
                if is_preamble:
                    continue

                # Check for command echo
                if re.match(cls.COMMAND_ECHO_PATTERN, line_stripped, re.IGNORECASE):
                    found_output_start = True
                    continue

                found_output_start = True

            # Skip trailing prompts
            if re.match(cls.TRAILING_PROMPT_PATTERN, line_stripped):
                continue

            cleaned_lines.append(line)

        # Remove trailing empty lines
        while cleaned_lines and not cleaned_lines[-1].strip():
            cleaned_lines.pop()

        return '\n'.join(cleaned_lines)


class TextFSMParser:
    """
    TextFSM-based CLI output parser.

    Uses tfsm_fire.TextFSMAutoEngine from scng.utils.

    Example:
        parser = TextFSMParser()
        result = parser.parse(output, "cisco_ios_show_cdp_neighbors")
        if result.success:
            for record in result.records:
                print(record)
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        verbose: bool = False,
    ):
        """
        Initialize parser.

        Args:
            db_path: Path to tfsm_templates.db. If None, searches default locations.
            verbose: Enable verbose output from tfsm_fire.
        """
        from scng.utils.tfsm_fire import TextFSMAutoEngine

        # Find database
        db = self._find_database(db_path)
        if not db:
            raise FileNotFoundError(
                "TextFSM template database not found. Searched:\n"
                f"  - {Path.home() / '.scng' / 'tfsm_templates.db'}\n"
                f"  - {Path(__file__).parent.parent.parent / 'utils' / 'tfsm_templates.db'}"
            )

        self.db_path = db
        self.verbose = verbose
        self._engine = TextFSMAutoEngine(db, verbose=verbose)
        logger.debug(f"TextFSMParser initialized with database: {db}")

    def _find_database(self, db_path: Optional[str]) -> Optional[str]:
        """Find template database."""
        if db_path and Path(db_path).exists():
            return db_path

        # Search default locations
        search_paths = [
            Path(__file__).parent.parent.parent / "utils" / "tfsm_templates.db",
            Path.home() / ".scng" / "tfsm_templates.db",
            Path.home() / ".vcollector" / "tfsm_templates.db",
        ]

        for p in search_paths:
            if p.exists():
                return str(p)

        return None

    def parse(
        self,
        output: str,
        filter_string: str,
        clean_output: bool = True,
    ) -> ParseResult:
        """
        Parse CLI output against matching templates.

        Args:
            output: Raw CLI output from device.
            filter_string: Template filter string
                          (e.g., "cisco_ios_show_cdp_neighbors_detail").
            clean_output: Clean output before parsing (default True).

        Returns:
            ParseResult with parsed records.
        """
        logger.debug(f"parse() called with filter={filter_string}, output_len={len(output) if output else 0}")

        if not output or not output.strip():
            logger.debug("parse() returning: empty output")
            return ParseResult(success=False, error="Empty output")

        # Clean the output
        if clean_output:
            logger.debug("parse() cleaning output...")
            output = OutputCleaner.clean(output)
            logger.debug(f"parse() cleaned output_len={len(output)}")
        if filter_string is None:
            filter_string = "lldp_neighbors"
        try:
            # Use tfsm_fire engine - returns (template, parsed_data, score)
            logger.debug("parse() calling tfsm_fire.find_best_template()...")
            template, parsed_data, score = self._engine.find_best_template(
                output, filter_string
            )
            logger.debug(f"parse() tfsm_fire returned: template={template}, records={len(parsed_data) if parsed_data else 0}, score={score}")

            if parsed_data and score > 0:
                return ParseResult(
                    success=True,
                    template_name=template,
                    records=parsed_data,
                    score=score,
                )
            else:
                return ParseResult(
                    success=False,
                    error=f"No matching template found for filter: {filter_string}"
                )

        except Exception as e:
            logger.debug(f"TextFSM parsing failed: {e}")
            return ParseResult(success=False, error=str(e))

    def list_templates(self, filter_string: Optional[str] = None) -> List[str]:
        """List available templates matching filter."""
        with self._engine.connection_manager.get_connection() as conn:
            templates = self._engine.get_filtered_templates(conn, filter_string)
            return [t['cli_command'] for t in templates]