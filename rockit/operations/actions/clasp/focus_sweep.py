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

"""Telescope action to do a focus sweep with one camera on a defined field"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

import threading
from astropy.coordinates import SkyCoord
from astropy.time import Time
import astropy.units as u
from rockit.common import validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .camera_helpers import cameras, cam_take_images, cam_stop
from .coordinate_helpers import zenith_radec
from .focus_helpers import focus_get, focus_set
from .mount_helpers import mount_slew_radec, mount_stop
from .pipeline_helpers import configure_pipeline
from .schema_helpers import camera_science_schema, pipeline_junk_schema

# Number of seconds to add to the exposure time to account for readout + object detection
# Consider the frame lost if this is exceeded
MAX_PROCESSING_TIME = 20

LOOP_INTERVAL = 5


class Progress:
    Waiting, Slewing, Focusing = range(3)


class FocusSweep(TelescopeAction):
    """
    Telescope action to do a focus sweep with one camera on a defined field

    Example block:
    {
        "type": "FocusSweep",
        "start": "2022-09-18T22:20:00", # Optional: defaults to immediately
        "expires": "2022-09-18T22:30:00", # Optional: defaults to never
        "ra": 0, # Optional: defaults to zenith
        "dec": -4.5, # Optional: defaults to zenith
        "min": 1000,
        "max": 2001,
        "step": 100,
        "camera": "cmos",
        "cmos": { # Must match "camera"
            "exposure": 1,
            "window": [1, 9600, 1, 6422] # Optional: defaults to full-frame
            # Also supports optional temperature, gain, offset, stream (advanced options)
        },
        "swir": { # Must match "camera"
            "exposure": 1,
            # Also supports optional temperature (advanced options)
        },
        "pipeline": {
           "prefix": "focussweep",
           "archive": ["CMOS"] # Optional: defaults to "camera"
           # Also supports optional subdirectory (advanced option)
       }
    }
    """
    def __init__(self, **args):
        super().__init__('Focus Sweep', **args)
        self._wait_condition = threading.Condition()
        self._progress = Progress.Waiting
        self._focus_measurements = {}

        if 'start' in self.config:
            self._start_date = Time(self.config['start'])
        else:
            self._start_date = None

        if 'expires' in self.config:
            self._expires_date = Time(self.config['expires'])
        else:
            self._expires_date = None

        self._camera_id = self.config['camera']

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
            ra = self.config.get('ra', None)
            dec = self.config.get('dec', None)
            if ra and dec:
                coord = SkyCoord(ra=ra, dec=dec, unit=u.deg)
                tasks.append(f'Slew to {coord.to_string("hmsdms", sep=":", precision=0)}')
            else:
                tasks.append('Slew to zenith')

        if self._progress < Progress.Focusing:
            tasks.append(f'Run Focus Sweep ({self._camera_id})')
        elif self._progress == Progress.Focusing:
            count = int((self.config['max'] - self.config['min']) / self.config['step'])
            tasks.append(f'Run Focus Sweep ({self._camera_id}; {len(self._focus_measurements) + 1} / {count})')

        return tasks

    def run_thread(self):
        """Thread that runs the hardware actions"""

        pipeline_config = self.config['pipeline'].copy()
        if 'archive' not in pipeline_config:
            pipeline_config['archive'] = [self._camera_id.upper()]

        pipeline_config.update({
            'type': 'SCIENCE',
            'hfd': True
        })

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

        # Fall back to zenith if coords not specified
        zenith_ra, zenith_dec = zenith_radec(self.site_location)
        ra = self.config.get('ra', zenith_ra)
        dec = self.config.get('dec', zenith_dec)

        self._progress = Progress.Slewing
        if not mount_slew_radec(self.log_name, ra, dec, True):
            self.status = TelescopeActionStatus.Error
            return

        self._progress = Progress.Focusing

        # Record the initial focus so we can return on error
        initial_focus = focus_get(self.log_name, self._camera_id)
        if initial_focus is None:
            mount_stop(self.log_name)
            self.status = TelescopeActionStatus.Error
            return

        # Move focuser to the start of the focus range
        current_focus = self.config['min']
        if not focus_set(self.log_name, self._camera_id, current_focus):
            mount_stop(self.log_name)
            self.status = TelescopeActionStatus.Error
            return

        # Configure the camera then take the first exposure to start the process
        camera_config = self.config[self._camera_id].copy()

        # The current QHY firmware adds an extra exposure time's delay
        # before returning the first frame. Use the single frame mode instead!
        camera_config['stream'] = False

        if not cam_take_images(self.log_name, self._camera_id, 1, camera_config):
            mount_stop(self.log_name)
            self.status = TelescopeActionStatus.Error
            return

        expected_next_exposure = Time.now() + (camera_config['exposure'] + MAX_PROCESSING_TIME) * u.s

        while True:
            # The wait period rate limits the camera status check
            # The frame received callback will wake this up immediately
            with self._wait_condition:
                self._wait_condition.wait(LOOP_INTERVAL)

            if self.aborted:
                break

            # The last measurement has finished - move on to the next
            if current_focus in self._focus_measurements:
                current_focus += self.config['step']
                if current_focus > self.config['max']:
                    break

                if not focus_set(self.log_name, self._camera_id, current_focus):
                    mount_stop(self.log_name)
                    self.status = TelescopeActionStatus.Error
                    return

                if not cam_take_images(self.log_name, self._camera_id, 1, camera_config):
                    mount_stop(self.log_name)
                    self.status = TelescopeActionStatus.Error
                    return

                expected_next_exposure = Time.now() + (camera_config['exposure'] + MAX_PROCESSING_TIME) * u.s

            elif Time.now() > expected_next_exposure:
                print('Exposure timed out - retrying')
                if not cam_take_images(self.log_name, self._camera_id, 1, camera_config):
                    mount_stop(self.log_name)
                    self.status = TelescopeActionStatus.Error
                    return

                expected_next_exposure = Time.now() + (camera_config['exposure'] + MAX_PROCESSING_TIME) * u.s

        mount_stop(self.log_name)
        if not focus_set(self.log_name, self._camera_id, initial_focus):
            self.status = TelescopeActionStatus.Error
            return

        self.status = TelescopeActionStatus.Complete

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()

        mount_stop(self.log_name)
        cam_stop(self.log_name, self._camera_id)

        with self._wait_condition:
            self._wait_condition.notify_all()

    def dome_status_changed(self, dome_is_open):
        """Notification called when the dome is fully open or fully closed"""
        super().dome_status_changed(dome_is_open)

        with self._wait_condition:
            self._wait_condition.notify_all()

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        if headers.get('CAMID', '').lower() != self._camera_id:
            return

        with self._wait_condition:
            if 'MEDHFD' in headers and 'HFDCNT' in headers and 'TELFOC' in headers:
                print('got hfd', headers['MEDHFD'], 'from', headers['HFDCNT'], 'sources')
                self._focus_measurements[headers['TELFOC']] = (headers['MEDHFD'], headers['HFDCNT'])
            else:
                print('Headers are missing MEDHFD, HFDCNT, or TELFOC')
                print(headers)

            self._wait_condition.notify_all()

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': ['min', 'max', 'step', 'camera', 'pipeline'],
            'properties': {
                'type': {'type': 'string'},
                'ra': {
                    'type': 'number',
                    'minimum': 0,
                    'maximum': 360
                },
                'dec': {
                    'type': 'number',
                    'minimum': -90,
                    'maximum': 90
                },
                'min': {
                    'type': 'integer',
                    'minimum': -10000,
                    'maximum': 10000
                },
                'max': {
                    'type': 'integer',
                    'minimum': -10000,
                    'maximum': 10000
                },
                'step': {
                    'type': 'integer',
                    'minimum': 0
                },
                'start': {
                    'type': 'string',
                    'format': 'date-time',
                },
                'expires': {
                    'type': 'string',
                    'format': 'date-time',
                },
                'pipeline': pipeline_junk_schema(),
                'camera': {
                    'type': 'string',
                    'enum': list(cameras.keys())
                }
            },
            'dependencies': {
                'ra': ['dec'],
                'dec': ['ra']
            },
            'anyOf': []
        }

        for camera_id in cameras:
            schema['properties'][camera_id] = camera_science_schema(camera_id)
            schema['anyOf'].append({
                'properties': {
                    'camera': {
                        'enum': [camera_id]
                    },
                    camera_id: camera_science_schema(camera_id)
                },
                'required': [camera_id]
            })

        return validation.validation_errors(config_json, schema)
