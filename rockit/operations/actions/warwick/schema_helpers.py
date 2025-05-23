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

from .camera_helpers import FOCUS_OFFSETS

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


def camera_science_schema():
    """Schema block for QHY cameras"""
    return {
        'type': 'object',
        'additionalProperties': False,
        'required': ['exposure'],
        'properties': {
            'exposure': {
                'type': 'number',
                'minimum': 0
            },
            'filter': {
                "type": "string",
                "enum": list(FOCUS_OFFSETS.keys())
            },
            'window': {
                'type': 'array',
                'maxItems': 4,
                'minItems': 4,
                'items': [
                    {
                        'type': 'number',
                        'minimum': 1,
                        'maximum': 9600,
                    },
                    {
                        'type': 'number',
                        'minimum': 1,
                        'maximum': 9600,
                    },
                    {
                        'type': 'number',
                        'minimum': 1,
                        'maximum': 6422,
                    },
                    {
                        'type': 'number',
                        'minimum': 1,
                        'maximum': 6422,
                    },
                ]
            },
            'bin': {
                'type': 'number',
                'minimum': 1,
                'maximum': 9600,
            },
            'bin_method': {
                'type': 'string',
                'enum': ['sum', 'mean']
            },
            'temperature': {
                'type': 'number',
                'minimum': -20,
                'maximum': 30,
            },
            'gain': {
                'type': 'integer',
                'minimum': 0,
                'maximum': 100,
            },
            'offset': {
                'type': 'integer',
                'minimum': 0,
                'maximum': 1000,
            },
            'stream': {
                'type': 'boolean'
            }
        }
    }


def camera_flat_schema():
    """Flat-specific schema block for QHY cameras"""
    schema = camera_science_schema()

    # Exposure is calculated dynamically
    schema['properties'].pop('exposure')
    schema['required'].remove('exposure')

    # Streaming is force-disabled as images are processed one-by-one
    schema['properties'].pop('stream')

    # Filters are defined in their own block
    schema['properties'].pop('filter')

    return schema
