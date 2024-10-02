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

"""Telescope action to measure a pointing model point at a given alt az"""

# pylint: disable=too-many-branches

import threading
from astropy import wcs
from astropy.time import Time
import astropy.units as u
from rockit.common import validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .camera_helpers import cameras, cam_take_images
from .coordinate_helpers import altaz_to_radec
from .mount_helpers import mount_slew_radec, mount_stop, mount_add_pointing_model_point
from .pipeline_helpers import configure_pipeline
from .schema_helpers import camera_science_schema

# Amount of time to allow for readout + object detection + wcs solution
# Consider the frame lost if this is exceeded
MAX_PROCESSING_TIME = 45 * u.s
MEASUREMENT_ATTEMPTS = 5


class Progress:
    Slewing, Measuring = range(2)


class PointingModelPointing(TelescopeAction):
    """
    Telescope action to measure a pointing model point at a given alt az

    Example block:
    {
        "type": "PointingModelPointing",
        "alt": 50,
        "az": 180,
        "refx": 4800,
        "refy": 3211,
        "camera": "cam1",
        "cam1": { # Must match "camera"
            "exposure": 1,
            "window": [1, 9600, 1, 6422] # Optional: defaults to full-frame
            # Also supports optional temperature, gain, offset, stream (advanced options)
        }
    }
    """
    def __init__(self, **args):
        super().__init__('Pointing Model', **args)
        self._wait_condition = threading.Condition()
        self._progress = Progress.Slewing
        self._wcs_status = WCSStatus.Inactive
        self._wcs = None
        self._camera_id = self.config['camera']
        self._measurement_attempt = 0

    def task_labels(self):
        """Returns list of tasks to be displayed in the schedule table"""
        tasks = []
        if self._progress <= Progress.Slewing:
            tasks.append(f'Slew to alt {round(self.config["alt"])}\u00B0, az {round(self.config["az"])}\u00B0')

        if self._progress <= Progress.Measuring:
            label = f'Measure position using {self._camera_id}'
            if self._measurement_attempt > 0:
                label += f' (attempt {self._measurement_attempt} / {MEASUREMENT_ATTEMPTS})'
            tasks.append(label)

        return tasks

    def run_thread(self):
        """Thread that runs the hardware actions"""
        ra, dec = altaz_to_radec(self.site_location, self.config['alt'], self.config['az'])
        if not mount_slew_radec(self.log_name, ra, dec, True):
            self.status = TelescopeActionStatus.Complete
            return

        if not self.dome_is_open:
            self.status = TelescopeActionStatus.Complete
            return

        self._progress = Progress.Measuring

        # Take a frame to solve field center
        pipeline_config = {
            'wcs': True,
            'type': 'JUNK',
            'object': 'WCS',
        }

        if not configure_pipeline(self.log_name, pipeline_config, quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        cam_config = self.config[self._camera_id].copy()
        cam_config['stream'] = False

        self._measurement_attempt = 1
        while not self.aborted and self.dome_is_open:
            self._wcs = None
            self._wcs_status = WCSStatus.WaitingForWCS

            print('PointingModelPointing: taking image')
            if not cam_take_images(self.log_name, self.config['camera'], 1, cam_config, quiet=True):
                self.status = TelescopeActionStatus.Error
                return

            # Wait for new frame
            expected_complete = Time.now() + cam_config['exposure'] * u.s + MAX_PROCESSING_TIME

            while True:
                with self._wait_condition:
                    remaining = (expected_complete - Time.now()).to(u.second).value
                    if remaining < 0 or self._wcs_status != WCSStatus.WaitingForWCS:
                        break

                    self._wait_condition.wait(max(remaining, 1))

            failed = self._wcs_status == WCSStatus.WCSFailed
            timeout = self._wcs_status == WCSStatus.WaitingForWCS
            self._wcs_status = WCSStatus.Inactive

            if failed or timeout:
                attempt_label = f'{self._measurement_attempt} / {MEASUREMENT_ATTEMPTS}'
                if failed:
                    print('PointingModelPointing: WCS failed for attempt ' + attempt_label)
                else:
                    print('PointingModelPointing: WCS timed out for attempt ' + attempt_label)

                if self._measurement_attempt == MEASUREMENT_ATTEMPTS:
                    self.status = TelescopeActionStatus.Complete
                    return

                self._measurement_attempt += 1

            actual_ra, actual_dec = self._wcs.all_pix2world(self.config['refx'], self.config['refy'],
                                                            1, ra_dec_order=True)

            mount_add_pointing_model_point(self.log_name, actual_ra.item(), actual_dec.item())
            self.status = TelescopeActionStatus.Complete
            break

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()
        mount_stop(self.log_name)

        with self._wait_condition:
            self._wait_condition.notify_all()

    def dome_status_changed(self, dome_is_open):
        """Notification called when the dome is fully open or fully closed"""
        super().dome_status_changed(dome_is_open)

        with self._wait_condition:
            self._wait_condition.notify_all()

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        if headers.get('CAMID', '').lower() != self.config['camera']:
            return

        with self._wait_condition:
            if self._wcs_status == WCSStatus.WaitingForWCS:
                if 'CRVAL1' in headers:
                    self._wcs = wcs.WCS(headers)
                    self._wcs_status = WCSStatus.WCSComplete
                else:
                    self._wcs_status = WCSStatus.WCSFailed

                self._wait_condition.notify_all()

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': ['alt', 'az', 'refx', 'refy', 'camera'],
            'properties': {
                'type': {'type': 'string'},
                'alt': {
                    'type': 'number',
                    'minimum': 0,
                    'maximum': 90
                },
                'az': {
                    'type': 'number',
                    'minimum': 0,
                    'maximum': 360
                },
                'refx': {
                    'type': 'number',
                    'minimum': 1,
                    'maximum': 9600
                },
                'refy': {
                    'type': 'number',
                    'minimum': 1,
                    'maximum': 6422
                },
                'camera': {
                    'type': 'string',
                    'enum': cameras.keys()
                }
            },
            'anyOf': []
        }

        for camera_id in cameras:
            schema['properties'][camera_id] = camera_science_schema()
            schema['anyOf'].append({
                'properties': {
                    'camera': {
                        'enum': [camera_id]
                    },
                    camera_id: camera_science_schema()
                },
                'required': [camera_id]
            })

        return validation.validation_errors(config_json, schema)


class WCSStatus:
    Inactive, WaitingForWCS, WCSFailed, WCSComplete = range(4)
