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

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

import sys
import threading
import traceback
import Pyro4
from astropy.coordinates import get_sun, EarthLocation, AltAz
from astropy.time import Time
from astropy import units as u
from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from warwick.observatory.common import log, validation
from .camera_helpers import cameras, cam_stop
from .mount_helpers import mount_status, mount_slew_altaz
from .pipeline_helpers import pipeline_enable_archiving, configure_pipeline
from .schema_helpers import pipeline_flat_schema, camera_flat_schema

LOOP_INTERVAL = 5


class SkyFlats(TelescopeAction):
    """
    Telescope action to acquire sky flats

    Example block:
    {
        "type": "SkyFlats",
        "evening": true,
        "cam<1..4>": { # Optional: cameras that aren't listed won't be used
            "window": [1, 9600, 1, 6422] # Optional: defaults to full-frame
            # Also supports optional temperature, gain, offset (advanced options)
        },
        "pipeline": {
           "prefix": "evening-flat",
           # Also supports optional subdirectory (advanced option)
       }
    }
    """
    def __init__(self, log_name, config):
        super().__init__('Sky Flats', log_name, config)
        self._wait_condition = threading.Condition()

        self._cameras = {}
        for camera_id, camera_daemon in cameras.items():
            self._cameras[camera_id] = CameraWrapper(camera_id, camera_daemon, self.config.get(camera_id, None),
                                                     self.config['evening'], self.log_name)

    def run_thread(self):
        """Thread that runs the hardware actions"""
        # Query site location from the telescope
        ms = mount_status(self.log_name)
        if not ms:
            self.status = TelescopeActionStatus.Error
            return

        # pylint: disable=no-member
        location = EarthLocation(lat=ms['site_latitude'] * u.deg,
                                 lon=ms['site_longitude'] * u.deg,
                                 height=ms['site_elevation'] * u.m)
        # pylint: enable=no-member

        # Configure pipeline immediately so the dashboard can show target name etc
        # Archiving will be enabled when the brightness is inside the required range
        pipeline_config = self.config['pipeline'].copy()
        pipeline_config.update({
            'intstats': True,
            'type': 'FLAT'
        })

        if not configure_pipeline(self.log_name, pipeline_config):
            self.status = TelescopeActionStatus.Error
            return

        while not self.aborted:
            waiting_for = []
            sun_altitude = sun_position(location)[0]
            if self.config['evening']:
                if sun_altitude < CONFIG['min_sun_altitude']:
                    log.info(self.log_name, 'AutoFlat: Sun already below minimum altitude')
                    self.status = TelescopeActionStatus.Complete
                    return

                if sun_altitude < CONFIG['max_sun_altitude'] and self.dome_is_open:
                    break

                if not self.dome_is_open:
                    waiting_for.append('Roof')

                if sun_altitude >= CONFIG['max_sun_altitude']:
                    waiting_for.append(f'Sun < {CONFIG["max_sun_altitude"]:.1f} deg')

                print(f'AutoFlat: {sun_altitude:.1f} > {CONFIG["max_sun_altitude"]:.1f}; ' +
                      f'roof {self.dome_is_open} - keep waiting')
            else:
                if sun_altitude > CONFIG['max_sun_altitude']:
                    log.info(self.log_name, 'AutoFlat: Sun already above maximum altitude')
                    self.status = TelescopeActionStatus.Complete
                    return

                if sun_altitude > CONFIG['min_sun_altitude'] and self.dome_is_open:
                    break

                if not self.dome_is_open:
                    waiting_for.append('Roof')

                if sun_altitude < CONFIG['min_sun_altitude']:
                    waiting_for.append(f'Sun > {CONFIG["min_sun_altitude"]:.1f} deg')

                print(f'AutoFlat: {sun_altitude:.1f} < {CONFIG["min_sun_altitude"]:.1f}; ' +
                      f'roof {self.dome_is_open} - keep waiting')

            self.set_task('Waiting for ' + ', '.join(waiting_for))
            with self._wait_condition:
                self._wait_condition.wait(CONFIG['sun_altitude_check_interval'])

        if self.aborted:
            self.status = TelescopeActionStatus.Complete
            return

        self.set_task('Slewing to antisolar point')

        # The anti-solar point is opposite the sun at 75 degrees
        sun_altaz = sun_position(location)
        print('AutoFlat: Sun position is', sun_altaz)

        if not mount_slew_altaz(self.log_name, 75, sun_altaz[1] + 180, False):
            if not self.aborted:
                log.error(self.log_name, 'AutoFlat: Failed to slew telescope')
                self.status = TelescopeActionStatus.Error
                return

        # Last chance to bail out before starting the main logic
        if self.aborted:
            self.status = TelescopeActionStatus.Complete
            return

        # This starts the autoflat logic, which is run
        # in the received_frame callbacks
        for camera in self._cameras.values():
            camera.start()

        # Wait until complete
        while True:
            with self._wait_condition:
                self._wait_condition.wait(LOOP_INTERVAL)

            codes = ''
            for camera in self._cameras.values():
                camera.check_timeout()
                codes += AutoFlatState.Codes[camera.state]

            self.set_task('Acquiring (' + ''.join(codes) + ')')
            if self.aborted:
                break

            if not self.dome_is_open:
                for camera in self._cameras.values():
                    camera.abort()

                log.error(self.log_name, 'AutoFlat: Roof has closed')
                break

            # We are done once all cameras are either complete or have errored
            if all(camera.state >= AutoFlatState.Complete for camera in self._cameras.values()):
                break

        if any(camera.state == AutoFlatState.Error for camera in self._cameras.values()):
            self.status = TelescopeActionStatus.Error
        else:
            self.status = TelescopeActionStatus.Complete

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()
        for camera in self._cameras.values():
            camera.abort()

        with self._wait_condition:
            self._wait_condition.notify_all()

    def dome_status_changed(self, dome_is_open):
        """Notification called when the roof is fully open or fully closed"""
        super().dome_status_changed(dome_is_open)

        with self._wait_condition:
            self._wait_condition.notify_all()

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        camera_id = headers.get('CAMID', '').lower()
        if camera_id in self._cameras:
            self._cameras[camera_id].received_frame(headers)
        else:
            print('AutoFlat: Ignoring unknown frame')

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': ['evening', 'pipeline'],
            'properties': {
                'type': {'type': 'string'},
                'evening': {'type': 'boolean'},
                'pipeline': pipeline_flat_schema()
            }
        }

        for camera_id in cameras:
            schema['properties'][camera_id] = camera_flat_schema()

        return validation.validation_errors(config_json, schema)


def sun_position(location):
    """Returns current (alt, az) of sun in degrees for the given location"""
    now = Time.now()
    frame = AltAz(obstime=now, location=location)
    sun = get_sun(now).transform_to(frame)
    return sun.alt.value, sun.az.value


class AutoFlatState:
    """Possible states of the AutoFlat routine"""
    Bias, Waiting, Saving, Complete, Error = range(5)
    Names = ['Bias', 'Waiting', 'Saving', 'Complete', 'Error']
    Codes = ['B', 'W', 'S', 'C', 'E']


class CameraWrapper:
    """Holds camera-specific flat state"""
    def __init__(self, camera_id, daemon, camera_config, is_evening, log_name):
        self.camera_id = camera_id
        self._daemon = daemon
        self._log_name = log_name
        self._camera_config = camera_config or {}
        self._expected_complete = Time.now()
        self._is_evening = is_evening
        self.state = AutoFlatState.Bias if camera_config is not None else AutoFlatState.Complete
        self._scale = CONFIG['evening_scale'] if is_evening else CONFIG['dawn_scale']
        self._start_exposure = CONFIG['min_exposure'] if is_evening else CONFIG['min_save_exposure']
        self._start_time = None
        self._exposure_count = 0
        self._bias_level = 0

    def start(self):
        """Starts the flat sequence for this camera"""
        if self.state == AutoFlatState.Complete:
            return

        with self._daemon.connect() as cam:
            config = self._camera_config.copy()

            # The current QHY firmware adds an extra exposure time's delay
            # before returning the first frame. Use the single frame mode instead!
            config['stream'] = False

            # Start by taking a full-frame image to measure the bias level,
            # as the actual flat frames may window away the overscan
            config.pop('window', None)

            cam.configure(config, quiet=True)

        self.__take_image(0)
        self._start_time = Time.now()

    def check_timeout(self):
        """Sets error state if an expected frame is more than 30 seconds late"""
        if self.state not in [AutoFlatState.Waiting, AutoFlatState.Saving]:
            return

        if Time.now() > self._expected_complete:
            log.error(self._log_name, f'AutoFlat: camera {self.camera_id} exposure timed out')
            self.state = AutoFlatState.Error

    def __take_image(self, exposure):
        """Tells the camera to take an exposure"""
        self._expected_complete = Time.now() + (exposure + CONFIG['max_processing_time']) * u.s

        try:
            # Need to communicate directly with camera daemon
            # to adjust exposure without resetting other config
            with self._daemon.connect() as cam:
                cam.set_exposure(exposure, quiet=True)
                cam.start_sequence(1, quiet=True)
        except Pyro4.errors.CommunicationError:
            log.error(self._log_name, 'Failed to communicate with camera ' + self.camera_id)
            self.state = AutoFlatState.Error
        except Exception:
            log.error(self._log_name, 'Unknown error with camera ' + self.camera_id)
            traceback.print_exc(file=sys.stdout)
            self.state = AutoFlatState.Error

    def received_frame(self, headers):
        """Callback to process an acquired frame. headers is a dictionary of header keys"""
        last_state = self.state

        if self.state == AutoFlatState.Bias:
            self._bias_level = headers['MEDBIAS']
            log.info(self._log_name, f'AutoFlat: {self.camera_id} bias is {self._bias_level:.0f} ADU')

            if 'window' in self._camera_config:
                try:
                    with self._daemon.connect() as cam:
                        cam.set_window(self._camera_config['window'], quiet=True)
                except Pyro4.errors.CommunicationError:
                    log.error(self._log_name, 'Failed to communicate with camera ' + self.camera_id)
                    self.state = AutoFlatState.Error
                    return
                except Exception:
                    log.error(self._log_name, 'Unknown error with camera ' + self.camera_id)
                    traceback.print_exc(file=sys.stdout)
                    self.state = AutoFlatState.Error
                    return

            # Take the first flat image
            self.state = AutoFlatState.Waiting
            self.__take_image(self._start_exposure)

        elif self.state in [AutoFlatState.Waiting, AutoFlatState.Saving]:
            if self.state == AutoFlatState.Saving:
                self._exposure_count += 1

            counts = headers['MEDCNTS'] - self._bias_level
            exposure = headers['EXPTIME']

            # If the count rate is too low then we scale the exposure by the maximum amount
            if counts > 0:
                new_exposure = self._scale * exposure * CONFIG['target_counts'] / counts
            else:
                new_exposure = exposure * CONFIG['max_exposure_delta']

            # Clamp the exposure to a sensible range
            clamped_exposure = min(new_exposure, CONFIG['max_exposure'], exposure * CONFIG['max_exposure_delta'])
            clamped_exposure = max(clamped_exposure, CONFIG['min_exposure'], exposure / CONFIG['max_exposure_delta'])

            clamped_desc = f' (clamped from {new_exposure:.2f}s)' if new_exposure > clamped_exposure else ''
            print(f'AutoFlat: camera {self.camera_id} exposure {exposure:.2f}s counts {counts:.0f} ADU ' +
                  f'-> {clamped_exposure:.2f}s' + clamped_desc)

            if self._is_evening:
                if clamped_exposure == CONFIG['max_exposure'] and counts < CONFIG['min_save_counts']:
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
                archive = self.state == AutoFlatState.Saving
                if not pipeline_enable_archiving(self._log_name, self.camera_id, archive):
                    self.state = AutoFlatState.Error
                    return

                print('AutoFlat: camera ' + self.camera_id + ' ' + AutoFlatState.Names[last_state] +
                      ' -> ' + AutoFlatState.Names[self.state])

                if self.state == AutoFlatState.Saving:
                    log.info(self._log_name, f'AutoFlat: {self.camera_id} saving enabled')
                elif self.state == AutoFlatState.Complete:
                    runtime = (Time.now() - self._start_time).total_seconds()
                    message = f'AutoFlat: camera {self.camera_id} acquired {self._exposure_count} flats ' + \
                              f'in {runtime:.0f} seconds'
                    log.info(self._log_name, message)

            if self.state != AutoFlatState.Complete:
                self.__take_image(clamped_exposure)

    def abort(self):
        """Aborts any active exposures and sets the state to complete"""
        if self.state == AutoFlatState.Saving:
            cam_stop(self._log_name, self.camera_id)
        self.state = AutoFlatState.Complete


CONFIG = {
    # Range of sun angles where we can acquire useful data
    'max_sun_altitude': -4,
    'min_sun_altitude': -10,
    'sun_altitude_check_interval': 30,

    # Exposure fudge factor to account for changing sky brightness
    'evening_scale': 1.07,
    'dawn_scale': 0.9,

    # Clamp exposure time deltas to this range (e.g. 5 -> 15 or 5 -> 1.6)
    'max_exposure_delta': 3,

    # Number of seconds to add to the exposure time to account for readout + object detection
    # Consider the frame lost if this is exceeded
    'max_processing_time': 20,

    # Exposure limits in seconds
    'min_exposure': 0.1,
    'max_exposure': 10,

    'min_save_exposure': 1.0,

    # Exposures with less counts than this lack the signal to noise ratio that we desire
    'min_save_counts': 15000,

    # Target flat counts to aim for
    'target_counts': 30000,
}
