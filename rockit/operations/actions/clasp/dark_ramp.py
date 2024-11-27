#
# This file is part of the Robotic Observatory Control Kit (rockit)
#
# rockit is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# rockit is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with rockit.  If not, see <http://www.gnu.org/licenses/>.

"""Telescope action to acquire a series of images with increasing exposure time pointing inside the dome"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

import threading
from astropy.time import Time
import astropy.units as u
from rockit.common import log, validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .camera_helpers import cameras, cam_take_images
from .mount_helpers import mount_slew_altaz, mount_stop
from .pipeline_helpers import configure_pipeline
from .schema_helpers import camera_science_schema, pipeline_junk_schema

CALIB_ALT = 5
CALIB_AZ = 82

LOOP_INTERVAL = 5

# Number of seconds to add to the exposure time to account for readout + processing
MAX_PROCESSING_TIME = 20

class Progress:
    Waiting, Slewing, Acquiring = range(3)


class DarkRamp(TelescopeAction):
    """
    Telescope action to acquire a series of images with increasing exposure time pointing inside the dome

    Example block:
    {
        "type": "DarkRamp",
        "start": "2022-09-18T22:20:00", # Optional: defaults to immediately
        "expires": "2022-09-18T22:30:00", # Optional: defaults to never
        "cmos": { # Optional: cameras that aren't listed won't be used
            "exposures": [0.1, ..., 10.0], # Optional
            "window": [1, 9600, 1, 6422] # Optional: defaults to full-frame
            # Also supports optional temperature, window, gain, offset, stream (advanced options)
        },
        "swir": { # Optional: cameras that aren't listed won't be focused
            "exposures": [0.1, ..., 10.0], # Optional
            # Also supports optional temperature (advanced options)
        },
        "pipeline": {
           "prefix": "dark-ramp",
           "archive": ["CMOS"] # Optional: defaults to enabled cameras
           # Also supports optional subdirectory (advanced option)
       }
    }
    """
    def __init__(self, **args):
        super().__init__('Dark Ramp', **args)
        self._wait_condition = threading.Condition()
        self._progress = Progress.Waiting

        if 'start' in self.config:
            self._start_date = Time(self.config['start'])
        else:
            self._start_date = None

        if 'expires' in self.config:
            self._expires_date = Time(self.config['expires'])
        else:
            self._expires_date = None

        self._cameras = {}
        for camera_id in cameras:
            self._cameras[camera_id] = CameraWrapper(camera_id, self.config.get(camera_id, None), self.log_name)

    def task_labels(self):
        """Returns list of tasks to be displayed in the schedule table"""
        tasks = []

        if self._progress <= Progress.Waiting:
            if self._start_date:
                tasks.append(f'Wait until {self._start_date.strftime("%H:%M:%S")}')
        elif not self.dome_is_open:
            label = 'Wait for dome'
            if self._expires_date:
                label += f' (expires {self._expires_date.strftime("%H:%M:%S")})'
            tasks.append(label)

        if self._progress <= Progress.Slewing:
            tasks.append('Slew to calibration position')

        if self._progress < Progress.Acquiring:
            camera_ids = [c.camera_id for c in self._cameras.values() if c.state != CameraWrapperState.Complete]
            tasks.append(f'Acquire Darks ({", ".join(camera_ids)})')
        else:
            tasks.append('Acquire Darks:')
            camera_state = []
            for camera_id, camera in self._cameras.items():
                if camera.state == CameraWrapperState.Active:
                    camera_state.append(f'{camera_id}: {camera.acquired} / {len(camera.exposures)} images')
                else:
                    camera_state.append(f'{camera_id}: {CameraWrapperState.Labels[camera.state]}')

            tasks.append(camera_state)

        return tasks

    def run_thread(self):
        """Thread that runs the hardware actions"""
        pipeline_config = self.config['pipeline'].copy()
        pipeline_config['type'] = 'DARK'
        if 'archive' not in pipeline_config:
            pipeline_config['archive'] = [c.upper() for c in cameras if c in self.config]

        if not configure_pipeline(self.log_name, pipeline_config):
            self.status = TelescopeActionStatus.Error
            return

        if self._start_date is not None and Time.now() < self._start_date:
            self.wait_until_time_or_aborted(self._start_date, self._wait_condition)

        while not self.aborted and not self.dome_is_open:
            if self._expires_date is not None and Time.now() > self._expires_date:
                break

            with self._wait_condition:
                self._wait_condition.wait(10)

        if self.aborted or self._expires_date is not None and Time.now() > self._expires_date:
            self.status = TelescopeActionStatus.Complete
            return

        if not mount_slew_altaz(self.log_name, CALIB_ALT, CALIB_AZ):
            self.status = TelescopeActionStatus.Error
            return

        self._progress = Progress.Acquiring

        # This starts the calibration logic, which is run on camera-specific threads
        for camera in self._cameras.values():
            camera.start()

        # Wait until complete
        while True:
            with self._wait_condition:
                self._wait_condition.wait(5)

            if self.aborted:
                break

            # We are done once all cameras are either complete or have errored
            if all(camera.state >= CameraWrapperState.Complete for camera in self._cameras.values()):
                break

        if any(camera.state == CameraWrapperState.Error for camera in self._cameras.values()):
            self.status = TelescopeActionStatus.Error
        else:
            self.status = TelescopeActionStatus.Complete

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()
        mount_stop(self.log_name)
        for camera in self._cameras.values():
            camera.abort()

        with self._wait_condition:
            self._wait_condition.notify_all()

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        camera_id = headers.get('CAMID', '').lower()
        if camera_id in self._cameras:
            self._cameras[camera_id].received_frame(headers)
        else:
            print('DarkRamp: Ignoring unknown frame')

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': [],
            'properties': {
                'type': {'type': 'string'},
                'start': {
                    'type': 'string',
                    'format': 'date-time',
                },
                'expires': {
                    'type': 'string',
                    'format': 'date-time',
                },
                'pipeline': pipeline_junk_schema()
            }
        }

        for camera_id in cameras:
            schema['properties'][camera_id] = camera_science_schema(camera_id)
            schema['properties'][camera_id]['required'].remove('exposure')
            schema['properties'][camera_id]['properties'].pop('exposure')
            schema['properties'][camera_id]['properties']['exposures'] = {
                'type': 'array',
                'items': {
                    'type': 'number',
                    'minimum': 0
                }
            }

        return validation.validation_errors(config_json, schema)


class CameraWrapperState:
    """Possible states of the CameraWrapper object"""
    Active, Aborting, Complete, Failed, Error = range(5)

    Labels = {
        0: 'Active',
        1: 'Aborting',
        2: 'Complete',
        3: 'Failed',
        4: 'Error'
    }


class CameraWrapper:
    """Holds camera-specific state"""
    def __init__(self, camera_id, camera_config, log_name):
        self.camera_id = camera_id
        self.acquired = 0

        if camera_config is not None:
            self.state = CameraWrapperState.Active

            self._camera_config = camera_config.copy()
            self.exposures = camera_config.pop('exposures', None)
            if self.exposures is None:
                self.exposures = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
            if self.camera_id == 'cmos':
                self._camera_config['stream'] = False
        else:
            self.state = CameraWrapperState.Complete

        self._log_name = log_name
        self._wait_condition = threading.Condition()

    def _run(self):
        """Thread running the main state machine"""
        expected_next_exposure = Time.now()
        last_acquired = -1
        retries = 0
        while True:
            if self.state == CameraWrapperState.Aborting:
                break

            if self.acquired > last_acquired or Time.now() > expected_next_exposure:
                if self.acquired == last_acquired:
                    retries += 1
                    if retries == 5:
                        log.error(self._log_name, f'DarkRamp: camera {self.camera_id} aborting after 5 attempts')
                        self.state = CameraWrapperState.Error
                        return
                else:
                    last_acquired = self.acquired
                    retries = 0

                if self.acquired == len(self.exposures):
                    self.state = CameraWrapperState.Complete
                    return

                self._camera_config['exposure'] = self.exposures[self.acquired]
                if not cam_take_images(self._log_name, self.camera_id, 1, self._camera_config):
                    self.state = CameraWrapperState.Error
                    return

                expected_next_exposure = Time.now() + (self._camera_config['exposure'] + MAX_PROCESSING_TIME) * u.s


            # The wait period rate limits the camera status check
            # The frame received callback will wake this up immediately
            with self._wait_condition:
                self._wait_condition.wait(LOOP_INTERVAL)


    def start(self):
        """Starts the autofocus sequence for this camera"""
        if self.state == CameraWrapperState.Complete:
            return

        threading.Thread(target=self._run).start()

    def abort(self):
        """Aborts any active exposures and sets the state to complete"""
        # Assume that focus images are always short so we can just wait for the state machine to clean up
        if self.state < CameraWrapperState.Complete:
            self.state = CameraWrapperState.Aborting

        with self._wait_condition:
            self._wait_condition.notify_all()

    def received_frame(self, _):
        """Callback to process an acquired frame. headers is a dictionary of header keys"""
        if self.state >= CameraWrapperState.Complete:
            return

        self.acquired += 1
        with self._wait_condition:
            self._wait_condition.notify_all()
