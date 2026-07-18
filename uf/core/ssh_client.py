"""
SCNG SSH Client - Paramiko wrapper for network device SSH.

Path: scng/discovery/ssh/client.py

Adapted from VCollector's ssh_client.py with:
- scng.creds integration
- Legacy device support (old ciphers/KEX)
- ANSI sequence filtering
- Sophisticated prompt detection
- Pagination disabling

Invoke-shell only - no exec mode. This is required for most network devices.

Changes from v1 (aligned with nterm config.py / manager.py):
- LegacySSHSupport: transport_factory instead of global class-level mutation
- Algorithm lists validated against paramiko 3.4 registries (removed 3 ciphers, 2 KEX)
- Fresh SSHClient on SHA2 RSA retry (fixes stale transport "unknown cipher")
"""

import os
import re
import time
import logging
from io import StringIO
from dataclasses import dataclass
from typing import Optional

import paramiko

logger = logging.getLogger(__name__)

# Force SSH debug to stdout: set SSH_DEBUG=1 or run with -v
if os.environ.get('SSH_DEBUG', '').strip() in ('1', 'true', 'yes'):
    if not logger.handlers:
        _handler = logging.StreamHandler()
        _handler.setFormatter(logging.Formatter('%(asctime)s [SSH] %(message)s', datefmt='%H:%M:%S'))
        logger.addHandler(_handler)
        logger.setLevel(logging.DEBUG)


def filter_ansi_sequences(text: str) -> str:
    """
    Remove ANSI escape sequences and control characters.

    Args:
        text: Input text with potential ANSI sequences.

    Returns:
        Cleaned text.
    """
    if not text:
        return text

    # Comprehensive pattern for ANSI sequences and control chars
    ansi_pattern = r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][AB012]|\x07|[\x00-\x08\x0B\x0C\x0E-\x1F]'
    return re.sub(ansi_pattern, '', text)


# Pagination disable commands - shotgun approach
# Fire all of these; wrong ones just error harmlessly
PAGINATION_DISABLE_SHOTGUN = [
    'terminal length 0',           # Cisco IOS/IOS-XE/NX-OS, Arista, Dell, Ubiquiti
    'terminal pager 0',            # Cisco ASA
    'set cli screen-length 0',     # Juniper Junos
    'screen-length 0 temporary',   # Huawei VRP
    'disable clipaging',           # Extreme EXOS
    'terminal more disable',       # Extreme VOSS
    'no page',                     # HP ProCurve
    'set cli pager off',           # Palo Alto
]


@dataclass
class SSHClientConfig:
    """SSH connection configuration."""
    host: str
    username: str
    password: Optional[str] = None
    key_content: Optional[str] = None  # PEM string (in-memory)
    key_file: Optional[str] = None     # Path to key file
    key_passphrase: Optional[str] = None
    port: int = 22
    timeout: int = 30
    shell_timeout: float = 5.0
    inter_command_time: float = 1.0
    expect_prompt_timeout: int = 3000  # ms
    prompt_count: int = 3
    legacy_mode: bool = False
    debug: bool = False
    # Auth isolation knobs. Defaults reproduce the prior hardcoded behavior, so
    # nothing changes unless a caller sets them. allow_agent=False is how a
    # key-file run proves the KEY authenticated and not an ambient agent key.
    allow_agent: bool = True
    look_for_keys: bool = False

    def __post_init__(self):
        if not self.password and not self.key_content and not self.key_file:
            raise ValueError("Either password, key_content, or key_file required")


class LegacySSHSupport:
    """
    Legacy device algorithm sets for paramiko 3.4+.

    Constants only — never applied globally. Used via transport_factory
    to configure individual Transport instances, keeping legacy sessions
    isolated from modern ones.

    All entries validated against paramiko 3.4's internal registries:
    - Ciphers:  Transport._cipher_info
    - KEX:      Transport._kex_info
    - Keys:     Transport._preferred_keys (all stock entries supported)

    Removed from v1 (not in paramiko 3.4 registry):
    - Ciphers:  aes256-gcm@openssh.com, aes128-gcm@openssh.com,
                chacha20-poly1305@openssh.com
    - KEX:      curve25519-sha256 (only @libssh.org variant exists),
                diffie-hellman-group18-sha512
    """

    # Legacy-first ordering: oldest algos first so ancient devices
    # find a match early in the negotiation list.

    LEGACY_KEX = (
        "diffie-hellman-group1-sha1",
        "diffie-hellman-group14-sha1",
        "diffie-hellman-group-exchange-sha1",
        "diffie-hellman-group-exchange-sha256",
        "ecdh-sha2-nistp256",
        "ecdh-sha2-nistp384",
        "ecdh-sha2-nistp521",
        "curve25519-sha256@libssh.org",
        "diffie-hellman-group16-sha512",
    )

    LEGACY_CIPHERS = (
        "aes128-cbc",
        "aes256-cbc",
        "3des-cbc",
        "aes192-cbc",
        "aes128-ctr",
        "aes192-ctr",
        "aes256-ctr",
    )

    LEGACY_KEYS = (
        "ssh-rsa",
        "ssh-dss",
        "rsa-sha2-256",
        "rsa-sha2-512",
    )

    @staticmethod
    def make_transport(sock, **kwargs):
        """
        Transport factory for paramiko SSHClient.connect(transport_factory=...).

        Creates a Transport with legacy algorithm preferences set at the
        INSTANCE level — no global state mutation.

        Args:
            sock: Connected socket (passed by SSHClient.connect)
            **kwargs: gss_kex, gss_deleg_creds, disabled_algorithms
                      (forwarded from SSHClient.connect)
        """
        t = paramiko.Transport(sock, **kwargs)
        t._preferred_kex = LegacySSHSupport.LEGACY_KEX
        t._preferred_ciphers = LegacySSHSupport.LEGACY_CIPHERS
        t._preferred_keys = LegacySSHSupport.LEGACY_KEYS
        logger.debug("Transport configured with legacy algorithms (instance-level)")
        return t


class SSHClient:
    """
    SSH client for network device interaction.

    Uses invoke_shell for interactive session - required for most
    network devices that don't support direct exec.

    Example:
        config = SSHClientConfig(
            host="192.168.1.1",
            username="admin",
            password="secret",
            legacy_mode=True,
        )
        client = SSHClient(config)
        client.connect()
        prompt = client.find_prompt()
        client.set_expect_prompt(prompt)
        output = client.execute_command("show version")
        client.disconnect()
    """

    def __init__(self, config: SSHClientConfig):
        self.config = config
        self._client: Optional[paramiko.SSHClient] = None
        self._shell: Optional[paramiko.Channel] = None
        self._output_buffer = StringIO()
        self._detected_prompt: Optional[str] = None
        self._expect_prompt: Optional[str] = None

    def _authenticate(self) -> None:
        """Establish the SSH transport and authenticate — NO shell.

        Extracted from connect() so the two shell strategies share ONE proven
        negotiation path: the legacy-cipher transport factory, the isolated
        pubkey load, and the SHA2-RSA retry-on-a-fresh-client. connect() adds the
        read-oriented shell on top of this; open_interactive_channel() adds a raw
        PTY on top of the same. The auth story is authored once.
        """
        logger.debug(f"Connecting to {self.config.host}:{self.config.port}")
        logger.debug(f"  Username: {self.config.username}")
        logger.debug(f"  Key file: {self.config.key_file}")
        logger.debug(f"  Legacy mode: {self.config.legacy_mode}")
        logger.debug(f"  Timeout: {self.config.timeout}s")

        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # Build connection params
        connect_params = {
            'hostname': self.config.host,
            'port': self.config.port,
            'username': self.config.username,
            'timeout': self.config.timeout,
            'allow_agent': self.config.allow_agent,
            'look_for_keys': self.config.look_for_keys,
            'disabled_algorithms': {'pubkeys': ['rsa-sha2-512', 'rsa-sha2-256']}
        }

        # Legacy mode: per-transport factory (instance-level, not global)
        if self.config.legacy_mode:
            connect_params['transport_factory'] = LegacySSHSupport.make_transport

        # Add authentication
        if self.config.key_content or self.config.key_file:
            pkey = self._load_private_key()
            connect_params['pkey'] = pkey
            if self.config.password:
                connect_params['password'] = self.config.password
        else:
            connect_params['password'] = self.config.password

        # Connect with fallback for SHA2 RSA
        try:
            logger.debug(f"SSH connect attempt 1 (disabled_algorithms={connect_params.get('disabled_algorithms')})")
            self._client.connect(**connect_params)
            logger.debug("SSH connect attempt 1 succeeded")
        except Exception as e:
            logger.debug(f"SSH connect attempt 1 failed: {type(e).__name__}: {e}")
            logger.debug("Retrying with SHA2 RSA algorithms enabled")
            # Fresh client — stale transport can't renegotiate
            self._client = paramiko.SSHClient()
            self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            connect_params.pop('disabled_algorithms', None)
            self._client.connect(**connect_params)
            logger.debug("SSH connect attempt 2 succeeded")

        logger.debug(f"Connected to {self.config.host}")

    def connect(self) -> None:
        """Establish SSH connection and open the read-oriented shell.

        Unchanged for the broker/read path: authenticate, then _create_shell()
        (which sizes a fixed PTY, sleeps to settle, and DRAINS the banner). The
        drain is right for structured reads and wrong for a terminal — see
        open_interactive_channel().
        """
        self._authenticate()
        logger.debug("Opening interactive shell...")

        # Open interactive shell
        self._create_shell()

    def open_interactive_channel(self, cols: int = 80, rows: int = 24):
        """Authenticate and return a RAW paramiko Channel for an xterm-style
        terminal — the INTERACTIVE posture's transport.

        Deliberately NOT connect()/_create_shell():

          * No drain. _create_shell() eats the login banner + first prompt into
            a log line; a terminal must SHOW them. Here every byte from the box,
            from byte zero, belongs to the user's screen — so we hand back the
            channel un-read.
          * No ANSI filtering. The read path strips escapes to parse clean text;
            xterm.js is a real VT emulator and NEEDS the escapes (colours, cursor
            moves, prompts). Filtering here would corrupt the very sequences the
            terminal exists to render. The caller reads bytes straight off this
            channel — it never routes through _recv_filtered/execute_command.
          * Caller-sized PTY. The pane knows its pixel box; it passes the fitted
            cols/rows and calls channel.resize_pty() on every resize. (The read
            path pins width=200/height=24 and leans on 'terminal length 0'; a
            human terminal wants a PTY that tracks the pane.)

        This is the engineer's own login on its own transport — ungated, and the
        coordinator never mediates these bytes (coordinator.interactive(): "trust:
        UNGATED — you already own the CLI"). Reuses _authenticate(), so a box that
        needs legacy KEX/ciphers or the SHA2-RSA retry reaches the terminal on the
        same negotiation the 1.5 reads proved against your gear.
        """
        self._authenticate()
        logger.debug(f"Opening RAW interactive PTY ({cols}x{rows})...")
        chan = self._client.invoke_shell(term='xterm-256color', width=cols, height=rows)
        # No settle-sleep, no drain: the reader thread is already draining into
        # the terminal, so the banner lands on screen instead of in a log line.
        self._shell = chan
        return chan

    def _create_shell(self) -> None:
        """Create interactive shell stream."""
        logger.debug("Creating shell stream")

        # Note: height=24 is required — some older IOS SSH implementations
        # (e.g., Cisco-1.25) reject or silently fail on height=0 PTY requests.
        # Pagination is handled by 'terminal length 0', not the PTY size.
        self._shell = self._client.invoke_shell(
            term='xterm',
            width=200,
            height=24
        )
        self._shell.settimeout(self.config.timeout)
        logger.debug("Shell opened, waiting 2s for initialization...")

        # Wait for shell initialization
        time.sleep(2)
        logger.debug("Shell init wait complete, draining initial output...")


        # Read initial output
        initial = self._drain_output()
        if initial:
            if "linux" in str.lower(initial):
                raise ValueError("Linux shell initialization is not supported")
            logger.debug(f"Initial output ({len(initial)} bytes): {initial[:200]!r}")
        else:
            logger.debug("No initial output received from shell")

    def _load_private_key(self) -> paramiko.PKey:
        """Load private key from PEM string or file."""
        passphrase = self.config.key_passphrase

        # Determine key source
        if self.config.key_content:
            key_source = StringIO(self.config.key_content)
            load_method = 'from_private_key'
            logger.debug("Loading key from memory")
        elif self.config.key_file:
            key_file = os.path.expanduser(self.config.key_file)
            if not os.path.exists(key_file):
                raise ValueError(f"Key file not found: {key_file}")
            key_source = key_file
            load_method = 'from_private_key_file'
            logger.debug(f"Loading key from file: {key_file}")
        else:
            raise ValueError("No key source provided")

        # Try each key type. ed25519 first so a modern key matches before the
        # RSA attempt logs a spurious "key load failed" on the way past it.
        # All three are file- and PEM-string-loadable via the same classmethods.
        key_classes = [
            ('ED25519', paramiko.Ed25519Key),
            ('ECDSA',   paramiko.ECDSAKey),
            ('RSA',     paramiko.RSAKey),
        ]

        last_error: Optional[Exception] = None
        for key_name, key_class in key_classes:
            try:
                loader = getattr(key_class, load_method)
                if load_method == 'from_private_key':
                    # Reset StringIO position for each attempt
                    if hasattr(key_source, 'seek'):
                        key_source.seek(0)
                    return loader(key_source, password=passphrase)
                else:
                    return loader(key_source, password=passphrase)
            except paramiko.PasswordRequiredException as e:
                # The key parsed as this type but is encrypted and no passphrase
                # was supplied. That is NOT "unsupported format" — it is the
                # right type, missing secret. Surface it distinctly so an e2e
                # run against an encrypted key file fails legibly.
                logger.debug(f"{key_name} key is encrypted, no passphrase: {e}")
                last_error = e
                continue
            except paramiko.SSHException as e:
                # Wrong type for this class (e.g. trying Ed25519Key on an RSA
                # file) raises here — expected while walking the type list. This
                # is a PROBE, not a failure: log it as such so a successful load
                # of a later type doesn't leave "failed" lines above it. The real
                # error is kept in last_error and only surfaces if ALL types miss.
                logger.debug(f"  {key_name} loader rejected key — trying next type")
                last_error = e
                continue
            except Exception as e:
                # Non-SSHException misses (e.g. ECDSA's decode error on RSA PEM)
                # — same story: a probe past the wrong type, not a real failure.
                logger.debug(f"  {key_name} loader rejected key — trying next type "
                             f"({type(e).__name__})")
                last_error = e
                continue

        # If every type raised PasswordRequired, the file is a valid encrypted
        # key and no passphrase was supplied — say so, don't blame the format.
        # (A *wrong* passphrase raises SSHException instead and lands in the
        # generic message below, with paramiko's decrypt error preserved.)
        if isinstance(last_error, paramiko.PasswordRequiredException):
            raise ValueError(
                "Private key is encrypted but no key_passphrase was supplied"
            ) from last_error
        raise ValueError(
            f"Unable to load private key - unsupported format or unreadable key "
            f"(last error: {last_error})"
        )

    def _recv_filtered(self, size: int = 4096) -> str:
        """Read from shell with ANSI filtering."""
        try:
            raw_data = self._shell.recv(size).decode('utf-8', errors='replace')
            return filter_ansi_sequences(raw_data)
        except Exception as e:
            logger.debug(f"Error reading from shell: {e}")
            return ""

    def _drain_output(self) -> str:
        """Read all available output from shell."""
        output = ""
        while self._shell.recv_ready():
            chunk = self._recv_filtered()
            output += chunk
            time.sleep(0.05)
        return output

    def resync(self, settle: float = 1.0) -> None:
        """Lightweight, HARD-BOUNDED prompt re-anchor for use BETWEEN commands.

        Unlike find_prompt(), this never sleeps unconditionally, never re-detects,
        and never resets the prompt to '#'. It drains residue, nudges once, and
        reads at most `settle` seconds for the ALREADY-KNOWN prompt. By
        construction it cannot block longer than `settle` — safe to call on every
        read in a poll. Best-effort: if the prompt isn't seen in time it returns
        anyway (the drain already cleaned the channel); a single read then fails
        on its own merits rather than wedging the poll.
        """
        if not self._shell:
            return
        self._drain_output()
        prompt = self._expect_prompt or self._detected_prompt
        if not prompt:
            return
        try:
            self._shell.send("\n")
        except Exception:
            return
        end = time.time() + settle
        buf = ""
        while time.time() < end:
            if self._shell.recv_ready():
                buf += self._recv_filtered()
                if prompt in buf:
                    return
            else:
                time.sleep(0.02)

    def find_prompt(self, attempt_count: int = 5, timeout: float = 5.0) -> str:
        """
        Auto-detect command prompt.

        Sends newlines and analyzes output to find prompt pattern.

        Args:
            attempt_count: Number of detection attempts.
            timeout: Timeout per attempt in seconds.

        Returns:
            Detected prompt string.
        """
        if not self._shell:
            raise RuntimeError("Shell not initialized")

        logger.debug("Attempting to auto-detect command prompt")

        # Clear any pending data
        stale = self._drain_output()
        if stale:
            logger.debug(f"Drained stale data before prompt detection ({len(stale)} bytes): {stale[:200]!r}")

        # Send newline to trigger prompt
        logger.debug("Sending newline to trigger prompt...")
        self._shell.send("\n")
        time.sleep(3)

        # Collect output
        buffer = ""
        start_time = time.time()
        while time.time() - start_time < 3:
            if self._shell.recv_ready():
                buffer += self._recv_filtered()
            else:
                time.sleep(0.1)

        logger.debug(f"Initial prompt buffer ({len(buffer)} bytes): {buffer!r}")

        # Try to extract prompt
        prompt = self._extract_prompt(buffer)
        if prompt:
            self._detected_prompt = prompt
            logger.debug(f"Detected prompt: {prompt!r}")
            return prompt

        # Additional attempts
        for i in range(attempt_count):
            logger.debug(f"Prompt detection attempt {i + 1}/{attempt_count}")

            self._shell.send("\n")
            buffer = ""

            start_time = time.time()
            while time.time() - start_time < timeout:
                if self._shell.recv_ready():
                    chunk = self._recv_filtered()
                    buffer += chunk
                    logger.debug(f"  Attempt {i+1} recv chunk ({len(chunk)} bytes): {chunk!r}")
                else:
                    if buffer:
                        prompt = self._extract_prompt(buffer)
                        if prompt:
                            self._detected_prompt = prompt
                            logger.debug(f"Detected prompt on attempt {i+1}: {prompt!r}")
                            return prompt
                    time.sleep(0.1)

            logger.debug(f"  Attempt {i+1} total buffer ({len(buffer)} bytes): {buffer[:200]!r}")

            if buffer:
                prompt = self._extract_prompt(buffer)
                if prompt:
                    self._detected_prompt = prompt
                    logger.debug(f"Detected prompt: {prompt!r}")
                    return prompt

        # Fallback
        logger.warning("Could not detect prompt after all attempts, using default '#'")
        self._detected_prompt = "#"
        return "#"

    def _extract_prompt(self, buffer: str) -> Optional[str]:
        """Extract prompt from buffer content."""
        if not buffer or not buffer.strip():
            return None

        # Get non-empty lines
        lines = [line.strip() for line in buffer.split('\n') if line.strip()]
        if not lines:
            return None

        # Prompt patterns - ordered by specificity
        patterns = [
            r'([A-Za-z0-9\-_.@()]+[#>$%])\s*$',  # Standard prompts
            r'([^\r\n]+[#>$%])\s*$',              # Anything ending with prompt char
        ]

        # Common prompt endings
        common_endings = ['#', '>', '$', '%', ':', ']', ')']

        # Check last lines for prompt
        for line in reversed(lines[-5:]):  # Check last 5 lines
            # Skip if line is too long (probably output, not prompt)
            if len(line) > 60:
                continue

            # Try regex patterns
            for pattern in patterns:
                match = re.search(pattern, line)
                if match:
                    prompt = match.group(1).strip()
                    # Handle repeated prompts (e.g., "router# router# router#")
                    base = self._extract_base_prompt(prompt)
                    return base if base else prompt

            # Check for common endings
            if any(line.endswith(char) for char in common_endings) and len(line) < 40:
                return line

        return None

    def _extract_base_prompt(self, text: str) -> Optional[str]:
        """Extract base prompt from potentially repeated text."""
        # Check for repeated patterns
        for ending in ['#', '>', '$', '%']:
            if ending in text:
                parts = text.split(ending)
                if len(parts) > 2:
                    # Multiple occurrences - check if repeated
                    base = parts[0].strip() + ending
                    if len(base) < 40:
                        return base
        return None

    def extract_hostname_from_prompt(self, prompt: Optional[str] = None) -> Optional[str]:
        """
        Extract hostname from detected prompt.

        Handles common formats:
        - Cisco/Arista/Juniper: "hostname#" or "hostname>"
        - Linux: "user@hostname:~$" or "user@hostname $"
        - Juniper: "user@hostname>"

        Args:
            prompt: Prompt string (uses detected prompt if None).

        Returns:
            Extracted hostname or None.
        """
        prompt = prompt or self._detected_prompt
        if not prompt:
            return None

        # Linux style: user@hostname:path$ or user@hostname$
        match = re.match(r'^[^@]+@([A-Za-z0-9\-_.]+)', prompt)
        if match:
            return match.group(1)

        # Network device style: hostname# or hostname> or hostname(config)#
        # Strip config mode indicators first
        clean_prompt = re.sub(r'\([^)]+\)', '', prompt)
        match = re.match(r'^([A-Za-z0-9\-_.]+)[#>$%:\]]', clean_prompt)
        if match:
            return match.group(1)

        return None

    @property
    def hostname(self) -> Optional[str]:
        """Get hostname extracted from prompt."""
        return self.extract_hostname_from_prompt()

    def set_expect_prompt(self, prompt: str) -> None:
        """Set the prompt string to expect after commands."""
        self._expect_prompt = prompt
        logger.debug(f"Expect prompt set to: {prompt!r}")

    def disable_pagination(self) -> None:
        """
        Disable pagination by trying common commands.

        Fires multiple vendor commands — wrong ones just produce errors
        that are drained and discarded. Each command is followed by a
        find_prompt() to confirm the shell returned to a clean state
        before sending the next.
        """
        logger.debug(f"Disabling pagination (shotgun approach, {len(PAGINATION_DISABLE_SHOTGUN)} commands)")

        for i, cmd in enumerate(PAGINATION_DISABLE_SHOTGUN):
            try:
                logger.debug(f"  Pagination [{i+1}/{len(PAGINATION_DISABLE_SHOTGUN)}]: {cmd}")
                self._shell.send(cmd + '\n')
                # Confirm prompt returns — consumes any error output
                # and validates the shell is ready for the next command
                # self.find_prompt(attempt_count=1, timeout=3.0)
            except Exception as e:
                logger.debug(f"  Pagination cmd failed (expected): {cmd} - {e}")

        # Final prompt check — confirm clean shell state
        logger.debug("Pagination commands sent, final prompt check...")
        prompt = self.find_prompt(attempt_count=2, timeout=3.0)
        logger.debug(f"Pagination disable complete, prompt={prompt!r}")

    def execute_command(
        self,
        command: str,
        timeout: Optional[float] = None,
    ) -> str:
        """
        Execute command and return output.

        Args:
            command: Command string. Can be comma-separated for multiple commands.
            timeout: Override default timeout.

        Returns:
            Command output with ANSI sequences filtered.
        """
        if not self._shell:
            raise RuntimeError("Not connected")

        timeout = timeout or self.config.expect_prompt_timeout / 1000

        # Split comma-separated commands
        commands = [cmd.strip() for cmd in command.split(',') if cmd.strip()]

        output_buffer = StringIO()

        for cmd in commands:
            if cmd in ('\\n', '\n'):
                self._shell.send('\n')
                time.sleep(0.1)
                continue

            # ── Drain stale data before sending ──────────────────
            # Between poll cycles, the channel may accumulate trailing
            # bytes from the previous command (post-prompt newlines,
            # late-arriving output fragments). Without draining, the
            # next _wait_for_prompt() reads stale data first, finds
            # the *previous* command's prompt, and returns immediately
            # with garbage — causing a one-command offset desync where
            # every collection parses the previous collection's output.
            stale = self._drain_output()
            if stale:
                logger.debug(
                    f"Drained {len(stale)} bytes of stale data before '{cmd}'"
                )

            logger.debug(f"Sending: {cmd}")
            self._shell.send(cmd + '\n')

            # Wait for prompt
            cmd_output = self._wait_for_prompt(timeout)
            output_buffer.write(cmd_output)

            time.sleep(self.config.inter_command_time)

        return output_buffer.getvalue()

    def _wait_for_prompt(self, timeout: float) -> str:
        """Wait for prompt to appear in output."""
        prompt = self._expect_prompt or self._detected_prompt

        if not prompt:
            # No prompt detection - just wait and read
            logger.debug(f"No prompt set, waiting {self.config.shell_timeout}s then draining")
            time.sleep(self.config.shell_timeout)
            return self._drain_output()

        logger.debug(f"Waiting for prompt {prompt!r} (timeout={timeout}s)")
        output = ""
        end_time = time.time() + timeout
        bytes_received = 0

        while time.time() < end_time:
            if self._shell.recv_ready():
                chunk = self._recv_filtered()
                output += chunk
                bytes_received += len(chunk)

                if prompt in output:
                    logger.debug(f"Prompt detected in output ({bytes_received} bytes received)")
                    # Brief settle: some devices send trailing bytes
                    # (newlines, control chars) after the prompt.
                    # Capture them now instead of leaving them to
                    # poison the next command's buffer.
                    time.sleep(0.05)
                    if self._shell.recv_ready():
                        output += self._recv_filtered()
                    return output

            time.sleep(0.01)

        elapsed = timeout
        logger.warning(f"Timeout waiting for prompt {prompt!r} after {elapsed:.1f}s ({bytes_received} bytes received)")
        if output:
            logger.debug(f"  Partial output tail: {output[-200:]!r}")
        return output

    def disconnect(self) -> None:
        """Close SSH connection."""
        if self._shell:
            try:
                self._shell.close()
            except Exception as e:
                logger.debug(f"Shell close error: {e}")
            self._shell = None

        if self._client:
            try:
                self._client.close()
            except Exception as e:
                logger.debug(f"Client close error: {e}")
            self._client = None

        logger.debug(f"Disconnected from {self.config.host}")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False