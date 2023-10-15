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

"""Helper function to validate and parse the json config file"""

from importlib import import_module
import importlib.util
from inspect import isclass
import json
import sys
import traceback
import jsonschema
from skyfield.api import Topos
from rockit.common import daemons, IP, validation
from .telescope_action import TelescopeAction

CONFIG_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': [
        'daemon', 'log_name', 'control_machines', 'pipeline_machines', 'loop_delay',
        'site_latitude', 'site_longitude', 'site_elevation', 'actions_module', 'dome',
        'environment_daemon', 'environment_conditions'
    ],
    'properties': {
        'daemon': {
            'type': 'string',
            'daemon_name': True
        },
        'log_name': {
            'type': 'string',
        },
        'control_machines': {
            'type': 'array',
            'items': {
                'type': 'string',
                'machine_name': True
            }
        },
        'pipeline_machines': {
            'type': 'array',
            'items': {
                'type': 'string',
                'machine_name': True
            }
        },
        'loop_delay': {
            'type': 'number',
            'minimum': 0
        },
        'site_latitude': {
            'type': 'string',
        },
        'site_longitude': {
            'type': 'string',
        },
        'site_elevation': {
            'type': 'number',
        },
        'actions_module': {
            'type': 'string',
            'actions_module': True
        },
        'dome': {
            'dome': True
        },
        'environment_daemon': {
            'type': 'string',
            'daemon_name': True
        },
        'environment_conditions': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['label', 'sensors'],
                'properties': {
                    'label': {
                        'type': 'string'
                    },
                    'sensors': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'required': ['label', 'sensor', 'parameter'],
                            'properties': {
                                'label': {
                                    'type': 'string'
                                },
                                'sensor': {
                                    'type': 'string'
                                },
                                'parameter': {
                                    'type': 'string'
                                },
                                'unsafe_key': {
                                    'type': 'string'
                                },
                                'warning_key': {
                                    'type': 'string'
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}


class Config:
    """Daemon configuration parsed from a json file"""
    def __init__(self, config_filename):
        # Will throw on file not found or invalid json
        with open(config_filename, 'r') as config_file:
            config_json = json.load(config_file)

        # pylint: disable=unused-argument
        def __actions_module_validator(validator, value, instance, schema):
            """Validate a string as an importable python module containing the required telescope actions"""
            try:
                if not importlib.util.find_spec(instance):
                    yield jsonschema.ValidationError(f'{instance} is not a valid python module')

                module = import_module(instance)
                park_action = getattr(module, 'ParkTelescope', None)
                if not isclass(park_action) or not issubclass(park_action, TelescopeAction):
                    yield jsonschema.ValidationError(f'{instance} does not define the required ParkTelescope action')
            except Exception as e:
                yield jsonschema.ValidationError(f'{instance} exception during import: {e}')

        def __dome_validator(validator, value, instance, schema):
            """Validate a string as an importable python module containing a dome interface"""
            try:
                if 'module' not in instance:
                    yield jsonschema.ValidationError("missing key 'module'")
                    return

                if not importlib.util.find_spec(instance['module']):
                    yield jsonschema.ValidationError(f'{instance["module"]} is not a valid python module')
                    return

                module = import_module(instance['module'])
                if isclass(getattr(module, 'DomeInterface', None)):
                    for e in module.validate_config(instance):
                        yield e
                else:
                    yield jsonschema.ValidationError(f'{instance["module"]} does not define a DomeInterface class')

            except Exception as e:
                traceback.print_exc(file=sys.stdout)
                yield jsonschema.ValidationError(f'{instance} exception during import: {e}')
        # pylint: enable=unused-argument

        # Will throw on schema violations
        validation.validate_config(config_json, CONFIG_SCHEMA, {
            'daemon_name': validation.daemon_name_validator,
            'machine_name': validation.machine_name_validator,
            'actions_module': __actions_module_validator,
            'dome': __dome_validator
        }, print_exception=True)

        self.daemon = getattr(daemons, config_json['daemon'])
        self.log_name = config_json['log_name']
        self.control_ips = [getattr(IP, machine) for machine in config_json['control_machines']]
        self.pipeline_ips = [getattr(IP, machine) for machine in config_json['pipeline_machines']]
        self.loop_delay = config_json['loop_delay']
        self.site_location = Topos(config_json['site_latitude'], config_json['site_longitude'],
                                   elevation_m=config_json['site_elevation'])

        # Import all TelescopeAction subclasses defined in actions_module
        actions_module = import_module(config_json['actions_module'])
        self.actions = {}
        for name in dir(actions_module):
            action = getattr(actions_module, name)
            if isclass(action) and issubclass(action, TelescopeAction):
                self.actions[name] = action

        self.dome_json = config_json['dome']
        self.dome_interface_type = getattr(import_module(config_json['dome']['module']), 'DomeInterface', None)
        self.environment_daemon = getattr(daemons, config_json['environment_daemon'])
        self.environment_conditions = config_json['environment_conditions']
