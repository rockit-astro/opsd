#!/usr/bin/env python3
#
# This file is part of opsd.
#
# opsd is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# opsd is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with opsd.  If not, see <http://www.gnu.org/licenses/>.

"""Telescope action to acquire sky flats"""

# pylint: disable=broad-except
# pylint: disable=invalid-name
# pylint: disable=too-many-arguments
# pylint: disable=too-many-return-statements
# pylint: disable=too-few-public-methods
# pylint: disable=too-many-branches
# pylint: disable=too-many-instance-attributes
# pylint: disable=too-many-statements

import datetime
import math
import sys
import threading
import traceback
import Pyro4

from astropy.coordinates import (
    get_sun,
    EarthLocation,
    AltAz
)
from astropy.time import Time
from astropy import units as u

from warwick.observatory.common import (
    daemons,
    log)
from warwick.observatory.operations import (
    TelescopeAction,
    TelescopeActionStatus)
from warwick.rasa.camera import (
    configure_validation_schema as camera_schema)
from warwick.rasa.pipeline import (
    configure_flats_validation_schema as pipeline_schema)
from .telescope_helpers import tel_status, tel_slew_altaz
from .camera_helpers import stop_camera
from .pipeline_helpers import pipeline_enable_archiving, configure_pipeline

SLEW_TIMEOUT = 120

class AutoFlatState:
    """Possible states of the AutoFlat routine"""
    Bias, Waiting, Saving, Complete, Error = range(5)
    Names = ['Bias', 'Waiting', 'Saving', 'Complete', 'Error']
    Codes = ['B', 'W', 'S', 'C', 'E']

CONFIG = {
    # Range of sun angles where we can acquire useful data
    'max_sun_altitude': -6,
    'min_sun_altitude': -10,
    'sun_altitude_check_interval': 30,

    # Exposure fudge factor to account for changing sky brightness
    'evening_scale': 1.07,
    'dawn_scale': 0.9,

    # Clamp exposure time deltas to this range (e.g. 5 -> 15 or 5 -> 1.6)
    'max_exposure_delta': 3,

    # Number of seconds to add to the exposure time to account for readout + object detection
    # Consider the frame lost if this is exceeded
    'max_processing_time': 10,

    # Exposure limits in seconds
    'min_exposure': 0.1,
    'max_exposure': 30,

    # Exposures shorter than this will have large shutter effects and will be discarded
    'min_save_exposure': 2.5,

    # Exposures with less counts than this lack the signal to noise ratio that we desire
    'min_save_counts': 15000,

    # Target flat counts to aim for
    'target_counts': 30000,

    # Delays to apply between evening flats to save shutter cycles
    # These delays are cumulative, so if the next exposure is calculated to be 1.2
    # 0.9 seconds the routine will wait 60 + 30 = 90 seconds before starting it
    'evening_exposure_delays': {
        1: 60,
        2.5: 30
    }
}

def sun_position(location):
    """Returns current (alt, az) of sun in degrees for the given location"""
    now = Time(datetime.datetime.utcnow(), format='datetime', scale='utc')
    frame = AltAz(obstime=now, location=location)
    sun = get_sun(now).transform_to(frame)
    return (sun.alt.value, sun.az.value)

class InstrumentArm:
    """Holds arm-specific flat state"""
    def __init__(self, name, daemon, camera_config, is_evening, log_name):
        self.name = name
        self.bias = 0
        self.state = AutoFlatState.Bias
        self._daemon = daemon
        self._log_name = log_name
        self._camera_config = camera_config
        self._expected_complete = datetime.datetime.utcnow()
        self._is_evening = is_evening
        self._scale = CONFIG['evening_scale'] if is_evening else CONFIG['dawn_scale']
        self._start_exposure = CONFIG['min_exposure'] if is_evening else CONFIG['min_save_exposure']
        self._start_time = None
        self._exposure_count = 0

    def start(self):
        """Starts the flat sequence for this arm"""
        self.__take_image(0, 0)
        self._start_time = datetime.datetime.utcnow()

    def check_timeout(self):
        """Sets error state if an expected frame is more than 30 seconds late"""
        if self.state not in [AutoFlatState.Waiting, AutoFlatState.Saving]:
            return

        if datetime.datetime.utcnow() > self._expected_complete:
            print('AutoFlat: ' + self.name + ' camera exposure timed out')
            log.error(self._log_name, 'AutoFlat: ' + self.name + ' camera exposure timed out')
            self.state = AutoFlatState.Error

    def __take_image(self, exposure, delay):
        """Tells the camera to take an exposure.
           if exposure is 0 then it will reset the camera
           configuration and take a bias with the shutter closed
        """
        self._expected_complete = datetime.datetime.utcnow() \
            + datetime.timedelta(seconds=exposure + delay + CONFIG['max_processing_time'])
        try:
            # Need to communicate directly with camera daemon
            # to allow set_exposure_delay and set_exosure
            with self._daemon.connect() as cam:
                if exposure == 0:
                    # .configure will reset all other parameters to their default values
                    cam_config = {}
                    cam_config.update(self._camera_config)
                    cam_config.update({
                        'shutter': False,
                        'exposure': 0
                    })
                    cam.configure(cam_config, quiet=True)
                else:
                    cam.set_exposure_delay(delay, quiet=True)
                    cam.set_exposure(exposure, quiet=True)
                    cam.set_shutter(True, quiet=True)

                cam.start_sequence(1, quiet=True)
        except Pyro4.errors.CommunicationError:
            print('AutoFlat: Failed to communicate with ' + self.name + ' camera daemon')
            log.error(self._log_name, 'Failed to communicate with ' + self.name + ' camera daemon')
            self.state = AutoFlatState.Error
        except Exception:
            print('AutoFlat: Unknown error with ' + self.name + ' camera')
            traceback.print_exc(file=sys.stdout)
            log.error(self._log_name, 'Unknown error with ' + self.name + ' camera')
            self.state = AutoFlatState.Error

    def received_frame(self, headers):
        """Callback to process an acquired frame.  headers is a dictionary of header keys"""
        last_state = self.state
        delay_exposure = 0

        if self.state == AutoFlatState.Bias:
            self.bias = headers['MEDCNTS']
            print('AutoFlat: {} bias level is {:.0f} ADU'.format(self.name, self.bias))
            log.info(self._log_name, 'AutoFlat: {} bias is {:.0f} ADU'.format(self.name, self.bias))

            # Take the first flat image
            self.state = AutoFlatState.Waiting
            self.__take_image(self._start_exposure, delay_exposure)

        elif self.state == AutoFlatState.Waiting or self.state == AutoFlatState.Saving:
            if self.state == AutoFlatState.Saving:
                self._exposure_count += 1

            exposure = headers['EXPTIME']
            counts = headers['MEDCNTS'] - self.bias

            # If the count rate is too low then we scale the exposure by the maximum amount
            if counts > 0:
                new_exposure = self._scale * exposure * CONFIG['target_counts'] / counts
            else:
                new_exposure = exposure * CONFIG['max_exposure_delta']

            # Clamp the exposure to a sensible range
            clamped_exposure = min(new_exposure, CONFIG['max_exposure'],
                                   exposure * CONFIG['max_exposure_delta'])
            clamped_exposure = max(clamped_exposure, CONFIG['min_exposure'],
                                   exposure / CONFIG['max_exposure_delta'])

            clamped_desc = ' (clamped from {:.2f}s)'.format(new_exposure) \
                if new_exposure > clamped_exposure else ''
            print('AutoFlat: {} exposure {:.2f}s counts {:.0f} ADU -> {:.2f}s{}'
                  .format(self.name, exposure, counts, clamped_exposure, clamped_desc))

            if self._is_evening:
                # Sky is decreasing in brightness
                for min_exposure in CONFIG['evening_exposure_delays']:
                    if new_exposure < min_exposure and counts > CONFIG['min_save_counts']:
                        delay_exposure += CONFIG['evening_exposure_delays'][min_exposure]

                if delay_exposure > 0:
                    print('AutoFlat: ' + self.name + ' waiting ' + str(delay_exposure) + \
                          's for it to get darker')

                if clamped_exposure == CONFIG['max_exposure'] \
                        and counts < CONFIG['min_save_counts']:
                    self.state = AutoFlatState.Complete
                elif self.state == AutoFlatState.Waiting and counts > CONFIG['min_save_counts'] \
                        and new_exposure > CONFIG['min_save_exposure']:
                    self.state = AutoFlatState.Saving
            else:
                # Sky is increasing in brightness
                if clamped_exposure < CONFIG['min_save_exposure']:
                    self.state = AutoFlatState.Complete
                elif self.state == AutoFlatState.Waiting and counts > CONFIG['min_save_counts']:
                    self.state = AutoFlatState.Saving

            if self.state != last_state:
                if not pipeline_enable_archiving(self._log_name, self.name,
                                                 self.state == AutoFlatState.Saving):
                    self.state = AutoFlatState.Error
                    return

                print('AutoFlat: ' + self.name + ' ' + AutoFlatState.Names[last_state] \
                    + ' -> ' + AutoFlatState.Names[self.state])

                if self.state == AutoFlatState.Saving:
                    log.info(self._log_name, 'AutoFlat: {} saving enabled'.format(self.name))
                elif self.state == AutoFlatState.Complete:
                    runtime = (datetime.datetime.utcnow() - self._start_time).total_seconds()
                    message = 'AutoFlat: {} acquired {} flats in {:.0f} seconds'.format(
                        self.name, self._exposure_count, runtime)
                    print(message)
                    log.info(self._log_name, message)

            if self.state != AutoFlatState.Complete:
                self.__take_image(clamped_exposure, delay_exposure)

    def abort(self):
        """Aborts any active exposures and sets the state to complete"""
        if self.state == AutoFlatState.Saving:
            stop_camera(self._log_name, self._daemon)
        self.state = AutoFlatState.Complete

class SkyFlats(TelescopeAction):
    """Telescope action to acquire sky flats"""
    def __init__(self, config):
        super().__init__('Sky Flats', config)
        self._wait_condition = threading.Condition()

        self._instrument_arms = {
            'RASA': InstrumentArm('RASA', daemons.rasa_camera, self.config.get('rasa', {}),
                                  self.config['evening'], self.log_name),
        }

    @classmethod
    def validation_schema(cls):
        return {
            'type': 'object',
            'additionalProperties': False,
            'required': ['evening'],
            'properties': {
                'type': {'type': 'string'},
                'evening': {
                    'type': 'boolean'
                },
                'rasa': camera_schema('rasa'),
                'pipeline': pipeline_schema()
            }
        }

    def run_thread(self):
        """Thread that runs the hardware actions"""
        # Query site location from the telescope
        ts = tel_status(self.log_name)
        if not ts:
            self.status = TelescopeActionStatus.Error
            return

        # pylint: disable=no-member
        location = EarthLocation(lat=ts['site_latitude']*u.rad,
                                 lon=ts['site_longitude']*u.rad,
                                 height=ts['site_elevation']*u.m)
        # pylint: enable=no-member

        while not self.aborted:
            waiting_for = []
            sun_altitude = sun_position(location)[0]
            if self.config['evening']:
                if sun_altitude < CONFIG['min_sun_altitude']:
                    print('AutoFlat: Sun already below minimum altitude')
                    log.info(self.log_name, 'AutoFlat: Sun already below minimum altitude')
                    self.status = TelescopeActionStatus.Complete
                    return

                if sun_altitude < CONFIG['max_sun_altitude'] and self.dome_is_open:
                    break

                if not self.dome_is_open:
                    waiting_for.append('Dome')

                if sun_altitude >= CONFIG['max_sun_altitude']:
                    waiting_for.append('Sun < {:.1f} deg'.format(CONFIG['max_sun_altitude']))

                print('AutoFlat: {:.1f} > {:.1f}; dome {} - keep waiting'.format(
                    sun_altitude, CONFIG['max_sun_altitude'], self.dome_is_open))
            else:
                if sun_altitude > CONFIG['max_sun_altitude']:
                    print('AutoFlat: Sun already above maximum altitude')
                    log.info(self.log_name, 'AutoFlat: Sun already above maximum altitude')
                    self.status = TelescopeActionStatus.Complete
                    return

                if sun_altitude > CONFIG['min_sun_altitude'] and self.dome_is_open:
                    break

                if not self.dome_is_open:
                    waiting_for.append('Dome')

                if sun_altitude < CONFIG['min_sun_altitude']:
                    waiting_for.append('Sun > {:.1f} deg'.format(CONFIG['min_sun_altitude']))

                print('AutoFlat: {:.1f} < {:.1f}; dome {} - keep waiting'.format(
                    sun_altitude, CONFIG['min_sun_altitude'], self.dome_is_open))

            self.set_task('Waiting for ' + ', '.join(waiting_for))
            with self._wait_condition:
                self._wait_condition.wait(CONFIG['sun_altitude_check_interval'])

        self.set_task('Slewing to antisolar point')

        # The anti-solar point is opposite the sun at 75 degrees
        sun_altaz = sun_position(location)
        print('AutoFlat: Sun position is', sun_altaz)

        if not tel_slew_altaz(self.log_name,
                              math.radians(75),
                              math.radians(sun_altaz[1] + 180),
                              False, SLEW_TIMEOUT):
            if not self.aborted:
                print('AutoFlat: Failed to slew telescope')
                log.error(self.log_name, 'AutoFlat: Failed to slew telescope')
                self.status = TelescopeActionStatus.Error
                return

        # Last chance to bail out before starting the main logic
        if self.aborted:
            self.status = TelescopeActionStatus.Complete
            return

        # Configure pipeline and camera for flats
        # Archiving will be enabled when the brightness is inside the required range
        pipeline_config = {}
        pipeline_config.update(self.config['pipeline'])
        pipeline_config.update({
            'intstats': True,
            'type': 'FLAT',
        })

        if not configure_pipeline(self.log_name, pipeline_config):
            self.status = TelescopeActionStatus.Error
            return

        # Take an initial bias frame for calibration
        # This starts the autoflat logic, which is run
        # in the received_frame callbacks
        for arm in self._instrument_arms.values():
            arm.start()

        # Wait until complete
        while True:
            with self._wait_condition:
                self._wait_condition.wait(5)

            codes = ''
            for arm in self._instrument_arms.values():
                arm.check_timeout()
                codes += AutoFlatState.Codes[arm.state]

            self.set_task('Acquiring (' + ''.join(codes) + ')')
            if self.aborted:
                break

            if not self.dome_is_open:
                for arm in self._instrument_arms.values():
                    arm.abort()

                print('AutoFlat: Dome has closed')
                log.error(self.log_name, 'AutoFlat: Dome has closed')
                break

            # We are done once all arms are either complete or have errored
            if all([arm.state >= AutoFlatState.Complete for arm in self._instrument_arms.values()]):
                break

        success = self.dome_is_open and all([arm.state == AutoFlatState.Complete
                                             for arm in self._instrument_arms.values()])

        if self.aborted or success:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def abort(self):
        """Aborted by a weather alert or user action"""
        super().abort()
        for arm in self._instrument_arms.values():
            arm.abort()

        with self._wait_condition:
            self._wait_condition.notify_all()

    def received_frame(self, headers):
        """Callback to process an acquired frame. headers is a dictionary of header keys"""
        if 'INSTRARM' in headers and headers['INSTRARM'] in self._instrument_arms:
            self._instrument_arms[headers['INSTRARM']].received_frame(headers)
        else:
            print('AutoFlat: Ignoring unknown frame')
