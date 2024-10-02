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

"""Telescope action to synchronise the on-sky pointing"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

import threading
from astropy.coordinates import SkyCoord
from astropy.time import Time
import astropy.units as u
from rockit.common import validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .action_helpers import FieldAcquisitionHelper
from .coordinate_helpers import zenith_radec
from .schema_helpers import camera_science_schema


LOOP_INTERVAL = 5


class Progress:
    Waiting, Acquiring = range(2)


class SyncPointing(TelescopeAction):
    """Telescope action to synchronise the on-sky pointing"""
    def __init__(self, **args):
        super().__init__('Sync Pointing', **args)
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

        self._acquisition_helper = FieldAcquisitionHelper(self)

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

        if self._progress <= Progress.Acquiring:
            ra = self.config.get('ra', None)
            dec = self.config.get('dec', None)
            if ra and dec:
                coord = SkyCoord(ra=ra, dec=dec, unit=u.deg)
                tasks.append(f'Sync to {coord.to_string("hmsdms", sep=":", precision=0)}')
            else:
                tasks.append('Sync to zenith')

        return tasks

    def run_thread(self):
        """Thread that runs the hardware actions"""
        if self._start_date is not None and Time.now() < self._start_date:
            self.wait_until_time_or_aborted(self._start_date, self._wait_condition)

        while not self.aborted and not self.dome_is_open:
            if self._expires_date is not None and Time.now() > self._expires_date:
                break

            with self._wait_condition:
                self._wait_condition.wait(LOOP_INTERVAL)

        if self.aborted or self._expires_date is not None and Time.now() > self._expires_date:
            self.status = TelescopeActionStatus.Complete
            return

        # Fall back to zenith if coords not specified
        zenith_ra, zenith_dec = zenith_radec(self.site_location)
        ra = self.config.get('ra', zenith_ra)
        dec = self.config.get('dec', zenith_dec)

        self._progress = Progress.Acquiring
        if self._acquisition_helper.acquire_field(ra, dec):
            self.status = TelescopeActionStatus.Complete
        else:
            self.status = TelescopeActionStatus.Error

    def abort(self):
        """Notification called when the telescope is stopped by the user"""
        super().abort()
        self._acquisition_helper.aborted_or_dome_status_changed()

        with self._wait_condition:
            self._wait_condition.notify_all()

    def dome_status_changed(self, dome_is_open):
        """Notification called when the dome is fully open or fully closed"""
        super().dome_status_changed(dome_is_open)
        self._acquisition_helper.aborted_or_dome_status_changed()

        with self._wait_condition:
            self._wait_condition.notify_all()

    def received_frame(self, headers):
        """Notification called when a frame has been processed by the data pipeline"""
        self._acquisition_helper.received_frame(headers)

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': ['camera'],
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
                'start': {
                    'type': 'string',
                    'format': 'date-time',
                },
                'expires': {
                    'type': 'string',
                    'format': 'date-time',
                },
                'camera': camera_science_schema()
            }
        }

        return validation.validation_errors(config_json, schema)
