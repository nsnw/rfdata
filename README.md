![RFDATA APRS BBS](https://aprs.rfdata.org/w/resources/assets/wiki.png)

# RFDATA

This is the source code for the RFDATA APRS BBS. This is not a complete solution, and is here for anyone interested in contributing. For more information, please see https://aprs.rfdata.org/.

# Development

RFDATA uses Django to store backend data and for the UI. You will need to be familiar with Django to get this running.

You are welcome to take this and use it to run your own service, but there are some things to bear in mind:-

* You should be familiar with APRS and APRS-IS
* To connect to APRS-IS, you should be a holder of an amateur radio license
* You will need a QRZ account for the QRZ lookup facility
* You will need a APRS.FI account and API key for anything related to APRS positioning
* You will need a CheckWX API key for the METAR details
* You will need a DarkSky API key for the weather lookup. DarkSky isn't accepting new accounts at the time of writing, so you will probably need to comment that feature out

As noted above, this is here mostly for anyone interested in contributing. You are more than welcome to run your own service, but I can't offer individual support.

# Running it

There's two parts to RFDATA - the main service (`service.py`) and the UI. The production RFDATA service runs in two Docker containers, but for development purposes you can run it from the command line:-

* Run the service by configuring `config.yml`, then running `./service.py -c config.yml`
* Run the UI by running `./managed.py runserver`

You will need to configure Django properly and apply the migrations for any of this to work.

# Feedback and more information

For more information, please see https://aprs.rfdata.org/. Feedback and suggestions is most welcome!

(c) 2020 Andy Smith, VE6LY <andy@nsnw.ca>
