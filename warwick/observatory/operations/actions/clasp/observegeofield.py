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

"""Telescope action to observe a static HA/Dec field within a defined time window"""

import threading
from astropy.time import Time
import astropy.units as u
from warwick.observatory.common import log, validation
from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from warwick.observatory.pipeline import configure_standard_validation_schema as pipeline_schema
from warwick.observatory.camera.qhy import configure_validation_schema as qhy_camera_schema
from .camera_helpers import cameras, cam_take_images, cam_stop
from .pipeline_helpers import configure_pipeline
from .mount_helpers import mount_slew_hadec

SLEW_TIMEOUT = 120

CONFIG_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['start', 'end', 'ha', 'dec'],
    'properties': {
        'type': {'type': 'string'},
        'start': {
            'type': 'string',
            'format': 'date-time',
        },
        'end': {
            'type': 'string',
            'format': 'date-time',
        },
        'ha': {
            'type': 'number',
            'minimum': -180,
            'maximum': 180
        },
        'dec': {
            'type': 'number',
            'minimum': -30,
            'maximum': 85
        },
        'onsky': {'type': 'boolean'},  # optional
    }
}


class ObserveGEOField(TelescopeAction):
    """Telescope action to observe a static HA/Dec field within a defined time window"""
    def __init__(self, log_name, config):
        super().__init__('Observe GEO field', log_name, config)
        self._start_date = Time(config['start'])
        self._end_date = Time(config['end'])
        self._wait_condition = threading.Condition()

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
        onsky = self.config.get('onsky', True)
        while True:
            remaining = target_time - Time.now()
            if remaining < 0 or self.aborted or (onsky and not self.dome_is_open):
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

        acquire_start = Time.now()
        if acquire_start > self._end_date:
            self.status = TelescopeActionStatus.Complete
            return

        if self.config.get('onsky', True) and not self.dome_is_open:
            log.error(self.log_name, 'Aborting: dome is not open')
            self.status = TelescopeActionStatus.Error
            return

        self.set_task('Slewing to field')
        if not mount_slew_hadec(self.log_name, self.config['ha'], self.config['dec'], SLEW_TIMEOUT):
            print('failed to slew to target')
            self.__set_failed_status()
            return

        self.set_task('Ends {}'.format(self._end_date.strftime('%H:%M:%S')))

        for camera_id in cameras:
            if camera_id in self.config:
                cam_take_images(self.log_name, camera_id, 0, self.config[camera_id])

        # Keep track of things while we observe
        while True:
            with self._wait_condition:
                self._wait_condition.wait(10)

            if self.aborted or Time.now() > self._end_date:
                self.status = TelescopeActionStatus.Complete

            if self.config.get('onsky', True) and not self.dome_is_open:
                log.error(self.log_name, 'Dome is not open')
                self.status = TelescopeActionStatus.Error

            # TODO: Monitor for camera errors

            if self.status != TelescopeActionStatus.Incomplete:
                break

        for camera_id in cameras:
            if camera_id in self.config:
                cam_stop(self.log_name, camera_id)

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        print('got frame from camera ' + headers.get('CAMID', '-1'))

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()

        with self._wait_condition:
            self._wait_condition.notify_all()

    def dome_status_changed(self, dome_is_open):
        """Notification called when the dome is fully open or fully closed"""
        super().dome_status_changed(dome_is_open)

        with self._wait_condition:
            self._wait_condition.notify_all()
