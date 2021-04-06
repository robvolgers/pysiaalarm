"""This is a class for SIA Events."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod, abstractproperty
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional, Union

from Crypto.Cipher import AES
from Crypto.Cipher._mode_cbc import CbcMode

from .account import SIAAccount
from .const import IV
from .errors import EventFormatError
from .utils import (
    MAIN_MATCHER,
    OH_MATCHER,
    MessageTypes,
    ResponseType,
    SIACode,
    SIAXData,
    _get_matcher,
    _load_adm_mapping,
    _load_sia_codes,
    _load_xdata,
)

_LOGGER = logging.getLogger(__name__)


@dataclass  # type: ignore
class BaseEvent(ABC):
    """Base class for Events."""

    # From Main Matcher
    full_message: Optional[str] = None
    msg_crc: Optional[str] = None
    length: Optional[str] = None
    encrypted: Optional[bool] = None
    message_type: Optional[Union[MessageTypes, str]] = None
    receiver: Optional[str] = None
    line: Optional[str] = None
    account: Optional[str] = None
    sequence: Optional[str] = None

    # Content to be parsed
    content: Optional[str] = None
    encrypted_content: Optional[str] = None

    # From (Encrypted) Content
    ti: Optional[str] = None
    id: Optional[str] = None
    ri: Optional[str] = None
    code: Optional[str] = None
    message: Optional[str] = None
    x_data: Optional[str] = None
    timestamp: Optional[datetime] = None

    # From ADM-CID
    event_qualifier: Optional[str] = None
    event_type: Optional[str] = None
    partition: Optional[str] = None

    # Parsed fields
    calc_crc: Optional[str] = None
    extended_data: Optional[SIAXData] = None
    sia_account: Optional[SIAAccount] = None
    sia_code: Optional[SIACode] = None

    @property
    def valid_message(self) -> bool:
        """Return True for OH and NAK Events."""
        return True  # pragma: no cover

    @property
    def code_not_found(self) -> bool:
        """Return True if there is no Code."""
        return True if self.sia_code is None else False

    @abstractproperty
    def response(self) -> Optional[ResponseType]:
        """Abstract method."""
        pass  # pragma: no cover

    @property
    def valid_timestamp(self) -> bool:
        """Return True for OH and NAK Events."""
        return True  # pragma: no cover

    @abstractmethod
    def create_response(self) -> bytes:
        """Abstract method."""
        pass  # pragma: no cover

    def set_sia_code(self) -> None:
        """Return the SIA Code object, based on the code field."""
        if self.code:  # pragma: no cover
            self.sia_code = _load_sia_codes().get(self.code)  # pylint: disable=E1101

    def _get_crypter(self) -> Optional[CbcMode]:
        """Give back a encrypter/decrypter."""
        if not self.sia_account:
            return None  # pragma: no cover
        if not self.sia_account.key_b:
            return None  # pragma: no cover
        cypher = AES.new(self.sia_account.key_b, AES.MODE_CBC, IV)
        if isinstance(cypher, CbcMode):
            return cypher
        return None  # pragma: no cover

    @classmethod
    def from_line(
        cls, incoming: str, accounts: Optional[Dict[str, SIAAccount]] = None
    ) -> SIAEvent:
        """Create a Event from a line.

        Arguments:
            incoming {str} -- The line to be parsed.
            accounts {List[SIAAccount]} -- accounts to check against, optional

        Raises:
            EventFormatError: If the event is not formatted according to SIA DC09 or ADM-CID.

        """
        line_match = MAIN_MATCHER.match(incoming)
        if not line_match:
            oh_event = OH_MATCHER.match(incoming)
            if oh_event:
                fields = oh_event.groupdict()
                return OHEvent(
                    full_message=incoming,
                    msg_crc="",
                    message_type=MessageTypes.OH,
                    length=str(len(incoming)),
                    encrypted=False,
                    receiver=fields["receiver"],
                    line=fields["line"],
                    account=fields["account"],
                    id=fields["id"],
                )
            raise EventFormatError(
                "No matches found, event was not a SIA or ADM Spec event, line was: %s",
                incoming,
            )
        main_content = line_match.groupdict()

        encrypted = True if main_content["encrypted_flag"] else False
        acc = main_content["account"]
        sia_account = None
        if accounts and acc:
            sia_account = accounts.get(acc, None)

        return SIAEvent(
            full_message=incoming[8:],
            msg_crc=main_content["crc"],
            length=main_content["length"],
            encrypted=encrypted,
            message_type=main_content["message_type"],
            sequence=main_content["sequence"],
            receiver=main_content["receiver"],
            line=main_content["line"],
            account=acc,
            content=main_content["rest"] if not encrypted else None,
            encrypted_content=main_content["rest"] if encrypted else None,
            sia_account=sia_account,
        )

    @staticmethod
    def _get_timestamp() -> str:
        """Create a timestamp in the right format."""
        return datetime.utcnow().strftime("_%H:%M:%S,%m-%d-%Y")

    @staticmethod
    def _crc_calc(msg: str) -> str:
        """Calculate the CRC of the msg."""
        crc = 0
        for letter in str.encode(msg):
            temp = letter
            for _ in range(0, 8):
                temp ^= crc & 1
                crc >>= 1
                if (temp & 1) != 0:
                    crc ^= 0xA001
                temp >>= 1
        return ("%x" % crc).upper().zfill(4)


@dataclass
class SIAEvent(BaseEvent):
    """Class for SIAEvents."""

    # From Main Matcher
    full_message: str
    msg_crc: str
    length: str
    encrypted: bool
    message_type: Union[MessageTypes, str]
    receiver: str
    line: str
    account: str

    def __post_init__(self) -> None:
        """Run post init logic."""
        # Convert the message type to the enum
        if isinstance(self.message_type, str):  # pragma: no cover
            self.message_type = MessageTypes(self.message_type)

        # Calculate the CRC of the message.
        self.calc_crc = self._crc_calc(self.full_message)
        # If there is encrypted content and a key, decrypt
        if self.sia_account and self.sia_account.encrypted and self.encrypted_content:
            self.decrypt_content()
        # If there is content (either after decrypting or directly) parse
        if self.content:
            self.parse_content()
        # If it is a ADM-CID message, map the qualifier and type to a code.
        if (
            self.message_type == MessageTypes.ADMCID
            and self.event_qualifier is not None
            and self.event_type is not None
        ):
            self.parse_adm()
        # If there is a code, map it to the full SIA Code spec.
        if self.code:
            self.set_sia_code()
        # If there is x_data, parse it.
        if self.x_data:
            self.parse_extended_data()  # pragma: no cover

    @property
    def response(self) -> Optional[ResponseType]:
        """Get the responsetype."""
        if not self.valid_message:
            return None
        if self.code_not_found:
            return ResponseType.DUH
        if not self.sia_account:
            return ResponseType.NAK  # pragma: no cover
        if not self.valid_timestamp:
            return ResponseType.NAK
        if self.extended_data is not None:  # pragma: no cover
            if self.extended_data.identifier in ["K"]:
                return ResponseType.RSP
        return ResponseType.ACK

    @property
    def sia_string(self) -> str:  # pragma: no cover
        """Create a string with the SIA codes and some other fields."""
        if self.sia_code:
            return f"Code: {self.code}, Type: {self.sia_code.type}, \
            Description: {self.sia_code.description}"
        return f"Code: {self.code}"

    @property
    def valid_length(self) -> bool:
        """Check if the length of the message is the same in the message and supplied. Will not throw an error if not correct."""
        return int(self.length) == int(
            str(len(self.full_message)), 16
        )  # pragma: no cover

    @property
    def valid_message(self) -> bool:
        """Check the validity of the message by comparing the sent CRC with the calculated CRC."""
        return self.msg_crc == self.calc_crc

    @property
    def valid_timestamp(self) -> bool:
        """Check if the timestamp is within bounds."""
        if not self.sia_account:
            return True  # pragma: no cover
        if self.sia_account.allowed_timeband is None:
            return True  # pragma: no cover
        if self.timestamp:
            current_time = datetime.utcnow()
            current_min = current_time - timedelta(
                seconds=self.sia_account.allowed_timeband[0]
            )
            current_plus = current_time + timedelta(
                seconds=self.sia_account.allowed_timeband[1]
            )
            return current_min <= self.timestamp <= current_plus
        return True  # pragma: no cover

    def create_response(self) -> bytes:
        """Create a response message, based on account, event and response type.

        Returns:
            bytes -- Response to send back to sender.

        """
        response_type = self.response
        x_data = None
        if response_type is None:
            return b"\n\r"
        if not self.sia_account:
            return f'"{response_type.value}"'.encode("ascii")
        if (
            self.extended_data
            and self.extended_data.identifier == "K"
            and self.sia_account.key is not None
        ):
            x_data = f"[K{self.sia_account.key}]"
        if not self.encrypted or response_type == ResponseType.DUH:
            res = f'"{response_type.value}"{self.sequence}R{self.receiver}L{self.line}#{self.account}[]{x_data if x_data else ""}'
        else:
            encrypted_content = self.encrypt_content(
                f']{x_data if x_data else ""}{self._get_timestamp()}'
            )
            res = f'"*{response_type.value}"{self.sequence}R{self.receiver}L{self.line}#{self.account}[{encrypted_content}'
        header = ("%04x" % len(res)).upper()
        return f"\n{self._crc_calc(res)}{header}{res}\r".encode("ascii")

    def decrypt_content(self) -> None:
        """Decrypt the content, if the account is encrypted, otherwise pass back the event."""
        if not self.encrypted_content:  # pragma: no cover
            return None
        decr = self._get_crypter()
        if not decr:
            return None  # pragma: no cover
        self.content = decr.decrypt(bytes.fromhex(self.encrypted_content)).decode(
            "ascii", "ignore"
        )

    def encrypt_content(self, message: str) -> Optional[str]:
        """Encrypt a string.

        Arguments:
            message {str} -- String to encrypt.

        Returns:
            str -- Encrypted string, if encrypted account.

        """
        encr = self._get_crypter()
        if not encr:
            return None  # pragma: no cover
        fill_size = len(message) + 16 - len(message) % 16
        return encr.encrypt(message.zfill(fill_size).encode("ascii")).hex().upper()

    def parse_adm(self) -> None:
        """Parse the event qualifier and type for ADM messages."""
        if self.event_qualifier and self.event_type:  # pragma: no cover
            sub_map = _load_adm_mapping().get(self.event_type, None)
            if sub_map:
                self.code = sub_map.get(self.event_qualifier, None)

    def parse_content(self) -> None:
        """Set the internal content field and also parse the content and store the right things."""
        _LOGGER.warning("Message Type: %s", self.message_type)
        matcher = _get_matcher(self.message_type, self.encrypted)
        matches = matcher.match(self.content)
        if not matches:
            raise EventFormatError(
                "Parse content: no matches found in %s, using matcher: %s",
                self.content,
                matcher,
            )
        content = matches.groupdict()
        _LOGGER.debug("Content matches: %s", content)
        # Parse specific fields per message type
        if self.message_type == MessageTypes.ADMCID:
            self.partition = content["partition"]
            self.event_qualifier = content["event_qualifier"]
            self.event_type = content["event_type"]
        else:
            self.code = content["code"]
            self.ti = content["ti"]
            self.id = content["id"]
            self.message = content["message"]

        # Parse generic fields
        if not self.account:
            self.account = content["account"]
        self.ri = content["ri"]
        self.x_data = content["xdata"]
        if content["timestamp"]:
            try:
                ts = datetime.strptime(content["timestamp"], "%H:%M:%S,%m-%d-%Y")
                self.timestamp = ts
            except ValueError:
                _LOGGER.warning(
                    "Timestamp could not be parsed as a timestamp: %s",
                    content["timestamp"],
                )
        if self.message_type == MessageTypes.NULL and self.code_not_found:
            self.code = "RP"
            self.ri = "0"

    def parse_extended_data(self) -> None:
        """Set extended data."""
        if self.x_data is not None:  # pragma: no cover
            xdata = _load_xdata().get(self.x_data[0], None)
            if xdata:
                xdata.value = self.x_data[1:]
                self.extended_data = xdata

    def sia_account_from_message(self) -> SIAAccount:
        """Return the SIA Account, if there is not account added, create one based on the account in the message."""
        return SIAAccount(self.account)  # pragma: no cover

    def __str__(self) -> str:
        """Return the event as a string."""
        return f"\
Content: {self.content}, \
Zone (ri): {self.ri}, \
Code: {self.code}, \
Message: {self.message if self.message else ''}, \
Account: {self.account}, \
Receiver: {self.receiver}, \
Line: {self.line}, \
Timestamp: {self.timestamp}, \
Length: {self.length}, \
Sequence: {self.sequence}, \
CRC: {self.msg_crc}, \
Calc CRC: {self.calc_crc}, \
Encrypted Content: {self.encrypted_content}, \
Full Message: {self.full_message}."


@dataclass
class OHEvent(SIAEvent):
    """Class for OH events."""

    code: str = "RP"

    def __post_init__(self) -> None:
        """If there is a code, map it to the full SIA Code spec."""
        self.set_sia_code()

    @property
    def response(self) -> ResponseType:
        """Return ACK for OH Events."""
        return ResponseType.ACK  # pragma: no cover

    def create_response(self) -> bytes:
        """Create a response message, based on account, event and response type.

        Returns:
            str -- Response to send back to sender.

        """
        return '"ACK"'.encode("ascii")  # pragma: no cover


@dataclass
class NAKEvent(BaseEvent):
    """Class for NAK Events."""

    @property
    def response(self) -> ResponseType:
        """Return NAK for NAK Events."""
        return ResponseType.NAK

    def create_response(self) -> bytes:
        """Create a response message, based on account, event and response type.

        Returns:
            str -- Response to send back to sender.

        """
        res = f'"NAK"0000L0R0A0[]{self._get_timestamp()}'
        header = ("%04x" % len(res)).upper()
        return f"\n{self._crc_calc(res)}{header}{res}\r".encode("ascii")