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

"""Telescope action to observe a sidereally tracked field"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

import threading
from astropy.time import Time
import astropy.units as u
from rockit.common import log, validation
from rockit.operations import TelescopeAction, TelescopeActionStatus
from .action_helpers import CameraWrapper, CameraWrapperStatus, FieldAcquisitionHelper
from .mount_helpers import mount_offset_radec, mount_slew_radec, mount_stop
from .pipeline_helpers import configure_pipeline
from .schema_helpers import camera_science_schema, pipeline_science_schema

# Amount of time to wait between camera status checks while observing
CAM_CHECK_STATUS_DELAY = 10 * u.s


class ObservationStatus:
    PositionLost, OnTarget, DomeClosed, Complete, Error = range(5)


class Progress:
    Waiting, Acquiring, Observing = range(3)


class ObserveImageSequence(TelescopeAction):
    """Telescope action to observe a sidereally tracked field"""
    def __init__(self, log_name, config):
        super().__init__('Observe Image Sequence', log_name, config)
        self._wait_condition = threading.Condition()
        self._progress = Progress.Waiting

        if 'start' in config:
            self._start_date = Time(config['start'])
        else:
            self._start_date = None

        if 'expires' in config:
            self._expires_date = Time(config['expires'])
        else:
            self._expires_date = None

        self._camera = CameraWrapper(self)
        self._acquisition_helper = FieldAcquisitionHelper(self)
        self._observation_status = ObservationStatus.PositionLost
        self._sequence_index = 0

    def task_labels(self):
        """Returns list of tasks to be displayed in the schedule table"""
        tasks = []

        if self._progress <= Progress.Waiting:
            if self._start_date:
                tasks.append(f'Wait until {self._start_date.strftime("%H:%M:%S")}')
        elif not self.dome_is_open and self.config.get('onsky', True):
            tasks.append('Wait for dome')

        target_name = self.config["pipeline"]["object"]
        if self._progress <= Progress.Acquiring:
            tasks.append(f'Acquire target field for {target_name}')
            if 'blind_offset_dra' in self.config:
                dra = self.config['blind_offset_dra']
                ddec = self.config['blind_offset_ddec']
                tasks.append(f'Using blind offset: {dra:.3f}, {ddec:.3f} deg')

        for i, s in enumerate(self.config['sequence']):
            if i < self._sequence_index:
                continue

            tasks.append(f'Acquire {s["count"]} images:')
            subtasks = [
                'Filter: ' + s.get('filter', 'NONE'),
                f'Exposure time: {s["exposure"]}s'
            ]

            if self._progress == Progress.Observing and i == self._sequence_index:
                subtasks.append(f'Complete: {self._camera.completed_frames} / {self._camera.target_frames}')

            tasks.append(subtasks)

        return tasks

    def __acquire_field(self):
        if self.aborted:
            return ObservationStatus.Complete

        self._progress = Progress.Acquiring

        # Point to the requested location
        print('ObserveImageSequence: slewing to target field')
        blind_offset_dra = self.config.get('blind_offset_dra', 0)
        blind_offset_ddec = self.config.get('blind_offset_ddec', 0)
        acquisition_ra = self.config['ra'] + blind_offset_dra
        acquisition_dec = self.config['dec'] + blind_offset_ddec

        if self.config.get('onsky', True):
            if not self._acquisition_helper.acquire_field(acquisition_ra, acquisition_dec):
                return ObservationStatus.Error
        else:
            if not mount_slew_radec(self.log_name, acquisition_ra, acquisition_dec, True):
                return ObservationStatus.Error

        if blind_offset_dra != 0 or blind_offset_ddec != 0:
            print('ObserveImageSequence: Offsetting to target')
            if not mount_offset_radec(self.log_name, -blind_offset_dra, -blind_offset_ddec):
                return ObservationStatus.Error

        return ObservationStatus.OnTarget

    def __wait_for_dome(self):
        self._progress = Progress.Waiting
        mount_stop(self.log_name)
        while True:
            with self._wait_condition:
                if self.aborted:
                    return ObservationStatus.Complete

                if self.dome_is_open:
                    return ObservationStatus.PositionLost

                self._wait_condition.wait(10)

    def __observe_field(self):
        # Start science observations
        pipeline_config = self.config['pipeline'].copy()
        pipeline_config['type'] = 'SCIENCE'
        pipeline_config['archive'] = ['QHY600M']

        if not configure_pipeline(self.log_name, pipeline_config):
            return ObservationStatus.Error

        if self.aborted:
            return ObservationStatus.Complete

        if not self.dome_is_open and self.config.get('onsky', True):
            return ObservationStatus.DomeClosed

        def camera_start_args():
            camera_config = self.config['sequence'][self._sequence_index].copy()
            count = camera_config.pop('count')
            return camera_config, count

        print('ObserveImageSequence: starting science observations')
        self._camera.start(*camera_start_args())

        # Monitor observation status
        self._progress = Progress.Observing
        return_status = ObservationStatus.Complete
        while True:
            if self.aborted:
                break

            if not self.dome_is_open and self.config.get('onsky', True):
                log.error(self.log_name, 'Aborting because dome is not open')
                return_status = ObservationStatus.DomeClosed
                break

            self._camera.update()
            if self._camera.status == CameraWrapperStatus.Error:
                return_status = ObservationStatus.Error
                break

            if self._camera.status == CameraWrapperStatus.Stopped:
                self._sequence_index += 1
                if self._sequence_index == len(self.config['sequence']):
                    break

                self._camera.start(*camera_start_args())

            self.wait_until_time_or_aborted(Time.now() + CAM_CHECK_STATUS_DELAY, self._wait_condition)

        print('ObserveImageSequence: stopping science observations')
        self._camera.stop()

        while True:
            if self._camera.status in [CameraWrapperStatus.Error, CameraWrapperStatus.Stopped]:
                break

            self._camera.update()

            with self._wait_condition:
                self._wait_condition.wait(CAM_CHECK_STATUS_DELAY.to_value(u.s))

        print('ObserveTimeSeries: camera has stopped')
        return return_status

    def run_thread(self):
        """Thread that runs the hardware actions"""
        # Configure pipeline immediately so the dashboard can show target name etc
        if not configure_pipeline(self.log_name, self.config['pipeline'], quiet=True):
            self.status = TelescopeActionStatus.Error
            return

        if self._start_date is not None and Time.now() < self._start_date:
            self.wait_until_time_or_aborted(self._start_date, self._wait_condition)

        # Outer loop handles transitions between states
        # Each method call blocks, returning only when it is ready to exit or switch to a different state
        while True:
            if self._expires_date is not None and Time.now() > self._expires_date:
                self._observation_status = ObservationStatus.Complete
                break

            if self._observation_status == ObservationStatus.Error:
                print('ObserveImageSequence: status is now Error')
                break

            if self._observation_status == ObservationStatus.Complete:
                print('ObserveImageSequence: status is now Complete')
                break

            if self._observation_status == ObservationStatus.OnTarget:
                print('ObserveImageSequence: status is now OnTarget')
                self._observation_status = self.__observe_field()

            if self._observation_status == ObservationStatus.PositionLost:
                print('ObserveImageSequence: status is now PositionLost')
                self._observation_status = self.__acquire_field()

            if self._observation_status == ObservationStatus.DomeClosed:
                print('ObserveImageSequence: status is now DomeClosed')
                self._observation_status = self.__wait_for_dome()

        mount_stop(self.log_name)

        if self._observation_status == ObservationStatus.Complete:
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
        print('ObserveImageSequence: Got frame')
        self._acquisition_helper.received_frame(headers)
        self._camera.received_frame(headers)

    @classmethod
    def validate_config(cls, config_json):
        """Returns an iterator of schema violations for the given json configuration"""
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'required': ['ra', 'dec', 'pipeline', 'sequence'],
            'properties': {
                'type': {'type': 'string'},
                'start': {
                    'type': 'string',
                    'format': 'date-time'
                },
                'expires': {
                    'type': 'string',
                    'format': 'date-time'
                },
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
                'blind_offset_dra': {
                    'type': 'number'
                },
                'blind_offset_ddec': {
                    'type': 'number'
                },
                'pipeline': pipeline_science_schema(),
                'sequence': {
                    'type': 'array',
                    'minItems': 1,
                    'items': camera_science_schema()
                },
                'onsky': {'type': 'boolean'}  # optional
            }
        }

        schema['properties']['sequence']['items']['required'] += ['count']
        schema['properties']['sequence']['items']['properties']['count'] = {
            'type': 'number',
            'minimum': 0
        }

        return validation.validation_errors(config_json, schema)
