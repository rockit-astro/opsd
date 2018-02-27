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

"""Telescope action to acquire flats in the evening"""

# pylint: disable=broad-except
# pylint: disable=invalid-name
# pylint: disable=too-many-return-statements
# pylint: disable=too-few-public-methods
# pylint: disable=too-many-branches
# pylint: disable=too-many-instance-attributes
# pylint: disable=too-many-statements

import datetime
import math
import threading
import Pyro4

from warwick.observatory.common import (
    daemons,
    log)
from warwick.w1m.camera import (
    configure_validation_schema as camera_schema)
from warwick.w1m.pipeline import (
    configure_flats_validation_schema as pipeline_schema)
from warwick.w1m.telescope import CommandStatus as TelCommandStatus

from . import TelescopeAction, TelescopeActionStatus

SLEW_TIMEOUT = 120

class AutoFlatState:
    """Possible states of the AutoFlat routine"""
    Bias, Waiting, Saving, Complete, Error = range(5)
    Names = ['Bias', 'Waiting', 'Saving', 'Complete', 'Error']
    Codes = ['B', 'W', 'S', 'C', 'E']

CONFIG = {
    # Exposure fudge factor to account for changing sky brightness
    'evening_scale': 1.1,
    'dawn_scale': 0.9,

    # Clamp exposure time deltas to this range (e.g. 5 -> 15 or 5 -> 1.6)
    'max_exposure_delta': 3,

    # Exposure limits in seconds
    'min_exposure': 0.1,
    'max_exposure': 30,

    # Exposures shorter than this will have large shutter effects and will be discarded
    'min_save_exposure': 2.5,

    # Exposures with less counts than this lack the signal to noise ratio that we desire
    'min_save_counts': 20000,

    # Target flat counts to aim for
    'target_counts': 35000,

    # Delays to apply between evening flats to save shutter cycles
    # These delays are cumulative, so if the next exposure is calculated to be 0.9
    # 0.9 seconds the routine will wait 5 + 25 = 30 seconds before starting it
    'evening_exposure_delays': {
        1: 25,
        2.5: 5
    }
}

class InstrumentArm:
    """Holds arm-specific flat state"""
    def __init__(self, name, daemon, camera_config, is_evening):
        self.name = name
        self.bias = 0
        self.state = AutoFlatState.Bias
        self._daemon = daemon
        self._camera_config = camera_config
        self._updated = datetime.datetime.utcnow()
        self._last_exposure = 0
        self._is_evening = is_evening
        self._scale = CONFIG['evening_scale'] if is_evening else CONFIG['dawn_scale']
        self._start_exposure = CONFIG['min_exposure'] if is_evening else CONFIG['min_save_exposure']

    def start(self):
        self.__take_image(0, 0)

    def check_timeout(self):
        """Sets error state if more than <last exposure> + 30 seconds has elapsed
           since take_flat was called"""
        if self.state not in [AutoFlatState.Waiting, AutoFlatState.Saving]:
            return

        delta = (datetime.datetime.utcnow() - self._updated).total_seconds()
        if delta > self._last_exposure + 30:
            print(self.name + ' camera exposure timed out')
            log.error('opsd', self.name + ' camera exposure timed out')
            self.state = AutoFlatState.Error

    def __take_image(self, exposure, delay):
        """Tells the camera to take an exposure.
           if exposure is 0 then it will reset the camera
           configuration and take a bias with the shutter closed
        """
        self._updated = datetime.datetime.utcnow()
        self._last_exposure = exposure
        try:
            with self._daemon.connect() as cam:
                if exposure == 0:
                    # .configure will reset all other parameters to their default values
                    cam_config = {}
                    cam_config.update(self._camera_config)
                    cam_config.update({
                        'shutter': False,
                        'exposure': 0
                    })
                    cam.configure(cam_config)
                else:
                    cam.set_exposure_delay(delay)
                    cam.set_exposure(exposure)
                    cam.set_shutter(True)

                cam.start_sequence(1)
        except Pyro4.errors.CommunicationError:
            print('Failed to communicate with ' + self.name + ' camera daemon')
            log.error('opsd', 'Failed to communicate with ' + self.name + ' camera daemon')
            self.state = AutoFlatState.Error
        except Exception as e:
            print('Unknown error with ' + self.name + ' camera')
            print(e)
            log.error('opsd', 'Unknown error with ' + self.name + ' camera')
            self.state = AutoFlatState.Error

    def received_frame(self, headers):
        """Callback to process an acquired frame.  headers is a dictionary of header keys"""
        last_state = self.state
        delay_exposure = 0

        if self.state == AutoFlatState.Bias:
            self.bias = headers['MEDCNTS']
            print(self.name + ' bias level is {:.0f} ADU'.format(self.bias))
            log.info('opsd', '{} bias is {:.0f} ADU'.format(self.name, self.bias))

            # Take the first flat image
            self.state = AutoFlatState.Waiting
            self.__take_image(self._start_exposure, delay_exposure)

        elif self.state == AutoFlatState.Waiting or self.state == AutoFlatState.Saving:
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
            print(self.name + ' exposure {:.2f}s counts {:.0f} ADU -> {:.2f}s{}'
                  .format(exposure, counts, clamped_exposure, clamped_desc))

            log.info('opsd', 'autoflat: {} {:.2f}s {:.0f} ADU -> {:.2f}s{}'
                     .format(self.name, exposure, counts, clamped_exposure, clamped_desc))

            if self._is_evening:
                # Sky is decreasing in brightness
                # TODO: Remove this once we account for sun elevation?
                for min_exposure in CONFIG['evening_exposure_delays']:
                    if new_exposure < min_exposure and counts > CONFIG['min_save_counts']:
                        delay_exposure = CONFIG['evening_exposure_delays'][min_exposure]
                        print(self.name + ' waiting ' + str(delay_exposure) + \
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
                try:
                    with daemons.onemetre_pipeline.connect() as pipeline:
                        pipeline.set_archive(self.name, self.state == AutoFlatState.Saving)
                except Pyro4.errors.CommunicationError:
                    print('Failed to communicate with pipeline daemon')
                    log.error('opsd', 'Failed to communicate with pipeline daemon')
                    self.state = AutoFlatState.Error
                    return
                except Exception as e:
                    print('Unknown error while configuring pipeline')
                    print(e)
                    log.error('opsd', 'Unknown error while configuring pipeline')
                    self.state = AutoFlatState.Error
                    return

                print('autoflat: ' + self.name + ' ' + AutoFlatState.Names[last_state] \
                    + ' -> ' + AutoFlatState.Names[self.state])
                log.info('opsd', 'autoflat: {} arm {} -> {}'.format(
                    self.name, AutoFlatState.Names[last_state], AutoFlatState.Names[self.state]))

            if self.state != AutoFlatState.Complete:
                self.__take_image(clamped_exposure, delay_exposure)

    def abort(self):
        """Aborts any active exposures and sets the state to error"""
        if self.state == AutoFlatState.Saving:
            try:
                with self._daemon.connect() as cam:
                    cam.stop_sequence()
            except Pyro4.errors.CommunicationError:
                print('Failed to communicate with ' + self.name + ' camera daemon')
                log.error('opsd', 'Failed to communicate with ' + self.name + ' camera daemon')
            except Exception as e:
                print('Unknown error with ' + self.name + ' camera')
                print(e)
                log.error('opsd', 'Unknown error with ' + self.name + ' camera')
        self.state = AutoFlatState.Error

class SkyFlats(TelescopeAction):
    def __init__(self, config):
        super().__init__('Sky Flats', config)
        self._wait_condition = threading.Condition()

        self._instrument_arms = {
            'BLUE': InstrumentArm('BLUE',
                                  daemons.onemetre_blue_camera,
                                  self.config.get('blue', {}),
                                  self.config['evening']),
            #'RED': InstrumentArm(
            #    'RED', daemons.onemetre_red_camera, self.config['red'], self.config['evening']),
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
                'blue': camera_schema('blue'),
                'red': camera_schema('red'),
                'pipeline': pipeline_schema()
            }
        }

    def run_thread(self):
        """Thread that runs the hardware actions"""

        self.set_task('Slewing to antisolar point')
        try:
            # The anti-solar point is opposite the sun at 75 degrees
            # TODO: Calculate azimuth of sun + 180 deg
            with daemons.onemetre_telescope.connect(timeout=SLEW_TIMEOUT) as teld:
                status = teld.slew_altaz(math.radians(75), math.radians(90))
                if not self.aborted and status != TelCommandStatus.Succeeded:
                    print('Failed to slew telescope')
                    log.error('opsd', 'Failed to slew telescope')
                    self.status = TelescopeActionStatus.Error
                    return
        except Pyro4.errors.CommunicationError:
            print('Failed to communicate with telescope daemon')
            log.error('opsd', 'Failed to communicate with telescope daemon')
            self.status = TelescopeActionStatus.Error
            return
        except Exception as e:
            print('Unknown error while slewing telescope')
            print(e)
            log.error('opsd', 'Unknown error while slewing telescope')
            self.status = TelescopeActionStatus.Error
            return

        # TODO: Wait for the sun elevation to reach the target
        self.set_task('Waiting for sun')

        # Configure pipeline and camera for flats
        # Archiving will be enabled when the brightness is inside the required range
        try:
            with daemons.onemetre_pipeline.connect() as pipeline:
                pipeline_config = {}
                pipeline_config.update(self.config['pipeline'])
                pipeline_config.update({
                    'intstats': True,
                    'type': 'FLAT',
                })
                pipeline.configure(pipeline_config)
        except Pyro4.errors.CommunicationError:
            print('Failed to communicate with pipeline daemon')
            log.error('opsd', 'Failed to communicate with pipeline daemon')
            self.status = TelescopeActionStatus.Error
            return
        except Exception as e:
            print('Unknown error while configuring pipeline')
            print(e)
            log.error('opsd', 'Unknown error while configuring pipeline')
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

            # We are done once all arms are either complete or have errored
            if all([arm.state >= AutoFlatState.Complete for arm in self._instrument_arms.values()]):
                break

        success = all([arm.state == AutoFlatState.Complete
                       for arm in self._instrument_arms.values()])
        if not self.aborted and success:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def abort(self):
        """Aborted by a weather alert or user action"""
        super().abort()
        for arm in self._instrument_arms.values():
            arm.abort()

    def received_frame(self, headers):
        """Callback to process an acquired frame. headers is a dictionary of header keys"""
        if 'INSTRARM' in headers and headers['INSTRARM'] in self._instrument_arms:
            self._instrument_arms[headers['INSTRARM']].received_frame(headers)
        else:
            print('Ignoring unknown frame')