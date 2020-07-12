#!/usr/bin/env python

# Imports
import aiohttp
import logging
import json
import re
from datetime import datetime

logger = logging.getLogger('aprs-service')


class Sun:
    """Class to handle querying the DXWatch API."""

    async def _sunrise_sunset_details(self, latitude: float, longitude: float):
        """Query for the sunrise/sunset."""
        logger.debug("Sunrise/sunset request for {}, {}".format(latitude, longitude))

        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://satellites.nsnw.ca/sun/loc={},{}".format(latitude, longitude)
            ) as response:
                return await response.text()

    async def sunrise_sunset(self, latitude: float, longitude: float):
        """Query satellite.nsnw.ca for sunrise/sunset info."""

        # Get DX details
        response = await self._sunrise_sunset_details(latitude, longitude)

        try:
            details = json.loads(response)

        except Exception as e:
            logger.error(e)
            return False

        if 'response' not in details:
            logger.info("Couldn't get sunrise/sunset details for {}, {}".format(
                latitude, longitude
            ))
            return False

        elif not details['response']['sunrise']['next'] and not details['response']['sunset']['next']:
            logger.info("No sunrise/sunset details for {}, {}".format(
                latitude, longitude
            ))
            return False

        else:
            logger.info(details['response']['sunrise']['next'])
            logger.info(re.match(r'(.*)T(.*)Z', details['response']['sunrise']['next']).groups())
            next_sunrise = datetime.fromisoformat(
                "{} {}".format(
                    *re.match(r'(.*)T(.*)Z', details['response']['sunrise']['next']).groups()
                )
            )
            next_sunset = datetime.fromisoformat(
                "{} {}".format(
                    *re.match(r'(.*)T(.*)Z', details['response']['sunset']['next']).groups()
                )
            )

            if next_sunset > next_sunrise:
                # Currently nighttime
                message = "Currently nighttime. Sun rises tomorrow at {0:02d}:{0:02d} UTC.".format(
                    next_sunrise.hour, next_sunrise.minute
                )

            else:
                # Currently daytime
                message = "Currently daytime. Sun sets tonight at {0:02d}:{0:02d} UTC.".format(
                    next_sunset.hour, next_sunset.minute
                )

            return message
