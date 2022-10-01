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

"""Base logic for observe_*_field telescope actions"""

# pylint: disable=no-self-use
# pylint: disable=too-many-branches

import threading
from astropy.time import Time
import jsonschema
from warwick.observatory.operations import TelescopeAction, TelescopeActionStatus
from warwick.observatory.camera.qhy import CameraStatus
from .camera_helpers import cameras, cam_status, cam_stop, cam_take_images
from .mount_helpers import mount_stop
from .pipeline_helpers import configure_pipeline
from .schema_helpers import pipeline_science_schema, camera_science_schema

CAM_STOP_TIMEOUT = 10
LOOP_INTERVAL = 30


class ObserveFieldBase(TelescopeAction):
    """
    Base field observation logic that is inherited by other telescope actions.
    Should not be scheduled directly.
    """
    def __init__(self, action_name, log_name, config):
        super().__init__(action_name, log_name, config)
        self._start_date = Time(config['start'])
        self._end_date = Time(config['end'])
        self._wait_condition = threading.Condition()
        self._camera_ids = [c for c in cameras if c in self.config]

    def slew_to_field(self):
        """
        Implemented by subclasses to move the mount to the target
        :return: True on success, false on failure
        """
        return False

    def run_thread(self):
        """Thread that runs the hardware actions"""
        # Configure pipeline immediately so the dashboard can show target name etc
        pipeline_config = self.config['pipeline'].copy()
        pipeline_config['type'] = 'SCIENCE'
        if 'archive' not in pipeline_config:
            pipeline_config['archive'] = [camera_id.upper() for camera_id in self._camera_ids]

        if not configure_pipeline(self.log_name, pipeline_config, quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        if Time.now() < self._start_date:
            self.set_task(f'Waiting until {self._start_date.strftime("%H:%M:%S")}')
            self.wait_until_time_or_aborted(self._start_date, self._wait_condition)

        if Time.now() < self._end_date:
            self.set_task('Slewing to field')
            if not self.slew_to_field():
                print('failed to slew to field')
                self.status = TelescopeActionStatus.Error
                return

        while Time.now() < self._end_date and not self.aborted:
            # Monitor cameras and dome status
            active = self.dome_is_open or not self.config.get('onsky', True)
            if active:
                self.set_task(f'Observing until {self._end_date.strftime("%H:%M:%S")}')
            else:
                self.set_task(f'Waiting until {self._end_date.strftime("%H:%M:%S")}')

            for camera_id in self._camera_ids:
                status = cam_status(self.log_name, camera_id)
                if status is None:
                    continue

                if status['state'] in [CameraStatus.Acquiring, CameraStatus.Reading] and not active:
                    cam_stop(self.log_name, camera_id, CAM_STOP_TIMEOUT)
                elif status['state'] == CameraStatus.Idle and active:
                    cam_take_images(self.log_name, camera_id, 0, self.config[camera_id])

            with self._wait_condition:
                self._wait_condition.wait(LOOP_INTERVAL)

        for camera_id in self._camera_ids:
            cam_stop(self.log_name, camera_id)

        mount_stop(self.log_name)
        self.status = TelescopeActionStatus.Complete

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        print('got frame from ' + headers.get('CAMID', 'UNKNOWN'))

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

    @classmethod
    def config_schema(cls):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': ['start', 'end', 'pipeline'],
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
                'pipeline': pipeline_science_schema(),
                'onsky': {'type': 'boolean'}  # optional
            }
        }

        for camera_id in cameras:
            schema['properties'][camera_id] = camera_science_schema()

        return schema

    @classmethod
    def validate_config(cls, config_json):
        return [jsonschema.exceptions.SchemaError('ObserveFieldBase cannot be scheduled directly')]
