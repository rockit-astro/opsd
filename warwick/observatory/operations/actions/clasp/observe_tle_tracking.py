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

"""Telescope action to observe a target defined by a Two Line Element orbit"""

# pylint: disable=too-many-branches

import threading

from astropy.time import Time
import astropy.units as u
from skyfield.sgp4lib import EarthSatellite
from skyfield.api import Loader, Topos

from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from warwick.observatory.common import validation
from warwick.observatory.pipeline import configure_standard_validation_schema as pipeline_schema
from warwick.observatory.camera.qhy import configure_validation_schema as qhy_camera_schema
from .mount_helpers import mount_track_tle, mount_stop, mount_status
from .camera_helpers import cameras, cam_take_images, cam_stop
from .pipeline_helpers import configure_pipeline

TLE_ACQUIRE_TIMEOUT = 60

# Note: pipeline and camera schemas are inserted in the validate_config method
CONFIG_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['tle', 'start', 'end'],
    'properties': {
        'type': {'type': 'string'},
        'tle': {
            'type': 'array',
            'maxItems': 3,
            'minItems': 3,
            'items': [
                {
                    'type': 'string',
                },
                {
                    'type': 'string',
                },
                {
                    'type': 'string',
                }
            ]
        },
        'start': {
            'type': 'string',
            'format': 'date-time',
        },
        'end': {
            'type': 'string',
            'format': 'date-time',
        },
        'onsky': {'type': 'boolean'}  # optional
    }
}


class ObserveTLETracking(TelescopeAction):
    """Telescope action to observe a satellite tracking its TLE"""
    def __init__(self, log_name, config):
        super().__init__('Observe TLE', log_name, config)
        self._wait_condition = threading.Condition()

        # TODO: Validate that end > start
        self._start_date = Time(config['start'])
        self._end_date = Time(config['end'])

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {}
        schema.update(CONFIG_SCHEMA)
        schema['properties']['pipeline'] = pipeline_schema()
        for camera_id in cameras:
            schema['properties'][camera_id] = qhy_camera_schema(camera_id)
        return validation.validation_errors(config_json, schema)

    def __set_failed_status(self):
        """Sets self.status to Complete if aborted otherwise Error"""
        if self.aborted:
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def __wait_until_or_aborted(self, target_time):
        """
        Wait until a specified time or the action has been aborted
        :param target: Astropy time to wait for
        :return: True if the time has been reached, false if aborted
        """
        while True:
            remaining = target_time - Time.now()
            if remaining < 0 or self.aborted or not self.dome_is_open:
                break

            with self._wait_condition:
                self._wait_condition.wait(min(10, remaining.to(u.second).value))

        return not self.aborted and self.dome_is_open

    def run_thread(self):
        """Thread that runs the hardware actions"""
        # Configure pipeline immediately so the dashboard can show target name etc
        if not configure_pipeline(self.log_name, self.config.get('pipeline', {}), quiet=True):
            self.__set_failed_status()
            return

        self.set_task('Waiting for observation start')
        self.__wait_until_or_aborted(self._start_date)
        if self.aborted or Time.now() > self._end_date:
            self.status = TelescopeActionStatus.Complete
            return

        # Make sure the target is above the horizon
        status = mount_status(self.log_name)
        observer = Topos(
            f'{status["site_latitude"]} N',
            f'{status["site_longitude"]} E',
            elevation_m=status['site_elevation'])

        target = EarthSatellite(
            self.config['tle'][1],
            self.config['tle'][2],
            name=self.config['tle'][0])

        timescale = Loader('/var/tmp').timescale()

        while not self.aborted:
            now = Time.now()
            if now > self._end_date:
                break

            alt, *_ = (target - observer).at(timescale.from_astropy(now)).altaz()
            if alt.to(u.deg) > 10 * u.deg:
                break

            print(f'Target alt is {alt}')
            self.set_task('Waiting for target to rise')
            with self._wait_condition:
                self._wait_condition.wait(5)

        require_onsky = self.config.get('onsky', True)
        if require_onsky and not self.dome_is_open:
            self.set_task('Waiting for dome')
            while not self.dome_is_open and Time.now() <= self._end_date and not self.aborted:
                with self._wait_condition:
                    self._wait_condition.wait(5)

        if self.aborted or Time.now() > self._end_date:
            self.status = TelescopeActionStatus.Complete
            return

        # Note: from this point we'll keep observing, even if the dome closes mid-pass.
        # This keeps the action simple, and we already expect the person reducing the data
        # to have to manually discard frames blocked by the dome walls, W1m dome, etc.
        self.set_task('Acquiring target')
        if not mount_track_tle(self.log_name, self.config['tle'], TLE_ACQUIRE_TIMEOUT):
            print('failed to track target')
            self.__set_failed_status()
            return

        # Start science observations
        for camera_id in cameras:
            if camera_id in self.config:
                cam_take_images(self.log_name, camera_id, 0, self.config[camera_id])

        self.set_task(f'Ends {self._end_date.strftime("%H:%M:%S")}')

        # Wait until the target sets or the requested end time
        while True:
            if self.aborted or Time.now() > self._end_date:
                break

            status = mount_status(self.log_name)
            if status.get('alt', 0) < 10:
                break

            with self._wait_condition:
                self._wait_condition.wait(5)

        mount_stop(self.log_name)
        for camera_id in cameras:
            if camera_id in self.config:
                cam_stop(self.log_name, camera_id)

        self.status = TelescopeActionStatus.Complete

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()

        # Note: mount and cameras will be stopped by the run thread
        with self._wait_condition:
            self._wait_condition.notify_all()

    def dome_status_changed(self, dome_is_open):
        """Notification called when the dome is fully open or fully closed"""
        super().dome_status_changed(dome_is_open)

        with self._wait_condition:
            self._wait_condition.notify_all()
