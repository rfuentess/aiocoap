# This file is part of the Python aiocoap library project.
#
# Copyright (c) 2012-2014 Maciej Wasilak <http://sixpinetrees.blogspot.com/>,
#               2013-2014 Christian Amsüss <c.amsuess@energyharvesting.at>
#
# aiocoap is free software, this file is published under the MIT license as
# described in the accompanying LICENSE file.

"""This module implements a TransportEndpoint for UDP based on the asyncio
DatagramProtocol.

As this makes use of RFC 3542 options (IPV6_PKTINFO), this is likely to only
work with IPv6 interfaces. Hybrid stacks are supported, though, so V4MAPPED
addresses (a la `::ffff:127.0.0.1`) will be used when name resolution shows
that a name is only available on V4."""

import asyncio
import urllib.parse
import socket
import ipaddress
import struct
from collections import namedtuple

from ..message import Message
from ..numbers import constants
from .. import error
from .. import interfaces
from ..numbers import COAP_PORT
from ..dump import TextDumper
from ..util.asyncio import RecvmsgDatagramProtocol
from ..util import hostportjoin
from ..util import socknumbers

from ..numbers import COAPS_PORT
from DTLSSocket import dtls

#DTLS Variables (FIXME Remove from aiocoap)
DTLS_EVENT_CONNECT = 0x01DC
DTLS_EVENT_CONNECTED = 0x01DE
DTLS_EVENT_RENEGOTIATE = 0x01DF
# from RFC 5246
LEVEL_WARNING = 1
LEVEL_FATAL = 2
CODE_CLOSE_NOTIFY = 0
LEVEL_NOALERT = 0 # seems only to be issued by tinydtls-internal events

class UDP6EndpointAddress:
    """Remote address type for :cls:`TransportEndpointUDP6`. Remote address is
    stored in form of a socket address; local address can be roundtripped by
    opaque pktinfo data.

    >>> local = UDP6EndpointAddress(socket.getaddrinfo('127.0.0.1', 5683, type=socket.SOCK_DGRAM, family=socket.AF_INET6, flags=socket.AI_V4MAPPED)[0][-1])
    >>> local.is_multicast
    False
    >>> local.hostinfo
    '127.0.0.1'
    >>> all_coap_site = UDP6EndpointAddress(socket.getaddrinfo('ff05:0:0:0:0:0:0:fd', 1234, type=socket.SOCK_DGRAM, family=socket.AF_INET6)[0][-1])
    >>> all_coap_site.is_multicast
    True
    >>> all_coap_site.hostinfo
    '[ff05::fd]:1234'
    >>> all_coap4 = UDP6EndpointAddress(socket.getaddrinfo('224.0.1.187', 5683, type=socket.SOCK_DGRAM, family=socket.AF_INET6, flags=socket.AI_V4MAPPED)[0][-1])
    >>> all_coap4.is_multicast
    True
    """

    # interface work in progress. chances are those should be immutable or at
    # least hashable, as they'll be frequently used as dict keys.
    def __init__(self, sockaddr, *, pktinfo=None):
        self.sockaddr = sockaddr
        self.pktinfo = pktinfo

    def __hash__(self):
        return hash(self.sockaddr)

    def __eq__(self, other):
        return self.sockaddr == other.sockaddr

    def __repr__(self):
        return "<%s [%s]:%d%s>"%(type(self).__name__, self.sockaddr[0], self.sockaddr[1], " with local address" if self.pktinfo is not None else "")

    @staticmethod
    def _strip_v4mapped(address):
        if address.startswith('::ffff:') and '.' in address:
            return address[7:]
        return address

    def _plainaddress(self):
        """Return the IP adress part of the sockaddr in IPv4 notation if it is
        mapped, otherwise the plain v6 address including the interface
        identifier if set."""

        return self._strip_v4mapped(self.sockaddr[0])

    def _plainaddress_local(self):
        """Like _plainaddress, but on the address in the pktinfo. Unlike
        _plainaddress, this does not contain the interface identifier."""

        addr, interface = struct.Struct("16si").unpack_from(self.pktinfo)

        return self._strip_v4mapped(socket.inet_ntop(socket.AF_INET6, addr))

    @property
    def hostinfo(self):
        port = self.sockaddr[1]
        if port == COAP_PORT:
            port = None

        # plainaddress: don't assume other applications can deal with v4mapped addresses
        return hostportjoin(self._plainaddress(), port)

    @property
    def uri(self):
        return 'coap://' + self.hostinfo

    # those are currently the inofficial metadata interface
    port = property(lambda self: self.sockaddr[1])

    @property
    def is_multicast(self):
        return ipaddress.ip_address(self._plainaddress().split('%', 1)[0]).is_multicast

    @property
    def is_multicast_locally(self):
        return ipaddress.ip_address(self._plainaddress_local()).is_multicast


class SockExtendedErr(namedtuple("_SockExtendedErr", "ee_errno ee_origin ee_type ee_code ee_pad ee_info ee_data")):
    _struct = struct.Struct("IbbbbII")
    @classmethod
    def load(cls, data):
        # unpack_from: recvmsg(2) says that more data may follow
        return cls(*cls._struct.unpack_from(data))

class DTLSSecurityStore:
    def _get_psk(self, host, port):
        return b"Client_identity", b"secretPSK"

class TransportEndpointUDP6(RecvmsgDatagramProtocol, interfaces.TransportEndpoint):
    def __init__(self, new_message_callback, new_error_callback, log, loop):
        self.new_message_callback = new_message_callback
        self.new_error_callback = new_error_callback
        self.log = log
        self.loop = loop

        self._shutting_down = None #: Future created and used in the .shutdown() method.

        self.security = DTLSSecurityStore()
        self._connecting = False

        pskId, psk = self.security._get_psk("::1", COAPS_PORT) # FIXME

        # dtls.setLogLevel(dtls.DTLS_LOG_DEBUG)
        dtls.setLogLevel(dtls.DTLS_LOG_INFO)
        # print("TinyDTLS Log level ",dtls.dtlsGetLogLevel() )
        self._dtls_socket = dtls.DTLS(
                read=self._dtls_read,   # FIXME
                write=self._dtls_write, # FIXME
                event=self._dtls_event, # FIXME
                # event= None,
                pskId=pskId,
                pskStore={pskId: psk},
                )

        self.ready = asyncio.Future() #: Future that gets fullfilled by connection_made (ie. don't send before this is done; handled by ``create_..._context``

    # FIXME(rfuentess) Releasing memory is requried
    def __del__(self):
        try:
            self._dtls_socket.dtls_free_context()
        except:
            return

    def _dtls_event(self, level, code):
        # print(" _dtls_event")
        if (level, code) == (LEVEL_NOALERT, DTLS_EVENT_CONNECT):
            print("DTLS-DEBUG: Client Connect")
            self._connecting = False
            return
        elif (level, code) == (LEVEL_NOALERT, DTLS_EVENT_CONNECTED):
            print("DTLS-DEBUG: Client handshake finished!")
            self._connecting = True
        elif (level, code) == (LEVEL_FATAL, CODE_CLOSE_NOTIFY):
            # FIXME how to shut down?
            # FIXME(rfuentess) You can't. tinyDTLs should not send DTLS alerts.
            pass
        elif level == LEVEL_FATAL:
            # FIXME how to shut down?
            self.log.error("Fatal DTLS error: code %d", code)
        else:
            self.log.warning("Unhandled alert level %d code %d", level, code)

    def _dtls_write(self, recipient, data):
        # print(" _dtls_write")

        sock = self.transport._sock
        # ancdata = []
        # ancdata.append((socket.IPPROTO_IPV6, socket.IPV6_PKTINFO,
        #                 message.remote.pktinfo))
        try:
            # self.send(data)
            sock.sendto(data, recipient)
        except:
        #     # tinydtls sends callbacks very very late during shutdown (ie.
        #     # `hasattr` and `AttributeError` are all not available any more,
        #     # and even if the DTLSClientConnection class had a ._transport, it
        #     # would already be gone), and it seems even a __del__ doesn't help
        #     # break things up into the proper sequence.
            return 0

        return len(data)

    def _dtls_read(self, sender, data):
        # ignoring sender: it's only _SENTINEL_*
        # FIXME Previous lien. If Peer is find ,we send otherwise nothing.
        # print(" _dtls_read ")

        self.log.info("potentially CoAP msg Received")

        try:
            pktinfo=None
            sock = self.transport._sock
            message = Message.decode(data, UDP6EndpointAddress(sender, pktinfo=pktinfo))
            # message = Message.decode(data, sock)
        except error.UnparsableMessage:
            self.log.warning("Ignoring unparsable message from %s"%(sender,))
            return

        self.new_message_callback(message)

        return len(data)

    @classmethod
    @asyncio.coroutine
    def _create_transport_endpoint(cls, new_message_callback, new_error_callback, log, loop, dump_to, bind, multicast=False):
        protofact = lambda: cls(new_message_callback=new_message_callback, new_error_callback=new_error_callback, log=log, loop=loop)
        if dump_to is not None:
            protofact = TextDumper.endpointfactory(open(dump_to, 'w'), protofact)

        transport, protocol = yield from loop.create_datagram_endpoint(protofact, family=socket.AF_INET6)

        sock = transport._sock

        if multicast:
            # FIXME this all registers only for one interface, doesn't it?
            s = struct.pack('4s4si',
                    socket.inet_aton(constants.MCAST_IPV4_ALLCOAPNODES),
                    socket.inet_aton("0.0.0.0"), 0)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, s)
            for a in constants.MCAST_IPV6_ALL:
                s = struct.pack('16si',
                        socket.inet_pton(socket.AF_INET6, a),
                        0)
                sock.setsockopt(socket.IPPROTO_IPV6,
                        socket.IPV6_JOIN_GROUP, s)

        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_RECVPKTINFO, 1)
        sock.setsockopt(socket.IPPROTO_IPV6, socknumbers.IPV6_RECVERR, 1)
        # i'm curious why this is required; didn't IPV6_V6ONLY=0 already make
        # it clear that i don't care about the ip version as long as everything looks the same?
        sock.setsockopt(socket.IPPROTO_IP, socknumbers.IP_RECVERR, 1)

        if bind is not None:
            # FIXME: SO_REUSEPORT should be safer when available (no port hijacking), and the test suite should work with it just as well (even without). why doesn't it?
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(bind)

        if dump_to is not None:
            protocol = protocol.protocol

        yield from protocol.ready

        return protocol

    @classmethod
    @asyncio.coroutine
    def create_client_transport_endpoint(cls, new_message_callback, new_error_callback, log, loop, dump_to):
        return (yield from cls._create_transport_endpoint(new_message_callback, new_error_callback, log, loop, dump_to, None, multicast=False))

    @classmethod
    @asyncio.coroutine
    def create_server_transport_endpoint(cls, new_message_callback, new_error_callback, log, loop, dump_to, bind):
        return (yield from cls._create_transport_endpoint(new_message_callback, new_error_callback, log, loop, dump_to, bind, multicast=True))

    @asyncio.coroutine
    def shutdown(self):
        self._shutting_down = asyncio.Future()

        self.transport.close()

        yield from self._shutting_down

        del self.new_message_callback
        del self.new_error_callback

    def send(self, message):
        ancdata = []
        if message.remote.pktinfo is not None:
            if message.remote.is_multicast_locally:
                # this is kind of a last-resort location; the `response.remote
                # = request.remote` places should better consider this.
                self.log.warn("Dropping pktinfo from ancdata because it" \
                        " indicates a multicast address")
            else:
                ancdata.append((socket.IPPROTO_IPV6, socket.IPV6_PKTINFO,
                    message.remote.pktinfo))
        # self.transport.sendmsg(message.encode(), ancdata, 0, message.remote.sockaddr)

        self._dtls_socket.write(self._dtls_session,message.encode() )
        # self._dtls_write(message.remote.sockaddr, message.encode())

    @asyncio.coroutine
    def determine_remote(self, request):

        if request.requested_scheme not in ('coap', None):
            return None

        ## @TODO this is very rudimentary; happy-eyeballs or
        # similar could be employed.

        if request.unresolved_remote is not None:
            pseudoparsed = urllib.parse.SplitResult(None, request.unresolved_remote, None, None, None)
            host = pseudoparsed.hostname
            port = pseudoparsed.port or COAP_PORT
        elif request.opt.uri_host:
            host = request.opt.uri_host
            port = request.opt.uri_port or COAP_PORT
        else:
            raise ValueError("No location found to send message to (neither in .opt.uri_host nor in .remote)")

        addrinfo = yield from self.loop.getaddrinfo(
            host,
            port,
            family=self.transport._sock.family,
            type=0,
            proto=self.transport._sock.proto,
            flags=socket.AI_V4MAPPED,
            )
        return UDP6EndpointAddress(addrinfo[0][-1])

    #
    # implementing the typical DatagramProtocol interfaces.
    #
    # note from the documentation: we may rely on connection_made to be called
    # before datagram_received -- but sending immediately after context
    # creation will still fail

    def connection_made(self, transport):
        """Implementation of the DatagramProtocol interface, called by the transport."""
        # print(" UDP connection_made")
        self.ready.set_result(True)
        self.transport = transport

    def datagram_msg_received(self, data, ancdata, flags, address):
        """Implementation of the RecvmsgDatagramProtocol interface, called by the transport."""
        print(" UDP (DTLS?) datagram_msg_received")
        pktinfo = None

        self._dtls_session = dtls.Session(address[0], address[1])
        try:
            # print
            # _, _, data2, data2leng = self._dtls_socket.handleMessage(self._dtls_session, data)
            self._dtls_socket.handleMessage(self._dtls_session, data)
        # except (RuntimeError, TypeError, NameError):
        #     # NOTE Don-t care about normal CoAP for now...
        #     print( "\tCHANGOS! ", RuntimeError, TypeError, NameError)
        #     return
        except Exception as ex:
            template = "An exception of type {0} occurred. Arguments:\n{1!r}"
            message = template.format(type(ex).__name__, ex.args)
            print (message)

        # if not self._connecting:
        #     return

        return


    def datagram_errqueue_received(self, data, ancdata, flags, address):
        assert flags == socket.MSG_ERRQUEUE
        pktinfo = None
        errno = None
        for cmsg_level, cmsg_type, cmsg_data in ancdata:
            assert cmsg_level == socket.IPPROTO_IPV6
            if cmsg_type == socknumbers.IPV6_RECVERR:
                errno = SockExtendedErr.load(cmsg_data).ee_errno
            elif cmsg_level == socket.IPPROTO_IPV6 and cmsg_type == socknumbers.IPV6_PKTINFO:
                pktinfo = cmsg_data
            else:
                self.log.info("Received unexpected ancillary data to recvmsg errqueue: level %d, type %d, data %r", cmsg_level, cmsg_type, cmsg_data)
        remote = UDP6EndpointAddress(address, pktinfo=pktinfo)

        # not trying to decode a message from data -- that works for
        # "connection refused", doesn't work for "no route to host", and
        # anyway, when an icmp error comes back, everything pending from that
        # port should err out.

        self.new_error_callback(errno, remote)

    def error_received(self, exc):
        """Implementation of the DatagramProtocol interface, called by the transport."""
        # TODO: what can we do about errors we *only* receive here? (eg. sending to 127.0.0.0)
        self.log.error("Error received and ignored in this codepath: %s"%exc)

    def connection_lost(self, exc):
        # TODO better error handling -- find out what can cause this at all
        # except for a shutdown
        print(" UDP connection_lost")
        if exc is not None:
            self.log.error("Connection lost: %s"%exc)

        if self._shutting_down is None:
            self.log.error("Connection loss was not expected.")
        else:
            self._shutting_down.set_result(None)
