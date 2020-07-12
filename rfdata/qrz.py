#!/usr/bin/env python

import aiohttp
import xmltodict
import logging
from asyncache import cached
from cachetools import TTLCache

logger = logging.getLogger('aprs-service')


class QRZ(object):
    def __init__(self, username: str, password: str):
        self._session = None
        self._session_key = None
        self._username = username
        self._password = password

    async def init_session(self):
        logger.info("Initializing QRZ session...")

        if not self._username or not self._password:
            logger.info("Missing QRZ username/password - QRZ lookups disabled.")
        else:
            url = 'https://xmldata.qrz.com/xml/current/?username={0}&password={1}&agent='.format(
                self._username,
                self._password,
                'RFDATA'
            )

            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    status = response.status
                    data = await response.text()

            if status == 200:
                raw_session = xmltodict.parse(data)
                self._session_key = raw_session['QRZDatabase']['Session']['Key']
                if self._session_key:
                    return True

            logger.info("Could not get QRZ session - QRZ lookups disabled.")
            return False

    @cached(TTLCache(ttl=900, maxsize=1000))
    async def callsign(self, callsign, retry=True):
        logger.info("Doing QRZ lookup for {}".format(callsign))

        if self._session_key is None:
            await self.init_session()

        url = """http://xmldata.qrz.com/xml/current/?s={0}&callsign={1}""".format(
            self._session_key, callsign
        )

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                status = response.status
                data = await response.text()

        if status != 200:
            return False

        raw = xmltodict.parse(data).get('QRZDatabase')
        if not raw:
            logger.info('Unexpected API Result')
            return False

        if raw['Session'].get('Error'):
            errormsg = raw['Session'].get('Error')
            if 'Session Timeout' in errormsg or 'Invalid session key' in errormsg:
                if retry:
                    self._session_key = None
                    self._session = None
                    return self.callsign(callsign, retry=False)
            elif "not found" in errormsg.lower():
                return False

            return False

        else:
            result = raw.get('Callsign')
            if result:
                address = []

                address.append(result['addr2'])

                if 'state' in result:
                    address.append(result['state'])

                address.append(result['land'])

                response = "{}/{} {}/{}".format(
                    result['call'],
                    result['fname'] if 'fname' in result else "",
                    result['name'] if 'name' in result else "",
                    '/'.join(address)
                )

                if result['grid']:
                    response += "/{}".format(result['grid'])

                return response
            else:
                return False
