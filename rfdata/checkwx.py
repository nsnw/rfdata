#!/usr/bin/env python

# Imports
import aiohttp
import logging
import json

logger = logging.getLogger('aprs-service')


class CheckWX:
    """Class to handle querying the CheckWX API."""

    def __init__(self, apikey: str):
        self._apikey = apikey

    async def _metar_details(self, icao):
        """Query the CheckWX API."""
        logger.debug("CheckWX request for {}".format(icao))

        # Query the CheckWX API
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.checkwx.com/metar/{}".format(icao),
                headers={'X-API-Key': self._apikey}
            ) as response:
                return await response.text()

    async def metar(self, icao):
        """Get METAR details for an airport."""

        # Get METAR details
        response = await self._metar_details(icao)

        try:
            details = json.loads(response)

        except Exception as e:
            logger.error(e)
            return False

        if details['results'] == 0:
            logger.error("Error response from CheckWX: {}".format(response))
            return False

        return details['data'][0]
