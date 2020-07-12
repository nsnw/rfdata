#!/usr/bin/env python

# Imports
import aiohttp
import logging
import json
import re
import maidenhead
from asyncache import cached
from cachetools import TTLCache

logger = logging.getLogger('aprs-service')


class PSKReporter:
    """Class to handle querying the PSKReporter API."""

    @cached(TTLCache(ttl=900, maxsize=1000))
    async def _psk_freq(self, locator: str):
        """Query for the busiest frequencies."""
        logger.debug("Busiest frequencies for {}".format(locator))

        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://pskreporter.info/cgi-bin/psk-freq.pl?grid={}".format(locator)
            ) as response:
                return await response.text()

    async def psk_freq(self, latitude: float, longitude: float):
        """Query pskreporter.info for the busiest frequencies given a location."""

        # Get DX details
        locator = maidenhead.toMaiden(float(latitude), float(longitude))[0:2]
        response = await self._psk_freq(locator)

        try:
            frequencies = []
            lines = response.split("\n")

            first = int(lines[0].split(" ")[0])/1000
            second = int(lines[1].split(" ")[0])/1000
            third = int(lines[2].split(" ")[0])/1000

            message = "Freqs for {}: {}, {} and {}".format(locator, first, second, third)

            return message

        except:
            return "Couldn't look up frequencies"
