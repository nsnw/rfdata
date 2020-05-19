#!/usr/bin/env python

# Imports
import aiohttp
import logging
import json

logger = logging.getLogger('aprs-service')


class DarkSky:
    """Class to handle querying the DarkSky API."""

    def __init__(self, apikey: str):
        self._apikey = apikey

    async def _wx(self, lat, lng):
        """Query the DarkSky API."""

        logger.info("DarkSky request for {}, {}".format(lat, lng))

        # Build URL
        url = "https://api.forecast.io/forecast/{}/{},{}?units=ca".format(
            self._apikey,
            lat,
            lng
        )

        # Query DarkSky API
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url
            ) as response:
                return await response.text()

    async def wx(self, lat, lng):
        """Get weather for a given lat/lng."""

        response = await self._wx(lat, lng)

        try:
            weather_data = json.loads(response)

        except Exception as e:
            logger.error(e)
            return False

        # Build response
        info = {}
        info['summary'] = weather_data['currently']['summary']
        info['temp_c'] = weather_data['currently']['temperature']
        info['humidity'] = weather_data['currently']['humidity'] * 100
        info['windspeed'] = weather_data['currently']['windSpeed']
        info['windGust'] = weather_data['currently']['windGust']
        wind_degrees = weather_data['currently']['windBearing']
        wind_cardinal = ('N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                        'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW', 'N')
        wind_res = round(wind_degrees / 22.5)
        info['wind_dir'] = wind_cardinal[int(wind_res)]
        info['feelslike_c'] = weather_data['currently']['apparentTemperature']

        return '{summary} {temp_c:.1f}C Hu:{humidity:.1f}% W:f/{wind_dir}@{windspeed:.1f}kmh(g:{windGust:.1f}kmh) FL:{feelslike_c:.1f}C'.format(**info)
