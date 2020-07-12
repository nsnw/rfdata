#!/usr/bin/env python

# RFDATA service
# https://aprs.rfdata.org/
# (c) 2020 Andy Smith, VE6LY <andy@nsnw.ca>
# Released under the MIT License

# Imports
import asyncio
import aiohttp
import logging
import re
import json
import click
import yaml
import sys
import maidenhead
from aprspy import APRS
from aprspy.packets.position import PositionPacket
from aprspy.packets.message import MessagePacket
from aprspy.exceptions import ParseError, UnsupportedError
from asgiref.sync import sync_to_async
from datetime import datetime
from textwrap import wrap
from rfdata.qrz import QRZ
from rfdata.aprsfi import APRSFI
from rfdata.checkwx import CheckWX
from rfdata.darksky import DarkSky
from rfdata.dxwatch import DXWatch
from rfdata.sun import Sun
from rfdata.pskreporter import PSKReporter

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    filename="aprs-service.log",
    format='%(asctime)s %(levelname)-8s %(name)-12s/%(funcName)-16s\n-> %(message)s'
)
logger = logging.getLogger('aprs-service')

# Import Django and models used for backend storage
import django #noqa
django.setup()
from data.models import Station, Chat, ChatMessage, ChatSubscription, Command


class APRSClient(asyncio.Protocol):
    """
    APRS client protocol handler
    """

    aprsfi = None
    checkwx = None
    darksky = None

    def __init__(self, *args, loop=None, callsign=None, ssid=None, passcode=None, filter=None,
                 **kwargs):
        if not loop:
            loop = asyncio.get_event_loop()

        self._args = args
        self._kwargs = kwargs
        self._loop = loop
        self._callsign = callsign
        self._ssid = ssid

        # If an SSID is given, append it to the source
        if self._ssid:
            self._source = self._callsign + "-" + self._ssid
        else:
            self._source = self._callsign

        self._passcode = passcode
        self._filter = filter
        self._stopping = False
        self._connector = None
        self._transport = None
        self._login_attempted = False
        self._logged_in = False
        self._data_queue = asyncio.Queue()
        self._send_queue = asyncio.Queue()
        self._buffer = None

        # Build pounce list
        self._pounce_list = [
            s.station for s in Station.objects.filter(pounce=True).filter(mailbox__unread=True)
        ]

        # Start up the main processing task
        self._loop.create_task(self.process_data())

    def connection_made(self, transport):
        """Called when a connection is made to the APRS-IS server."""
        logger.info("Connection made!")
        self._transport = transport

    def data_received(self, data):
        """Called when data is received from the APRS-IS server."""

        # For incoming data, we buffer it and then split it based on newlines.
        # Usually incoming data consists of complete lines, but if there's a lot of data some
        # might be split in the middle - so this handles that
        decoded_data = data.decode('UTF-8', 'replace')
        logger.debug(decoded_data)

        if self._buffer:
            # There's data already in the buffer, so prepend it to the new data
            decoded_data = self._buffer + decoded_data
            self._buffer = None
            logger.debug("Cleared buffer")

        # Split at newlines
        lines = decoded_data.split('\r\n')

        if decoded_data[-1] != "\n":
            # Last character of the incoming data isn't a newline, so buffer it
            logger.debug("Adding partial packet to buffer")
            self._buffer = lines.pop()

        for line in lines:
            # For each line, put it on the queue to be processed
            if len(line) > 0:
                self._data_queue.put_nowait(line)

    async def process_data(self):
        """Process incoming data."""

        # Process incoming data
        while not self._stopping:
            logger.debug("Items in data queue: {}".format(self._data_queue.qsize()))

            # Get a line from the data queue
            item = await self._data_queue.get()

            if item is None:
                # No data waiting, sleep momentarily
                logger.debug("No data in queue.")
                await asyncio.sleep(0.1)

            logger.debug("Got from data queue: {}".format(item))
            try:
                # Decode as UTF-8, strip the newline
                decoded_data = item

                # Lines beginning with a '#' are status lines from the APRS-IS server
                if re.match('^#', decoded_data):
                    self.parse_server(decoded_data)
                else:
                    # This is a packet, so decode it
                    await self.parse_packet(decoded_data)

            except Exception as e:
                # Something went wrong while parsing
                logger.error(e)
                logger.error("Failed to parse message from APRS-IS: {}".format(decoded_data))
                self._data_queue.task_done()

        logger.info("Stopping data handler...")

    async def parse_packet(self, line):
        """Parse incoming packets."""

        logger.debug("Got packet: {}".format(line))
        try:
            # Parse the packet
            packet = APRS.parse(line)

            # We use position packets to trigger pounce notifications, if the station has them
            # enabled
            if issubclass(type(packet), PositionPacket):
                if packet.source in self._pounce_list:
                    logger.info("{} is on the pounce list".format(packet.source))

                    # Send the number of messages waiting to the station
                    await self.handle_list_messages(packet.source)

            # Check for message packets
            elif type(packet) is MessagePacket and packet.addressee == self._source:

                # This message is addressed to us
                logger.info("Message for us: {}".format(packet.raw))

                # Log the message
                await self.log_command(packet.source, packet.message)

                # If the message has a message ID, cknowledge it
                if packet.message_id:
                    self.send_message(
                        packet.source, "ack{}".format(packet.message_id), log=False
                    )

                # Convert to lower case for the command
                parts = packet.message.lower().split(' ')
                logger.info(parts)

                if len(parts) >= 1:
                    cmd = parts[0]

                else:
                    cmd = None

                # Check what the command is
                if cmd == "?APRS?" or cmd == "?help" or cmd == "?he" or cmd == "?h":
                    # Respond to help
                    self.send_message(
                        packet.source,
                        "L R S D E Y C J P PN T A M MH MT W WX Q QRZ DX SOL"
                    )

                    self.send_message(
                        packet.source,
                        "Send ?<cmd> for help"
                    )

                    self.send_message(
                        packet.source, "Visit https://aprs.rfdata.org/ for more information!"
                    )

                elif cmd == "?aprst" or cmd == "?ping?":
                    # Return the path the message took to us
                    logger.info("{} requested {}".format(packet.source, cmd.upper()))
                    self.send_message(packet.source, packet.path.path)

                elif cmd == "?aprsv":
                    # Return the current version
                    logger.info("{} requested {}".format(packet.source, cmd.upper()))
                    self.send_message(
                        packet.source, "aprs-service v0.1 by Andy Smith VE6LY andy@nsnw.ca"
                    )

                # Prefixing commands with ? sends help for that command
                elif cmd == "?l":
                    # List number of waiting messages
                    self.send_message(
                        packet.source, "L: List messages waiting for your callsign-ssid"
                    )

                elif cmd == "?r":
                    # Read a message
                    self.send_message(
                        packet.source, "R <number>: Read message <number> for your callsign-ssid"
                    )

                elif cmd == "?d":
                    # Delete a message
                    self.send_message(
                        packet.source, "D <number>: Delete message <number> for your callsign-ssid"
                    )

                elif cmd == "?e":
                    # Delete all messages
                    self.send_message(
                        packet.source, "E: Empty the mailbox for your callsign-ssid"
                    )

                elif cmd == "?i":
                    # Show message info
                    self.send_message(
                        packet.source, "I <number>: Show info for message <number>"
                    )

                elif cmd == "?s":
                    # Send a message
                    self.send_message(
                        packet.source, "S <callsign> <message>: Send <message> to <callsign>"
                    )

                elif cmd == "?c":
                    # Create a chat
                    self.send_message(
                        packet.source, "C <name>: Create chat <name>"
                    )

                elif cmd == "?j":
                    # Join a chat
                    self.send_message(
                        packet.source, "J <name>: Join chat <name>"
                    )

                elif cmd == "?p":
                    # Leave (part) a chat
                    self.send_message(
                        packet.source, "P <name>: Leave (part) chat <name>"
                    )

                elif cmd == "?pn":
                    # Toggle pounce mode
                    self.send_message(
                        packet.source, "PN: Toggle notifications for messages"
                    )

                elif cmd == "?t":
                    # Send a message to a chat
                    self.send_message(
                        packet.source, "T <name> <message>: Send <message> to chat <name>"
                    )

                elif cmd == "?m":
                    # Show which chats the station is a member of
                    self.send_message(
                        packet.source, "M: Show chats your callsign-ssid is a member of"
                    )

                elif cmd == "?mh":
                    # Show current Maidenhead grid square
                    self.send_message(
                        packet.source, "MH: Show your current Maidenhead grid square"
                    )

                elif cmd == "?mt":
                    # Show METAR data for an airport
                    self.send_message(
                        packet.source, "MT <airport>: METAR for airport <airport>"
                    )

                elif cmd == "?a":
                    # Set topic for a chat
                    self.send_message(
                        packet.source, "A <chat> <topic>: Set the topic for a chat you own"
                    )

                elif cmd == "?qrz":
                    # QRZ lookup
                    self.send_message(
                        packet.source, "QRZ <callsign>: Look up callsign in QRZ"
                    )

                elif cmd == "?dx":
                    # DX cluster lookup
                    self.send_message(
                        packet.source, "DX <callsign>: Look up callsign on DX cluster"
                    )

                elif cmd == "?sol":
                    # Solar data lookup
                    self.send_message(
                        packet.source, "SOL <callsign>: Look up solar data"
                    )

                elif cmd == "?sun":
                    # Solar data lookup
                    self.send_message(
                        packet.source, "SUN: Sunrise/sunset times for your location"
                    )

                elif cmd == "?pskr":
                    # Solar data lookup
                    self.send_message(
                        packet.source, "PSKR: Get best freqs for your grid from pskreporter.info"
                    )

                elif cmd == "?q":
                    # APRS position lookup
                    self.send_message(
                        packet.source, "Q <callsign>: Look up APRS position for callsign"
                    )

                elif cmd == "?wx":
                    # Weather lookup
                    self.send_message(
                        packet.source, "WX: Get weather at your current location"
                    )

                elif cmd == "qrz":
                    # Handle QRZ lookups
                    await self.handle_qrz(packet.source, parts)

                elif cmd == "s":
                    # Handle sending messages
                    await self.handle_send_message(packet)

                elif cmd == "l":
                    # Handle listing waiting messages
                    await self.handle_list_messages(packet.source)

                elif cmd == "r":
                    # Handle reading messages
                    await self.handle_read_message(packet.source, parts)

                elif cmd == "y":
                    # Handle replying to messages
                    await self.handle_reply_message(packet)

                elif cmd == "i":
                    # Handle message info
                    await self.handle_info_message(packet.source, parts)

                elif cmd == "d":
                    # Handle deleting messages
                    await self.handle_delete_message(packet.source, parts)

                elif cmd == "e":
                    # Handle deleting all messages
                    await self.handle_delete_all_messages(packet.source)

                elif cmd == "c":
                    # Handle creating chats
                    await self.handle_create_chat(packet.source, parts)

                elif cmd == "j":
                    # Handle joining chats
                    await self.handle_join_chat(packet.source, parts)

                elif cmd == "p":
                    # Handle leaving chats
                    await self.handle_leave_chat(packet.source, parts)

                elif cmd == "pn":
                    # Handle pounce mode
                    await self.handle_pounce(packet.source, parts)

                elif cmd == "w":
                    # Handle showing chat membership
                    await self.handle_show_all_chats(packet.source)

                elif cmd == "t" or cmd[0] == ".":
                    # Handle chat messages
                    await self.handle_chat_message(packet)

                elif cmd == "a":
                    # Handle chat topic
                    await self.handle_chat_topic(packet)

                elif cmd == "m":
                    # Handle showing all chats
                    chats = await self.list_chats(packet.source)

                    self.send_message(packet.source, "Chats: {}".format(" ".join(chats)))

                elif cmd == "dx":
                    # Handle DX cluster lookup
                    await self.handle_dx(packet.source, parts)

                elif cmd == "sol" or cmd == "solar":
                    # Handle solar data
                    await self.handle_solar(packet.source)

                elif cmd == "sun":
                    # Handle sunrise/sunset
                    await self.handle_sunrise_sunset(packet.source)

                elif cmd == "pskr":
                    # Handle pskreporter best frequencies
                    await self.handle_pskr_freq(packet.source)

                elif cmd == "q" or cmd == "seen":
                    # Handle APRS position lookup
                    await self.handle_seen(packet.source, parts)

                elif cmd == "mh" or cmd == "grid":
                    # Handle Maidenhead grid square lookup
                    await self.handle_mh(packet.source)

                elif cmd == "mt" or cmd == "metar":
                    # Handle METAR lookup
                    await self.handle_metar(packet.source, parts)

                elif cmd == "wx" or cmd == "weather":
                    # Handle weather lookup
                    await self.handle_wx(packet.source)

        except ParseError:
            logger.debug("Failed to parse packet: {}".format(line))

        except UnsupportedError:
            logger.debug("Unsupported packet: {}".format(line))

    async def handle_qrz(self, source, args):
        """Handle QRZ lookups."""

        # If no callsign is given, send help
        if len(args) < 2:
            self.send_message(
                source, "Usage: QRZ <callsign>"
            )

        else:
            logger.info("{} querying QRZ for {}".format(
                source, args[1].upper()
            ))

            # Query QRZ
            response = await self.get_qrz(args[1])

            # Send response
            self.send_message(
                source, response
            )

    async def handle_dx(self, source, args):
        """Handle DX cluster lookups."""

        # If no callsign is given, send help
        if len(args) < 2:
            self.send_message(
                source, "Usage: DX <callsign>"
            )

        else:
            logger.info("{} querying DX cluster for {}".format(
                source, args[1].upper()
            ))

            # Uppercase the callsign, and query the DX cluster
            callsign = args[1].upper()
            message = await self.get_dx(callsign)

            # Send response
            self.send_message(
                source, message
            )

    async def handle_solar(self, source):
        """Handle solar data lookup."""

        logger.info("{} querying for solar data".format(
            source
        ))

        # Get solar data
        response = await self.get_solar()

        try:
            # Format the response
            data = json.loads(response)

            message = "F:{} A:{} K:{} S:{} @{}".format(
                data['flux'], data['a'], data['k'], data['ssn'], data['date']
            )

        except Exception as e:
            logger.error(e)
            message = "Could not query solar data"

        # Send response
        self.send_message(
            source, message
        )

    async def handle_seen(self, source, args):
        """Handle APRS position lookup."""

        # If no callsign is given, send help
        if len(args) < 2:
            self.send_message(
                source, "Usage: Q <callsign>"
            )

        else:
            logger.info("{} querying aprs.fi for {}".format(
                source, args[1].upper()
            ))

            # Uppercase the callsign, and query aprs.fi
            callsign = args[1].upper()
            response = await self.aprsfi.station(callsign)

            if response:
                # Format response
                timestamp = datetime.fromtimestamp(int(response['lasttime']))

                message = "{}: {},{} @{}: {}".format(
                    callsign,
                    round(float(response['lat']), 2),
                    round(float(response['lng']), 2),
                    timestamp,
                    response['comment'] if 'comment' in response else ""
                )

                # Send response
                self.send_message(
                    source, message
                )

            else:
                self.send_message(
                    source, "Could not find {}".format(callsign)
                )

    async def handle_metar(self, source, args):
        """Handle METAR lookups."""

        # If no airport is given, send help
        if len(args) < 2:
            self.send_message(
                source, "Usage: MT <ICAO code>"
            )

        else:
            logger.info("{} querying CheckWX for {}".format(
                source, args[1].upper()
            ))

            # Uppercase the airport code and query CheckWX
            icao = args[1].upper()
            response = await self.checkwx.metar(icao)

            if response:
                # Send response
                self.send_message(
                    source, response
                )

            else:
                self.send_message(
                    source, "Could not find {}".format(icao)
                )

    async def handle_mh(self, source):
        """Handle Maidenhead grid square lookup."""

        logger.info("Querying aprs.fi for {}".format(
            source
        ))

        # Query aprs.fi for current position
        response = await self.aprsfi.station(source)

        if response:
            # Format response
            timestamp = datetime.fromtimestamp(int(response['lasttime']))

            locator = maidenhead.toMaiden(
                float(response['lat']), float(response['lng'])
            )

            message = "{} ({}, {}) @ {}".format(
                locator,
                round(float(response['lat']), 2),
                round(float(response['lng']), 2),
                timestamp
            )

            # Send response
            self.send_message(
                source, message
            )

        else:
            self.send_message(
                source, "Could not find {}".format(source)
            )

    async def handle_wx(self, source):
        logger.info("Querying aprs.fi for {}".format(
            source
        ))

        # Query aprs.fi for current position
        response = await self.aprsfi.station(source)

        if response:
            # Query Darksky for weather
            wx = await self.darksky.wx(response['lat'], response['lng'])

            # Send response
            self.send_message(
                source, wx
            )

        else:
            self.send_message(
                source, "Could not find {}".format(source)
            )

    async def handle_sunrise_sunset(self, source):
        logger.info("Querying aprs.fi for {}".format(
            source
        ))

        # Query aprs.fi for current position
        response = await self.aprsfi.station(source)

        if response:
            # Query for sunrise/sunset
            sunrise_sunset = await self.sun.sunrise_sunset(response['lat'], response['lng'])

            # Send response
            self.send_message(
                source, sunrise_sunset
            )

        else:
            self.send_message(
                source, "Could not find {}".format(source)
            )

    async def handle_pskr_freq(self, source):
        logger.info("Querying aprs.fi for {}".format(
            source
        ))

        # Query aprs.fi for current position
        response = await self.aprsfi.station(source)

        if response:
            # Query for sunrise/sunset
            best_freqs = await self.pskr.psk_freq(response['lat'], response['lng'])

            # Send response
            self.send_message(
                source, best_freqs
            )

        else:
            self.send_message(
                source, "Could not find {}".format(source)
            )

    async def handle_send_message(self, packet):
        """Handle sending a message to another station."""

        # If there's not enough arguments, send help
        if len(packet.message.split(' ')) < 3:
            self.send_message(
                packet.source, "Usage: S <callsign> <message>"
            )

        else:
            # Split the arguments, get the addressee and the message
            args = packet.message.split(' ')
            addressee = args[1].upper()
            message = " ".join(args[2:])

            # Put the message in the addressee's mailbox
            sent = await self.add_mailbox_message(
                packet.source, addressee, message
            )

            if sent:
                self.send_message(packet.source, "Message to {} sent".format(
                    addressee
                ))

    async def handle_reply_message(self, packet):
        """Handle replying to a message."""

        # Split the parts of the message
        parts = packet.message.split(' ')

        # If there's not enough arguments, or the message number isn't a number, send help
        if len(parts) < 3:
            self.send_message(
                packet.source, "Usage: Y <message number> <message>"
            )

        elif not re.match("[0-9]+", parts[1]):
            self.send_message(
                packet.source, "Usage: Y <message number> <message>"
            )

        else:
            # Get the message number and the reply
            number = int(parts[1])
            reply = " ".join(parts[2:])

            # Get the message by the given message number
            message = await self.get_message_by_number(packet.source, number)

            if message:
                # Get the addressee from the message
                addressee = message.source.station

                # Put the message in the addressee's mailbox
                sent = await self.add_mailbox_message(
                    packet.source, addressee, reply
                )

                if sent:
                    self.send_message(packet.source, "Reply to {} sent".format(
                        addressee
                    ))

            else:
                # No matching message number found
                self.send_message(packet.source, "Reply #{} not found".format(
                    number
                ))

    async def handle_read_message(self, source, args):
        """Handle reading messages."""

        # If there's not enough arguments, or if the message number is not a number, send help
        if len(args) < 2:
            self.send_message(
                source, "Usage: R <message number>"
            )

        elif not re.match("[0-9]+", args[1]):
            self.send_message(
                source, "Usage: R <message number>"
            )

        else:
            # Get the message
            number = int(args[1])
            message = await self.get_message_by_number(source, number)

            if message:
                # Send response
                self.send_message(source, "{}:{}".format(
                    message.source.station, message.message
                ))

            else:
                # No matching message number found
                self.send_message(source, "Message #{} not found".format(
                    number
                ))

    async def handle_list_messages(self, source):
        """Handle listing number of messages."""

        # Get number of messages waiting
        count = await self.get_mailbox_count(source)

        # Send response
        self.send_message(source, "You have {} message(s) waiting".format(count))

    async def handle_info_message(self, source, args):
        """Handle message info."""

        # If no message number given, or message number is not a number, send help
        if len(args) < 2:
            self.send_message(
                source, "Usage: I <message number>"
            )

        elif not re.match("[0-9]+", args[1]):
            self.send_message(
                source, "Usage: I <message number>"
            )

        else:
            # Get message
            number = int(args[1])
            message = await self.get_message_by_number(source, number)

            if message:
                # Send response
                self.send_message(source, "From: {} On: {}".format(
                    message.source.station, message.timestamp.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                ))

            else:
                # No matching message number found
                self.send_message(source, "Message #{} not found".format(
                    number
                ))

    async def handle_delete_message(self, source, args):
        """Handle deleting messages."""

        # If no message number given or message number is not a number, send help
        if len(args) < 2:
            self.send_message(
                source, "Usage: D <message number>"
            )

        elif not re.match("[0-9]+", args[1]):
            self.send_message(
                source, "Usage: D <message number>"
            )

        else:
            # Get message
            number = int(args[1])

            # Delete it
            # NOTE: Messages aren't actually deleted, just made invisible
            deleted = await self.delete_message_by_number(source, number)

            if deleted:
                # Message deleted
                self.send_message(source, "Message #{} deleted".format(
                    number
                ))

            else:
                # No matching message number found
                self.send_message(source, "Message #{} not found".format(
                    number
                ))

    async def handle_delete_all_messages(self, source):
        """Handle deleting all messages."""

        # Delete them
        # NOTE: Messages aren't actually deleted, just made invisible
        deleted = await self.delete_all_messages(source)

        if deleted:
            # Message deleted
            self.send_message(source, "Messages deleted")

        else:
            # No matching message number found
            self.send_message(source, "Messages not deleted")

    async def handle_create_chat(self, source, args):
        """Handle creating chats."""

        # If there's no arguments, send help
        if len(args) < 2:
            self.send_message(source, "Usage: C <chat>")
            return

        # Uppercase the chat name
        name = args[1].upper()

        # Create the chat
        created = await self.create_chat(name, source)

        if created:
            # Chat created
            self.send_message(source, "Chat {} created".format(name))

        else:
            # Chat not created
            self.send_message(source, "Chat {} exists".format(name))

    async def handle_show_all_chats(self, source):
        """Handle showing all available chats."""

        # Get chats
        chats = await self.show_all_chats()

        # Send response
        self.send_message(source, "Chats: {}".format(" ".join(chats)))

    async def handle_join_chat(self, source, args):
        """Handle joining a chat."""

        # If there's no arguments, send help
        if len(args) < 2:
            self.send_message(source, "Usage: J <chat>")
            return

        # Uppercase the chat name
        name = args[1].upper()

        # Join the chat
        joined = await self.join_chat(name, source)

        if joined == 0:
            # Chat joined
            self.send_message(source, "Chat {} joined".format(name))

        elif joined == 1:
            # Chat does not exist
            self.send_message(source, "Chat {} does not exist".format(name))

        elif joined == 2:
            # Station already in the chat
            self.send_message(source, "Already in chat {}".format(name))

    async def handle_leave_chat(self, source, args):
        """Handle leaving a chat."""

        # If there's no arguments, send help
        if len(args) < 2:
            self.send_message(source, "Usage: P <chat>")
            return

        # Uppercase the chat name
        name = args[1].upper()

        # Leave the chat
        left = await self.leave_chat(name, source)

        if left == 0:
            # Chat left
            self.send_message(source, "Chat {} left".format(name))

        elif left == 1:
            # Chat does not exist
            self.send_message(source, "Chat {} does not exist".format(name))

        elif left == 2:
            # Station is not in chat
            self.send_message(source, "Not in chat {}".format(name))

    async def handle_chat_message(self, packet):
        """Handle chat messages."""

        # If there's missing arguments, send help. Otherwise, parse the chat name and message
        if packet.message[0].lower() == "t":
            if len(packet.message.split(' ')) < 3:
                self.send_message(packet.source, "Usage: T <chat> <message>")
                return

            chat_name = packet.message.split(' ')[1].upper()
            message = ' '.join(packet.message.split(' ')[2:])
        else:
            if len(packet.message.split(' ')) < 2:
                self.send_message(packet.source, "Usage: .<chat> <message>")
                return

            chat_name = packet.message[1:].split(' ')[0].upper()
            message = ' '.join(packet.message.split(' ')[1:])

        # Send message to chat
        success = await self.send_to_chat(chat_name, packet.source, message)

        if not success:
            # Chat does not exist
            self.send_message(packet.source, "Chat {} does not exist".format(chat_name))

    async def handle_chat_topic(self, packet):
        """Handle setting the chat topic."""

        # No/missing args, send help
        if len(packet.message.split(' ')) < 3:
            self.send_message(packet.source, "Usage: A <chat> <topic>")
            return

        # Parse the chat name, uppercase it, and parse the topic
        chat_name = packet.message.split(' ')[1].upper()
        topic = ' '.join(packet.message.split(' ')[2:])

        # Set the chat topic
        result = await self.set_chat_topic(chat_name, packet.source, topic)

        if result == 1:
            # Chat does not exist
            self.send_message(packet.source, "Chat {} does not exist".format(chat_name))

        elif result == 2:
            # Station does not own the chat
            self.send_message(packet.source, "You are not the owner of {}".format(chat_name))

    async def handle_pounce(self, source, args):
        """Handle pounce mode"""

        # If there's no arguments, send help
        if len(args) < 2:
            self.send_message(source, "Usage: PN <on|off>")
            return

        state = args[1].lower()

        if state == "on":
            enabled = True
        elif state == "off":
            enabled = False
        else:
            self.send_message(source, "Usage: PN <on|off>")
            return

        # Toggle pounce mode for station
        result = await self.set_pounce(source, enabled)

        if result:
            if enabled:
                self.send_message(source, "Pounce mode enabled")
            else:
                self.send_message(source, "Pounce mode disabled")

        else:
            # Uh oh
            if enabled:
                self.send_message(source, "Failed to enable pounce mode")
            else:
                self.send_message(source, "Failed to disable pounce mode")

    @sync_to_async
    def log_command(self, source, message, response=False):
        """Log a command."""

        # Get or create the station object
        station, created = Station.objects.get_or_create(station=source)

        if created:
            logger.info("Created new station for {}".format(source))
            station.save()

        logger.info("Logged command from {}: {} (is response: {})".format(
            source, message, response
        ))

        # Log the command and save it
        command = Command(source=station, command=message, response=response)
        command.save()

    @sync_to_async
    def list_chats(self, source):
        """Get the list of chats a station is a member of."""

        # Get or create the station object
        station, created = Station.objects.get_or_create(station=source)

        if created:
            logger.info("Created new station for {}".format(source))
            station.save()

        logger.info("{} requesting chats".format(source))

        chats = []

        # Iterate over the chats the station is a member of
        for cs in ChatSubscription.objects.filter(station__station=source):
            chats.append(cs.chat.name)

        return chats

    @sync_to_async
    def create_chat(self, name, source):
        """Create a chat."""

        # Get or create the station object
        owner, created = Station.objects.get_or_create(station=source)

        if created:
            owner.save()

        logger.info("{} creating chat {}".format(source, name))

        # Get or create the chat object
        chat, created = Chat.objects.get_or_create(name=name, owner=owner)

        if not created:
            # Chat exists
            logger.info("Chat {} already exists".format(name))
            return False
        else:
            # Chat created
            # Set the owner to the station
            chat.owner = owner
            chat.save()
            logger.info("Chat {} created".format(name))

            # Add the station to the chat
            cs = ChatSubscription(chat=chat, station=owner)
            cs.save()

        return True

    @sync_to_async
    def show_all_chats(self):
        """Get all chats."""

        # Return a list of all chats
        return [chat.name for chat in Chat.objects.all()]

    @sync_to_async
    def join_chat(self, name, source):
        """Join a chat."""

        # Get or create the station object
        member, created = Station.objects.get_or_create(station=source)

        if created:
            member.save()

        logger.info("{} joining chat {}".format(source, name))

        try:
            chat = Chat.objects.get(name=name)

        except Chat.DoesNotExist:
            logger.info("Chat {} does not exist".format(name))
            return 1

        # Add station to chat
        cs, created = ChatSubscription.objects.get_or_create(chat=chat, station=member)

        if not created:
            # Station is already a member of the chat
            logger.info("{} already a member of {}".format(source, name))
            return 2

        else:
            # Save chat membership
            cs.save()
            logger.info("{} joined chat {}".format(source, name))

            # Send join message to other chat members
            for member in chat.members:
                if source != member.station:
                    self.send_message(member.station, "[{}] {} joined".format(name, source))

            # Send chat topic to station
            self.send_message(source, "Topic for {}: {}".format(name, chat.topic))

            return 0

    @sync_to_async
    def leave_chat(self, name, source):
        """Leave a chat."""

        # Get or create the station object
        member, created = Station.objects.get_or_create(station=source)

        if created:
            member.save()

        logger.info("{} leaving chat {}".format(source, name))

        try:
            chat = Chat.objects.get(name=name)

        except Chat.DoesNotExist:
            logger.info("Chat {} does not exist".format(name))
            return 1

        try:
            # Remove station from chat
            cs = ChatSubscription.objects.get(chat=chat, station=member)
            cs.delete()
            logger.info("{} left chat {}".format(source, name))

            # Send leave message to chat members
            for member in chat.members:
                self.send_message(member.station, "[{}] {} left".format(name, source))

            return 0

        except ChatSubscription.DoesNotExist:
            # Station is not a member of the chat
            logger.info("{} not a member of {}".format(source, name))
            return 2

    @sync_to_async
    def send_to_chat(self, name, sender, message):
        """Send a message to a chat."""

        # Get or create the station object
        source, created = Station.objects.get_or_create(station=sender)

        if created:
            source.save()

        logger.info("{} sending message to chat {}: {}".format(sender, name, message))

        try:
            chat = Chat.objects.get(name=name)

        except Chat.DoesNotExist:
            logger.info("Chat {} does not exist".format(name))
            return False

        # Send message to chat members
        for member in chat.members:
            self.send_message(member.station, "{}>{}: {}".format(sender, name, message))

        # Save the chat message
        m = ChatMessage(chat=chat, source=source, message=message)
        m.save()

        return True

    @sync_to_async
    def set_chat_topic(self, name, sender, topic):
        """Set the topic for a chat."""

        # Get or create the station object
        source, created = Station.objects.get_or_create(station=sender)

        if created:
            source.save()

        logger.info("{} setting topic of chat {}: {}".format(sender, name, topic))

        try:
            chat = Chat.objects.get(name=name)

        except Chat.DoesNotExist:
            # Chat does not exist
            logger.info("Chat {} does not exist".format(name))
            return 1

        if chat.owner.station != source.station:
            # Chat is not owned by the station
            logger.info("Chat {} not owned by {}".format(name, source))
            return 2

        # Set the chat topic
        chat.topic = topic
        chat.save()

        # Send topic to chat members
        for member in chat.members:
            self.send_message(member.station, "{} set topic: {}".format(sender, topic))

        return 0

    @sync_to_async
    def add_mailbox_message(self, sender, to, message):
        """Add a message to a mailbox."""

        # Get or create the source station object
        source, created = Station.objects.get_or_create(station=sender)

        if created:
            source.save()

        # Get or create the addressee station object
        addressee, created = Station.objects.get_or_create(station=to)

        if created:
            addressee.save()

        # Get the addressee's mailbox
        mailbox = addressee.mailbox

        # Add the message to the mailbox
        mailbox.add(source, message)

        # Mark the mailbox as having unread messages
        mailbox.unread = True

        # Save the mailbox
        mailbox.save()

        # Handle pounces if enabled
        self._pounce_list = [
            s.station for s in Station.objects.filter(pounce=True).filter(mailbox__unread=True)
        ]
        logger.info("Pounce list is now: {}".format(self._pounce_list))

        logger.info("{} sent message to {}: {}".format(
            sender, addressee.station, message
        ))

        return True

    @sync_to_async
    def get_mailbox_count(self, source):
        """Get the number of messages in a mailbox."""

        # Get or create the station object
        station, created = Station.objects.get_or_create(station=source)

        if created:
            station.save()

        # Get the mailbox
        mailbox = station.mailbox
        logger.info("Mailbox is {}".format(mailbox))
        logger.info("Mailbox count is {}".format(mailbox.count))

        # Mark the mailbox as read
        mailbox.unread = False
        mailbox.save()

        # Handle pounces is enabled
        self._pounce_list = [
            s.station for s in Station.objects.filter(pounce=True).filter(mailbox__unread=True)
        ]
        logger.info("Pounce list is now: {}".format(self._pounce_list))

        return mailbox.count

    @sync_to_async
    def get_message_by_number(self, source, number):
        """Get a message from a mailbox by message ID."""

        # Get the station object
        # TODO: This should be a get or create
        station = Station.objects.get(station=source)

        # Get the mailbox
        mailbox = station.mailbox
        logger.info("Mailbox is {}".format(mailbox))
        logger.info("Mailbox count is {}".format(mailbox.count))

        # Mark the mailbox as read
        mailbox.unread = False
        mailbox.save()

        # Get the message
        message = mailbox.get_message_by_number(number)

        if message:
            # Found message
            logger.info("Message from {}, to {}: {}".format(
                message.source.station, message.mailbox.owner.station, message.message
            ))

        else:
            # Message not found
            logger.info("Message #{} not found for {}".format(number, source))
            return False

        # Mark message as read
        message.read = True
        message.save()

        return message

    @sync_to_async
    def delete_message_by_number(self, source, number):
        """Delete a message from a mailbox by message ID."""

        # Get the station object
        # TODO: This should be a get or create
        station = Station.objects.get(station=source)

        # Get mailbox
        mailbox = station.mailbox
        logger.info("Mailbox is {}".format(mailbox))
        logger.info("Mailbox count is {}".format(mailbox.count))

        # Mark mailbox as read
        mailbox.unread = False
        mailbox.save()

        # Delete message from mailbox
        deleted = mailbox.delete_message_by_number(number)

        if deleted:
            # Message deleted
            logger.info("Message #{} for {} deleted.".format(
                number, source
            ))

        else:
            # Message not found
            logger.info("Message #{} not found for {}".format(number, source))

        return deleted

    @sync_to_async
    def delete_all_messages(self, source):
        """Delete all messages from a mailbox."""

        # Get the station object
        # TODO: This should be a get or create
        station = Station.objects.get(station=source)

        # Get mailbox
        mailbox = station.mailbox
        logger.info("Mailbox is {}".format(mailbox))
        logger.info("Mailbox count is {}".format(mailbox.count))

        # Mark mailbox as read
        deleted = mailbox.delete_all()

        if deleted:
            # Message deleted
            logger.info("Messages for {} deleted.".format(source))

        else:
            logger.info("Message not deleted for {}".format(source))

        return deleted

    @sync_to_async
    def load_pounce_list(self):
        """Load the pounce list."""

        # Get current pounce list based on pounce mode and mailbox unread status
        self._pounce_list = [
            s.station for s in Station.objects.filter(pounce=True).filter(mailbox__unread=True)
        ]
        logger.info("Pounce list is now: {}".format(self._pounce_list))

    @sync_to_async
    def set_pounce(self, source, enabled):
        """Toggle pounce mode."""

        # Get the station object
        # TODO: This should get a get or create
        station = Station.objects.get(station=source)

        if enabled:
            # Enable pounce mode
            station.pounce = True
            station.save()

            logger.info("Pounce for {} enabled".format(source, station.pounce))
            self._pounce_list = [
                s.station for s in Station.objects.filter(pounce=True).filter(mailbox__unread=True)
            ]
            logger.info("Pounce list is now: {}".format(self._pounce_list))

            return True

        else:
            # Disable pounce mode
            station.pounce = False
            station.save()

            logger.info("Pounce for {} disabled".format(source, station.pounce))
            self._pounce_list = [
                s.station for s in Station.objects.filter(pounce=True).filter(mailbox__unread=True)
            ]
            logger.info("Pounce list is now: {}".format(self._pounce_list))

            return True

    async def get_qrz(self, callsign):
        """Query the QRZ API for a callsign."""
        logger.debug("QRZ request for {}".format(callsign))

        return await self.qrz.callsign(callsign)

    async def get_solar(self):
        """Query DXCluster for solar info."""

        # Query DXCLuster
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://www.dxcluster.co.uk/api/solar"
            ) as response:
                return await response.text()

    async def get_dx(self, callsign):
        """Query DXCluster for DX info."""

        logger.info("DX cluster request for {}".format(callsign))

        return await self.dxwatch.dx(callsign)

    def parse_server(self, line):
        """Handle APRS-IS server lines."""

        logger.info("APRS-IS: {}".format(line))

        # Create regexes for APRS-IS server response
        version_re = re.compile(r'^# ([\w\-\.]+) ([\w\-\.]+)$')
        logresp_re = re.compile(r'^# logresp ([A-Z0-9\-]+) (\w+), server ([A-Z0-9\-]+)', re.I)

        if version_re.match(line):
            # This is probably a version string
            (software, version) = version_re.match(line).groups()

            logger.info("Server is {}, version {}".format(software, version))

            if not self._logged_in and not self._login_attempted:
                # We're not logged in, so do the login
                self.login()
            else:
                logger.info("Not doing login")

        if logresp_re.match(line):
            # Handle login response
            (callsign, verified, server) = logresp_re.match(line).groups()

            if verified == 'verified':
                # We're logged in to the APRS-IS server
                logger.info("Logged in to {} as {}".format(server, callsign))
                self._logged_in = True
            else:
                # Failed to log in
                logger.error("Failed to log in to {} as {}".format(server, callsign))

    def send(self, msg):
        """Send data to the APRS-IS server."""

        if not self._logged_in:
            # Not currently logged in, so don't send
            logger.warning("APRSIS: Not yet logged in!")
        else:
            # Send data to APRS-IS
            logger.info("Sending: {}".format(msg))
            self._transport.write("{}\n".format(msg).encode('UTF-8'))

    def send_message(self, destination, message, ack=None, log=True):
        """Send an APRS message."""

        # If a message ID is given, add it
        if ack:
            message = "{" + ack + "}" + message

        # Split the message if it's longer than 67 characters
        messages = wrap(message, 67)

        # Iterate over the messages and generate a MessagePacket for them
        for message in messages:
            packet = MessagePacket(
                source=self._source,
                destination="APPYAP",
                path="TCPIP*",
                addressee=destination,
                message=message
            )

            # Send message
            self.send(packet.generate())

            # Log command
            if log:
                self._loop.create_task(self.log_command(destination, message, True))

    def login(self):
        """Log in to APRS-IS."""

        logger.info("Attempting login...")

        # If we've attempted previously, don't try again
        if self._login_attempted or self._logged_in:
            logger.info("Already logged in or login already attempted")
            return False

        # Build login string
        # If there's no callsign specified, we can't log in
        if not self._callsign:
            logger.info("No callsign given for login")
            self._login_attempted = True
            return False

        # Add callsign
        login_string = "user {}".format(self._callsign)

        # Add SSID, if specified
        if self._ssid:
            login_string += "-{}".format(self._ssid)

        # Add passcode, if specified
        if self._passcode:
            login_string += " pass {}".format(self._passcode)

        # Add our version string
        login_string += " vers rfdata-aprs 0.1"

        # If a filter is given, add that too
        if self._filter:
            login_string += " filter {}".format(self._filter)

        # Send our login string
        logger.info("Login string: {}".format(login_string))
        self._transport.write("{}\n".format(login_string).encode('UTF-8'))

    def connection_lost(self, exc):
        """Handle losing connection."""

        logger.info("Connection lost!")

        # Reset login flags and retry
        self._login_attempted = False
        self._logged_in = False
        self.retry()

    def connection_failed(self, exc):
        """Handle connection failed."""

        logger.info("Connection failed!: {}".format(exc))

        # Retry
        self.retry()

    def retry(self):
        """Handle connection retry."""

        # Don't retry if we're stopping anyway
        if self._stopping:
            return

        logger.info("Retrying connection...")

        # Reconnect
        self._loop.call_soon(self.connect)

    def connect(self):
        """Attempt connection to APRS-IS."""

        if self._connector is None:
            self._connector = self._loop.create_task(self._connect())

    async def _connect(self):
        """Handle connection to APRS-IS."""

        try:
            await self._loop.create_connection(lambda: self, *self._args, **self._kwargs)

        except Exception as e:
            self._loop.call_soon(self.connection_failed, e)

        finally:
            self._connector = None


@click.command()
@click.option('-c', '--config-file', default="config.yml", help="config file")
def service(config_file):
    config = None

    try:
        with open(config_file, 'r') as fp:
            config = yaml.load(fp.read(), Loader=yaml.SafeLoader)

    except FileNotFoundError:
        print("Configuration file {} not found.".format(config_file))
        sys.exit(1)

    except PermissionError:
        print("Couldn't open configuration file {}.".format(config_file))
        sys.exit(1)

    except yaml.parser.ParserError:
        print("Couldn't parse configuration file {}.".format(config_file))
        sys.exit(1)

    server = None
    port = None
    callsign = None
    ssid = None
    passcode = None
    aprs_filter = None
    qrz_username = None
    qrz_password = None
    aprsfi_key = None
    checkwx_key = None
    darksky_key = None

    if 'aprsis' not in config:
        print("Missing 'aprsis' section in configuration file.")
        sys.exit(1)

    if 'server' not in config['aprsis']:
        print("Missing 'server' value in 'aprsis' section in configuration file.")
        sys.exit(1)

    if 'port' not in config['aprsis']:
        print("Missing 'port' value in 'aprsis' section in configuration file.")
        sys.exit(1)

    if 'callsign' not in config['aprsis']:
        print("Missing 'callsign' value in 'aprsis' section in configuration file.")
        sys.exit(1)

    if 'passcode' not in config['aprsis']:
        print("Missing 'passcode' value in 'aprsis' section in configuration file.")
        sys.exit(1)

    if 'qrz' not in config:
        print("Missing 'qrz' section in configuration file.")
        sys.exit(1)

    if 'username' not in config['qrz']:
        print("Missing 'username' value in 'qrz' section in configuration file.")
        sys.exit(1)

    if 'password' not in config['qrz']:
        print("Missing 'password' value in 'qrz' section in configuration file.")
        sys.exit(1)

    if 'aprsfi' not in config:
        print("Missing 'aprsfi' section in configuration file.")
        sys.exit(1)

    if 'key' not in config['aprsfi']:
        print("Missing 'key' value in 'aprsfi' section in configuration file.")
        sys.exit(1)

    if 'checkwx' not in config:
        print("Missing 'checkwx' section in configuration file.")
        sys.exit(1)

    if 'key' not in config['checkwx']:
        print("Missing 'key' value in 'checkwx' section in configuration file.")
        sys.exit(1)

    if 'darksky' not in config:
        print("Missing 'darksky' section in configuration file.")
        sys.exit(1)

    if 'key' not in config['darksky']:
        print("Missing 'key' value in 'darksky' section in configuration file.")
        sys.exit(1)

    server = config['aprsis'].get('server')
    port = config['aprsis'].get('port')
    callsign = config['aprsis'].get('callsign')
    ssid = config['aprsis'].get('ssid', None)
    passcode = config['aprsis'].get('passcode')
    aprs_filter = config['aprsis'].get('filter', None)
    qrz_username = config['qrz'].get('username')
    qrz_password = config['qrz'].get('password')
    aprsfi_key = config['aprsfi'].get('key')
    checkwx_key = config['checkwx'].get('key')
    darksky_key = config['darksky'].get('key')

    if ssid:
        ssid = str(ssid)

    print("Starting with callsign {} (SSID: {}) connecting to {}:{} with filter {}.".format(
        callsign, ssid, server, port, aprs_filter
    ))

    # Set up event loop
    loop = asyncio.get_event_loop()

    # Build APRS client
    client = APRSClient(server, port, loop=loop, callsign=callsign, ssid=ssid,
                        passcode=passcode, filter=aprs_filter)

    # Add 3rd party APIs
    client.qrz = QRZ(username=qrz_username, password=qrz_password)
    client.aprsfi = APRSFI(apikey=aprsfi_key)
    client.checkwx = CheckWX(apikey=checkwx_key)
    client.darksky = DarkSky(apikey=darksky_key)
    client.dxwatch = DXWatch()
    client.sun = Sun()
    client.pskr = PSKReporter()

    # Connect to APRS-IS
    client.connect()

    # Start async loop
    loop.run_forever()
    loop.close()


if __name__ == '__main__':
    service()
