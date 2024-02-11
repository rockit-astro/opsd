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

from .camera_helpers import cameras


def pipeline_science_schema():
    """Schema block for science actions"""
    return {
        'type': 'object',
        'additionalProperties': False,
        'required': ['prefix', 'object'],
        'properties': {
            'subdirectory': {'type': 'string'},
            'prefix': {'type': 'string'},
            'object': {'type': 'string'},
            'archive': {
                'type': 'array',
                'items': {
                    'type': 'string',
                    'enum': [camera_id.upper() for camera_id in cameras]
                }
            }

            # NOTE: wcs, intstats, hfd, guide are considered internal properties
            # that cannot be set through the json definitions.
        }
    }


def pipeline_junk_schema():
    """Schema block for junk actions"""
    return {
        'type': 'object',
        'additionalProperties': False,
        'required': ['prefix'],
        'properties': {
            'subdirectory': {'type': 'string'},
            'prefix': {'type': 'string'},
            'archive': {
                'type': 'array',
                'items': {
                    'type': 'string',
                    'enum': [camera_id.upper() for camera_id in cameras]
                }
            }
        }
    }


def pipeline_flat_schema():
    """Schema block for skyflat actions"""
    return {
        'type': 'object',
        'additionalProperties': False,
        'required': ['prefix'],
        'properties': {
            'subdirectory': {'type': 'string'},
            'prefix': {'type': 'string'},
        }
    }


def camera_science_schema(camera_id):
    """Schema block for QHY cameras"""
    if camera_id == 'red':
        ccd_width_with_overscan = 2088
    else:
        ccd_width_with_overscan = 2048

    return {
        'type': 'object',
        'additionalProperties': False,
        'required': ['exposure'],
        'properties': {
            'temperature': {
                'type': 'number',
                'minimum': -80,
                'maximum': 0,
            },
            'cooler': {
                'type': 'boolean'
            },
            'shutter': {
                'type': 'boolean'
            },
            'gainindex': {
                'type': 'integer',
                'minimum': 0,
                'maximum': 2
            },
            'readoutindex': {
                'type': 'integer',
                'minimum': 0,
                'maximum': 3
            },
            'exposure': {
                'type': 'number',
                'minimum': 0
            },
            'delay': {
                'type': 'number',
                'minimum': 0
            },
            'bin': {
                'type': 'array',
                'minItems': 2,
                'maxItems': 2,
                'items': {
                    'type': 'integer',
                    'minimum': 1
                }
            },
            'window': {
                'type': 'array',
                'minItems': 4,
                'maxItems': 4,
                'items': [
                    {
                        'type': 'integer',
                        'minimum': 1,
                        'maximum': ccd_width_with_overscan
                    },
                    {
                        'type': 'integer',
                        'minimum': 1,
                        'maximum': ccd_width_with_overscan
                    },
                    {
                        'type': 'integer',
                        'minimum': 1,
                        'maximum': 2048
                    },
                    {
                        'type': 'integer',
                        'minimum': 1,
                        'maximum': 2048
                    }
                ]
            }
        }
    }


def camera_flat_schema(camera_id):
    """Flat-specific schema block for Andor cameras"""
    schema = camera_science_schema(camera_id)

    # Exposure is calculated dynamically
    schema['required'].remove('exposure')
    schema['properties'].pop('exposure')
    schema['properties'].pop('delay')

    # Shutter is managed by the action
    schema['properties'].pop('shutter')

    return schema
