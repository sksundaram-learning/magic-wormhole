from __future__ import print_function, unicode_literals
from collections import namedtuple
from attr import attrs, attrib
from attr.validators import optional, instance_of, provides
from automat import MethodicalMachine
from twisted.python import log
from twisted.internet.protocol import Protocol
from twisted.internet.interfaces import ITransport
from noise.exceptions import NoiseInvalidMessage
from .._interfaces import IDilationConnector
from ..observer import OneShotObserver
from .encode import to_be4, from_be4
from .roles import FOLLOWER

# InboundFraming is given data and returns Frames (Noise wire-side
# bytestrings). It handles the relay handshake and the prologue. The Frames it
# returns are either the ephemeral key (the Noise "handshake") or ciphertext
# messages.

# The next object up knows whether it's expecting a Handshake or a message. It
# feeds the first into Noise as a handshake, it feeds the rest into Noise as a
# message (which produces a plaintext stream). It emits tokens that are either
# "i've finished with the handshake (so you can send the KCM if you want)", or
# "here is a decrypted message (which might be the KCM)".

# the transmit direction goes directly to transport.write, and doesn't touch
# the state machine. we can do this because the way we encode/encrypt/frame
# things doesn't depend upon the receiver state. It would be more safe to e.g.
# prohibit sending ciphertext frames unless we're in the received-handshake
# state, but then we'll be in the middle of an inbound state transition ("we
# just received the handshake, so you can send a KCM now") when we perform an
# operation that depends upon the state (send_plaintext(kcm)), which is not a
# coherent/safe place to touch the state machine.

# we could set a flag and test it from inside send_plaintext, which kind of
# violates the state machine owning the state (ideally all "if" statements
# would be translated into same-input transitions from different starting
# states). For the specific question of sending plaintext frames, Noise will
# refuse us unless it's ready anyways, so the question is probably moot.

def first(l):
    return l[0]

class Disconnect(Exception):
    pass
RelayOK = namedtuple("RelayOk", [])
Prologue = namedtuple("Prologue", [])
Frame = namedtuple("Frame", ["frame"])

@attrs
class _Framer(object):
    _transport = attrib(validator=provides(ITransport))
    _outbound_prologue = attrib(validator=instance_of(bytes))
    _inbound_prologue = attrib(validator=instance_of(bytes))
    _buffer = b""
    _can_send_frames = False

    # in: use_relay
    # in: connectionMade, dataReceived
    # out: prologue_received, frame_received
    # out (shared): transport.loseConnection
    # out (shared): transport.write (relay handshake, prologue)
    # states: want_relay, want_prologue, want_frame
    m = MethodicalMachine()
    set_trace = getattr(m, "_setTrace", lambda self, f: None)

    @m.state()
    def want_relay(self): pass # pragma: no cover
    @m.state(initial=True)
    def want_prologue(self): pass # pragma: no cover
    @m.state()
    def want_frame(self): pass # pragma: no cover

    @m.input()
    def use_relay(self, relay_handshake): pass
    @m.input()
    def connectionMade(self): pass
    @m.input()
    def parse(self): pass
    @m.input()
    def got_relay_ok(self): pass
    @m.input()
    def got_prologue(self): pass

    @m.output()
    def store_relay_handshake(self, relay_handshake):
        self._outbound_relay_handshake = relay_handshake
        self._expected_relay_handshake = b"ok\n"
    @m.output()
    def send_relay_handshake(self):
        self._transport.write(self._outbound_relay_handshake)

    @m.output()
    def send_prologue(self):
        self._transport.write(self._outbound_prologue)

    @m.output()
    def parse_relay_ok(self):
        if self._get_expected("relay_ok", self._expected_relay_handshake):
            return RelayOK()

    @m.output()
    def parse_prologue(self):
        if self._get_expected("prologue", self._inbound_prologue):
            return Prologue()

    @m.output()
    def can_send_frames(self):
        self._can_send_frames = True # for assertion in send_frame()

    @m.output()
    def parse_frame(self):
        if len(self._buffer) < 4:
            return None
        frame_length = from_be4(self._buffer[0:4])
        if len(self._buffer) < frame_length:
            return None
        frame = self._buffer[4:4+frame_length]
        self._buffer = self._buffer[4+frame_length:] # TODO: avoid copy
        return Frame(frame=frame)

    want_prologue.upon(use_relay, outputs=[store_relay_handshake],
                       enter=want_relay)

    want_relay.upon(connectionMade, outputs=[send_relay_handshake],
                    enter=want_relay)
    want_relay.upon(parse, outputs=[parse_relay_ok], enter=want_relay,
                    collector=first)
    want_relay.upon(got_relay_ok, outputs=[send_prologue], enter=want_prologue)

    want_prologue.upon(connectionMade, outputs=[send_prologue],
                       enter=want_prologue)
    want_prologue.upon(parse, outputs=[parse_prologue], enter=want_prologue,
                       collector=first)
    want_prologue.upon(got_prologue, outputs=[can_send_frames], enter=want_frame)

    want_frame.upon(parse, outputs=[parse_frame], enter=want_frame,
                    collector=first)


    def _get_expected(self, name, expected):
        lb = len(self._buffer)
        le = len(expected)
        # if the buffer starts with the expected string, consume it and return
        # True
        if self._buffer.startswith(expected):
            self._buffer = self._buffer[le:]
            return True
        # the data we've received so far does not match the expected value, so
        # this can't possibly be right. Don't complain until we see the
        # expected length, or a newline, so we can capture the weird input in
        # the log for debugging.
        if self._buffer != expected[:lb]:
            if (b"\n" in self._buffer or lb >= le):
                log.msg("bad {} {}".format(name, self._buffer[:le]))
                raise Disconnect()
            return False # wait a bit longer
        # good so far, just waiting for the rest
        return False

    # external API is: connectionMade, add_and_parse, and send_frame

    def add_and_parse(self, data):
        # we can't make dataReceived an @m.input because we can't change the
        # state from within an input. Instead, let the state choose the parser
        # to use, and use the parsed token drive a state transition.
        self._buffer += data
        while True:
            # it'd be nice to use an iterator here, but since self.parse()
            # dispatches to a different parser (depending upon the current
            # state), we'd be using multiple iterators
            token = self.parse()
            if isinstance(token, RelayOK):
                self.got_relay_ok()
            elif isinstance(token, Prologue):
                self.got_prologue()
                yield token # triggers send_handshake
            elif isinstance(token, Frame):
                yield token
            else:
                break

    def send_frame(self, send, frame):
        assert self._can_send_frames
        self._transport.write(to_be4(len(frame)) + frame)

# RelayOK: Newline-terminated buddy-is-connected response from Relay.
#          First data received from relay.
# Prologue: double-newline-terminated this-is-really-wormhole response
#           from peer. First data received from peer.
# Frame: Either handshake or encrypted message. Length-prefixed on wire.
# Handshake: the Noise ephemeral key, first framed message
# Message: plaintext: encoded KCM/PING/PONG/OPEN/DATA/CLOSE/ACK
# KCM: Key Confirmation Message (encrypted b"\x00"). First frame
#      from peer. Sent immediately by Follower, after Selection by Leader.
# Record: namedtuple of KCM/Open/Data/Close/Ack/Ping/Pong

Handshake = namedtuple("Handshake", [])
# decrypted frames: produces KCM, Ping, Pong, Open, Data, Close, Ack
KCM = namedtuple("KCM", [])
Ping = namedtuple("Ping", ["ping_id"])
Pong = namedtuple("Pong", ["ping_id"])
Open = namedtuple("Open", ["seqnum", "scid"])
Data = namedtuple("Data", ["seqnum", "scid", "data"])
Close = namedtuple("Close", ["seqnum", "scid"])
Ack = namedtuple("Ack", ["resp_seqnum"])
Records = (KCM, Ping, Pong, Open, Data, Close, Ack)
Handshake_or_Records = (Handshake,) + Records

T_KCM = b"\x00"
T_PING = b"\x01"
T_PONG = b"\x02"
T_OPEN = b"\x03"
T_DATA = b"\x04"
T_CLOSE = b"\x05"
T_ACK = b"\x06"

def parse_record(plaintext):
    msgtype = plaintext[0:1]
    if msgtype == T_KCM:
        return KCM()
    if msgtype == T_PING:
        ping_id = plaintext[1:5]
        return Ping(ping_id)
    if msgtype == T_PONG:
        ping_id = plaintext[1:5]
        return Pong(ping_id)
    if msgtype == T_OPEN:
        scid = plaintext[1:5]
        seqnum = plaintext[5:9]
        return Open(seqnum, scid)
    if msgtype == T_DATA:
        scid = plaintext[1:5]
        seqnum = plaintext[5:9]
        data = plaintext[9:]
        return Data(seqnum, scid, data)
    if msgtype == T_CLOSE:
        scid = plaintext[1:5]
        seqnum = plaintext[5:9]
        return Close(seqnum, scid)
    if msgtype == T_ACK:
        resp_seqnum = plaintext[1:5]
        return Ack(resp_seqnum)
    log.err("received unknown message type {}".format(plaintext))
    # TODO: raise

@attrs
class _Record(object):
    _framer = attrib(validator=instance_of(_Framer))
    _noise = attrib()

    n = MethodicalMachine()
    # TODO: set_trace

    def __attrs_post_init__(self):
        self._noise.start_handshake()

    # in: role=
    # in: prologue_received, frame_received
    # out: handshake_received, record_received
    # out: transport.write (noise handshake, encrypted records)
    # states: want_prologue, want_handshake, want_record

    @n.state(initial=True)
    def want_prologue(self): pass # pragma: no cover
    @n.state()
    def want_handshake(self): pass # pragma: no cover
    @n.state()
    def want_message(self): pass # pragma: no cover

    @n.input()
    def got_prologue(self):
        pass
    @n.input()
    def got_frame(self, frame):
        pass

    @n.output()
    def send_handshake(self):
        handshake = self._noise.write_message() # generate the ephemeral key
        self._framer.send_frame(handshake)

    @n.output()
    def process_handshake(self, frame):
        try:
            payload = self._noise.read_message(frame)
            # Noise can include unencrypted data in the handshake, but we don't
            # use it
            del payload
        except NoiseInvalidMessage as e:
            log.err(e, "bad inbound noise handshake")
            raise Disconnect()
        return Handshake()

    @n.output()
    def decrypt_message(self, frame):
        try:
            message = self._noise.decrypt(frame)
        except NoiseInvalidMessage as e:
            # if this happens during tests, flunk the test
            log.err(e, "bad inbound noise frame")
            raise Disconnect()
        return parse_record(message)

    want_prologue.upon(got_prologue, outputs=[send_handshake],
                       enter=want_handshake)
    want_handshake.upon(got_frame, outputs=[process_handshake],
                        collector=first, enter=want_message)
    want_message.upon(got_frame, outputs=[decrypt_message],
                      collector=first, enter=want_message)

    # external API is: connectionMade, dataReceived, send_record

    def connectionMade(self):
        self._f.connectionMade()

    def dataReceived(self, data):
        for token in self._f.add_and_parse(data):
            if isinstance(token, Prologue):
                self.got_prologue() # triggers send_handshake
            else:
                assert isinstance(token, Frame)
                yield self.got_frame(token.frame) # Handshake or a Record type

    def send_record(self, r):
        message = encode_record(r)
        frame = self._noise.send(message)
        self._framer.send_frame(frame)

def encode_record(r):
    if isinstance(r, KCM):
        return b"\x00"
    if isinstance(r, Ping):
        return b"\x01" + r.ping_id
    if isinstance(r, Pong):
        return b"\x02" + r.ping_id
    if isinstance(r, Open):
        return b"\x03" + r.scid + r.seqnum
    if isinstance(r, Data):
        return b"\x04" + r.scid + r.seqnum + r.data
    if isinstance(r, Close):
        return b"\x05" + r.scid + r.seqnum
    if isinstance(r, Ack):
        return b"\x06" + r.resp_seqnum
    raise TypeError(r)


@attrs
class DilatedConnectionProtocol(Protocol):
    """I manage an L2 connection.

    When a new L2 connection is needed (as determined by the Leader),
    both Leader and Follower will initiate many simultaneous connections
    (probably TCP, but conceivably others). A subset will actually
    connect. A subset of those will successfully pass negotiation by
    exchanging handshakes to demonstrate knowledge of the session key.
    One of the negotiated connections will be selected by the Leader for
    active use, and the others will be dropped.

    At any given time, there is at most one active L2 connection.
    """

    _role = attrib()
    _connector = attrib(validator=provides(IDilationConnector))
    _noise = attrib()
    _outbound_prologue = attrib(validator=instance_of(bytes))
    _inbound_prologue = attrib(validator=instance_of(bytes))
    _use_relay = attrib(validator=instance_of(bytes))
    _relay_handshake = attrib(validator=optional(instance_of(bytes)))

    m = MethodicalMachine()
    set_trace = getattr(m, "_setTrace", lambda self, f: None)

    def __attrs_post_init__(self):
        self._manager = None # set if/when we are selected
        self._disconnected = OneShotObserver()
        self._can_send_records = False

    @m.state(initial=True)
    def unselected(self): pass # pragma: no cover
    @m.state()
    def selecting(self): pass # pragma: no cover
    @m.state()
    def selected(self): pass # pragma: no cover

    @m.input()
    def got_kcm(self):
        pass
    @m.input()
    def select(self, manager):
        pass
    @m.input()
    def got_record(self, record):
        pass

    @m.output()
    def add_candidate(self):
        self._connector.add_candidate(self)

    @m.output()
    def set_manager(self, manager):
        self._manager = manager

    @m.output()
    def can_send_records(self, manager):
        self._can_send_records = True

    @m.output()
    def deliver_record(self, record):
        self._manager.got_record(record)

    unselected.upon(got_kcm, outputs=[add_candidate], enter=selecting)
    selecting.upon(select, outputs=[set_manager, can_send_records], enter=selected)
    selected.upon(got_record, outputs=[deliver_record], enter=selected)

    # called by Connector

    def when_disconnected(self):
        return self._disconnected.when_fired()

    def disconnect(self):
        self.transport.loseConnection()

    @m.input()
    def select(self, manager):
        pass # fires set_manager()

    # called by Manager
    def send_record(self, record):
        assert self._can_send_records
        self._record.send_record(record)

    # IProtocol methods

    def connectionMade(self):
        framer = _Framer(self.transport,
                         self._outbound_prologue, self._inbound_prologue)
        if self._use_relay:
            framer.use_relay(self._relay_handshake)
        self._record = _Record(framer, self._noise)
        self._record.connectionMade()

    def dataReceived(self, data):
        try:
            for token in self._record.dataReceived(data):
                assert isinstance(token, Handshake_or_Records)
                if isinstance(token, Handshake):
                    if self._role is FOLLOWER:
                        self._record.send_record(KCM())
                elif isinstance(token, KCM):
                    # if we're the leader, add this connection as a candiate.
                    # if we're the follower, accept this connection.
                    self.got_kcm() # connector.add_candidate()
                else:
                    self.got_record(token) # manager.got_record()
        except Disconnect:
            self.loseConnection()

    def connectionLost(self, why=None):
        self._disconnected.fire(self)
