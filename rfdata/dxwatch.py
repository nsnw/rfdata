#!/usr/bin/env python

# Imports
import aiohttp
import logging
import json

logger = logging.getLogger('aprs-service')


class DXWatch:
    """Class to handle querying the DXWatch API."""

    async def _dx_details(self, callsign):
        """Query the DXWatch API."""
        logger.debug("DXWatch request for {}".format(callsign))

        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://dxwatch.com/dxsd1/s.php?s=0&r=15&cdx={}".format(callsign)
            ) as response:
                return await response.text()

    async def dx(self, callsign):
        """Query the DXWatch API."""

        # Get DX details
        response = await self._dx_details(callsign)

        try:
            details = json.loads(response)

        except Exception as e:
            logger.error(e)
            return False

        if 'ns' in details:
            logger.info("No spot found for {}".format(callsign))
            return False

        else:
            # We only want the first spot
            spot = list(details['s'].values())[-1]

            message = "{} de {} on {} @{}".format(
                spot[2], spot[0], spot[1], spot[4]
            )

            return message
