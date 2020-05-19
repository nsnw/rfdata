#!/usr/bin/env python

# Imports
import aiohttp
import logging
import json

logger = logging.getLogger('aprs-service')


class APRSFI:
    """Class to handle querying the aprs.fi API."""

    def __init__(self, apikey: str):
        self._apikey = apikey

    async def _station_details(self, callsign):
        """Query station details via aprs.fi."""

        logger.debug("APRS.FI request for {}".format(callsign))

        # Query aprs.fi
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.aprs.fi/api/get?name={}&what=loc&apikey={}&format=json".format(
                    callsign, self._apikey
                )
            ) as response:
                return await response.text()

    async def station(self, callsign):
        """Get station position details."""

        response = await self._station_details(callsign)

        try:
            details = json.loads(response)

        except Exception as e:
            # Something went wrong
            logger.error(e)
            return False

        if details['result'] != "ok":
            # Something went wrong
            logger.error("Error response from aprs.fi: {}".format(response))
            return False

        return details['entries'][0]
